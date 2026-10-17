from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..aria2.rpc import Aria2Client, Aria2Error
from ..models import ProgressSnapshot
from . import urls as url_utils

logger = logging.getLogger("qq_aria2_dl.tasks")

STATUS_TEXT = {
    "active": "下载中",
    "waiting": "排队中",
    "paused": "已暂停",
    "complete": "已完成",
    "error": "失败",
    "removed": "已取消",
    "unknown": "未知",
}

STATUS_ICON = {
    "active": "📥",
    "waiting": "⏳",
    "paused": "⏸️",
    "complete": "✅",
    "error": "❌",
    "removed": "🚫",
    "unknown": "⚠️",
}


@dataclass
class SubmitResult:
    ok: bool
    message: str
    gid: str | None = None
    duplicate: bool = False


class ProgressCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, ProgressSnapshot]] = {}
        self._lock = asyncio.Lock()

    async def get(self, gid: str, max_age: float) -> ProgressSnapshot | None:
        async with self._lock:
            item = self._data.get(gid)
            if item is None:
                return None
            ts, snap = item
            if time.monotonic() - ts > max_age:
                return None
            return snap

    async def put(self, gid: str, snap: ProgressSnapshot) -> None:
        async with self._lock:
            self._data[gid] = (time.monotonic(), snap)
            if len(self._data) > 5000:
                now = time.monotonic()
                for k in list(self._data):
                    if now - self._data[k][0] > 600:
                        del self._data[k]


class TaskService:
    def __init__(
        self,
        settings: Any,
        db: Any,
        aria2: Aria2Client,
        acl: Any,
        progress: ProgressCache,
    ) -> None:
        self.settings = settings
        self.db = db
        self.aria2 = aria2
        self.acl = acl
        self.progress = progress
        self._dedupe_locks: dict[str, asyncio.Lock] = {}
        self._dedupe_locks_guard = asyncio.Lock()
        self.reserved_bytes = 0
        self.disk_guard_state = "ok"
        self._head_client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        self._head_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.disk_head_timeout_sec, connect=5.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._head_client is not None:
            await self._head_client.aclose()
            self._head_client = None

    # ----- submit -----
    def _dedupe_key(self, chat_type: str, chat_id: int, user_id: int, url: str) -> str:
        scope = self.settings.dedupe_scope
        if scope == "global":
            base = f"global::{url}"
        elif scope == "user":
            base = f"user::{user_id}::{url}"
        else:
            base = f"{chat_type}:{chat_id}:{url}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    async def _get_dedupe_lock(self, key: str) -> asyncio.Lock:
        async with self._dedupe_locks_guard:
            lock = self._dedupe_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._dedupe_locks[key] = lock
            return lock

    async def submit(
        self,
        *,
        chat_type: str,
        chat_id: int,
        user_id: int,
        message_id: str | None,
        url: str,
        out_name: str | None = None,
    ) -> SubmitResult:
        canonical = url_utils.normalize_url(
            url, self.settings.allowed_schemes, self.settings.max_url_length
        )
        if canonical is None:
            return SubmitResult(False, "不支持该链接类型（仅支持 http/https）。")

        safe_name = None
        if out_name:
            safe_name = url_utils.sanitize_filename(out_name)
            if safe_name is None:
                return SubmitResult(False, "文件名不合法。")

        dedupe_key = self._dedupe_key(chat_type, chat_id, user_id, canonical)
        lock = await self._get_dedupe_lock(dedupe_key)
        async with lock:
            inserted = await self.db.try_insert_dedupe(
                dedupe_key, chat_type, chat_id, canonical, self.settings.dedupe_ttl_sec
            )
            if not inserted:
                return SubmitResult(False, "这个链接刚刚已经提交过了，请稍后再试。", duplicate=True)

            try:
                free, reserve = await self._disk_status()
                estimated = await self._estimate_size(canonical)
                if free - (self.reserved_bytes + estimated) < reserve:
                    await self.db.delete_dedupe(dedupe_key)
                    return SubmitResult(
                        False,
                        f"磁盘空间不足（剩余 {url_utils.format_bytes(free)}，"
                        f"需预留 {url_utils.format_bytes(reserve)}）。",
                    )

                gid = secrets.token_hex(8)
                while gid == "0" * 16:
                    gid = secrets.token_hex(8)

                await self.db.insert_task(
                    gid=gid,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    user_id=user_id,
                    message_id=message_id,
                    url=canonical,
                    out_name=safe_name,
                    estimated_size=estimated,
                    status="submitting",
                )

                try:
                    await self.aria2.add_uri([canonical], self._add_options(gid, safe_name))
                except Exception as exc:
                    await self.db.delete_dedupe(dedupe_key)
                    await self.db.finish_task(gid, "failed", error_message=str(exc))
                    await self.db.mark_notified(gid)
                    logger.error(
                        "addUri failed gid=%s chat_type=%s chat_id=%s err=%s",
                        gid, chat_type, chat_id, exc,
                    )
                    return SubmitResult(False, f"提交失败：{_short_error(exc)}", gid=gid)

                await self.db.set_status(gid, "submitted", aria2_status="waiting")
                self.reserved_bytes += estimated
                logger.info(
                    "task submitted gid=%s chat_type=%s chat_id=%s user_id=%s message_id=%s url=%s",
                    gid, chat_type, chat_id, user_id, message_id, canonical,
                )
                name = safe_name or "（由 aria2 命名）"
                return SubmitResult(True, f"已加入下载队列\n文件：{name}\ngid：{gid}", gid=gid)
            except Exception as exc:  # pragma: no cover - safety net
                await self.db.delete_dedupe(dedupe_key)
                logger.exception("submit failed unexpectedly")
                return SubmitResult(False, f"提交失败：{_short_error(exc)}")

    def _add_options(self, gid: str, out_name: str | None) -> dict[str, str]:
        opts: dict[str, str] = {
            "gid": gid,
            "max-connection-per-server": str(self.settings.aria2_max_connection_per_server),
            "split": str(self.settings.aria2_split),
            "continue": "true",
            "max-tries": "3",
            "retry-wait": "10",
            "http-accept-gzip": "false",
        }
        if self.settings.download_user_agent:
            opts["user-agent"] = self.settings.download_user_agent
        if self.settings.aria2_download_dir:
            opts["dir"] = self.settings.aria2_download_dir
        if out_name:
            opts["out"] = out_name
        if self.settings.download_referer:
            opts["referer"] = self.settings.download_referer
        return opts

    async def _disk_status(self) -> tuple[int, int]:
        if not self.settings.aria2_download_dir:
            # aria2 uses its own default dir (possibly remote); no local check.
            return (10**18, 0)
        try:
            total, _used, free = await asyncio.to_thread(
                shutil.disk_usage, self.settings.aria2_download_dir
            )
        except FileNotFoundError:
            return (10**18, 0)
        except Exception as exc:
            logger.warning("local disk check unavailable: %s", exc)
            return (10**18, 0)
        reserve = max(
            self.settings.disk_min_free_bytes,
            int(total * self.settings.disk_min_free_percent / 100),
        )
        return free, reserve

    async def _estimate_size(self, url: str) -> int:
        if url.lower().startswith("magnet:"):
            return self.settings.disk_unknown_size_reserve_bytes
        try:
            if self._head_client is None:
                return self.settings.disk_unknown_size_reserve_bytes
            resp = await self._head_client.head(url)
            length = resp.headers.get("content-length")
            if length and length.isdigit():
                return int(length)
        except Exception:
            pass
        return self.settings.disk_unknown_size_reserve_bytes

    # ----- cancel -----
    async def cancel(
        self, gid: str, *, user_id: int, chat_type: str, chat_id: int, is_admin: bool
    ) -> str:
        task = await self.db.get_task(gid)
        if task is None:
            return "任务不存在。"
        if not is_admin and str(task["user_id"]) != str(user_id):
            return "你没有权限取消该任务。"
        if task["chat_type"] != chat_type or int(task["chat_id"]) != int(chat_id):
            return "任务不属于当前会话。"

        try:
            await self.aria2.remove(gid)
        except Aria2Error as exc:
            if exc.code != 1:  # 1 == GID not found, still fine to cancel locally
                logger.warning("aria2 remove failed gid=%s err=%s", gid, exc)

        await self.db.finish_task(gid, "cancelled", aria2_status="removed")
        await self.db.mark_notified(gid)
        return f"已取消任务 {gid}。"

    # ----- snapshots -----
    async def refresh_snapshot(self, gid: str) -> ProgressSnapshot:
        try:
            result = await self.aria2.tell_status(gid)
            snap = ProgressSnapshot.from_aria2(result)
        except Aria2Error as exc:
            if exc.code == 1:
                snap = ProgressSnapshot(
                    gid=gid,
                    aria2_status="unknown",
                    error_message="aria2 中不存在该 gid（可能服务重启丢失会话）",
                )
            else:
                raise
        await self.progress.put(gid, snap)
        return snap

    async def get_snapshot(self, gid: str, *, force: bool = False) -> ProgressSnapshot:
        if not force:
            cached = await self.progress.get(gid, self.settings.progress_cache_ttl_sec)
            if cached is not None:
                return cached
        try:
            return await self.refresh_snapshot(gid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ProgressSnapshot(
                gid=gid,
                aria2_status="unknown",
                error_message=f"aria2 不可用：{_short_error(exc)}",
            )

    # ----- visibility -----
    @staticmethod
    def _can_view(
        task: dict[str, Any], chat_type: str, chat_id: int, user_id: int, is_admin: bool
    ) -> bool:
        if task["chat_type"] != chat_type:
            return False
        if chat_type == "private":
            return str(task["user_id"]) == str(user_id)
        if int(task["chat_id"]) != int(chat_id):
            return False
        return is_admin or str(task["user_id"]) == str(user_id)

    # ----- formatting -----
    def _display_name(self, task: dict[str, Any], snap: ProgressSnapshot | None) -> str:
        if snap and snap.file_name:
            return snap.file_name
        if task.get("out_name"):
            return task["out_name"]
        path = urlsplit(task["url"]).path
        name = path.rsplit("/", 1)[-1]
        return name or task["url"]

    def _format_single(self, task: dict[str, Any], snap: ProgressSnapshot) -> str:
        name = self._display_name(task, snap)
        status = snap.aria2_status
        icon = STATUS_ICON.get(status, "ℹ️")
        status_text = STATUS_TEXT.get(status, status)

        if status in ("complete", "error", "removed", "unknown"):
            return self._format_terminal(task, snap)

        total = snap.total_length
        completed = snap.completed_length
        speed = snap.download_speed
        lines = [f"{icon} {status_text}"]
        lines.append(f"文件：{name}")

        if total > 0:
            pct = completed / total * 100
            lines.append(f"进度：{url_utils.progress_bar(pct)} {pct:.1f}%")
            lines.append(
                f"大小：{url_utils.format_bytes(completed)} / {url_utils.format_bytes(total)}"
            )
            if speed > 0:
                remaining = (total - completed) / speed
                lines.append(f"剩余：约 {url_utils.format_duration(remaining)}")
        else:
            lines.append("进度：获取元数据中")

        lines.append(f"速度：{url_utils.format_speed(speed)}")
        lines.append(f"连接：{snap.connections}")
        lines.append(f"gid：{task['gid']}")
        return "\n".join(lines)

    def _format_terminal(self, task: dict[str, Any], snap: ProgressSnapshot) -> str:
        name = self._display_name(task, snap)
        icon = STATUS_ICON.get(snap.aria2_status, "ℹ️")
        if snap.aria2_status == "complete":
            return f"{icon} 已完成：{name}（{url_utils.format_bytes(snap.total_length)}）\ngid：{task['gid']}"
        if snap.aria2_status == "error":
            reason = snap.error_message or "未知"
            return f"{icon} 失败：{name}\n原因：{reason}\ngid：{task['gid']}"
        if snap.aria2_status == "removed":
            return f"{icon} 已取消：{name}\ngid：{task['gid']}"
        reason = snap.error_message or "未知"
        return f"{icon} 未知状态：{name}\n原因：{reason}\ngid：{task['gid']}"

    def format_terminal_notice(self, task: dict[str, Any], snap: ProgressSnapshot) -> str:
        return self._format_terminal(task, snap)

    def format_unknown_notice(self, task: dict[str, Any]) -> str:
        return (
            f"⚠️ 任务丢失\n文件：{self._display_name(task, None)}\n"
            f"链接：{task['url']}\ngid：{task['gid']}\n"
            "aria2 中已找不到该任务，可能是 aria2 重启后会话丢失。"
        )

    def _format_multi(self, tasks: list[dict[str, Any]], snaps: dict[str, ProgressSnapshot]) -> str:
        limit = self.settings.progress_max_tasks_per_query
        lines = [f"当前任务（最多显示 {limit} 个）："]
        for i, task in enumerate(tasks[:limit], start=1):
            snap = snaps.get(task["gid"])
            name = self._display_name(task, snap)
            if snap is None or snap.aria2_status in ("unknown",):
                lines.append(f"{i}. {name}  未知状态")
                continue
            if snap.total_length > 0:
                pct = snap.completed_length / snap.total_length * 100
                bar = url_utils.progress_bar(pct, width=10)
                lines.append(
                    f"{i}. {name}  {bar} {pct:.1f}%  {url_utils.format_speed(snap.download_speed)}"
                )
            else:
                lines.append(f"{i}. {name}  获取元数据中")
        if len(tasks) > limit:
            lines.append("……")
        lines.append("使用 /progress <gid> 查看详情，或 /ls 查看完整列表。")
        return "\n".join(lines)

    # ----- queries -----
    async def progress_detail(
        self, gid: str, *, chat_type: str, chat_id: int, user_id: int, is_admin: bool
    ) -> str:
        task = await self.db.get_task(gid)
        if task is None:
            return "任务不存在。"
        if not self._can_view(task, chat_type, chat_id, user_id, is_admin):
            return "你没有权限查看该任务。"
        snap = await self.get_snapshot(gid)
        return self._format_single(task, snap)

    async def stat_detail(
        self, gid: str, *, chat_type: str, chat_id: int, user_id: int, is_admin: bool
    ) -> str:
        task = await self.db.get_task(gid)
        if task is None:
            return "任务不存在。"
        if not self._can_view(task, chat_type, chat_id, user_id, is_admin):
            return "你没有权限查看该任务。"
        snap = await self.get_snapshot(gid)
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task["created_at"]))
        lines = [
            f"任务详情 {task['gid']}",
            f"文件：{self._display_name(task, snap)}",
            f"链接：{task['url']}",
            f"状态：{STATUS_TEXT.get(snap.aria2_status, snap.aria2_status)}",
            f"创建时间：{created}",
            f"大小：{url_utils.format_bytes(snap.total_length)}",
        ]
        if snap.error_message:
            lines.append(f"错误：{snap.error_message}")
        return "\n".join(lines)

    async def progress_list(
        self, *, chat_type: str, chat_id: int, user_id: int, is_admin: bool
    ) -> str:
        tasks = await self.db.list_tasks(
            chat_type,
            chat_id,
            user_id,
            admin=is_admin,
            only_open=True,
            limit=50,
        )
        if not tasks:
            return "当前没有进行中的任务。"
        snaps: dict[str, ProgressSnapshot] = {}
        for task in tasks:
            try:
                snaps[task["gid"]] = await self.get_snapshot(task["gid"])
            except Aria2Error:
                snaps[task["gid"]] = ProgressSnapshot(
                    gid=task["gid"], aria2_status="unknown", error_message="aria2 不可用"
                )
        return self._format_multi(tasks, snaps)

    async def list_tasks_text(
        self, *, chat_type: str, chat_id: int, user_id: int, is_admin: bool
    ) -> str:
        tasks = await self.db.list_tasks(
            chat_type,
            chat_id,
            user_id,
            admin=is_admin,
            only_open=False,
            limit=20,
        )
        if not tasks:
            return "当前没有任务记录。"
        lines = ["最近任务："]
        for task in tasks:
            st = STATUS_TEXT.get(task["status"], task["status"])
            name = task.get("out_name") or urlsplit(task["url"]).path.rsplit("/", 1)[-1] or task["url"]
            lines.append(f"- [{st}] {name}  gid：{task['gid']}")
        return "\n".join(lines)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, Aria2Error):
        return exc.message[:200]
    return str(exc)[:200]