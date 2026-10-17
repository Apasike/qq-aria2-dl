from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("qq_aria2_dl.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    gid                     TEXT PRIMARY KEY,
    chat_type               TEXT NOT NULL CHECK (chat_type IN ('group', 'private')),
    chat_id                 INTEGER NOT NULL,
    user_id                 INTEGER NOT NULL,
    message_id              TEXT,
    url                     TEXT NOT NULL,
    out_name                TEXT,
    estimated_size          INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'accepted',
    aria2_status            TEXT,
    error_message           TEXT,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL,
    finished_at             INTEGER,
    notified_at             INTEGER,
    notification_attempts   INTEGER NOT NULL DEFAULT 0,
    last_notification_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending_notify
    ON tasks(notified_at) WHERE notified_at IS NULL;

CREATE TABLE IF NOT EXISTS dedupe_keys (
    key         TEXT PRIMARY KEY,
    chat_type   TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    url         TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dedupe_expires ON dedupe_keys(expires_at);

CREATE TABLE IF NOT EXISTS allowed_users (
    qq         TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(_SCHEMA)
        await self.conn.commit()
        logger.info("sqlite connected: %s", self.path)

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _require(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("database is not connected")
        return self.conn

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # ----- tasks -----
    async def insert_task(
        self,
        *,
        gid: str,
        chat_type: str,
        chat_id: int,
        user_id: int,
        message_id: str | None,
        url: str,
        out_name: str | None,
        estimated_size: int,
        status: str = "accepted",
    ) -> None:
        now = int(time.time())
        await self._require().execute(
            """
            INSERT INTO tasks
                (gid, chat_type, chat_id, user_id, message_id, url, out_name,
                 estimated_size, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (gid, chat_type, chat_id, user_id, message_id, url, out_name,
             estimated_size, status, now, now),
        )
        await self._require().commit()

    async def get_task(self, gid: str) -> dict[str, Any] | None:
        cur = await self._require().execute("SELECT * FROM tasks WHERE gid = ?", (gid,))
        return self._row(await cur.fetchone())

    async def set_status(
        self,
        gid: str,
        status: str,
        aria2_status: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = int(time.time())
        await self._require().execute(
            """
            UPDATE tasks
               SET status = ?, aria2_status = ?, error_message = ?, updated_at = ?
             WHERE gid = ?
            """,
            (status, aria2_status, error_message, now, gid),
        )
        await self._require().commit()

    async def finish_task(
        self,
        gid: str,
        status: str,
        error_message: str | None = None,
        aria2_status: str | None = None,
    ) -> None:
        now = int(time.time())
        await self._require().execute(
            """
            UPDATE tasks
               SET status = ?,
                   error_message = COALESCE(?, error_message),
                   aria2_status = COALESCE(?, aria2_status),
                   finished_at = ?, updated_at = ?
             WHERE gid = ?
            """,
            (status, error_message, aria2_status, now, now, gid),
        )
        await self._require().commit()

    async def mark_notified(self, gid: str) -> None:
        now = int(time.time())
        await self._require().execute(
            "UPDATE tasks SET notified_at = ?, updated_at = ? WHERE gid = ?",
            (now, now, gid),
        )
        await self._require().commit()

    async def record_notify_failure(self, gid: str, error: str) -> None:
        now = int(time.time())
        await self._require().execute(
            """
            UPDATE tasks
               SET notification_attempts = notification_attempts + 1,
                   last_notification_error = ?, updated_at = ?
             WHERE gid = ?
            """,
            (error[:500], now, gid),
        )
        await self._require().commit()

    async def list_reconcile_tasks(self) -> list[dict[str, Any]]:
        cur = await self._require().execute(
            """
            SELECT * FROM tasks
             WHERE notified_at IS NULL
                OR status IN ('accepted', 'submitting', 'submitted', 'downloading', 'paused')
             ORDER BY created_at ASC
            """
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_tasks(
        self,
        chat_type: str,
        chat_id: int,
        user_id: int,
        *,
        admin: bool = False,
        only_open: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        where = ["chat_type = ?", "chat_id = ?"]
        params: list[Any] = [chat_type, chat_id]
        if not admin or chat_type == "private":
            where.append("user_id = ?")
            params.append(user_id)
        if only_open:
            where.append("status IN ('accepted', 'submitting', 'submitted', 'downloading', 'paused')")
        sql = f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cur = await self._require().execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    # ----- dedupe -----
    async def try_insert_dedupe(
        self, key: str, chat_type: str, chat_id: int, url: str, ttl_sec: int
    ) -> bool:
        now = int(time.time())
        try:
            await self._require().execute(
                """
                INSERT INTO dedupe_keys (key, chat_type, chat_id, url, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, chat_type, chat_id, url, now, now + ttl_sec),
            )
            await self._require().commit()
            return True
        except sqlite3.IntegrityError:
            return False

    async def delete_dedupe(self, key: str) -> None:
        await self._require().execute("DELETE FROM dedupe_keys WHERE key = ?", (key,))
        await self._require().commit()

    async def cleanup_dedupe(self) -> None:
        await self._require().execute(
            "DELETE FROM dedupe_keys WHERE expires_at < ?", (int(time.time()),)
        )
        await self._require().commit()

    async def delete_old_tasks(self, retention_days: int) -> None:
        cutoff = int(time.time()) - retention_days * 86400
        await self._require().execute(
            "DELETE FROM tasks WHERE notified_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        await self._require().commit()

    # ----- download whitelist -----
    async def seed_allowed_users(self, users: list[str]) -> None:
        now = int(time.time())
        for user in users:
            qq = str(user).strip()
            if qq:
                await self._require().execute(
                    "INSERT OR IGNORE INTO allowed_users (qq, created_at) VALUES (?, ?)",
                    (qq, now),
                )
        await self._require().commit()

    async def list_allowed_users(self) -> list[dict[str, Any]]:
        cur = await self._require().execute(
            "SELECT qq, created_at FROM allowed_users ORDER BY qq"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_allowed_user(self, qq: str) -> None:
        await self._require().execute(
            "INSERT OR IGNORE INTO allowed_users (qq, created_at) VALUES (?, ?)",
            (str(qq).strip(), int(time.time())),
        )
        await self._require().commit()

    async def remove_allowed_user(self, qq: str) -> None:
        await self._require().execute(
            "DELETE FROM allowed_users WHERE qq = ?", (str(qq).strip(),)
        )
        await self._require().commit()