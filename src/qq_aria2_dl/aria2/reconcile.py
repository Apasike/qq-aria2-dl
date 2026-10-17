from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any

from ..bot.urls import format_bytes
from .rpc import Aria2Client, Aria2Error

logger = logging.getLogger("qq_aria2_dl.reconcile")


class ReconcileService:
    def __init__(
        self,
        settings: Any,
        db: Any,
        aria2: Aria2Client,
        onebot: Any,
        tasks: Any,
        progress: Any,
    ) -> None:
        self.settings = settings
        self.db = db
        self.aria2 = aria2
        self.onebot = onebot
        self.tasks = tasks
        self.progress = progress
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._last_disk_check = 0.0
        self._released: set[str] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="reconcile-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        backoff = max(1.0, self.settings.aria2_poll_interval_sec)
        while not self._stopping:
            try:
                await self.run_once()
                backoff = max(1.0, self.settings.aria2_poll_interval_sec)
            except Aria2Error as exc:
                logger.warning("aria2 RPC unavailable: %s", exc)
                backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconcile loop error")
                backoff = min(backoff * 2, 60.0)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break

    async def run_once(self) -> None:
        now = time.monotonic()
        if now - self._last_disk_check >= 5:
            self._last_disk_check = now
            await self._disk_guard()

        await self.db.cleanup_dedupe()
        await self.db.delete_old_tasks(self.settings.storage_task_retention_days)

        tasks = await self.db.list_reconcile_tasks()
        if not tasks:
            return

        for task in tasks:
            gid = task["gid"]
            snap = await self.tasks.refresh_snapshot(gid)
            status = snap.aria2_status
            if status == "unknown":
                await self._handle_unknown(task)
                continue

            await self._apply_status(task, snap)
            if status in ("complete", "error", "removed"):
                await self._maybe_notify(task, snap)

    def _local_status(self, aria2_status: str) -> str:
        return {
            "active": "downloading",
            "waiting": "submitted",
            "paused": "paused",
            "complete": "completed",
            "error": "failed",
            "removed": "cancelled",
        }.get(aria2_status, aria2_status)

    async def _apply_status(self, task: dict[str, Any], snap: Any) -> None:
        gid = task["gid"]
        local = self._local_status(snap.aria2_status)
        if snap.aria2_status in ("complete", "error", "removed"):
            await self.db.finish_task(
                gid,
                local,
                error_message=snap.error_message or None,
                aria2_status=snap.aria2_status,
            )
            self._release_reservation(task)
        else:
            await self.db.set_status(
                gid, local, aria2_status=snap.aria2_status, error_message=snap.error_message or None
            )

    async def _maybe_notify(self, task: dict[str, Any], snap: Any) -> None:
        gid = task["gid"]
        if task.get("notified_at") is not None:
            return
        text = self.tasks.format_terminal_notice(task, snap)
        try:
            await self.onebot.send_message(task["chat_type"], task["chat_id"], text)
        except Exception as exc:
            await self.db.record_notify_failure(gid, str(exc))
            logger.warning(
                "notification failed gid=%s attempts=%s err=%s",
                gid, task.get("notification_attempts", 0) + 1, exc,
            )
            return

        await self.db.mark_notified(gid)
        logger.info(
            "download_notified gid=%s status=%s chat_type=%s chat_id=%s user_id=%s",
            gid, snap.aria2_status, task["chat_type"], task["chat_id"], task["user_id"],
        )

    async def _handle_unknown(self, task: dict[str, Any]) -> None:
        gid = task["gid"]
        await self.db.finish_task(
            gid,
            "unknown",
            error_message="aria2 中不存在该 gid",
            aria2_status="unknown",
        )
        self._release_reservation(task)

        if task.get("notified_at") is not None:
            return
        text = self.tasks.format_unknown_notice(task)
        try:
            await self.onebot.send_message(task["chat_type"], task["chat_id"], text)
            await self.db.mark_notified(gid)
        except Exception as exc:
            await self.db.record_notify_failure(gid, str(exc))
            logger.warning("unknown task notification failed gid=%s err=%s", gid, exc)

    def _release_reservation(self, task: dict[str, Any]) -> None:
        gid = task["gid"]
        if gid in self._released:
            return
        self._released.add(gid)
        estimated = int(task.get("estimated_size") or 0)
        self.tasks.reserved_bytes = max(0, self.tasks.reserved_bytes - estimated)

    async def _disk_guard(self) -> None:
        if not self.settings.aria2_download_dir:
            # aria2 runs remotely (or uses its own default dir); local disk
            # guard does not apply.
            return
        try:
            total, _used, free = await asyncio.to_thread(
                shutil.disk_usage, self.settings.aria2_download_dir
            )
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("local disk check unavailable: %s", exc)
            return

        reserve = max(
            self.settings.disk_min_free_bytes,
            int(total * self.settings.disk_min_free_percent / 100),
        )
        state = self.tasks.disk_guard_state

        if free < self.settings.disk_critical_free_bytes:
            if state != "critical":
                self.tasks.disk_guard_state = "critical"
                try:
                    await self.aria2.pause_all()
                except Exception as exc:
                    logger.warning("aria2 pauseAll failed: %s", exc)
                logger.warning("disk critical: free=%s critical=%s", free, self.settings.disk_critical_free_bytes)
                await self._notify_admins(
                    f"⚠️ 磁盘进入 critical：剩余 {format_bytes(free)}，已暂停所有下载。"
                )
            return

        if state == "critical":
            if self.settings.disk_auto_resume and free >= int(reserve * 1.2):
                self.tasks.disk_guard_state = "ok"
                try:
                    await self.aria2.unpause_all()
                except Exception as exc:
                    logger.warning("aria2 unpauseAll failed: %s", exc)
                logger.info("disk recovered: free=%s", free)
                await self._notify_admins(f"✅ 磁盘空间已恢复：剩余 {format_bytes(free)}，已恢复下载。")

    async def _notify_admins(self, text: str) -> None:
        for admin in self.settings.admin_users:
            if admin == "*":
                continue
            try:
                await self.onebot.send_message("private", int(admin), text)
            except Exception:
                logger.warning("notify admin %s failed", admin)