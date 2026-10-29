"""消息路由：命令分发与群自动下载。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..onebot.messages import extract_text, is_at_self
from .acl import AclService
from .rate_limit import SlidingWindowRateLimiter
from .tasks import TaskService
from .urls import extract_urls

logger = logging.getLogger("qq_aria2_dl.router")

HELP_TEXT = """可用命令：
/dl <url> [文件名]  添加下载任务
/progress [gid]     查询任务进度（/p 同义）
/stat [gid]         查询任务详情
/cancel <gid>       取消任务
/ls                 列出可见任务
/wl list            查看下载白名单
/help               显示本帮助

管理员额外命令：
/wl add <qq>        添加下载白名单
/wl del <qq>        删除下载白名单"""


class Router:
    def __init__(
        self,
        settings: Any,
        onebot: Any,
        tasks: TaskService,
        acl: AclService,
    ) -> None:
        self.settings = settings
        self.onebot = onebot
        self.tasks = tasks
        self.acl = acl
        self.submit_limiter = SlidingWindowRateLimiter()
        self.query_limiter = SlidingWindowRateLimiter()
        self._seen_messages: dict[str, float] = {}
        self._seen_lock = asyncio.Lock()

    async def handle_event(self, payload: dict[str, Any]) -> None:
        if payload.get("post_type") != "message":
            return
        message_type = payload.get("message_type")
        if message_type not in ("group", "private"):
            return

        self_id = payload.get("self_id")
        user_id = payload.get("user_id")
        if user_id is None or str(user_id) == str(self_id):
            logger.info("ignore self message user_id=%s self_id=%s", user_id, self_id)
            return

        message_id = payload.get("message_id")
        if message_id is not None and not await self._mark_seen(str(message_id)):
            logger.info("ignore duplicate message message_id=%s", message_id)
            return

        text = extract_text(payload.get("message"))
        logger.info(
            "onebot message received message_type=%s user_id=%s message_id=%s text=%r",
            message_type, user_id, message_id, text[:200],
        )
        if not text.strip():
            return

        if message_type == "group":
            group_id = payload.get("group_id")
            if not self._group_allowed(group_id):
                logger.info("ignore non-allowed group group_id=%s user_id=%s", group_id, user_id)
                return
            cfg = self.settings.group(group_id)
            if cfg is not None and not cfg.enabled:
                logger.info("ignore disabled group group_id=%s user_id=%s", group_id, user_id)
                return
            await self._route_group(
                text=text,
                group_id=int(group_id),
                user_id=int(user_id),
                message_id=message_id,
                cfg=cfg,
                at_self=is_at_self(payload.get("message"), self_id),
            )
            return

        # private
        await self._route_private(
            text=text,
            user_id=int(user_id),
            message_id=message_id,
        )

    def _group_allowed(self, group_id: int | str) -> bool:
        allowed = self.settings.allowed_groups
        if not allowed:
            return True
        return str(group_id) in allowed

    async def _route_group(
        self,
        *,
        text: str,
        group_id: int,
        user_id: int,
        message_id: Any,
        cfg: Any,
        at_self: bool,
    ) -> None:
        stripped = text.strip()
        allowed = self.acl.is_download_allowed(user_id) or self.acl.is_global_admin(user_id)

        if stripped.startswith("/"):
            if not allowed:
                logger.info("reject group command: user %s not in whitelist", user_id)
                await self._safe_reply("group", group_id, "你没有下载权限。", message_id)
                return
            is_admin = await self.acl.is_admin(user_id, group_id)
            reply = await self._handle_command(
                stripped,
                chat_type="group",
                chat_id=group_id,
                user_id=user_id,
                message_id=message_id,
                is_admin=is_admin,
                cfg=cfg,
            )
            if reply:
                await self._safe_reply("group", group_id, reply, message_id)
            return

        # auto-download (opt-in per group)
        if cfg is None or not cfg.auto_download:
            return
        if cfg.require_at and not at_self:
            return
        if not allowed:
            return

        urls = extract_urls(text, self.settings.allowed_schemes, cfg.max_urls_per_message)
        if not urls:
            return

        results: list[str] = []
        for url in urls:
            if not await self._allow_submit(user_id, group_id):
                results.append("提交过于频繁，请稍后再试。")
                break
            result = await self.tasks.submit(
                chat_type="group",
                chat_id=group_id,
                user_id=user_id,
                message_id=str(message_id) if message_id is not None else None,
                url=url,
            )
            if result.ok:
                results.append(result.message)
            elif not result.duplicate:
                results.append(result.message)

        if results:
            await self._safe_reply("group", group_id, "\n".join(results), message_id)

    async def _route_private(
        self, *, text: str, user_id: int, message_id: Any
    ) -> None:
        allowed = self.acl.is_download_allowed(user_id) or self.acl.is_global_admin(user_id)
        if not allowed:
            logger.info("reject private command: user %s not in whitelist", user_id)
            await self._safe_reply("private", user_id, "你没有下载权限。", message_id)
            return

        stripped = text.strip()
        if stripped.startswith("/"):
            reply = await self._handle_command(
                stripped,
                chat_type="private",
                chat_id=user_id,
                user_id=user_id,
                message_id=message_id,
                is_admin=self.acl.is_global_admin(user_id),
                cfg=None,
            )
            if reply:
                await self._safe_reply("private", user_id, reply, message_id)
            return

        await self._safe_reply(
            "private", user_id, "私聊请使用 /dl <url> 添加任务，或发送 /help 查看命令。", message_id
        )

    async def _handle_command(
        self,
        text: str,
        *,
        chat_type: str,
        chat_id: int,
        user_id: int,
        message_id: Any,
        is_admin: bool,
        cfg: Any,
    ) -> str:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/")
        args = parts[1:]

        if cmd in ("p", "progress"):
            return await self._cmd_progress(args, chat_type, chat_id, user_id, is_admin)
        if cmd == "stat":
            return await self._cmd_stat(args, chat_type, chat_id, user_id, is_admin)
        if cmd == "dl":
            return await self._cmd_dl(args, chat_type, chat_id, user_id, is_admin, message_id)
        if cmd == "cancel":
            return await self._cmd_cancel(args, chat_type, chat_id, user_id, is_admin)
        if cmd == "ls":
            return await self.tasks.list_tasks_text(
                chat_type=chat_type, chat_id=chat_id, user_id=user_id, is_admin=is_admin
            )
        if cmd in ("wl", "whitelist"):
            return await self._cmd_whitelist(args, user_id)
        if cmd == "help":
            return HELP_TEXT
        return "未知命令，发送 /help 查看帮助。"

    async def _cmd_dl(
        self,
        args: list[str],
        chat_type: str,
        chat_id: int,
        user_id: int,
        is_admin: bool,
        message_id: Any,
    ) -> str:
        if not args:
            return "用法：/dl <url> [文件名]"
        if not await self._allow_submit(user_id, chat_id if chat_type == "group" else None):
            return "提交过于频繁，请稍后再试。"

        url = args[0]
        out_name = args[1] if len(args) > 1 else None
        result = await self.tasks.submit(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(message_id) if message_id is not None else None,
            url=url,
            out_name=out_name,
        )
        return result.message

    async def _cmd_whitelist(self, args: list[str], user_id: int) -> str:
        sub = args[0].lower() if args else "list"
        if sub in ("list", "ls"):
            users = self.acl.list_allowed_users()
            if not users:
                return "下载白名单为空。"
            return "下载白名单：\n" + "\n".join(users)

        if sub in ("add", "del", "rm"):
            if not self.acl.is_global_admin(user_id):
                return "只有全局管理员可以修改白名单。"
            if len(args) < 2:
                return "用法：/wl add <qq> 或 /wl del <qq>"
            qq = args[1]
            if not qq.isdigit():
                return "QQ 号必须是数字。"
            if sub == "add":
                await self.acl.add_allowed_user(qq)
                return f"已添加 {qq} 到下载白名单。"
            await self.acl.remove_allowed_user(qq)
            return f"已从下载白名单移除 {qq}。"

        return "用法：/wl list、/wl add <qq>、/wl del <qq>"

    async def _allow_submit(self, user_id: int, group_id: int | None) -> bool:
        user_ok = await self.submit_limiter.allow(
            f"user:{user_id}", self.settings.rate_limit_per_user_per_10min, 600
        )
        if not user_ok:
            return False
        if group_id is not None:
            return await self.submit_limiter.allow(
                f"group:{group_id}", self.settings.rate_limit_per_group_per_10min, 600
            )
        return True

    async def _cmd_progress(
        self,
        args: list[str],
        chat_type: str,
        chat_id: int,
        user_id: int,
        is_admin: bool,
    ) -> str:
        if not await self._allow_query(chat_type, chat_id, user_id):
            return "查询过于频繁，请稍后再试。"
        if args:
            return await self.tasks.progress_detail(
                args[0], chat_type=chat_type, chat_id=chat_id, user_id=user_id, is_admin=is_admin
            )
        return await self.tasks.progress_list(
            chat_type=chat_type, chat_id=chat_id, user_id=user_id, is_admin=is_admin
        )

    async def _cmd_stat(
        self,
        args: list[str],
        chat_type: str,
        chat_id: int,
        user_id: int,
        is_admin: bool,
    ) -> str:
        if not args:
            return "用法：/stat <gid>"
        if not await self._allow_query(chat_type, chat_id, user_id):
            return "查询过于频繁，请稍后再试。"
        return await self.tasks.stat_detail(
            args[0], chat_type=chat_type, chat_id=chat_id, user_id=user_id, is_admin=is_admin
        )

    async def _cmd_cancel(
        self,
        args: list[str],
        chat_type: str,
        chat_id: int,
        user_id: int,
        is_admin: bool,
    ) -> str:
        if not args:
            return "用法：/cancel <gid>"
        return await self.tasks.cancel(
            args[0], user_id=user_id, chat_type=chat_type, chat_id=chat_id, is_admin=is_admin
        )

    async def _allow_query(self, chat_type: str, chat_id: int, user_id: int) -> bool:
        key = f"query:{chat_type}:{chat_id}:{user_id}"
        return await self.query_limiter.allow(
            key, 1, self.settings.progress_query_cooldown_sec
        )

    async def _safe_reply(
        self, chat_type: str, chat_id: int, text: str, message_id: Any
    ) -> None:
        try:
            await self.onebot.send_message(
                chat_type, chat_id, text, reply_message_id=message_id
            )
        except Exception:
            logger.warning(
                "send message failed chat_type=%s chat_id=%s message_id=%s",
                chat_type, chat_id, message_id, exc_info=True,
            )

    async def _mark_seen(self, message_id: str) -> bool:
        now = time.monotonic()
        async with self._seen_lock:
            if message_id in self._seen_messages and now - self._seen_messages[message_id] < 3600:
                return False
            self._seen_messages[message_id] = now
            if len(self._seen_messages) > 10000:
                for k in list(self._seen_messages):
                    if now - self._seen_messages[k] > 3600:
                        del self._seen_messages[k]
            return True