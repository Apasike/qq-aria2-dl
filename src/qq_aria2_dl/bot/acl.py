"""权限控制：下载白名单与管理员判定。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("qq_aria2_dl.acl")


class AclService:
    """Download whitelist is QQ-number based and persisted in SQLite."""

    def __init__(self, settings: Any, onebot: Any, db: Any = None) -> None:
        self.settings = settings
        self.onebot = onebot
        self.db = db
        self._allowed_users: set[str] = set()
        self._allowed_lock = asyncio.Lock()
        self._group_admin_cache: dict[tuple[str, str], tuple[bool, float]] = {}

    async def init(self) -> None:
        async with self._allowed_lock:
            self._allowed_users = set()
            if self.db is not None:
                await self.db.seed_allowed_users(self.settings.download_allowed_users)
                rows = await self.db.list_allowed_users()
                self._allowed_users = {str(r["qq"]) for r in rows}
            else:
                self._allowed_users = {str(u) for u in self.settings.download_allowed_users}
        logger.info("download whitelist loaded: %s", sorted(self._allowed_users))

    def is_download_allowed(self, user_id: int | str) -> bool:
        u = str(user_id)
        return "*" in self._allowed_users or u in self._allowed_users

    def is_global_admin(self, user_id: int | str) -> bool:
        return self.settings.is_global_admin(user_id)

    def list_allowed_users(self) -> list[str]:
        return sorted(self._allowed_users)

    async def add_allowed_user(self, qq: str) -> None:
        qq = str(qq).strip()
        if self.db is not None:
            await self.db.add_allowed_user(qq)
        async with self._allowed_lock:
            self._allowed_users.add(qq)

    async def remove_allowed_user(self, qq: str) -> None:
        qq = str(qq).strip()
        if self.db is not None:
            await self.db.remove_allowed_user(qq)
        async with self._allowed_lock:
            self._allowed_users.discard(qq)

    async def is_admin(self, user_id: int | str, group_id: int | str | None) -> bool:
        if self.is_global_admin(user_id):
            return True
        if group_id is None:
            return False
        cfg = self.settings.group(group_id)
        if cfg and str(user_id) in cfg.admins:
            return True
        return await self.is_group_admin(group_id, user_id)

    async def is_group_admin(self, group_id: int | str, user_id: int | str) -> bool:
        key = (str(group_id), str(user_id))
        now = time.monotonic()
        cached = self._group_admin_cache.get(key)
        if cached and now - cached[1] < 60:
            return cached[0]

        try:
            info = await self.onebot.get_group_member_info(group_id, user_id)
            role = str(info.get("role", "member")).lower()
            is_admin = role in ("owner", "admin")
        except Exception:
            logger.debug("get_group_member_info failed for gid=%s uid=%s", group_id, user_id)
            is_admin = False

        self._group_admin_cache[key] = (is_admin, now)
        return is_admin