from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from .aria2.reconcile import ReconcileService
from .aria2.rpc import Aria2Client
from .bot.acl import AclService
from .bot.router import Router
from .bot.tasks import ProgressCache, TaskService
from .config import Settings, get_settings
from .logging import get_logger, setup_logging
from .onebot.client import OneBotClient
from .onebot.server import OneBotServer
from .storage.db import Database

logger = get_logger("qq_aria2_dl.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.resolve_paths()
    setup_logging(settings.log_level)

    db = Database(settings.storage_db_path)
    onebot = OneBotClient(settings)
    acl = AclService(settings, onebot, db)
    aria2 = Aria2Client(settings)
    progress = ProgressCache()
    tasks = TaskService(settings, db, aria2, acl, progress)
    reconcile = ReconcileService(settings, db, aria2, onebot, tasks, progress)
    router = Router(settings, onebot, tasks, acl)
    server = OneBotServer(settings, onebot)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        await acl.init()
        await aria2.open()
        await tasks.open()
        await onebot.start_worker(router)
        reconcile.start()
        logger.info("qq-aria2-dl started")
        try:
            yield
        finally:
            logger.info("qq-aria2-dl shutting down")
            await reconcile.stop()
            await onebot.stop_worker()
            await tasks.close()
            await aria2.close()
            await db.close()

    app = FastAPI(title="qq-aria2-dl", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        status: dict[str, Any] = {
            "onebot_connected": onebot.connected,
            "aria2_ok": False,
            "sqlite_ok": False,
            "disk_critical": False,
        }
        try:
            await aria2.get_global_stat()
            status["aria2_ok"] = True
        except Exception:
            pass
        try:
            await db.conn.execute("SELECT 1")
            status["sqlite_ok"] = True
        except Exception:
            pass
        try:
            _total, _used, free = await asyncio.to_thread(
                shutil.disk_usage, settings.aria2_download_dir
            )
            status["disk_free_bytes"] = free
            status["disk_critical"] = free < settings.disk_critical_free_bytes
        except Exception:
            pass

        ok = (
            status["onebot_connected"]
            and status["aria2_ok"]
            and status["sqlite_ok"]
            and not status["disk_critical"]
        )
        return JSONResponse(status_code=200 if ok else 503, content=status)

    @app.websocket(settings.onebot_path)
    async def onebot_ws(websocket: WebSocket) -> None:
        await server.handle(websocket)

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()