"""OneBot 客户端：维护 WS 连接、action 调用与 echo 匹配。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

from .messages import text_segments

logger = logging.getLogger("qq_aria2_dl.onebot")


class OneBotNotConnected(Exception):
    pass


class OneBotActionError(Exception):
    def __init__(self, retcode: int | None, message: str = "") -> None:
        self.retcode = retcode
        super().__init__(message or f"OneBot action failed with retcode={retcode}")


class OneBotClient:
    """Holds the single reverse-websocket connection and dispatches actions."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._ws: WebSocket | None = None
        self._ws_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.connected_at: float | None = None
        self.last_heartbeat: float = 0.0
        self.event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def heartbeat_age(self) -> float:
        if self.last_heartbeat == 0.0:
            return 0.0
        return time.monotonic() - self.last_heartbeat

    # ----- connection lifecycle -----
    async def attach(self, websocket: WebSocket) -> None:
        async with self._ws_lock:
            old = self._ws
            self._ws = websocket
            self.connected_at = time.monotonic()
            self.last_heartbeat = time.monotonic()
            if old is not None:
                try:
                    await old.close(code=1000, reason="replaced")
                except Exception:  # pragma: no cover
                    pass
        self._fail_pending()

    async def detach(self, websocket: WebSocket) -> None:
        async with self._ws_lock:
            if self._ws is websocket:
                self._ws = None
                self.connected_at = None
        self._fail_pending()

    def _fail_pending(self) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(OneBotNotConnected("OneBot connection lost"))
        self.pending.clear()

    # ----- worker -----
    async def start_worker(self, router: Any) -> None:
        self._worker = asyncio.create_task(self._consume(router), name="onebot-event-worker")

    async def stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _consume(self, router: Any) -> None:
        while True:
            event = await self.event_queue.get()
            try:
                await router.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("error while handling onebot event: %s", event)
            finally:
                self.event_queue.task_done()

    def enqueue_event(self, event: dict[str, Any]) -> None:
        self.event_queue.put_nowait(event)

    # ----- responses -----
    def on_response(self, payload: dict[str, Any]) -> None:
        echo = payload.get("echo")
        fut = self.pending.pop(echo, None)
        if fut is None or fut.done():
            return
        status = payload.get("status")
        retcode = payload.get("retcode")
        if status == "ok" or retcode == 0:
            fut.set_result(payload.get("data", payload))
        else:
            fut.set_exception(OneBotActionError(retcode, str(payload)))

    # ----- action calls -----
    async def call(
        self, action: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        async with self._ws_lock:
            ws = self._ws
        if ws is None:
            raise OneBotNotConnected("OneBot is not connected")

        echo = uuid.uuid4().hex
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        try:
            async with self._send_lock:
                await ws.send_json({"action": action, "params": params, "echo": echo})
            timeout = timeout or self.settings.onebot_send_timeout_sec
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(echo, None)

    async def send_message(
        self,
        chat_type: str,
        chat_id: int | str,
        text: str,
        reply_message_id: int | str | None = None,
    ) -> Any:
        segments = text_segments(text, reply_message_id)
        if chat_type == "group":
            return await self.call("send_group_msg", {"group_id": int(chat_id), "message": segments})
        return await self.call("send_private_msg", {"user_id": int(chat_id), "message": segments})

    async def get_group_member_info(self, group_id: int | str, user_id: int | str) -> dict[str, Any]:
        data = await self.call(
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": int(user_id)},
        )
        return data if isinstance(data, dict) else {}