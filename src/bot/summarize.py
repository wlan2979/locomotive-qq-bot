"""滚动摘要：把「马上要被裁掉的旧对话」压成一段要点。

为什么要做
----------
`MemoryStore._trim()` 在写入时就把超出 `history_max_turns` 的消息**直接删掉**——
这是必须的（不然内存和 history.json 会无界增长），但代价是机器人对早先的对话
**彻底失忆**：你说过"我不吃香菜"，20 轮之后它就当没听过。

摘要补的就是这块：在那些消息被丢掉**之前**，把它们压成几句话留下。
所以这是纯赚的 —— 压的是本来就要消失的内容，不会挤占还能留住的细节。

设计上的几个取舍
----------------
* **只压溢出部分**（见 `pending_for_summary`）。去压还留得住的消息等于用概括
  换掉真实细节，净亏。
* **增量更新**：新摘要 = 旧摘要 + 这批要丢的消息。不是每次重读全部历史，
  否则越到后面越贵，而且早期细节会被反复重写、越写越淡。
* **失败不抛**：这是后台维护任务，网络抖动/额度用尽都不该影响聊天，
  跳过等下一轮即可。所以这里把所有异常都吃掉，只记日志。
* **不发进群、不进历史正文**：摘要是给模型看的，不是对话的一部分。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .llm import DeepSeekClient, LLMError
from .memory import MemoryStore

log = logging.getLogger(__name__)

# 摘要提示词。措辞按「这是什么、要留什么、不要什么」三段写。
#
# 特别注意最后一条：摘要会被塞进后续每一轮的上下文，如果它开始演人设、
# 或者复述聊天里的语气，模型会把它当成"我之前说过的话"照抄，
# 于是机器人开始用第三人称谈自己。所以必须明确要求"客观陈述"。
_SUMMARY_SYSTEM = """你在为一个聊天机器人维护长期记忆。给你的是它和某个人更早的对话，
以及已有的旧摘要。请把它们合并成一段**新的摘要**。

要保留的（按重要性）：
  · 对方的身份、称呼、稳定偏好、明确说过的好恶
  · 双方约定过的事、进行中的话题、答应过要做的事
  · 对以后对话有用的结论（比如"他不用微信""她在准备考试"）

不要保留：
  · 寒暄、临时情绪、逐句复述
  · 已经过时的细节

写法要求：
  · 用对话本身的语言写（中文对话就用中文）
  · 客观陈述，**不要扮演机器人的人设**、不要用它的语气说话
  · 只输出摘要正文，不要任何标题、编号、解释或前后缀
  · 控制在 200 字以内；旧摘要里已经有的事不要重复
"""


def _render(messages: List[Dict[str, Any]]) -> str:
    """把消息列表摊成纯文本。图片占位、工具痕迹都压掉，只留人和话。"""
    lines: List[str] = []
    for m in messages:
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):
            continue  # tool 消息对摘要没价值，跳过
        content = m.get("content")
        if isinstance(content, list):   # 多模态 block 数组
            parts = [
                str(b.get("text") or "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(p for p in parts if p)
        text = str(content or "").strip()
        if not text:
            continue
        # 群聊里用真实昵称，别一律叫「对方」。
        #
        # 摘要会作为第一条历史喂回模型（见 `MemoryStore.context()`）。如果这里
        # 把所有群友揉成一个「对方」，那模型读完成摘要就永久失去了人物区分 ——
        # 原文细节已经被裁掉了，摘要里再没有名字，它就真的分不清了。
        # 2026-10-27 跟 `[昵称]` 前缀一起改的，两处必须一致。
        if role == "user":
            who = str(m.get("sender") or "").strip() or "对方"
        else:
            who = "机器人"
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


class Summarizer:
    """按会话维护滚动摘要。"""

    def __init__(
        self,
        memory: MemoryStore,
        llm: DeepSeekClient,
        keep_turns: int = 4,
        max_chars: int = 2000,
    ) -> None:
        self.memory = memory
        self.llm = llm
        # 压缩时留多少条不动（给摘要提供上下文余量，避免只压到半句话）
        self.keep_turns = max(0, int(keep_turns))
        self.max_chars = max(200, int(max_chars))
        self.compressed_count = 0

    # ------------------------------------------------------------------ #

    async def summarize_session(self, key: str) -> bool:
        """压一个会话。返回是否真的写了新摘要。"""
        session = self.memory.get(key)
        revision = session.revision
        start = session.summarized_upto
        pending = self.memory.pending_for_summary(key, self.keep_turns)
        if not pending:
            return False

        body = _render(pending)
        if not body.strip():
            # 全是图片/空消息 —— 没内容可压，但**游标必须推过去**，
            # 否则每轮都会重新捡起同一批空消息，白转一圈而且永远卡住。
            self.memory.set_summary(
                key,
                self.memory.summary_of(key),
                self.memory.summarized_upto_of(key) + len(pending),
            )
            log.debug("会话 %s 这批 %d 条没有可摘要文本，仅推进游标", key, len(pending))
            return False

        old = self.memory.summary_of(key)
        user_block = ""
        if old:
            user_block += f"【已有摘要】\n{old}\n\n"
        user_block += f"【要合并进去的新对话】\n{body}"

        try:
            text = await self.llm.chat(
                [
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": user_block},
                ]
            )
        except LLMError as exc:
            # 摘要失败不该影响聊天。下一轮再试，历史照旧。
            log.warning("会话 %s 摘要失败（跳过，下轮再试）：%s", key, exc)
            return False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("会话 %s 摘要时发生未预期异常", key)
            return False

        text = (text or "").strip()
        if self.memory.get(key) is not session or session.revision != revision:
            return False
        if not text:
            log.warning("会话 %s 摘要返回空文本，保留旧摘要", key)
            return False
        if len(text) > self.max_chars:
            # 超长说明模型没照要求写。截断而不是丢弃 —— 留着前半段也比没有强。
            log.warning("会话 %s 摘要超长（%d 字），已截断", key, len(text))
            text = text[: self.max_chars]

        upto = start + len(pending)
        self.memory.set_summary(key, text, upto)
        self.compressed_count += 1
        log.info(
            "会话 %s 已摘要：压缩 %d 条消息 → %d 字（累计覆盖到第 %d 条）",
            key, len(pending), len(text), upto,
        )
        return True

    async def run_once(self) -> int:
        """把所有该压的会话过一遍，返回压了几个。"""
        done = 0
        for key in self.memory.sessions_with_messages():
            try:
                if await self.summarize_session(key):
                    done += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("处理会话 %s 的摘要时异常，继续下一个", key)
        return done

    async def loop(self, interval_seconds: float = 600.0) -> None:
        """后台循环。间隔默认 10 分钟。"""
        log.info("摘要循环已启动，每 %.0f 秒检查一次", interval_seconds)
        while True:
            await asyncio.sleep(max(30.0, float(interval_seconds)))
            try:
                done = await self.run_once()
                if done:
                    log.info("本轮摘要完成 %d 个会话", done)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("摘要循环出错，等待下一轮")
