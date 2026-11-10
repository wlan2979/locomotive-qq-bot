"""语音合成：把文本丢给本机 VoiceStudio，拿回音频，当 QQ 语音发出去。

## 为什么走 VoiceStudio 而不是在线 TTS

和生图那边是同一个取舍：本机跑 = **免费、不限量、不出网、没有 key 会过期**。
代价是它得开着，所以这里的失败路径必须是「明确报错 + 降级成文字」，
不能是静默吞掉 —— 用户发 `/语音` 却什么都没收到，比收到一句
「语音没合成出来」难查十倍。

## 和表情包/生图的结构对齐

* **有则用、无则退**：后端连不上就退回发文字，接口不变。
* **副作用在工具里**：音频不可能经模型的手传出去，工具直接发。
  回给模型的那句是旁白（「已经发出去了」），否则它会开始复述内容。
* **每轮限一次**：和表情包同理，语音比表情更重（要等合成），
  一轮发两条必然显得吵。

## 协议端 那边怎么收

OneBot 11 的 `record` 段，协议端 收到后自己用 ffmpeg 转成 silk 再发。
`file` 支持**本地绝对路径**、`file://` URI、`base64://` 三种写法 ——
但**哪个能用取决于 协议端 版本**，没有一份权威说法。

所以这里不赌：`_send_voice` 会**依次试**（配置的那个优先，最后兜底 base64），
任一条成功就停。每次尝试都是一次本机 WS 调用，几毫秒，成本可以忽略。
（`tests/probe_voice.py` 是单独探这个的脚本，想知道具体哪个版本吃哪种，
直接跑它。）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)
PAUSE_FILE = Path(r"D:\DS\lora-tools\.gpu-lease-paused")

# 合成缓存目录。同一句话重复合成是纯浪费（本机引擎一次要几秒），
# 而聊天里「哈哈哈」「好」这种短句重复率极高。
CACHE_DIR_NAME = "tts"

# 单次合成的文本上限。QQ 语音超过 60 秒就变成「长语音」，
# 手机上体验很差，调用方应按完整句子分段。
MAX_CHARS = 180

# ComfyUI 的显存释放接口。
#
# 这台机器只有 8GB 显存：ComfyUI 出图时占 5GB+，VoiceStudio 加载模型又要
# 5GB，两边同时驻留必然 CUDA OOM。ComfyUI 提供了 `POST /free` 可以立刻卸载
# 模型，所以在语音请求发出前先捅一下它，让它把显存让出来。
#
# 本机画图前由 core 请求卸载空闲 IndexTTS；两类推理在 QQ 内串行。
_COMFYUI_FREE_URL = os.environ.get(
    "QQBOT_COMFYUI_FREE_URL", "http://127.0.0.1:8188/free"
)
# 释放请求的超时。要短 —— 这是「顺手清一下」，不能拖慢语音。
# ComfyUI 没开时连接会被立刻拒绝，正常情况远低于这个值。
_COMFYUI_FREE_TIMEOUT = 3.0

# --------------------------------------------------------------------------- #
# 已知音色（2026-10-19 整理）
#
# VoiceStudio 的克隆音色，全部是「桃桃」这个人设的克隆。想换声线就改
# `tts.voice`；**缓存 key 里有 voice，换完自动失效旧音频，不用手动清。**
#
# | voice        | 名字             | 备注 |
# |--------------|------------------|------|
# | `84724f04`   | 桃桃·I           | **2026-10-30 起在用**（用户指定）。直播 `tts.env` 也是这个 |
# | `dfb7f486`   | 桃桃（旧默认）   | 参考音频最短 |
# | `91d1a6d3`   | 桃桃·ATRI-全     | **参考最长**，样本输出最大，漂移最少（音色漂移严重时换这个） |
# | `e337e9a7`   | 桃桃·ATRI-归一   | 同为长参考 |
# | `3de74289`   | 桃桃·ATRI-6s     | 只 6 秒参考，漂移风险最高 |
# | `29b1cff3`   | 桃桃·高音版      | 更尖更幼 |
#
# ## 音色漂移的真相
#
# 「前半句音色不对」是 omnivoice 克隆后端的固性问题，**换音色只能缓解**。
# 证据（`D:\DS\lora-tools\_tts_samples\09_当前在用_重复a.wav` 与
# `10_当前在用_重复b.wav`）：同一音色、同一句日语、同一参数，跑两遍
# 输出大小都不一样（96044 vs 96524 字节）—— 这个引擎**没有可固定的 seed**，
# 每次合成都是一次新的采样，必然不完全一致。
# 唯一的抓手是选「参考音频更长更完整」的音色（ATRI-全 / 归一）。
#
# ## 文本必须显式传 language
#
# 克隆音色的参考音频是外语时不传 `language`，模型会跟着参考走 ——
# 实测日语参考直接合成出日语，中文全废。所以 `tts.language` 必填。
# --------------------------------------------------------------------------- #


class VoiceUnavailable(RuntimeError):
    """后端没起来 / 连不上 —— 调用方通常要降级成文字。"""


class VoiceError(RuntimeError):
    """后端起来了但合成失败（音色不存在、引擎没装等）。"""


def _clean(text: str) -> str:
    """把文本收拾成适合朗读的样子。

    聊天文本里有大量**给眼睛看、给耳朵听就是噪音**的东西：
    markdown 强调符、`|||` 分条符、代码块、URL。念出来是灾难。
    这里只做保守清理 —— 宁可留一点，也别把正文吃掉了。
    """
    text = text or ""
    # 分条符是机器人自己的协议，绝不能念出来
    text = text.replace("|||", "，")
    text = re.sub(r"\s*，\s*", "，", text)
    # 代码块整段去掉（念代码没有意义）
    text = re.sub(r"```.*?```", "（代码）", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # markdown 强调/标题/列表符号。
    # `\s+` 而不是 `\s*`：`#1 号方案` 的 `#` 是正文，不是标题标记。
    # 用 `\s*` 会把井号吃掉，念出来变成「1 号方案」—— 少了半句话。
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]*)\*(?!\*)", r"\1", text)
    text = re.sub(r"^[#>\-\*\+]\s+", "", text, flags=re.M)
    # 链接只留文字
    text = re.sub(r"\[([^\]]*)\]\((?:[^)]*)\)", r"\1", text)
    text = re.sub(r"https?://\S+", "（链接）", text)
    # 表情/emoji 附近的空白收一收
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _cache_path(cfg: Any, text: str) -> Path:
    """缓存文件路径。按「音色 + 语言 + 步数 + 文本」算哈希 —— 任一变了都不该命中旧缓存。"""
    key = (
        f"{getattr(cfg, 'voice', '')}|{getattr(cfg, 'model', '')}"
        f"|{getattr(cfg, 'language', '')}|{getattr(cfg, 'num_step', 0)}|{text}"
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    base = Path(getattr(cfg, "cache_dir", "") or ".")
    return base / CACHE_DIR_NAME / f"{digest}.{getattr(cfg, 'format', 'wav') or 'wav'}"


async def _release_comfyui_vram(http: aiohttp.ClientSession) -> None:
    """尽力让 ComfyUI 卸载模型、释放显存。

    刻意吞掉所有异常：ComfyUI 没开、超时、返回非 200 都只意味着「这次没让出
    显存」，不该让语音功能跟着挂。真 OOM 时 VoiceStudio 会自己报错，这里再抛
    异常只会把「语音失败」的原因指向错误的地方。
    """
    try:
        async with http.post(
            _COMFYUI_FREE_URL,
            json={"unload_models": True, "free_memory": True},
            timeout=aiohttp.ClientTimeout(total=_COMFYUI_FREE_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                log.info("已请求 ComfyUI 释放显存（unload_models + free_memory）")
            else:
                log.debug("ComfyUI /free 返回 HTTP %s", resp.status)
    except Exception as exc:  # noqa: BLE001 — 见上，这里就是要全吞
        log.debug("跳过 ComfyUI 显存释放：%s", exc)


def split_for_speech(text: str) -> List[str]:
    text = _clean(text)
    chunks = []
    while len(text) > MAX_CHARS:
        boundary = max(text.rfind(mark, 0, MAX_CHARS) for mark in "。！？!?；;\n")
        if boundary < 0:
            boundary = text.rfind(" ", 0, MAX_CHARS)
        end = boundary + 1 if boundary >= 0 else MAX_CHARS
        chunks.append(text[:end].strip())
        text = text[end:].strip()
    if text:
        chunks.append(text)
    return chunks


async def synthesize(
    http: aiohttp.ClientSession, cfg: Any, text: str
) -> bytes:
    """合成一段语音，返回音频字节。

    抛 `VoiceUnavailable`（连不上）或 `VoiceError`（后端拒绝）。
    调用方应该分别处理 —— 前者是「应用没开」，后者是「配置写错了」，
    对用户来说是完全不同的两句话。
    """
    text = _clean(text)
    if not text:
        raise VoiceError("没有可朗读的内容")

    if len(text) > MAX_CHARS:
        raise VoiceError(f"单段语音超过 {MAX_CHARS} 字，请分段后发送。")

    cache_file: Optional[Path] = None
    if getattr(cfg, "cache", True):
        try:
            cache_file = _cache_path(cfg, text)
            if cache_file.is_file() and cache_file.stat().st_size > 0:
                log.info("语音命中缓存：%s", cache_file.name)
                return cache_file.read_bytes()
        except OSError:
            cache_file = None  # 缓存目录不可写不该让功能挂掉

    body: Dict[str, Any] = {
        "input": text,
        "model": (getattr(cfg, "model", "") or "tts-1"),
        "voice": (getattr(cfg, "voice", "") or "alloy"),
        "response_format": getattr(cfg, "format", "wav") or "wav",
    }
    # 语言必须显式传。克隆音色的参考音频是外语时，不传 language 模型会
    # 跟着参考的语言走 —— 实测日语参考直接合成出日语，中文全废。
    language = (getattr(cfg, "language", "") or "").strip()
    if language:
        body["language"] = language
    # 步数默认 16 会偶发吞字，配置里建议填 32。
    num_step = int(getattr(cfg, "num_step", 0) or 0)
    if num_step > 0:
        body["num_step"] = num_step
    headers = {"Content-Type": "application/json"}
    api_key = (getattr(cfg, "api_key", "") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = (getattr(cfg, "base_url", "") or "http://127.0.0.1:3900").rstrip("/")
    timeout = aiohttp.ClientTimeout(total=float(getattr(cfg, "timeout", 120.0)))

    # 缓存没命中才走到这儿，说明真要跑一次推理了 —— 先让 ComfyUI 腾地方。
    # 放在这里而不是函数开头，是因为缓存命中时根本不需要动显存。
    if PAUSE_FILE.exists():
        raise VoiceError("显存已让位，语音合成暂停；运行 gpu-lease.ps1 warm 后恢复。")
    if getattr(cfg, "release_comfyui", True):
        await _release_comfyui_vram(http)
    if PAUSE_FILE.exists():
        raise VoiceError("显存已让位，语音合成暂停；运行 gpu-lease.ps1 warm 后恢复。")

    try:
        async with http.post(
            f"{url}/v1/audio/speech", json=body, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:300]
                raise VoiceError(f"HTTP {resp.status}：{detail}")
            data = await resp.read()
    except aiohttp.ClientConnectorError as exc:
        raise VoiceUnavailable(
            f"连不上 VoiceStudio（{url}）：{exc}。"
            "桌面应用开着吗？首次启动要装运行时，会等一会儿。"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise VoiceUnavailable(f"合成超时（{timeout.total}s）") from exc

    if not data:
        raise VoiceError("后端返回了 0 字节音频")

    if cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)
        except OSError as exc:
            log.warning("语音缓存写入失败（不影响发送）：%s", exc)

    return data


def pad_wav_tail(data: bytes, pad_ms: int) -> bytes:
    """在 WAV 末尾补一段静音，防止 QQ 端「吞尾」。

    为什么需要：本机引擎的输出**结尾直接停在字上**，几乎没有余音
    （实测最后 150ms 从 26505 掉到 31，中间没有静音段）。QQ 语音要走
    SILK 编码，它按 20ms 一帧切；解码器收尾时会吃掉最后半帧到一帧。
    音频末尾没有静音可吃，吃掉的就直接是最后一个字 —— 听感上就是
    「话没说完」。补 300~400ms 静音，让它吃静音，别吃字。

    **只处理合法的 16bit PCM WAV**。mp3/opus 原样返回 —— 那些格式有自己的
    容器尾，硬补字节会把文件搞坏。不是 wav 就跳过，绝不抛异常打断发送。
    """
    if pad_ms <= 0 or not data:
        return data
    try:
        with wave.open(io.BytesIO(data), "rb") as src:
            params = src.getparams()
            frames = src.readframes(src.getnframes())
    except (wave.Error, EOFError, ValueError):
        return data  # 不是 wav，或者不是 PCM —— 交给原样发送

    if params.comptype != "NONE":
        return data  # 压缩的 wav，补静音没意义

    frames_per_ms = params.framerate / 1000.0
    n = int(frames_per_ms * pad_ms)
    if n <= 0:
        return data
    silence = b"\x00" * (n * params.sampwidth * params.nchannels)

    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(params.nchannels)
        dst.setsampwidth(params.sampwidth)
        dst.setframerate(params.framerate)
        dst.writeframes(frames + silence)
    return out.getvalue()


def build_voice_segment(path: Path, mode: str = "uri") -> Dict[str, Any]:
    """构造 OneBot 的 `record` 段。

    `mode` 三选一（协议端 各版本吃的写法不一样，所以三种都留着）：

    * `uri`    —— `file:///C:/...`，OneBot 生态最标准的写法（**默认**）
    * `path`   —— `C:\\...`，裸绝对路径。协议端 也吃，而且日志里最好读
    * `base64` —— 内联，不依赖 协议端 读盘权限

    `file` 是 `uri` 的别名，留给配置文件里的旧值。
    """
    if mode == "base64":
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "record", "data": {"file": f"base64://{b64}"}}
    if mode == "path":
        return {"type": "record", "data": {"file": str(path)}}
    # uri：`as_uri()` 对相对路径会抛 ValueError，所以先 resolve。
    return {"type": "record", "data": {"file": path.resolve().as_uri()}}


def send_modes(preferred: str) -> List[str]:
    """按优先级排出要依次尝试的发送方式。

    顺序是**配置的优先，base64 永远兜底**。base64 放最后不是因为它最差，
    而是因为它不依赖 协议端 读盘 —— 前面两条都失败时，它是唯一还成立的假设。
    """
    want = (preferred or "uri").strip().lower()
    if want == "file":
        want = "uri"
    order = [want] + [m for m in ("uri", "path", "base64") if m != want]
    # 去重且保序
    seen: List[str] = []
    for m in order:
        if m in ("uri", "path", "base64") and m not in seen:
            seen.append(m)
    return seen


def build_send_voice_tool() -> Dict[str, Any]:
    """工具声明。

    **措辞两头都写清楚**（这是这个项目反复踩出来的教训）：
    只说「别滥用」→ 模型一次不发；只说「多用」→ 每条都发。
    语音的门槛本来就比表情高（要等合成、对方要戴耳机/开声音），
    所以正面场景要写具体，而不是笼统地鼓励。

    **2026-10-06 实测修正（重要）**：群日志显示 24 轮回复里
    `send_sticker` 调了 8 次，`send_voice` **一次都没有** —— 尽管
    语音提示每 4 轮就在注入。对比两边的措辞就明白了：表情包写的是
    「常见，别犹豫」「没什么特别的理由、但你就是想甩一张，那就发」，
    语音写的是「不多，但别完全不用」+ 一长串「什么时候别用」。

    **还发现一句错的**：原文写着「语音发出去就**没有文字了**」，
    但实测（19:49 日志）语音和文字是**一起发出去**的 —— 模型完全
    可以语音说一句、文字再接一句。这句假约束等于在劝模型「发语音
    要付出放弃文字的代价」，是最大的抑制源。已删掉并改成正面说明。
    """
    return {
        "type": "function",
        "function": {
            "name": "send_voice",
            "description": (
                "用**语音**说一段话（本机合成，免费不限量）。"
                "**这是你表达情绪的方式之一，不是特殊操作。**\n"
                "**⚠️ `text` 必须写中文。** 用**口语、短句、带语气词**的说法 —— "
                "「诶？」「真的假的」「才不要呢」这种口气，别堆敬语、别像念稿子。"
                "（和你的文字回复是同一种语言，只是用声音说。）\n"
                "**别只写一个词或一个短叹词**（「诶？」「耶！」这种）—— "
                "太短容易听不清，**至少写一整句话**。\n"
                "**语音和文字可以同时发。** 语音说一句带语气的，文字再接一句短的，"
                "两边不用重复 —— **发语音不等于放弃文字回复**，别为了这个不用它。"
                "发完只需要接一句很短的话，**干脆不用接**也行。\n"
                "**频率参考：每五六条回复就该有一条语音。** 攒着一直不发，"
                "比偶尔发一条更假。\n"
                "**什么时候值得用（常见，别犹豫）**："
                "撒娇 / 哄人 / 卖萌 / 委屈 / 得意 / 吐槽 —— "
                "情绪本身就值得用声音说的时候；"
                "对方说想听你说话、或者刚发过语音过来；"
                "一句话用文字很难传达语气（拖长音、叹气、笑）。"
                "**没什么特别的理由、但你就是想用声音说一句，那就发。**\n"
                "什么时候别用（这是边界，不是「尽量别用」）："
                "说正经事、讲步骤、报数据；这条回复超过两三句的时候；这轮已经发过语音了。"
                "**别把长内容塞进来** —— 语音没法复制、没法回看。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "要念出来的内容。**必须写中文**"
                            "（口语、短句、带语气词，别堆敬语也别像念稿子）。别写 markdown。"
                        ),
                    }
                },
                "required": ["text"],
            },
        },
    }

