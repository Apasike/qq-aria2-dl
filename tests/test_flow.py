import json

import httpx
import pytest

from qq_aria2_dl.aria2.reconcile import ReconcileService
from qq_aria2_dl.aria2.rpc import Aria2Client
from qq_aria2_dl.bot.acl import AclService
from qq_aria2_dl.bot.router import Router
from qq_aria2_dl.bot.tasks import ProgressCache, TaskService
from qq_aria2_dl.config import Settings
from qq_aria2_dl.storage.db import Database


class FakeOneBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_type, chat_id, text, reply_message_id=None):
        self.sent.append((chat_type, chat_id, text, reply_message_id))
        return {"message_id": 999}

    async def get_group_member_info(self, group_id, user_id):
        return {"role": "member"}


def make_rpc_handler(download_dir):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        params = body["params"]
        rid = body["id"]
        if method == "aria2.addUri":
            gid = params[2].get("gid", "deadbeefdeadbeef")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": gid})
        if method == "aria2.tellStatus":
            gid = params[1]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
                "gid": gid,
                "status": "complete",
                "totalLength": "1024",
                "completedLength": "1024",
                "downloadSpeed": "0",
                "connections": "1",
                "files": [{"path": f"{download_dir}/a.zip", "length": "1024"}],
                "errorMessage": "",
            }})
        if method == "aria2.remove":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": params[1]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": None})

    return handler


async def build_env(tmp_path):
    download_dir = tmp_path / "dl"
    settings = Settings(
        _env_file=None,
        download_allowed_users=["1001"],
        groups={"123": {"enabled": True, "auto_download": True, "allowed_users": []}},
        aria2_download_dir=str(download_dir),
        storage_db_path=str(tmp_path / "bot.db"),
        aria2_rpc_url="http://fake/jsonrpc",
        aria2_secret="tok",
    )
    settings.resolve_paths()

    db = Database(settings.storage_db_path)
    await db.connect()

    onebot = FakeOneBot()
    acl = AclService(settings, onebot, db)
    await acl.init()
    aria2 = Aria2Client(settings)
    aria2._client = httpx.AsyncClient(transport=httpx.MockTransport(make_rpc_handler(download_dir)))
    progress = ProgressCache()
    tasks = TaskService(settings, db, aria2, acl, progress)
    await tasks.open()
    router = Router(settings, onebot, tasks, acl)
    reconcile = ReconcileService(settings, db, aria2, onebot, tasks, progress)

    return settings, db, onebot, aria2, tasks, router, reconcile


async def teardown_env(aria2, tasks, db):
    await tasks.close()
    await aria2.close()
    await db.close()


async def test_auto_download_submit_dedupe_and_notify(tmp_path):
    settings, db, onebot, aria2, tasks, router, reconcile = await build_env(tmp_path)
    try:
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "user_id": 1001,
            "self_id": 2000,
            "message_id": 1,
            "message": [{"type": "text", "data": {"text": "https://example.com/a.zip"}}],
        }
        await router.handle_event(event)

        assert len(onebot.sent) == 1
        assert "已加入下载队列" in onebot.sent[0][2]
        tasks_list = await db.list_tasks("group", 123, 1001, admin=False, only_open=False)
        assert len(tasks_list) == 1
        gid = tasks_list[0]["gid"]

        # duplicate within TTL
        event2 = dict(event, message_id=2)
        await router.handle_event(event2)
        assert len(onebot.sent) == 1  # duplicate is silent in auto mode

        # reconcile sees complete status and notifies once
        await reconcile.run_once()
        await reconcile.run_once()
        done = await db.get_task(gid)
        assert done["notified_at"] is not None
        assert any("已完成" in text for _, _, text, _ in onebot.sent)
    finally:
        await teardown_env(aria2, tasks, db)


async def test_manual_dl_and_cancel(tmp_path):
    settings, db, onebot, aria2, tasks, router, reconcile = await build_env(tmp_path)
    try:
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 1001,
            "self_id": 2000,
            "message_id": 10,
            "message": [{"type": "text", "data": {"text": "/dl https://example.com/b.zip 我的文件.zip"}}],
        }
        # private chat: user 1001 is already in the download whitelist
        await router.handle_event(event)
        assert len(onebot.sent) == 1
        assert "已加入下载队列" in onebot.sent[0][2]
        tasks_list = await db.list_tasks("private", 1001, 1001, admin=False, only_open=False)
        assert len(tasks_list) == 1
        gid = tasks_list[0]["gid"]
        assert tasks_list[0]["out_name"] == "我的文件.zip"

        cancel_event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 1001,
            "self_id": 2000,
            "message_id": 11,
            "message": [{"type": "text", "data": {"text": f"/cancel {gid}"}}],
        }
        await router.handle_event(cancel_event)
        done = await db.get_task(gid)
        assert done["status"] == "cancelled"
        assert done["notified_at"] is not None
    finally:
        await teardown_env(aria2, tasks, db)