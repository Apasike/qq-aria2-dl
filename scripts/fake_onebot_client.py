"""A tiny fake NapCat client for local smoke testing without a real QQ account.

It connects to the bot's reverse-websocket endpoint, sends a group message event,
and answers every OneBot action the bot sends back with a successful echo so the
bot does not block on its 10s send timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser(description="fake OneBot v11 client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--path", default="/onebot/v11/ws")
    parser.add_argument("--token", default="test-token")
    parser.add_argument("--group", type=int, default=123456)
    parser.add_argument("--user", type=int, default=10001)
    parser.add_argument("--self-id", type=int, default=2000)
    parser.add_argument(
        "--url",
        default="https://speed.cloudflare.com/__down?bytes=1024",
        help="URL to send; use /dl URL to test manual download instead",
    )
    parser.add_argument("--text", default=None, help="raw text to send (overrides --url)")
    parser.add_argument("--wait", type=float, default=12.0, help="seconds to keep listening")
    args = parser.parse_args()

    text = args.text if args.text is not None else args.url
    uri = f"ws://{args.host}:{args.port}{args.path}?access_token={args.token}"

    print(f"connecting to {uri}")
    async with websockets.connect(uri) as ws:
        # heartbeat first
        await ws.send(json.dumps({
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "self_id": args.self_id,
            "time": int(time.time()),
        }))

        await ws.send(json.dumps({
            "post_type": "message",
            "message_type": "group",
            "group_id": args.group,
            "user_id": args.user,
            "self_id": args.self_id,
            "message_id": int(time.time() * 1000),
            "raw_message": text,
            "message": [{"type": "text", "data": {"text": text}}],
        }))
        print(f"sent group message to {args.group}: {text}")

        deadline = time.time() + args.wait
        while time.time() < deadline:
            remain = deadline - time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remain)
            except asyncio.TimeoutError:
                break

            msg = json.loads(raw)
            if isinstance(msg, dict) and "echo" in msg and "action" in msg:
                print(f"\n[bot -> {msg.get('action')}]")
                print(json.dumps(msg.get("params", {}), ensure_ascii=False, indent=2))
                await ws.send(json.dumps({
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 1},
                    "echo": msg["echo"],
                }))
            elif isinstance(msg, dict):
                print(f"\n[event] {json.dumps(msg, ensure_ascii=False)[:300]}")

    print("\ndone")


if __name__ == "__main__":
    asyncio.run(main())