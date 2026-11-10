"""OneBot 11 消息段解析与构造。

OneBot 11 有两种消息表示：
- 字符串形式（CQ 码）："你好[CQ:at,qq=123]"
- 数组形式：[{"type":"text","data":{"text":"你好"}}, {"type":"at","data":{"qq":"123"}}]

NapCat 默认给数组形式。这里统一成数组，字符串形式做兜底解析。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger(__name__)

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_][a-zA-Z0-9_]*)((?:,[^\[\]]*)?)\]")
_SEGMENT_ALIASES = {
    "record": "voice",
    "audio": "voice",
    "face": "face",
    "reply": "reply",
    "json": "json",
    "xml": "xml",
}

# 在纯文本里怎么表示这些非文字消息段
_TEXT_MARKERS = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "music": "[音乐]",
    "forward": "[合并转发]",
    "poke": "[戳一戳]",
    "mface": "[表情包]",
}


def _unescape(value: str) -> str:
    """还原 CQ 码里的转义。"""
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def parse_cq(text: str) -> List[Dict[str, Any]]:
    """把 CQ 码字符串拆成消息段数组。"""
    segments: List[Dict[str, Any]] = []
    pos = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > pos:
            plain = text[pos : match.start()]
            if plain:
                segments.append({"type": "text", "data": {"text": _unescape(plain)}})

        seg_type = _SEGMENT_ALIASES.get(match.group(1), match.group(1))
        data: Dict[str, Any] = {}
        raw_params = match.group(2) or ""
        if raw_params.startswith(","):
            raw_params = raw_params[1:]
        if raw_params:
            for pair in raw_params.split(","):
                if "=" not in pair:
                    continue
                key, _, value = pair.partition("=")
                data[key.strip()] = _unescape(value.strip())
        segments.append({"type": seg_type, "data": data})
        pos = match.end()

    if pos < len(text):
        tail = text[pos:]
        if tail:
            segments.append({"type": "text", "data": {"text": _unescape(tail)}})

    return segments


def _stringify(value: Any) -> str:
    """把消息段 data 里的值统一成字符串。

    CQ 码解析出来的永远是字符串，数组形式却可能给 int/bool。
    不统一的话，同一条消息走两条路径会得到不同结构，下游判定容易踩坑。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize(raw: Union[str, List[Dict[str, Any]], None]) -> List[Dict[str, Any]]:
    """把任意形式的 message 字段统一成消息段数组。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return parse_cq(raw)
    if isinstance(raw, list):
        result: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            seg_type = str(item.get("type", "text"))
            seg_type = _SEGMENT_ALIASES.get(seg_type, seg_type)
            data = item.get("data")
            if not isinstance(data, dict):
                data = {} if data is None else {"value": data}
            result.append(
                {"type": seg_type, "data": {k: _stringify(v) for k, v in data.items()}}
            )
        return result
    return []


def to_text(segments: List[Dict[str, Any]]) -> str:
    """转成给人看的纯文本，非文字段用标记占位。"""
    parts: List[str] = []
    for seg in segments:
        seg_type = seg.get("type")
        data = seg.get("data", {})
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            parts.append("@全体成员" if qq == "all" else f"@{data.get('name') or qq}")
        elif seg_type in _TEXT_MARKERS:
            parts.append(_TEXT_MARKERS[seg_type])
        elif seg_type == "reply":
            continue
        else:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def images(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """取出所有图片段。"""
    return [seg for seg in segments if seg.get("type") == "image"]


def is_at_me(segments: List[Dict[str, Any]], self_id: Optional[int]) -> bool:
    """消息里是否 @ 了机器人（或 @全体成员）。"""
    for seg in segments:
        if seg.get("type") != "at":
            continue
        qq = str(seg.get("data", {}).get("qq", ""))
        if qq == "all":
            return True
        if self_id is not None and qq == str(self_id):
            return True
    return False


def at_others(segments: List[Dict[str, Any]], self_id: Optional[int]) -> bool:
    """消息里是否 @ 了**除机器人以外的人**。

    和 `is_at_me` 是互补的判定，用途完全不同：
    `is_at_me` 回答「要不要理这条」，这个回答「这条是不是在跟别人说话」。

    为什么需要它（实测 2026-10-02）：机器人被 @ 过一次后，`group_activate_minutes`
    会给这个群开一段「激活窗口」，窗口内**不带 @ 的消息也回**——本意是让连续
    对话接得上。但窗口期里出现「@3321181587 你猜」这种**@别人**的消息时，
    机器人照样回话，用户的原话是「艾特别人时他插话」。
    根因是把「群刚被叫醒」当成了「这条在叫我」，所以这里要能把两者区分开。

    **@全体成员（qq == "all"）不算「@ 别人」** —— 那是叫所有人，机器人被
    顺带叫到，理它是合理的。
    """
    for seg in segments:
        if seg.get("type") != "at":
            continue
        qq = str(seg.get("data", {}).get("qq", ""))
        if qq == "all":
            continue
        if self_id is not None and qq == str(self_id):
            continue
        return True
    return False


def strip_at(segments: List[Dict[str, Any]], self_id: Optional[int]) -> List[Dict[str, Any]]:
    """去掉 @机器人 的段（@全体成员 保留），让喂给模型的内容更干净。"""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if self_id is not None and qq == str(self_id):
                continue
        result.append(seg)
    return result


def strip_prefix(text: str, prefixes: List[str]) -> Optional[str]:
    """如果文本以某个前缀开头，返回去掉前缀后的内容；否则返回 None。"""
    stripped = text.lstrip()
    for prefix in prefixes:
        if not prefix:
            continue
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix) :].strip()
    return None


def chunk_text(text: str, limit: int) -> List[str]:
    """把长回复切成多条，优先在换行/句号处断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", ";", "，", ",", " "):
            idx = window.rfind(sep)
            if idx > limit * 0.5:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def split_messages(text: str, separator: str = "|||", max_parts: int = 3) -> List[str]:
    """按**模型自己标的边界**把一条回复拆成多条消息。

    ## 为什么还要这个（不是已经有 chunk_text 了吗）

    `chunk_text()` 只在**超长**时才切 —— 正常长度的回复永远是一条消息。
    真人聊天不是这样：想到一件说一件，经常连着发两三条短的
    （「诶在呢」/「你找我啥事」）。用户明确要的就是这个。

    ## 为什么让模型标边界，而不是按句号 / 长度自动切

    自动切会切在错的地方。「你去吗 ||| 我不去」这种对话式的分界，
    按句号切不出来。模型知道语义边界在哪。

    代价是**模型可能把「分 2~3 条」变成新的模板**（跟人设例句被逐字抄
    同一个病根），所以 prompt 里必须写「默认一条说完」，并且
    **历史里存合并后的版本** —— 不让它从自己的历史里学到「我总是分几条」。

    ## 边界

    * 开头 / 结尾的分隔符、连续分隔符 → 产生的空块丢掉
    * 没有分隔符 → 原样返回一条（**不是空列表**）
    * 超过 `max_parts` → 多出来的**并入最后一条**，内容不能丢
    """
    if not text or not separator:
        return [text] if (text and text.strip()) else []

    parts = [p.strip() for p in text.split(separator)]
    parts = [p for p in parts if p]
    if not parts:
        return []
    if len(parts) == 1:
        return parts

    limit = max(1, int(max_parts))
    if len(parts) <= limit:
        return parts
    # 超出的部分并入最后一条 —— 宁可最后一条长一点，也不能把内容丢了
    return parts[: limit - 1] + ["\n".join(parts[limit - 1 :])]


def build_text_message(text: str, reply_id: Optional[int] = None, at_user: Optional[int] = None) -> List[Dict[str, Any]]:
    """构造要发出去的消息段数组。"""
    segments: List[Dict[str, Any]] = []
    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})
    if at_user is not None:
        segments.append({"type": "at", "data": {"qq": str(at_user)}})
        segments.append({"type": "text", "data": {"text": " "}})
    segments.append({"type": "text", "data": {"text": text}})
    return segments


def build_image_message(
    image_b64: str, reply_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """构造一条图片消息。

    图片用 `base64://` 内联，不走外部图床 —— 少一个会过期的中间环节，
    也就少一种「发出去是坏图」的可能。NapCat 两种写法都吃。
    """
    segments: List[Dict[str, Any]] = []
    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})
    segments.append({"type": "image", "data": {"file": f"base64://{image_b64}"}})
    return segments
