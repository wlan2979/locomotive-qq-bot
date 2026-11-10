"""日志：控制台 + 滚动文件，外加全局异常兜底。

写这层的原因：机器人是常驻进程，崩了如果没日志就是黑盒。
所有未捕获异常（主线程 / 子线程 / asyncio）都必须落到 logs/bot.log。
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _force_utf8_console() -> None:
    """Windows 控制台默认可能是 GBK，中文日志会炸。强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> logging.Logger:
    _force_utf8_console()

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FMT, _DATEFMT)

    file_handler = RotatingFileHandler(
        log_path / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.addHandler(console)

    # 降噪：aiohttp 的 DEBUG 日志太吵
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _install_excepthooks()
    return root


def _install_excepthooks() -> None:
    """让任何漏网的异常都留下痕迹，而不是静默死掉。"""
    logger = logging.getLogger("uncaught")

    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("主线程未捕获异常，进程可能退出", exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "子线程 %s 未捕获异常",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
