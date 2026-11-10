"""Exercise the actual windowed EXE against loopback-only fake services."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

from aiohttp import web

import app


async def check(executable, directory):
    replies = []
    requests = []
    replied = asyncio.Event()

    async def model(request):
        payload = await request.json()
        requests.append(payload)
        return web.json_response({"id": "offline-test", "choices": [{"message": {"role": "assistant", "content": "你好，我是小北。"}, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    async def onebot(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for incoming in ws:
            if incoming.type != web.WSMsgType.TEXT:
                continue
            message = json.loads(incoming.data)
            action = message.get("action")
            if action == "get_login_info":
                data = {"user_id": 10000, "nickname": "离线测试机器人"}
            else:
                data = {"message_id": 99}
            await ws.send_json({"status": "ok", "retcode": 0, "data": data, "echo": message.get("echo")})
            if action == "get_login_info":
                await ws.send_json({"time": int(time.time()), "self_id": 10000, "post_type": "message", "message_type": "private", "sub_type": "friend",
                                    "message_id": 1, "user_id": 20000, "sender": {"user_id": 20000, "nickname": "离线测试用户"},
                                    "raw_message": "你好", "message": [{"type": "text", "data": {"text": "你好"}}]})
            if action == "send_private_msg":
                replies.append(message)
                replied.set()
        return ws

    fake = web.Application()
    fake.router.add_post("/chat/completions", model)
    fake.router.add_get("/onebot", onebot)
    runner = web.AppRunner(fake)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    directory.mkdir(parents=True, exist_ok=True)
    config = app.make_config(f"ws://127.0.0.1:{port}/onebot", "offline-test-token")
    config["deepseek"]["base_url"] = f"http://127.0.0.1:{port}"
    config["deepseek"]["api_key"] = "fake-key-used-only-on-loopback"
    config["bot"].update(split_messages=False, enable_vision=False)
    app.write_json(directory / "config.json", config)
    (directory / "persona.txt").write_text("你叫小北，友善地用中文聊天。", encoding="utf-8")
    (directory / "engine-stop").unlink(missing_ok=True)
    (directory / "engine-ready.json").unlink(missing_ok=True)
    environment = os.environ.copy()
    for name in ("DEEPSEEK_API_KEY", "BOCHA_API_KEY", "IMAGE_API_KEY", "ONEBOT_ACCESS_TOKEN"):
        environment.pop(name, None)
    child = subprocess.Popen([str(executable), "--engine", "--data-dir", str(directory)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, creationflags=app.NO_WINDOW)
    result = {"passed": False, "real_qq_used": False, "paid_api_called": False}
    try:
        await asyncio.wait_for(replied.wait(), timeout=40)
        if child.poll() is not None:
            raise AssertionError("Frozen engine exited prematurely")
        if not requests or not replies or not (directory / "engine-ready.json").exists():
            raise AssertionError("Frozen engine did not complete its fake chat roundtrip")
        (directory / "engine-stop").touch()
        await asyncio.wait_for(asyncio.to_thread(child.wait), timeout=20)
        if child.returncode != 0:
            raise AssertionError("Frozen engine did not stop gracefully")
        if not (directory / "data/history.json").exists():
            raise AssertionError("History was not saved")
        result.update(passed=True, fake_chat_roundtrip=True, graceful_stop=True, history_saved=True)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        await runner.cleanup()
        app.write_json(directory / "result.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    import sys
    asyncio.run(check(Path(sys.argv[1]), Path(__file__).resolve().parent.parent / "frozen-engine-test"))
