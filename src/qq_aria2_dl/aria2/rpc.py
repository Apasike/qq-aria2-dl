from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("qq_aria2_dl.aria2")

STATUS_KEYS = [
    "gid",
    "status",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "connections",
    "files",
    "errorMessage",
    "dir",
    "bittorrent",
    "infoHash",
]


class Aria2Error(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"aria2 error {code}: {message}")


class Aria2Client:
    def __init__(self, settings: Any) -> None:
        self.url = settings.aria2_rpc_url
        self.secret = settings.aria2_secret
        self.timeout = 5.0
        self._client: httpx.AsyncClient | None = None
        self._id = 0

    async def open(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if self._client is None:
            raise Aria2Error(-1, "client not connected")
        self._id += 1
        payload_params: list[Any] = [f"token:{self.secret}"]
        if params:
            payload_params.extend(params)
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._id),
            "method": f"aria2.{method}",
            "params": payload_params,
        }
        try:
            resp = await self._client.post(self.url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Aria2Error(-1, f"rpc transport/parse error: {exc}") from exc
        if "error" in data:
            err = data["error"]
            raise Aria2Error(err.get("code"), str(err.get("message", "unknown error")))
        return data.get("result")

    async def add_uri(self, urls: list[str], options: dict[str, str] | None = None) -> str:
        return await self.call("addUri", [urls, options or {}])

    async def tell_status(self, gid: str, keys: list[str] | None = None) -> dict[str, Any]:
        result = await self.call("tellStatus", [gid, keys or STATUS_KEYS])
        return result if isinstance(result, dict) else {}

    async def tell_active(self, keys: list[str] | None = None) -> list[dict[str, Any]]:
        return await self.call("tellActive", [keys or STATUS_KEYS])

    async def tell_waiting(
        self, offset: int = 0, num: int = 100, keys: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return await self.call("tellWaiting", [offset, num, keys or STATUS_KEYS])

    async def tell_stopped(
        self, offset: int = 0, num: int = 100, keys: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return await self.call("tellStopped", [offset, num, keys or STATUS_KEYS])

    async def remove(self, gid: str) -> str:
        return await self.call("remove", [gid])

    async def force_remove(self, gid: str) -> str:
        return await self.call("forceRemove", [gid])

    async def pause(self, gid: str) -> str:
        return await self.call("pause", [gid])

    async def unpause(self, gid: str) -> str:
        return await self.call("unpause", [gid])

    async def pause_all(self) -> str:
        return await self.call("pauseAll")

    async def unpause_all(self) -> str:
        return await self.call("unpauseAll")

    async def get_global_stat(self) -> dict[str, Any]:
        result = await self.call("getGlobalStat")
        return result if isinstance(result, dict) else {}