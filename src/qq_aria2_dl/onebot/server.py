"""OneBot 反向 WebSocket 服务端处理。"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("qq_aria2_dl.onebot")


class OneBotServer:
    def __init__(self, settings: Any, client: Any) -> None:
        self.settings = settings
        self.client = client

    async def handle(self, websocket: WebSocket) -> None:
        if not self._token_ok(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return

        await websocket.accept()
        await self.client.attach(websocket)
        try:
            while True:
                payload = await websocket.receive_json()
                if not isinstance(payload, dict):
                    continue
                self.client.last_heartbeat = time.monotonic()

                # action response
                if "echo" in payload and ("status" in payload or "retcode" in payload):
                    self.client.on_response(payload)
                    continue

                # ignore self-sent message events, keep the queue free
                if payload.get("post_type") == "message_sent":
                    continue

                if payload.get("post_type") == "message":
                    self.client.enqueue_event(payload)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("onebot websocket error")
        finally:
            await self.client.detach(websocket)

    def _token_ok(self, websocket: WebSocket) -> bool:
        expected = self.settings.onebot_token
        if not expected:
            return True
        token = websocket.query_params.get("access_token") or websocket.query_params.get("token")
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif auth.lower().startswith("token "):
            token = auth[6:].strip()
        if token and hmac.compare_digest(token, expected):
            return True
        logger.warning("onebot websocket rejected: bad token")
        return False