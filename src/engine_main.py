"""入口：装配模块、自检、启动。

用法：
    python main.py                 # 正常启动
    python main.py --check         # 只测 DeepSeek 连通性，不连 QQ
    python main.py -c my.json      # 指定配置文件
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp

from bot.config import Config
from bot.core import BotCore
from bot.llm import DeepSeekClient, LLMError
from bot.logger import setup_logging
from bot.imagegen import init_usage
from bot.memory import MemoryStore
from bot.onebot import OneBotClient
from bot.summarize import Summarizer

log = logging.getLogger("main")

# 单实例锁占用的本地端口。随便挑一个高位端口，只要不和系统里别的服务撞。
SINGLE_INSTANCE_PORT = 47201


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QQ AI Bot —— OneBot 11 + DeepSeek")
    parser.add_argument(
        "-c",
        "--config",
        default="config.json",
        help="配置文件路径，默认 ./config.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="自检模式：只测试 DeepSeek 连通性，不连接 QQ",
    )
    return parser.parse_args()


async def amain(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    # 先起一个基础日志，保证配置解析阶段的报错也能落盘
    setup_logging("INFO", config_path.resolve().parent / "logs")

    cfg = Config.load(config_path)
    setup_logging(cfg.log.level, cfg.log_dir)

    loop = asyncio.get_running_loop()

    def loop_exception_handler(_loop, context):
        log.error(
            "asyncio 未处理异常：%s",
            context.get("message"),
            exc_info=context.get("exception"),
        )

    loop.set_exception_handler(loop_exception_handler)

    log.info("=" * 56)
    log.info("QQ AI Bot 启动")
    log.info("配置文件：%s", config_path.resolve())
    log.info("模型：%s @ %s", cfg.deepseek.model, cfg.deepseek.base_url)
    log.info("=" * 56)

    warnings = cfg.validate()
    for warning in warnings:
        log.warning("配置提醒：%s", warning)

    if not cfg.deepseek.api_key:
        log.error(
            "缺少 DeepSeek API key，无法启动。\n"
            "  请在 config.json 里填 deepseek.api_key，"
            "或设置环境变量 DEEPSEEK_API_KEY。\n"
            "  申请地址：https://platform.deepseek.com/api_keys"
        )
        return 2

    persist_path = cfg.data_dir / "history.json" if cfg.bot.history_persist else None
    memory = MemoryStore(
        max_turns=cfg.bot.history_max_turns,
        ttl_minutes=cfg.bot.history_ttl_minutes,
        persist_path=persist_path,
        summary_batch_turns=cfg.bot.summary_batch_turns,
    )
    memory.load()

    # 生图用量统计（预算保护的依据）。**必须落盘** —— 机器人重启很频繁，
    # 只放内存的话每次重启预算就清零，等于没有保护。
    # 落盘顺带让「今天画了几张、估算花了多少」可查 ——
    # 这是唯一能回答花费的数据源，因为火山引擎的余额要 AK/SK 签名才能查。
    usage = init_usage(cfg.data_dir / "image_usage.json")
    total_n, total_cost = usage.total()
    quota = int(cfg.imagegen.api_free_quota or 0)
    left = max(0, quota - total_n) if quota > 0 else -1

    def _limit_text(v: int) -> str:
        """0 在闸门语义里是「不限制」。直接显示 0 会被读成「一张都不许」——
        实测启动日志里那行「每小时 0 张」就让人误解过一次。"""
        return "不限" if int(v or 0) <= 0 else f"{int(v)} 张"

    log.info(
        "生图用量：累计 %d 张%s；闸门 每小时 %s / 每天 %s",
        total_n,
        f"（免费额度 {quota} 张，剩 {left} 张）" if quota > 0 else "",
        _limit_text(cfg.imagegen.api_hourly_limit),
        _limit_text(cfg.imagegen.api_daily_limit),
    )
    if quota > 0 and left <= 20:
        log.warning(
            "在线生图免费额度只剩 %d 张 —— 建议去方舟控制台开启「安心体验模式」"
            "（免费额度用完即停、不自动转付费），免得额度耗尽后开始真扣费。",
            left,
        )

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)) as http:
        llm = DeepSeekClient(cfg.deepseek, http)

        if args.check:
            log.info("自检模式：测试 DeepSeek 连通性…")
            try:
                answer = await llm.ping()
            except LLMError as exc:
                log.error("DeepSeek 连通失败：%s", exc)
                return 3
            log.info("DeepSeek 连通正常，模型返回：%s", answer)
            log.info("自检通过。")
            return 0

        # onebot 需要 core 处理事件，core 需要 onebot 发消息 —— 用 holder 打破循环依赖
        holder: dict[str, BotCore] = {}

        async def on_event(event):
            core = holder.get("core")
            if core is not None:
                await core.on_event(event)

        onebot = OneBotClient(cfg.onebot, on_event)
        core = BotCore(cfg, onebot, llm, memory, http)
        holder["core"] = core

        tasks = [
            asyncio.create_task(onebot.run(), name="onebot"),
            asyncio.create_task(memory.gc_loop(300), name="memory-gc"),
        ]
        # 表情包预热：后台把每个情绪各画一张，让「第一次发表情」不用等 9 秒。
        # 放在这里而不是 __init__ 里 —— 装配完成之后才轮到它动显卡。
        if cfg.sticker_gen.enable and cfg.sticker_gen.prewarm:
            core.start_sticker_prewarm()
        if persist_path is not None:
            tasks.append(asyncio.create_task(memory.autosave_loop(30), name="memory-save"))
        # 滚动摘要：把即将被裁掉的旧对话压成要点，别让机器人彻底失忆。
        # 放在所有前置检查之后 —— 它要花钱调模型，配置没起来时不该跑。
        if cfg.bot.summary_enabled:
            summarizer = Summarizer(
                memory, llm, keep_turns=cfg.bot.summary_keep_turns
            )
            tasks.append(
                asyncio.create_task(
                    summarizer.loop(cfg.bot.summary_interval_minutes * 60.0),
                    name="memory-summary",
                )
            )
            log.info(
                "滚动摘要已启用：每 %.0f 分钟检查一次（超出 %d 轮的部分会被压缩保留）",
                cfg.bot.summary_interval_minutes, cfg.bot.history_max_turns,
            )
        log.info("开始监听消息。按 Ctrl+C 退出。")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("任务被取消，正在退出…")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await onebot.close()
            memory.save(force=True)
            log.info("已保存历史，进程退出。")

    return 0


def _install_exit_logging() -> None:
    """装退出探针：进程不管怎么死的，日志里都留一条。

    ## 为什么需要（2026-10-21 踩到）

    早上用户说「她不回我了」。查日志：最后一条是 09:36:44 的预热结束，
    **之后什么都没有** —— 没有 traceback、没有「已保存历史，进程退出」，
    进程就是没了。用户只能看到「不回话」，我这边查不出是崩溃、
    被任务管理器杀掉、还是那个 cmd 窗口被关了。

    `run.bat` 末尾有 `pause`，所以**真崩溃**时窗口会停住显示
    「Bot exited.」；窗口直接消失 = 被外部终止。但这条推理用户不知道，
    日志里也看不出来。

    这个探针覆盖三种死法：
      · `atexit`        —— 正常 return / 解释器退出
      · SIGTERM / SIGINT —— 被 taskkill / Ctrl+C 打断
      · `sys.excepthook` —— 未捕获异常（asyncio 之外的）

    注意 SIGTERM 在 Windows 上不可靠（taskkill 默认发的是 TERM，
    但 Python 未必收到），所以 atexit 那条是主力 —— 只要解释器还有机会
    做清理，就会写一行。
    """
    import atexit
    import signal

    logger = logging.getLogger("main")
    started = {"at": time.time()}

    def _bye(reason: str) -> None:
        uptime = time.time() - started["at"]
        logger.info(
            "进程退出（原因：%s，已运行 %.1f 分钟）", reason, uptime / 60.0
        )

    atexit.register(lambda: _bye("解释器正常退出"))

    def _on_signal(signum, _frame):
        _bye(f"收到信号 {signum}（{getattr(signal.Signals(signum), 'name', '?')}）")
        # 记完就走 —— 让 atexit 和 asyncio 的 finally 有机会跑
        raise SystemExit(128 + signum)

    for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持 —— 不该让启动失败
            pass

    def _hook(exc_type, exc, tb):
        logger.critical("未捕获异常，进程即将退出", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def _acquire_single_instance(lock_dir: Path):
    """单实例锁：同一个账号只允许一个机器人进程。

    ## 为什么必须有（2026-10-21 踩到）

    早上排查「她不回我了」，发现有**两组机器人进程同时在跑**，
    都连着同一个 协议端 账号。后果：

      · 同一条消息被两个进程各处理一遍 → 可能回两次，也可能互相抢走
      · 两个客户端同时发图 → QQ 侧报 `rich media transfer failed`
        / `网络连接异常`（`result: 1006514`）—— 看起来像网络故障，
        实际是自己打自己

    这个坑很容易踩：`run.bat` 双击一次开一个窗口，用户不确定有没有开着
    就再点一次；或者上次的窗口最小化在任务栏里看不见。

    ## 为什么用端口而不是文件锁

    先试过 `msvcrt.locking` 锁文件，实测第二个进程在 `open()` 阶段就抛
    `PermissionError`，异常从 `except OSError` 旁边溜出去，还打了一屏 traceback。

    端口绑定简单得多：占一个本地端口，占不到就说明有人在跑。
    进程死了系统立刻回收端口，不会有残留锁文件。跨平台也一致。

    返回 socket（必须持有引用，否则被 GC 就释放了）；占不到返回 None。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows 上不设 SO_REUSEADDR，否则两个进程都能绑同一个端口
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None
    except Exception:  # noqa: BLE001
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def main() -> int:
    args = parse_args()
    _install_exit_logging()

    # 自检模式不需要锁（它不连 QQ，跑完就退）
    lock = None
    if not args.check:
        cfg_for_lock = Config.load(Path(args.config))
        lock = _acquire_single_instance(cfg_for_lock.data_dir)
        if lock is None:
            print()
            print("=" * 64)
            print("  已经有一个机器人进程在跑了，这次启动取消。")
            print()
            print("  两个进程会抢同一个 QQ 账号，表现为：")
            print("    · 同一句话回两遍，或者干脆不回")
            print("    · 发图报「网络连接异常」「rich media transfer failed」")
            print()
            print("  要重启就先双击「桃桃 → stop.bat」，再 run.bat。")
            print("=" * 64)
            print()
            return 3

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print()
        logging.getLogger("main").info("收到 Ctrl+C，已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
