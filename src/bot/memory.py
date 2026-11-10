"""多轮上下文记忆。

按会话维度（群 / 私聊）保存历史，带 TTL、最大轮数、可选的 JSON 持久化。
每个会话一把 asyncio.Lock —— 同一个人连发几条消息时不会串行错乱。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .llm import prepend_speaker

log = logging.getLogger(__name__)

Message = Dict[str, Any]
IMAGE_TTL_SECONDS = 900.0


class Session:
    __slots__ = ("key", "messages", "updated_at", "lock", "summary", "summarized_upto", "generation", "revision")

    def __init__(self, key: str) -> None:
        self.key = key
        self.messages: List[Message] = []
        self.updated_at = time.time()
        self.lock = asyncio.Lock()
        self.generation = 0
        self.revision = 0
        # 被压掉的旧对话的摘要。见 bot/summarize.py —— 它替代的是**本来就要被
        # _trim 丢掉**的那些消息，所以是纯赚，不会挤掉还能留住的细节。
        self.summary = ""
        # 摘要已经覆盖到「消息列表里的第几条」（索引）。用它判断有没有新内容要压，
        # 也用来在历史被裁掉之后重新对齐下标。
        self.summarized_upto = 0


class MemoryStore:
    def __init__(
        self,
        max_turns: int = 20,
        ttl_minutes: int = 120,
        persist_path: Optional[Path] = None,
        max_sessions: int = 1000,
        summary_batch_turns: int = 6,
    ) -> None:
        self.max_turns = max(0, int(max_turns))
        self.ttl_seconds = max(0, int(ttl_minutes)) * 60
        self.persist_path = Path(persist_path) if persist_path else None
        self.max_sessions = max(1, int(max_sessions))
        # 攒够这么多轮「尚未摘要」的新对话就可以压一次，不必等历史上限。
        # 见 pending_for_summary 的说明 —— 只按上限触发的话摘要永远慢一步。
        self.summary_batch_turns = max(1, int(summary_batch_turns))

        self._sessions: Dict[str, Session] = {}
        self._dirty = False

        # 每个会话最近发来的参考图（data URI）+ 时间戳，跟着历史一起持久化。
        #
        # **为什么要落盘**：这是实测踩出来的 —— 用户「先发图、隔几分钟再说要画」，
        # 而机器人恰好在这中间重启过（我们在调东西，它重启得很频繁），
        # 内存里的缓存就没了。表现是模型明明填了 reference_image，
        # 工具却说「本轮只有 0 张」，然后凭空重画一张。
        # 它能直接复用同一份 _dirty / save 机制，代价可忽略。
        #
        # 只留最近一张：既够用，又避免把 base64 越堆越多（每条都是几百 KB）。
        self._session_images: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #

    def get(self, key: str) -> Session:
        session = self._sessions.get(key)
        if session is None:
            session = Session(key)
            self._sessions[key] = session
            self._evict_if_needed()
        return session

    def _evict_if_needed(self) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        # 按最后活跃时间淘汰最旧的
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].updated_at)
        for key, _ in ordered[: len(self._sessions) - self.max_sessions]:
            self._sessions.pop(key, None)
            self.forget_images(key)
        self._dirty = True

    # ------------------------------------------------------------------ #

    def _trim(self, session: Session) -> None:
        """在写入时就裁掉超出的历史。

        关键：不能只在读取时裁剪 —— 那样存储层是无界增长的，
        跑几天下来内存和 history.json 都会越滚越大。

        **裁剪时必须同步下移 `summarized_upto`**：那个游标记的是「列表里的第几条」，
        而从头部删消息会让所有下标前移。不跟着挪的话游标就指向了错误的位置 ——
        表现是摘要第二次压缩挑错区间、甚至永远挑空（踩过这个坑，而且不报错，
        只是摘要从此不再更新）。
        """
        limit = self.max_turns * 2
        if limit <= 0:
            session.messages.clear()
            session.summarized_upto = 0
            return
        overflow = len(session.messages) - limit
        if overflow > 0:
            del session.messages[:overflow]
            # 被删掉的这些如果已经进过摘要，游标要跟着减；否则夹到 0。
            session.summarized_upto = max(0, session.summarized_upto - overflow)

    def append(self, key: str, role: str, content: Any, **extra: Any) -> None:
        """追加一条消息。

        `extra` 是给调用方挂元数据用的（目前只有生图那轮的 `draws` 计数）。
        刻意不在这里解释它的含义 —— 存储层只管存，
        怎么用由 `core._expand_history_traces()` 决定。
        """
        session = self.get(key)
        # `at` 是给「清除一段时间内的记忆」用的时间戳（见 `forget_recent`）。
        # 它**只用于内部判断**，喂模型前会被 `context()` 剥掉 —— 大模型 API
        # 不认多余字段，带着发出去等于自找 400。
        message: Message = {"role": role, "content": content, "at": time.time()}
        if extra:
            message.update(extra)
        session.messages.append(message)
        session.revision += 1
        session.updated_at = time.time()
        self._trim(session)
        self._dirty = True

    def extend(self, key: str, messages: List[Message]) -> None:
        session = self.get(key)
        session.messages.extend(messages)
        session.revision += 1
        session.updated_at = time.time()
        self._trim(session)
        self._dirty = True

    def clear(self, key: str, image_keys: Sequence[str] = ()) -> int:
        """清空整个会话：原文 + 摘要 + 参考图缓存。

        ## ⚠️ 摘要必须一起清（2026-10-03 修的真实 bug）

        摘要会被 `context()` **插在历史最前面**发给模型。原来这里只清了
        `messages`，摘要原封不动 —— 于是用户发 `/reset`，机器人回一句
        「已清空上下文」，下一句却还认得他。表现就是「**说了清空但没清**」。

        同一批数据里 `summary` 和 `summarized_upto` 是一对，必须一起复位：
        只清 `summary` 而留着 `summarized_upto` 会让 `pending_for_summary()`
        算出负区间，摘要从此再也不更新（不报错，只是永远不压了）。

        `image_keys` 兼容调用方提供的其他缓存键；正常业务只清当前会话。
        """
        session = self._sessions.get(key)
        if session is None:
            # 会话对象不在，但参考图缓存可能是独立存在的 —— 顺手清掉再返回。
            self.forget_images(key, *image_keys)
            return 0
        count = len(session.messages)
        session.generation += 1
        session.revision += 1
        session.messages.clear()
        session.summary = ""
        session.summarized_upto = 0
        session.updated_at = time.time()
        self.forget_images(key, *image_keys)
        self._dirty = True
        return count

    def forget_images(self, *keys: str) -> int:
        """丢掉这些键的参考图缓存，返回清掉几个键。

        仅删除明确传入的会话键，不跨私聊或群聊查找。
        """
        dropped = 0
        for key in keys:
            if key and self._session_images.pop(str(key), None) is not None:
                dropped += 1
        if dropped:
            self._dirty = True
        return dropped

    def forget_recent(
        self,
        key: str,
        turns: int = 0,
        seconds: float = 0.0,
        image_keys: Sequence[str] = (),
    ) -> int:
        """丢掉**最近**的若干轮 / 若干秒，返回删掉几条。

        和 `clear()` 的区别：clear 是整个会话推平重来，这里是「把刚才那几句
        抹掉」，更早的对话照旧留着 —— 用户说的「清除一段时间内的记忆」是后者。

        ## 两个维度（二选一，turns 优先）

          · `turns`  ：一轮 = 用户+助手两条（和 `_trim` 里 `max_turns * 2` 对齐）
          · `seconds`：按消息上的 `at` 时间戳

        ## 旧消息没有 `at`（这个字段是后加的）

        按时间删时从尾部往回走，**遇到没有 `at` 的就停下** —— 宁可少删，
        也别把时间不明的老对话误当成「最近」删掉。重启一次之后就都有 `at` 了。

        ## 一个必须注意的点

        删的是**尾部**，而 `summarized_upto` 记的是「摘要覆盖到第几条」，
        可能被指到列表外面去。必须夹到当前长度，否则摘要压缩会挑错区间
        （踩过这个坑：不报错，只是摘要从此不再更新）。
        """
        if turns <= 0 and seconds <= 0:
            return 0
        session = self._sessions.get(key)
        self.forget_images(key, *image_keys)
        if session is None:
            return 0
        session.generation += 1
        session.revision += 1
        if not session.messages:
            return 0

        total = len(session.messages)
        if turns > 0:
            drop = min(total, int(turns) * 2)
        elif seconds > 0:
            deadline = time.time() - float(seconds)
            drop = 0
            for message in reversed(session.messages):
                at = message.get("at")
                if at is None or float(at) < deadline:
                    break
                drop += 1
        else:
            return 0

        if drop <= 0:
            return 0

        del session.messages[total - drop:]

        # ---- 摘要：被删的那段只要和它有重叠，就必须一起丢 ----
        #
        # `summarized_upto` = 摘要覆盖到「消息列表的第几条」。删掉尾部之后，
        # 如果新长度比它还短，就说明**摘要里包含着刚被删掉的内容**。
        #
        # 摘要是一段自然语言，没法从里面精确摘掉某几句 —— 所以只能整个丢掉。
        # 宁可多忘一点，也不能「说忘了其实没忘」：用户发 /forget 就是要它忘，
        # 结果它还记着，比不做更糟（他会认为机器人不听话）。
        if len(session.messages) < session.summarized_upto:
            session.summary = ""
        session.summarized_upto = max(
            0, min(session.summarized_upto, len(session.messages))
        )
        session.updated_at = time.time()
        # 参考图也一起丢：它只存「最近一条消息」的那几张，按定义就是"最近的东西"，
        # 而 /forget 正是要忘掉最近的。不清的话用户会看到机器人还在用那张图。
        self.forget_images(key, *image_keys)
        self._dirty = True
        return drop

    def context(self, key: str, *, show_sender: bool = False) -> List[Message]:
        """返回可直接喂给模型的裁剪后历史。

        如果这个会话有摘要，摘要会作为**第一条**插在最前面，后面接还没被压掉的
        近期消息。这样模型每轮都能看到早期对话的要点，而不是彻底忘掉 ——
        那些原文本来已经被 _trim 丢掉了。

        顺序上有个坑（踩过一次）：**必须先裁近期消息、再插摘要**。
        反过来写的话，`messages[-limit:]` 会把刚插进去的摘要当成"最旧的一条"
        又切掉，摘要等于白写 —— 而且不报错，只是模型永远看不到它。

        ## `show_sender`：群聊必须开

        存储层给 user 消息挂的 `sender` 元数据，在这里变成 `[昵称] ` 前缀。
        **不开的话模型分不清谁是谁** —— OpenAI 协议的 `user` 角色天然暗示
        「同一个人」，历史里几个群友的发言会被它算成一个人说的。
        2026-10-27 用户报的「把两个群友当成一个人」就是这个。

        私聊不用开：只有一个人，前缀是纯噪音。

        ⚠️ `sender` 是**内部字段，无论开不开都必须剥掉**。带着发出去
        大模型 API 会直接 400（未知字段）。
        """
        session = self._sessions.get(key)
        if session is None:
            return []

        messages = session.messages
        limit = self.max_turns * 2
        if limit and len(messages) > limit:
            messages = messages[-limit:]

        # 首条必须是 user —— 从 assistant 开头的历史容易让模型跑偏
        while messages and messages[0].get("role") != "user":
            messages = messages[1:]
        # 末条如果是 user（说明上一轮没答完），去掉避免重复
        while messages and messages[-1].get("role") != "assistant":
            messages = messages[:-1]

        # 剥掉 `at`：那是内部时间戳，大模型 API 不认这个字段，带着发出去
        # 可能被直接拒（400）。旧数据本来就没有 `at`，这样也顺手兼容。
        #
        # `sender` 同理 —— 顺便在这里按需转成可见前缀。
        out: List[Message] = []
        for m in messages:
            item = {k: v for k, v in m.items() if k != "at"}
            speaker = item.pop("sender", None)
            if show_sender and speaker and item.get("role") == "user":
                item["content"] = prepend_speaker(item.get("content"), str(speaker))
            out.append(item)

        if session.summary:
            # 摘要用 user 角色、并标明「这是摘要不是原话」—— 否则模型会把它
            # 当成自己上轮说过的话照抄，于是开始用第三人称谈自己。
            out.insert(0, {
                "role": "user",
                "content": f"（以下是更早对话的摘要，不是原话）\n{session.summary}",
            })
            # 摘要之后必须跟一条 assistant：两条 user 连在一起，模型会当成
            # 一次说了两句。给一条极短确认，让消息序列合法。
            out.insert(1, {"role": "assistant", "content": "（记住了）"})
        return out

    # ------------------------------------------------------------------ #
    # 摘要（见 bot/summarize.py）

    def pending_for_summary(self, key: str, keep_turns: int) -> List[Message]:
        """挑出「还没被摘要、而且即将要被裁掉」的那批消息。

        为什么要挑，而不是无脑压最旧的：摘要的价值全在「替代原本会被丢掉的内容」。
        去压还留得住的消息等于用一句概括换掉真实细节 —— 那是净亏。

        触发条件有两个，**取先到的那个**（这里踩过一次坑）：
          · 已经达到历史上限（再多一条就会把最旧的挤出去）
          · 已经攒够 `summary_batch_turns` 轮新对话

        为什么要第二个条件：只按「达到上限」触发的话，摘要永远慢一步 ——
        等到超限时最旧的内容**已经被 _trim 删掉了**，没东西可压。
        而且历史不足上限的会话（比如只聊了 8 轮）永远不会被摘要，
        可它的早期细节同样会随对话变长而丢失。加一个批量阈值让它提前动手。

        `keep_turns` 是压缩时留出的余量，避免只压到半句话。
        """
        session = self._sessions.get(key)
        if session is None or not session.messages:
            return []

        limit = self.max_turns * 2
        if limit <= 0:
            return []

        total = len(session.messages)
        at_capacity = total >= limit
        enough_batch = (total - session.summarized_upto) >= self.summary_batch_turns * 2
        if not (at_capacity or enough_batch):
            return []

        # 保留最近 keep_turns 轮不碰（给摘要留上下文余量）
        end = max(0, total - max(0, int(keep_turns)) * 2)
        start = session.summarized_upto
        if end <= start:
            return []
        return [dict(m) for m in session.messages[start:end]]

    def set_summary(self, key: str, summary: str, upto: int) -> None:
        """写入摘要，并记下它覆盖到第几条。"""
        session = self._sessions.get(key)
        if session is None:
            return
        session.summary = summary.strip()
        session.summarized_upto = max(session.summarized_upto, int(upto))
        session.revision += 1
        self._dirty = True

    def sessions_with_messages(self) -> List[str]:
        """有内容的会话 key（摘要循环要遍历的就是这些）。"""
        return [k for k, s in self._sessions.items() if s.messages]

    def summary_of(self, key: str) -> str:
        session = self._sessions.get(key)
        return session.summary if session else ""

    def summarized_upto_of(self, key: str) -> int:
        session = self._sessions.get(key)
        return session.summarized_upto if session else 0

    def stats(self, key: str) -> Dict[str, Any]:
        session = self._sessions.get(key)
        if session is None:
            return {"turns": 0, "messages": 0}
        return {
            "turns": len([m for m in session.messages if m.get("role") == "user"]),
            "messages": len(session.messages),
            "idle_seconds": int(time.time() - session.updated_at),
        }

    # ------------------------------------------------------------------ #

    def gc(self) -> int:
        """清掉过期会话，返回清理数量。"""
        self._gc_images()
        if not self.ttl_seconds:
            return 0
        deadline = time.time() - self.ttl_seconds
        expired = [k for k, s in self._sessions.items() if s.updated_at < deadline and not s.lock.locked()]
        for key in expired:
            self._sessions.pop(key, None)
            self.forget_images(key)
        if expired:
            self._dirty = True
            log.debug("清理过期会话 %d 个", len(expired))
        return len(expired)

    async def gc_loop(self, interval_seconds: int = 300) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.gc()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("会话清理失败")

    # ------------------------------------------------------------------ #

    def save(self, force: bool = False) -> None:
        if self.persist_path is None:
            return
        self._gc_images()
        if not self._dirty and not force:
            return
        payload = {
            "version": 4,   # v4 的图片缓存严格按会话隔离
            "saved_at": time.time(),
            "images": self._session_images,
            "summaries": {
                key: {
                    "text": session.summary,
                    "upto": session.summarized_upto,
                }
                for key, session in self._sessions.items()
                if session.summary
            },
            "sessions": {
                key: session.messages
                for key, session in self._sessions.items()
                if session.messages
            },
        }
        tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.persist_path)  # 原子替换，避免写一半崩了把历史弄坏
            self._dirty = False
        except Exception:  # noqa: BLE001
            log.exception("保存历史失败")

    def load(self) -> int:
        if self.persist_path is None or not self.persist_path.exists():
            return 0
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.exception("读取历史失败，将从空历史开始")
            return 0

        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, dict):
            return 0

        loaded = 0
        now = time.time()
        for key, messages in sessions.items():
            if not isinstance(messages, list) or not messages:
                continue
            session = Session(str(key))
            session.messages = [m for m in messages if isinstance(m, dict) and "role" in m]
            session.updated_at = now
            self._sessions[str(key)] = session
            loaded += 1

        # 摘要（v2 才有；老文件没有这个字段，读不到就当空摘要，不报错）
        summaries = payload.get("summaries") if isinstance(payload, dict) else None
        if isinstance(summaries, dict):
            for key, item in summaries.items():
                session = self._sessions.get(str(key))
                if session is None or not isinstance(item, dict):
                    continue
                session.summary = str(item.get("text") or "").strip()
                try:
                    session.summarized_upto = int(item.get("upto") or 0)
                except (TypeError, ValueError):
                    session.summarized_upto = 0
                # 历史条数可能比存的时候少（比如手工编辑过 history.json），
                # 这时把下标夹回合法范围，否则 pending_for_summary 会算出空区间、
                # 摘要永远卡住不更新。
                session.summarized_upto = min(session.summarized_upto, len(session.messages))

        self._dirty = False
        # 旧缓存混用了群聊和私聊来源，不能安全迁移；消息与摘要照常保留。
        images = payload.get("images") if isinstance(payload, dict) else None
        if images and payload.get("version") != 4:
            images = None
            self._dirty = True
        if isinstance(images, dict):
            for key, item in images.items():
                if not isinstance(item, dict):
                    continue
                uris = item.get("uris")
                stamp = item.get("at")
                if not isinstance(uris, list) or not uris:
                    continue
                try:
                    stamp_f = float(stamp)
                except (TypeError, ValueError):
                    continue
                self._session_images[str(key)] = {"at": stamp_f, "uris": [str(u) for u in uris]}

        self._gc_images()
        log.info(
            "已恢复 %d 个会话的历史（其中 %d 个带摘要，%d 个带参考图缓存）",
            loaded, sum(1 for s in self._sessions.values() if s.summary),
            len(self._session_images),
        )
        return loaded

    # ------------------------------------------------------------------ #
    # 参考图缓存

    def _gc_images(self) -> None:
        cutoff = time.time() - IMAGE_TTL_SECONDS
        expired = [key for key, item in self._session_images.items() if float(item.get("at") or 0) < cutoff]
        self.forget_images(*expired)
        excess = len(self._session_images) - self.max_sessions
        if excess > 0:
            oldest = sorted(self._session_images, key=lambda key: self._session_images[key]["at"])
            self.forget_images(*oldest[:excess])

    def remember_images(self, key: str, uris: List[str]) -> None:
        """记下某个会话最近发来的参考图（data URI）。

        只留最近一条消息里的那几张：更早的图对「用这张画」没有意义，
        而且每条 data URI 都是几百 KB，攒起来会把 history.json 撑大。
        """
        if not uris:
            return
        self._session_images[str(key)] = {"at": time.time(), "uris": list(uris)}
        self._gc_images()
        self._dirty = True

    def recent_images(
        self, key: str, ttl_seconds: float
    ) -> Optional[Tuple[float, List[str]]]:
        """取回没过期的参考图，返回 (距今秒数, data URI 列表)；没有就返回 None。

        过期判定放在这里而不是 load()：TTL 是调用方的配置，存储层不该复制一份。
        """
        item = self._session_images.get(str(key))
        if not isinstance(item, dict):
            return None
        age = time.time() - float(item.get("at") or 0.0)
        if ttl_seconds > 0 and age > ttl_seconds:
            # 顺手清掉，别让它一直占着内存和文件
            self._session_images.pop(str(key), None)
            self._dirty = True
            return None
        uris = [str(u) for u in (item.get("uris") or [])]
        return (age, uris) if uris else None

    async def autosave_loop(self, interval_seconds: int = 30) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.save()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("自动保存失败")
