"""OneBot 11 正向 WebSocket 客户端。

设计要点：
- 断线自动重连，指数退避，不会因为 协议端 重启就把进程搞死。
- 调用 API 用 echo 字段配对请求/响应，每个调用有独立超时。
- 事件回调包在 try/except 里，单条消息处理失败不影响连接。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import aiohttp

from .config import OneBotConfig

log = logging.getLogger(__name__)

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    """调用 OneBot API 失败。"""


class OneBotClient:
    def __init__(self, cfg: OneBotConfig, on_event: EventHandler) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._counter = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()
        self.self_id: Optional[int] = None
        self._closed = False

    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def run(self) -> None:
        """重连循环。除非被 cancel，否则一直跑。

        ## 连续失败要「喊出来」，不能一直 INFO 刷屏（2026-10-22 加）

        用户的原话是「机器人一段时间后就会自动掉线」。查日志发现真正发生的是：

            16:40:10  断线
            16:40:14  QQ 重启（QQ 自己更新/崩溃）
            16:40:15  起，每 60 秒一条 `Cannot connect to host 127.0.0.1:3001`
                      —— 一直刷到 17:58 用户重启机器人还在刷

        **协议端 是注入进 QQ 进程的**，QQ 一重启，注入的 hook 就没了，
        3001 端口整个消失。这时候必须重新跑 `协议端的启动脚本`
        （要管理员权限），光重启机器人没用。

        但原来的日志全是 INFO 级别，夹在几百行里根本看不见，
        用户只感觉到「她不回我了」。

        现在连续失败 5 次（约 5 分钟）就升到 ERROR，并写清楚**要做什么**。
        只在跨过阈值时喊一次，之后每 30 次再提醒一次 —— 不然又变成刷屏。
        """
        delay = float(self.cfg.reconnect_interval)
        fails = 0
        while not self._closed:
            try:
                await self._connect_and_listen()
                if fails >= 5:
                    log.info("已恢复与 OneBot 的连接（之前连续失败 %d 次）", fails)
                fails = 0
                delay = float(self.cfg.reconnect_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 任何异常都不该终止循环
                fails += 1
                if fails >= 5:
                    # 连不上不是「网络抖动」了，是 协议端 没了 —— 说清楚怎么办
                    if fails == 5 or fails % 30 == 0:
                        log.error(
                            "已经连续 %d 次连不上 OneBot（%s）。"
                            "**这通常是协议端掉了，不是机器人的问题** —— "
                            "QQ 重启/更新后协议端连接可能失效。"
                            "处理办法：重新运行 %s "
                            "（会重启 QQ，可能需要重新登录），然后不用动机器人，它会自己连上。",
                            fails, exc,
                            r"<请填写你的 OneBot 11 协议端启动脚本路径>",
                        )
                    else:
                        log.warning("连接异常（第 %d 次）：%s", fails, exc)
                else:
                    log.warning("连接异常：%s: %s", type(exc).__name__, exc)

            if self._closed:
                break
            self._fail_pending("连接已断开")
            log.info("%.1f 秒后重连…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, float(self.cfg.max_reconnect_interval))

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        for task in list(self._tasks):
            task.cancel()
        self._fail_pending("客户端关闭")

    # ------------------------------------------------------------------ #

    async def _connect_and_listen(self) -> None:
        headers = {}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10.0)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            log.info("正在连接 OneBot 服务端：%s", self.cfg.ws_url)
            async with session.ws_connect(
                self.cfg.ws_url,
                heartbeat=max(15.0, float(self.cfg.heartbeat_timeout) / 3),
                max_msg_size=16 * 1024 * 1024,
            ) as ws:
                self._ws = ws
                log.info("OneBot 连接就绪")

                # 关键：登录信息请求必须和读取循环并发跑。
                # 如果在启动 async for 之前 await 它，响应永远没人读，future 不会完成，
                # self_id 会一直是 None —— 群里 @ 机器人就永远不会触发。
                login_task = asyncio.create_task(self._refresh_self_id())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_raw(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self._handle_raw(bytes(msg.data).decode("utf-8", "replace"))
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                finally:
                    login_task.cancel()
                    await asyncio.gather(login_task, return_exceptions=True)
        self._ws = None

    async def _refresh_self_id(self) -> None:
        try:
            info = await self.call("get_login_info")
            if isinstance(info, dict) and info.get("user_id"):
                self.self_id = int(info["user_id"])
                log.info("机器人账号：%s（%s）", info.get("nickname"), self.self_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("获取登录信息失败：%s", exc)

    def _handle_raw(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("收到无法解析的报文：%s", raw[:200])
            return

        if not isinstance(data, dict):
            return

        # OneBot 11 的事件里一定带 self_id —— 用它兜底，不依赖 get_login_info 是否成功
        if self.self_id is None:
            event_self = data.get("self_id")
            if event_self is not None:
                try:
                    self.self_id = int(event_self)
                    log.info("从事件中获取到机器人账号：%s", self.self_id)
                except (TypeError, ValueError):
                    pass

        # API 响应：有 echo 字段
        echo = data.get("echo")
        if echo is not None:
            future = self._pending.get(str(echo))
            if future is not None and not future.done():
                future.set_result(data)
            return

        # 心跳 / 生命周期，不打日志到事件层
        if data.get("post_type") == "meta_event":
            if data.get("meta_event_type") == "lifecycle":
                log.info("OneBot 生命周期事件：%s", data.get("sub_type"))
            return

        task = asyncio.create_task(self._safe_dispatch(data))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_dispatch(self, event: Dict[str, Any]) -> None:
        try:
            await self.on_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("处理事件失败：%s", event.get("post_type"))

    def _fail_pending(self, reason: str) -> None:
        for echo, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(OneBotError(reason))
            self._pending.pop(echo, None)

    # ------------------------------------------------------------------ #

    async def call(self, action: str, **params: Any) -> Any:
        """调用 OneBot API，返回 data 字段。"""
        if not self.connected or self._ws is None:
            raise OneBotError(f"未连接到 OneBot 服务端，无法调用 {action}")

        echo = str(next(self._counter))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        payload = {"action": action, "params": params, "echo": echo}

        try:
            async with self._send_lock:
                await self._ws.send_json(payload)
            resp = await asyncio.wait_for(future, timeout=float(self.cfg.call_timeout))
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"调用 {action} 超时") from exc
        finally:
            self._pending.pop(echo, None)

        status = resp.get("status")
        retcode = resp.get("retcode")
        if status == "failed" or (isinstance(retcode, int) and retcode not in (0, 1)):
            raise OneBotError(
                f"调用 {action} 失败：status={status} retcode={retcode} "
                f"msg={resp.get('message') or resp.get('wording')}"
            )
        return resp.get("data")

    # ------------------------------------------------------------------ #
    # 常用 API 封装

    async def send_group_msg(self, group_id: int, message: Any) -> Any:
        return await self.call("send_group_msg", group_id=int(group_id), message=message)

    async def send_private_msg(self, user_id: int, message: Any) -> Any:
        return await self.call("send_private_msg", user_id=int(user_id), message=message)

    async def get_image(self, file: str) -> Any:
        return await self.call("get_image", file=file)

    async def get_msg(self, message_id: int) -> Any:
        return await self.call("get_msg", message_id=int(message_id))

    async def get_group_member_info(self, group_id: int, user_id: int) -> Any:
        return await self.call(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )

    async def get_stranger_info(self, user_id: int) -> Any:
        return await self.call("get_stranger_info", user_id=int(user_id), no_cache=False)
