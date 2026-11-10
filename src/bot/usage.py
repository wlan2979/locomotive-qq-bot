"""生图用量统计与预算闸门。

**为什么需要落盘**：机器人重启很频繁（开发期间一会儿一次），
如果计数只放内存，每次重启预算就清零 —— 等于没有保护。
落盘顺带让"今天花了多少"可查，这是唯一能回答"我花了多少钱"的数据源，
因为火山引擎的**余额要 AK/SK 签名才能查，方舟的 API Key 查不了**。

设计取舍：
  · **按小时 + 按天双重限制**。只限天的话，一次误触（比如模型连着调十次）
    就能把一天的额度瞬间打光；只限小时的话，慢慢积累照样超。
  · 计数与时间戳一起存，判定时**过滤掉过期记录**而不是定时清零 ——
    省一个后台任务，也不会因为进程休眠而漏清。
  · 写文件失败只记日志、不抛 —— 统计出问题不该让画图整个失败。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("bot.usage")


class UsageTracker:
    """记录每次生图的时间、通道、画幅，并按配置判定是否放行。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        # 每条记录：{"at": 时间戳, "tier": 画幅档位, "cost": 估算花费, "channel": 通道名}
        self._events: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            events = raw.get("events") if isinstance(raw, dict) else raw
            if isinstance(events, list):
                self._events = [e for e in events if isinstance(e, dict) and "at" in e]
            log.info("已恢复生图用量记录 %d 条", len(self._events))
        except Exception:  # noqa: BLE001
            log.exception("读取用量记录失败，将从零开始")
            self._events = []

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"events": self._events}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)   # 原子替换，避免写一半崩了把记录弄坏
        except Exception:  # noqa: BLE001
            log.exception("保存用量记录失败（忽略）")

    # ------------------------------------------------------------------ #

    def summary(self, hours: float = 24.0) -> Tuple[int, float]:
        """返回最近 `hours` 小时内的 (张数, 估算花费)。"""
        cutoff = time.time() - hours * 3600
        recent = [e for e in self._events if float(e.get("at") or 0) >= cutoff]
        cost = sum(float(e.get("cost") or 0.0) for e in recent)
        return len(recent), cost

    def total(self) -> Tuple[int, float]:
        """**累计**用量（不随窗口重置）。用于免费额度计数。

        为什么要单独一个累计值：免费额度是"给了 200 张，用完就没有"，
        不是"每 24 小时刷新 200 张"。用窗口计数会在第二天把额度"还回来"，
        于是额度早就用完了、这边却还在放行、一直到服务端拒绝为止。
        """
        # 不清理：累计值要看全部历史
        cost = sum(float(e.get("cost") or 0.0) for e in self._events)
        return len(self._events), cost

    def allow(
        self,
        *,
        hourly_limit: int,
        daily_limit: int,
        total_limit: int,
    ) -> Tuple[bool, str]:
        """能不能再画一张。返回 (是否放行, 拒绝原因)。

        0 或负数表示该维度不限制。
        """
        if total_limit > 0:
            n, _ = self.total()
            if n >= total_limit:
                return False, f"免费额度已用完（累计 {n}/{total_limit} 张）"
        if hourly_limit > 0:
            n, _ = self.summary(1.0)
            if n >= hourly_limit:
                return False, f"最近 1 小时已画 {n} 张（上限 {hourly_limit}）"
        if daily_limit > 0:
            n, _ = self.summary(24.0)
            if n >= daily_limit:
                return False, f"最近 24 小时已画 {n} 张（上限 {daily_limit}）"
        return True, ""

    def record(
        self, *, cost: float, tier: str, channel: str,
        tokens: int = 0,
    ) -> None:
        self._events.append({
            "at": time.time(),
            "cost": float(cost),
            "tier": str(tier),
            "channel": str(channel),
            "tokens": int(tokens or 0),
        })
        self.save()
