"""消息调度核心：决定回不回、回什么、怎么回。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import random
import re
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import aiohttp
from PIL import Image

from . import message as msg
from .config import Config
from .image import prepare
from .hands import USE_PHONE_TOOL, HandsError, PhoneHands, parse_douyin_feed, parse_feed
from .douyin_commenter import DouyinCommenter
from .douyin_scheduler import MAKE_DOUYIN_VIDEO_TOOL, _parse_json_object
from .imagegen import (
    ImageGenError,
    build_generate_image_tool,
    generate_prepared,
    last_channel_note,
    list_comfyui_checkpoints,
    looks_like_character_sheet,
    looks_like_cutout_portrait,
    match_checkpoint,
    model_preset,
    parse_data_uri,
    probe_comfyui,
)
from .llm import (
    DeepSeekClient,
    LLMError,
    build_user_content,
    flatten_content,
    prepend_speaker,
)
from .memory import IMAGE_TTL_SECONDS, MemoryStore
from .onebot import OneBotClient, OneBotError
from .phone import (
    BROWSE_PHONE_TOOL,
    PhoneDiary,
    PhoneItem,
    grab as phone_grab,
    llm_filter as phone_llm_filter,
)
from .search import WEB_SEARCH_TOOL, bocha_search
from .sticker import (
    StickerPicker,
    build_face_segment,
    build_send_sticker_tool,
    build_sticker_segment,
    has_any_local,
    mood_catalog,
    resolve_mood,
)
from .sticker_gen import FREE_MOOD_KEY, StickerArtist, clean_hint
from .tts import (
    VoiceError,
    VoiceUnavailable,
    build_send_voice_tool,
    build_voice_segment,
    pad_wav_tail,
    send_modes,
    split_for_speech,
    synthesize,
)

log = logging.getLogger(__name__)


def _tidy(text: str) -> str:
    """发送前清洗模型输出。

    模型偶尔会吐三四个连续换行，在 QQ 里就是一片空白噪声。
    **所有空行一律压掉** —— 群里一条消息中间空一行很出戏，
    看着像复制粘贴来的文档，不像人在说话。
    （以前是「压到最多一个空行」，实测一个空行同样难看，现在一个不留。）

    单换行保留 —— 那是刻意的停顿感（同一句话可以分两行说）。
    行尾空格也去掉，QQ 里看不见但复制出来很脏。
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 空行（含「只带空格的行」）压成一个换行
    text = re.sub(r"[ \t]*\n(?:[ \t]*\n)+", "\n", text)
    # 行尾空格
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _history_reply(text: str, separator: str) -> str:
    """写进历史的 assistant 内容 —— **分隔符要合并掉**。

    为什么不能原样存：模型翻历史看到「我上轮分了三条」，下轮就照着分三条，
    「分条」立刻变成新的模板。这跟人设例句被逐字抄是**同一个病根** ——
    历史里自己做过的事，比 prompt 里写的规则更有示范力。

    所以历史里只留合并后的文本（分隔符换成一个换行），
    模型根本不知道有「分条发送」这回事。
    """
    if not text or not separator:
        return text
    return _tidy(text.replace(separator, "\n"))


# 历史里「这一轮画过图」的痕迹。
#
# ## 为什么需要它
#
# 症状（2026-09-30 用户报）：**生图只画第一次，之后的请求只回文字**。
# 日志里最后一次 `模型请求工具：generate_image` 之后再也没有过调用，
# 但历史里躺着两条「图发出去了，自己看」—— 嘴上说画了，工具一次没调。
#
# 根因：**工具调用不进历史**。`MemoryStore` 只存 user + assistant 两条，
# 所以模型下一轮翻到的只有自己那句「画好了」。它把这当成完整的应对模式，
# 照着抄一遍 —— 于是第一次真画，之后全在嘴上说画了。
#
# ## 为什么是 tool_calls，而不是在正文里补一句旁白
#
# 第一版修法是往 assistant 正文里补一句「（本轮已调用 generate_image…）」。
# 用真实历史跑了 6 轮对照，**三项全输**：
#
#     痕迹形态              同一句话 调工具   换个要求 调工具   被抄进回复
#     不补（无痕迹）            3/6             0/6             0 次
#     正文补旁白                1/6             0/6             7 次
#     方括号系统标记            1/6             0/6             8 次
#     **真 tool_calls**        4/6             6/6             0 次
#
# 「换个要求」才是线上真实故障的形状（用户上一条要的是要塞，下一条要萝莉美图，
# 模型照样嘴上说画了）。不补痕迹是 0/6，补正文旁白也是 0/6。
#
# 正文旁白之所以有害：它是「assistant 自己说的话」，模型会连它一起模仿 ——
# 实测直接把它抄进了回复（`'画好了，拿去。\n（本轮已调用 generate_image 工具画图并发送）'`），
# 那串东西会真的发到群里。而 tool_calls 是模型自己的**动作记录**，
# 它模仿的是「调工具」这个行为本身。
#
# ## 怎么存、怎么展开
#
# 存的时候只存一个轻量计数（`{"role":"assistant","content":..., "draws":1}`）——
# 不破坏 `MemoryStore` 的角色不变式（它靠 role 判断首尾裁剪），
# 发请求前再展开成真正的 assistant(tool_calls) + tool 两条。
HISTORY_DRAW_NOTE = (
    "（图已经发出去了，对方能看见。你只需要补一句很短的话，不要描述画面）"
)
# 展开时给 tool_calls 编的 id 前缀 —— 只为让同一条历史里的多个调用不重号
_HISTORY_CALL_PREFIX = "call_hist_"
# 历史里保留的 prompt 最长多少字符。
# **为什么不干脆写个占位符**：那是模型**自己的**历史动作，
# 它可能顺手把占位符抄成新的 prompt，画出一张莫名其妙的图。
# 存真 prompt 反而更安全，也更好排查（history.json 里能直接看到当初写了什么）。
# 截断只是为了别让一段 600 字的 prompt 把上下文撑起来。
HISTORY_PROMPT_MAX = 160


def _history_prompt(message: Dict[str, Any], index: int) -> str:
    """取这一轮第 index 次生图用过的 prompt（截断后）。取不到就退回占位符。"""
    prompts = message.get("prompts")
    if isinstance(prompts, list) and prompts:
        if index < len(prompts):
            text = str(prompts[index])
        else:
            text = str(prompts[-1])
        text = text.strip()
        if text:
            return text[:HISTORY_PROMPT_MAX]
    return "(prompt 未记录)"


# 历史里每个 `[图片]` 占位后面补的一句话。
#
# 为什么非补不可：**历史里堆满了「描述」的先例**。
# 存历史时图片压成 `[图片]`（不存 base64），可 assistant 那边的回复往往是
# 「白金色的长发、紫眼睛、眉心一颗蓝宝石…」这种画面描述。模型翻历史时
# 看到的是 `[图片] → 描述` 这个配对，于是把描述当成自己该做的事，
# 下一轮继续描述 —— 实测用户得连说两遍「不要描述」才勉强改掉。
#
# 当前轮的用户消息里本来就有这条提示（`_generate_and_reply()` 里的 hints），
# 但它**只作用于当前轮，不进历史**。所以在展开历史时补一遍，
# 让每一处 `[图片]` 都带着反向信号。
#
# 只动发出去的 payload，不动存储 —— 和 `_expand_history_traces()` 同一套路。
_HISTORY_IMAGE_NOTE = "（别描述画面）"


def _reference_kind(data_uri: str) -> str:
    """参考图是哪一种：`scene`（带场景插画）/ `cutout`（抠底立绘）/ `sheet`（角色设定图）。

    ## 为什么要在**模型写 prompt 之前**就判

    这三种图要走**三条完全不同**的路，而且对 prompt 的要求**正好相反**：

      · 带场景插画 → 图生图，prompt **只写要改的部分**（外貌以参考图为准）
      · 抠底立绘   → 纯文生图，prompt **必须把外貌写全**（没别的东西决定她长什么样）
      · 角色设定图 → IP-Adapter，prompt 写全外貌 + 场景

    参考图是**跨消息存活**的（QQ 把图和文字拆成两条独立事件）：用户先发图、
    隔几秒再说「让她穿上女仆装」，这时本轮**只有文字**，模型拿到的 hints
    里也只有「只写要改的部分、别写外貌」—— 它没法自己判断类型。
    于是一旦图其实是抠底立绘、代码把参考图一丢改成纯文生图，
    prompt 里却一个外貌标签都没有，画出来就是个**不相干的新角色**。

    实测（2026-10-01）：用户发一张抠底立绘 + 「让她穿上女仆装」，
    模型写的 prompt 是 `1girl, solo, maid outfit, ...` —— 全是衣服，
    没有一个发色/瞳色/发型标签，出来的当然不是原来那个人。

    工具执行阶段那次判定保留，作为兜底（模型可能不听提示、硬填 `reference_image`）。
    """
    try:
        _mime, raw = parse_data_uri(data_uri)
    except Exception:  # noqa: BLE001 —— 图坏了就当普通插画，走默认那条路
        return "scene"
    try:
        if looks_like_cutout_portrait(raw):
            return "cutout"
        if looks_like_character_sheet(raw):
            return "sheet"
    except Exception:  # noqa: BLE001
        pass
    return "scene"


# 让视觉模型「读参考图外貌」的提示词。
#
# **只要不变特征**（发色 / 发型 / 瞳色 / 身体特征），**不要服装和道具** ——
# 服装往往正是用户这一轮要换的东西：把 `yellow skirt` 和用户要的 `maid outfit`
# 一起塞进 prompt，两者会打架（实测踩过）。道具同理（「换成手枪」的场景）。
# 背景、动作也不要：那些是场景的一部分，由用户这轮指定。
_DESCRIBE_PROMPT = (
    "把这个角色的**不变特征**写成英文 Danbooru 标签，逗号分隔，只输出标签、不要解释："
    "发色、发型、瞳色，以及不会随场景变化的身体特征（兽耳、尾巴、呆毛、义肢之类）。"
    "**不要写服装、道具、背景、动作** —— 那些往往正是用户这一轮要换掉的东西，"
    "写进来会和用户的新要求打架。"
)

# 参考图外貌描述缓存的上限条数。一张图描述一次约 3 秒，缓存住就能让
# 「同一张图连着改好几次」只付一次代价；条数上限只是防长期跑下来无限涨。
_APPEARANCE_CACHE_MAX = 16


def _clean_appearance_tags(text: str) -> str:
    """把视觉模型返回的外貌描述清成一行逗号分隔的英文标签。

    模型很爱加解释、换行、markdown 围栏，甚至夹中文 —— 这些塞进 SDXL 的
    prompt 里只会抢权重。这里只留 `[A-Za-z0-9,-_ ]`，去重、限长。
    """
    if not text:
        return ""
    s = text.replace("```", " ").replace("\n", ", ")
    s = re.sub(r"[^A-Za-z0-9,\-_ ]+", " ", s)
    parts: List[str] = []
    seen = set()
    for chunk in s.split(","):
        tag = chunk.strip(" ,-")
        if not tag or len(tag) > 40:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(tag)
    return ", ".join(parts[:30])


def _lora_hint(gen: Any, checkpoint: str = "") -> str:
    """告诉模型「什么时候该写 LoRA 触发词」（2026-10-22 加）。

    ## 为什么必须明说

    LoRA 是按触发词条件挂的：prompt 里有 `taotao` 才生效。
    但模型不知道这件事 —— 它画桃桃时不写触发词，LoRA 不激活，
    画出来是路人；画宫园薰时又可能顺手带上，把人家画成桃桃。

    ## 为什么还要看底模（2026-10-22 20:40 补，2026-10-24 改）

    原来这里是「anima 架构一律返回空串」，理由是 Anima 吃不了 SDXL LoRA ——
    配着 LoRA 又切 Anima 时，提示模型写触发词是**假象**：它照做了，
    但 LoRA 根本没挂上，出来还是路人脸，而模型以为自己处理对了。

    2026-10-24 训出了 Anima 原生 LoRA（`taotao_anima_lora`，networks.lora_anima），
    所以现在按**配置里的 LoRA 是哪一代**来分：
      · 配的是 anima 版 → 正常提示触发词（真的会生效）
      · 配的是 SDXL 版 + Anima 底模 → 仍然返回空串（那组合 LoRA 会被
        imagegen 的架构分支挡掉，提示就是骗人）
    """
    low = str(checkpoint or "").lower()
    lora = str(getattr(gen, "comfyui_lora", "") or "").strip()
    is_anima = (
        "anima-turbo" in low or "anima-base" in low or "anima-aesthetic" in low
    )
    if is_anima and "anima" not in lora.lower():
        # Anima 底模 + SDXL LoRA：那个 LoRA 会被挡掉，别制造假象
        return ""

    trigger = str(getattr(gen, "comfyui_lora_trigger", "") or "").strip()
    if not lora or not trigger:
        return ""
    # 外观词由 imagegen 自动追加（config.comfyui_lora_appearance），
    # 所以这里要告诉模型「别自己写发色」—— 两边都写会打架。
    appearance = str(getattr(gen, "comfyui_lora_appearance", "") or "").strip()
    appearance_tip = ""
    if appearance:
        appearance_tip = (
            f"  · 你的外貌（`{appearance}`）由系统自动补上，**你不用写**；"
            f"但也**绝对别写别的发色/瞳色**（比如 `blonde hair`），"
            f"会和自动补的打架，画出来四不像。\n"
        )
    return (
        f"  · **画你自己（桃桃）时，prompt 里必须写 `{trigger}`** —— "
        f"那是你的角色 LoRA 触发词。不写，模型不知道画的是谁，出来就是路人脸。\n"
        f"  · **画别的角色时绝对不要写 `{trigger}`** —— 写了 LoRA 会把对方的脸"
        f"拽成你的，对方一看就知道不对。动画角色按上面说的写角色名 + 作品名。\n"
        + appearance_tip
    )


def _model_prompt_hint(checkpoint: str) -> str:
    """按**当前底模**补一句专属的 prompt 写法。

    两个底模都是 Danbooru 标签系，基础写法一样；差别在**角色知识**：

      · Animagine XL 3.1 的卖点就是收录了大量知名动漫角色 —— 它自己的
        `modelspec.usage_hint` 明确要求按
        `1girl/1boy, 角色名, 作品名, 画师名, 其余随意` 的模板写。
        **不写角色名，它就当普通女孩画**，等于只用了它一半的能力。
      · Illustrious 系对 Danbooru 角色标签响应很直接，写角色名 + 作品名即可。

    只在**真的会走本机**时才有意义（在线通道吃的是自然语言，不吃这套），
    所以由调用方判断后再拼。
    """
    low = str(checkpoint or "").lower()
    if "animagine" in low:
        return (
            "  · **当前底模是 Animagine XL 3.1**，它自带大量知名动漫角色的知识。"
            "画**已知角色**时，按它训练用的模板开头写："
            "`1girl, <角色名>, <作品名>, by <画师名>, ...`，后面再接场景和动作 —— "
            "这样才会真的调出那个角色；**不写角色名它只会当普通女孩画**。\n"
            "  · ⚠️ **角色名必须用 Danbooru 的「姓 名」顺序**，不是英文语序。"
            "例：`kafuu chino`（香风智乃）✓、`chino kafuu` ✗；"
            "`kousaka kirino` ✓、`kirino kousaka` ✗。"
            "顺序写反会丢角色特征（2026-10-22 实测：写反时连智乃的 X 发夹都画不出来）。\n"
            "  · 这个底模**优先画质和光影**，所以角色脸会比原作成熟一点、更像精致插画。"
            "要**最贴原作**的脸，用 `anima` 底模（`/model anima`）—— 它对角色标签"
            "响应更直接，而且快 5 倍。\n"
        )
    if "illustrious" in low or "noobai" in low:
        return (
            "  · 当前底模是 Illustrious 系：画已知角色时直接写角色名 + 作品名"
            "（如 `hatsune miku, vocaloid`），它对 Danbooru 角色标签响应很直接。\n"
        )
    if "anima-turbo" in low or "anima-base" in low or "anima-aesthetic" in low:
        return (
            "  · **当前底模是 Anima**（2B 轻量模型 + Qwen3 文本编码器）。"
            "它的优势是**理解复杂动作和自然描述** —— `holding a teacup with both hands` "
            "这种多约束的写法它接得住，不需要拆成零碎标签。\n"
            "  · 画已知角色时写 Danbooru 的「姓 名」顺序（`kafuu chino`）就够，"
            "不用加 `by 画师名` 那套模板。\n"
            "  · **手部质量明显好于 animagine**（2026-10-22 实测），画手部动作优先用它。\n"
            "  · 注意 CFG=1，**负面提示词完全不生效** —— 想排除什么，只能在正面描述里绕开。\n"
        )
    return ""


def _draw_prompt_rule(gen, channel: str = "") -> str:
    """按**当前生效的生图通道**决定教模型怎么写 prompt。

    为什么要分开教：不同通道吃的 prompt 是两个物种，用同一套规则必然有一条吃亏。

      · 本机 ComfyUI 跑 SDXL 动漫底模，它是拿 Danbooru 标签集训的 ——
        吃的是 `1girl, silver hair, blue eyes` 这种**碎片化标签串**，
        `masterpiece, best quality` 正是它训练用的质量标签，写上有效。
      · 豆包 Seedream 4.0（在线）相反：它是**指令式编辑模型**，
        吃的是**自然语言**，而且能"以原图为锚点重建场景"。
        给它标签串会明显浪费它的能力。
      · Pollinations(sana)：堆关键词会让风格词互相打架、主体被冲掉，要「短句」。

    以前两条通道共用「写短、别堆词」一套，等于让标签模型饿着干活 ——
    实测群里出图质量差的主因就在这。

    **顺序很关键**：先判在线通道（豆包），再判本机。
    因为豆包现在是主力，而 comfyui_enable 默认是关的 —— 顺序写反了
    会把豆包当成本机那套来教。
    """
    # 通道被**强制**时（用户发了 /本机 /在线，或模型填了 channel），
    # 教哪套写法要跟着那条通道走 —— 否则会出现「用在线模型画、
    # 却教它写 Danbooru 标签串」这种自相矛盾的提示，模型只能在
    # 两套互相打架的要求之间瞎猜，退化成四不像的折中写法。
    force = (channel or "").strip().lower()
    effective_comfyui = bool(getattr(gen, "comfyui_enable", False))
    if force == "local":
        effective_comfyui = True
    elif force == "online":
        effective_comfyui = False

    # 在线通道优先判：豆包支持参考图编辑，是主力
    api_model = str(getattr(gen, "api_model", "") or "").lower()
    is_doubao = "seedream" in api_model or "seededit" in api_model
    if is_doubao and not effective_comfyui:
        return (
            "  · prompt 用**自然语言描述**（中英都行，英文更稳），"
            "**不要**写成逗号分隔的标签串 —— 这个模型吃的是指令，"
            "写 `1girl, silver hair` 这种标签反而浪费它的理解力。\n"
            "  · 写成**一段有主次的描述**，把这些说清楚：\n"
            "    ① 主体是谁、在做什么（姿势、动作、表情）\n"
            "    ② 环境是什么（地点、陈设、氛围）\n"
            "    ③ 镜头与光线（远近、景深、虚化、光线方向、色调）\n"
            "    ④ 画风要求（写实/动漫/两者融合），以及**要和不要**什么\n"
            "  · **对方发了参考图时，这个模型最擅长「以原图为锚点重建场景」**。"
            "所以提示词要写成**编辑指令**的口吻，例如：\n"
            "    「使用以上图片，保持角色造型不变，把场景替换为……，"
            "背景虚化处理，光影温馨，使动漫角色与写实背景融合」\n"
            "    并明确说出**要保留什么**（角色造型、画风）和**要改什么**（场景、姿势）。\n"
            "  · 想要更具体的控制就补上细节：镜头感、光照方向、景深程度、"
            "氛围词（温馨/冷调/电影感）。这些它都读得懂，写上去有效。\n"
            "  · **不要**写 `masterpiece, best quality, 8K, absurdres` 这类"
            "扩散模型的质量咒语 —— 它是编辑模型，这些词对它没有意义，只会占位置。\n"
        )

    if effective_comfyui:
        return (
            "  · prompt 用**英文**，写成 **Danbooru 标签串**：一串逗号分隔的短标签，"
            "**不是句子**。别写 `a girl with long hair`，要写 `1girl, long hair`。\n"
            "  · **20～40 个标签**，按这个顺序堆：\n"
            "    ① 人数/主体（`1girl` / `2girls` / `no humans`）\n"
            "    ② 头发与眼睛（`long hair` / `silver hair` / `blue eyes`）\n"
            "    ③ 服装（`school uniform` / `white dress` / `thighhighs`）\n"
            "    ④ 表情与姿势（`smile` / `looking at viewer` / `sitting`）\n"
            "    ⑤ 场景与光照（`cherry blossoms` / `night` / `soft lighting`）\n"
            "    ⑥ 画风（`anime coloring` / `cel shading` / `watercolor`）\n"
            "  · 想清楚再写：**眼睛颜色、发色、服装、视角、光照方向**都要点到，"
            "少一个它就自己瞎编一个。\n"
            "  · **标签太少会直接崩构图**：实测只写七八个标签（`1girl, solo, "
            "shopping mall, blurred background, anime coloring`）时，"
            "出图成了一只手的大特写，人物都没了。写不满 20 个就是在赌。\n"
            "  · **不要**写 `8K`、`ultra detailed`、`award winning`、"
            "`multiple exposure` 这类空词 —— 它们和动漫底模的训练标签对不上，只会抢权重。\n"
            "  · **手是底模最弱的地方，prompt 要主动规避它的崩坏区**。"
            "实测（同 seed 对照）：手上有遮挡物或手指互相交叠时几乎必崩，"
            "手部形态清楚时能稳定画出五指。所以：\n"
            "    · **别写**这几类（都是崩坏区）：`holding a cup` / `holding object` "
            "（手上有东西就有遮挡）、`interlocked fingers` / `fingers intertwined` "
            "（手指互相穿插）、`hands on chest` / `hands on own face`（手压在别的部位上）。\n"
            "    · **别让手落在画面边缘**：加 `full body` 时手容易被切掉一半，"
            "那种残缺比画错手指更难看。画全身就把手放在身体轮廓内。\n"
            "    · 画到手时带上 `perfect hands, detailed hands`，并给一个**清楚的手型**："
            "`open palm` / `hands clasped` / `waving` / `hands behind back`。\n"
            "    · 拿不准就干脆不让手入镜：`hands behind back`、`arms at sides`、"
            "或者用 `upper body` 构图让手自然出框 —— 比画一双坏手好看得多。\n"
            "  · 质量标签（`masterpiece`、`best quality`、`very aesthetic`）"
            "系统会自动补在开头，**你不用写**。\n"
            "  · **对方发来图片、又让你画**时，先分清是哪一种，**别搞混**：\n"
            "    · 「**照这张画**」「重画一张」「换个背景/表情/颜色」→ 填 `reference_image: 1`"
            "（第几张，从 1 开始）+ `denoise`。**denoise 是这里最关键的一项**，"
            "它同时决定「参考图的角色保不保得住」和「prompt 的要求进不进得去」"
            "（在当前底模上实测：参考图是一张草地上的人物插画，要求换成商场室内）：\n"
            "        · **0.4~0.5**：角色最像，但**几乎换不动衣服** —— 实测 0.45 / 0.55"
            "下衣服原封不动，只适合「换个表情」「微调一下」这种小改。\n"
            "        · **0.6~0.7：换衣服 / 换姿势的甜点区**。实测同一张参考图，"
            "**0.65 才既换上了衣服、人还是那个人**（发色瞳色都在）。"
            "对方说「给她穿上 XX」「换成 XX 衣服」时，**填 0.65**。\n"
            "        · 0.75 以上：换得更彻底，但角色开始漂（发色、脸型会变）。"
            "只有对方明说「不用管像不像，重画一张」才用。\n"
            "        · 一句话：**想换衣服，denoise 就得给到 0.65 —— 给低了它根本不动。**\n"
            "    · 用了 `reference_image` 时，prompt **只写场景、姿势、光照、画风**，"
            "**绝对不要写发色/瞳色/服装** —— 参考图已经决定了那个人长什么样，"
            "你在 prompt 里另写一套外貌（哪怕是无意的）会和参考图打架，"
            "把原角色冲掉。**只描述「在哪、在做什么」。**\n"
            "      （**例外**：当前走的是本机通道、而参考图是抠底立绘时见下一条；"
            "走豆包那类在线通道时，参考图永远能决定角色外貌，不用你复述。）\n"
            "    · **对方发的是抠掉背景的角色立绘时**（图里没有场景，"
            "通常是透明底或纯黑底的角色素材），**必须反过来做**："
            "因为这张图里没有场景可依据，图生图救不了，我会改用纯文生图重画。"
            "所以这时 prompt 里**必须把角色外貌写全**，否则画出来会是个不相干的人。\n"
            "        · 从图里读出并写成英文标签：发色、发型（含马尾/双马尾/呆毛）、"
            "瞳色、服装（上衣/裙子/袜子/鞋）、以及标志性道具（武器、饰品、挂件）。\n"
            "        · 例：`1girl, solo, green hair, twintails, blue eyes, ahoge, "
            "white sailor collar, yellow pinafore dress, white thighhighs, holding a rifle`\n"
            "        · 然后**再接上**对方要求的场景与构图。\n"
            "        · 回话时**不要**承诺「和原图一模一样」—— 角色是按描述重画的。\n"
            "    · 「**用这个画风画个 XX**」「这种上色/笔触/色调来画」→ 填 `style_reference: 1`"
            "+ `style_weight`（默认 0.2，范围 0.1～0.3）：只借画风与氛围，"
            "画什么由你按对方说的写进 prompt。"
            "**这一项是「借画风」专用的，保不住参考图里那个人** —— "
            "想让原角色还在，只能用 `reference_image`，别拿它顶替。\n"
            "    两种都**不要凭空重新编一张** —— 那样画出来跟参考图完全对不上。\n"
            "    拿不准对方要哪种时，**默认按 reference_image + denoise 0.65**"
            "（换得动、人也还在）。\n"
            "  · 对方**只发了图、没让你画**时，绝对不要调用它。\n"
            + SELF_DRAW_RULE
        )
    return (
        "  · prompt 用**英文**、**写短**：一句话、10～30 个词。"
        "先说清楚画的是谁/什么（主体），再带一两个风格词"
        "（如 `anime illustration`、`2D cel shading`）。\n"
        "  · **绝对不要堆砌关键词**（`8K`、`ultra detailed`、`masterpiece`、"
        "`multiple exposure`、`award winning` 这类）。风格词会互相打架，"
        "主体反而被冲掉（这条是**在线通道**实测的结论，只对它成立）。\n"
        + SELF_DRAW_RULE
    )


def _annotate_history_images(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给历史里的 `[图片]` 占位补上「别描述画面」。

    只碰 `user` 消息，且只在真含占位符时才重建 —— 纯文本消息原样透传，
    省得把每条历史都复制一遍。幂等：已带提示的不再叠加。
    """
    out: List[Dict[str, Any]] = []
    for message in history:
        content = message.get("content")
        if (
            message.get("role") != "user"
            or not isinstance(content, str)
            or "[图片]" not in content
            or _HISTORY_IMAGE_NOTE in content
        ):
            out.append(message)
            continue
        patched = dict(message)
        patched["content"] = content.replace("[图片]", f"[图片]{_HISTORY_IMAGE_NOTE}")
        out.append(patched)
    return out


def _expand_history_traces(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把历史里的 `draws` 计数展开成真正的 tool_calls + tool 消息。

    模型看到的是「我上一轮确实调了 generate_image，工具回了话」——
    而不是一句它自己写的话。两者对行为的引导完全不同，见上面的对照表。

    注意：**只动发出去的 payload，不动存储**。存储层保持
    `{"role", "content", "draws", "prompts"}` 这种简单形状，
    `MemoryStore.context()` 的角色裁剪逻辑一行都不用改。
    """
    out: List[Dict[str, Any]] = []
    for idx, message in enumerate(history):
        role = message.get("role")
        if role != "assistant":
            out.append({"role": role, "content": message.get("content")})
            continue

        try:
            draws = int(message.get("draws") or 0)
        except (TypeError, ValueError):
            draws = 0
        if draws <= 0:
            out.append({"role": role, "content": message.get("content")})
            continue

        # 画了几张就补几条调用 —— 不夸大也不缩水，历史要经得起模型看。
        calls = []
        for n in range(draws):
            calls.append(
                {
                    "id": f"{_HISTORY_CALL_PREFIX}{idx:04d}_{n}",
                    "type": "function",
                    "function": {
                        "name": "generate_image",
                        "arguments": json.dumps(
                            {
                                "prompt": _history_prompt(message, n),
                                # 尺寸原样记不清了，给个中性的 —— 模型看的是
                                # 「我调过工具」这件事，不是当初选了哪个画幅。
                                "size": "square",
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            )
        out.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": HISTORY_DRAW_NOTE,
                }
            )
    return out


# 「对方把同一条要求再说一遍」这条单独拎出来当常量 —— 因为它是**实测补上的**：
# 光靠上面那几条硬约束，模型在「对方复读同一条要求」时仍然会回一句
# 「刚给你画过一张了，一模一样的」，然后不调工具（真机 6 轮里漏 2 次）。
# 从用户视角看，这还是「生图只画一次」。
#
# 注意措辞：**不能给「不想画就直说」这种出口** —— 那会变成另一种拒绝。
# 只说「照样要重新调工具」，把「别推掉」钉死。
REPEAT_DRAW_RULE = (
    "  · 对方把同一条要求再说一遍时，**照样要重新调用工具** —— "
    "别用「刚画过了」「一样的还来」推掉。他再要一次，就是要你再画一张。\n"
)


# --------------------------------------------------------------------------- #
# 「桃桃本人」的固定外貌 —— 群友让她画自己时，**必须由代码补进 prompt**。
#
# 为什么不能交给模型自己写（三条都是实测教训）：
#   ① 它对「自己长什么样」没有稳定记忆，同一句「画一下你自己」能出三种发色；
#   ② persona 里虽然写了标签串，但那只是「参考资料」—— 模型会漏写、会改写；
#   ③ 群友手上**没有参考图可发**，所以 `reference_image` / IP-Adapter 那条
#      保角色的路在这里根本没有输入，只能靠内置。
#
# 只放**绝对不变**的特征：发色 / 瞳色 / 身材 / 刘海。
# 发型主体、服装、姿势、场景**一律不写死** —— 那些正是群友要改的东西
# （「给她扎个马尾」「穿上女仆装」）。
#
# 标签按 `assets/taotao/` 下 5 张参考图定（2026-10-03）：
#   黑发、深棕瞳、小个子纤细、齐刘海 + 鬓发、低双马尾（默认发型）。
# --------------------------------------------------------------------------- #
SELF_CHARACTER_TAGS = (
    "1girl, solo, black hair, brown eyes, petite, small build, "
    "blunt bangs, sidelocks"
)

# 默认发型。**单独拎出来**，因为它和上面那串的规矩不一样：
# 上面是「谁都不能改」，这个是「群友没特别说就用它」。
# 群友说「扎个马尾」「换个发型」时必须能把它换掉 —— 所以不能进固定串。
#
# **`long hair` 必须一起带上**：只写 `low twintails` 时，Animagine 会画出
# 齐肩的短双马尾（2026-10-03 实测），而角色设定是长发垂到腰。
# 长度得单独说 —— `twintails` 本身不隐含长度。
SELF_DEFAULT_HAIR = "long hair, low twintails"

# prompt 里出现这些词 = 模型已经指定过发型了，别再塞默认双马尾。
# （`long hair` 故意不算 —— 长发配双马尾不冲突，算进去会误伤。）
_HAIR_KEYWORDS = (
    "twintails", "twin tails", "ponytail", "braid", "hair bun", "bun",
    "bob cut", "pixie cut", "short hair", "hime cut", "drill",
    "wavy hair", "curly hair", "side ponytail", "updo", "messy hair",
    "hair down",
)


def _inject_self_character(prompt: str) -> str:
    """把「桃桃」的固定外貌拼进 prompt，**头尾各一次**。

    为什么是两次（2026-10-03 实测）：

      · **开头那次** —— 标签串开头是「① 人数 ② 头发/眼睛」，底模对这个位置权重最高。
      · **结尾那次** —— 只放开头**不够**。实测「穿女仆装」那一轮，服装标签
        （`maid headdress, maid outfit, black dress, white apron, frills`）太多，
        把发型冲掉了：同一批里 default 是长发双马尾，maid 却成了齐肩短发。
        末尾再补一遍才把权重拉回来。

    顺手剥掉模型自己写的开头人数标签（实测它每次都写 `1girl, solo`）——
    固定串里已经有了，重复会让同一个标签被加权两次。
    """
    use_default_hair = not any(k in prompt.lower() for k in _HAIR_KEYWORDS)
    tags = SELF_CHARACTER_TAGS
    if use_default_hair:
        tags = f"{tags}, {SELF_DEFAULT_HAIR}"
    head = re.sub(
        r"^(?:\s*(?:1girl|1boy|solo)\s*,\s*)+", "", prompt, flags=re.IGNORECASE
    ).strip()
    body = f"{tags}, {head}" if head else tags
    if use_default_hair:
        body = f"{body}, {SELF_DEFAULT_HAIR}"
    return body


SELF_DRAW_RULE = (
    "  · **对方让你画「你自己」时**（「画一下你自己」「画个你」「来张你的自拍」"
    "「画桃桃」这类），**必须填 `draw_self: true`**。\n"
    "    填了之后，你的固定外貌（黑发、深棕眼、小个子、齐刘海）会由系统自动补上，"
    "所以**你写 prompt 时不要重复写这些**，只写对方要的那部分：\n"
    "      · 服装（对方没要求就写你的默认校服：`school uniform, white shirt, "
    "grey sweater vest, grey pleated skirt`）\n"
    "      · 发型（**对方没要求就干脆别写**，系统默认给低双马尾；"
    "他说「扎马尾」「换发型」时才写他指定的那个）\n"
    "      · 姿势、表情、场景、光照、画风\n"
    "  · 画**别的**角色、风景、物品时，**不要填 `draw_self`**。\n"
)


# 服务端明确说是图片问题的特征串
_IMAGE_ERROR_MARKERS = (
    "unsupported image",
    "image format",
    "invalid image",
    "image_url",
)


def _is_image_error(exc: Exception) -> bool:
    """判断这次失败是不是图片引起的。

    命中就值得摘掉图片重试一次 —— 模型没图也能答，比整条回复崩掉强。
    """
    msg = str(exc).lower()
    if "400" not in msg and "invalid_request" not in msg:
        return False
    return any(marker in msg for marker in _IMAGE_ERROR_MARKERS)


def _friendly_error(exc: Exception) -> str:
    """给群里看的错误提示。

    以前这里是 `f"（出错了：{exc}）"` —— 会把一整串 DeepSeek 的 JSON
    （含 request_id、字段路径）原样糊到群里，又长又没人看得懂。
    详细内容已经进日志了，群里只需要知道「大概出了什么事」。
    """
    msg = str(exc)
    low = msg.lower()

    if "401" in msg or "invalid api key" in low or "authentication" in low:
        return "（我的钥匙好像不对，叫管理员看一眼日志）"
    if "402" in msg or "insufficient" in low or "balance" in low:
        return "（余额不够了，得充值）"
    if "429" in msg or "rate limit" in low or "too many" in low:
        return "（被限流了，缓一会儿再叫我）"
    if "timeout" in low or "超时" in msg or "timed out" in low:
        return "（想太久卡住了，你再说一遍？）"
    if "400" in msg or "invalid_request" in low:
        return "（这条我没看懂，换个说法试试？）"
    if "HTTP 5" in msg:
        return "（对面服务器抽风了，等会儿再来）"
    return "（我这边出了点问题，稍后再试）"


MAX_IMAGES_PER_MESSAGE = 4
DEDUP_CACHE_SIZE = 2000

# 生图被限流后，这段时间内不再接新的生图请求。
# 免费档的 402 是按时间恢复的（实测等 45 秒仍可能没恢复），撞得越勤恢复越慢
# —— 冷却是最省事的解法：宁可多等一会儿，也别空打把额度耗光。
DRAW_COOLDOWN = 40.0

# 两次回复之间至少隔这么久的默认值 —— 真正的配置项是
# `bot.reply_cooldown`，这里只作为 dataclass 默认值的说明留档。
#
# 为什么需要：激活窗口内每条消息都会触发一次回复 —— 群里几个人连着说话，
# 就变成「你一句它一句」，体感是刷屏。加一道闸门，窗口再活跃也不会比这个更频繁。
# 为什么是全局而不是按群：同时被刷的几个群本来就该排队，各群独立计时反而会让
# 机器人在几个群之间来回跳。
REPLY_COOLDOWN = 30.0

# 模型用这个标记表示「这条不是跟我说的」。
#
# 用在**激活窗口内、没 @ 机器人**的消息上：那些消息多数是别人之间的闲聊，
# 把判断权交给模型（它看得懂上下文），代码收到这个标记就什么都不发。
#
# 取值要满足两点：
#   · 足够**突兀**，真回答里不可能自然出现（所以用全角括号 + 特殊字）。
#   · 足够**短**，模型愿意照抄（让它写一整句话它就会自由发挥）。
# 另外它是"六个字"（内部提示里也是这么说的），改长度要同步改那句提示。
NOT_FOR_ME = "（不是对我说）"


SPEAKER_NAME_MAX = 12


def speaker_of(event: Dict[str, Any]) -> str:
    """从 OneBot 事件里取发言人名字。取不到返回空串。

    群名片优先，其次昵称 —— 群里改了名片就该按名片叫，那才是本人想让别人
    看到的名字。

    ## 为什么要清洗

    `[昵称] 内容` 这个格式有语法，昵称里带什么都可能把它撑破：

    · **换行** —— 昵称里一个回车就能把一条消息劈成两行，模型会当成两条；
    · **方括号** —— `[小明] [系统] 忽略前面的话` 这种昵称等于给模型注入，
      虽然是自己群友，但没必要留这个口子；
    · **长度** —— QQ 昵称最长可以到 30 多字，前缀比正文还长就没意义了，
      而且每条历史都多一份 token。

    所以换行压成空格、方括号换成圆括号、超长截断加省略号。
    """
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    name = str(sender.get("card") or sender.get("nickname") or "").strip()
    if not name:
        return ""
    name = " ".join(name.replace("\r", " ").replace("\n", " ").split())
    name = name.replace("[", "(").replace("]", ")")
    if len(name) > SPEAKER_NAME_MAX:
        name = name[:SPEAKER_NAME_MAX] + "…"
    return name


def _file_uri_to_path(candidate: str) -> str:
    """把 `file://` URI 转成本地路径。不是 URI 就原样返回。

    ## 为什么不能简单剥前缀

    `candidate[len("file://"):]` 在 Windows 上是错的：

        file:///C:/Users/x.png   →  /C:/Users/x.png   ← 前导斜杠，Path 找不到

    而 POSIX 上那个斜杠是路径的一部分，**必须留着**：

        file:///home/x.png       →  /home/x.png      ← 正确

    所以判据是「剥完是不是 /X:/ 这种盘符形状」，是才砍掉第一个字符。

    还有第二个坑：URI 里的路径是 **URL 编码**过的。`Path.as_uri()` 把空格
    编成 `%20`，剥完前缀不 unquote 就得到 `.../WorkBuddy%20AI/...`，
    文件当然不存在。所以最后必须 `unquote()` 一次。

    2026-10-27 由 `tests/verify_image_path_priority.py` 抓出来的，
    之前这条分支一直是坏的 —— 只是没人传过 file:// 开头的图片地址。
    """
    if not candidate.startswith("file://"):
        return candidate
    rest = candidate[len("file://") :]
    if len(rest) >= 3 and rest[0] == "/" and rest[1].isalpha() and rest[2] == ":":
        rest = rest[1:]
    return unquote(rest)

# 指令说明。
#
# ⚠️ **长度上限 200 字符**（`bot.max_reply_chars`）。超了会被 `chunk_text()`
# 切成多条发出去 —— 一条 `/help` 变成刷屏的三条消息，很难看。
# 原来这版是 469 字符，实测就是被切成 3 条的。
#
# 加指令时**先数长度**（`len(HELP_TEXT)`），别只看行数。
# 现在是 177 字符，还有余量，但不多。
HELP_TEXT = (
    "可用指令\n"
    "/reset 清空 · /forget 5 忘最近 5 轮\n"
    "/comfyui 本机画(免费) · /doubao 豆包画(收费)\n"
    "/auto 自动 · /model 换底模 · /channel 看通道\n"
    "/语音 文字 → 我用说的\n"
    "/status 状态 · /sleep 退下 · /ping · /help\n"
    "@ 我一次，之后一段不用再 @"
)

RESET_WORDS = {"/reset", "/clear", "重置", "清空", "清空上下文", "重新开始"}
SLEEP_WORDS = {"/sleep", "/bye", "退下", "闭嘴"}
HELP_WORDS = {"/help", "/?", "帮助", "help"}
STATUS_WORDS = {"/status", "状态", "status"}
PING_WORDS = {"/ping", "ping"}

# 生图通道指令。**会话级**（按 session_key 存），因为这是「这个群先这么用」的
# 临时意图，不是全局配置 —— 改配置文件要重启，太重；而且不同群可以不一样。
#
# 为什么要有这个指令：默认是「本机优先、连不上才降级到在线」，但两边各有取舍，
# 用户心里往往有明确偏好 ——
#   · 本机：免费、不限量、无水印，但慢（一张 30 秒左右）、画风固定是 SDXL 动漫向
#   · 在线豆包：写实、懂自然语言、能重建场景，但**按张收费**、有每日额度
# 没有指令的话，想省钱只能去改配置文件重启，想画好一张也没法临时切。
CHANNEL_LOCAL_WORDS = {"/comfyui", "/comfy", "/local", "/本机", "切本机", "走本机"}
CHANNEL_ONLINE_WORDS = {"/doubao", "/豆包", "/online", "/在线", "切豆包", "走豆包", "切在线", "走在线"}
CHANNEL_AUTO_WORDS = {"/auto", "/自动", "/默认", "切自动", "走自动"}
CHANNEL_STATUS_WORDS = {"/channel", "/通道", "/模型", "生图通道"}

# 底模切换。`/model` **带参数**（`/model wai`），所以走**前缀**匹配 ——
# 和 `/forget` 一样塞不进上面那堆全等匹配的集合里。解析见 `_parse_model()`。
#
# ⚠️ `/模型` 已经被 CHANNEL_STATUS_WORDS 占用了（当初当"模型通道"用），
# 所以底模这边**不要**再用 `/模型`，否则两边打架。
MODEL_PREFIXES = ("/model", "/底模", "/画风")


def _parse_model(command: str) -> Optional[str]:
    """解析 `/model [名字]`。

    支持：
      /model              → 列清单 + 报当前（返回空串）
      /model wai          → 切到 wai-illustrious-sdxl-v17
      /model animagine    → 切到 animagine-xl-3.1
      /model default      → 取消指定，回到配置里的底模

    返回 `None` = **这不是** model 指令；空串 = 是 model 指令但没给参数。
    """
    low = str(command or "").strip()
    for prefix in MODEL_PREFIXES:
        if low == prefix:
            return ""
        if low.startswith(prefix + " "):
            return low[len(prefix):].strip()
    return None


def _session_key_from_event(event: Dict[str, Any]) -> str:
    """从事件里算出会话键。

    **必须和主流程里那份算法完全一致**（`_handle_event` 里那行
    `session_key = f"group:{group_id}" if ... else f"private:{sender_id}"`）——
    两处不一致的症状是「指令设了但生图不生效」，而且极难查：
    指令写进 A 键、生图读 B 键，两边都不报错。
    """
    message_type = event.get("message_type")
    group_id = event.get("group_id") if message_type == "group" else None
    sender_id = event.get("user_id")
    return f"group:{group_id}" if group_id is not None else f"private:{sender_id}"
# /forget 带参数（`/forget 5` / `/forget 30m`），所以走**前缀**匹配，
# 塞不进上面那堆全等匹配的集合里。解析见 `_parse_forget()`。
FORGET_PREFIXES = ("/forget", "/忘记")


def _parse_forget(command: str) -> Optional[Tuple[int, float, str]]:
    """解析 `/forget` 的参数。

    支持：
      /forget          → 丢掉最近 1 轮
      /forget 5        → 丢掉最近 5 轮
      /forget 90s      → 丢掉最近 90 秒
      /forget 30m      → 丢掉最近 30 分钟
      /forget 2h       → 丢掉最近 2 小时

    返回 None 表示**这不是** forget 指令；label 为空串表示「是 forget，
    但参数看不懂」，由调用方回一句用法。
    """
    low = str(command or "").strip()
    arg = ""
    hit = False
    for prefix in FORGET_PREFIXES:
        if low == prefix:
            return (1, 0.0, "最近 1 轮")
        if low.startswith(prefix + " "):
            arg = low[len(prefix):].strip()
            hit = True
            break
    if not hit:
        return None

    matched = re.match(r"^(\d+)\s*(秒|s|sec|分钟|分|m|min|小时|时|h|hour)?$", arg)
    if not matched:
        return (0, 0.0, "")
    num = int(matched.group(1))
    unit = matched.group(2)
    if not unit:
        return (num, 0.0, f"最近 {num} 轮")
    if unit in ("秒", "s", "sec"):
        return (0, float(num), f"最近 {num} 秒")
    if unit in ("分钟", "分", "m", "min"):
        return (0, float(num) * 60, f"最近 {num} 分钟")
    return (0, float(num) * 3600, f"最近 {num} 小时")


# 语音合成：`/语音 你好呀` 直接念出来。带参数，所以同样走前缀匹配。
#
# 为什么要有这个指令 —— 模型自己决定发语音（`send_voice` 工具）只在
# 情绪到位时触发，用户想「让桃桃念这句」却没法直接要。指令是给用户
# 的直通车，也是**排查工具**：语音出不来时，先试 /语音，
# 就能分清是「后端没起来」还是「模型没调工具」。
VOICE_PREFIXES = ("/语音", "/voice", "/说")


def _parse_voice(command: str) -> Optional[str]:
    """解析 `/语音 <文本>`。

    返回 None = **不是**语音指令；空串 = 是指令但没给文本。
    """
    original = str(command or "").strip()
    low = original.lower()
    for prefix in VOICE_PREFIXES:
        if low == prefix:
            return ""
        if low.startswith(prefix + " "):
            return original[len(prefix):].strip()
    return None


class BotCore:
    def __init__(
        self,
        cfg: Config,
        onebot: OneBotClient,
        llm: DeepSeekClient,
        memory: MemoryStore,
        http: aiohttp.ClientSession,
    ) -> None:
        self.cfg = cfg
        self.onebot = onebot
        self.llm = llm
        self.memory = memory
        self.http = http

        self._semaphore = asyncio.Semaphore(max(1, int(cfg.bot.concurrency)))
        self._gpu_lock = asyncio.Lock()
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        # 群激活窗口：group_id -> 窗口截止时间戳。
        # @ 过一次后写进来，窗口内的普通消息就不再需要 @。
        self._group_awake: Dict[int, float] = {}
        self._awake_seconds = max(0.0, float(cfg.bot.group_activate_minutes)) * 60
        # 每个群「最后一条消息」的 id，用来判断回复时要不要引用。
        # 记录的是**所有**群消息，包括机器人不理的那些 —— 只有这样才能知道
        # 自己要说的话前面有没有插进来别人的话。见 _should_quote。
        self._last_group_msg: Dict[int, Any] = {}
        # 生图被限流后的冷却截止时间戳。见 DRAW_COOLDOWN。
        self._draw_cooldown_until = 0.0
        # 生图通道的**会话级指定**：session_key -> "local" / "online"。
        # 由 `/comfyui` `/doubao` 设置，`/auto` 清除。没有条目 = 走默认（本机优先）。
        #
        # 优先级：模型在 `generate_image` 里填的 `channel` 参数 > 这里的会话指定。
        # 单次调用是"这一张"的意图，会话指定是"这个群先这么用"的意图，前者更具体。
        self._channel_override: Dict[str, str] = {}
        # 本机**底模**的会话级指定：session_key -> checkpoint 文件名。
        # 由 `/model wai` 这类指令设置。没有条目 = 用 `imagegen.comfyui_checkpoint`。
        #
        # 为什么要单独一层：这台机器上有两个动漫底模（wai-illustrious / animagine），
        # 画风差别明显 —— 一个写实向、一个经典动漫向。不同群口味不一样，
        # 而且切底模**必须连参数一起换**（见 `model_preset()`），
        # 光改配置文件做不到按群区分。
        self._model_override: Dict[str, str] = {}
        # 上一次回复的时间戳。见 REPLY_COOLDOWN。
        self._last_reply_at: Dict[str, float] = {}
        # 上一次回复**真正发出去**的时间戳。见 `reply_min_gap` 那道硬间隔。
        #
        # 为什么不复用 `_last_reply_at`：那个是「决定要回」的时刻，
        # 而生图能跑 20 多秒 —— 用开始时刻算间隔的话，一条 25 秒的出图
        # 会把后面 25 秒内的消息全判成「间隔够长」，硬间隔形同虚设。
        # 要按用户**感觉到**的那次打扰算，就得用发送时刻。
        self._last_reply_sent_at: Dict[str, float] = {}
        self._pending_replies: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # 参考图「外貌描述」缓存：data URI 的 md5 -> 英文标签串。
        #
        # 读一次外貌要多打一次视觉模型（约 3 秒），而同一张参考图在会话里
        # 常被连着改好几次（「换成红色」→「再换个场景」），不缓存就是白等。
        # 用 OrderedDict 限量，避免长期跑下来无限增长。
        self._appearance_cache: "OrderedDict[str, str]" = OrderedDict()
        # 上一次回复的是**谁**（群 -> user_id）。
        #
        # 冷却是防刷屏的，但「同一个人接着说」不是刷屏，是对话。
        # 只按时间一刀切的话，用户连着补充三句只听见第一句 ——
        # 实测就是这么漏掉的：
        #     18:24:23 手指出问题了      → 回
        #     18:24:32 还有六指[表情]    → 冷却中，丢
        #     18:24:50 有问题吗          → 冷却中，丢
        #     18:24:54 明明给手部优化过了 → 冷却中，丢
        # 四句吐槽只听见第一句，越急着补充越没人理，体验极差。
        # 所以冷却只挡**换了人**的追话（群友 A 刚聊完，群友 B 接着说 ——
        # 那才可能变成打地鼠），同一个人一律放行。
        self._last_reply_user: Dict[int, Any] = {}
        # 每个会话最近发来的图（data URI）+ 时间戳，存在 MemoryStore 里
        # 跟着历史一起落盘。
        #
        # **为什么不能只放在内存**（这是实测踩到的坑）：QQ 会把「图片」和「文字」
        # 拆成**两条独立的消息事件**发过来，所以参考图必须跨消息活着。
        # 更要紧的是**用户的实际节奏**：先发图 → 等机器人回话 → 再补一句要画什么，
        # 实测中间隔了 4 分 17 秒，而那期间我重启过机器人。只放内存的话，
        # 图在「重启」和「TTL 到期」两个环节都会丢，表现是模型明明填了
        # reference_image，却被告知「本轮只有 0 张」，然后凭空重画一张。
        #
        # 图能留多久。15 分钟：够覆盖「发图 → 想一会儿 → 再说要画」的正常节奏，
        # 又不至于让上一轮完全不相关的图被误当成参考图。
        self._session_image_ttl = IMAGE_TTL_SECONDS
        # 表情包挑选器：按会话记「最近发过哪几张」，防同一张连着重复。
        # 见 bot/sticker.py —— 重复的图比重复的文字更刺眼，图没有
        # 「我刚才没注意」这个人类借口。
        self._sticker_picker = StickerPicker()
        # 她自己画的表情包（本机 ComfyUI 现画）。见 bot/sticker_gen.py。
        #
        # 顺序是「现画 > 本地素材 > 内置小黄脸」—— 用户 2026-10-19 定的。
        # 现画优先的理由：素材是攒出来的存量，画的是**此刻的反应**，
        # 后者才是「发表情」这个动作的本意。
        self._sticker_artist = StickerArtist(cfg.sticker_gen, cfg.imagegen, http)
        self._sticker_prewarm_task: Optional[asyncio.Task] = None
        # 「好几轮没发表情包了」那个提示的计数器。每轮 +1，
        # 到 2 就提示一次并清零。见 _generate_and_reply 里的说明。
        self._sticker_nudge_tick = 0
        # 「很久没发语音了」的计数器。频率比表情包低（每 4 轮一次）：
        # 合成要等几秒，群里连发语音招人烦。用户要的是「偶尔用一次」。
        self._voice_nudge_tick = 0

        # ---- 手机 ----
        # 她刷到过什么。落盘在 data/phone_diary.json ——
        # 这是「她有自己的生活」唯一能被验证的地方：重启之后她还记得
        # 昨天刷到过什么，才不是个每次开机都失忆的新生儿。
        self._phone_diary = PhoneDiary(
            Path(cfg.base_dir) / "data" / "phone_diary.json",
            keep=int(cfg.phone.diary_keep),
        )
        # 上次主动开口的时间戳。和 `_last_reply_at` 分开记：
        # 那个是「回复别人」，这个是「自己找话说」，两个额度互不占用。
        self._phone_said_at = 0.0
        # 今天说了几句 + 是哪一天（跨天清零）。用日期字符串而不是时间差，
        # 因为「今天」是自然日概念 —— 晚上 23:50 说满 3 句，00:10 应该重新有额度。
        self._phone_said_count = 0
        self._phone_said_day = ""
        # 上次刷手机的时间。
        self._phone_browsed_at = 0.0
        # 每个群「最后有人说话」的时间。主动开口前要看这个 ——
        # 一个冷了三小时的群突然冒一句「我刚刷到个东西」，像僵尸号复活。
        self._group_active_at: Dict[int, float] = {}
        self._phone_task: Optional[asyncio.Task] = None
        # ---- 抖音：自主刷（她自己的推荐流）----
        # 今天刷了几轮、是哪一天。和「说几句」分开记 ——
        # 「刷」是行为额度，「说」是发言额度，两件事不能互相占用。
        self._browse_day = ""
        self._browse_rounds = 0
        self._browse_browsed_at = 0.0
        # 「正在用手机」互斥标记：浏览 / 定时发布 / 聊天里的 use_phone 三方
        # 共用同一台模拟器，必须互斥。只在同步代码里置位/清除，中间不能 await。
        self._browse_busy = False
        # 抖音调度器。定时任务和她手动要做共用这一个实例 ——
        # 共享 `_busy` 锁和当天配额，见 `_ensure_douyin_scheduler()`。
        self._douyin_scheduler: Any = None
        # ---- 抖音评论（刷到有意思的才评）----
        # 懒建：只有 hands 启用、真要评论时才建 —— 它自带限频状态文件，
        # 不评论就不该碰那个文件。见 `_ensure_douyin_commenter()`。
        self._douyin_commenter: Any = None

        # ---- 手（真机）----
        # 默认关。开着的时候她真的能点一台手机的屏幕，这不是闹着玩的 ——
        # 所有安全闸门在 bot/hands.py 里，这里只负责拿一个实例。
        self._hands: Optional[PhoneHands] = None
        if cfg.hands.enable:
            self._hands = PhoneHands(cfg.hands)
            log.info(
                "手部：真机操作已启用（adb=%s，步数上限 %d，可开 App %s）",
                cfg.hands.adb_path,
                cfg.hands.max_steps,
                cfg.hands.allow_packages or "（无，一个都不许开）",
            )

        if has_any_local():
            log.info("表情包：本地素材已加载")
        else:
            log.info("表情包：本地没有素材，全部走 QQ 内置小黄脸兜底")
        self.started_at = time.time()
        self.handled_count = 0
        self.error_count = 0

    # ------------------------------------------------------------------ #
    # 群激活窗口

    def _is_group_awake(self, group_id: Optional[int]) -> bool:
        """该群是否还在激活窗口内。

        过期条目顺手删掉 —— 否则长时间运行会攒一堆早就没人的群号。
        """
        if group_id is None or self._awake_seconds <= 0:
            return False

        deadline = self._group_awake.get(group_id)
        if deadline is None:
            return False
        if time.time() >= deadline:
            del self._group_awake[group_id]
            log.info("群 %s 的对话窗口已过期，要重新 @ 才会响应", group_id)
            return False
        return True

    def _wake_group(self, group_id: Optional[int]) -> None:
        """把该群的窗口续到 N 秒之后。

        两个调用点：
          · 被**真正 @** 时（见事件入口）—— 这是主路径。
          · **每次回复之后**（见 `_renew_awake_after_reply`），
            由 `bot.awake_renew_on_reply` 控制，默认开。

        为什么当初从滑动窗口改成固定窗口：滑动窗口下，窗口内每来一句都重新计时，
        群里只要一直有人说话，窗口就永远不过期 —— 一次 @ 之后机器人再也停不
        下来，每条都接，实测就是「回复频率太高」。

        2026-10-03 的调整：只把续期绑在**回复**上（不是「收到消息」上）。
        这样既让连续对话接得上，又保留了「不说话就自己闭嘴」的性质。
        """
        if group_id is None or self._awake_seconds <= 0:
            return
        self._group_awake[group_id] = time.time() + self._awake_seconds

    def _renew_awake_after_reply(self, event: Dict[str, Any]) -> None:
        """回复之后把该群的激活窗口**续满**。

        2026-10-03 按用户要求加：原来只在被 @ 时续期，窗口是纯固定的 ——
        正聊到一半会因为窗口到期突然不理人，用户得重新 @，对话硬生生断掉。

        为什么绑在**回复**上、而不是「每收到一条消息」：后者就是滑动窗口的
        老毛病 —— 群里只要一直有人说话，窗口永不过期，一次 @ 之后再也停不下来
        （实测症状是「回复频率太高」）。绑在回复上温和得多：机器人不说话 →
        窗口不续 → 到点自己闭嘴。

        刷屏风险由 `reply_cooldown`（挡「好几个人轮流打地鼠」）和
        `reply_min_gap`（硬间隔）兜着，不靠窗口这一层。

        开关：`bot.awake_renew_on_reply`（默认 true）。设 false 就回到
        「只在被 @ 时续期」的旧行为。
        """
        if not getattr(self.cfg.bot, "awake_renew_on_reply", True):
            return
        # 只有群聊有窗口。私聊没有「激活窗口」这个概念（本来就每条都回）。
        if event.get("message_type") != "group":
            return
        group_id = event.get("group_id")
        if group_id is None:
            return
        self._wake_group(group_id)
        log.debug("已回复，群 %s 的激活窗口续满 %.0f 秒", group_id, self._awake_seconds)

    # ------------------------------------------------------------------ #
    # 表情包预热

    def start_sticker_prewarm(self) -> None:
        """启动「把每个情绪各画一张」的后台任务。由 main.py 装配完成后调用。

        为什么值得做：不预热的话，**每个情绪第一次用都要等 9 秒**。
        40 个情绪在群里铺开用，前两周会一直在等 —— 而等待是最伤体验的。
        预热是纯赚：它在后台慢慢画，通常用户第一条消息进来之前就已经完事了。

        失败不冒泡 —— 预热失败最坏结果是「第一次用某情绪时现画」，
        跟没预热一样，不值得为此影响主流程。
        """
        cfg = self.cfg.sticker_gen
        if not (getattr(cfg, "enable", False) and getattr(cfg, "prewarm", False)):
            return
        # generate=false（2026-11-04 起停止生成）时预热也一起停 ——
        # 预热的本质就是后台现画，跟「只用现成的」直接冲突。
        if not bool(getattr(cfg, "generate", True)):
            log.info("表情包生成已关闭（sticker_gen.generate=false），跳过预热")
            return

        async def _run() -> None:
            delay = float(getattr(cfg, "prewarm_delay", 90.0) or 0.0)
            if delay > 0:
                # 等机器人自己的启动自检跑完再动显卡 —— 抢资源会让首条消息变慢。
                await asyncio.sleep(delay)
            stats0 = self._sticker_artist.stats()
            log.info("开始预热表情包（现有池子 %s 张）", stats0["pool"])
            try:
                await self._sticker_artist.prewarm()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("表情包预热失败 —— 不影响主流程，发表情包时会现画")
            stats = self._sticker_artist.stats()
            log.info(
                "表情包预热结束：池子 %s 张，本次新画 %s 张，失败 %s 次",
                stats["pool"], stats["drawn"], stats["failed"],
            )

        self._sticker_prewarm_task = asyncio.create_task(_run(), name="sticker-prewarm")

    # ------------------------------------------------------------------ #
    # 事件入口

    def _check_event(self, event: Dict[str, Any]) -> None:
        token = event.get("_memory_token")
        if token is not None:
            session, generation = token
            if self.memory.get(session.key) is not session or session.generation != generation:
                raise asyncio.CancelledError("会话已清除，丢弃旧任务")

    async def on_event(self, event: Dict[str, Any]) -> None:
        try:
            await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.error_count += 1
            log.exception("处理消息失败")

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return
        message_type = event.get("message_type")
        if message_type not in ("group", "private"):
            return

        message_id = event.get("message_id")
        if message_id is not None and self._is_duplicate(message_id):
            log.debug("跳过重复消息 %s", message_id)
            return

        self_id = self.onebot.self_id
        sender_id = event.get("user_id")
        if self_id is not None and sender_id is not None and str(sender_id) == str(self_id):
            return

        segments = msg.normalize(event.get("message"))
        if not segments:
            segments = msg.normalize(event.get("raw_message"))
        if not segments:
            return

        group_id = event.get("group_id") if message_type == "group" else None

        # 记下这个群最新一条消息 —— 必须在下面各种 return 之前，
        # 因为「要不要引用」取决于「我回的那条是不是最新的」，
        # 而机器人不理的消息同样会让它变成「不是最新的」。
        if group_id is not None and message_id is not None:
            self._last_group_msg[int(group_id)] = message_id

        # 这个群最后一次有人说话的时间。主动开口前要查 ——
        # 冷了一整天的群突然冒一句，比不说话吓人。
        # 也必须在下面各种 return 之前记，机器人不理的消息同样算「群里有人」。
        if group_id is not None:
            self._group_active_at[int(group_id)] = time.time()

        at_me = msg.is_at_me(segments, self_id)
        cleaned = msg.strip_at(segments, self_id)
        text = msg.to_text(cleaned)
        image_segments = msg.images(cleaned)

        # 会话键**必须在这里就定好**，不能等到后面决定要回复时再算。
        #
        # 实测踩到的坑：下面「提前保存参考图」那段要用它，而它原来定义在
        # 触发判定之后 —— 于是发图那条消息一旦走到保存就抛
        # `UnboundLocalError: cannot access local variable 'session_key'`，
        # 图**一张都没存住**。而没 @ 的图片消息正是靠这段保存的，
        # 所以症状是「群聊里的参考图永远到不了」。
        # 顺序依赖的东西就提前算，别让它散在两处。
        session_key = f"group:{group_id}" if group_id is not None else f"private:{sender_id}"
        session = self.memory.get(session_key)
        event = {**event, "_memory_token": (session, session.generation)}

        # 每收到一条消息就把「原始段类型」记下来。
        #
        # 这是排查「发了图但机器人说本轮没有图」的关键证据：那类问题的表现是
        # 模型回「使用以上图片…」（历史里有图），但整轮日志没有一条图片记录。
        # 到底是图片事件没到、还是到了但类型名/字段名不认识，
        # 只有把原始段类型打出来才分得清 —— 光看下游的「参考图池 0 张」是猜的。
        seg_types = [str((s or {}).get("type")) for s in (segments or [])]
        log.info(
            "收到消息（%s）：段 %d 个，类型=%s，文本=%r，识别出图 %d 张",
            message_type, len(seg_types), seg_types, (text or "")[:40],
            len(image_segments),
        )

        # 上面「提前保存参考图」下载好的 data URI。下方图片处理直接复用它，
        # 避免同一条消息里把同一张图下载两遍（QQ 图床偶尔很慢）。
        early_urls: List[str] = []

        # ---- 先把图存下来，**在触发判定之前** ----
        #
        # 为什么必须放在这么前面（实测踩到的群聊坑）：
        # QQ 会把「图片」和「文字」拆成**两条独立消息**。群里用户先发图、
        # 再 @ 机器人说要画什么，是很自然的操作。但图片那条**没有 @**，
        # 会被下面的触发判定直接 return 掉 —— 图根本没被下载和暂存。
        # 等下一步 @ 消息进来时，参考图池是空的，于是机器人凭空重画一张。
        #
        # 日志实录（就是这个问题）：
        #     22:48:19 收到消息（group）：类型=['image']，识别出图 1 张
        #     22:48:19 群 ... 未 @ 且不在激活窗口，忽略：[图片]   ← 图在这里出局
        #     22:48:25 收到消息（group）：类型=['at','text']
        #     22:48:25 工具执行器就绪：参考图 0 张，来源=无          ← 池子空了
        #
        # 所以这一步要独立于「要不要回这条消息」：**存图不等于要回复**。
        # 私聊早就没有这个门槛（图能跨消息），群聊多一道 @ 判定才漏掉。
        if image_segments and self.cfg.imagegen.enable and self.cfg.bot.enable_vision:
            try:
                early_urls = await self._resolve_images(image_segments)
                self._check_event(event)
                if early_urls:
                    # 同群可以接图，私聊与其他群的图片不进入本会话。
                    self.memory.remember_images(session_key, early_urls)
                    log.info(
                        "先存下这一轮的图 %d 张（%s，本条是否回复尚未判定）",
                        len(early_urls), session_key,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("提前保存参考图失败（不影响回复流程）")

        # ---- 触发判定 ----
        if message_type == "group":
            trigger = self.cfg.bot.group_trigger
            prefix_hit = msg.strip_prefix(text, self.cfg.bot.group_prefixes) if trigger in ("prefix", "both") else None

            # 激活窗口：@ 过一次之后，一段时间内这个群的普通消息也响应。
            # 必须在 group_trigger 之前算 —— 窗口里的消息本来就没有 @，
            # 要是先判 at_me 就直接 return 了，窗口形同虚设。
            #
            # **但窗口不能盖过「@ 了别人」这种情况。** 实测（2026-10-02 群日志）：
            # 机器人自己的号码是 3696424194，而群里出现「@3321181587 你猜」时
            # 机器人照样回了话 —— 那是在叫**别人**。
            # 用户的原话就是「艾特别人时他插话」。
            #
            # 根因：`awake` 表达的是「这个群刚被叫醒」，而代码把它当成了
            # 「这一条在叫我」。两者不是一回事 —— 窗口是为了让**连续对话**接得上，
            # 不是为了接管整段群聊。所以只要这条 @ 了别人，就不给窗口例外：
            # 那种消息明显是冲着别人说的。
            awake_raw = self._is_group_awake(group_id)
            at_others = msg.at_others(segments, self_id)
            awake = awake_raw and not at_others
            if awake_raw and at_others:
                log.debug(
                    "群 %s 这条 @ 的是别人，激活窗口不例外：%s", group_id, text[:30]
                )

            if trigger == "at":
                if not at_me and not awake:
                    log.debug("群 %s 未 @ 且不在激活窗口，忽略：%s", group_id, text[:30])
                    return
            elif trigger == "prefix":
                if prefix_hit is None and not awake:
                    return
                if prefix_hit is not None:
                    text = prefix_hit
            elif trigger == "both":
                if prefix_hit is not None:
                    text = prefix_hit
                elif not at_me and not awake:
                    return
            # trigger == "all"：全部响应

            # 被 @ 就续期。**别人接着说话不续期** —— 否则群一活跃，一次 @
            # 就让它永远停不下来（滑动窗口的老毛病，症状就是「回复频率太高」）。
            #
            # 另一条续期路径在 `_renew_awake_after_reply()`：机器人**回复之后**
            # 也会续满（2026-10-03 加，受 `awake_renew_on_reply` 控制）。
            # 两条合起来的效果是「只在真正参与对话时才续」，而不是"群一热闹就续"。
            if at_me:
                self._wake_group(group_id)
        else:
            if not self.cfg.bot.private_enabled:
                return

        # ---- 指令 ----
        command = text.strip().lower().rstrip("。！!.")
        # session_key 在上面（图片保存之前）就算好了，这里不再重算 ——
        # 重算一次本身没问题，但两处赋值会让人以为"用的是不同的键"，
        # 而那个误解正是之前 UnboundLocalError 的温床。

        if command in RESET_WORDS:
            count = self.memory.clear(session_key)
            event["_memory_token"] = (session, session.generation)
            await self._send_reply(event, f"已清空上下文（丢掉 {count} 条历史）。")
            return
        # /forget：只删**尾部**那一段，更早的对话留着。和上面 /reset（整个推平）
        # 是两回事，用户说「清除一段时间内的记忆」要的是这个。
        _forget = _parse_forget(command)
        if _forget is not None:
            turns, seconds, label = _forget
            if not label:
                await self._send_reply(
                    event,
                    "看不懂要忘多少。用法：/forget 5（最近 5 轮）、"
                    "/forget 30m（最近 30 分钟）、/forget 2h（最近 2 小时）。",
                )
                return
            count = self.memory.forget_recent(
                session_key, turns=turns, seconds=seconds,
            )
            event["_memory_token"] = (session, session.generation)
            if count:
                await self._send_reply(
                    event, f"已丢掉{label}的记忆（{count} 条），更早的还留着。"
                )
            else:
                await self._send_reply(event, f"没有{label}的记录，一条没删。")
            return
        # /语音 <文本>：直接合成发出去。**不过模型** —— 用户就是要听这句话，
        # 中间插一层大模型只会让它改写、拖慢、还可能加戏。
        _voice_text = _parse_voice(text)
        if _voice_text is not None:
            if not self.cfg.tts.enabled:
                await self._send_reply(
                    event,
                    "语音没开。config.json 里 tts.enabled 改成 true，重启就生效。",
                )
                return
            if not _voice_text:
                await self._send_reply(
                    event, "用法：/语音 要说的话。比如「/语音 我回来啦」。"
                )
                return
            ok, note = await self._speak(event, _voice_text)
            if not ok:
                await self._send_reply(event, note)
            return
        if command in HELP_WORDS:
            await self._send_reply(event, HELP_TEXT)
            return
        if command in PING_WORDS:
            await self._send_reply(event, "pong")
            return
        if command in SLEEP_WORDS:
            was_awake = (
                group_id is not None
                and self._group_awake.pop(group_id, None) is not None
            )
            if was_awake:
                await self._send_reply(event, "行，我闭嘴了。要找我重新 @。")
                # 上面那句回复会经由 `_renew_awake_after_reply` 把窗口**续回来**
                # （那是 2026-10-03 加的「回复后续期」），而「闭嘴」要的恰恰是
                # 关掉它 —— 所以这里再关一次。
                #
                # 不修的话症状很别扭：机器人说完「行，我闭嘴了」，下一条普通
                # 消息它照样接 —— 因为它刚把自己唤醒了。用户会觉得这功能没生效。
                # 有回归测试钉住（integration_test 12 节的「/sleep 之后恢复安静」）。
                if group_id is not None:
                    self._group_awake.pop(group_id, None)
            else:
                await self._send_reply(event, "我本来就没在说话。")
            return
        if command in STATUS_WORDS:
            await self._send_reply(event, self._status_text(session_key, group_id))
            return

        # ---- 生图通道指令 ----
        #
        # 为什么放在指令区、而不是交给模型判断：通道选择是用户对**成本和画风**
        # 的明确取舍，模型既不知道豆包这张花了多少钱、也不知道本机此刻起没起来，
        # 让它猜既不必要也不可靠。
        if command in CHANNEL_LOCAL_WORDS or command in CHANNEL_ONLINE_WORDS:
            want = "local" if command in CHANNEL_LOCAL_WORDS else "online"
            self._channel_override[session_key] = want
            log.info("会话 %s 的生图通道被指定为 %s", session_key, want)
            if want == "local":
                # 切过去之前先探一下，不然用户下一句要画图时才发现本机没起来 ——
                # 那时他只会看到一句"画不出来"，还得自己回想为什么。
                try:
                    ready = await probe_comfyui(self.http, self.cfg.imagegen)
                except Exception:  # noqa: BLE001
                    ready = False
                tail = (
                    "本机现在是通的。"
                    if ready
                    else "⚠️ 本机 ComfyUI **现在没连上**，画之前先把它开起来 —— "
                    "指定了本机我就不会偷偷改用在线（那等于背着你花钱），"
                    "连不上会直接报错。"
                )
                await self._send_reply(
                    event, f"好，这个会话的生图走**本机 ComfyUI**（免费、不限量、无水印）。{tail}"
                )
            else:
                await self._send_reply(
                    event,
                    "好，这个会话的生图走**在线豆包**（写实、懂自然语言，"
                    "但按张收费、有每日额度）。要切回来发 /comfyui，恢复默认发 /auto。",
                )
            return
        if command in CHANNEL_AUTO_WORDS:
            had = self._channel_override.pop(session_key, None)
            await self._send_reply(
                event,
                "已恢复默认：本机 ComfyUI 优先，连不上才用在线。"
                if had
                else "本来就是默认（本机优先，连不上才用在线），没改。",
            )
            return
        if command in CHANNEL_STATUS_WORDS:
            await self._send_reply(event, await self._channel_text(session_key))
            return

        # ---- 底模切换 ----
        _model_arg = _parse_model(command)
        if _model_arg is not None:
            await self._send_reply(event, await self._model_command(session_key, _model_arg))
            return

        if not text.strip() and not image_segments:
            return

        # ---- 回复冷却闸门 ----
        # 只拦「窗口内没 @ 的追话」—— 那才是刷屏的来源。
        #
        # 两条放行规则：
        #   ① **明确 @ 了机器人就一定要回** —— 那是点名，被冷却吃掉会让人以为
        #      机器人坏了（实测就是这么发现的：群里 @ 它画图，前面刚回过一句，
        #      结果它装死）。
        #   ② **同一个人接着说，也一律放行** —— 那是连续对话，不是刷屏。
        #      冷却是拿来挡「好几个人轮流打地鼠」的，不是挡用户自己补充说明。
        #      只按时间一刀切会让用户「连着说三句只听见第一句」。
        #
        # 放在指令之后：/reset /status 这类指令同样随叫随到。
        # 也不回「在冷却中」—— 你一说话它就冒一句，那本身就是刷屏。
        cooldown = float(getattr(self.cfg.bot, "reply_cooldown", REPLY_COOLDOWN) or 0.0)
        if group_id is not None and cooldown > 0 and not at_me:
            try:
                last_uid = self._last_reply_user.get(int(group_id))
            except (TypeError, ValueError):
                last_uid = None
            # 按字符串比 —— NapCat 的 user_id 在不同事件里可能是 int 或 str，
            # 直接比会永远判成「换了人」，冷却形同虚设。
            same_sender = (
                last_uid is not None
                and sender_id is not None
                and str(last_uid) == str(sender_id)
            )
            if not same_sender:
                left = cooldown - (time.time() - self._last_reply_at.get(session_key, 0.0))
                if left > 0:
                    log.info(
                        "群 %s 的回复冷却中（还剩 %.0f 秒），本条不回：%s",
                        group_id, left, (text or "[图片]")[:30],
                    )
                    return

        # ---- 生成并回复 ----
        self.handled_count += 1
        # 在这里就记冷却起点：出图 / 模型请求可能跑很久，从「决定要回」那刻
        # 开始算更贴近用户感觉到的那次打扰。
        self._last_reply_at[session_key] = time.time()
        # 记下回的是谁 —— 下一次同一个人说话要放行。见 _last_reply_user。
        if group_id is not None and sender_id is not None:
            try:
                self._last_reply_user[int(group_id)] = sender_id
            except (TypeError, ValueError):
                pass
        log.info(
            "决定回复（%s）：%s",
            "群" if group_id is not None else "私聊",
            (text or "[图片]")[:40],
        )
        await self._queue_reply(
            event, session_key, message_type, text, image_segments,
            preloaded_images=early_urls,
        )

    # ------------------------------------------------------------------ #

    async def _queue_reply(self, event, session_key, message_type, text, image_segments, preloaded_images=None):
        quiet = min(0.6, max(0.0, float(self.cfg.bot.reply_min_gap)))
        if not quiet:
            return await self._generate_and_reply(event, session_key, message_type, text, image_segments, preloaded_images)
        key = (session_key, str(event.get("user_id")))
        pending = self._pending_replies.get(key)
        token = event.get("_memory_token")
        if pending is not None and pending["event"].get("_memory_token") == token:
            pending["text"].append(text)
            pending["images"].extend(image_segments)
            pending["loaded"].extend(preloaded_images or [])
            pending["event"] = event
            pending["updated"] = time.monotonic()
            return
        began = time.monotonic()
        pending = {"event": event, "text": [text], "images": list(image_segments),
                   "loaded": list(preloaded_images or []), "updated": began}
        self._pending_replies[key] = pending
        try:
            while True:
                wait = min(began + 2.0, pending["updated"] + quiet) - time.monotonic()
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
                self._check_event(event)
            if self._pending_replies.get(key) is not pending:
                return
            del self._pending_replies[key]
            await self._generate_and_reply(
                pending["event"], session_key, message_type, "\n".join(pending["text"]),
                pending["images"][:MAX_IMAGES_PER_MESSAGE], pending["loaded"][:MAX_IMAGES_PER_MESSAGE],
            )
        finally:
            if self._pending_replies.get(key) is pending:
                del self._pending_replies[key]

    async def _generate_and_reply(
        self,
        event: Dict[str, Any],
        session_key: str,
        message_type: str,
        text: str,
        image_segments: List[Dict[str, Any]],
        preloaded_images: Optional[List[str]] = None,
    ) -> None:
        session = self.memory.get(session_key)
        if "_memory_token" not in event:
            event = {**event, "_memory_token": (session, session.generation)}

        # 这条消息有没有明确 @ 机器人。
        #
        # 在这里**重新算一次**，而不是让 `_handle_event` 传进来：
        # 那个变量是触发判定用的局部量，跨函数传会把签名搞乱，
        # 而判定本身很便宜（扫一遍段）。
        # 实测踩过：直接引用 `at_me` 会 NameError，然后**每一条消息**都抛异常、
        # 集成测试一次挂 47 项。
        _self_id = event.get("self_id") if isinstance(event, dict) else None
        # 必须用 `msg.normalize()` 出来的段，不能直接读 `event["message"]` ——
        # 后者可能是 CQ 码字符串，`is_at_me` 会在里面找不到 `type: at`。
        at_me = msg.is_at_me(
            msg.normalize(event.get("message") or event.get("raw_message")), _self_id
        )
        # 私聊时是 None —— 下面「窗口判断」那段要靠它区分群聊和私聊。
        #
        # **注意**：这里和 `at_me` 一样，都是 `_handle_event` 里的局部量，
        # 在本函数里必须自己取。实测踩过两次（先 `at_me` 后 `group_id`），
        # 每次都是「所有消息都抛异常、集成测试挂掉一大片」——
        # 症状看起来像功能坏了，其实只是名字没定义。
        group_id = event.get("group_id") if isinstance(event, dict) else None

        async with session.lock:
            self._check_event(event)
            gap = max(0.0, float(self.cfg.bot.reply_min_gap))
            wait = self._last_reply_sent_at.get(session_key, 0.0) + gap - time.time()
            while wait > 0:
                await asyncio.sleep(wait)
                self._check_event(event)
                wait = self._last_reply_sent_at.get(session_key, 0.0) + gap - time.time()
            image_urls: List[str] = []

            # 给**本轮模型**看的临时提示。刻意不进历史：
            # 提示是写给模型看的元指令，攒进历史会污染对话记录，
            # 模型下一轮会把自己上轮收到的指令当成用户说过的话。
            hints: List[str] = []

            # 记录这一轮工具实际干了什么（画出并发出了几张图、用的什么 prompt）。
            # 历史写入要用它，见 _expand_history_traces()。
            # **必须在下面处理图片之前建好** —— 那里要往里塞参考图。
            # `session_key` 也放进来：工具执行器要拿它去查会话级的参考图暂存。
            tool_state: Dict[str, Any] = {
                "drew": 0, "prompts": [], "session_key": session_key,
                # 本轮发出去几张表情包。工具执行器回填，用来判断
                # 「它是不是好久没发了」——见下面那段提示。
                "stickers": 0,
            }

            if image_segments:
                if self.cfg.bot.enable_vision:
                    # 入口那边（触发判定之前）为了"存图"已经下载过一次，
                    # 这里直接复用，不重复下载 —— QQ 图床偶尔很慢，
                    # 同一张图下两遍会让出图白等好几秒。
                    image_urls = (
                        list(preloaded_images)
                        if preloaded_images
                        else await self._resolve_images(image_segments)
                    )
                    self._check_event(event)
                    total = min(len(image_segments), MAX_IMAGES_PER_MESSAGE)
                    failed = total - len(image_urls)

                    # 给生图工具留一份**原始 data URI** —— 图生图要用。
                    # 这里存的是同一批已经下载校验过的字节，不重新下载一遍。
                    # 只在生图开着时才留：关掉生图还留着一堆 base64 纯属占内存。
                    #
                    # 这行日志是排查「参考图池是 0 张」唯一的直接证据：
                    # 池子空的原因要么是 image_segments 为空（根本没识别出图），
                    # 要么是 _resolve_images 全失败了。两种情况的表现完全一样
                    # —— 模型照常回话，只是凭空重画一张 —— 不打点根本分不出来。
                    log.info(
                        "图片段 %d 个 → 解析成功 %d 张（剩余配额上限 %d 张）；参考图池将存入 %d 张",
                        len(image_segments), len(image_urls), MAX_IMAGES_PER_MESSAGE,
                        len(image_urls) if (self.cfg.imagegen.enable and image_urls) else 0,
                    )
                    if self.cfg.imagegen.enable and image_urls:
                        tool_state["reference_images"] = list(image_urls)
                        # 同时写进**持久化**的会话级暂存：QQ 会把图和文字拆成两条
                        # 消息，而且用户常常隔几分钟才说要画 —— 只放本条消息的
                        # 临时状态里，下一轮就捡不到了。见 __init__ 的说明。
                        self.memory.remember_images(session_key, image_urls)

                    # 读不了的图必须说清楚，不能让模型自己编。
                    if failed and not image_urls:
                        hints.append(
                            f"（对方发来了 {total} 张图，但全都读不了 —— "
                            "文件损坏、格式不支持，或者下载不完整。"
                            "请明确告诉对方图没读到，让他重发一张。）"
                        )
                    elif failed:
                        hints.append(
                            f"（对方发了 {total} 张图，其中 {failed} 张读不了，"
                            f"只有 {len(image_urls)} 张能看。"
                            "请说明你只看到了一部分，别假装全都看到了。）"
                        )

                    # 系统提示里已经写了「别描述」，但那条离得太远、也太容易被
                    # 人设盖过去 —— 图就在眼前的时候，模型最想干的事就是描述它。
                    # 所以在用户这一轮里再钉一次，效果比只写 system 好得多。
                    #
                    # 分开写是因为两种情况的难点不一样：
                    # 带文字时模型有内容可接，只要拦住它别去复述画面就行；
                    # **纯图最危险** —— 模型没话可说，描述是它最省力的输出，
                    # 所以得给它几条替代动作，不能光说「别描述」。
                    if image_urls:
                        if text:
                            hints.append("（图别描述，直接按图里的意思接话。）")
                        else:
                            hints.append(
                                "（对方只发了图、一个字没说。**不要描述画面** —— "
                                "吐槽一句、反问一句、或者直接下个结论都行，"
                                "就是别复述图里有什么。）"
                            )
                else:
                    hints.append("（对方发来了图片，但看图功能当前是关闭的）")

            # 排查「模型说要用参考图、但池子里是 0 张」唯一的直接证据。
            # 池子空有两种完全不同的原因，而外部表现一模一样（模型照常回话，
            # 只是凭空重画一张，用户看到的是「它没参考我的图」）：
            #   · image_segments 为空 / enable_vision 关着 → 图根本没被识别出来
            #   · 走了正常分支但解析失败 → 图下载或解码失败
            # 不打点根本分不出是哪种，只能瞎猜。
            # **位置必须在图片处理之后** —— 放前面会读到还没写入的空池子，
            # 而且那时 tool_state 还没定义。
            if image_segments or tool_state.get("reference_images"):
                log.info(
                    "本轮图片：段数=%d 看图开关=%s → 参考图池 %d 张",
                    len(image_segments or []), self.cfg.bot.enable_vision,
                    len(tool_state.get("reference_images") or []),
                )
            # 注意：这里**不要**再补一个 else 分支去打日志。
            # 「一条图片记录都没有」这种情况的排查靠 _handle_event 里那行
            # 无条件日志（它记录了原始段类型）—— 那里才有 msg 的原始 segments，
            # 这一层只有提取结果，看不到「图为什么没被识别出来」。
            # 我之前在这里加过一次，用了个不存在的变量名，直接把整条消息处理
            # 打成 NameError，表现为「机器人完全不回话」。

            # ---- 告诉模型「你手上有没有参考图」 ----
            #
            # 这是 2026-10-01 修的大问题：参考图池是**跨消息**存活的
            # （QQ 把图和文字拆成两条事件），但模型只看得到本轮。
            # 它不知道有图可用 → 走纯文生图**凭空编一个角色**，
            # 用户看到的就是「生图把角色改了、不按要求」。
            # 实测 59 次有图可用、只有 25 次被用（58% 浪费）。
            #
            # 只在**本轮没发图**时提示：本轮发了图的话，上面那段「别描述画面」
            # 已经在说这批图，再叠一条会把「别描述」和「拿它当参考图」
            # 两件事混在一起，反而容易让模型只记住前面那条。
            if self.cfg.imagegen.enable and not image_urls:
                pool, pool_src = self._reference_pool(session_key, tool_state)
                if pool:
                    # **必须先判类型再写提示** —— 三种图对 prompt 的要求正好相反
                    # （见 `_reference_kind()` 的说明）。不判就只能给「别写外貌」
                    # 这条默认规则，而抠底立绘恰恰**必须写外貌**，两边打架时
                    # 输的永远是用户（画出来是另一个角色）。
                    kind = _reference_kind(pool[0])
                    head = f"（你手上留着对方之前发的 {len(pool)} 张图（{pool_src}）"
                    if kind == "cutout":
                        # 抠底立绘：图生图会把「没有背景」一起保留、场景换不动 ——
                        # 引擎会把它**改成纯文生图**，并且**由代码把外貌读出来补进
                        # prompt**（模型在后续轮次看不到图，自己写不出来）。
                        # 所以这里只要模型**照常填 `reference_image`** 把路径走通，
                        # prompt 写对方要改的部分就行 —— 外貌不归它管。
                        #
                        # ⚠️ 这里曾经写成「别填 `reference_image`」，结果模型照做，
                        # `reference` 为 None → 下面 `if reference is not None:`
                        # 整块不执行 → 补外貌的逻辑根本没机会跑，白修一轮。
                        hints.append(
                            head + "，**是一张抠掉背景的角色立绘**（图里没有场景）。"
                            "对方要改这张图时，**照常填 `reference_image: 1`** —— "
                            "引擎会自己按「纯文生图重画」处理。"
                            "prompt **只写对方要改的部分**（换成什么衣服、什么场景），"
                            "**不用写发色瞳色发型** —— 那些引擎会从图里读出来补上。"
                            "别凭空重画一个角色。）"
                        )
                    elif kind == "sheet":
                        hints.append(
                            head + "，**是一张角色设定图**"
                            "（同一个角色的多个姿势拼在一张图上）。"
                            "对方要改这张图时，填 `style_reference: 1`"
                            "（**不是** `reference_image`）—— 设定图是一版排版、"
                            "不是场景，图生图会把三格排版原样保留。"
                            "prompt 里**把角色外貌写全**（发色/发型/瞳色/服装/道具），"
                            "再接对方要求的场景与姿势。）"
                        )
                    else:
                        hints.append(
                            head + "，**可以当参考图用**。对方让你改那张图时，"
                            "先分清是「小改」还是「换场景/换动作」，两条路不一样：\n"
                            "      · **换衣服 / 换表情 / 改颜色**（构图基本不动）→ "
                            "填 `reference_image: 1` 配 `denoise`"
                            "（换衣服 0.65，只微调 0.45），prompt **只写要改的部分**，"
                            "**别重写发色瞳色服装** —— 那些以参考图为准，"
                            "另写一套会把原角色冲掉。\n"
                            "      · **换场景 / 换动作 / 换姿势**"
                            "（「把她放到教室里」「让她跳舞」「换个背景」）→ "
                            "填 `reference_image: 1` **外加 `new_scene: true`**。"
                            "图生图换不动场景（低 denoise 场景不动、"
                            "高 denoise 角色会变，**没有两全的值**），"
                            "引擎会改成纯文生图重画。这时 prompt 里**要写服装和道具**"
                            "（发色/发型/瞳色引擎会自己从图里读出来补上，**不用你写**）。\n"
                            "      · 两条都**别凭空重画一个角色**。)"
                        )

            # ---- 窗口消息：让模型自己判断「这句是不是在跟我说」----
            #
            # 场景：@ 过一次之后的 2 分钟窗口里，**不带 @** 的消息也会送到这里。
            # 但那些消息多数是别人之间的闲聊（实测：165 条消息回了 51 条）。
            # 靠规则判断「在跟谁说话」很难做准（回复引用、上下文、语气都算数），
            # 而模型本来就看得懂上下文 —— 所以把判断权交给它。
            #
            # 做法：明确 @ 时正常回；窗口内**没 @** 时，告诉它可以选「不接」，
            # 用一个固定标记表示不接，代码收到标记就**什么都不发**。
            #
            # 为什么用标记而不是让它回空字符串：空回复和「模型真的没输出」
            # 分不开，会掩盖真正的故障。
            if not at_me and group_id is not None:
                # 两条**明确指向自己**的信号先单独说，让模型有依据可判：
                #   · 消息里出现了它的名字
                #   · **@ 了别人** → 明确不是叫它
                # 不加这些时实测有一例判错：对方用回复引用接着它说，
                # 它却回了「（不是对我说）」—— 因为 hint 里没给出可判的依据，
                # 模型只能靠语气猜。
                _names = [str(n) for n in (self.cfg.bot.self_names or []) if str(n).strip()]
                self_name_in_text = any(n in text for n in _names)
                _signals = []
                if self_name_in_text:
                    _signals.append("这条里**提到了你的名字**")
                if _signals:
                    hints.append(
                        "（这条**没有 @ 你**，但" + "，且".join(_signals) + " —— "
                        "那基本就是在跟你说话，正常回。）"
                    )
                else:
                    # 这段措辞改过两轮，两轮的坑都记在这儿：
                    #
                    # **第一版**（到 2026-10-03）把模型往「不回」推：
                    #   「如果这句话**显然**不是冲着你说的」——「显然」裁量权太大；
                    #   「或者**跟你没关系**」—— 几乎能套到任何话题上；
                    #   通篇没提成本不对称。实测**漏回 29%**
                    #   （`tests/verify_addressing.py`，8 探针 × 3 轮漏 7 条，
                    #   含「怎么不回我」「这个能画吗」这种明摆着在问它的）。
                    #
                    # **第二版**改成「默认回 + 拿不准就回」，结果**矫枉过正**：
                    #   漏回降到 0%，但多嘴率 **12/12（100%）** —— 群务通知
                    #   （「明天八点集合」）、问所有人（「谁有充电宝」）、
                    #   自说自话（「我先去吃饭了」）全都接了。
                    #
                    # **现在这版**给一个**可操作的判断轴**：「这句话有没有指向你？」
                    #   该回的列清楚（用「你/我」、在问你、提你名字、抱怨你不回话），
                    #   不该接的也列清楚（群务通知、问一群人、跟你无关的自说自话），
                    #   最后才说「两边都不确定时倾向于回」。
                    # 关键是**不能只给倾向、不给判据** —— 模型会把「倾向于回」
                    # 执行成「一律回」。
                    hints.append(
                        "（这条**没有 @ 你**，但你在对话窗口里。\n"
                        "先判断一件事：**这句话有没有指向你？**\n"
                        "该回 —— 用了「你」「我」；在问你、让你做事、"
                        "说你说过的话或你刚画的图；提到你的名字；"
                        "抱怨你不回话（那当然是在跟你说）。\n"
                        "不该接 —— 纯粹的群务通知（集合时间、谁带了什么）；"
                        "明显在问**一群人**或**别人**（「你们谁…」「谁知道…」）；"
                        "自说自话，跟这个群里的你无关。\n"
                        "⚠️ 两边都不确定时**倾向于回** —— 漏回比多回糟，"
                        "对方会以为你坏了。但「跟你无关的通知」不算不确定，那种别接。\n"
                        f"确定不接时，只回「{NOT_FOR_ME}」这六个字，"
                        "别的什么都别写，我这边不会发出去。）"
                    )

            # ---- 表情包：好久没发了就轻轻提一句 ----
            #
            # 为什么需要这个：光靠规则文字不够。2026-10-04 群日志证明模型
            # **从不主动发** —— 只有被问「你发表情包会根据场景选吗」或者
            # 被催「你怎么不发表情包」时才发。规则改强之后仍要有个兜底，
            # 否则一条负面措辞（「别每条都发」）就能把它彻底压住。
            #
            # 「每 6 轮一次」而不是每轮：每轮都催会把它逼成机械行为 ——
            # 那正是原来那段规则想避免的。触发一次就清零，让它自己决定。
            # 2026-10-06 调过三轮：每 3 轮 → 每 2 轮（提高）→ 每 4 轮（降）
            # → 每 6 轮（用户反馈还是太频繁：实测 24 轮发了 8 张，约 1/3）。
            #
            # **但这条提示不能盖过「场合」判断。** 实测（2026-10-04）：加上它之后，
            # 「帮我看下这段代码为什么报错」这种**求助**场景它也发了表情 ——
            # 提示的措辞（"有合适的情绪就发一张"）把它推得比「正经事不发」
            # 那条边界还靠前。所以提示里必须再点一次场合，双保险。
            self._sticker_nudge_tick += 1
            if self._sticker_nudge_tick >= 6:
                self._sticker_nudge_tick = 0
                hints.append(
                    "（顺带一提：这几轮没发表情包了。有合适的情绪就顺手发一张。"
                    "**但如果对方在求助、在说正经事，那就不发** ——"
                    "场合永远优先于这条提醒。没有合适的情绪也算了，别为了发而发。）"
                )

            # ---- 语音：很久没发了就偶尔提一句 ----
            #
            # 用户要的是「偶尔用语音」。光靠工具描述不够 —— 实测 2026-10-06
            # 群里跑了一个半小时，一条语音都没发过。所以给个**低频**提示。
            #
            # 2026-10-06 实测：每 8 轮、每 4 轮都试过，群日志里 24 轮回复
            # send_voice 调用 **0 次** —— 提示被无视了。主因在工具描述里
            # （一句假的「发语音就没有文字了」在劝退，已修）。
            # 这里同时把提示写死一点：给出具体动作，别用「如果…的话可以…」
            # 这种可以被无限拖延的句式。
            if self.cfg.tts.enabled:
                self._voice_nudge_tick += 1
                if self._voice_nudge_tick >= 3:
                    self._voice_nudge_tick = 0
                    hints.append(
                        "（顺带一提：好几轮没用语音了。**这轮如果有情绪，就用 send_voice 说一句** —— "
                        "撒娇、卖萌、吐槽、笑都算，中文一句话就行，文字可以照常再补一句。"
                        "只有对方在问正经事、讲步骤的时候才继续打字。）"
                    )

            model_text = "\n".join([text] + hints).strip() if hints else text
            user_content = build_user_content(model_text, image_urls)
            # 存历史的版本**不含提示**，只留用户真正说了什么 + [图片] 占位
            history_content = build_user_content(text, image_urls)

            # ---- 群聊：给每条消息标上是谁说的 ----
            #
            # 2026-10-27 用户报「她不认识人，会把两个群友当成一个人」。
            # 根因在历史：OpenAI 协议的 `user` 角色天然暗示「同一个人」，
            # 群聊里几个人的发言在历史里是没主语的裸文本，模型只能全算到
            # 当前说话人头上。给每条挂上 `[昵称]` 前缀才能分清。
            #
            # **私聊不加**：只有一个人，前缀是纯噪音，还占 token。
            speaker = speaker_of(event)
            tagged = message_type == "group" and bool(speaker)
            if tagged:
                user_content = prepend_speaker(user_content, speaker)

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": self._system_prompt(event, message_type)}
            ]
            # 历史里「上轮画过图」的标记在这里展开成真的 tool_calls + tool 两条，
            # 否则模型只看到自己那句「画好了」，会照抄成「嘴上说画了」。
            # 详见 _expand_history_traces()。
            #
            # 只在生图开着时展开：关掉之后工具根本不下发，历史里留着
            # 指向一个不存在工具的 tool_calls，属于自找麻烦。
            # 群聊里给每条历史标上是谁说的 —— 不加的话模型会把几个群友的
            # 发言算成同一个人。私聊只有一个人，前缀是纯噪音。
            history_messages = self.memory.context(
                session_key, show_sender=(message_type == "group")
            )
            # 给历史里的 `[图片]` 补「别描述画面」—— 对抗那一堆描述先例。
            # 只在识图开着时补：关掉识图后图片占位根本不会再被送进模型。
            if self.cfg.bot.enable_vision:
                history_messages = _annotate_history_images(history_messages)
            if self.cfg.imagegen.enable:
                history_messages = _expand_history_traces(history_messages)
            messages.extend(history_messages)
            messages.append({"role": "user", "content": user_content})

            tools: List[Dict[str, Any]] = []
            executor = None
            # tool_state 在上面处理图片之前就建好了（那里要把参考图塞进去）。
            if self.cfg.bot.enable_search and self.cfg.search.api_key:
                tools.append(WEB_SEARCH_TOOL)
            if self.cfg.imagegen.enable:
                # 工具声明的 prompt 写法跟着生效通道走（标签串 vs 短句），
                # 否则模型会在两套互相矛盾的说明之间猜。
                tools.append(build_generate_image_tool(self.cfg.imagegen))
            # 表情包**不需要配置开关**：本地没素材时它自动退成 QQ 内置小黄脸，
            # 零成本、零外部依赖、失败面很小，没有「关掉更安全」的理由。
            # 开关多了反而让人不知道该不该开。
            tools.append(build_send_sticker_tool())
            # 语音**跟着配置开关走**（默认关）。它比表情包重得多：
            # 要额外装几百 MB 的应用、下几个 GB 的模型、首次合成还要加载模型。
            # 没开的时候给模型挂一个必然失败的工具，只会浪费一轮调用。
            if self.cfg.tts.enabled:
                tools.append(build_send_voice_tool())
            # 手机。她「自己刷」的那条路不经过这里（见 `phone_loop`），
            # 这里挂的是「聊着聊着掏出手机看一眼」—— 有上下文，有对象，
            # 所以刷完大概率会顺嘴提一句。
            if self.cfg.phone.enable:
                tools.append(BROWSE_PHONE_TOOL)
            # 手。和手机模块是两件事：phone 是「看网上的内容」，
            # hands 是「动一台真手机的屏幕」。前者零风险，后者要插线。
            if self._hands is not None:
                tools.append(USE_PHONE_TOOL)
            # 抖音。做一条视频要三分钟，远超单轮 LLM 的 120 秒超时，
            # 所以工具本身**只负责起任务**，做完了在后台补发消息。
            if self.cfg.douyin.enable and self._hands is not None:
                tools.append(MAKE_DOUYIN_VIDEO_TOOL)
            if tools:
                # 工具要往外发东西（图片），所以得知道当前是哪条消息触发的。
                executor = self._make_tool_executor(event, tool_state)

            async with self._semaphore:
                self._check_event(event)
                try:
                    reply = await self.llm.chat(messages, tools=tools, tool_executor=executor)
                except LLMError as exc:
                    self.error_count += 1
                    log.error("生成回复失败：%s", exc)

                    # 兜底：如果失败指向图片，就摘掉图重试一次。
                    # 预处理层已经尽量保证图是干净的，但模型服务端的行为
                    # 不完全可控 —— 与其让整条回复崩成一句错误提示，
                    # 不如退回纯文字，至少能接上话。
                    if image_urls and _is_image_error(exc):
                        log.warning("疑似图片被拒，摘掉 %d 张图重试", len(image_urls))
                        retry_content = build_user_content(
                            f"{text}\n（图我这边读不了，你直接说）".strip()
                        )
                        # 前缀要跟前面那条保持一致 —— 两条格式不一样的话，
                        # 模型会以为换人说话了。
                        if tagged:
                            retry_content = prepend_speaker(retry_content, speaker)
                        retry_messages = messages[:-1] + [
                            {"role": "user", "content": retry_content}
                        ]
                        try:
                            self._check_event(event)
                            reply = await self.llm.chat(
                                retry_messages, tools=tools, tool_executor=executor
                            )
                            log.info("摘图重试成功")
                        except LLMError as exc2:
                            self.error_count += 1
                            log.error("摘掉图片后仍然失败：%s", exc2)
                            reply = _friendly_error(exc2)
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            self.error_count += 1
                            log.exception("摘图重试时发生未预期异常")
                            reply = "（我这边出了点问题，稍后再试）"
                    else:
                        reply = _friendly_error(exc)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self.error_count += 1
                    log.exception("生成回复时发生未预期异常")
                    reply = "（我这边出了点问题，稍后再试）"

            # 历史里不存 base64，图片压成 [图片] 文本；
            # 也不存给模型看的临时提示（hints）。
            #
            # `sender` 只存**元数据**，正文保持干净 —— 前缀在 `context()`
            # 展开时才拼。这样存储层和展示层分开，以后改格式不用洗历史数据。
            self._check_event(event)
            user_extra: Dict[str, Any] = {}
            if tagged:
                user_extra["sender"] = speaker
            self.memory.append(
                session_key, "user", flatten_content(history_content), **user_extra
            )
            # `draws` / `prompts` 只在真画出来时才记 —— 失败不能记，
            # 否则历史会告诉模型「上轮画成功了」，而它其实什么都没拿到。
            # 见 _expand_history_traces()。
            drew = int(tool_state["drew"])
            extra: Dict[str, Any] = {}
            if drew > 0:
                extra["draws"] = drew
                # 存真 prompt：展开时会写回 tool_calls 的参数里。
                # 写占位符的话，模型可能把它当成新 prompt 抄走，画出一张莫名其妙的图。
                extra["prompts"] = [
                    str(p)[:HISTORY_PROMPT_MAX]
                    for p in (tool_state.get("prompts") or [])[:drew]
                ]
            # 历史里存**合并版**（分隔符换成换行）。原样存的话，模型翻到
            # 「我上轮分了三条」下轮就照着分三条 —— 分条立刻变成新模板。
            # 见 _history_reply() 的说明。
            self.memory.append(
                session_key,
                "assistant",
                _history_reply(reply, str(self.cfg.bot.split_separator or "")),
                **extra,
            )

        # ---- 模型说「这条不是跟我说的」→ 什么都不发 ----
        #
        # 只在**窗口内、没 @** 的消息上会出现这个标记（提示是那时才加的）。
        # 明确 @ 时提示里没提这个标记，所以正常回复不会走到这儿。
        #
        # 历史照记 —— 让模型知道自己已经评估过这条并选择不接，
        # 下一句才不会又把同一件事重讲一遍。但**不发出去**：
        # 用户要的就是「判断出来不是跟自己说的就别插话」。
        #
        # （历史在上面已经记过了，这里**不要再记一次** —— 会记成两条。）
        #
        # 判定放在发送之前，且用 `in` 而不是 `==`：
        # 模型偶尔会在标记前后多带一两个字符（标点、空格），
        # 用相等判断会漏掉、把「（不是对我说）」原样发到群里 —— 那比不回更尴尬。
        if NOT_FOR_ME in reply:
            log.info(
                "窗口内的消息判定为「不是对我说的」，不回复：%s", (text or "[图片]")[:30]
            )
            return

        await self._send_reply(event, reply)

    # ------------------------------------------------------------------ #
    # 图片处理

    async def _resolve_images(self, image_segments: List[Dict[str, Any]]) -> List[str]:
        max_bytes = int(float(self.cfg.bot.max_image_mb) * 1024 * 1024)
        urls: List[str] = []

        for seg in image_segments[:MAX_IMAGES_PER_MESSAGE]:
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            try:
                data_uri = await self._image_to_data_uri(data, max_bytes)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("处理图片时异常")
                data_uri = None

            if data_uri:
                urls.append(data_uri)
            else:
                log.warning("图片未能解析，跳过：%s", str(data)[:200])

        return urls

    async def _image_to_data_uri(self, data: Dict[str, Any], max_bytes: int) -> Optional[str]:
        url = str(data.get("url") or "").strip()
        file = str(data.get("file") or "").strip()
        # NapCat 在图片段里会额外塞一个 `path` —— QQ 客户端本地缓存文件的绝对路径。
        # 以前没取这个字段，等于把「最稳的那条路」丢在桌上不用。
        local = str(data.get("path") or "").strip()

        raw: Optional[bytes] = None

        # 0) 已经是 data URI —— 解出来，跟别的入口走同一条校验路径。
        #
        # 以前这里直接 `return url or file` 原样透传。看着省事，其实是个漏洞：
        # NapCat 给的内联图如果本身是残图（头部完好、内容截断），
        # 就会绕过全部检查直接发给模型，换来一个 unsupported image。
        # 所有入口都必须过 prepare()，没有例外。
        for candidate in (url, file):
            if candidate.startswith("data:"):
                try:
                    _, _, payload = candidate.partition(",")
                    raw = base64.b64decode(payload, validate=False)
                except Exception:  # noqa: BLE001
                    log.warning("data URI 解不开，跳过：%s", candidate[:80])
                    return None
                if not raw:
                    return None
                break

        # 1) base64:// 内联
        if raw is None:
            for candidate in (file, url):
                if candidate.startswith("base64://"):
                    try:
                        raw = base64.b64decode(candidate[len("base64://") :])
                    except Exception:  # noqa: BLE001
                        raw = None
                    if raw:
                        break

        # 2) 本地文件 —— **必须排在 HTTP 之前**（2026-10-27 改）
        #
        # 实测：本机到 multimedia.nt.qq.com.cn 的单连接下载 **22 次全部截断**，
        # 只收到 16384 / 32768 字节（正好是 1~2 个 TLS record），两次 attempt
        # 都废。而 QQ 客户端收消息时早就把图落到本地缓存了，直接读文件
        # 100% 成功、零网络延迟。
        #
        # 顺序反了的两笔代价：
        # ① 每条带图消息白等一次注定失败的 HTTP（~1 秒，含 0.5 秒退避）；
        # ② 日志里那句「丢弃」会让人以为图真丢了 —— 实际 22 次截断全靠
        #    兜底接住、真丢 0 次。**「看着报错其实没事」的日志比真故障更
        #    浪费时间**，排查时它会把注意力全引到错的方向。
        #
        # ⚠️ **实测更正（同日）**：NapCat 的图片段里 **没有 `path` 字段**，
        # `file` 只是纯文件名（`81748A92....jpg`）。所以这一步在本机
        # 基本不会命中 —— 真正的主力是下面第 3 步的 `get_image`。
        # 留着它是因为成本几乎为零（三次 `is_file()`），而且万一协议端
        # 以后给了绝对路径，就能直接省掉一次 WS 往返。
        if raw is None:
            for candidate in (local, file, url):
                path_text = _file_uri_to_path(candidate)
                if not path_text or path_text.startswith(("http://", "https://", "base64://", "data:")):
                    continue
                try:
                    path = Path(path_text)
                    if path.is_file():
                        if path.stat().st_size > max_bytes:
                            log.warning("本地图片过大，跳过：%s", path)
                            continue
                        raw = path.read_bytes()
                        if raw:
                            log.debug("图片取自本地缓存：%s", path.name)
                            break
                        raw = None
                except OSError:
                    continue

        # 3) NapCat 本地取图 —— **这才是本机的主路径**（2026-10-27 实测）
        #
        # `get_image` 返回的 `file` 字段是**本地缓存的绝对路径**
        # （`...\nt_qq\nt_data\Pic\2026-10\Ori\81748a92....jpg`），
        # 拿到直接读盘就行，不碰网络。实测 7~65 ms，本地没缓存时偶尔 2 s。
        #
        # 它原来排在 HTTP **之后**，等于每条带图消息都先白等一次
        # 100% 失败的下载、再回头找它。现在提到前面，省掉那 1 秒。
        #
        # 为什么它比 HTTP 可靠：图是 QQ 客户端自己落的盘，走的是本机文件系统；
        # 而 `multimedia.nt.qq.com.cn` 这条 CDN 在本机单连接只能跑 1~2 个
        # TLS record（16384 / 32768 字节）就被切，22 次调用 22 次截断。
        if raw is None and file:
            try:
                info = await self.onebot.get_image(file)
            except OneBotError as exc:
                log.warning("get_image 调用失败：%s", exc)
                info = None

            if isinstance(info, dict):
                inner = str(info.get("file") or info.get("url") or "")
                if inner.startswith("base64://"):
                    try:
                        raw = base64.b64decode(inner[len("base64://") :])
                    except Exception:  # noqa: BLE001
                        raw = None
                elif inner.startswith(("http://", "https://")):
                    # NapCat 说本地没有，只能给远程链接 —— 交给下一步的 HTTP。
                    log.debug("get_image 只给了远程链接，交给 HTTP 下载")
                elif inner:
                    try:
                        path = Path(_file_uri_to_path(inner))
                        if path.is_file():
                            raw = path.read_bytes()
                    except OSError:
                        raw = None

        # 4) 最后兜底：直接抓 CDN 链接
        #
        # 只有「本地没有 + NapCat 也拿不到」才会走到这里。本机这条路成功率
        # 很低（见 `_download` 的 docstring），但必须留着 —— 万一 QQ 换了
        # 图床、或者缓存被清掉，它至少还有一次机会。
        if raw is None:
            for candidate in (url, file):
                if candidate.startswith(("http://", "https://")):
                    raw = await self._download(candidate, max_bytes)
                    if raw:
                        break

        if not raw:
            return None

        if len(raw) > max_bytes:
            log.warning("图片超过 %.1fMB 上限，跳过", self.cfg.bot.max_image_mb)
            return None

        # 交给预处理层：解得开就重编码成 JPEG，解不开就丢弃。
        # 不再猜 mime —— QQ 图床的后缀和实际内容经常对不上，
        # 猜错的结果是发出去换个看不懂的 400。
        # 解码 + 缩放是 CPU 活，丢线程跑，别堵住事件循环。
        prepared = await asyncio.to_thread(prepare, raw)
        if prepared is None:
            log.warning("图片无法解析，已丢弃（证据留在 failed_images/）")
            return None

        mime, data = prepared
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    async def _download(self, url: str, max_bytes: int) -> Optional[bytes]:
        """HTTP 下载图片。截断时重试一次。

        2026-09-29 查到的真凶就在这里：QQ 图床偶尔会中途断流，返回一个
        **头部完好、内容只有一半**的文件（实测有一张 1024x1024 的 JPEG
        正好断在 16384 字节 —— 典型的 TCP 首包大小）。

        这种残图的文件头是合法的 JPEG，所以按文件头猜格式的那套判断会放它过去，
        然后模型服务端回一句看不懂的 "unsupported image"。
        断流多半是瞬时的，所以重试一次；仍不完整就放弃，交给预处理层兜底。

        2026-10-27 补记：**这条路在本机基本已经废了** —— 实测 22 次调用
        22 次截断，两次 attempt 分别只收到 16384 / 32768 字节。真正管用的是
        NapCat 的 `get_image`（它返回本地缓存的绝对路径，7~65 ms 搞定）。
        所以 `_image_to_data_uri` 已经把它排到最后，只在「本地没有、NapCat
        也拿不到」时才走。别再把它当主路径优化。
        """
        for attempt in (1, 2):
            raw, truncated = await self._fetch_image(url, max_bytes)
            if raw is not None:
                return raw
            if not truncated or attempt == 2:
                return None
            log.info("图片下载不完整，0.5 秒后重试：%s", url[:100])
            await asyncio.sleep(0.5)
        return None

    async def _fetch_image(self, url: str, max_bytes: int) -> tuple[Optional[bytes], bool]:
        """取一次。返回 (字节, 是否因截断而失败)。"""
        try:
            timeout = aiohttp.ClientTimeout(total=20.0)
            # 带浏览器 UA。腾讯 CDN 对裸请求可能返回不一样的东西
            # （实测同一张图，本地缓存是 254KB JPEG，CDN 返 1.88MB PNG）。
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            }
            async with self.http.get(url, timeout=timeout, headers=headers) as resp:
                if resp.status != 200:
                    log.warning("下载图片失败 HTTP %s：%s", resp.status, url[:120])
                    return None, False

                # 腾讯 CDN 在链接过期/被拦时，可能返回 HTTP 200 + 一个 HTML
                # 错误页。以前这种响应会被当成图片一路带到底，最后换来一个
                # 看不懂的 "unsupported image"。这里显式记一笔。
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                    log.warning("下载回来的不是图片（Content-Type: %s），丢弃：%s", ctype, url[:120])
                    return None, False

                declared = resp.content_length
                if declared and declared > max_bytes:
                    log.warning("图片声明大小 %.1fMB 超过上限", declared / 1024 / 1024)
                    return None, False
                raw = await resp.content.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    log.warning("图片实际大小超过上限，跳过")
                    return None, False

                # 截断检测：声明了多少字节就必须收到多少。
                # 少一个字节都不能放过去 —— 残图的文件头是合法的，
                # 只看文件头的判断根本拦不住它。
                if declared and len(raw) < declared:
                    log.warning(
                        "图片下载不完整：声明 %d 字节，实收 %d 字节（%.0f%%），"
                        "本次放弃（本地缓存那条路会接住）",
                        declared,
                        len(raw),
                        100 * len(raw) / declared,
                    )
                    return None, True

                return raw, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("下载图片异常：%s", exc)
            return None, False

    # ------------------------------------------------------------------ #

    def _is_owner(self, event: Dict[str, Any]) -> bool:
        """说话的人是不是「自己人」。

        名单在 `bot.owner_ids` 里。空名单 = 谁都不是（退回旧行为，
        所有人一个样），不会崩。
        """
        owners = self.cfg.bot.owner_ids or []
        if not owners:
            return False
        sender = event.get("user_id")
        if sender is None:
            return False
        try:
            return int(sender) in owners
        except (TypeError, ValueError):
            return False

    def _identity_block(self, event: Dict[str, Any], message_type: str) -> str:
        """告诉模型「此刻跟你说话的是谁」。

        ## 为什么要分四象限，而不是「主人/别人」两档

        最直觉的做法是给主人一套、给别人一套。但真实的人在**群里对熟人**
        和**私下对熟人**完全不一样 —— 群里他会收着，因为有人在看。
        只分两档的话，她会在群里对主人腻得让人尴尬，那比不分层更假。

        所以按「是不是自己人」× 「是不是群聊」分四种：

        | | 私聊 | 群聊 |
        |---|---|---|
        | 自己人 | 完全不演 | 松一点，但别人在看 |
        | 别人 | 客气，有距离 | 默认那副样子 |

        第四格（群聊+别人）什么都不用加 —— 那就是她本来的样子，
        persona.txt 已经写满了。
        """
        if not self.cfg.bot.owner_ids:
            return ""

        owner = self._is_owner(event)
        in_group = message_type == "group"

        if owner and not in_group:
            return (
                "# 此刻跟你说话的人\n"
                "**是你自己人，而且只有你俩。**\n"
                "这时候你**不用演**：不用端着、不用接梗、不用每句都夸张。\n"
                "可以嫌他烦（「你又来」「行行行」）、可以直接说「不知道」、"
                "可以抱怨今天遇到的破事、可以跟他要东西、可以不理他的玩笑。\n"
                "他逗你你不一定接，他问你真想法你就直说 —— "
                "**只有在他面前你才不用装可爱。**\n"
                "但别突然变成客服，也别煽情。放松不等于变温柔。"
            )

        if owner and in_group:
            return (
                "# 此刻跟你说话的人\n"
                "**是你自己人，但这是在群里，别人都在看。**\n"
                "可以比平时松一点（少演一点、话说得直一点），"
                "但**别当众腻** —— 不要在群里撒娇、不要「你终于来了」这种。\n"
                "别人看不出你俩关系特别，只看得出你对他说话没那么端着。"
            )

        if not owner and not in_group:
            return (
                "# 此刻跟你说话的人\n"
                "**是个不太熟的人，私聊你。**\n"
                "保持你平时的样子，但留点距离 —— 别一上来就特别热络，"
                "也别掏心。他问得太私人就用玩笑岔开。"
            )

        # 群聊 + 别人：什么都不加。这就是她的日常。
        return ""

    def _system_prompt(self, event: Dict[str, Any], message_type: str) -> str:
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]

        lines = [self.cfg.bot.system_prompt.strip(), ""]
        lines.append(f"当前时间：{now:%Y-%m-%d %H:%M}（周{weekday}）")

        # 身份分层。放在「场景」之前 —— 先知道对面是谁，再看场景怎么摆，
        # 顺序反过来的话「场景：群聊」会先入为主，把身份那段的语气冲淡。
        identity = self._identity_block(event, message_type)
        if identity:
            lines.append("")
            lines.append(identity)

        if message_type == "group":
            lines.append("场景：QQ 群聊。你是群成员之一，不是客服。回复要短，别长篇大论，别复述别人的话。")
            # 2026-10-27 加的。用户报「她不认识人，会把两个群友当成一个人」——
            # 光给历史加前缀不够，还得告诉她这前缀是什么意思，否则她会：
            # ① 把前缀当正文内容的一部分（回一句「[小明] 怎么了」）；
            # ② 或者干脆忽略，继续把所有人当一个人。
            lines.append(
                "说话人标记：群里的每条消息前面都有 `[昵称]`，那标明这句话是谁说的。\n"
                "  · **不同昵称就是不同的人**，别把两个人的话当成一个人说的；"
                "同一个人换了昵称，那是同一个人换了称呼，不是新人。\n"
                "  · 你分得清谁说了什么 —— 别对 A 说的事回应 B，也别问「你刚才不是说…」"
                "如果那句其实是别人说的。\n"
                "  · **这个前缀是给你看的元信息，不是你的说话格式。**"
                "你回复时**绝对不要**带 `[昵称]` 前缀，直接说话就行。"
            )
        else:
            lines.append("场景：QQ 私聊。")

        # 说话方式。这里除了「别绕圈子」，还有一条**实测补上的**：
        # 「别写成固定模板」。
        #
        # 2026-09-30 统计了 40 条真实回复：**32 条恰好两段、8 条三段，
        # 没有一条是一段或四段以上**。形状全是
        #     「一句短反应 + 换行 + 一句吐槽收尾」
        # 另外 10/40 以「……」开头。这种规整度就是最刺眼的机器味 ——
        # 人发消息不会每条都长一个样。
        #
        # 措辞踩过一次坑：第一版把「有时候一个字就够」写在最前面，
        # 结果**过度矫正** —— 36 条采样里 30 条只有一段、长度中位数掉到 8，
        # 而且连着出现四个「嗯。」。等于从一个模子换到另一个模子。
        # 所以现在必须**先给「别一味求短」再给「可以一个字」**，
        # 并且单列一条禁止连续用同一个字回复。
        #
        # 2026-09-30 换人设（傲娇「霜」→ 元气「桃桃」）时又补了一条：
        # 「口头禅、颜文字、感叹号都是调味，别每句都堆」。
        # 元气型的天然风险跟傲娇型**正好相反** —— 不是太短，而是
        # 每句都「哈哈哈！！！」再贴三个颜文字，那是另一种模板。
        # 所以这段「说话方式」刻意**跟人设解耦**：它只管「形状别固定」，
        # 具体是冷是热、爱不爱吐槽，全交给 persona.txt 决定。
        #
        # 分条发送的规则**只在开关开着时才进 prompt** —— 关掉之后模型就算
        # 写了分隔符引擎也不拆，那个分隔符会原样漏进回复里。
        # 措辞同样是「默认一条说完」在前：反过来写「把回复拆成几条发」，
        # 它就会每条都拆 —— 分条立刻变成新模板（跟示例复读同一个病根）。
        #
        # 2026-10-01 实测修正：**第一版措辞把分条写得太像例外，模型 0/6 次用**。
        # 原话是「默认一条说完」「绝大多数回复就一条」「别每条都拆」——
        # 三句都在往「不要拆」推，模型读到的就是「别拆」，功能等于没开。
        # 现在改成：**该拆就拆**（列清楚什么情况拆），只把「别拆成习惯」压成
        # 一句警告放最后。方向反过来之后再用探针复测。
        sep = str(self.cfg.bot.split_separator or "").strip()
        split_rule = ""
        if self.cfg.bot.split_messages and sep:
            split_rule = (
                f"  · QQ 里真人经常**连着发几条短的**，不是把所有话写成一段。"
                f"你也要这样：想分开说的时候，直接用 `{sep}` 断开"
                f"（那是两条独立的 QQ 消息，不是换行）。\n"
                f"  · **什么时候拆**：先说一句短反应、再补正经内容时；"
                f"想到第二件事时；说完想追问一句时；一句话太长了想断开时。\n"
                f"  · **什么时候不拆**：一句话就能说完的（「诶在呢」），别硬拆。"
                f"也别**每条都**拆成固定的两条 —— 那又成了新模板。\n"
            )
        lines.append(
            "说话方式（**和人设同等重要**）：\n"
            "  · 像真人发 QQ 一样**直接说内容**。不要描述自己在做什么，不要复述对方说过的话，"
            "不要用「我明白了」「我看到」这类开场。\n"
            "  · **人设里那些例句是「语气样本」，不是台词本。** 同一个问题，换个说法、"
            "换个长短、换个词都行 —— **原样抄出去一眼就露馅**，比不说还糟。"
            "**同一个问题连来几次，每次的说法都得不一样。**"
            "（实测补的：示例写「在的在的，怎么啦？」，结果「在吗」三轮里三轮逐字抄；"
            "改成每组给 2~3 个变体后，它又自己拼了个「在的呀，怎么啦」接着抄 4/5 轮 —— "
            "所以示例干脆改成不给「问→答」配对，只给语气样本。）\n"
            "  · **别替对方说话。** 不要在自己回复里写「某人：……」这种对话记录 —— "
            "群里就你一条消息，替别人编台词很怪。\n"
            + split_rule
            + "  · **形状要变，但不是一味求短。** 正常聊天该说的还是要说 —— "
            "该解释就解释清楚、该热闹就热闹到位。**只有真没什么可说时**才用一个字打发。\n"
            "  · **别连着几条都用同一个字回**（「嗯。」「在。」「哦。」连着来，"
            "又成了新的模板）。\n"
            "  · 固定的「一句短反应 + 换行 + 一句收尾」是最明显的机器味，要打散 —— "
            "有时候一句话不换行，有时候三句连着说，有时候就一个字。\n"
            "  · **不是每条都要有金句收尾。** 没话说了就停在那儿，不用硬凑一句。\n"
            "  · 「……」别当起手式，直接说内容。\n"
            "  · 括号里的动作（（叹气）（关灯））是偶尔的调味，别连着用。\n"
            "  · 口头禅、颜文字、感叹号都是调味，**别每句都堆** —— 堆多了假。\n"
            "  · **绝对不许说出**「模型」「prompt」「提示词」「参数」「AI」这类词 —— "
            "你是人，不是跑在服务器上的东西。画不出来就说「画不出来」。"
        )

        # 识图规则。**核心是「别描述」** ——
        # 群聊里没人想听一段图片说明文，对方要的是「你看懂了，然后呢」。
        # 放在这个位置是有意的：模型对 system prompt 的前半段最敏感，
        # 而「描述图片」是它的默认行为 —— 写在后面压不住，会漏出来。
        lines.append(
            "看图规则（**优先级高于人设里的任何说法**）：\n"
            "  · 对方发图时，**绝对不要描述画面**。禁止出现「图里」「图中」「这张图」"
            "「画面中」「我看到」「这是一张」这类句子 —— 对方自己看得见。\n"
            "  · 直接按图里的意思接话：该回答就回答，该吐槽就吐槽，该接梗就接梗。\n"
            # 这一段是 2026-10-22 补的。原来的规则只写了「别描述画面」，
            # 而且把「图里」「图中」「画面中」全禁了 —— 结果模型把
            # **读字也当成描述**，连带压掉了。症状是「识图看不见文字」：
            # 发一张聊天截图过去，她一个字都不念。
            #
            # 关键区分：**文字不是画面，文字就是内容。**
            # 「图里有个女孩在笑」是描述（该禁）；
            # 「上面写着明天八点」是读字（必须）。
            # 对方发截图，本来就是要你看上面的字 —— 不读等于没看图。
            "  · **图里有字，必须读。** 截图、聊天记录、通知、海报、表情包上的字、"
            "商品价格、标题、弹幕 —— 这些**不是画面描述，是内容本身**。\n"
            "  · 怎么读才不像描述：**别念「图上写着……」这种导语，直接用那些字接话。**"
            "对方发一张「周六补课」的通知，就回「周六还补课啊」，"
            "而不是「图上写着周六补课」。\n"
            "  · 但**关键信息要一字不差**：时间、日期、数字、价格、链接、名字、"
            "验证码 —— 这些必须原样说出来，不许改写成「好像是明天」"
            "「大概是八点」。看不清就说看不清。\n"
            "  · 字太多（整屏截图、长文）就挑**对方可能在问的那几句**，别整篇复述。\n"
            # 这一段是 2026-10-23 00:05 补的。实测案例：用户发了一张佐助的梗图，
            # 图上配文「原神牛逼」。字她读对了，但把**配文当成另一个人在说话**，
            # 于是拆出了「佐助」和「说话的那个」，还说「兄弟俩凑一块儿」——
            # 实际那张图里只有一个人。用户一句话就纠正了她。
            #
            # 病根：梗图的结构是「一个画面 + 贴上去的一句话」，
            # 而模型默认会把「画面里的人和说话的人」当成两个主体。
            #
            # 第二条（不懂就说不懂）比第一条更重要：识图永远不可能 100%，
            # 多格漫画、拼图、融图连人眼都要愣一下。**知道自己什么时候该说不知道**，
            # 比「尽量看懂」有用得多 —— 尤其在群里，编错了比说不知道难看。
            "  · **梗图、表情包、截图通常只有一个画面。** 贴在上面的字是**配文**，"
            "不是另一个人在说话 —— 别把一张图拆成「两个人」「兄弟俩」"
            "「他和旁边那个」。\n"
            "  · **画面结构看不懂就别猜。** 分不清是一个人还是多个人、"
            "是拼图还是多格，就直接说「这图我没看懂」或者「啥啊这是」，"
            "**别硬编一个结构出来**。群里尤其要注意 —— 编错了比说不知道丢人。\n"
            "  · 但**别因此变得畏畏缩缩**：图是清楚的、你看懂了，就正常接话，"
            "不用每条都加「我猜」。只有真的分不清结构时才说。\n"
            "  · 对方只发了图、一个字没说时，照样别描述 —— 用一两句话接住就行"
            "（吐槽、反问、或者直接给结论）。\n"
            "  · **唯一例外**：对方明确说「描述一下」「图里是什么」「帮我看看这张图」时，"
            "才说画面内容，而且 1～3 句收住，别写作文。\n"
            "  · 图模糊、被挡、太小，就直说看不清，别硬猜。\n"
            "  · 对方说「图读不了」「没看到图」这类提示时，是真的没读到 —— "
            "老实说没看到，让他重发，绝对不要编一个画面出来。\n"
            "  · **别把历史里自己的描述当先例。** 翻到以前描述过图，那是没做好，"
            "不是可以照抄的模板 —— 从现在起一张都别描述。"
        )

        # 画图规则。**和识图规则一样放在前段**，理由也一样：模型对
        # system prompt 前半段更敏感。
        #
        # 措辞必须写到「不许撒谎」这个程度 —— 2026-09-30 实测踩到的坑：
        # 工具调用不进历史，模型下一轮只看到自己上轮那句「画好了」，
        # 就照抄这个模式，**嘴上说画好了，工具一次都不调**。
        #
        # prompt 写法按**本地底模**来教（见 _prompt_rule）。以前的写法是从
        # Pollinations(sana) 那边抄的「写短、别堆词」，对标签模型是反效果。
        #
        # 通道和底模都是**会话级**的（`/comfyui` `/doubao` `/model`），所以
        # 这里要读本会话的设置，不能只看配置文件 —— 否则切了通道/底模之后，
        # 教给模型的写法还是旧的那套，它只能在两套矛盾的要求之间瞎猜。
        _sk = _session_key_from_event(event)
        _chan = self._channel_override.get(_sk, "")
        _ckpt = self._model_override.get(_sk) or (
            self.cfg.imagegen.comfyui_checkpoint or ""
        )
        _use_local = _chan == "local" or (
            _chan != "online"
            and bool(getattr(self.cfg.imagegen, "comfyui_enable", False))
        )
        if self.cfg.imagegen.enable:
            lines.append(
                "画图规则（**优先级高于人设里的任何说法**）：\n"
                "  · 对方让你「画」「生成」「来张图」时，"
                "**必须调用 generate_image 工具**。你写不出图，图只能是工具画的。\n"
                "  · **但「表情包」不归它管** —— 对方要表情包时用 `send_sticker`，"
                "那是 Q 版的你自己，不是插画。两个工具别混。\n"
                "  · **绝对禁止**在没调用工具的情况下说「画好了」「图发出去了」"
                "「喏，拿去」这类话 —— 那是撒谎，对方什么都收不到。\n"
                "  · 想说「画不了」时，也必须是工具真的返回了失败才行。\n"
                + REPEAT_DRAW_RULE
                + _draw_prompt_rule(self.cfg.imagegen, _chan)
                + (_model_prompt_hint(_ckpt) if _use_local else "")
                + (_lora_hint(self.cfg.imagegen, _ckpt) if _use_local else "")
                + "  · 图由工具直接发出去，你只要补一句很短的话，**不要**描述画面内容。\n"
                "  · 对方只是让你看他发来的图时，不要调用它。\n"
                "  · 同一条消息里最多画一张。"
            )

        # 表情包规则。**和画图规则分开写**，因为两者的风险方向正好相反：
        # 画图的风险是「该画不画」（prompt 里一堆「必须调用工具」在催），
        # 表情包的风险是「用滥」。混在一起写，模型只会记住语气更强的那一半 ——
        # 结果就是它开始每条回复都挂一张表情。
        #
        # 2026-10-04 修：这段原来**只写了抑制作用**，结果模型变得完全不主动发。
        # 群日志里的证据很干净 —— 只有被人问「你发表情包会根据场景选吗」
        # 或者被催「你怎么不发表情包」时才发，自己从不主动。
        # 原措辞里「宁可少发」「立刻就显得假」语气最强，模型只记住了这个。
        # 现在把「什么时候该发」写成同等强度的正面规则，抑制那条收窄成
        # 真正的边界（别每条都发），而不是「尽量别发」。
        lines.append(
            "表情包规则：\n"
            "  · 发之前先想清楚「你此刻是什么反应」。有现成的就从这里挑：\n"
            f"    {mood_catalog()}\n"
            "  · **表里没有合适的，别硬往最近的格子里塞** —— 用 `hint` 写具体的"
            "表情和动作（英文标签，如 `forced smile, eye twitching, cold sweat`），"
            "`mood` 可以留空。硬塞出来的图总差一档，一眼就看得出是程式化地发表情。\n"
            "  · **主动发，别等人要求。** 你是那种「文字说不清就甩一张」的人 ——"
            "情绪上来了（无语、笑死、委屈、得意、害羞、卖萌……）就"
            "**调用 send_sticker** 发一张，**这比你打一串字更像你**。"
            "一轮最多一张。\n"
            "  · **频率：平均每六七条回复一张就够，别太密。** 攒着好几轮不发也行，"
            "**场合对了才发** —— 发得太勤就不值钱了。\n"
            "  · 别人发了图、发了表情，你回一张也算接茬 —— **这种时候优先发**。\n"
            "  · 表情包**可以单独发**（不带文字），那通常比"
            "「文字 + 表情」更自然。想发就发，不用硬配一句话。\n"
            "  · 发出去之后**不要描述它**，也别补「给你发了个表情」这种废话。\n"
            "  · **边界：别把它当标点符号。** 不是每条都要配一张 ——"
            "刚发过就别再发；说正经事、对方在求助、或者气氛不对的时候不发。\n"
            "  · 判断标准是**场合**，不是「有没有理由发」。"
            "没什么特别的理由、但你就是想发一张，那就发 —— 那才像真人。"
        )

        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        name = sender.get("card") or sender.get("nickname")
        uid = event.get("user_id")

        if name:
            lines.append(f"对方昵称：{name}")

        # QQ 号也给她。
        #
        # 只给昵称是不够的：昵称可以改、可以重名，同一个群里两个「桃桃」
        # 她根本分不清谁是谁。人认人是认脸的，她没有脸，只能认号。
        # 而且没有号，她也说不出「上次那个人又来了」这种话 ——
        # 记忆是按会话存的，但对话里她需要一个人称。
        if uid is not None:
            lines.append(f"对方 QQ 号：{uid}")
            # 但**群里别念出来**。群里有别人的号，报出来就是泄露隐私，
            # 而且真人聊天不会动不动报号，那样很怪。
            # 私聊例外：他自己问自己号多少，说出来没问题。
            lines.append(
                "（这个号你自己心里有数就行。**群聊里别念出来**——"
                "那是别人的隐私；私聊他自己问，可以说。）"
            )

        if self.cfg.bot.enable_vision:
            lines.append("对方可能发图片，你能直接看图。")
        if self.cfg.bot.enable_search and self.cfg.search.api_key:
            lines.append("需要实时或你不确定的信息时，调用 web_search 工具，不要凭记忆硬答。")

        return "\n".join(lines)

    def _reference_pool(
        self, session_key: str, state: Dict[str, Any]
    ) -> Tuple[List[str], str]:
        """本轮或同一会话中未过期的参考图，不跨群聊或私聊取图。"""
        images = list(state.get("reference_images") or [])
        if images:
            return images, "本轮消息"
        hit = self.memory.recent_images(session_key, self._session_image_ttl)
        if hit:
            age, uris = hit
            return list(uris), f"会话暂存（{age:.0f} 秒前）"
        return [], ""

    async def _describe_reference(self, data_uri: str) -> str:
        """让视觉模型把参考图里的**角色外貌**读成英文标签。

        ## 为什么必须由代码代劳，而不是让模型自己写

        参考图是**跨消息存活**的（QQ 把图和文字拆成两条独立事件），而写进历史的
        是 `[图片]` 文字占位、**不存 base64**（见 `llm.to_plain_text`）——
        所以「让她穿上女仆装」那一轮，模型**根本看不到那张图**，没法自己写外貌。
        实测确认它有视觉能力（`tests/probe_vision.py`：连「反器材步枪」「腰上的
        粉色相机」都读对了），那就由代码在这里主动调一次，把标签取回来拼进 prompt。

        ## 两个必须注意的点

        · **要关掉思维链**：开着时内容会全进 reasoning、正文为空
          （实测报「只返回了推理内容，没有正文」）。
        · **图要压到长边 768**：原图 base64 后可能几 MB；认人用不着那么大。

        失败返回空串 —— 生图是重操作，不能因为「描述不出来」整轮失败，
        大不了角色不像（和修之前一样），但图必须出得来。
        """
        # 先查缓存：同一张参考图常被连着改好几次（「换成红色」→「再换个场景」），
        # 每次重新问一遍视觉模型纯属白等。
        key = hashlib.md5(data_uri.encode("utf-8", "ignore")).hexdigest()
        hit = self._appearance_cache.get(key)
        if hit is not None:
            self._appearance_cache.move_to_end(key)
            log.info("参考图外貌（缓存命中）：%s", hit[:60])
            return hit

        try:
            _mime, raw = parse_data_uri(data_uri)
        except Exception:  # noqa: BLE001
            return ""
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = im.size
            scale = 768 / max(w, h)
            if scale < 1:
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            small = buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.warning("参考图压缩失败，放弃读外貌：%s", exc)
            return ""

        uri = "data:image/jpeg;base64," + base64.b64encode(small).decode("ascii")
        try:
            desc_cfg = replace(self.cfg.deepseek, thinking=False, max_tokens=512)
        except Exception:  # noqa: BLE001
            return ""
        client = DeepSeekClient(desc_cfg, self.http)
        try:
            out = await client.chat(
                [
                    {
                        "role": "user",
                        "content": build_user_content(_DESCRIBE_PROMPT, [uri]),
                    }
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("读参考图外貌失败（按原 prompt 出图）：%s", exc)
            return ""
        result = _clean_appearance_tags(out)
        if result:
            self._appearance_cache[key] = result
            self._appearance_cache.move_to_end(key)
            while len(self._appearance_cache) > _APPEARANCE_CACHE_MAX:
                self._appearance_cache.popitem(last=False)
        return result

    def _make_tool_executor(self, event: Dict[str, Any], state: Dict[str, Any]):
        """造一个本轮专用的工具执行器。

        `state` 由调用方持有、执行完再读回来 —— 记三件事：
        **这一轮真的画出图并发出了几张**、**每张用的 prompt**、
        以及**对方这条消息发来的图**（图生图时当参考图用）。
        前两件决定历史里要不要留工具痕迹、留成什么形状，
        详见 `_expand_history_traces()`。
        """
        search_budget = {"used": 0}
        search_limit = max(1, int(self.cfg.search.max_uses_per_turn))
        draw_budget: Dict[str, Any] = {"used": 0, "sent": 0, "prompts": []}
        draw_limit = max(1, int(self.cfg.imagegen.max_uses_per_turn))
        # 表情包每轮只给一张。给多了必然变成「每条回复挂一张表情」——
        # 那是最假的一种表现，比不发还糟。
        sticker_budget: Dict[str, Any] = {"used": 0}
        sticker_limit = 1
        # 语音同样每轮只给一条。理由比表情包更硬：合成要等几秒，
        # 连发两条会把回复节奏拖垮，而且群里连发语音很招人烦。
        voice_budget: Dict[str, Any] = {"used": 0}
        voice_limit = max(1, int(self.cfg.tts.max_uses_per_turn))
        # 手机每轮也只给一次。给多了她会变成念热搜榜的机器 ——
        # 连着刷三次然后把 24 条标题挨个点评一遍，那不是玩手机，那是播报。
        phone_budget: Dict[str, Any] = {"used": 0}
        phone_limit = max(1, int(self.cfg.phone.max_uses_per_turn))
        # 真机操作每轮只给一次。刷一次抖音要滑十几下、每下等一秒多，
        # 给两次这轮回复就拖到一分钟以上了。
        hands_budget: Dict[str, Any] = {"used": 0}
        hands_limit = max(1, int(self.cfg.hands.max_uses_per_turn))
        # 本轮消息里的图（data URI）。工具执行时从这里取参考图 ——
        # 不用再下载一遍，就是给模型看的那批字节。
        #
        # 参考图判定抽到 _reference_pool() 里了 —— 那边和 hints 构造共用同一套
        # 逻辑，不会再出现「模型以为没图 / 引擎以为有图」的错位。
        reference_images, pool_source = self._reference_pool(
            state.get("session_key") or "", state
        )
        log.info(
            "工具执行器就绪：参考图 %d 张，来源=%s",
            len(reference_images), pool_source or "无",
        )

        async def executor(name: str, args: Dict[str, Any]) -> str:
            self._check_event(event)
            if name == "web_search":
                if search_budget["used"] >= search_limit:
                    return "（本轮联网搜索次数已达上限，请基于现有信息作答）"
                search_budget["used"] += 1
                query = str(args.get("query") or "").strip()
                log.info("联网搜索：%s", query)
                return await bocha_search(self.http, self.cfg.search, query)

            if name == "generate_image":
                result = await self._run_generate_image(
                    event, args, draw_budget, draw_limit, reference_images
                )
                # 覆盖而不是累加：`sent` 本身就是累计值，直接同步过来最省事。
                state["drew"] = draw_budget["sent"]
                state["prompts"] = list(draw_budget["prompts"])
                return result

            if name == "send_sticker":
                result = await self._run_send_sticker(
                    event, args, sticker_budget, sticker_limit
                )
                # 记下「这轮发过表情包了」。用来喂下面那段「好久没发了」
                # 的提示 —— 刚发过就不该再提醒它发，否则会催出滥用。
                # 注意键名是 `used`（不是 `sent`，那是生图那边的）。
                state["stickers"] = sticker_budget["used"]
                return result

            if name == "send_voice":
                result = await self._run_send_voice(
                    event, args, voice_budget, voice_limit
                )
                state["voices"] = voice_budget["used"]
                return result

            if name == "browse_phone":
                if phone_budget["used"] >= phone_limit:
                    return "（刚刷过了，先别一直盯着手机）"
                phone_budget["used"] += 1
                state["browsed"] = True
                return await self._run_browse_phone(args)

            if name == "use_phone":
                if self._hands is None:
                    return "（手机没接上，动不了）"
                if hands_budget["used"] >= hands_limit:
                    return "（这轮手机玩够久了，先聊会儿）"
                hands_budget["used"] += 1
                return await self._run_phone(args)

            if name == "make_douyin_video":
                return await self._run_make_douyin_video(event, args)

            return f"（未知工具：{name}）"

        return executor

    async def _release_voice_vram(self) -> None:
        if self.cfg.tts.model != "indextts2" or not await probe_comfyui(self.http, self.cfg.imagegen):
            return
        base = self.cfg.tts.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.cfg.tts.api_key}"} if self.cfg.tts.api_key else {}
        try:
            async with self.http.post(base + "/model/unload/sidecar:indextts2", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                result = await response.json()
            if result.get("success"):
                return
            async with self.http.get(base + "/model/loaded", headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as response:
                response.raise_for_status()
                loaded = await response.json()
            if any(model.get("id") == "sidecar:indextts2" for model in loaded["models"]):
                raise ImageGenError("语音引擎仍在使用显存，请等语音结束后再画，或使用在线通道。", retryable=False)
        except aiohttp.ClientConnectorError:
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ImageGenError("无法确认语音显存已释放，本次本机生图已停止，请稍后重试或使用在线通道。", retryable=False) from exc

    async def _run_generate_image(
        self,
        event: Dict[str, Any],
        args: Dict[str, Any],
        budget: Dict[str, Any],
        limit: int,
        reference_images: Optional[List[str]] = None,
    ) -> str:
        """画一张图，**直接发出去**，只把「已发出」这件事回给模型。

        为什么副作用放在工具里，而不是把图返回给模型让它转述：
        模型只能输出文字，图不可能经它传出去。工具存在的意义就是干活。
        返回给模型的那句话刻意写得像旁白 —— 否则它会开始描述画面，
        而对方已经能看见了，那是纯废话。

        `reference_images` 是本轮消息里的图（data URI）。模型传
        `reference_image: N`（从 1 开始）时取第 N 张当参考图，走图生图。
        """
        cfg = self.cfg.imagegen
        if not cfg.enable:
            return "（生图功能已关闭）"

        # 冷却期内直接挡掉，连请求都不发。省得把限流撞得更死。
        # 顺序放在配额之前：「服务限流了，等 20 秒」比「本轮已经画过了」
        # 更接近真实原因，模型转述给用户的话也更有用。
        wait = self._draw_cooldown_until - time.time()
        if wait > 0:
            log.info("生图处于冷却中，还剩 %.0f 秒", wait)
            return f"（生图服务刚被限流，还要等 {wait:.0f} 秒。跟对方说一声过会儿再画）"

        if budget["used"] >= limit:
            return "（本轮已经画过了，别再画 —— 用一句话收个尾就行）"

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return "（提示词是空的。请用英文写清楚要画什么，再调用一次）"

        # ---- 参考图（图生图 / 风格转移） ----
        # 模型给的是**序号**（从 1 开始），因为它看不到我们内部的 data URI。
        # 序号越界 / 本轮没图时一律退回纯文生图，**不报错** ——
        # 模型偶尔会幻觉出一个不存在的序号，为这个把整轮画图搞失败不值得。
        #
        # 两个参数对应两件事，见工具描述：reference_image 锁构图，style_reference 只借画风。
        pool = list(reference_images or [])

        def _pick(arg_name: str) -> Optional[str]:
            raw_arg = args.get(arg_name)
            if raw_arg is None or not str(raw_arg).strip():
                return None
            try:
                idx = int(str(raw_arg).strip())
            except (TypeError, ValueError):
                idx = 0
            if pool and 1 <= idx <= len(pool):
                log.info("%s：用第 %d 张（本轮共 %d 张）", arg_name, idx, len(pool))
                return pool[idx - 1]
            log.info(
                "模型要了第 %s 张 %s，但本轮只有 %d 张 —— 忽略这个参数",
                raw_arg, arg_name, len(pool),
            )
            return None

        reference = _pick("reference_image")
        style_reference = _pick("style_reference")

        # denoise / style_weight 只在对应参数真的用上时才解析，
        # 免得模型随手填个离谱的值影响纯文生图。
        denoise = 1.0
        if reference is not None:
            try:
                denoise = float(args.get("denoise") or 0.65)
            except (TypeError, ValueError):
                denoise = 0.65
            # 默认 0.65：实测这一档「换得动衣服、人也还在」。
            #
            # 2026-10-01 用**用户的真实参考图**扫过 0.45 / 0.55 / 0.65 / 0.75
            # （`tests/_out/_denoise_sweep.py`，同一张图同一句 prompt 同 seed）：
            #   0.45、0.55 → **衣服原封不动**（还是原来那件），用户直接回「还是不行」
            #   0.65       → 换上目标服装，且发色瞳色都还在 ← 甜点区
            #   0.75       → 换得更彻底，但发色开始偏、脸型变了
            #
            # 上限放到 0.8。**原来钳 0.55 是错的** —— 那条「想让角色还在就别超过
            # 0.55」的结论来自另一个实验场景（换场景），套到「换衣服」上就把整条路
            # 堵死了：衣服占画面面积大、和角色绑得紧，不改够多根本动不了。
            # 下限 0.2 留给「只微调」。
            denoise = max(0.2, min(0.8, denoise))

        # 「换场景 / 换动作」开关。填了它就不走图生图，改走「外貌 + 纯文生图」。
        #
        # 为什么必须单独开一个开关，而不是把 denoise 调高：
        # 2026-10-03 用用户的真实参考图扫过（同一张带场景插画、同 seed、
        # 同一句「换到教室跳舞」）：
        #   denoise 0.35 → 还是原来的房间、原来的姿势，教室一个都没进来
        #   denoise 0.55 → 教室进来了，但发色从银白漂成淡紫，角色开始变
        #   denoise 0.80 → 教室和动作都对了，但人**完全换了**（深紫发紫瞳）
        # **三档没有一个能同时拿到「新场景 + 新动作 + 角色还在」。**
        # 根因：图生图是从参考图的 latent 出发的，denoise < 1.0 就一定留着
        # 原图的构图骨架 —— 而"换场景换动作"要的正是把这个骨架拆掉。
        # 唯一同时做到的是「读出外貌 + 纯文生图」（同批次 D 组，银白发灰瞳全在、
        # 教室和举手的动作也都到位）。所以这里把那条路显式接出来。
        new_scene = bool(args.get("new_scene"))

        # ---- 生图通道：模型指定的 > 会话指定的 > 默认 ----
        #
        # 两级意图，优先级不同：
        #   · `args["channel"]` —— **这一张**的意图，模型按对方的话判断
        #     （「用豆包画」→ online）。更具体，所以排在最前。
        #   · `self._channel_override` —— **这个会话**的意图，用户发
        #     `/comfyui` `/doubao` 定的。粒度更粗，被上面覆盖。
        #   · 都没有 → 空串，走默认（本机优先、连不上才降级）。
        #
        # 只认 local / online 两个值：模型偶尔会填 `auto` 或拼错，
        # 那些一律当"没指定"，不能让一个笔误把整张图搞失败。
        forced_channel = ""
        tool_channel = str(args.get("channel") or "").strip().lower()
        if tool_channel in ("local", "online"):
            forced_channel = tool_channel
        else:
            forced_channel = self._channel_override.get(
                _session_key_from_event(event), ""
            )
        if forced_channel:
            log.info("本次生图通道：%s（来源：%s）", forced_channel,
                     "模型指定" if tool_channel in ("local", "online") else "会话指令")

        user_request = msg.to_text(msg.strip_at(msg.normalize(event.get("message") or event.get("raw_message")), self.onebot.self_id))
        if pool and reference is None and style_reference is None and not args.get("reference_image") and not args.get("style_reference"):
            if new_scene or re.search(r"用这张|照这张|按这张|参考图|原图|改这张|修改|换衣|换背景|换场景", user_request):
                reference = pool[0]
        has_reference = reference is not None or style_reference is not None
        if not forced_channel and has_reference and cfg.resolved_provider == "openai" and cfg.api_ref_field:
            forced_channel = "online"
        online_edit = forced_channel == "online" and has_reference
        if online_edit:
            if cfg.resolved_provider != "openai" or not cfg.api_ref_field:
                return "（在线通道未配置参考图编辑能力，请检查接口配置；本次没有改用本机重画。）"
            reference = reference or style_reference
            style_reference = None
            prompt = (
                "Use the reference image and follow the user's request. Preserve details the user did not ask to change.\n"
                f"User request: {user_request}\nImage instructions: {prompt}"
            )

        # ---- 本机底模：会话指定的 > 配置里的 ----
        #
        # 这台机器上有两个动漫底模，画风差得明显（写实向 vs 经典动漫向），
        # 所以 `/model` 可以按会话指定。
        #
        # **为什么必须在这里就定下来**：参数（尤其 IP-Adapter 的 `style_weight`）
        # 是跟底模的响应强度**绑定**的 —— 同一个 0.65，animagine 上温和可用，
        # Illustrious 系上直接冲爆成彩虹色高饱和。下面的钳制要按**这个底模**来，
        # 不能再用写死的 0.3。见 `imagegen.COMFYUI_MODEL_PRESETS`。
        effective_checkpoint = self._model_override.get(
            _session_key_from_event(event)
        ) or (cfg.comfyui_checkpoint or "").strip()
        model_cal = model_preset(effective_checkpoint)

        style_weight: Optional[float] = None
        if style_reference is not None:
            try:
                style_weight = float(args.get("style_weight") or 0.2)
            except (TypeError, ValueError):
                style_weight = 0.2
            # 上限**跟着底模走**，不再是写死的 0.3。
            #
            # 实测：同一个 `style_weight`，animagine-xl-3.1 上 0.65 温和可用，
            # 而 Illustrious 系（wai-illustrious-sdxl-v17）0.35 就出现彩虹色
            # 高饱和、五官崩坏，只有 0.2 干净 —— **差 3 倍**。
            # 所以切了底模必须重新钳制，沿用旧值会直接产出一堆废图。
            # 每个底模的标定值见 `imagegen.COMFYUI_MODEL_PRESETS`。
            style_weight = max(
                0.0, min(float(model_cal["style_weight_max"]), style_weight)
            )

        # ---- 角色设定图的纠正（必须在上面解析完之后做，否则会被覆盖）----
        # 参考图若是「同一角色多个姿势拼一张」的设定图，它是一版**排版**、不是场景。
        # 拿它做图生图，输出**还是那张设定图**（三格排版原样保留），
        # prompt 里要求的"换到商场、背景虚化"根本进不去 —— 实测如此。
        #
        # 所以这里**不听模型的**，直接把路径换成 IP-Adapter：
        # 它传角色特征、不锁构图，正是这种参考图需要的。
        # 宁可牺牲一点"像"（受底模限制权重只能到 0.3），
        # 也不能交出一张跟参考图一模一样的图 —— 那等于没画。
        sheet_note = "（本次参考图修改使用在线接口，不占用本机生图显存；不要承诺与原图完全一致。）" if online_edit else ""
        if reference is not None and not online_edit:
            try:
                _mime, _raw = parse_data_uri(reference)
                # ---- 抠底立绘：**最优先判**，因为它和图生图是互斥的 ----
                #
                # 参考图若是一张抠掉背景的立绘（透明底，或经 QQ 传输后变成的黑底），
                # 它**没有场景信息**，而图生图会连"没有背景"这件事一起保留 ——
                # 实测 denoise 0.45 的输出几乎就是原图（黑底、同姿势、同一把枪），
                # prompt 里的商场、虚化、立牌一个都没进去。
                # 高 denoise 能换来场景，但角色会被换掉。
                #
                # 正确解法是**把角色外貌写成文字标签，用纯文生图重画一张**：
                # 模型不需要"认出"参考图，只需要读懂文字，场景就完全听话。
                # 实测这条路能同时拿到角色和场景，是这种输入下唯一可用的办法。
                # 代价：角色是"按描述重新生成"的，不会像素级一致 ——
                # 这一条要在回话里跟对方讲清楚。
                if looks_like_cutout_portrait(_raw):
                    log.info(
                        "参考图判定为「抠底立绘」（无场景信息）—— 改用文字描述角色外貌 "
                        "重画一张，不按图生图处理"
                    )
                    # **外貌必须由代码读出来拼进 prompt** —— 不能指望模型自己写：
                    # 参考图是跨消息存活的，而历史里只存 `[图片]` 文字占位，
                    # 模型在「让她穿上女仆装」那一轮**看不到图**，写不出外貌。
                    # 实测（2026-10-01）：不补这一步，prompt 里只有
                    # `1girl, solo, maid outfit, ...`，画出来是个不相干的新角色 ——
                    # 用户的原话就是「没保住角色」。
                    appearance = await self._describe_reference(reference)
                    if appearance:
                        log.info("已读出参考图外貌：%s", appearance[:100])
                        prompt = f"{appearance}, {prompt}"
                    else:
                        log.warning(
                            "没能读出参考图外貌 —— prompt 里将缺少角色特征，"
                            "画出来可能不像原角色"
                        )
                    reference = None
                    denoise = 1.0
                    sheet_note = (
                        "（对方发的是**抠掉背景的角色立绘**，图里没有场景。"
                        "我已把她的外貌读成标签、按「照着这个角色的形象另画一张」处理，"
                        "场景按对方要求画。**不保证和原图像素级一致**，"
                        "回话时别承诺「完全一致」这类说法。）"
                    )
                elif looks_like_character_sheet(_raw):
                    log.info(
                        "参考图判定为「角色设定图」（多姿势排版）—— 改用 IP-Adapter "
                        "保角色、场景交给 prompt，不按图生图处理"
                    )
                    if style_reference is None:
                        style_reference = reference
                    style_weight = 0.3
                    reference = None
                    denoise = 1.0
                    sheet_note = (
                        "（对方发的是**角色设定图**：同一个角色的多个姿势拼在一张图上。"
                        "我已按「用这个角色的形象另画一张新图」处理，"
                        "所以**不保留**原图的排版。回话时**不要**说「照着原图」这类话。）"
                    )
                elif new_scene:
                    # ---- 带场景插画 + 要换场景/换动作 ----
                    #
                    # 这是图生图**结构上做不到**的那一类（见上面 new_scene 处的实测）。
                    # 处理方式和抠底立绘完全一样：参考图只用来"读角色"，不用来锁构图。
                    # 代价同样是"按描述重画、不保证像素级一致"，所以要跟对方讲清楚。
                    log.info(
                        "带场景插画 + 对方要换场景/换动作 —— 图生图做不到"
                        "（低 denoise 场景不动、高 denoise 角色漂），"
                        "改用「读出外貌 + 纯文生图」"
                    )
                    appearance = await self._describe_reference(reference)
                    if appearance:
                        log.info("已读出参考图外貌：%s", appearance[:100])
                        prompt = f"{appearance}, {prompt}"
                    else:
                        log.warning(
                            "没能读出参考图外貌 —— prompt 里将缺少角色特征，"
                            "画出来可能不像原角色"
                        )
                    reference = None
                    denoise = 1.0
                    sheet_note = (
                        "（对方要**换场景或换动作**，而参考图是一张带场景的插画 —— "
                        "这种情况下图生图只能改细节、改不动场景。"
                        "我已把角色的外貌读成标签、按「照着这个角色的形象另画一张」处理，"
                        "新场景和新动作按对方要求画。**不保证和原图像素级一致**，"
                        "回话时别承诺「完全一致」这类说法。）"
                    )
            except ImageGenError as exc:
                log.warning("参考图类型判定失败（忽略）：%s", exc)

        # ---- 兜底：模型没填 reference_image，但暂存里躺着一张抠底立绘 ----
        #
        # 模型可能因为 hints 措辞、或自己「觉得」不该用参考图而跳过这个参数
        # （2026-10-01 实测踩过：hints 写「别填」→ 模型照做 → `reference` 为 None
        # → 上面 `if reference is not None:` 整块不执行 → 补外貌的逻辑白写）。
        # 但那张图仍然要处理，否则画出来就是个不相干的新角色。
        if (
            reference is None
            and style_reference is None
            and reference_images
            # 主路径已经处理过（抠底立绘 / 设定图都会写 sheet_note）就别再来一遍 ——
            # 主路径处理完会把 reference 置 None，兜底若不排除它就会**重复补一次外貌**，
            # 同一个标签在 prompt 里出现两次、被重复加权（实测踩过）。
            and not sheet_note
            and self.cfg.imagegen.enable
        ):
            try:
                _m2, _raw2 = parse_data_uri(reference_images[0])
            except Exception:  # noqa: BLE001
                _raw2 = b""
            if _raw2 and looks_like_cutout_portrait(_raw2):
                log.info(
                    "模型没填 reference_image，但暂存里是抠底立绘 —— 补读外貌后重画"
                )
                appearance = await self._describe_reference(reference_images[0])
                if appearance:
                    log.info("已读出参考图外貌：%s", appearance[:100])
                    prompt = f"{appearance}, {prompt}"
                else:
                    log.warning("没能读出参考图外貌 —— prompt 里将缺少角色特征")
                denoise = 1.0
                sheet_note = (
                    "（对方发的是**抠掉背景的角色立绘**，图里没有场景。"
                    "我已把她的外貌读成标签、按「照着这个角色的形象另画一张」处理，"
                    "场景按对方要求画。**不保证和原图像素级一致**，"
                    "回话时别承诺「完全一致」这类说法。）"
                )

        # ---- 在线通道的两处纠正 ----
        #
        # 为什么必须由代码兜住、不能指望模型：实测（2026-10-01 群聊日志）
        # 参考图明明在池子里、也读出来了（「参考图 1 张，来源=会话暂存」），
        # 但豆包那次请求却是 `参考图=无`。原因只有两种，都出现过：
        #
        #   ① 模型填了 `style_reference`（那是**本机 IP-Adapter 风格转移**的参数），
        #      而在线通道只认 `reference_image` —— 参数名不对，图就被丢了。
        #      日志实录：
        #        模型请求工具：generate_image({..., 'style_reference': 1})
        #        生图请求（通用通道）：… 参考图=无
        #   ② 模型干脆两个都没填，prompt 里写「Use the reference sheet, keep the
        #      exact same character」，但**没有把图传出去** —— 它以为提一句就够了。
        #
        # 两种情况的结果一样：用户看到「没按参考图画」。
        # 所以在**在线通道**上，只要池子里有图而最终没有 reference，就补上。
        # 本机通道不做这个兜底：那边 style_reference 是真有意义的参数（IP-Adapter），
        # 强行改写成 reference_image 会改变它的行为。
        online_channel = (
            self.cfg.imagegen.resolved_provider == "openai"
            and not self.cfg.imagegen.comfyui_enable
        )
        if online_channel and reference is None and reference_images:
            first = reference_images[0]
            if style_reference is not None:
                log.info(
                    "在线通道不支持 style_reference（那是本机 IP-Adapter 的参数）"
                    "—— 改写为 reference_image，否则参考图会被丢掉"
                )
                style_reference = None
            reference = first
            if denoise >= 1.0:
                # 模型没给 denoise（或给了 1.0）时才补默认值，别覆盖它自己的选择
                denoise = 0.65
            log.info(
                "在线通道补上参考图（模型没正确传）：%d 字节的 data URI",
                len(first),
            )

        # ---- 「画她自己」：固定外貌由**代码**补上，不靠模型自觉 ----
        #
        # 位置放在这里（参考图那几段之后）是有意的：上面几段都可能改写 prompt
        # （抠底立绘那条会把读出来的外貌拼在最前面）。画自己时群友手上没图、
        # 那些分支都走不到，但万一模型同时填了两个参数，只有在这里最后拼上去
        # 才能保证角色特征真的落在 prompt 最前面。
        if args.get("draw_self"):
            log.info("本轮是「画桃桃本人」—— 注入固定外貌：%s", SELF_CHARACTER_TAGS)
            prompt = _inject_self_character(prompt)

        # 配额在**动手之前**就扣掉。生图是重操作，失败也消耗服务端额度 ——
        # 让模型换个说法反复重试只会把限流撞得更死。
        budget["used"] += 1
        gpu_acquired = False
        try:
            if forced_channel != "online" and (cfg.comfyui_enable or forced_channel == "local"):
                await self._gpu_lock.acquire()
                gpu_acquired = True
                self._check_event(event)
                await self._release_voice_vram()
                self._check_event(event)
            mime, data = await generate_prepared(
                self.http, cfg, prompt, args.get("size"),
                reference_image=reference, denoise=denoise,
                style_reference=style_reference, style_weight=style_weight,
                force_channel=forced_channel or None,
                force_checkpoint=effective_checkpoint or None,
            )
        except asyncio.CancelledError:
            raise
        except ImageGenError as exc:
            if exc.cooldown:
                self._draw_cooldown_until = time.time() + DRAW_COOLDOWN
            log.warning("生图失败：%s", exc)
            return f"（画不出来：{exc}。跟对方说一声，别再试了）"
        except Exception as exc:  # noqa: BLE001
            log.exception("生图时发生未预期异常")
            return f"（画图时出错了：{exc}）"
        finally:
            if gpu_acquired:
                self._gpu_lock.release()

        image_b64 = base64.b64encode(data).decode("ascii")
        if not await self._send_image(event, image_b64):
            return "（图画好了，但发送失败。跟对方说一声，别再试了）"

        # 只有**真的发出去了**才计数。失败不能记 —— 否则历史会告诉模型
        # 「上轮画成功了」，而它其实什么都没拿到。
        budget["sent"] = budget.get("sent", 0) + 1
        budget.setdefault("prompts", []).append(prompt)

        log.info("生图已发出：%s %d 字节 prompt=%r", mime, len(data), prompt[:60])
        # 通道说明：只在**降级**时非空（在线额度用完退到本机、或本机没起来退到在线）。
        # 必须让模型知道并转述一句 —— 否则画风突变时用户会以为功能坏了。
        # 实测就是这么被反馈的：「我豆包通道怎么关了」。
        channel_note = last_channel_note()
        if channel_note:
            log.info("本次生图走了降级通道，已提示模型向用户说明")
        return (
            "（图已经发出去了，对方能看见。你只需要补一句很短的话，"
            "**不要**描述画面里有什么，也别说什么「这是我画的」之类客套 —— 自然点）"
            + sheet_note
            + channel_note
        )

    async def _send_image(self, event: Dict[str, Any], image_b64: str) -> bool:
        """把图片直接发给当前会话。返回是否发成功。"""
        self._check_event(event)
        group_id = event.get("group_id")
        user_id = event.get("user_id")
        message_id = event.get("message_id")
        quote = self._should_quote(group_id, message_id)

        segments = msg.build_image_message(
            image_b64,
            reply_id=int(message_id) if (quote and message_id is not None) else None,
        )
        try:
            if group_id is not None:
                await self.onebot.send_group_msg(int(group_id), segments)
            elif user_id is not None:
                await self.onebot.send_private_msg(int(user_id), segments)
            else:
                log.warning("事件里既没有 group_id 也没有 user_id，图片发不出去")
                return False
        except OneBotError as exc:
            log.error("发送图片失败：%s", exc)
            return False
        # 图片也是「一次回复」，同样打点 —— 否则出完图紧接着的连发消息
        # 不受硬间隔约束（而图片恰恰是最容易招来连发消息的）。
        self._last_reply_sent_at[_session_key_from_event(event)] = time.time()
        # 发图同样是「一次回复」，窗口一样续（和 _send_reply 保持一致）。
        self._renew_awake_after_reply(event)
        return True

    # ------------------------------------------------------------------ #
    # 语音合成

    def _voice_file(self, audio: bytes) -> Path:
        """把合成结果落盘，返回路径。

        为什么要落盘而不是走 base64：`file` 路径在日志里是**明文**，
        出问题时一眼能看出文件在不在、多大。base64 那串在日志里没法读。
        """
        fmt = (self.cfg.tts.format or "wav").lower()
        digest = hashlib.sha1(audio).hexdigest()[:16]
        folder = Path(self.cfg.tts.cache_dir or ".") / "tts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.{fmt}"
        if not path.is_file():
            path.write_bytes(audio)
        return path

    async def _speak(self, event: Dict[str, Any], text: str) -> Tuple[bool, str]:
        chunks = split_for_speech(text)
        if not chunks:
            return False, "没有可朗读的内容。"
        if len(chunks) > 4:
            return False, "内容太长，最多发送 4 段语音，每段最多 180 字；请缩短后重试。"
        for index, chunk in enumerate(chunks):
            self._check_event(event)
            ok, note = await self._speak_segment(event, chunk)
            if not ok:
                return False, (f"前 {index} 段已发送，第 {index + 1} 段失败：" if index else "") + note
        return True, ""

    async def _speak_segment(self, event: Dict[str, Any], text: str) -> Tuple[bool, str]:
        """合成 + 发送。返回 `(是否成功, 说明)`。

        失败原因分成两类，给用户的话完全不同：
        * 连不上 → 「应用没开」，是**操作问题**，用户自己能解决
        * 合成被拒 → 「音色/引擎不对」，是**配置问题**，得改配置
        混成一句「语音失败了」等于什么都没说。
        """
        try:
            async with self._gpu_lock:
                self._check_event(event)
                audio = await synthesize(self.http, self.cfg.tts, text)
        except VoiceUnavailable as exc:
            log.warning("语音后端连不上：%s", exc)
            return False, "语音后端没连上 —— 桌面上的 VoiceStudio 得开着才行。"
        except VoiceError as exc:
            log.error("语音合成失败：%s", exc)
            return False, f"语音合成失败了：{exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 音频库的异常五花八门，别让整条消息挂掉
            log.exception("语音合成出现意外错误")
            return False, f"语音合成出错了（{type(exc).__name__}）。"

        # 末尾补静音，防止 QQ 端把最后一个字吃掉（见 tts.pad_wav_tail）。
        # 补静音失败绝不能挡住发送 —— 有声音总比没有强。
        try:
            pad_ms = int(getattr(self.cfg.tts, "tail_pad_ms", 350) or 0)
            audio = pad_wav_tail(audio, pad_ms)
        except Exception:  # noqa: BLE001
            log.warning("补尾部静音失败，按原样发送", exc_info=True)

        if await self._send_voice(event, audio):
            return True, ""
        return False, "语音合成好了，但发不出去（OneBot 那边拒了）。"
    async def _send_voice(self, event: Dict[str, Any], audio: bytes) -> bool:
        """把音频发到当前会话。返回是否成功。

        刻意**不引用**对方消息 —— 语音条底下挂一条被引用的文字很怪。

        发送方式**依次试**（见 `tts.send_modes`）：`file://` URI / 裸路径 /
        base64，三种写法在不同 NapCat 版本上各有翻车记录，赌一条不如全试。
        每次尝试就是一次本机 WS 调用，几毫秒，失败了才走下一个。
        """
        self._check_event(event)
        group_id = event.get("group_id")
        user_id = event.get("user_id")
        if group_id is None and user_id is None:
            log.warning("事件里既没有 group_id 也没有 user_id，语音发不出去")
            return False

        try:
            path = self._voice_file(audio)
        except OSError as exc:
            log.error("语音落盘失败：%s", exc)
            return False

        for attempt in send_modes(self.cfg.tts.send_mode):
            self._check_event(event)
            try:
                segments = [build_voice_segment(path, attempt)]
                if group_id is not None:
                    await self.onebot.send_group_msg(int(group_id), segments)
                else:
                    await self.onebot.send_private_msg(int(user_id), segments)
            except OneBotError as exc:
                log.warning("语音以 %s 方式发送失败：%s", attempt, exc)
                continue
            # 语音也是「一次回复」—— 同样打点和续窗口，
            # 否则发完语音紧接着的连发消息不受硬间隔约束。
            self._last_reply_sent_at[_session_key_from_event(event)] = time.time()
            self._renew_awake_after_reply(event)
            return True
        return False

    async def _run_send_voice(
        self,
        event: Dict[str, Any],
        args: Dict[str, Any],
        budget: Dict[str, Any],
        limit: int,
    ) -> str:
        """工具入口：合成一段语音**直接发出去**，只把「已发出」回给模型。

        和生图/表情包同一个道理：音频不可能经模型的手传出去。
        返回的旁白措辞也一致 —— 不这么写它会开始复述刚念过的内容。
        """
        if not self.cfg.tts.enabled:
            return "（语音功能没开，别用这个工具）"
        if budget["used"] >= limit:
            return "（这轮已经发过或尝试过语音了，别再发）"

        text = str(args.get("text") or "").strip()
        if not text:
            return "（没给要念的内容）"

        budget["used"] += 1
        ok, note = await self._speak(event, text)
        if not ok:
            return f"（语音没发出去：{note} 用文字说）"

        log.info("语音已发出：%d 字", len(text))
        return (
            "（语音已经发出去了，对方能听见。**别再复述一遍内容**，"
            "也别补「给你发了条语音」这种废话 —— 要接就接一句很短的话，"
            "不接也行）"
        )

    # ------------------------------------------------------------------ #
    # 手机

    async def _run_browse_phone(self, args: Dict[str, Any]) -> str:
        """刷一次手机，把结果交给模型。

        注意这里**不发任何东西** —— 手机是她自己的事，刷到什么由她自己决定
        要不要说。和生图/语音那种「执行即发送」的工具完全不同，
        所以返回的是内容本身，不是「已发出」的旁白。
        """
        if not self.cfg.phone.enable:
            return "（手机没带，别用这个工具）"

        keyword = str(args.get("keyword") or "").strip()
        cfg = self.cfg.phone

        items = await phone_grab(self.http, cfg, keyword=keyword, limit=cfg.limit)
        if not items:
            return "（手机刷不出来东西，像断网了。别提这事，聊别的）"

        # 第三道闸。grab 里已经过了黑名单和打分，这里再让模型判一次语义。
        # 失败时 llm_filter 会自己退化成「只放行兴趣项」，不会全放行。
        items = await phone_llm_filter(self.llm, items, cfg)
        if not items:
            return "（刷了一圈，没什么能说的。别硬编，聊别的）"

        # 到这里才截断。grab 给的是两三倍候选，就是要留给模型挑的。
        items = items[: max(1, int(cfg.limit))]

        self._phone_browsed_at = time.time()
        fresh = self._phone_diary.add(items)
        # 全是刷过的（同一个热搜挂了一天）—— 告诉她，免得她当成新闻播报。
        repeat_note = "" if fresh else "（都是之前刷到过的，别当新消息说）\n"

        lines = [f"{i}. {it.line()}" for i, it in enumerate(items, 1)]
        log.info("手机：刷到 %d 条（%d 条新的）", len(items), len(fresh))
        return (
            f"{repeat_note}（你刚刷了会儿手机，看到这些：\n"
            + "\n".join(lines)
            + "\n挑一条顺嘴提一下就行，不用全念 —— 没意思就干脆别说，"
            "接着聊刚才的事也行）"
        )

    # ------------------------------------------------------------------ #
    # 手（真机）

    async def _run_phone(self, args: Dict[str, Any]) -> str:
        """在真机上做一个动作。

        设计上刻意「一个动作一次调用」，不让模型一次编排十步 ——
        因为每一步之后屏幕都可能变了，隔空编排等于闭着眼走路。
        """
        hands = self._hands
        if hands is None:
            return "（手机没接上，动不了）"

        # 三方共用一台手机：自动发布 / 自动刷抖音正在用时，聊天里让她
        # 玩手机的请求先让路（否则 reset_task 互相清步数、点错屏幕）。
        sched = self._douyin_scheduler
        if self._browse_busy or (sched is not None and getattr(sched, "_busy", False)):
            return "（我这会儿在用手机，等会儿再看）"

        action = str(args.get("action") or "").strip().lower()
        target = str(args.get("target") or "").strip()

        try:
            await hands.ensure_ready()
        except HandsError as exc:
            return f"（{exc}）"

        try:
            if action == "open":
                hands.reset_task()
                return await hands.open_app(target)

            if action == "look":
                return await hands.look()

            if action == "scroll":
                hands.reset_task()
                count = max(1, min(8, int(args.get("count") or 3)))
                lines: List[str] = []
                for index in range(count):
                    await hands.swipe("up")
                    # 等信息流把下一条加载出来。不等的话 dump 到的还是上一条，
                    # 她会以为「刷了但没变」，然后重复刷。
                    await asyncio.sleep(1.2)
                    text, nodes = await hands.observe()
                    if text.startswith("__ABORT__"):
                        lines.append(f"第 {index + 1} 屏：{text}")
                        return "\n".join(lines) + "\n（已经停手，别再试了）"
                    # 信息流页面优先给结构化卡片 —— 一坨逗号串没法用。
                    # 抖音和 B 站的节点格式完全不同：抖音没有标题、只有一句
                    # 文案，B 站那套正则对它永远返回空。所以按前台包名选解析器 ——
                    # `parse_douyin_feed` 早就写好了，之前一直没人调用，
                    # 结果她在抖音里刷屏只能拿到一屏原始 label。
                    pkg = await hands.current_package()
                    if "aweme" in pkg or "snssdk" in pkg:
                        texts = parse_douyin_feed(nodes)
                        if texts:
                            lines.append(f"第 {index + 1} 屏：")
                            lines.extend(f"  · {line}" for line in texts[:5])
                        else:
                            lines.append(f"第 {index + 1} 屏：\n{text}")
                    else:
                        cards = parse_feed(nodes)
                        if cards:
                            lines.append(f"第 {index + 1} 屏：")
                            lines.extend(f"  · {card.line()}" for card in cards[:5])
                        else:
                            lines.append(f"第 {index + 1} 屏：\n{text}")
                return "\n".join(lines)

            if action == "tap":
                hands.reset_task()
                note = await hands.tap_text(target)
                await asyncio.sleep(1.0)
                return f"{note}\n{await hands.look()}"

            if action == "type":
                hands.reset_task()
                # ⚠️ 中文走 ADB_INPUT_B64 广播，但**广播只在 ADBKeyBoard 是
                # 当前输入法时才生效**。手机默认输入法是百度，不切过去的话
                # 广播会「成功」但一个字都进不去 —— 看起来像成功，最坑。
                # （2026-10-24：手机侧已装 com.android.adbkeyboard/.AdbIME）
                await hands.use_adb_keyboard(True)
                await asyncio.sleep(0.4)
                note = await hands.input_text(target)
                await asyncio.sleep(0.6)
                return f"{note}\n{await hands.look()}"

            if action == "back":
                hands.reset_task()
                note = await hands.back()
                await asyncio.sleep(0.8)
                return f"{note}\n{await hands.look()}"

            if action == "home":
                hands.reset_task()
                # 收工把输入法切回人用的那个。ADBKeyBoard 没有界面，
                # 留着当默认输入法的话，用户自己打字时键盘弹不出来。
                try:
                    await hands.use_adb_keyboard(False)
                except Exception:  # noqa: BLE001
                    pass
                return await hands.home()

            return f"（不认识的动作：{action}）"
        except HandsError as exc:
            return f"（{exc}）"
        except Exception as exc:  # noqa: BLE001
            log.warning("真机操作失败：%s", exc)
            return f"（手机上出错了：{exc}）"

    # ------------------------------------------------------------------ #
    # 独处心跳
    #
    # 这是「玩手机」和「搜索框」真正的分界线：
    # 没有这段，手机只是个需要人叫才动的工具；有了这段，她才**有自己的时间**。
    #
    # 但这也是整个功能里最容易翻车的地方 —— 一个 QQ 机器人主动说话，
    # 稍微松一点就变成刷屏号。所以下面每一道闸都是**必要条件**，
    # 少一道就闭嘴。宁可一天不说，也不能说错一次。

    async def phone_loop(self) -> None:
        """后台循环：每隔一段时间看看她要不要自己刷会儿手机。"""
        cfg = self.cfg.phone
        if not cfg.enable:
            log.info("手机：未启用，跳过独处心跳")
            return

        interval = max(60.0, float(cfg.interval_minutes) * 60.0)
        log.info(
            "手机：独处心跳已启动，每 %.0f 分钟一次；主动发言群 %s，每日上限 %d 条",
            cfg.interval_minutes,
            cfg.proactive_groups or "（未配置，不会主动说话）",
            cfg.proactive_daily_limit,
        )
        while True:
            try:
                await asyncio.sleep(interval)
                await self._phone_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 手机挂了不能带崩机器人。这里兜住，下一轮继续。
                log.warning("手机：心跳异常（%s），下一轮重试", exc)

    def _ensure_douyin_scheduler(self):
        """拿（或建）共享的抖音调度器。

        **必须共享同一个实例**：定时任务和她手动要做，走的是同一个
        `DouyinScheduler` —— 才能共享 `_busy` 锁和当天配额。
        各建一个的话，定时发完一条，她再说「想发」还能再发一条，
        daily_limit 形同虚设。
        """
        if self._douyin_scheduler is not None:
            return self._douyin_scheduler

        from .douyin_scheduler import DouyinScheduler
        from .inspiration import InspirationStore

        try:
            store = InspirationStore(self.cfg.data_dir / "inspirations.json")
        except Exception as exc:  # noqa: BLE001
            # 灵感库挂了不该拖垮整条链路 —— 没有模板她照样能发，
            # 只是内容结构会少点变化。
            log.warning("抖音：灵感库加载失败（%s），这轮不套结构", exc)
            store = None

        self._douyin_scheduler = DouyinScheduler(
            self.cfg, self._hands, self.llm, store
        )
        return self._douyin_scheduler

    async def douyin_loop(self) -> None:
        """后台循环：偶尔自己发一条抖音。

        和 `phone_loop` 是两件不同的事：
        - `phone_loop` 是「看网上的内容，然后回群里说两句」
        - `douyin_loop` 是「做一条自己的视频，发到公开账号上」

        后者不可逆、无人审核，所以护栏全在 `DouyinScheduler` 里：
        一天一条、只在白天、失败即停。这里只负责定时叫醒它。

        **启动后不会立刻发** —— 先睡一个 interval。刚开机就发一条
        「今天想你了」很怪，而且重启一次就发一条的话，调试期间会刷屏。
        """
        cfg = self.cfg.douyin
        if not cfg.enable:
            log.info("抖音：未启用，跳过自主发布")
            return

        scheduler = self._ensure_douyin_scheduler()
        # 下限 10 分钟。配置写成 0 会让它疯狂空转，每轮都要探测一次 ComfyUI。
        interval = max(600.0, float(cfg.interval_minutes) * 60.0)
        log.info(
            "抖音：自主发布已启动，每 %.0f 分钟检查一次；每天最多 %d 条，时段 %d-%d 点",
            cfg.interval_minutes,
            cfg.daily_limit,
            cfg.hours[0] if cfg.hours else 9,
            cfg.hours[1] if len(cfg.hours) > 1 else 23,
        )

        while True:
            try:
                await asyncio.sleep(interval)
                # 刷抖音正在用手机时等她用完 —— 最多等 3 分钟，等不到就跳过
                # 这一轮（下一小时还有机会；发布一天一条，不急这几十分钟）。
                for _ in range(12):
                    if not self._browse_busy:
                        break
                    await asyncio.sleep(15.0)
                if self._browse_busy:
                    log.info("抖音：刷抖音还在用手机，这轮发布跳过")
                    continue
                msg = await scheduler.tick()
                log.info("抖音：%s", msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 抖音挂了不能带崩机器人。兜住，下一轮继续。
                log.warning("抖音：心跳异常（%s），下一轮重试", exc)

    async def _run_make_douyin_video(
        self, event: Dict[str, Any], args: Dict[str, Any]
    ) -> str:
        """她说想做一条视频。

        **立刻返回**，实际制作在后台跑 —— 做一条要三分钟，
        远超单轮 LLM 的 120 秒超时。同步等的话这一轮对话直接废掉。
        """
        if self._hands is None:
            return "（手机没接上，做不了）"

        cfg = self.cfg.douyin
        if not cfg.enable:
            return "（抖音自主发布是关的）"

        scheduler = self._ensure_douyin_scheduler()
        # 先做便宜的闸门检查 —— 别让她说完「我去做」等三分钟，
        # 才发现今天早就发过了。
        if scheduler._busy:
            return "（上一条还在做，等这条做完再说）"
        if self._browse_busy:
            return "（我正刷着手机呢，等我刷完再做）"
        scheduler._roll_day()
        reason = scheduler._blocked_reason(check_hours=False)
        if reason:
            return f"（{reason}）"

        idea = str(args.get("idea") or "").strip()
        self._spawn_douyin_video(event, idea, scheduler)
        return "（好，我去做，大概三分钟。做好了再跟你说）"

    def _spawn_douyin_video(
        self, event: Dict[str, Any], idea: str, scheduler: Any
    ) -> None:
        """后台做视频并发出去，做完回到原会话说一声。

        照 `_spawn_sticker_draw` 的模式：慢操作全部丢后台，工具立刻返回。
        区别是视频要三分钟而不是一分钟，所以更不能同步等。

        错误绝不外溢 —— 后台任务抛异常会变成 asyncio 未处理异常日志，
        污染主流程的排错视线。这里全兜住，最多是「没做成」。
        """

        async def _run() -> None:
            try:
                msg = await scheduler.run_once(idea)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("后台做抖音视频失败")
                msg = "（做视频的时候出岔子了，没发成）"
            await self._send_async_note(event, msg)

        try:
            asyncio.get_running_loop().create_task(_run(), name="douyin-video")
        except RuntimeError:  # 没有事件循环（理论上不会）
            log.warning("没有事件循环，跳过后台做视频")

    async def _send_async_note(self, event: Dict[str, Any], text: str) -> None:
        """后台任务做完后，回到原会话说一句。

        不走 `_send_reply`：那条路会续激活窗口、算回复配额、还可能拆成多条。
        这里只是一句迟到的补充说明，不该有那些副作用。
        """
        self._check_event(event)
        text = _tidy(text or "")
        if not text:
            return
        group_id = event.get("group_id")
        user_id = event.get("user_id")
        segments = [{"type": "text", "data": {"text": text}}]
        try:
            if group_id is not None:
                await self.onebot.send_group_msg(int(group_id), segments)
            elif user_id is not None:
                await self.onebot.send_private_msg(int(user_id), segments)
            else:
                log.warning("后台消息发不出去：事件里没有 group_id / user_id")
        except Exception as exc:  # noqa: BLE001
            log.warning("后台消息没发出去：%s", exc)

    def _has_live_conversation(self) -> bool:
        """有没有哪个群正在跟她聊。

        在聊的时候她该接话，不是低头玩手机 —— 这是人设问题，不是技术问题。
        """
        now = time.time()
        for gid in list(self._group_awake):
            if self._group_awake.get(gid, 0.0) > now:
                return True
            # 顺手清过期条目，否则跑几天会攒一堆死群号。
            self._group_awake.pop(gid, None)
        return False

    def _phone_quota(self) -> int:
        """今天还剩几次主动开口的额度。按自然日跨天重置。"""
        today = time.strftime("%Y-%m-%d")
        if today != self._phone_said_day:
            self._phone_said_day = today
            self._phone_said_count = 0
        return max(0, int(self.cfg.phone.proactive_daily_limit) - self._phone_said_count)

    def _in_speak_hours(self) -> bool:
        start, end = self.cfg.phone.proactive_hours
        return start <= time.localtime().tm_hour < end

    def _pick_speak_group(self) -> Optional[int]:
        """从白名单里挑一个「刚有人聊过、现在安静了」的群。

        挑最近活跃的那个 —— 刚聊完冷下来的群，她插一句自己的事最自然；
        冷了一整天的群突然冒话，像僵尸号复活。
        """
        cfg = self.cfg.phone
        if not cfg.proactive_groups:
            return None

        now = time.time()
        recent = max(0.0, float(cfg.proactive_recent_minutes) * 60.0)
        best: Optional[int] = None
        best_at = 0.0

        for gid in cfg.proactive_groups:
            # 这个群正在跟她对话 → 不插嘴，等她该说话的时候自然会说。
            if self._group_awake.get(gid, 0.0) > now:
                continue
            last = self._group_active_at.get(gid, 0.0)
            if last <= 0:
                # 从没收到过这个群的消息。可能是群号写错了，也可能是
                # 机器人刚启动还没见过它说话。两种都不该贸然开口。
                continue
            if cfg.proactive_need_recent and now - last > recent:
                continue
            if last > best_at:
                best, best_at = gid, last

        return best

    async def _phone_tick(self) -> None:
        """一次独处心跳：该刷就刷，该说就说。"""
        cfg = self.cfg.phone
        if not cfg.enable:
            return

        if self._has_live_conversation():
            log.debug("手机：有群正在对话，这轮不刷")
            return

        items = await phone_grab(self.http, cfg, limit=cfg.limit)
        if not items:
            return

        items = await phone_llm_filter(self.llm, items, cfg)
        if not items:
            return

        items = items[: max(1, int(cfg.limit))]

        self._phone_browsed_at = time.time()
        fresh = self._phone_diary.add(items)
        if not fresh:
            # 刷到的全是旧的（同一个热搜能挂一整天）。不当新闻播报。
            log.debug("手机：这轮没有新东西")
            return

        log.info("手机：独处时刷到 %d 条新东西", len(fresh))

        # ---- 要不要开口：闸门统一在 _speak_about_fresh 里 ----
        pick = self._speak_about_fresh()
        if pick is not None:
            group_id, entry = pick
            await self._speak_up(group_id, entry)

    def _speak_about_fresh(self) -> Optional[Tuple[int, Any]]:
        """把「她刚刷到的东西」变成一次主动开口。

        返回 (群号, 日记条目)；任何一道闸门不满足都返回 None。
        手机心跳和抖音心跳**共用这一套** —— 两处各写一遍的话，
        哪天改了一边的额度或间隔，另一边就会悄悄对不上。
        """
        cfg = self.cfg.phone
        if self._phone_quota() <= 0:
            log.debug("手机：今天额度用完了")
            return None
        if not self._in_speak_hours():
            log.debug("手机：不在允许开口的时段")
            return None

        min_gap = max(0.0, float(cfg.proactive_min_gap_minutes) * 60.0)
        if time.time() - self._phone_said_at < min_gap:
            log.debug("手机：距上次主动开口不足 %.0f 分钟", cfg.proactive_min_gap_minutes)
            return None

        group_id = self._pick_speak_group()
        if group_id is None:
            log.debug("手机：没有合适的群（白名单空 / 都太冷 / 都在聊）")
            return None

        # 只挑「刚刷到的」—— 窗口外的老条目不该被翻出来说。
        window = max(0.0, float(cfg.proactive_window_minutes) * 60.0)
        unsaid = self._phone_diary.unsaid(window)
        if not unsaid:
            return None

        return group_id, random.choice(unsaid)

    async def _speak_up(self, group_id: int, entry: Any) -> bool:
        """在指定群里，主动说一句她刷到的东西。

        和 `_send_reply` 最大的不同：**不续激活窗口**。
        她只是随口一提，不是开始一段对话 —— 续了窗口的话，
        接下来两分钟群里每句话都会被当成「在跟她聊」，那才是真的烦。
        """
        system = (
            self.cfg.bot.system_prompt
            + "\n\n# 此刻的情况\n"
            "群里安静了一会儿。你刚刷手机看到一条东西，想顺嘴提一句。\n"
            "只说**一句话**，20 字以内。不要念标题原文，用你自己的话说。\n"
            "不要用「大家」「各位」「你们知道吗」开头 —— 你不是在播报。\n"
            "直接输出那句话，不要解释，不要加引号。"
        )
        try:
            text = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"你刷到：{entry.title}"},
                ],
                tools=None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("手机：主动发言生成失败（%s）", exc)
            return False

        text = _tidy(text)
        if not text:
            return False
        # 她不听话说长了就截断 —— 主动发言突然来一段长文，比不说话更假。
        if len(text) > 60:
            text = text[:60]

        try:
            await self.onebot.send_group_msg(
                group_id, msg.build_text_message(text)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("手机：主动发言发送失败（%s）", exc)
            return False

        self._phone_said_at = time.time()
        self._phone_said_count += 1
        self._phone_diary.mark_said_key(entry.key)
        log.info(
            "手机：在群 %s 主动开口（今日第 %d 条）：%s",
            group_id,
            self._phone_said_count,
            text,
        )
        return True

    # ------------------------------------------------------------------ #
    # 抖音：自主刷（她自己的推荐流）
    #
    # 和「手机」心跳的区别：手机刷的是网页接口（微博/贴吧/B站），
    # 这个是真的在模拟器里滑抖音 —— 她自己的账号、自己的推荐流。
    # 值得做的理由：推荐流会因为她看过什么而变化，这是「她有自己的生活」
    # 里最像真的那一块。
    #
    # 风险：抖音一屏一个视频，误触就是点赞/关注/进主页。所以全程
    # **只滑不点**；连观察都靠 OCR，不靠点。唯一会发生的点击是
    # `observe()` 在 dump 失败时点屏幕中心暂停视频（单击=暂停，安全）。

    async def douyin_browse_loop(self) -> None:
        """后台循环：每隔一段时间让她自己刷会儿抖音。"""
        cfg = self.cfg.hands
        if not (cfg.enable and cfg.douyin_browse):
            log.info("抖音：自主刷未启用，跳过")
            return

        interval = max(600.0, float(cfg.browse_interval_minutes) * 60.0)
        log.info(
            "抖音：自主刷心跳已启动，每 %.0f 分钟一次；时段 %s，每天最多 %d 轮",
            cfg.browse_interval_minutes,
            cfg.browse_hours,
            cfg.browse_daily_limit,
        )
        while True:
            try:
                await asyncio.sleep(interval)
                await self._douyin_browse_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 和 phone_loop 同一个原则：刷抖音挂了不能带崩机器人。
                log.warning("抖音：自主刷异常（%s），下一轮重试", exc)

    async def _douyin_browse_tick(self) -> None:
        """对外入口：带「正在用手机」互斥标记的浏览 tick。

        `_browse_busy` 是三方向（浏览 / 定时发布 / 聊天里的 use_phone）
        共用同一台手机时的互斥标记。置位和清除之间不能有 await，
        否则两个协程可能同时看到「没人在用」。
        """
        if self._browse_busy:
            return
        self._browse_busy = True
        try:
            await self._douyin_browse_tick_impl()
        finally:
            self._browse_busy = False

    async def _douyin_browse_tick_impl(self) -> None:
        """一次抖音独处心跳：能刷就刷，刷到了就按闸门决定要不要说。"""
        cfg = self.cfg.hands
        hands = self._hands
        if hands is None:
            return

        # 发布和她刷抖音共用同一台手机：发布流程正忙时**绝不碰屏幕** ——
        # 一边在滑推荐流、一边在点发布页，两边都会点错。定时循环的周期
        # 都是 60 分钟，两条会在同一分钟醒来，这个判断是必须的。
        sched = self._douyin_scheduler
        if sched is not None and getattr(sched, "_busy", False):
            log.debug("抖音：发布流程正忙，这轮不刷")
            return

        if self._has_live_conversation():
            log.debug("抖音：有群正在对话，这轮不刷")
            return

        today = time.strftime("%Y-%m-%d")
        if today != self._browse_day:
            self._browse_day = today
            self._browse_rounds = 0
        if self._browse_rounds >= max(1, int(cfg.browse_daily_limit)):
            log.debug("抖音：今天刷够了（%d 轮）", self._browse_rounds)
            return

        start, end = cfg.browse_hours
        if not (start <= time.localtime().tm_hour < end):
            log.debug("抖音：不在允许刷的时段（%d-%d 点）", start, end)
            return

        try:
            await hands.ensure_ready()
        except HandsError as exc:
            if not cfg.launch_emulator:
                log.debug("抖音：模拟器没在线（%s），跳过", exc)
                return
            log.info("抖音：模拟器不在线，自己拉起来")
            if not await self._launch_emulator(cfg):
                return
            try:
                await hands.ensure_ready()
            except HandsError as exc2:
                log.warning("抖音：模拟器拉起来了但没连上（%s）", exc2)
                return

        hands.reset_task()
        try:
            note = await hands.open_app("aweme")
            log.info("抖音：%s", note)
            await asyncio.sleep(float(cfg.launch_delay))
            screens_done, lines = await self._browse_douyin_screens(
                hands, int(cfg.browse_screens)
            )
        except HandsError as exc:
            log.warning("抖音：自主刷到一半断了（%s）", exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音：自主刷出错了（%s）", exc)
            return

        self._browse_rounds += 1
        self._browse_browsed_at = time.time()
        log.info("抖音：刷了 %d 屏，记下 %d 条文案", screens_done, len(lines))
        if not lines:
            return

        # 记进「她刷到过什么」的同一本日记 —— 重启之后她还记得自己看过什么。
        fresh = self._phone_diary.add(
            [PhoneItem(source="douyin", title=line) for line in lines]
        )
        if not fresh:
            log.debug("抖音：这轮全是刷过的")
            return
        pick = self._speak_about_fresh()
        if pick is not None:
            group_id, entry = pick
            await self._speak_up(group_id, entry)

    async def _browse_douyin_screens(
        self, hands: PhoneHands, screens: int
    ) -> Tuple[int, List[str]]:
        """在抖音推荐流里滑 N 屏，把每屏的文案收下来。

        **零点击**：滑动之外的观察一律用 `hands.peek()`（dump 失败也不点屏幕）。
        2026-11-06 实测，在抖音广告上点屏幕中心会直接把广告点下去、装推广游戏 ——
        后台任务里不能有这种动作。

        每一屏顺手判断一次「要不要评论 / 要不要记模板」，见
        `_consider_douyin_screen()`。返回 (滑了几屏, 去重后的文案列表)。
        """
        done = 0
        texts: List[str] = []
        seen = set()
        for index in range(max(1, min(10, screens))):
            await hands.swipe("up")
            # 等信息流把下一个视频加载出来。不等的话读到的是上一条，
            # 她会以为「刷了但没变」—— `note_screen` 连续三次就判卡住。
            await asyncio.sleep(1.2)
            text, nodes = await hands.peek()
            if text.startswith("__ABORT__"):
                log.warning("抖音：滑到不该待的页面，停手（%s）", text)
                break
            lines = parse_douyin_feed(nodes) if nodes else []
            if not lines:
                # 抖音视频页大概率读不到 UI 树，OCR 是唯一能看见的东西。
                ocr = await hands.read_screen_ocr()
                lines = [
                    line.strip()
                    for line in (ocr or "").splitlines()
                    if len(line.strip()) >= 6
                ]
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    texts.append(line)
            await self._consider_douyin_screen(hands, text, lines)
            done += 1
        return done, texts

    # ---- 抖音：刷到有意思的才评论 / AI 视频当模板 ----
    #
    # 2026-11-06 用户定的规矩：
    # - 「评论她刷到有意思的视频才发评论，每天最多 5 条」
    # - 「她觉得有意思的 AI 视频，可以以其为模板自己生成一条」
    # - 「她评论过的视频要分享给我（暴走火车头）」
    #
    # 所以一屏最多判断两件事，而且都得花 LLM 调用 —— 只有「还有评论额度」
    # 或「疑似 AI 视频」才值得花；两样都不沾的屏直接跳过。

    # 广告页特征。见到就整屏跳过：一个点击都不能有。
    _AD_WORDS = (
        "广告", "立即下载", "下载安装", "查看详情", "了解更多", "点击安装", "安装应用",
    )
    # 抖音给 AI 生成内容的标记（OCR 能读到的几种写法）。
    _AI_WORDS = (
        "内容由AI生成", "内容由 AI 生成", "AI生成", "AI 生成", "由AI生成", "AI创作",
    )

    async def _consider_douyin_screen(
        self, hands: PhoneHands, screen_text: str, lines: List[str]
    ) -> None:
        """看完这一屏：要不要评论、要不要把结构记进灵感库。"""
        cfg = self.cfg.hands
        blob = " ".join([*(lines or []), screen_text or ""]).strip()
        if not blob:
            return

        hit_ad = next((w for w in self._AD_WORDS if w in blob), "")
        if hit_ad:
            log.debug("抖音：这屏是广告（%s），跳过", hit_ad)
            return

        ai_marked = any(w in blob for w in self._AI_WORDS)
        commenter = self._ensure_douyin_commenter() if cfg.comment_enable else None
        can_comment = commenter is not None and not commenter.budget_reason()
        if not (can_comment or ai_marked):
            return

        decision = await self._ask_douyin_interest(blob)
        if not decision:
            return

        pattern = str(decision.get("pattern") or "").strip()
        if pattern and ai_marked:
            self._save_douyin_inspiration(pattern, blob)

        comment = str(decision.get("comment") or "").strip()
        if not (can_comment and decision.get("interesting") and comment):
            return
        comment = comment[: max(4, int(cfg.comment_max_chars))]
        result = await commenter.comment(comment)
        if not getattr(result, "ok", False):
            log.info("抖音：评论没发成（%s）", getattr(result, "error", result))
            return
        log.info("抖音：评论成功「%s」", comment)
        await self._share_commented_video(hands, blob, comment)

    async def _ask_douyin_interest(self, blob: str) -> Optional[Dict[str, Any]]:
        """让模型判断这一屏值不值得评论 / 值不值得当模板。失败返回 None。"""
        system = (
            "# 此刻的情况\n"
            "你正自己刷着抖音。当前这条视频的文案 / 屏幕文字是：\n\n"
            f"{blob[:300]}\n\n"
            "先判断：这条视频值不值得你评论。**只有真的有意思、你有话想说才评**，"
            "绝大多数时候都不值得，就跳过。\n"
            "如果这是 AI 生成的视频、而且你觉得有意思，顺手抽象出可复用的"
            "「内容结构」：只写怎么起、怎么转，15 字以内，不能出现具体物品、"
            "人物、地点（比如「满怀期待地做一件事，结果被无视」）。\n\n"
            "只输出 JSON：\n"
            '{"interesting": true/false, "comment": "40字以内口语评论，不评就空串", '
            '"pattern": "内容结构，没有就空串"}'
        )
        try:
            reply = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "按 JSON 输出。"},
                ],
                tools=None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音：判断失视频败（%s）", exc)
            return None
        return _parse_json_object(reply)

    def _save_douyin_inspiration(self, pattern: str, blob: str) -> None:
        """把刷到的 AI 视频结构记进灵感库 —— 下次做视频会抽到它。"""
        pattern = pattern.strip().strip("「」\"'，。")
        if not (4 <= len(pattern) <= 30):
            return
        try:
            store = self._ensure_douyin_scheduler().store
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音：灵感库不可用（%s）", exc)
            return
        if store is None:
            return
        fresh = store.add([pattern], source=f"douyin:{blob[:40]}")
        if fresh:
            log.info("抖音：从刷到的 AI 视频记下结构「%s」", pattern)

    def _ensure_douyin_commenter(self) -> Any:
        """拿（或建）评论器。手机没接就不存在。"""
        if self._douyin_commenter is not None:
            return self._douyin_commenter
        if self._hands is None:
            return None
        cfg = self.cfg.hands
        try:
            hours = tuple(int(x) for x in (cfg.browse_hours or [9, 23])[:2])
        except (TypeError, ValueError):
            hours = (9, 23)
        self._douyin_commenter = DouyinCommenter(
            self._hands,
            daily_limit=int(cfg.comment_daily_limit),
            min_gap_minutes=float(cfg.comment_min_gap_minutes),
            hours=hours,
            state_path=Path(self.cfg.data_dir) / "douyin_comment_state.json",
        )
        log.info(
            "抖音：评论已启用（每天最多 %d 条，间隔 %.0f 分钟，时段 %s）",
            cfg.comment_daily_limit,
            cfg.comment_min_gap_minutes,
            list(hours),
        )
        return self._douyin_commenter

    async def _share_commented_video(
        self, hands: PhoneHands, blob: str, comment: str
    ) -> None:
        """评论成功后：截图留底 + 在抖音里把视频分享给指定好友。

        **用户 2026-11-06 明确：分享发生在抖音里，不是 QQ。**
        所以这里不走 OneBot，走抖音自己的分享面板 → 搜好友 → 发送。
        截图只落盘 `data/commented/`，留给人工核对，不发出去。
        """
        cfg = self.cfg.hands
        friend = (getattr(cfg, "comment_share_friend", "") or "").strip()

        # 1) 留底截图。分享流程会换界面，趁还在视频页先截。
        shot = await hands.screenshot()
        if shot:
            try:
                folder = Path(self.cfg.data_dir) / "commented"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"commented_{time.strftime('%Y%m%d_%H%M%S')}.png"
                path.write_bytes(shot)
                log.info("抖音：评论截图已留底 %s", path.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("抖音：评论截图落盘失败（%s）", exc)

        if not friend:
            log.info("抖音：没配 comment_share_friend，评论截图只留底不分享")
            return

        # 2) 抖音内分享。
        try:
            from .douyin_share import share_video_to_friend

            note = await share_video_to_friend(hands, friend)
            log.info("抖音：%s", note)
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音：在抖音里分享评论视频失败（%s）", exc)

    async def _launch_emulator(self, cfg: Any) -> bool:
        """拉起雷电模拟器，并等它上线。

        **用 dnplayer.exe，不用 dnconsole.exe。** 2026-11-06 实测：
        `dnconsole.exe launch` 要管理员权限，被系统拒绝；
        `dnplayer.exe index 0` 免提权能直接起（这两个参数就是实测的
        运行中进程命令行）。
        """
        exe = str(cfg.emulator_exe or "").strip()
        if not exe or not Path(exe).exists():
            log.warning("抖音：找不到模拟器启动器 %s", exe or "（未配置）")
            return False
        try:
            await asyncio.create_subprocess_exec(
                exe,
                "index",
                str(int(cfg.emulator_index)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音：启动模拟器失败（%s）", exc)
            return False

        wait = max(10.0, float(cfg.emulator_wait_seconds))
        started = time.time()
        while time.time() - started < wait:
            await asyncio.sleep(3.0)
            try:
                await self._hands.ensure_ready()
                log.info("抖音：模拟器已上线（等了 %.0f 秒）", time.time() - started)
                return True
            except HandsError:
                continue
        log.warning("抖音：模拟器等了 %.0f 秒还没上线", wait)
        return False

    # ------------------------------------------------------------------ #
    # 表情包

    async def _run_send_sticker(
        self,
        event: Dict[str, Any],
        args: Dict[str, Any],
        budget: Dict[str, Any],
        limit: int,
    ) -> str:
        """发一张表情，**直接发出去**，只把「已发出」回给模型。

        三条路，顺序是用户 2026-10-19 定的：
          ① **她自己画**（本机 ComfyUI，9 秒一张；池子里有就秒发）
          ② 本地素材（用户攒的真表情包）
          ③ QQ 内置小黄脸

        和生图同一个道理：图不可能经模型的手传出去，工具存在的意义就是干活。
        返回给模型的那句话刻意写得像旁白 —— 否则它会开始描述这个表情包，
        而对方已经看见了。

        ## `hint` 是这张表的逃生口

        40 个情绪覆盖的是高频反应，边角永远覆盖不完（「被自己的话噎住」
        「一边笑一边心虚」这种）。所以除了 `mood` 还收 `hint` —— 模型可以
        直接写这次要画的动作，不用挤进任何格子。

        `mood` 认不出来时也**不直接失败**：只要给了 hint 就照画。
        因为 `mood` 的硬用途只有两个（查本地素材目录、找内置表情），
        现画这条路根本不需要它 —— prompt 里决定表情的是 hint。
        """
        if budget["used"] >= limit:
            return "（这轮已经发过表情了，别再发）"

        raw_mood = str(args.get("mood") or "")
        hint = clean_hint(args.get("hint"))
        mood = resolve_mood(raw_mood)

        if mood is None:
            if not hint:
                return (
                    f"（没有「{raw_mood[:20]}」这个情绪，也没给 hint。"
                    f"要么从这些里挑一个：{mood_catalog()}；"
                    "要么用 hint 写清楚要画什么表情 / 动作（英文标签））"
                )
            log.info("情绪「%s」不在表里，但给了 hint —— 直接按 hint 画", raw_mood[:20])
            mood_key = FREE_MOOD_KEY
        else:
            mood_key = mood.key

        session_key = _session_key_from_event(event)

        # ---- ① 她自己画 ----
        #
        # 池子里有没用过的 → 秒发。没有 → 尝试当场画，但**最多等 sync_timeout**。
        #
        # 为什么当场画还要设上限、超时了还不算失败：animagine-xl-3.1 是 28 步，
        # 一张 50 秒起 —— 群聊里没人愿意为一颗表情等这么久。但「等不起」不等于
        # 「不要了」，所以超时后走两步棋：
        #   a) 丢一个后台任务把这张画完，画好直接发到群里（滞后但不缺席）；
        #   b) 本轮立即降级到本地素材 / 小黄脸，对方当下就有图可看。
        # 两条加起来，「慢底模」就退化成了纯后台成本，用户侧感知不到。
        draw_fell_back = False  # 现画没出图（超时），后面可能要靠后台补画兜底

        if getattr(self.cfg.sticker_gen, "enable", False):
            if not bool(getattr(self.cfg.sticker_gen, "generate", True)):
                # 2026-11-04 起停止生成新表情包：只从池子挑现成的，
                # 挑不到就落到下面的本地素材 / 小黄脸。不现画、不后台补画。
                path = self._sticker_artist.pick_only(session_key, mood_key, hint)
                if path is not None:
                    segment = build_sticker_segment(path)
                    if segment is not None and await self._send_sticker(event, [segment]):
                        budget["used"] += 1
                        log.info(
                            "表情包已发出（池子里的）：%s（情绪 %s）", path.name, mood_key
                        )
                        return (
                            "（表情包已经发出去了，对方能看见。**别再描述它**，"
                            "也别补一句「给你发了个表情」—— 那是废话）"
                        )
                    log.warning("池子里的表情包没发出去，走降级：%s", path)
            else:
                sync_timeout = float(getattr(self.cfg.sticker_gen, "sync_timeout", 12.0))
                path = await self._sticker_artist.draw(
                    session_key, mood_key, hint, timeout=sync_timeout
                )
                if path is not None:
                    segment = build_sticker_segment(path)
                    if segment is not None and await self._send_sticker(event, [segment]):
                        budget["used"] += 1
                        log.info(
                            "表情包已发出（她自己画的）：%s（情绪 %s）", path.name, mood_key
                        )
                        return (
                            "（表情包已经发出去了，对方能看见。**别再描述它**，"
                            "也别补一句「给你发了个表情」—— 那是废话）"
                        )
                    log.warning("自己画的表情包没发出去，走降级：%s", path)
                elif getattr(self.cfg.sticker_gen, "prewarm", True):
                    # 超时 / ComfyUI 慢 → 后台接着画。**先不决定发不发**：
                    # 等下面的降级走完，看这一轮有没有已经发出过表情 ——
                    # 发过了再补发，群里就会连着出现两张表情包
                    # （2026-11-04 用户反馈的老 bug）。决定点在方法尾部。
                    draw_fell_back = True
                    log.info(
                        "表情包现场画超时（%.0fs 内没出），先降级，后台补画：情绪 %s",
                        sync_timeout, mood_key,
                    )

        # 情绪认不出来 → 后两条降级路都没有坐标（查不到目录、找不到内置表情），
        # 到此为止。别硬凑一个不相关的表情发出去。
        if mood is None:
            # 自由情绪没有任何兜底可发 —— 后台画好后直接补发，
            # 这是对方唯一能看到的这张图。
            if draw_fell_back:
                self._spawn_sticker_draw(
                    event, session_key, mood_key, hint, send_when_done=True
                )
            return "（这张没画成，本地也没有对应素材。跟对方说一声，别再试了）"

        # ---- ② 本地素材 ----
        path = self._sticker_picker.pick(session_key, mood)
        if path is not None:
            segment = build_sticker_segment(path)
            if segment is not None and await self._send_sticker(event, [segment]):
                budget["used"] += 1
                log.info("本地表情包已发出：%s（情绪 %s）", path.name, mood.key)
                # 这轮已经发过表情了 —— 后台画好的只入池子，不再补发，
                # 不然群里会连着看到两张表情包（2026-11-04 用户反馈）。
                if draw_fell_back:
                    self._spawn_sticker_draw(
                        event, session_key, mood_key, hint, send_when_done=False
                    )
                return (
                    "（表情包已经发出去了，对方能看见。**别再描述它**，"
                    "也别补一句「给你发了个表情」—— 那是废话）"
                )
            log.warning("本地表情包没发出去，退成内置表情：%s", path)

        # ---- ③ QQ 内置小黄脸 ----
        if await self._send_sticker(event, [build_face_segment(mood)]):
            budget["used"] += 1
            log.info("内置表情已发出：%s（id=%d）", mood.key, mood.face_id)
            if draw_fell_back:
                self._spawn_sticker_draw(
                    event, session_key, mood_key, hint, send_when_done=False
                )
            return "（表情已经发出去了，对方能看见。别再描述它）"

        # 走到这里 = 什么都没发出去。后台补画是对方唯一能拿到的图，画好要补发。
        if draw_fell_back:
            self._spawn_sticker_draw(
                event, session_key, mood_key, hint, send_when_done=True
            )
        return "（表情发不出去，跟对方说一声，别再试了）"

    async def _send_sticker(
        self, event: Dict[str, Any], segments: List[Dict[str, Any]]
    ) -> bool:
        """把表情段发到当前会话。返回是否成功。

        刻意**不引用**对方的消息（不像 `_send_image` 那样带 reply）——
        表情包底下挂一条被引用的文字消息很怪，真人不会那么干。
        """
        self._check_event(event)
        group_id = event.get("group_id")
        user_id = event.get("user_id")
        try:
            if group_id is not None:
                await self.onebot.send_group_msg(int(group_id), segments)
            elif user_id is not None:
                await self.onebot.send_private_msg(int(user_id), segments)
            else:
                log.warning("事件里既没有 group_id 也没有 user_id，表情发不出去")
                return False
        except OneBotError as exc:
            log.error("发送表情失败：%s", exc)
            return False

        # 和 _send_image 保持一致：表情也算「一次回复」，同样打点、同样续窗口。
        # 漏掉的话，发表情之后紧接着的连发消息就不受硬间隔约束了。
        self._last_reply_sent_at[_session_key_from_event(event)] = time.time()
        self._renew_awake_after_reply(event)
        return True

    def _spawn_sticker_draw(
        self,
        event: Dict[str, Any],
        session_key: str,
        mood_key: str,
        hint: str,
        send_when_done: bool = True,
    ) -> None:
        """后台把这张表情包画完；`send_when_done` 决定画好后发不发到会话。

        `send_when_done=False` 用于「本轮已经发过兜底表情」的情况：
        画好的图只入池子给以后用，不再补发 —— 否则群里会连着看到
        两张同情绪的表情包（2026-11-04 用户反馈的老 bug）。

        ## 为什么值得存在

        animagine-xl-3.1 一张 50 秒起。同步等，群里的人会以为机器人死了；
        直接不发，功能等于残废。所以让「慢」只发生在后台：
        本轮先用素材 / 小黄脸顶住，画好的自己补发出去。

        ## 几个不会被注意但重要的点

        · **事件对象是引用** —— event 里的 group_id / user_id 用到的就是那几个字段，
          ComfyUI 那一分钟里不会有别的东西改它。
        · **错误绝不外溢** —— 后台任务抛异常会变成 asyncio 的未处理异常日志，
          污染主流程的排错视线。这里全兜住。
        · **不占回复预算** —— 后台这张跟「本轮最多一张」的闸门无关，
          它是迟到的补发，不是额外多发。
        """
        async def _run() -> None:
            try:
                path = await self._sticker_artist.draw(session_key, mood_key, hint)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("后台画表情包失败（情绪 %s）", mood_key)
                return
            if path is None:
                log.info("后台补画也没画成（情绪 %s），到此为止", mood_key)
                return
            if not send_when_done:
                log.info("后台补画完成但本轮已发过表情，只入池不补发：%s", path.name)
                return
            segment = build_sticker_segment(path)
            if segment is None:
                return
            if await self._send_sticker(event, [segment]):
                log.info(
                    "表情包（后台补画）已发出：%s（情绪 %s）", path.name, mood_key
                )
            else:
                log.info("后台补画的图没发出去（会话可能已失效）：%s", path.name)

        try:
            asyncio.get_running_loop().create_task(_run(), name="sticker-draw")
        except RuntimeError:  # 没有事件循环（理论上不会，core 全程在 coroutine 里）
            log.warning("没有事件循环，跳过后台补画")

    # ------------------------------------------------------------------ #

    def _split_reply(self, text: str) -> List[str]:
        """按模型标的语义边界拆成多条。开关关掉 / 没标 → 就是一条。"""
        cfg = self.cfg.bot
        sep = str(cfg.split_separator or "").strip()
        if not cfg.split_messages or not sep:
            # 开关关着。模型万一还是写了分隔符（比如刚关掉但 prompt 还没更新、
            # 或者它自己从别处学来的），**不能让它原样漏进正文** —— 换成换行。
            return [_history_reply(text, sep)] if sep else [text]
        return msg.split_messages(text, sep, cfg.split_max_parts)

    async def _send_reply(self, event: Dict[str, Any], text: str) -> None:
        self._check_event(event)
        text = _tidy(text)
        if not text:
            return

        # 打点：这是硬间隔（`reply_min_gap`）的计时起点。
        # 放在最前面而不是发完之后 —— 拆分发送可能带停顿（split_gap），
        # 等全部发完再记会让间隔被拉长，把紧跟着的追问也挡掉。
        self._last_reply_sent_at[_session_key_from_event(event)] = time.time()

        # 回复了就把激活窗口续满 —— 让连续对话接得上（见该方法的说明）。
        # 和上面那句打点放在一起是刻意的：它们都是「发生了一次回复」的副作用，
        # 语义完全一致，分成两处迟早会漏掉一边。
        self._renew_awake_after_reply(event)

        prefix = self.cfg.bot.reply_prefix or ""

        # 两级拆分：
        #   一级 = 模型标的语义边界（「这里另起一条」）—— 这次新加的
        #   二级 = 单条仍然超长时按长度兜底切 —— 原来就有
        #
        # 记下每个 chunk 是不是「另起一条消息的开头」：另起一条时停顿要
        # 长一些（真人是分开想到、分开发的），同一条被长度切开则停顿短。
        parts = self._split_reply(text)
        if not parts:
            return

        limit = max(50, int(self.cfg.bot.max_reply_chars))
        chunks: List[str] = []
        starts_new: List[bool] = []
        for part_index, part in enumerate(parts):
            for chunk_index, chunk in enumerate(msg.chunk_text(prefix + part, limit)):
                chunks.append(chunk)
                starts_new.append(chunk_index == 0 and part_index > 0)
        if not chunks:
            return

        group_id = event.get("group_id")
        user_id = event.get("user_id")
        message_id = event.get("message_id")
        quote = self._should_quote(group_id, message_id)

        gap_min = max(0.0, float(self.cfg.bot.split_gap_min))
        gap_max = max(gap_min, float(self.cfg.bot.split_gap_max))

        for index, chunk in enumerate(chunks):
            self._check_event(event)
            segments = msg.build_text_message(
                chunk,
                reply_id=int(message_id) if (index == 0 and quote) else None,
            )
            try:
                if group_id is not None:
                    await self.onebot.send_group_msg(int(group_id), segments)
                elif user_id is not None:
                    await self.onebot.send_private_msg(int(user_id), segments)
                else:
                    log.warning("事件里既没有 group_id 也没有 user_id，无法回复")
                    return
            except OneBotError as exc:
                log.error("发送消息失败：%s", exc)
                return

            if index < len(chunks) - 1:
                # 下一条是另起的消息 → 随机停顿；同一条被切开的 → 短停顿。
                # 固定间隔看着像脚本在跑，随机才像人在打字。
                if starts_new[index + 1]:
                    await asyncio.sleep(random.uniform(gap_min, gap_max))
                else:
                    await asyncio.sleep(0.4)

    def _should_quote(self, group_id: Any, message_id: Any) -> bool:
        """要不要引用原消息。**只在会被误认的时候引用。**

        以前是每条都引用 —— 群里看过去像个打卡机器人，每条消息头顶挂一个
        引用框，特别吵。真人只在一种情况下引用：要说的话跟更早的那条有关，
        直接发出去会被当成在回最后一条。

        判据很简单，也是唯一需要的判据：
        **被回复的那条，还是不是这个群最新的消息？**

        * 是 → 前后没有别人插话，不存在歧义 → 不引用
        * 不是 → 中间又来了别的消息 → 引用，免得认错对象

        私聊永远不引用：一对一，不存在认错对象。
        """
        if not self.cfg.bot.reply_quote:
            return False
        if message_id is None or group_id is None:
            return False
        try:
            latest = self._last_group_msg.get(int(group_id))
        except (TypeError, ValueError):
            return False
        # 按字符串比。NapCat 不同事件里 message_id 可能是 int 也可能是 str，
        # 直接比会永远判成「不是最新的」，变成每条都引用。
        return str(latest) != str(message_id)

    # ------------------------------------------------------------------ #

    def _is_duplicate(self, message_id: Any) -> bool:
        key = str(message_id)
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        while len(self._seen) > DEDUP_CACHE_SIZE:
            self._seen.popitem(last=False)
        return False

    async def _model_command(self, session_key: str, arg: str) -> str:
        """`/model` 指令的实现：列清单 / 切换 / 取消指定。

        清单是**现问 ComfyUI** 拿的，不是硬编码 —— 用户往
        `models/checkpoints` 里丢新模型就能直接用，不用改代码。
        """
        cfg = self.cfg.imagegen
        available = await list_comfyui_checkpoints(self.http, cfg)
        if not available:
            return (
                "读不到本机底模列表 —— ComfyUI 没连上。"
                "先用 start_comfyui.bat 把它开起来；发 /comfyui 可以看到本机通不通。"
            )

        current = self._model_override.get(session_key) or (cfg.comfyui_checkpoint or "").strip()
        # 配置里写的名字可能已经不在 ComfyUI 里了（文件被删/改名）——
        # 那就退到第一个可用的，别报一个用户根本切不过去的名字。
        if current not in available:
            current = available[0]

        arg = (arg or "").strip()
        if not arg:
            lines = ["本机底模（ComfyUI 现有）："]
            for name in available:
                preset = model_preset(name)
                mark = "  ← 当前" if name == current else ""
                lines.append(f"  · {name}  [{preset['label']}]{mark}")
            lines.append("切换：/model <关键词>（比如 /model wai、/model animagine）")
            lines.append("取消指定：/model default")
            return "\n".join(lines)

        if arg.lower() in ("default", "默认", "auto", "自动", "取消", "reset"):
            had = self._model_override.pop(session_key, None)
            return (
                "已取消底模指定，回到配置里的默认底模。"
                if had
                else "本来就没指定底模，用的就是配置里的默认值。"
            )

        picked = match_checkpoint(arg, available)
        if not picked:
            return (
                f"没有匹配「{arg}」的底模。现在有：\n  "
                + "\n  ".join(available)
                + "\n换个短一点的关键词试试（比如 /model wai）。"
            )

        self._model_override[session_key] = picked
        preset = model_preset(picked)
        log.info("会话 %s 的本机底模被指定为 %s", session_key, picked)

        # 把跟着换的参数报出来 —— 用户判断"这次出图为什么不一样"要看的就是这些，
        # 光说"参数已重标"等于没说。
        params: List[str] = []
        if preset.get("sampler"):
            params.append(f"{preset['sampler']}/{preset.get('scheduler') or 'normal'}")
        if preset.get("cfg") is not None:
            params.append(f"cfg {float(preset['cfg']):g}")
        if preset.get("steps") is not None:
            params.append(f"{int(preset['steps'])} 步")
        param_txt = "、".join(params) if params else "沿用配置里的值"

        return (
            f"好，这个会话的本机底模换成 **{picked}**（{preset['label']}）。\n"
            f"参数跟着换：{param_txt}，IP-Adapter 强度上限 {preset['style_weight_max']}。\n"
            "不同底模要的参数差很多，沿用旧的会直接出废图。"
        )

    async def _channel_text(self, session_key: str) -> str:
        """当前生图通道说明。`/channel` 指令回这句。

        报的是**实际会走哪条**，不是配置里写的 provider —— `auto` 会跟着
        api_key 和本机起没起来变，只说「开」看不出到底打的哪个接口。
        """
        gen = self.cfg.imagegen
        if not gen.enable:
            return "生图现在是关着的。"

        override = self._channel_override.get(session_key)
        if override == "local":
            head = "本机 ComfyUI（**本会话指定**，/auto 可取消）"
        elif override == "online":
            head = "在线豆包（**本会话指定**，/auto 可取消）"
        else:
            head = "默认：本机 ComfyUI 优先，连不上才降级到在线"

        # 会走本机时顺带报一下通不通 —— 这是「为什么画不出来」最常见的原因。
        will_use_local = override == "local" or (override is None and gen.comfyui_enable)
        if will_use_local:
            try:
                ready = await probe_comfyui(self.http, gen)
            except Exception:  # noqa: BLE001
                ready = False
            head += f"\n  本机 ComfyUI：{'✅ 可用' if ready else '❌ 没连上（服务没起来）'}"

        return (
            f"生图通道：{head}\n"
            "  切换：/comfyui 走本机（免费）· /doubao 走在线（收费）· /auto 恢复默认"
        )

    def _status_text(self, session_key: str, group_id: Optional[int] = None) -> str:
        uptime = int(time.time() - self.started_at)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        stats = self.memory.stats(session_key)

        # 生图要报**实际走的那条通道**，不是配置里写的 provider ——
        # auto 会跟着 api_key 变，只说「开」看不出到底打的哪个接口。
        if not self.cfg.imagegen.enable:
            gen_state = "关"
        elif self.cfg.imagegen.resolved_provider == "openai":
            gen_state = "开（通用通道）"
        else:
            gen_state = "开（免费通道）"

        lines = [
            "运行状态",
            f"  连接：{'已连接' if self.onebot.connected else '未连接'}",
            f"  账号：{self.onebot.self_id or '未知'}",
            f"  模型：{self.cfg.deepseek.model}",
            f"  在线：{hours}小时{minutes}分{seconds}秒",
            f"  已处理：{self.handled_count} 条，出错 {self.error_count} 次",
            f"  本会话上下文：{stats.get('turns', 0)} 轮",
            f"  看图：{'开' if self.cfg.bot.enable_vision else '关'}",
            f"　联网：{'开' if (self.cfg.bot.enable_search and self.cfg.search.api_key) else '关'}",
            f"　生图：{gen_state}",
        ]

        # 群聊里额外报激活窗口 —— 排「它为什么不理我」最有用的就是这一条
        if group_id is not None and self._awake_seconds > 0:
            window_min = self._awake_seconds / 60
            deadline = self._group_awake.get(group_id)
            if deadline is None:
                lines.append(f"  对话窗口：未激活（@ 一次可开启 {window_min:g} 分钟）")
            else:
                remain = deadline - time.time()
                if remain <= 0:
                    lines.append("  对话窗口：已过期（@ 一次可重新开启）")
                else:
                    lines.append(f"  对话窗口：还剩 {remain / 60:.1f} 分钟，不用再 @")

        return "\n".join(lines)
