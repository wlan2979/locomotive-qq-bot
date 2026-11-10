"""生图工具。两条通道，走哪条由配置决定。

通道 A：Pollinations（默认，零配置）
------------------------------------
免费、不用注册、不用 API key —— 装完就能用。质量够聊天用。

免费档的实情（2026-09-30 实测，别被文档骗了）
  * **分辨率有硬上限**：免费档把出图压到约 **768x768 的总像素**（实测
    589,280 / 589,824 两个样本，误差 0.1%）。传 `width=1216&height=832`
    拿回来的是 928x635 —— **画幅比例保留，分辨率等比缩小**。
    所以 `size` 参数是有效的，但别指望能要到 4K。
  * **只有 `sana` 模型稳定可用**。`flux` / `turbo` 更容易吃 **HTTP 402**，
    而且同样的请求换个时间又能过 —— 是「容易失败」不是「一定不行」。
  * **402 是限流，不是配置错误**。实测连着打 2～3 张就开始 402，
    恢复要十几秒到一分钟（跑真机验证脚本时实测等 45 秒仍可能没恢复）。
    `cooldown` 标记就是为它准备的。
  * 右下角有 `pollinations.ai` 水印，`nologo=true` **不生效**（试过三种写法，
    返回的字节完全一样）。所以这里干脆裁掉底部一条。
  * **只吃英文提示词**。中文能提交，但出图质量明显差 ——
    所以工具描述里明确要求模型自己写英文 prompt。
  * 出图带 EXIF，元数据里塞了本次请求的完整参数，排查时可以读。

**一句话总结：它是「能用的免费额度」，不是「稳定的服务」。**
真要靠得住，就配通道 B —— 那才是这个模块有两条通道的原因。

通道 B：任意 OpenAI 兼容接口（填了 api_key 自动启用）
-----------------------------------------------------
`POST {api_base}/images/generations`
头：`Authorization: Bearer <key>`、`Content-Type: application/json`
体：`{"model": ..., "prompt": ..., "size": "1024x1024", "n": 1}`

写这份代码时的取舍（踩过才知道）：
  * **响应格式两家不一样，代码自己认**。智谱、硅基流动返回
    `data[0].url`（图在对象存储上，得再下一次）；OpenAI 返回
    `data[0].b64_json`。所以 `_extract_api_image()` 两种都接 ——
    不为这个让用户去配一个开关。
  * **不传 `response_format`**。OpenAI 的 `gpt-image-1` 收到这个参数直接报错，
    而它本来就返回 b64；dall-e-3 默认给 url，我们反正会去下载。
    不传 = 三家都能过。
  * **不传 `negative_prompt`**。只有硅基流动支持，智谱会拒。宁可不给。
  * **画幅用 `api_sizes` 可覆盖**。各家支持的尺寸不一致：OpenAI 认
    `1024x1536`，智谱认 `768x1344` 却**不认** `768x1024`。默认值选最通用的一组，
    不合适的用一行配置改掉，比代码里猜准。
  * 这条通道**没有水印**，所以不裁底部。

和 `bot/image.py` 的分工
-----------------------
`image.py` 管「用户发来的图怎么处理」（解码验证 + 重编码）。
这里管「要一张新图怎么拿到」。**两条通道拿到的字节都走同一道校验**
（`prepare()`），不让一个坏响应流到 QQ 里。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import aiohttp
from PIL import Image

from .config import ImageGenConfig
from .image import prepare
from .usage import UsageTracker

# 生图用量统计。由 main.py 在启动时调用 init_usage() 绑定落盘路径。
#
# 为什么做成模块级：调用链很深（core → imagegen → 各通道），
# 一路往下传一个统计对象会污染每一个函数的签名，而我们只需要"记一笔"。
_usage = UsageTracker()

# 最近一次在线生图消耗的 token（方舟在响应 usage 里给）。
# 同样是"顺路带回来"的旁路信息 —— 为了它去改 generate() 的返回类型
# （现在返回 bytes）会牵动所有调用点，不值得。
_last_api_tokens = 0

# 最近一次生图**实际走的通道**，以及给用户看的一句话说明。
#
# 为什么要回传这个：通道会**静默降级** —— 在线额度用完/接口挂了就自动退到本机，
# 本机挂了就退到在线。日志里记了，但用户在群里只看到「画风变了」，
# 会以为功能坏了或"豆包通道被关了"（实测就是这么反馈的）。
# 所以降级时要说一句，让用户知道发生了什么。
#
# 空串 = 走的是配置里的主通道，无需解释。
_last_channel_note = ""


def last_channel_note() -> str:
    """最近一次生图的通道说明（空串表示无需向用户解释）。"""
    return _last_channel_note


def init_usage(path: Optional[Path] = None) -> UsageTracker:
    """绑定用量记录的落盘位置并载入历史。启动时调一次。"""
    global _usage
    _usage = UsageTracker(path)
    _usage.load()
    return _usage


def usage_summary(hours: float = 24.0) -> Tuple[int, float]:
    """最近 `hours` 小时的 (张数, 估算花费)。给上层做状态展示用。"""
    return _usage.summary(hours)


def usage_total() -> Tuple[int, float]:
    """**累计** (张数, 估算花费)。免费额度计数用它。"""
    return _usage.total()


def _online_allowed(cfg: ImageGenConfig) -> Tuple[bool, str]:
    """在线通道还能不能用。返回 (是否放行, 拒绝原因)。"""
    return _usage.allow(
        hourly_limit=int(getattr(cfg, "api_hourly_limit", 0) or 0),
        daily_limit=int(getattr(cfg, "api_daily_limit", 0) or 0),
        total_limit=int(getattr(cfg, "api_free_quota", 0) or 0),
    )


def _record_online_use(
    cfg: ImageGenConfig, size: Optional[str], tokens: int = 0
) -> None:
    """记一笔在线通道的用量。**只在真的出图成功后调**。"""
    try:
        cost = float(getattr(cfg, "api_cost_per_image", 0) or 0)
        _usage.record(
            cost=cost,
            tier=str(size or "default"),
            channel=str(getattr(cfg, "api_model", "") or "online"),
            tokens=tokens,
        )
    except Exception:  # noqa: BLE001
        log.exception("记录生图用量失败（忽略，不影响出图）")

log = logging.getLogger(__name__)

# 重试等待时间，分两种。
#   * 普通可重试错误（5xx、网络抖动）：等 3 秒就够，服务端缓一下就回来了。
#   * 限流（402/429）：实测十几秒到一分钟才恢复，等 3 秒再撞纯属浪费 ——
#     所以给它一个明显更长的等待，**但只多试这一次**。
#     真·长时间限流由 BotCore 的冷却（DRAW_COOLDOWN）兜，这里不硬耗。
RETRY_WAIT = 3.0
COOLDOWN_WAIT = 12.0

# 认为「这不是一张图」的字节下限。见过 2 字节的 402 响应体 —— 别把它往下传。
MIN_IMAGE_BYTES = 1024

# 给模型看的工具声明。
# prompt 必须英文这一条写在 description 里 —— 模型很听话，
# 但你不说它就默认用用户的原话（中文），出图质量会掉一档。
#
# 「写短、别堆关键词」这条是 2026-09-30 实测补上的：
# 模型很爱写「8K, ultra detailed, masterpiece, multiple exposure, award winning」
# 这种关键词沙拉，700+ 字符，主体被埋在最后。同 seed 同模型对比下来，
# 长 prompt 那张明显更差 —— 风格词互相打架，脸和结构先崩。
GENERATE_IMAGE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "画一张新图并发出去。当对方说「画一个」「生成图片」「来张图」"
            "「给我做个表情包」这类**要你创作图片**的请求时调用。\n"
            "注意事项：\n"
            "  · **必须真的调用这个工具，图才会出现。** 只回一句「画好了」是骗人 —— "
            "你自己写不出图，图只能是这个工具画的。\n"
            "  · prompt 必须用**英文**，而且**要短** —— 一句话、10～30 个词。"
            "先说清楚画的是谁/什么，再带一两个风格词就够了。\n"
            "    对方用中文描述时，你先把意思翻成英文再填进来。\n"
            "  · **不要堆关键词。** 禁写 `8K`、`ultra detailed`、`masterpiece`、"
            "`multiple exposure`、`award winning`、`highly detailed` 这类 —— "
            "堆关键词会让风格词互相打架、主体被冲掉 —— 这是**这条通道**实测的结论，"
            "别和下面本机 ComfyUI 那套说明混用。\n"
            "  · 如果对方只是让你**看**他发来的图，不要调用这个工具。\n"
            "  · 调用成功后图会直接发出去。你只需要补一句简短的话，"
            "**不要**再描述画面里有什么 —— 对方自己能看见。\n"
            "  · 同一条消息里最多画一张。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "英文画面描述，**一句话就够**（10～30 个词）。"
                        "先说主体再说风格，例如 'an anime girl with silver twin-tails "
                        "in a snowy street, soft blue lighting'。"
                        "不要堆砌 8K / masterpiece / ultra detailed 这类空词。"
                    ),
                },
                "size": {
                    "type": "string",
                    "enum": ["square", "portrait", "landscape"],
                    "description": "画幅。默认 square（正方形）。",
                },
                "draw_self": {
                    "type": "boolean",
                    "description": (
                        "**对方让你画「你自己」时填 true** —— 「画一下你自己」"
                        "「画个你」「来张自拍」「画你自己」这类。\n"
                        "填了之后系统会把你的**固定外貌**（黑发、深棕眼、小个子、"
                        "齐刘海）自动补进 prompt，**所以 prompt 里千万别再写这些**，"
                        "只写这几样：服装、发型（**对方没要求就干脆别写**，"
                        "系统默认给你低双马尾）、姿势、表情、场景、光照、画风。\n"
                        "画**别的**角色、风景、物品时**不要填**。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}

# 本机 ComfyUI 通道用的 prompt 写法说明。
#
# 为什么和上面那份不一样：底层模型是两个物种。
#   · 本机 SDXL 动漫底模（animagine-xl-3.1）拿 Danbooru 标签集训的，
#     吃 `1girl, silver hair, blue eyes, school uniform` 这种**标签串**；
#     通顺英文句子对它来说是浪费 —— 眼睛颜色、视角、光照没点到它就自己编。
#   · Pollinations(sana) 相反，堆关键词会让风格词互相打架、主体被冲掉。
# 用同一套说明必然有一条通道吃亏，所以按通道分开。
_COMFYUI_PROMPT_DESC = (
    "英文 **Danbooru 标签串**（**不是句子**）：逗号分隔的短标签，20～40 个。\n"
    "例：'1girl, solo, long silver hair, blue eyes, school uniform, "
    "smile, looking at viewer, cherry blossoms, soft lighting, anime coloring'\n"
    "顺序：① 人数 ② 头发/眼睛 ③ 服装 ④ 表情/姿势 ⑤ 场景/光照 ⑥ 画风。\n"
    "把**眼睛颜色、发色、服装、视角、光照**都点到，少一个模型就自己编一个。\n"
    "别写 `8K`、`ultra detailed`、`award winning` 这类空词 —— "
    "和动漫底模的训练标签对不上，只会抢权重。\n"
    "质量标签（masterpiece / best quality）系统会自动补，不用你写。"
)

_COMFYUI_DESC_SUFFIX = (
    "\n  · prompt 写成 **Danbooru 标签串**（逗号分隔的短标签、20～40 个），"
    "**不要写成通顺句子** —— 本地底模是拿标签集训的。"
    "眼睛颜色、发色、服装、视角、光照都要点到。\n"
    "  · 质量标签系统自动补，不用写 `masterpiece`；也别写 `8K`、"
    "`award winning` 这类和训练标签对不上的空词。\n"
    "  · **对方发来图片、又让你画**（「照这张画一张」「改成 XX 风格」"
    "「把这张重画」）时，用 `reference_image` 指定那张图，别凭空重新编一张。"
    "对方只发了图、没让你画时，不要调用这个工具。\n"
    "  · **对方要换场景或换动作**（「把她放到教室里」「让她跳舞」「换个背景」"
    "「让她躺下来」）时，除了填 `reference_image`，**还要填 `new_scene: true`** —— "
    "图生图换不动场景。这时引擎会改成纯文生图重画，"
    "所以 prompt 里**要把服装和道具也写上**"
    "（发色/发型/瞳色引擎会自己从参考图读出来补上，**不用你写**，"
    "写了会和补进去的重复加权）。\n"
)


# ---- 挂了 LoRA 之后的 prompt 说明 ----
#
# 为什么是**替换**而不是「追加一句」：上面 `_COMFYUI_PROMPT_DESC` 明确要求
# 「把**眼睛颜色、发色**、服装、视角、光照都点到」—— 挂了 LoRA 之后
# 这一句就是**错的**：角色的长相由 LoRA 决定，再写一遍会双重加权把脸带偏。
# 两份说明直接矛盾时，模型不会「听更对的那份」，而是退化成四不像的折中写法
# （本项目在 prompt 写法上反复踩过这个坑）。
# 所以外貌那一条必须从源头换掉，而不是在后面补一句「别写外貌」。
_COMFYUI_PROMPT_DESC_LORA = (
    "英文 **Danbooru 标签串**（**不是句子**）：逗号分隔的短标签，20～40 个。\n"
    "例：'1girl, solo, school uniform, smile, looking at viewer, "
    "cherry blossoms, soft lighting, anime coloring'\n"
    "顺序：① 人数 ② 服装 ③ 表情/姿势 ④ 场景/光照 ⑤ 画风。\n"
    "⚠️ **绝对不要写发色、发型、瞳色、身材这类外貌标签。**\n"
    "系统已经给底模挂了角色 LoRA，这个角色长什么样是**固定的** —— "
    "再写一遍会双重加权、把脸带偏，结果反而不像。\n"
    "**只写：服装、表情、姿势、场景、光照、画风。**\n"
    "别写 `8K`、`ultra detailed`、`award winning` 这类空词 —— "
    "和动漫底模的训练标签对不上，只会抢权重。\n"
    "质量标签（masterpiece / best quality）系统会自动补，不用你写。"
)

_COMFYUI_LORA_DESC_SUFFIX = (
    "\n  · ⚠️ **本机底模挂了角色 LoRA，角色的长相是固定的。**"
    "prompt 里**只写服装、表情、姿势、场景、光照、画风**，"
    "**绝对不要写发色 / 发型 / 瞳色 / 身材**这类外貌标签 —— "
    "写了会和 LoRA 打架，把脸带偏。\n"
    "  · 对方明确要求改外貌（「给她染成蓝发」「换个发型」）时，"
    "照常把这个要求写进 prompt，但**不要承诺改得动** —— "
    "LoRA 大概率会把它拉回去。可以照画一张，如实说明效果有限。\n"
)


def build_generate_image_tool(gen: Any) -> Dict[str, Any]:
    """按本机文生图和在线参考图编辑能力提供工具参数。"""
    online_edits = getattr(gen, "resolved_provider", "") == "openai" and bool(getattr(gen, "api_ref_field", ""))
    if not getattr(gen, "comfyui_enable", False) and not online_edits:
        return GENERATE_IMAGE_TOOL

    tool = json.loads(json.dumps(GENERATE_IMAGE_TOOL))  # 深拷贝，别改到常量
    fn = tool["function"]
    fn["description"] = fn["description"] + _COMFYUI_DESC_SUFFIX
    props = fn["parameters"]["properties"]
    # 挂了 LoRA 就换成「别写外貌」那一版说明。
    # 这里**必须整段替换** —— 原来那版明确要求写发色瞳色，和 LoRA 直接冲突。
    lora_on = bool((getattr(gen, "comfyui_lora", "") or "").strip())
    if lora_on:
        fn["description"] = fn["description"] + _COMFYUI_LORA_DESC_SUFFIX
    props["prompt"]["description"] = (
        _COMFYUI_PROMPT_DESC_LORA if lora_on else _COMFYUI_PROMPT_DESC
    )
    props["reference_image"] = {
        "type": "integer",
        "description": (
            "参考图的序号（图生图用）。对方这条消息发来的第几张图，从 1 开始。"
            "只有「照着这张画 / 改这张图」这类请求才填；"
            "纯凭描述画新图时**不要填**。填了就会以那张图为底重绘。"
        ),
    }
    props["denoise"] = {
        "type": "number",
        "description": (
            "重绘幅度，0.2～0.8，默认 0.65（只在给了 reference_image 时有意义）。"
            "**0.6~0.7 = 保留大致构图、换掉细节**（换衣服、换姿势的甜点区）；"
            "**0.35 = 变化很小**，基本是微调（加个帽子、改个颜色）。"
            "对方说「只改一点」就往 0.3~0.5 靠。\n"
            "**注意：上限就是 0.8，别再往上要** —— 图生图是「从参考图的构图出发」，"
            "这个值再大也换不掉整个场景。要让场景/动作真的换掉，用 `new_scene`。"
        ),
    }
    props["new_scene"] = {
        "type": "boolean",
        "description": (
            "**这一轮要换场景、或换动作/姿势**时填 true。"
            "例如「把她放到教室里」「让她在跑步」「换个背景」「让她跳舞」「躺下来」。\n"
            "**为什么需要这个开关**：参考图是「带场景的插画」时，图生图在结构上"
            "就做不到这件事 —— 实测（同一张图、同 seed、同一句「换到教室跳舞」）："
            "denoise 0.35 连场景都换不动；0.55 场景进来了、发色却开始变；"
            "0.80 场景动作都对了、人却完全换了。**不存在两全的 denoise 值。**\n"
            "填 true 后引擎会改成「读出角色外貌 + 纯文生图重画一张」，"
            "新场景和新动作才真的进得去。\n"
            "**代价**：角色是按描述重画的，不会和原图像素级一致。\n"
            "只改风格、只换衣服、只换表情时**不要填**（那些图生图做得好）；"
            "填了 true 时 `denoise` 会被忽略。"
        ),
    }
    props["channel"] = {
        "type": "string",
        "enum": ["auto", "local", "online"],
        "description": (
            "用哪条通道画。**绝大多数情况留空或填 `auto`，不要自作主张。**\n"
            "  · `auto` —— 本机 ComfyUI 优先（免费、不限量、无水印），"
            "本机没起来才降级到在线。**默认值，画普通图就用它。**\n"
            "  · `local` —— **强制**本机。对方明确说「用本机画」「用本地模型」"
            "「省点钱」「不要花钱」时填。本机没起来会**直接失败**，"
            "不会偷偷转在线（偷偷转了就是背着他花钱）。\n"
            "  · `online` —— **强制**在线（豆包 Seedream）。"
            "对方说「用豆包画」「用在线画」「画写实一点」「画精致点」时填 —— "
            "在线模型更懂自然语言、能重建场景、画风更写实，**但要花钱、有每日额度**。\n"
            "⚠️ **换通道要连 prompt 写法一起换**：本机是 SDXL 动漫底模，"
            "吃逗号分隔的 Danbooru 标签串；在线是自然语言指令模型，"
            "吃一整句描述。用错写法会让出图质量掉一大截。"
        ),
    }
    props["style_reference"] = {
        "type": "integer",
        "description": (
            "**画风参考图**的序号（从 1 开始）。和 `reference_image` 完全不同："
            "`reference_image` 是**照着这张画**（构图跟着走），"
            "`style_reference` 是**用这张的画风去画别的内容**（构图和人物由 prompt 决定）。"
            "对方说「用这个画风」「这种上色/笔触/色调来画 XX」时填它。"
            "画新东西、只是想借用参考图的画风时才填，**不要**和 reference_image 混用同一个意图。"
        ),
    }
    props["style_weight"] = {
        "type": "number",
        "description": (
            "画风强度（只在给了 style_reference 时有意义）。"
            "**可用区间跟着底模走，不是固定值** —— 同一个数在有的底模上干净、"
            "在另一个上直接崩。系统会按当前底模自动钳到安全上限，"
            "所以你填个 0.5 也不会出事，但**别指望靠调高它拿到更浓的画风**。\n"
            "**想要更浓的画风，改用 `reference_image`（图生图）那条路**，"
            "那才是真正保内容的手段。"
        ),
    }
    if online_edits:
        fn["description"] += (
            "\n参考图修改默认使用在线接口，不占本机生图显存；普通文生图遵从当前默认通道。"
            "使用参考图时必须填写 reference_image 或 style_reference。"
            "在线修改请用自然语言描述需要改变和保留的内容，不要把参考图仅概括成标签重画。"
            "除非用户指定通道，否则 channel 留空。"
        )
        props["reference_image"]["description"] = "本轮或同会话参考图的序号，从 1 开始；默认在线依据原图修改。"
        props["new_scene"]["description"] = "换场景或动作时填 true；在线编辑仍携带原参考图，不要只概括人物标签重画。"
        props["denoise"]["description"] = "仅手动选择本机图生图时使用，范围 0.2 到 0.8；默认在线编辑不使用此参数。"
    return tool


# 画幅预设。别让模型自由填宽高 —— 它会写出 2000x300 这种没法看的比例。# 注意免费档会把总像素压到 ~768² 上限（比例保留），所以这里的数字是「意图」，
# 不是「保证拿到的分辨率」。
SIZE_PRESETS: Dict[str, Tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    # 9:16 竖版，手机竖屏原尺寸（704×1248）。
    #
    # 2026-10-24 加：视频链路要用。原来的 `portrait` 是 832×1216（0.68），
    # 不是 9:16（0.5625）—— 拿去当 seedance 首帧会被裁掉两侧，
    # 构图偏一点就把人裁出去了。704/1248 = 0.564，基本正好。
    # 两个数都是 8 的倍数（88 / 156），ComfyUI 不会因为对齐再改一次。
    "portrait_9x16": (704, 1248),
}
DEFAULT_SIZE = "square"

# 通道 B 的画幅 —— 这里给的是**字符串**，直接进请求体的 `size`。
#
# 为什么默认选这三个：它们是最通用的一组（OpenAI / 硅基流动 Kolors 都认，
# 智谱也认 1024x1024）。但**没有一组尺寸能让所有厂商都满意** ——
# 智谱认 768x1344 却不认 768x1024。所以 `imagegen.api_sizes` 留了口子，
# 用哪家就照哪家的文档覆盖一下，比代码里猜准。
API_SIZE_PRESETS: Dict[str, str] = {
    "square": "1024x1024",
    "portrait": "768x1024",
    "landscape": "1024x768",
}


class ImageGenError(RuntimeError):
    """生图失败。

    `retryable` 用来区分「等会儿再试可能就好了」（限流、超时）
    和「再试也没用」（提示词为空、服务返回的不是图片）。

    `cooldown` 更严格一层：这次失败说明**现在整条链路都不该再打**了
    （402 / 429 限流）。调用方看到它就该把生图功能冷一段时间，
    而不是换条提示词接着撞 —— 越撞恢复得越慢。
    """

    def __init__(
        self, message: str, retryable: bool = False, cooldown: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cooldown = cooldown


def resolve_size(size: Optional[str]) -> Tuple[int, int]:
    key = str(size or "").strip().lower()
    return SIZE_PRESETS.get(key, SIZE_PRESETS[DEFAULT_SIZE])


def resolve_api_size(cfg: ImageGenConfig, size: Optional[str]) -> str:
    """通道 B 的画幅字符串。配置里覆盖过就用配置的，否则用默认那组。"""
    key = str(size or "").strip().lower()
    if not key:
        key = DEFAULT_SIZE
    custom = cfg.api_sizes if isinstance(cfg.api_sizes, dict) else {}
    override = str(custom.get(key) or "").strip()
    if override:
        return override
    return API_SIZE_PRESETS.get(key, API_SIZE_PRESETS[DEFAULT_SIZE])


def build_url(
    cfg: ImageGenConfig,
    prompt: str,
    width: int,
    height: int,
    seed: Optional[int] = None,
) -> str:
    """拼通道 A 的请求地址。抽出来是为了能在测试里断言，不用真发请求。"""
    params: Dict[str, Any] = {
        "width": width,
        "height": height,
        "model": cfg.model,
        "nologo": "true",
        # 不指定 seed 时服务端会固定返回缓存图 ——「再画一张」就永远是同一张。
        # 所以每次随机一个，保证有变化。
        "seed": seed if seed is not None else random.randint(1, 2**31 - 1),
    }
    if cfg.negative_prompt:
        params["negative_prompt"] = cfg.negative_prompt
    if cfg.enhance:
        params["enhance"] = "true"
    return f"{cfg.endpoint.rstrip('/')}/{quote(prompt, safe='')}?{urlencode(params)}"


def build_api_url(cfg: ImageGenConfig) -> str:
    """拼通道 B 的请求地址。

    `api_base` 要带上版本段（`.../v1`、`.../api/paas/v4`），
    不同厂商的版本段写法不一样，所以由配置给全，这里只补最后一段。
    """
    return cfg.api_base.rstrip("/") + "/images/generations"


def build_api_payload(cfg: ImageGenConfig, prompt: str, size: str) -> Dict[str, Any]:
    """通道 B 的请求体。

    刻意只有这四个字段：
      * 不传 `response_format` —— `gpt-image-1` 收到它会直接报错，
        而它本来就返回 b64；dall-e-3 默认给 url，我们反正会去下载。
      * 不传 `negative_prompt` —— 只有硅基流动支持，智谱会拒。
    少传 = 三家都能过。
    """
    return {
        "model": (cfg.api_model or "").strip(),
        "prompt": prompt,
        "size": size,
        "n": 1,
    }


def _api_error_text(raw: bytes) -> str:
    """从出错响应里挖一句人话出来。

    各家的错误结构不一样（`{"error":{"message":...}}` /
    `{"error":"..."}` / `{"message":"..."}` / 直接一段 HTML），
    挖不到就退回截断的原文 —— 总比「HTTP 400」什么都不说强。
    """
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return "响应为空"
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text[:200]

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or err.get("code")
            if msg:
                return str(msg)[:200]
        if isinstance(err, str) and err.strip():
            return err.strip()[:200]
        for key in ("message", "msg", "detail", "error_msg"):
            msg = data.get(key)
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:200]
    return text[:200]


def _extract_api_image(data: Any) -> Tuple[Optional[str], Optional[str]]:
    """从通道 B 的响应里取出图片，返回 `(b64, url)`，两者取其一。

    两家给的字段不一样，所以两种都接：
      * 智谱、硅基流动 → `data[0].url`（图在对象存储上，还得再下一次）
      * OpenAI          → `data[0].b64_json`
    兼容实现五花八门，所以顺带认几个同义字段名。
    """
    items = data.get("data") if isinstance(data, dict) else None
    # 有的实现直接给对象而不是数组
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ImageGenError("生图接口没返回图片（响应里没有 data[0]）")

    first = items[0]
    if isinstance(first, str):
        # 极少数实现直接给一个 base64 字符串
        return first.strip() or None, None
    if not isinstance(first, dict):
        raise ImageGenError("生图接口返回的 data[0] 结构不认识")

    for key in ("b64_json", "b64", "image_base64", "image"):
        value = first.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_data_uri(value.strip()), None

    for key in ("url", "image_url"):
        value = first.get(key)
        if isinstance(value, str) and value.strip():
            return None, value.strip()

    raise ImageGenError("生图接口返回的 data[0] 里既没有 url 也没有 b64_json")


def _strip_data_uri(value: str) -> str:
    """有的实现把 base64 写成 `data:image/png;base64,xxxx`，得把前缀剥掉。"""
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1].strip()
    return value


async def generate(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    prompt: str,
    size: Optional[str] = None,
    seed: Optional[int] = None,
    init_image: Optional[str] = None,
) -> bytes:
    """按提示词生成一张图，返回原始字节。

    只做入口校验和分派 —— 具体走哪条通道由 `cfg.resolved_provider` 决定：
    填了 api_key 走通用通道，否则走 Pollinations 免费档。

    `init_image` 是参考图的 data URI。**只有通用通道可能用得上**
    （且要配 `api_ref_field` 才会真的发出去）；Pollinations 是纯文生图接口，
    传了也会被忽略 —— 那条通道的参数全在 URL 查询串里，塞不进图。

    失败一律抛 `ImageGenError`，调用方拿它去决定怎么跟用户说。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageGenError("提示词为空")

    if not cfg.enable:
        raise ImageGenError("生图功能已关闭")

    if cfg.resolved_provider == "openai":
        return await _generate_openai(session, cfg, prompt, size, init_image=init_image)
    return await _generate_pollinations(session, cfg, prompt, size, seed)


async def _generate_pollinations(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    prompt: str,
    size: Optional[str],
    seed: Optional[int],
) -> bytes:
    """通道 A：GET 一个拼好参数的地址，直接吐图。"""
    width, height = resolve_size(size)
    url = build_url(cfg, prompt, width, height, seed)
    log.info(
        "生图请求（Pollinations）：%dx%d model=%s prompt=%r",
        width,
        height,
        cfg.model,
        prompt[:80],
    )

    timeout = aiohttp.ClientTimeout(total=float(cfg.timeout))
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 402:
                # 免费档的限流是 402 而不是 429，这点很反直觉
                # （2026-09-30 实测：连着请求几张之后，连 512x512 都 402，
                #  等十几秒自己就恢复了）。也可能是请求了要付费的模型。
                raise ImageGenError(
                    "生图服务限流了（免费额度用完，过一会儿才恢复）",
                    retryable=True,
                    cooldown=True,
                )
            if resp.status == 429:
                raise ImageGenError(
                    "生图服务请求太频繁，缓一下", retryable=True, cooldown=True
                )
            if resp.status >= 500:
                raise ImageGenError(f"生图服务出错（HTTP {resp.status}）", retryable=True)
            if resp.status != 200:
                raise ImageGenError(f"生图失败（HTTP {resp.status}）")

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                # 出错时它会返回一小段 JSON 或纯文本，不是图片
                body = (await resp.content.read(300)).decode("utf-8", "replace")
                raise ImageGenError(f"生图服务返回的不是图片（{ctype or '无类型'}）：{body[:120]}")

            declared = resp.content_length
            raw = await resp.read()

            # 声明了多少字节就必须收到多少 —— 生成服务同样会中途断流。
            # 这一步只是第一道；真正兜底的是 prepare() 的解码校验。
            if declared and len(raw) < declared:
                raise ImageGenError(
                    f"生图响应不完整（声明 {declared} 字节，实收 {len(raw)} 字节）",
                    retryable=True,
                )
    except ImageGenError:
        raise
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"连不上生图服务：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"生图请求异常：{exc}", retryable=True) from exc

    if len(raw) < MIN_IMAGE_BYTES:
        # 见过 2 字节的 402 响应体 —— 别把它当图片往下传
        raise ImageGenError("生图服务返回的内容过短，多半是错误响应", retryable=True)

    log.info("生图完成（Pollinations）：%d 字节", len(raw))
    return raw


async def _generate_openai(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    prompt: str,
    size: Optional[str],
    init_image: Optional[str] = None,
) -> bytes:
    """通道 B：POST 一个 JSON，从响应里把图取出来。

    图可能是 base64（OpenAI）也可能是 URL（智谱、硅基流动），
    两种情况在这里统一成字节 —— 上层不用关心。

    `init_image` 是**参考图的 data URI**。默认不发这个字段，因为多数接口收到
    不认识的参数会直接 400（智谱就会拒 `negative_prompt`，同理）。
    所以我们**按配置**决定发不发、字段叫什么 —— 不同家的名字不一样：

      · 火山方舟 / 豆包 Seedream 系列 → `image`（支持「在参考图基础上修改」）
      · 其他家可能用 `image`、`init_image`、`reference_image` 等

    配置 `imagegen.api_ref_field` 填字段名后才会带上。
    填错的表现是接口报「未知参数」，那时改这个配置即可，不用动代码。
    """
    api_key = (cfg.api_key or "").strip()
    if not api_key:
        raise ImageGenError("通用生图通道没配 api_key", retryable=False)
    if not cfg.api_base.startswith(("http://", "https://")):
        raise ImageGenError(
            f"imagegen.api_base 不是合法地址：{cfg.api_base or '空'}", retryable=False
        )
    model = (cfg.api_model or "").strip()
    if not model:
        raise ImageGenError("通用生图通道没配模型名（imagegen.api_model）", retryable=False)

    size_str = resolve_api_size(cfg, size)
    url = build_api_url(cfg)
    payload = build_api_payload(cfg, prompt, size_str)
    ref_field = (getattr(cfg, "api_ref_field", "") or "").strip()
    if init_image and ref_field:
        # 方舟这批模型的 `image` 同时接受 URL 和 base64 data URI，
        # 我们手上就是 data URI，直接塞进去，省一次上传。
        payload[ref_field] = init_image
        log.info("通用通道带参考图（字段 %s，%d 字节）", ref_field, len(init_image))
    elif init_image:
        log.info(
            "通用通道没配参考图字段（imagegen.api_ref_field），"
            "本次忽略参考图、按纯文生图出"
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    log.info(
        "生图请求（通用通道）：%s model=%s size=%s 参考图=%s prompt=%r",
        url,
        model,
        size_str,
        "有" if (init_image and ref_field) else "无",
        prompt[:80],
    )

    timeout = aiohttp.ClientTimeout(total=float(cfg.timeout))
    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            # 无论成败都先读 body —— 出错时那句 message 才是最有用的东西。
            raw = await resp.read()

            if resp.status in (401, 403):
                raise ImageGenError(
                    f"生图接口鉴权失败（HTTP {resp.status}）：{_api_error_text(raw)}"
                    " —— 检查 imagegen.api_key",
                    retryable=False,
                )
            if resp.status == 402:
                # 通道 B 的 402 是余额/额度，不是限流 —— 重试也没用
                raise ImageGenError(
                    f"生图接口返回 402：{_api_error_text(raw)} —— 多半是余额或额度用完了",
                    retryable=False,
                )
            if resp.status == 429:
                raise ImageGenError(
                    f"生图接口限流（HTTP 429）：{_api_error_text(raw)}",
                    retryable=True,
                    cooldown=True,
                )
            if resp.status >= 500:
                raise ImageGenError(
                    f"生图接口出错（HTTP {resp.status}）：{_api_error_text(raw)}",
                    retryable=True,
                )
            if resp.status != 200:
                err_text = _api_error_text(raw)
                # 400 绝大多数是参数问题（尺寸不支持、模型名写错）—— 重试无用。
                #
                # **但有一类例外：内容审核的随机误判。** 实测（豆包 Seedream 5.0-pro）
                # 同一张参考图 + 同一句提示词会随机返回
                #     HTTP 400: The output image may be related to copyright restrictions
                # 而**同条件重试就能成功**（实测 7 次里挂 1 次，重试 2 次全过）。
                # 归到「不可重试」的话，那一次用户请求就直接废掉了 ——
                # 实测线上就是这么挂的：一次偶发拦截 = 用户白等 90 秒后拿到"画不出来"。
                #
                # 所以按关键词把这一类挑出来标成可重试。判据用宽泛的词，
                # 各家措辞不同（copyright / content policy / moderation / risk ...）。
                _low = err_text.lower()
                _moderation = any(
                    k in _low for k in (
                        "copyright", "content policy", "content_policy",
                        "moderation", "safety", "risk control", "sensitive",
                    )
                )
                raise ImageGenError(
                    f"生图失败（HTTP {resp.status}）：{err_text}",
                    retryable=_moderation,
                )
    except ImageGenError:
        raise
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"连不上生图接口：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"生图请求异常：{exc}", retryable=True) from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise ImageGenError(
            f"生图接口返回的不是 JSON：{raw[:120].decode('utf-8', 'replace')}"
        ) from exc

    # 有的实现 HTTP 200 但 body 里带错误 —— 这种坑不查会当成「没返回图片」
    if isinstance(data, dict) and data.get("error") and not data.get("data"):
        raise ImageGenError(f"生图接口报错：{_api_error_text(raw)}", retryable=False)

    # 记下这次实际消耗的 token（方舟会在响应里给 usage）。
    # 免费额度是按量折算的，所以这个数比"张数"更能反映真实消耗。
    global _last_api_tokens
    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
        try:
            _last_api_tokens = int(data["usage"].get("total_tokens") or 0)
        except (TypeError, ValueError):
            _last_api_tokens = 0

    b64, image_url = _extract_api_image(data)

    if b64:
        try:
            image_bytes = base64.b64decode(b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ImageGenError(f"生图接口返回的 base64 解不开：{exc}") from exc
        log.info("生图完成（通用通道 / base64）：%d 字节", len(image_bytes))
    else:
        image_bytes = await _download_image(session, image_url, cfg.timeout)
        log.info("生图完成（通用通道 / URL）：%d 字节", len(image_bytes))

    if len(image_bytes) < MIN_IMAGE_BYTES:
        raise ImageGenError("生图接口返回的内容过短，多半是错误响应", retryable=True)

    return image_bytes


async def _download_image(
    session: aiohttp.ClientSession, url: str, timeout_seconds: float
) -> bytes:
    """把通道 B 给的图片 URL 下载下来。

    智谱、硅基流动都是「先给一个对象存储链接」，所以这一步是必经之路。
    链接是临时签名 URL，不保证多久失效 —— 拿到就赶紧下。
    """
    if not str(url or "").startswith(("http://", "https://")):
        raise ImageGenError(f"生图接口给的图片地址不合法：{url}")

    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise ImageGenError(
                    f"下载生成的图失败（HTTP {resp.status}）", retryable=True
                )
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            # 对象存储的 Content-Type 有时是 application/octet-stream，所以只挡
            # 「明确是文本」的情况 —— 那一定是错误页而不是图。
            if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
                body = (await resp.content.read(200)).decode("utf-8", "replace")
                raise ImageGenError(
                    f"图片地址返回的不是图（{ctype}）：{body[:120]}", retryable=True
                )
            raw = await resp.read()
    except ImageGenError:
        raise
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"下载生成的图失败：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"下载生成的图异常：{exc}", retryable=True) from exc

    if len(raw) < MIN_IMAGE_BYTES:
        raise ImageGenError("下载到的图片过短，多半是错误响应", retryable=True)
    return raw


# --------------------------------------------------------------------------- #
# 通道 C：本机 ComfyUI
#
# 存在的理由：A 是免费额度（限流紧、768² 上限、带水印），B 按张计费，
# 而二次元画风只有本地 SDXL 底模（Illustrious XL 这类）出得来 ——
# 不花钱、不限量、无水印，还能靠 LoRA 固定画风。
#
# 硬前提：**机器得开着、ComfyUI 得在跑**。而这台机器是会关机的，
# 所以这条通道从设计上就**永远带降级**：探不到服务、超时、图解不开，
# 一律退回 A / B。它不是「首选且唯一」，是「能用就赚到」。
# --------------------------------------------------------------------------- #

# 探测结果缓存：url -> (过期时间戳, 是否存活)。
# 机器关着时探测是毫秒级失败（连接直接被拒），但一轮里可能连着问几次，
# 缓存一下省掉重复请求。TTL 很短，ComfyUI 一关下次请求自然就降级了。
_COMFYUI_PROBE: Dict[str, Tuple[float, bool]] = {}

# 探测只回答「服务在不在」，用短超时就够了，别跟出图一样等 120 秒。
_PROBE_TIMEOUT = 3.0
# 轮询出图结果的间隔。1.5 秒是折中：太急刷请求，太慢让用户多等。
_POLL_INTERVAL = 1.5
# 出图**失败**后多久内不再试本地。和探测 TTL 分开是有原因的：
# 探测失败很便宜（毫秒级），但「服务活着却出图超时」是**等满 120 秒**才失败的 ——
# 不记住这一点，群里连着要三张图就要干等六分钟。
_COMFYUI_DOWN_TTL = 60.0


def _comfyui_base(cfg: ImageGenConfig) -> str:
    return (cfg.comfyui_url or "").strip().rstrip("/")


def _mark_comfyui_down(cfg: ImageGenConfig, seconds: float = _COMFYUI_DOWN_TTL) -> None:
    """记住「本地通道现在不能用」，seconds 秒内别再试。"""
    _COMFYUI_PROBE[_comfyui_base(cfg)] = (time.time() + max(0.0, seconds), False)


async def probe_comfyui(session: aiohttp.ClientSession, cfg: ImageGenConfig) -> bool:
    """本机 ComfyUI 还活着吗。带 TTL 缓存，失败是常态所以只记 INFO。"""
    base = _comfyui_base(cfg)
    now = time.time()
    cached = _COMFYUI_PROBE.get(base)
    if cached and cached[0] > now:
        return cached[1]

    alive = False
    try:
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT)
        async with session.get(f"{base}/system_stats", timeout=timeout) as resp:
            alive = resp.status == 200
    except Exception:  # noqa: BLE001
        # 连不上、超时、解析失败 —— 对探测来说都一样：不在。
        alive = False

    _COMFYUI_PROBE[base] = (now + max(0.0, float(cfg.comfyui_probe_ttl)), alive)
    if not alive:
        # 机器关机是预期内的，用 WARNING 会让人以为坏了。
        log.info("本机 ComfyUI 探测不到（%s）—— 本次走降级通道", base)
    return alive


async def _comfyui_checkpoint(session: aiohttp.ClientSession, cfg: ImageGenConfig) -> str:
    """决定用哪个底模。配了就用配的，没配就问 ComfyUI 现在加载了哪些，取第一个。

    不去猜文件名 —— 猜错只会白白多等一轮采样（几十秒），不如直接失败降级。
    """
    name = (cfg.comfyui_checkpoint or "").strip()
    if name:
        return name

    base = _comfyui_base(cfg)
    timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 2)
    async with session.get(
        f"{base}/object_info/CheckpointLoaderSimple", timeout=timeout
    ) as resp:
        raw = await resp.read()

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise ImageGenError(f"读不懂 ComfyUI 的模型列表：{raw[:120]}") from exc

    node = data.get("CheckpointLoaderSimple") or {}
    required = ((node.get("input") or {}).get("required") or {})
    options = (required.get("ckpt_name") or [[]])[0]
    if not options:
        raise ImageGenError("ComfyUI 里没有可用 checkpoint（models/checkpoints 是空的）")
    return str(options[0])


async def list_comfyui_checkpoints(
    session: aiohttp.ClientSession, cfg: ImageGenConfig
) -> List[str]:
    """问 ComfyUI 现在有哪些底模。**拿不到就返回空列表**，不抛异常 ——

    这个函数是给 `/model` 指令列清单用的，列不出来顶多回一句"读不到"，
    不该让指令本身失败。

    2026-10-10 起同时查 `CheckpointLoaderSimple`（SDXL 系，models/checkpoints）
    和 `UNETLoader`（diffusion_models 架构，models/diffusion_models，如 Anima），
    两边合并 —— `/model` 的模糊匹配按文件名走，不关心架构。
    """
    found: List[str] = []
    try:
        base = _comfyui_base(cfg)
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 2)
        for node_name, field in (
            ("CheckpointLoaderSimple", "ckpt_name"),
            ("UNETLoader", "unet_name"),
        ):
            try:
                async with session.get(
                    f"{base}/object_info/{node_name}", timeout=timeout
                ) as resp:
                    raw = await resp.read()
                data = json.loads(raw.decode("utf-8", "replace"))
                node = data.get(node_name) or {}
                required = ((node.get("input") or {}).get("required") or {})
                options = (required.get(field) or [[]])[0]
                found.extend(str(o) for o in options)
            except Exception:  # noqa: BLE001
                log.debug("读取 %s 模型列表失败", node_name, exc_info=True)
        return sorted(set(found))
    except Exception:  # noqa: BLE001
        log.debug("读取 ComfyUI 底模列表失败", exc_info=True)
        return []


async def list_comfyui_loras(
    session: aiohttp.ClientSession, cfg: ImageGenConfig
) -> List[str]:
    """问 ComfyUI 现在有哪些 LoRA。拿不到就返回空列表，不抛异常。

    **注意：和底模不一样，LoRA 不做「自动取第一个」。**
    底模留空时自动取第一个是安全的 —— 只有一两个，且出图风格是可预期的。
    LoRA 不一样：挂错一个 LoRA 会**直接改变角色长相**，而且从图上看不出
    「挂错了」还是「模型不行」。所以 `comfyui_lora` 必须显式写文件名，
    留空就是不挂 —— 宁可没有，也不要随机挂一个。
    """
    try:
        base = _comfyui_base(cfg)
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 2)
        async with session.get(
            f"{base}/object_info/LoraLoader", timeout=timeout
        ) as resp:
            raw = await resp.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        node = data.get("LoraLoader") or {}
        required = ((node.get("input") or {}).get("required") or {})
        options = (required.get("lora_name") or [[]])[0]
        return [str(o) for o in options]
    except Exception:  # noqa: BLE001
        log.debug("读取 ComfyUI LoRA 列表失败", exc_info=True)
        return []


# 底模预设：**切底模必须连参数一起换**。
#
# 为什么不能只换文件名 —— `style_weight`（IP-Adapter 强度）是**跟底模的响应
# 强度绑定的**。2026-10-01 实测，同一个 0.65：
#   · animagine-xl-3.1        → 温和可用，风格明显但画面干净
#   · Illustrious 系（wai-illustrious-sdxl-v17）→ **冲爆成彩虹色高饱和、五官崩坏**，
#     扫过 0.2 / 0.35 / 0.5 只有 **0.2** 干净
# 差 3 倍。沿用旧底模的标定值 = 直接产出一堆废图。
#
# 换新底模时**必须重新标定**再往这张表里加，别照抄别的模型的数。
COMFYUI_MODEL_PRESETS: List[Dict[str, Any]] = [
    {
        "match": ("illustrious", "noobai", "wai"),
        "label": "Illustrious 系（写实向动漫）",
        "style_weight": 0.2,
        "style_weight_max": 0.3,   # 实测 0.35 就崩
        "sampler": "dpmpp_2m_sde",
        "scheduler": "karras",
        # 2026-10-07 实测标定（同 prompt、同 seed、512x512、28 步）：
        # karras 下 CFG 对饱和度的影响是**非线性**的，5.5 → 6.0 是断崖 ——
        #   cfg4.0  S=0.365   过饱和像素(S>0.8)  3.8%
        #   cfg4.5  S=0.407                      10.8%
        #   cfg5.0  S=0.422                      13.3%   ← 选它
        #   cfg5.5  S=0.465                      18.6%
        #   cfg6.0  S=0.653                      49.4%   ← 原值，用户反馈「颜色太鲜艳」
        # 6.0 那档不只是艳，**构图还会跑偏**（同 seed 从半身特写变成全身远景）。
        # 顺带排除两个嫌疑：`very aesthetic` 只占 3%（0.653→0.631），不是主因；
        # 换 `scheduler=normal` 也能压到 0.386，但会丢掉 karras 的画质优势，
        # 所以保留 karras、只降 CFG。
        "cfg": 5.0,
        "steps": 28,
    },
    {
        "match": ("animagine",),
        "label": "Animagine XL 3.1（经典动漫风）",
        "style_weight": 0.65,
        "style_weight_max": 0.7,
        # 2026-10-03 实测标定（同 prompt、同 seed、1024x1024、28 步）：
        #   dpmpp_2m_sde + karras cfg6（= Illustrious 的配置）
        #       → **脸过曝发糊、合十的手糊成一团肉块**
        #   euler_ancestral + normal cfg6  → 手清楚、脸有层次、色彩正常 ← 选它
        #   euler_ancestral + normal cfg5  → 偏平、对比弱
        #   euler_ancestral + normal cfg7  → 色彩更浓，也可用
        #   dpmpp_2m + karras cfg6         → 手不错但脸略僵
        # 另外单独验了 VAE：**内置 VAE 更亮更灰**，外置 sdxl-vae-fp16-fix 更好，
        # 所以这里不覆盖 `comfyui_vae`，沿用配置里的外置那个。
        #
        # ---- 2026-10-22 修正：CFG 6.0 → 4.5 ----
        #
        # 用户反馈「anima 的手部比 animagine 好特别多」。专门测了双手捧杯
        # （`tests/_exp_hands.py`，832x1216，28 步，只改 CFG）：
        #
        #   cfg 3.5 → 双手结构清晰，但整体偏平、对比弱
        #   cfg 4.5 → 双手清楚、脸有层次、色彩正常   ← 选它
        #   cfg 6.0 → **左手糊成一团黑影、多画出第三只手**
        #   anima cfg1.0（对照）→ 双手位置自然，还多给了托盘
        #
        # 10-03 那次「手清楚」的结论只在**手部动作简单**时成立。动作一复杂
        # （双手 + 持物），CFG 6.0 就开始放大错误：高 CFG 强化 prompt 对齐的
        # 同时，也把模型自己的错误模式一起放大了。
        #
        # 代价：降 CFG 会略微削弱 prompt 对齐（角色标签权重变弱）。
        # 如果发现画角色不够像，先试 5.0~5.5，别直接回 6.0。
        "sampler": "euler_ancestral",
        "scheduler": "normal",
        "cfg": 4.5,
        "steps": 28,
    },
    {
        # ⚠️ 必须排在 animagine 之后：匹配是子串匹配，"anima" 是
        # "animagine" 的前缀，放前面会把 animagine 抢走。
        # 用三个完整前缀做 match 也是为了把这个隐患钉死。
        "match": ("anima-turbo", "anima-aesthetic", "anima-base"),
        "label": "Anima（2B 轻量动漫，Turbo v1.1）",
        # ---- 架构标记：非 SDXL ----
        # Anima 是 Cosmos-Predict2 底座 + Qwen3 编码器，工作流走
        # UNETLoader + CLIPLoader(type=qwen_image) + qwen VAE（见
        # _build_diffusion_workflow）。SDXL 生态的 LoRA / IP-Adapter /
        # 外置 SDXL VAE 在它身上**全部不适用**，所以三个可选模块
        # 在 _generate_comfyui 的架构分支里被整体禁用。
        "architecture": "diffusion",
        "style_weight": 0.0,
        "style_weight_max": 0.0,   # 架构分支直接忽略风格图，这里是给钳制兜底
        "sampler": "er_sde",
        "scheduler": "simple",
        # 2026-10-10 按官方 README 标定（Turbo 蒸馏版）：CFG 1、8~12 步。
        # CFG 1 = 不用负面引导，negative 接了也不起作用。
        # 首图实测（1024²，er_sde+simple）：28s 含首次加载；
        # 饱和度 S=0.325，比调参后的 wai（0.422）更柔和，过饱和像素 0.1%。
        "cfg": 1.0,
        # 2026-10-23 从 10 改到 20。
        #
        # 起因：做短视频首帧时发现画面**满屏噪点/横向扫描线纹理**，
        # 完全不像动漫截图。排查过负面词（CFG 1 时负面根本不起作用，
        # 加多少都白费）、prompt 长度（精简到 12 个标签也一样），
        # 最后定位在步数：
        #
        #   10 步 → 噪点明显，有横向纹理
        #   20 步 → **噪点基本消失，手清晰** ← 选它
        #   28 步 → **反而崩**：整体发灰过曝、手糊成一条线
        #
        # 28 步崩掉说明 turbo 蒸馏模型**不能按普通 SDXL 的思路加步数**，
        # 20 已经是上限。代价是单张从 9 秒涨到 17 秒，可以接受。
        "steps": 20,
        # 官方 README：positive 用 masterpiece, best quality 前缀。
        # 覆盖 config 里 Illustrious 系的那串质量标签（very aesthetic 等
        # 是 Danbooru score 标签，对 Anima 是未标定输入，不赌）。
        "positive_prefix": "masterpiece, best quality",
        # 文本编码器与 VAE 是 Anima 专属文件，跟 preset 走而不是跟 config 走
        # （config.comfyui_vae 里那个 sdxl-vae-fp16-fix 对 Anima 无效）。
        "clip_file": "qwen_3_06b_base.safetensors",
        "vae_file": "qwen_image_vae.safetensors",
    },
    {
        # ⚠️ match 里别写裸 "z_image" 之外的短词 —— 和 anima 那条一样，
        # 子串匹配容易误伤。
        "match": ("z_image", "z-image", "zimage"),
        "label": "Z-Image Turbo（6B，中文提示词最强）",
        # ---- 架构标记：非 SDXL，且**和 Anima 也不一样** ----
        # Z-Image 是 6B DiT（lumina2 系）+ Qwen3-4B 编码器 + 专用 ae VAE。
        # 接线差异见 `_build_zimage_workflow` 的 docstring。
        "architecture": "zimage",
        "style_weight": 0.0,
        "style_weight_max": 0.0,
        # 2026-10-25 按官方 workflow 标定
        # （Comfy-Org/workflow_templates/templates/image_z_image_turbo.json）：
        #   KSampler: steps=8, cfg=1, sampler=res_multistep, scheduler=simple
        #   ModelSamplingAuraFlow: shift=3
        # 实测（1024²，nvfp4 + fp4 编码器，RTX 5060 Laptop 8G）：
        #   首张 49.8s（含加载），后续 27.2s/张，显存峰值 7.15~7.32 GB。
        "sampler": "res_multistep",
        "scheduler": "simple",
        # CFG 1 = 不走负面引导。负向分支在 workflow 里用
        # ConditioningZeroOut 直接清零，不接负向文本编码。
        "cfg": 1.0,
        "steps": 8,
        # Z-Image 官方 prompt 是自然语言长句，**不吃** Danbooru 质量标签。
        # 空串 = 不加前缀（None 才会回落到 config.comfyui_positive）。
        "positive_prefix": "",
        # 编码器与 VAE 跟预设走：config 里那套 SDXL 的对它无效。
        "clip_file": "qwen_3_4b_fp4_mixed.safetensors",
        "vae_file": "ae.safetensors",
    },
]

# 匹配不上就用这个 —— **宁可风格弱一点，也别崩画面**。
# 未知底模的响应强度无从得知，保守取 Illustrious 那一档的值。
#
# 采样器 / 调度器留空 = **不覆盖配置里的值**（用 `imagegen.comfyui_sampler`）。
# 因为对未知底模来说，配置里那套至少是用户自己在用的，比我们瞎猜强。
DEFAULT_MODEL_PRESET: Dict[str, Any] = {
    "label": "未知底模",
    "style_weight": 0.2,
    "style_weight_max": 0.3,
    "sampler": "",
    "scheduler": "",
    "cfg": None,
    "steps": None,
}

# Anima LLLite ControlNet 的补丁映射（2026-10-25 加）。
#
# 文件在 `ComfyUI/models/model_patches/`，由 `ModelPatchLoader` 加载、
# `AnimaLLLiteApply` 挂到 model 上。**不是 controlnet/ 目录** ——
# LLLite 是 model patch，放错目录 ComfyUI 直接认不到。
#
# canny 复用 lineart 补丁：LLLite 的 lineart 吃的就是边缘图，而 Canny 产出的
# 正是边缘图，输入形态一致，没必要单独一个补丁。
CONTROLNET_PATCHES: Dict[str, str] = {
    "pose": "anima-lllite-pose-1.safetensors",
    "lineart": "anima-lllite-lineart-1.safetensors",
    "depth": "anima-lllite-depth-1.safetensors",
    "scribble": "anima-lllite-scribble-1.safetensors",
    "canny": "anima-lllite-lineart-1.safetensors",
}


def model_preset(checkpoint: str) -> Dict[str, Any]:
    """按文件名挑预设。认不出就用保守默认。"""
    low = str(checkpoint or "").lower()
    for preset in COMFYUI_MODEL_PRESETS:
        if any(key in low for key in preset["match"]):
            return preset
    return DEFAULT_MODEL_PRESET


def match_checkpoint(query: str, available: List[str]) -> Optional[str]:
    """把用户输入的短名模糊匹配到底模文件名。

    `/model wai` → `wai-illustrious-sdxl-v17.safetensors`
    `/model animagine` → `animagine-xl-3.1.safetensors`

    匹配规则（按优先级）：
      ① 完全相等（不区分大小写）
      ② 文件名**去掉扩展名**后完全相等
      ③ 文件名包含查询串（子串）
      ④ 查询串包含文件名主干（防止用户写得太长）
    多条命中时取**最短**的那个 —— 用户写得越短通常越明确，
    而最短的文件名最不容易是巧合命中的。

    ## 分隔符归一化（2026-10-25 加）

    文件名是 `z_image_turbo_nvfp4.safetensors`，但没人会这么打。实测用户会写
    `z-image` / `zimage` / `z image`，按字面匹配**这三种全部认不出**，
    只有 `z_image` 能用 —— 等于强迫用户背文件名。

    所以比较前做两级归一化：
      · `_sep`    —— `-` 和空格统一成 `_`      → `z-image` 命中
      · `_squash` —— 再把下划线也去掉           → `zimage` 命中
    原样比较保留在最前面，精确输入永远优先，不会被归一化抢走。

    返回 None = 认不出来，由调用方回一句"有这些，你要哪个"。
    """
    q = str(query or "").strip().lower()
    if not q:
        return None

    def _sep(s: str) -> str:
        return str(s).lower().replace("-", "_").replace(" ", "_")

    def _squash(s: str) -> str:
        return _sep(s).replace("_", "")

    names = list(available or [])
    qs, qq = _sep(q), _squash(q)

    # ①② 完全相等（原样 / 归一化 / 去分隔符），带扩展名和去扩展名各比一遍
    for name in names:
        low = name.lower()
        if low == q or _sep(low) == qs or _squash(low) == qq:
            return name
    for name in names:
        stem = name.rsplit(".", 1)[0]
        if _sep(stem) == qs or _squash(stem) == qq:
            return name

    # ③④ 子串（同样走归一化）
    hits = [n for n in names if qs in _sep(n)]
    if not hits:
        hits = [n for n in names if qq in _squash(n)]
    if not hits:
        hits = [
            n for n in names
            if _sep(n.rsplit(".", 1)[0]) in qs or qs in _sep(n.rsplit(".", 1)[0])
        ]
    if not hits:
        return None
    return sorted(hits, key=len)[0]


def _build_diffusion_workflow(
    cfg: ImageGenConfig,
    prompt: str,
    width: int,
    height: int,
    checkpoint: str,
    seed: int,
    init_image: Optional[str] = None,
    denoise: float = 1.0,
    clip_file: str = "",
    vae_file: str = "",
    lora_file: str = "",
    lora_strength: Optional[float] = None,
    control_mode: str = "",
    control_image: str = "",
    control_strength: float = 1.0,
) -> Dict[str, Any]:
    """Anima 系（diffusion_models 架构）工作流，和 SDXL 版是完全不同的接线。

    差异点：
      · 模型走 `UNETLoader`（文件在 models/diffusion_models），不是 CheckpointLoaderSimple
      · 文本编码器是**独立文件**（Qwen3 0.6B），`CLIPLoader type="qwen_image"` 装载
      · 模型**不带内置 VAE**，必须挂 qwen_image_vae（编码和解码用同一个）
    采样参数（Turbo: CFG 1 / 10 步 / er_sde / simple）由预设表通过 `cfg = replace()`
    覆盖后传进来，这里只管接线。

    ## LoRA（2026-10-24 加）

    以前这里写着「不支持 LoRA」—— 那是对的，**当时手上只有 SDXL LoRA**，
    键名 `lora_unet_*` 挂在 28-block 的 Anima DiT 上会被 ComfyUI 静默忽略。

    现在用 `networks.lora_anima` 训了 Anima 原生 LoRA（同样是 `lora_unet_*` 前缀，
    但 shape 对得上 28 blocks / 2048 dim），可以挂了。

    ⚠️ **只认 anima 版**。判断在调用方（`_generate_comfyui` 的架构分支）做完，
    这里不再防御 —— 但如果有人绕过调用方直接调本函数，挂 SDXL LoRA 不会报错，
    只会静默无效果。这是 ComfyUI 的行为，不是这里能拦的。

    LoRA 接在 UNETLoader 之后、KSampler 之前，clip 侧同样串一层
    （Anima 的 LoRA 是 DiT-only，`lora_te_*` 不存在，clip 那路其实是空转，
    但 LoraLoader 要求两个输入都接，接上不会有副作用）。

    **不支持** IP-Adapter / 外置 SDXL VAE —— SDXL 生态对 Anima 无效。

    图生图（init_image）理论上可用（VAEEncode 接同一个 qwen VAE），
    但 2026-10-10 只实测过文生图 —— 图生图出问题先查这里。
    """
    graph: Dict[str, Any] = {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": checkpoint, "weight_dtype": "default"},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["14", 0]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": cfg.comfyui_negative, "clip": ["14", 0]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": int(cfg.comfyui_steps),
                "cfg": float(cfg.comfyui_cfg),
                "sampler_name": cfg.comfyui_sampler,
                "scheduler": cfg.comfyui_scheduler,
                "denoise": max(0.0, min(1.0, float(denoise))),
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["13", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "QQBot"},
        },
        "13": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_file},
        },
        "14": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": clip_file, "type": "qwen_image", "device": "default"},
        },
    }

    # ---- LoRA：接在 UNETLoader 之后、KSampler 之前 ----
    #
    # 显式用 model_ref / clip_ref 覆盖下面的接线，而不是去改上面 dict 里
    # 写死的 ["4", 0] / ["14", 0] —— 和 SDXL 分支（graph["12"] 那段）同一套写法，
    # 免得两处逻辑分叉。
    model_ref = ["4", 0]
    clip_ref = ["14", 0]
    lora_name = (lora_file or "").strip()
    if lora_name:
        strength = float(
            cfg.comfyui_lora_strength if lora_strength is None else lora_strength
        )
        # 和 SDXL 分支一样钳到 [-2, 2]：超范围只会出废图
        strength = max(-2.0, min(2.0, strength))
        graph["15"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["4", 0],
                "clip": ["14", 0],
                "lora_name": lora_name,
                "strength_model": strength,
                "strength_clip": strength,
            },
        }
        model_ref = ["15", 0]
        clip_ref = ["15", 1]
        log.info(
            "Anima 挂 LoRA：%s（strength %.2f）", lora_name, strength
        )

    graph["6"]["inputs"]["clip"] = clip_ref
    graph["7"]["inputs"]["clip"] = clip_ref

    # ---- ControlNet（Anima LLLite，2026-10-25 加）----
    #
    # 接在 LoRA **之后**、KSampler 之前。LLLite 是 model patch，作用于最终
    # 模型；顺序反了（先 patch 后 LoRA）会让 LoRA 的权重把 patch 影响盖掉。
    #
    # 控制图走 `reference_image` 那条上传链路（LoadImage 只认 ComfyUI input
    # 目录里的文件名，调用方已经上传过）。
    #
    # ⚠️ 只认 Anima 系。Z-Image 走 `_build_zimage_workflow`，那边没有这段 ——
    # 补丁是 anima-lllite-*，挂到别的架构上是静默无效。
    if control_mode and control_image:
        patch_name = CONTROLNET_PATCHES.get(control_mode)
        if patch_name:
            graph["30"] = {
                "class_type": "ModelPatchLoader",
                "inputs": {"name": patch_name},
            }
            graph["31"] = {
                "class_type": "LoadImage",
                "inputs": {"image": control_image, "upload": "image"},
            }
            control_src = ["31", 0]
            if control_mode == "canny":
                # 从原图提边缘，再喂给 lineart 补丁。
                # 阈值用 ComfyUI 默认（0.4 / 0.8）：实测 0.3/0.7 会把皮肤纹理、
                # 布料褶皱全当边缘带进来，画面变脏；0.5/0.9 又丢细线条。
                graph["32"] = {
                    "class_type": "Canny",
                    "inputs": {
                        "image": ["31", 0],
                        "low_threshold": 0.4,
                        "high_threshold": 0.8,
                    },
                }
                # ⚠️ 必须反相。Canny 输出**黑底白线**，而 LLLite 的 lineart
                # 补丁是在**白底黑线**的线稿上训的 —— 不反相的话模型看到的
                # 是一张几乎全黑的图，控制信号退化成噪声。
                # 2026-10-25 实测：不反相出图完全不受控（画面只多几根红线，
                # 构图完全跑偏）。这一行是踩出来的，别删。
                graph["34"] = {
                    "class_type": "ImageInvert",
                    "inputs": {"image": ["32", 0]},
                }
                control_src = ["34", 0]
            graph["33"] = {
                "class_type": "AnimaLLLiteApply",
                "inputs": {
                    "model": model_ref,
                    "model_patch": ["30", 0],
                    "image": control_src,
                    "strength": max(-10.0, min(10.0, float(control_strength))),
                    "start_percent": 0.0,
                    "end_percent": 1.0,
                },
            }
            model_ref = ["33", 0]
            log.info(
                "挂 ControlNet：mode=%s patch=%s 强度=%.2f 控制图=%s",
                control_mode, patch_name, float(control_strength), control_image,
            )
        else:
            log.warning(
                "comfyui_controlnet_mode=%r 不认识，这一轮不挂 ControlNet（可选：%s）",
                control_mode, ", ".join(sorted(CONTROLNET_PATCHES)),
            )

    graph["3"]["inputs"]["model"] = model_ref

    if init_image:
        # 图生图：和 SDXL 版同一套路 —— 空 latent 换成参考图编码。
        # 编码用 13 号 qwen VAE，和解码保持同一个（否则色调漂移）。
        graph["20"] = {
            "class_type": "LoadImage",
            "inputs": {"image": init_image, "upload": "image"},
        }
        graph["21"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["20", 0], "vae": ["13", 0]},
        }
        graph["3"]["inputs"]["latent_image"] = ["21", 0]
        graph.pop("5", None)

    return graph


def _build_zimage_workflow(
    cfg: ImageGenConfig,
    prompt: str,
    width: int,
    height: int,
    checkpoint: str,
    seed: int,
    init_image: Optional[str] = None,
    denoise: float = 1.0,
    clip_file: str = "",
    vae_file: str = "",
) -> Dict[str, Any]:
    """Z-Image Turbo 工作流（6B lumina2 系 DiT）。

    **和 Anima 那套不是同一个接线**，四处硬差异，照抄 Anima 会直接报错：

      1. `CLIPLoader type="lumina2"`，不是 `qwen_image`
      2. latent 用 `EmptySD3LatentImage`，不是 `EmptyLatentImage`
         （Z-Image 走 SD3 式 16 通道 latent，用错节点是 shape 不匹配）
      3. 模型和采样器之间必须插 `ModelSamplingAuraFlow(shift=3)`
         —— lumina2 系的时间步偏移，漏了出灰雾图
      4. CFG=1，负向不参与计算，官方模板用 `ConditioningZeroOut` 把正向
         清零后当负向喂，省一次文本编码

    参数来源：`Comfy-Org/workflow_templates/templates/image_z_image_turbo.json`
    （2026-10-25 拉的原文件）。

    ⚠️ 官方模板**不挂** `z_image_turbo_distill_patch_lora_bf16` ——
    nvfp4 主模型已内置蒸馏，手动挂反而可能破坏效果。这里同样不挂。

    图生图用 ae VAE 编码（和 VAEDecode 同一个，否则色调漂移）。
    **图生图未实测** —— 2026-10-25 只验了文生图，出问题先查这里。
    """
    # 官方值。写死而不是走 config，是因为它属于架构常量而非用户偏好 ——
    # 改了就不是 Z-Image 了。
    shift = 3.0

    graph: Dict[str, Any] = {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": checkpoint, "weight_dtype": "default"},
        },
        "10": {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": ["4", 0], "shift": shift},
        },
        "14": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip_file,
                "type": "lumina2",
                "device": "default",
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["14", 0]},
        },
        "7": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["6", 0]},
        },
        "5": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": int(cfg.comfyui_steps),
                "cfg": float(cfg.comfyui_cfg),
                "sampler_name": cfg.comfyui_sampler,
                "scheduler": cfg.comfyui_scheduler,
                "denoise": max(0.0, min(1.0, float(denoise))),
                "model": ["10", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["13", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "QQBot"},
        },
        "13": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_file},
        },
    }

    if init_image:
        graph["20"] = {
            "class_type": "LoadImage",
            "inputs": {"image": init_image, "upload": "image"},
        }
        graph["21"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["20", 0], "vae": ["13", 0]},
        }
        graph["3"]["inputs"]["latent_image"] = ["21", 0]
        graph.pop("5", None)

    return graph


def build_comfyui_workflow(
    cfg: ImageGenConfig,
    prompt: str,
    width: int,
    height: int,
    checkpoint: str,
    seed: int,
    init_image: Optional[str] = None,
    denoise: float = 1.0,
    style_image: Optional[str] = None,
    style_weight: Optional[float] = None,
    ipadapter_file: str = "",
    clip_vision: str = "",
    vae_file: str = "",
    lora_file: str = "",
    lora_strength: Optional[float] = None,
    architecture: str = "sdxl",
    clip_file: str = "",
    model_vae_file: str = "",
    hand_detailer: bool = False,
    control_mode: str = "",
    control_image: str = "",
    control_strength: float = 1.0,
) -> Dict[str, Any]:
    """SDXL 文生图 / 图生图 / 风格转移工作流（ComfyUI 的 API 图格式）。

    刻意用最普通的 `CLIPTextEncode` 而不是 `CLIPTextEncodeSDXL`：
    前者在 ComfyUI 各版本里参数一直没变过（只有 text / clip），
    后者带一坨分辨率参数，版本一变就报错、还得猜语义。
    Illustrious 用普通编码器一样出图，1024 档下差别不明显。

    三种模式可以叠加，都是「给谁就加谁」：

      · 只给 `prompt`         → 纯文生图。**生成的图和以前逐字节一致**，
                                不多带任何节点，老路径一点没动。
      · 给 `init_image`       → **图生图（变体）**：LoadImage → VAEEncode →
                                KSampler 从它出发、按 `denoise` 只重绘一部分。
                                构图跟着参考图走。denoise 语义：1.0 = 完全重画，
                                0.4~0.7 = 保留构图换细节，0.3 以下变化很小。
      · 给 `style_image`      → **风格转移（IP-Adapter）**：把参考图的风格编码成
                                一组 embedding 灌进模型，构图与人物仍由 prompt 决定。
                                这是「用这个画风画别的东西」，和上面那件事不同。

    `ipadapter_file` / `clip_vision` 只在用了 style_image 时才需要
    （由 `_comfyui_ipadapter_files()` 问出来）。

      · 给 `lora_file`        → **LoRA（角色一致性）**：模型先过 `LoraLoader`
                                再往下走。这是目前唯一能拿到「像素级同一张脸」
                                的手段（IP-Adapter / InstantID 都做不到，已实测）。
                                挂了之后 prompt 里**别再写该角色的外貌标签**。

    想要双编码器 / 放大 / ControlNet，就自己导一份工作流填
    `comfyui_workflow` —— 那才是定制该走的路，别改这里。

    `architecture` 非 "sdxl"（当前即 Anima 系）时分发到
    `_build_diffusion_workflow()` —— 那是完全不同的接线，见其 docstring。
    默认值 "sdxl" 保证老调用方（不传这个参数）走的一字不改。
    """
    # Z-Image 单独一条：它和 Anima 虽然都不是 SDXL，但接线互不兼容
    # （lumina2 vs qwen_image、SD3 latent vs 普通 latent、要 ModelSamplingAuraFlow）。
    # 判断必须在下面那个 `!= "sdxl"` 之前，否则会被 Anima 分支吃掉。
    # ControlNet 只对 Anima 系（architecture="diffusion"）有效 ——
    # 现成的补丁全是 anima-lllite-*。Z-Image / SDXL 挂上去是静默无效，
    # 比报错更坑（画出来不对但看不出原因），所以这里直接清空并说一声。
    if control_mode and architecture != "diffusion":
        log.warning(
            "底模 %s 是 %s 架构，没有对应的 LLLite 补丁，control_mode=%r 已忽略",
            checkpoint, architecture, control_mode,
        )
        control_mode = ""

    if architecture == "zimage":
        return _build_zimage_workflow(
            cfg, prompt, width, height, checkpoint, seed,
            init_image=init_image, denoise=denoise,
            clip_file=clip_file, vae_file=model_vae_file,
        )
    if architecture != "sdxl":
        return _build_diffusion_workflow(
            cfg, prompt, width, height, checkpoint, seed,
            init_image=init_image, denoise=denoise,
            clip_file=clip_file, vae_file=model_vae_file,
            # 2026-10-24：Anima 原生 LoRA（networks.lora_anima 训的）可以挂了，
            # 之前这里故意不传，因为手上只有 SDXL LoRA、挂上去是静默无效。
            lora_file=lora_file, lora_strength=lora_strength,
            control_mode=control_mode,
            control_image=control_image,
            control_strength=control_strength,
        )
    graph: Dict[str, Any] = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": cfg.comfyui_negative, "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": int(cfg.comfyui_steps),
                "cfg": float(cfg.comfyui_cfg),
                "sampler_name": cfg.comfyui_sampler,
                "scheduler": cfg.comfyui_scheduler,
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "QQBot"},
        },
    }

    # ---- LoRA（角色一致性）----
    #
    # 模型要先过 LoraLoader 再往下走，所以这里把「模型 / CLIP 的上游引用」
    # 统一成两个变量，后面所有消费者（两个 CLIPTextEncode、KSampler、
    # 以及可能的 IP-Adapter）都跟着它走 —— **别在任何地方写死 `["4", 0]`**。
    # 写死的后果是「LoRA 挂了但没生效」：图看着就是不像，
    # 而日志里一切正常，属于最难查的一类。
    #
    # 顺序：LoRA 在 IP-Adapter **之前**。LoRA 改的是权重，IP-Adapter 叠的是条件，
    # 先把权重打好补丁再叠条件才对；反了等于拿没打补丁的模型去接风格。
    model_ref: List[Any] = ["4", 0]
    clip_ref: List[Any] = ["4", 1]
    lora_name = (lora_file or "").strip()
    if lora_name:
        strength = float(
            cfg.comfyui_lora_strength if lora_strength is None else lora_strength
        )
        # 钳到 [-2, 2]：负值本身合法（反向减掉某个特征），
        # 但超出这个范围只会产出废图，钳掉比让它自由发挥强。
        strength = max(-2.0, min(2.0, strength))
        graph["12"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["4", 0],
                "clip": ["4", 1],
                "lora_name": lora_name,
                "strength_model": strength,
                "strength_clip": strength,
            },
        }
        model_ref = ["12", 0]
        clip_ref = ["12", 1]

    # 显式把 LoRA 改的道接上去 —— 上面 dict 里写的 ["4", ...] 不会自己跟着变。
    graph["6"]["inputs"]["clip"] = clip_ref
    graph["7"]["inputs"]["clip"] = clip_ref
    graph["3"]["inputs"]["model"] = model_ref

    if init_image:
        # 图生图：把「空 latent」换成「参考图编码出来的 latent」，并打开 denoise。
        # 节点 ID 用 20/21 这种高位，避免和上面的 3~9 冲突（外部工作流也可能占位）。
        graph["20"] = {
            "class_type": "LoadImage",
            "inputs": {"image": init_image, "upload": "image"},
        }
        graph["21"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["20", 0], "vae": ["4", 2]},
        }
        graph["3"]["inputs"]["latent_image"] = ["21", 0]
        graph["3"]["inputs"]["denoise"] = max(0.0, min(1.0, float(denoise)))
        # EmptyLatentImage 不再被引用。留着不碍事（ComfyUI 只执行被引用的节点），
        # 但删掉更干净，也省得排查时看花眼。
        graph.pop("5", None)

    # 外部 VAE（可选）。
    #
    # 不给也能出图 —— 底模自带一个。但 Illustrious 系配 SDXL 专用 VAE
    # 是通行做法，实测两者**确实有差异**（约 49% 的像素不同，
    # 平均差值 0.3、最大 28~44）：外部 VAE 的肤色略暖、暗部更实。
    # 差异不大，但它是标准做法且没有副作用，所以接上更稳妥。
    #
    # 要放在最后：图生图的 VAEEncode 也指向 VAE，一并换掉。
    # 编码和解码必须用**同一个** VAE，否则图生图会出现色调漂移。
    if vae_file:
        graph["13"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae_file}}
        graph["8"]["inputs"]["vae"] = ["13", 0]          # VAEDecode 用外部 VAE
        if init_image:
            graph["21"]["inputs"]["vae"] = ["13", 0]      # VAEEncode 也用同一个

    if style_image:
        # 风格转移：KSampler 的 model 输入不再直接接 checkpoint，而是先过 IP-Adapter。
        # 这一串是 cubiq/ComfyUI_IPAdapter_plus 的标准接法。
        #
        # 和上面的图生图是**两件不同的事**，别混：
        #   · init_image   → 参考图决定构图，denoise 决定改多少
        #   · style_image  → 只借画风/氛围，构图与人物完全由 prompt 决定
        # 两者可以同时给（在参考图构图上套另一种画风），节点互不冲突。
        weight = float(
            cfg.comfyui_style_weight if style_weight is None else style_weight
        )
        graph["30"] = {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": clip_vision},
        }
        graph["31"] = {
            "class_type": "IPAdapterModelLoader",
            "inputs": {"ipadapter_file": ipadapter_file},
        }
        graph["32"] = {
            "class_type": "LoadImage",
            "inputs": {"image": style_image, "upload": "image"},
        }
        graph["33"] = {
            "class_type": "IPAdapterAdvanced",
            "inputs": {
                # 接 `model_ref` 而不是 ["4", 0] —— 挂了 LoRA 时这里必须是
                # LoraLoader 的输出，否则 IP-Adapter 会把 LoRA 整个绕过去。
                "model": model_ref,
                "ipadapter": ["31", 0],
                "image": ["32", 0],
                "clip_vision": ["30", 0],
                "weight": max(0.0, min(0.9, weight)),
                # linear + 全程覆盖：风格从头到尾都生效。换成 "style transfer"
                # 会让风格更重但不稳，做默认不值当（想要更重就调 weight）。
                "weight_type": "linear",
                "combine_embeds": "concat",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": "V only",
            },
        }
        graph["3"]["inputs"]["model"] = ["33", 0]

    # ---- 手部 detailer（可选，SDXL 架构）----
    #
    # FaceDetailer 是通用 detailer：bbox_detector 换成 hand_yolo 就是手部版。
    # 链路：VAEDecode 的图 → hand_yolov8s 找手 → 裁下放大重绘 → 贴回。
    # 检测不到手时节点内部自动跳过（几乎零开销）。
    # 接线纪律：model/clip 用 model_ref/clip_ref（挂了 LoRA 时必须带补丁），
    # vae 用 8 号的当前引用（外部 VAE 时是 13 号，保证编码解码同源）。
    #
    # ## 2026-10-20 重做：之前这套参数等于没开
    #
    # 老参数 denoise 0.5 + crop_factor 3.0 的实测结果：detailer **确实在跑**
    # （像素 diff 约 10%，正好是手部面积），但重绘出来的手和原图一模一样地糊。
    # 原因：denoise 0.5 只加一半噪声，KSampler 仍然被原图的糊状结构牵着走，
    # 只能"微调"不能"重画" —— 糊死的手需要重画，微调救不回来。
    # 另外 crop_factor 3.0 把框扩了 3 倍，重绘区域里大半是衣服和背景，
    # 有效像素被稀释；没 SAM 时 mask 是矩形，连背景一起重画。
    #
    # 新参数（同 seed 对照实验，A/B/C/D/F/G/H/I/J 十组，详见
    # `2026-10-20-21-51-53/hand_fix.py`）：
    #   denoise 0.5 → 0.80      重画而不是微调，这是唯一能救回糊手的一档
    #   crop_factor 3.0 → 1.5   框贴紧手部，别把衣服带进来
    #   dilation 10 → 6         同上
    #   guide_size 512 → 768    手裁出来放大更多，模型看到的"手"更大
    #   feather 5 → 12          0.8 denoise 下接缝更明显，羽化要加大
    #   + sam_model_opt         有 SAM 才是贴合手形的 mask，否则是矩形
    #
    # ⚠️ SAM 必须存在 `models/sams/sam_vit_b_01ec64.pth`。
    # 2026-10-10 那次报 "Invalid SAMLoader is connected" 是因为 sams 目录是空的，
    # 不是接线错了。现在模型已就位（2026-10-20 下载，357MB）。
    #
    # ## ⚠️ 2026-10-22：这套参数**会毁图**，默认已关（`comfyui_hand_detailer=False`）
    #
    # 用户的判断是准确的：「原先还行，加了手部修理节点之后就非常差」。
    #
    # 关掉前后的同 prompt 对照（`tests/_out/hands/`）：
    #   开着（denoise 0.8）→ 左手糊成一团黑影、**多画出第三只手**
    #   关掉            → 双手手指结构清晰、和碗的接触自然
    #
    # 为什么 denoise 0.8 反而更糟：它几乎完全重画手部区域，但重绘时用的是
    # **整句原 prompt**（`holding a teacup with both hands, ...`），不是手部专用
    # 描述。模型把「both hands」重新理解一遍，画出来的是一只**新的**手，
    # 和原图那只脱节 —— 于是接缝、结构错乱、手指数量对不上。
    #
    # 换句话说：detailer 的 denoise 越高，它越不像「修手」，越像「在手的位置
    # 重新生一张图」。而手部这个位置，模型本来就没有可靠的先验。
    #
    # **想救糊手，靠换底模（anima）或降 CFG，不是靠这个节点。**
    # 这段代码留着是因为参数可复用（换成 face detailer 时思路一样），
    # 但别再默认打开。
    if hand_detailer:
        graph["40"] = {
            "class_type": "UltralyticsDetectorProvider",
            "inputs": {"model_name": "bbox/hand_yolov8s.pt"},
        }
        graph["42"] = {
            "class_type": "SAMLoader",
            "inputs": {"model_name": "sam_vit_b_01ec64.pth", "device_mode": "AUTO"},
        }
        graph["41"] = {
            "class_type": "FaceDetailer",
            "inputs": {
                "image": ["8", 0],
                "model": model_ref,
                "clip": clip_ref,
                "vae": graph["8"]["inputs"]["vae"],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "bbox_detector": ["40", 0],
                "sam_model_opt": ["42", 0],
                "wildcard": "",
                "seed": seed,
                "steps": int(cfg.comfyui_steps),
                "cfg": float(cfg.comfyui_cfg),
                "sampler_name": cfg.comfyui_sampler,
                "scheduler": cfg.comfyui_scheduler,
                "denoise": 0.8,
                "guide_size": 768,
                "guide_size_for": True,
                "max_size": 1024,
                "feather": 12,
                "noise_mask": True,
                "force_inpaint": True,
                "bbox_threshold": 0.30,
                "bbox_dilation": 6,
                "bbox_crop_factor": 1.5,
                "sam_detection_hint": "center-1",
                "sam_dilation": 0,
                "sam_threshold": 0.93,
                "sam_bbox_expansion": 0,
                "sam_mask_hint_threshold": 0.7,
                "sam_mask_hint_use_negative": "False",
                "drop_size": 10,
                "cycle": 1,
                "inpaint_model": False,
                "noise_mask_feather": 20,
            },
        }
        # SaveImage 改道：发到群里的是手部修复后的图
        graph["9"]["inputs"]["images"] = ["41", 0]

    return graph


def parse_data_uri(data_uri: str) -> Tuple[str, bytes]:
    """把 `data:image/jpeg;base64,xxxx` 拆成 (mime, 字节)。

    为什么需要：机器人给模型看的图是 data URI 形式，而 ComfyUI 的
    LoadImage 要的是「已经上传到它 input 目录的文件名」。中间这一步转换
    必须自己走，没有现成接口。
    """
    text = (data_uri or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise ImageGenError("参考图不是合法的 data URI")
    header, payload = text.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip().lower() or "image/png"
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"参考图 base64 解不开：{exc}") from exc
    if not raw:
        raise ImageGenError("参考图是空的")
    return mime, raw


def _reference_filename(mime: str) -> str:
    """给上传的参考图起个唯一文件名。

    为什么要唯一：ComfyUI 的 input 目录是**共享**的，多个人同时用机器人时
    固定叫 `ref.png` 会让两份请求互相覆盖，画出来的图对不上号。
    （`upload_image_to_comfyui` 那边用 overwrite=true，只防重名不防并发。）
    """
    ext = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get((mime or "").lower(), "png")
    return f"qqbot_ref_{uuid.uuid4().hex}.{ext}"


# 参考图长边上限。**这个上限是必须的，不是优化**。
#
# 图生图的 latent 尺寸**跟着参考图走**，不跟 `size` 参数走。实测：
# 参考图 2324×4096 时，VAE 编码 + 28 步采样慢到必然超时（三档 denoise 全部
# 「出图超时」），而同一套参数换成 1024 长边就只要 18~23 秒。
# 用户随手发一张手机截图/原图就可能踩到 —— 而且症状是「画不出来」，
# 完全看不出是尺寸问题。
_MAX_REFERENCE_SIDE = 1024


def downscale_reference(raw: bytes, limit: int = _MAX_REFERENCE_SIDE) -> bytes:
    """把参考图缩到长边不超过 `limit`，返回 PNG 字节。

    缩不了（解码失败）就原样返回 —— 让后面的上传照常尝试，
    真正的坏图会由 ComfyUI 那边报错，不在这里静默吞掉。
    """
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            if max(im.size) <= limit:
                return raw
            scale = limit / max(im.size)
            new_size = (
                max(1, round(im.width * scale)),
                max(1, round(im.height * scale)),
            )
            resized = im.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            log.info(
                "参考图 %dx%d 超过 %d，已缩到 %dx%d（图生图的 latent 尺寸跟着参考图走，"
                "大图会慢到超时）",
                im.width, im.height, limit, new_size[0], new_size[1],
            )
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return raw


async def _comfyui_pick_option(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    node: str,
    field: str,
    prefer: str,
    what: str,
) -> str:
    """决定用 ComfyUI 里的哪个文件（IP-Adapter 模型 / CLIP-Vision）。

    和 checkpoint 一样的策略：**配了就信配置，没配就问 ComfyUI 扫到了什么**。
    不去猜文件名 —— 猜错只会白等一轮采样，不如直接失败。
    """
    chosen = (prefer or "").strip()
    if chosen:
        return chosen

    base = _comfyui_base(cfg)
    try:
        async with session.get(
            f"{base}/object_info/{node}",
            timeout=aiohttp.ClientTimeout(total=30.0),
        ) as resp:
            raw = await resp.read()
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"读不到 ComfyUI 的 {what} 列表：{exc}", retryable=True) from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        choices = data[node]["input"]["required"][field][0]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise ImageGenError(f"读不懂 ComfyUI 的 {what} 列表") from exc

    if not choices:
        raise ImageGenError(
            f"ComfyUI 里没有可用的 {what} —— 相关模型文件没放到 models 目录下"
        )
    # 后加的排在前面：多个候选时倾向用较新的那个
    return str(choices[-1])


async def _comfyui_ipadapter_files(
    session: aiohttp.ClientSession, cfg: ImageGenConfig
) -> Tuple[str, str]:
    """确定 IP-Adapter 需要的两个文件名：(ipadapter_file, clip_vision)。"""
    ip = await _comfyui_pick_option(
        session, cfg, "IPAdapterModelLoader", "ipadapter_file",
        cfg.comfyui_ipadapter_file, "IP-Adapter 模型",
    )
    clip = await _comfyui_pick_option(
        session, cfg, "CLIPVisionLoader", "clip_name",
        cfg.comfyui_clip_vision, "CLIP-Vision 模型",
    )
    return ip, clip


def looks_like_character_sheet(raw: bytes) -> bool:
    """判断参考图是不是**角色设定图**（同一角色多个姿势拼在一张图上）。

    为什么要单独判这一类：图生图保的是「整张图的构图」，
    而设定图是**一版排版**、不是一个场景。喂它做图生图，输出**还是那张设定图** ——
    三个姿势、分格排版原样保留，prompt 里要求的"换到商场、背景虚化"根本进不去。
    实测就是这样：用户发了张三姿势设定图，denoise 0.45 出来的还是那张设定图。

    这不是参数问题，是**输入形态**问题 —— 所以正确做法是先告诉用户
    「这张图我用来参考角色，场景我另外画」，而不是硬把它当场景底图。

    判据（实测标定）：看**近全白行/列构成的「白带」有多宽**。

        图                  最宽白带(行/列)   说明
        设定图（三姿势）        5 / 6        排版细缝
        单场景：立牌场景        0 / 0        没有成带的白
        单场景：商场            0 / 0        同上
        纯白图                  120 / 120    整张都是白
        白底立绘                20 / 36      整片留白

    区分点是**宽度**，不是有没有白带：
      · 设定图的白带是**排版缝**（几个像素宽）→ 判 True
      · 白底立绘的白带是**整片留白**（几十像素）→ 判 False
      · 纯白图整张都是白 → 判 False

    为什么不能用「近全白行数 > 0」这种简单条件：我第二版就是这么写的，
    结果一张白底立绘（上下各一大片白）照样命中，**误判成设定图**。
    白底立绘是很常见的参考图，误判会把它降级处理，比漏判更糟。

    也**不要**用长宽比 —— 我第一版加了「接近正方形」，结果真实设定图是
    724x1024（比例 0.71）被直接卡掉，检测形同虚设。竖构图的设定图很常见。

    宁可漏判也不要误判：漏判只是维持现状，误判会把好图降级。
    """
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB").resize((120, 120))
            px = im.load()

            def white(x: int, y: int) -> int:
                r, g, b = px[x, y]
                return 1 if (r > 235 and g > 235 and b > 235) else 0

            total = 0
            white_total = 0
            border_total = 0
            border_white = 0
            row_white = [0] * 120
            col_white = [0] * 120

            for y in range(120):
                edge_row = y < 6 or y >= 114
                row_sum = 0
                for x in range(120):
                    w = white(x, y)
                    row_sum += w
                    col_white[x] += w
                    total += 1
                    white_total += w
                    if edge_row or x < 6 or x >= 114:
                        border_total += 1
                        border_white += w
                row_white[y] = row_sum

            if not total or not border_total:
                return False

            white_ratio = white_total / total
            border_ratio = border_white / border_total
            if white_ratio > 0.85:
                return False

            def widest_band(counts) -> int:
                """近全白行/列里，连续成带的最长长度。"""
                longest = 0
                cur = 0
                for c in counts:
                    if c >= 108:      # 该行/列 ≥90% 是白的
                        cur += 1
                        longest = max(longest, cur)
                    else:
                        cur = 0
                return longest

            widest = max(widest_band(row_white), widest_band(col_white))
            # 设定图的缝是窄的；白底立绘/纯白图是宽的
            return 2 <= widest <= 12 and border_ratio >= 0.30
    except Exception:  # noqa: BLE001
        return False


def looks_like_cutout_portrait(raw: bytes) -> bool:
    """判断参考图是不是**抠掉背景的立绘**（透明底或纯色底的单个角色素材）。

    为什么要单独判这一类：图生图是从**参考图的 latent** 加噪出发，
    所以参考图的一切都会被保留 —— **包括"没有背景"这件事**。
    实测：用户发了一张透明底立绘，要求「做成商场里的等身立牌」，
    denoise 0.45 的输出**几乎就是原图**（黑底、同一个姿势、同一把枪），
    prompt 里的商场、虚化、立牌一个都没进去。

    这类图和"带场景的插画"需要完全不同的处理：
      · 带场景的插画 → 图生图，denoise 控制改动幅度
      · 抠底立绘     → **不能靠图生图**。参考图里没有场景信息可依据，
        低 denoise 保住角色就必然丢掉场景，高 denoise 换来场景但角色变了。
        正确的做法是**把角色外貌写成文字**，用纯文生图重新画一张
        （见 core 里对这条的处理）。实测这条路能同时拿到角色和场景。

    判据（两种都覆盖，实测标定）：

      ① **图片带真正的透明通道**（PNG 抠图，alpha < 16 的像素 ≥15%）
      ② **纯色底且占比过半**：四角颜色一致、是黑/白/灰、且该颜色占全图 **≥40%**

    为什么必须覆盖第 ② 种：实测用户发来的"透明底立绘"其实**不是**透明 PNG ——
    经 QQ 传输后变成了 mode=RGB、**纯黑背景**、黑底占 51% 的图。
    只看 alpha 通道会完全漏掉这一类，而它恰恰是最常见的抠图素材形态。

    为什么阈值要定在 40%（而不是 30%）：实测主色占比能把三类图分开 ——

        用户的抠图素材（单角色、黑底）      51.3%   → 判 True
        三姿势设定图（多格排版、白底）      31.0%   → 判 False
        带场景插画（黑边、有内容）           9.4%   → 判 False

    设定图是**多格排版**，单格之外还有别的格子和文字，所以主色占比天然低于
    单角色素材；带场景插画的主色只是画框边。30% 会把这两类都误判成抠图素材，
    40% 能干净切开。误判的代价是把一张正常的图降级处理，所以宁严勿宽。

    还要限定"黑/白/灰"（明度很低或很高、且三通道接近）：场景插画的四角
    即使相近也很少是低饱和中性色。
    """
    try:
        with Image.open(io.BytesIO(raw)) as im:
            # ① 透明通道
            if im.mode in ("RGBA", "LA", "PA"):
                alpha = im.convert("RGBA").getchannel("A").resize((96, 96))
                px = list(alpha.getdata())
                if sum(1 for a in px if a < 16) / len(px) >= 0.15:
                    return True

            # ② 纯色底
            rgb = im.convert("RGB").resize((64, 64))
            w, h = rgb.size
            corners = [
                rgb.getpixel((0, 0)), rgb.getpixel((w - 1, 0)),
                rgb.getpixel((0, h - 1)), rgb.getpixel((w - 1, h - 1)),
            ]
            base = corners[0]
            for c in corners[1:]:
                if max(abs(c[i] - base[i]) for i in range(3)) > 20:
                    return False      # 四角不一致 → 有背景内容
            lum = sum(base) / 3
            spread = max(base) - min(base)
            if not (lum < 60 or lum > 210) or spread > 26:
                return False          # 不是黑/白/灰这类中性底色
            data = list(rgb.getdata())
            same = sum(
                1 for c in data
                if max(abs(c[i] - base[i]) for i in range(3)) <= 24
            )
            return same / len(data) >= 0.40
    except Exception:  # noqa: BLE001
        return False


async def upload_image_to_comfyui(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    raw: bytes,
    filename: str,
    mime: str = "image/png",
    timeout: float = 30.0,
) -> str:
    """把参考图 POST 到 ComfyUI 的 /upload/image，返回它给的引用名。

    为什么必须走这一步：ComfyUI 的 LoadImage 不是从任意路径读图，
    它只认自己 input 目录里的文件名（`/upload/image` 就是官方给的上传口）。
    直接把本地路径写进工作流是读不到的。
    """
    base = _comfyui_base(cfg)
    form = aiohttp.FormData()
    form.add_field(
        "image",
        raw,
        filename=filename,
        content_type=mime or "image/png",
    )
    # overwrite=true：同名文件直接覆盖。重名会导致 LoadImage 拿到上一张旧图，
    # 那种 bug 表现为「参考图和上一轮一样」，极难排查。
    form.add_field("overwrite", "true")
    form.add_field("type", "input")

    try:
        async with session.post(
            f"{base}/upload/image",
            data=form,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.read()
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"参考图上传失败（连不上 ComfyUI）：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"参考图上传异常：{exc}", retryable=True) from exc

    if resp.status != 200:
        raise ImageGenError(
            f"ComfyUI 拒收参考图（HTTP {resp.status}）：{_api_error_text(body)}",
            retryable=True,
        )

    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise ImageGenError("ComfyUI 上传参考图后返回的不是 JSON") from exc

    # ComfyUI 会回 {"name": "...", "subfolder": "...", "type": "input"}
    # LoadImage 只吃 name，带 subfolder 时要拼成 "sub/name"。
    name = str(data.get("name") or "").strip()
    sub = str(data.get("subfolder") or "").strip()
    if not name:
        raise ImageGenError("ComfyUI 没返回参考图文件名")
    ref = f"{sub}/{name}" if sub else name
    log.info("参考图已上传到 ComfyUI：%s（%d 字节）", ref, len(raw))
    return ref


def _load_external_workflow(
    cfg: ImageGenConfig,
    prompt: str,
    negative: str,
    width: int,
    height: int,
    checkpoint: str,
    seed: int,
) -> Optional[Dict[str, Any]]:
    """读外部工作流 JSON（若配了）。占位符替换，不做节点智能识别。

    支持的占位符：`{{prompt}}` `{{negative}}` `{{width}}` `{{height}}`
    `{{seed}}` `{{checkpoint}}` `{{steps}}` `{{cfg}}` `{{sampler}}` `{{scheduler}}`。

    为什么只做字符串替换：想定制工作流的人本来就会从 ComfyUI 导出一份，
    自己放占位符是最直白的方式。去猜「哪个节点是 positive 输入」属于过度设计，
    而且猜错了会画出完全不对的图，比报错更难查。
    """
    # 这个字段可能被配置层解析成 Path（而不是声明里的 str），所以先统一成
    # 字符串再判空。空值代表「用内置工作流」，这是默认情况，必须走通。
    raw = cfg.comfyui_workflow
    raw = "" if raw is None else str(raw).strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        # 相对路径按项目根目录算（配置文件所在目录）
        path = Path(__file__).resolve().parents[1] / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageGenError(f"读不到 comfyui_workflow 指向的文件：{path}（{exc}）") from exc

    mapping = {
        "{{prompt}}": json.dumps(prompt, ensure_ascii=False)[1:-1],
        "{{negative}}": json.dumps(negative, ensure_ascii=False)[1:-1],
        "{{width}}": str(width),
        "{{height}}": str(height),
        "{{seed}}": str(seed),
        "{{checkpoint}}": json.dumps(checkpoint, ensure_ascii=False)[1:-1],
        "{{steps}}": str(int(cfg.comfyui_steps)),
        "{{cfg}}": str(float(cfg.comfyui_cfg)),
        "{{sampler}}": json.dumps(cfg.comfyui_sampler, ensure_ascii=False)[1:-1],
        "{{scheduler}}": json.dumps(cfg.comfyui_scheduler, ensure_ascii=False)[1:-1],
    }
    for key, value in mapping.items():
        text = text.replace(key, value)

    try:
        return json.loads(text)
    except ValueError as exc:
        raise ImageGenError(f"comfyui_workflow 不是合法 JSON（{path}）：{exc}") from exc


def _extract_comfyui_images(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 /history 的一条记录里把所有输出图片抠出来。

    不认节点 ID —— 工作流一换 ID 就变了，按「输出里带 images 数组」
    来找才是对的做法。顺带兼容用户自己工作流里别的出图节点。
    """
    outputs = entry.get("outputs") or {}
    found: List[Dict[str, Any]] = []
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        imgs = node_out.get("images")
        if isinstance(imgs, list):
            found.extend(
                i for i in imgs if isinstance(i, dict) and i.get("filename")
            )
    return found


async def _fetch_comfyui_image(
    session: aiohttp.ClientSession,
    base: str,
    image: Dict[str, Any],
    timeout_seconds: float,
) -> bytes:
    """把 ComfyUI 产出的图取回本地字节。"""
    params = {
        "filename": image.get("filename") or "",
        "subfolder": image.get("subfolder") or "",
        "type": image.get("type") or "output",
    }
    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    try:
        async with session.get(
            f"{base}/view?{urlencode(params)}", timeout=timeout
        ) as resp:
            if resp.status != 200:
                raise ImageGenError(
                    f"取不回 ComfyUI 的图（HTTP {resp.status}）", retryable=True
                )
            raw = await resp.read()
    except ImageGenError:
        raise
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"取不回 ComfyUI 的图：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"取 ComfyUI 的图时异常：{exc}", retryable=True) from exc

    if len(raw) < MIN_IMAGE_BYTES:
        raise ImageGenError("ComfyUI 返回的图过短，多半是错误响应", retryable=True)
    return raw


async def _generate_comfyui(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    prompt: str,
    size: Optional[str],
    init_image: Optional[str] = None,
    denoise: float = 1.0,
    style_image: Optional[str] = None,
    style_weight: Optional[float] = None,
    checkpoint: Optional[str] = None,
) -> bytes:
    """通道 C：把工作流丢给本机 ComfyUI，轮询拿图。

    `init_image` / `style_image` 都是**已经上传到 ComfyUI 的文件名**
    （不是本地路径、不是 URL）—— 转换由 `upload_image_to_comfyui()` 做，
    见那里的说明。

    `checkpoint` 传了就覆盖配置里的底模（`/model` 指令指定的那个）。
    不传就走 `_comfyui_checkpoint()` 的原逻辑。

    失败一律抛 `ImageGenError` 且 `retryable=True` —— 上层看到就降级，
    不在这里硬重试（本地出图一张几十秒，重试一次用户已经等不住了）。
    """
    base = _comfyui_base(cfg)
    width, height = resolve_size(size)
    checkpoint = (checkpoint or "").strip() or await _comfyui_checkpoint(session, cfg)

    # ---- 采样器 / 调度器 / CFG / 步数**跟着底模走** ----
    #
    # 这几个值换底模时必须重标，不能沿用旧值。实测（2026-10-03，同 prompt、
    # 同 seed）：animagine 沿用 Illustrious 的 `dpmpp_2m_sde + karras`
    # 会出**脸过曝发糊、合十的手糊成一团肉块**；换成 `euler_ancestral + normal`
    # 手就清楚了。标定过程见 COMFYUI_MODEL_PRESETS 里 animagine 那段的注释。
    #
    # 预设里留空的字段（未知底模）**不覆盖**，沿用配置里的值 ——
    # 配置里那套至少是用户自己在用的，比瞎猜强。
    preset = model_preset(checkpoint)
    overrides: Dict[str, Any] = {}
    if preset.get("sampler"):
        overrides["comfyui_sampler"] = str(preset["sampler"])
    if preset.get("scheduler"):
        overrides["comfyui_scheduler"] = str(preset["scheduler"])
    if preset.get("cfg") is not None:
        overrides["comfyui_cfg"] = float(preset["cfg"])
    if preset.get("steps") is not None:
        overrides["comfyui_steps"] = int(preset["steps"])
    if overrides:
        cfg = replace(cfg, **overrides)
    seed = random.randint(1, 2**31 - 1)

    # ---- 架构分支：Anima 系（diffusion_models，非 SDXL）----
    #
    # Anima（Cosmos-Predict2 底座 + Qwen3 编码器）和 SDXL 是两套互不兼容的
    # 工作流。SDXL 生态的三个可选模块在这里全部不适用，直接清空：
    #   · IP-Adapter：SDXL 专用权重，接不上 Anima
    #   · LoRA：SDXL LoRA 对 Anima 无效
    #   · 外置 SDXL VAE：Anima 必须用 qwen_image_vae（由预设表指定）
    # 风格参考图无法满足时**忽略并警告**，让这一轮按纯文生图出 ——
    # 比抛错降级到在线通道好：本机免费且 prompt 仍然生效，只是丢了风格。
    architecture = str(preset.get("architecture") or "sdxl")

    # ---- LoRA 只在「画这个角色」时挂（2026-10-22 加）----
    #
    # 之前 `comfyui_lora` 一配就**无条件挂到每一次生图**。后果：画宫园薰
    # （`kaori miyazono, blonde hair, blue eyes`）时角色的 LoRA 照样生效，
    # 脸型、发色、气质全被拽向那个角色 —— 用户的原话是「animagine 模型效果不如之前了」。
    # 那不是模型变差，是 LoRA 在污染所有角色。
    #
    # LoRA 本来就是靠**触发词**激活的东西。所以：
    #   · 配了 `comfyui_lora_trigger` → prompt 里含这个词才挂
    #   · 没配触发词 → 保持旧行为（总是挂），避免悄悄改变已有配置的语义
    #
    # 判断用**原始 prompt** 而不是拼了质量前缀的 full_prompt：
    # 前缀是固定的模板文字，永远不含触发词，用它判断没意义。
    #
    # ⚠️ 这段必须排在**架构判断之前**：anima 不支持 SDXL LoRA，如果先判断架构，
    # 会先打一条「不适用，已忽略」的警告 —— 但这次本来就因为触发词不匹配
    # 没打算挂，那条警告是纯噪音，会把排查方向带偏。
    lora_file = (cfg.comfyui_lora or "").strip()
    trigger = str(getattr(cfg, "comfyui_lora_trigger", "") or "").strip()
    if lora_file and trigger and trigger.lower() not in (prompt or "").lower():
        log.info(
            "prompt 里没有触发词 %r，这次不挂 LoRA（%s）—— 画的是别的角色",
            trigger, lora_file,
        )
        lora_file = ""

    if architecture != "sdxl":
        if style_image:
            log.warning(
                "底模 %s 是 %s 架构，不支持 IP-Adapter 风格转移 —— 风格参考图被忽略",
                checkpoint, architecture,
            )
            style_image = None
        if lora_file:
            # ⚠️ 2026-10-24 改了这里：Anima 现在**有**原生 LoRA 了。
            #
            # 以前这段是无条件清空，因为手上只有 SDXL LoRA，挂上去是静默无效。
            # 现在用 `networks.lora_anima` 训出了 Anima 版，
            # 键名同样是 lora_unet_*，但 shape 对得上 28 blocks / 2048 dim。
            #
            # 靠**文件名**区分两代 LoRA：
            #   · *_anima_lora.safetensors → Anima 原生，挂
            #   · 其它 *_lora.safetensors → SDXL，挡掉（挂上去不报错但无效，
            #                                      比报错更坑：会让人以为画对了）
            if "anima" in lora_file.lower():
                log.info(
                    "底模 %s 是 %s 架构，挂 Anima 原生 LoRA（%s）",
                    checkpoint, architecture, lora_file,
                )
            else:
                log.warning(
                    "底模 %s 是 %s 架构，SDXL LoRA（%s）不适用，已忽略",
                    checkpoint, architecture, lora_file,
                )
                lora_file = ""

    # 正向：质量标签**在前**，用户描述在后。
    # 二次元底模吃 Danbooru 标签体系，前缀质量标签是**有效的** ——
    # 这跟 Pollinations(sana) 那边正好相反（那边堆关键词会把主体冲掉）。
    # 两套模型脾气不同，别把经验混着用。
    # 预设可以用 positive_prefix 覆盖（Anima 官方只要 masterpiece, best quality，
    # Illustrious 系那串 score 标签对它是未标定输入）—— 没写就沿用 config。
    prefix = preset.get("positive_prefix")
    if prefix is None:
        prefix = (cfg.comfyui_positive or "").strip()
    prefix = str(prefix).strip()
    full_prompt = f"{prefix}, {prompt}" if prefix else prompt

    # ---- 挂 LoRA 时补外观词（2026-10-24 加）----
    #
    # 打标时把发色/瞳色剔掉了（角色 LoRA 的标准做法，为了让触发词隐式绑定外观），
    # 但实测触发词绑不住：同一份 prompt + 同一个 LoRA，seed 20261024 出银发蓝眼、
    # seed 777 出黑长直。服装倒是一直对 —— 因为标注里有服装标签，没有发色标签。
    #
    # 所以挂 LoRA 的这轮，把 config.comfyui_lora_appearance 拼进去。
    # ⚠️ 必须放在架构分支**之后**：画别的角色时 lora_file 已被清空，
    # 这里就不会追加，不会把人家也画成黑长直。
    appearance = str(getattr(cfg, "comfyui_lora_appearance", "") or "").strip()
    if lora_file and appearance:
        full_prompt = f"{full_prompt}, {appearance}"
        log.info("挂了角色 LoRA，补外观词：%s", appearance)

    # IP-Adapter 的两个文件名只在真的要做风格转移时才去问 ——
    # 它要额外请求 ComfyUI 的 object_info，纯文生图没必要付这个开销。
    ipadapter_file = clip_vision = ""
    if style_image:
        ipadapter_file, clip_vision = await _comfyui_ipadapter_files(session, cfg)

    # ---- ControlNet 准备（2026-10-25 加）----
    #
    # 控制图**复用用户发的参考图**（已经上传到 ComfyUI input 目录的那个名字）。
    # 配了 ControlNet 就把 init_image 清空、不再走图生图 —— 两者语义冲突：
    # 图生图要保留原图构图（denoise<1），ControlNet 要按骨架/边缘重画（denoise=1），
    # 同时开等于让模型既保留又重画，结果两边都不像。
    control_mode = str(
        getattr(cfg, "comfyui_controlnet_mode", "") or ""
    ).strip().lower()
    control_image = ""
    if control_mode:
        if init_image:
            control_image = init_image
            init_image = None
            log.info(
                "ControlNet 模式 %r：参考图改当控制图用，这一轮不走图生图",
                control_mode,
            )
        else:
            log.warning(
                "配了 controlnet_mode=%r 但这一轮没收到参考图，ControlNet 不生效",
                control_mode,
            )
            control_mode = ""

    graph = _load_external_workflow(
        cfg, full_prompt, cfg.comfyui_negative, width, height, checkpoint, seed
    )
    if graph is None:
        graph = build_comfyui_workflow(
            cfg, full_prompt, width, height, checkpoint, seed,
            init_image=init_image, denoise=denoise,
            style_image=style_image, style_weight=style_weight,
            ipadapter_file=ipadapter_file, clip_vision=clip_vision,
            vae_file=(cfg.comfyui_vae or "").strip(),
            lora_file=lora_file,
            architecture=architecture,
            clip_file=str(preset.get("clip_file") or ""),
            model_vae_file=str(preset.get("vae_file") or ""),
            hand_detailer=bool(getattr(cfg, "comfyui_hand_detailer", False)),
            control_mode=control_mode,
            control_image=control_image,
            control_strength=float(
                getattr(cfg, "comfyui_controlnet_strength", 1.0) or 1.0
            ),
        )
    elif init_image or style_image:
        # 外部工作流是用户自己导出的固定图，这里**不猜**怎么塞参考图进去 ——
        # 猜错会画出完全不对的图，比明确失败更难查（和 _load_external_workflow
        # 里那段「不做节点智能识别」是同一个理由）。
        log.warning(
            "配了外部工作流，图生图/风格转移不会生效（参考图被忽略）。"
            "想用它们请清空 imagegen.comfyui_workflow，或用它自己的占位符机制。"
        )

    payload = {"prompt": graph, "client_id": uuid.uuid4().hex}
    # 日志里带上采样器 / 调度器 / CFG —— 它们是**跟着底模走**的，
    # 出图不对时第一件事就是确认这一轮到底用了哪套（换底模漏标参数是本项目
    # 反复踩的坑，只记 ckpt 看不出问题）。
    log.info(
        "生图请求（本机 ComfyUI）：%dx%d ckpt=%s %s/%s cfg=%.1f steps=%d denoise=%.2f "
        "LoRA=%s(%.2f) 参考图=%s 风格图=%s(权重%.2f) prompt=%r",
        width, height, checkpoint, cfg.comfyui_sampler, cfg.comfyui_scheduler,
        float(cfg.comfyui_cfg), int(cfg.comfyui_steps), float(denoise),
        # 用 `lora_file` 而不是 `cfg.comfyui_lora` —— 后者是「配置里有什么」，
        # 前者是「这次真的挂了什么」。触发词不匹配时前者为空、后者非空，
        # 拿后者打日志会让人以为 LoRA 生效了（2026-10-22 实测踩到）。
        lora_file or "无",
        float(cfg.comfyui_lora_strength) if lora_file else 0.0,
        init_image or "无", style_image or "无",
        float(cfg.comfyui_style_weight if style_weight is None else style_weight),
        prompt[:80],
    )

    # 提交和取图各自用短超时，整体由 deadline 兜 ——
    # 一个 total=120s 的 timeout 用在轮询上会导致「卡住直到整体超时」，
    # 而我们希望的是「每次轮询快进快出，到点就放弃」。
    post_timeout = aiohttp.ClientTimeout(total=30.0)
    try:
        async with session.post(
            f"{base}/prompt", json=payload, timeout=post_timeout
        ) as resp:
            raw = await resp.read()
    except aiohttp.ClientError as exc:
        raise ImageGenError(f"连不上本机 ComfyUI：{exc}", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError(f"提交 ComfyUI 任务异常：{exc}", retryable=True) from exc

    if resp.status != 200:
        raise ImageGenError(
            f"ComfyUI 拒收任务（HTTP {resp.status}）：{_api_error_text(raw)}",
            retryable=True,
        )

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise ImageGenError(f"ComfyUI 返回的不是 JSON：{raw[:120]}") from exc

    # ComfyUI 的脾气：工作流有问题时它照样 200，错误塞在 node_errors 里。
    # 不查这一层，症状会变成「提交成功但永远轮询不到结果」，很难排查。
    if data.get("node_errors"):
        raise ImageGenError(f"ComfyUI 工作流有错：{data['node_errors']}")
    if data.get("error"):
        raise ImageGenError(f"ComfyUI 报错：{_api_error_text(raw)}")

    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ImageGenError("ComfyUI 没返回 prompt_id")

    deadline = time.time() + float(cfg.comfyui_timeout)
    poll_timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 3)
    while time.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            async with session.get(
                f"{base}/history/{prompt_id}", timeout=poll_timeout
            ) as resp:
                if resp.status != 200:
                    continue
                hist = json.loads((await resp.read()).decode("utf-8", "replace"))
        except (aiohttp.ClientError, ValueError, TypeError):
            # 轮询期间服务重启/抖动很常见，跳过这一轮继续等
            continue

        entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        if not entry:
            continue
        images = _extract_comfyui_images(entry)
        if images:
            return await _fetch_comfyui_image(
                session, base, images[0], cfg.comfyui_timeout
            )
        # 历史里已经有了但没图 —— 说明这个任务执行失败了（比如节点报错）。
        # 不在这里退出的话会一直等到超时，白等满 120 秒才报「超时」，
        # 掩盖掉真正的错误原因。
        status = (entry.get("status") or {}).get("status_str")
        if status == "error":
            raise ImageGenError(
                f"ComfyUI 执行失败：{_api_error_text(json.dumps(entry.get('status'), ensure_ascii=False).encode('utf-8'))}"
            )

    # 超时前**主动把这个任务从 ComfyUI 队列里撤掉**。
    #
    # 为什么必须撤：ComfyUI 收到任务就开始排队执行，不受我们这边超时的约束。
    # 我们放弃之后它还在占着显存跑，下一个请求排在后面、更容易再超时 ——
    # 于是「一次超时」会滚成「之后次次超时」，表现像服务坏了。
    # 这不是理论：实测把队列堆到 running=1/pending=1、GPU 99% 之后，
    # 原本 16 秒能出的图变成必然超时。
    await _cancel_comfyui_job(session, base, prompt_id)
    raise ImageGenError("ComfyUI 出图超时", retryable=True)


async def _cancel_comfyui_job(
    session: aiohttp.ClientSession, base: str, prompt_id: str
) -> None:
    """尽力撤销一个排队中/运行中的任务。失败只记日志，不影响主流程。"""
    try:
        async with session.post(
            f"{base}/queue",
            json={"delete": [prompt_id]},
            timeout=aiohttp.ClientTimeout(total=10.0),
        ) as resp:
            if resp.status == 200:
                log.info("已从 ComfyUI 队列撤销超时任务 %s", prompt_id)
            else:
                log.debug("撤销任务 %s 返回 HTTP %s", prompt_id, resp.status)
    except Exception:  # noqa: BLE001
        log.debug("撤销任务 %s 失败（忽略）", prompt_id, exc_info=True)


async def generate_prepared(
    session: aiohttp.ClientSession,
    cfg: ImageGenConfig,
    prompt: str,
    size: Optional[str] = None,
    seed: Optional[int] = None,
    attempts: int = 3,
    reference_image: Optional[str] = None,
    denoise: float = 0.6,
    style_reference: Optional[str] = None,
    style_weight: Optional[float] = None,
    force_channel: Optional[str] = None,
    force_checkpoint: Optional[str] = None,
) -> Tuple[str, bytes]:
    """生成一张**校验过**的图，返回 (mime, 字节)。

    两条通道共用这一层，所以上层（BotCore）不用管走的是哪条。

    两个参考图参数是**两件不同的事**，别混：
      · `reference_image`（图生图变体）：data URI。参考图决定**构图**，
        `denoise` 决定改多少。适合「照这张重画」「换个表情」。
      · `style_reference`（IP-Adapter 风格转移）：data URI。只借**画风与氛围**，
        构图和人物由 prompt 决定。适合「用这个画风画个 XX」。
    两者可以同时给。

    **只有本机 ComfyUI 通道吃它们** —— 在线通道都是纯文生图接口，塞不进参考图，
    那边会忽略并降到纯文生图，只记一条日志。这是有意的：让「画张猫」和
    「照这张改」在在线通道下都能出图，而不是因为带不了参考图就整个失败。

    两道关：
      1. `generate()` 拿字节，网络/HTTP 层失败按 `retryable` / `cooldown`
         决定等多久再试（3 次机会，最坏情况多等约 15 秒）；
      2. `prepare()` 解码校验 —— 生成服务同样可能返回被截断的响应，
         不校验就等于把「坏图直接发进群」这条老路又铺了一遍。
         顺带把水印裁掉、把图重编码成 JPEG，QQ 那边更稳。

    为什么给 3 次：Pollinations 最常见的失败序列是 **HTTP 500 → 重试时 402**。
    只试一次的话，一个服务端抖动就白白浪费掉用户的请求。
    （通道 B 少见这种情况，但多试一次也不亏。）

    `seed` 只给测试用（固定种子才能对比裁水印前后是不是同一张图）。
    生产环境不传，每次随机 —— 否则「再画一张」会永远是同一张。

    `force_channel`（`"local"` / `"online"` / `None`）是**通道强制**，两个来源：
      · 用户发的 `/本机` `/在线` 指令（会话级，见 BotCore._channel_override）
      · 模型在 `generate_image` 里填的 `channel` 参数（单次，优先级更高）
    不传 = 走原来的默认（本机优先，失败降级）。

    **强制 local 时不降级** —— 用户要本机图的就是「免费、不限量、无水印」，
    偷偷切到在线会花钱，那是背着用户改主意。本机不可用就如实报错。
    强制 online 时仍然受额度闸门约束（额度是硬性的花钱上限，不能靠指令绕过）。

    失败一律抛 `ImageGenError`，调用方拿它决定怎么跟用户说。
    """
    if getattr(cfg, "local_only", False):
        if force_channel == "online":
            raise ImageGenError("当前仅启用了本机 ComfyUI 插件，请使用本机生图。", retryable=False)
        force_channel = "local"
    total = max(1, int(attempts))
    last: Optional[ImageGenError] = None
    # 裁水印**只对免费通道做**。这是有原因的，不是漏了通道 B：
    #
    #   · Pollinations 的水印是右下角一条 `pollinations.ai` 横幅，
    #     位置固定、高度 44px，裁掉不影响构图 —— 而且那是服务商的**广告**，
    #     不是合规标识，裁掉没毛病。
    #   · 通道 B 里**智谱也会加「AI生成」水印**（2026-09-30 实测，
    #     方版 1024x1024 和竖版 768x1344 都有，在右下角）。那是它履行
    #     《人工智能生成合成内容标识办法》的**显式标识** —— 不该裁。
    #     所以这里不碰通道 B 的图。
    #   · OpenAI / 硅基流动实测不加。
    #
    # 一句话：`trim_watermark` 是「裁广告」，不是「去标识」，两者别混。
    trim = (
        int(cfg.watermark_px)
        if (cfg.trim_watermark and cfg.resolved_provider == "pollinations")
        else 0
    )

    # 预算闸门。
    #
    # 在线通道按量计费，而机器人是「来一张画一张」—— 群里有人连点、
    # 或者模型自己反复重试，就能把额度打光。所以超限时**不给在线通道**，
    # 但**不直接失败**：先尝试本机（不花钱），本机也不行才拒画。
    #
    # 顺序上必须放在「试在线通道」之前判定，且用 online_ok 记住结论 ——
    # 后面在线那段要读它，不能在那儿再判一次（会重复 log、也可能状态已变）。
    online_ok, online_block_reason = _online_allowed(cfg)
    if not online_ok:
        log.warning("在线通道已达预算上限（%s）", online_block_reason)

    # ---- 通道强制 ----
    # 只认 `local` / `online` 两个值，别的（含拼错、None、空串）一律当"没指定"。
    # 不抛异常是刻意的：通道名拼错不该让整张图失败，退回默认行为更友好。
    force = str(force_channel or "").strip().lower()
    if force not in ("local", "online"):
        force = ""
    if force:
        log.info("本次生图通道被强制指定：%s", force)

    # 通道说明是"这一次生图"的属性，每次进来先清空 ——
    # 不清的话上一次降级的说明会**粘到这一次**，模型就会无缘无故跟用户解释。
    #
    # `global` 必须声明在函数内**第一次赋值之前**。写在下面某个分支里会直接
    # SyntaxError：`name '_last_channel_note' is assigned to before global
    # declaration` —— 实测踩过。
    global _last_channel_note
    _last_channel_note = ""

    # 通道 C 优先：本机显卡出图，不花钱、不限量、无水印。
    #
    # 但它是「能用就赚到」，不是「必须成功」—— 这台机器会关机，
    # ComfyUI 不在是预期内的常态。所以任何一步失败都**立刻**转降级，
    # 不在这里重试：本地一张几十秒，重试一次用户已经等不住了。
    #
    # 超预算时会**强制**走这里（online_ok=False 时即使 comfyui_enable 关着也试），
    # 这样"省钱"优先于"配置"。
    if force != "online" and (force == "local" or cfg.comfyui_enable or not online_ok):
        try:
            if await probe_comfyui(session, cfg):
                # 图生图 / 风格转移：参考图都要先上传到 ComfyUI 的 input 目录，
                # 再把它给的文件名写进工作流。上传失败不致命 ——
                # 退回纯文生图，别让用户什么都拿不到。
                async def _upload(uri: str) -> Optional[str]:
                    try:
                        ref_mime, ref_raw = parse_data_uri(uri)
                        # 先缩到 1024：图生图的 latent 跟着参考图走，
                        # 大图会让 VAE 编码 + 采样慢到超时。见 _MAX_REFERENCE_SIDE。
                        ref_raw = await asyncio.to_thread(downscale_reference, ref_raw)
                        # 扩展名统一用 .png：上面那步会把图**重编码成 PNG**，
                        # 所以不能再用原始 mime 去起名字 —— 否则会写出
                        # 「.jpg 结尾、内容其实是 PNG」的文件。实测到过这种，
                        # 后果是外部工具按扩展名解码会失败（我们自己的读图工具就直接拒读），
                        # 排查时非常费解。内容是什么格式，名字就得是什么。
                        return await upload_image_to_comfyui(
                            session, cfg, ref_raw,
                            _reference_filename(ref_mime).rsplit(".", 1)[0] + ".png",
                            "image/png",
                        )
                    except ImageGenError as exc:
                        log.warning("参考图处理失败，这一张参考图忽略：%s", exc)
                        return None

                init_ref: Optional[str] = None
                if reference_image:
                    init_ref = await _upload(reference_image)

                style_ref: Optional[str] = None
                if style_reference:
                    # 风格图和内容图是分开的两张，所以必须各上传一次 ——
                    # 复用同一个文件名会让 ComfyUI 拿到后上传的那张，
                    # 表现是「风格转移用的其实是内容图」，画出来跟参考图一模一样。
                    style_ref = await _upload(style_reference)

                raw = await _generate_comfyui(
                    session, cfg, prompt, size,
                    init_image=init_ref, denoise=denoise,
                    style_image=style_ref, style_weight=style_weight,
                    checkpoint=force_checkpoint,
                )
                # 本地出的图没有服务商水印，不裁。
                # （`trim_watermark` 裁的是 Pollinations 打的**广告**，
                #   跟合规标识是两回事 —— 本地自用这条不涉及。）
                prepared = await asyncio.to_thread(prepare, raw, 0)
                if prepared is not None:
                    mime, data = prepared
                    mode = "文生图"
                    if init_ref and style_ref:
                        mode = "图生图+风格转移"
                    elif init_ref:
                        mode = "图生图"
                    elif style_ref:
                        mode = "风格转移"
                    log.info(
                        "生图校验通过（本机 ComfyUI）：%s %d 字节（原始 %d 字节，%s）",
                        mime, len(data), len(raw), mode,
                    )
                    if not cfg.comfyui_enable and not force:
                        # 本机通道本来是**关着**的，这次是被额度闸门顶上来干活的
                        # （见上面 `if cfg.comfyui_enable or not online_ok:`）。
                        # 用户配的主通道是在线，所以必须说一句 ——
                        # 否则画风突变会让人以为功能坏了。实测就是这么被反馈的：
                        # 「我豆包通道怎么关了」。
                        #
                        # 注意：`global _last_channel_note` 声明在函数开头 ——
                        # Python 要求 global 出现在**任何赋值之前**，
                        # 写在这个分支里会直接 SyntaxError（实测踩过）。
                        _last_channel_note = (
                            f"（这次是用**本机模型**画的，不是常用的在线模型 —— "
                            f"原因：{online_block_reason or '在线通道不可用'}。"
                            "本机模型画风不同，回话时顺口跟对方说一声。）"
                        )
                    return mime, data
                log.warning("本机 ComfyUI 出的图解不开，转降级通道")
                _mark_comfyui_down(cfg)
        except ImageGenError as exc:
            log.warning("本机 ComfyUI 生图失败，转降级通道：%s", exc)
            _mark_comfyui_down(cfg)
        except Exception:  # noqa: BLE001
            log.exception("本机 ComfyUI 生图异常，转降级通道")
            _mark_comfyui_down(cfg)

    # ---- 强制走本机、但本机没起来：**绝不降级** ----
    # 用户要本机图的就是「免费、不限量、无水印」。偷偷切到在线会花钱，
    # 那是背着用户改主意 —— 而且他下一句就会问「怎么画风变了、是不是扣钱了」。
    # 宁可如实报错，让他自己决定是开 ComfyUI 还是改用 /在线。
    if force == "local":
        raise ImageGenError(
            "指定用本机画，但本机 ComfyUI 没连上（服务没起来或还在启动）。"
            "跟对方说一声：把 ComfyUI 开起来，或者改用 /在线（要花钱）、/自动。",
            retryable=False,
        )

    # 超预算时不许用在线通道。本机上面已经试过了（超预算时会强制试），
    # 走到这里说明本机也不可用 —— 那就明确拒画，别偷偷花钱。
    if not online_ok:
        if force == "online":
            # 用户明确要在线，那就把"额度用完"这件事说清楚，
            # 别套用下面那句"本机也不可用"（那句的前提是走过本机，这里没走）。
            raise ImageGenError(
                f"指定用在线画，但在线额度已用完（{online_block_reason}）。"
                "跟对方说一声：要么等额度恢复，要么改用 /本机、/自动。",
                retryable=False,
            )
        raise ImageGenError(
            f"生图额度已用完（{online_block_reason}），本机通道也不可用，"
            "暂时画不了。跟对方说一声稍后再试。",
            retryable=False,
        )

    # 在线通道也能带参考图 —— 前提是配置里写了字段名
    # （`imagegen.api_ref_field`，豆包/方舟 Seedream 系用 "image"）。
    # 没配就按纯文生图出：多数接口收到不认识的参数会直接 400，
    # 所以宁可不发，也不要因为一个多余字段把整张图搞失败。
    #
    # 反过来也要说一句：如果配置的主通道本来是**本机**、结果本机没起来
    # （`comfyui_enable=True` 但上面那段没返回），这次就是降级到在线在画。
    # 不说的话用户会以为「怎么突然变成另一个画风了」。
    if cfg.comfyui_enable and not force:
        _last_channel_note = (
            "（这次是**在线模型**画的，不是本机模型 —— 本机 ComfyUI 没连上。"
            "画风会不一样，回话时顺口跟对方说一声。）"
        )

    ref_field = (getattr(cfg, "api_ref_field", "") or "").strip()
    if reference_image and not ref_field:
        log.info(
            "走在线通道，但没配参考图字段（imagegen.api_ref_field）—— "
            "本次按纯文生图出。配了就能用上参考图（豆包 Seedream 系填 image）。"
        )
    online_ref = reference_image if ref_field else None

    # 整轮生图的时间预算。**重试不能把用户拖过预算**。
    #
    # 实测：5.0-pro 单张要 75~110 秒。如果无条件重试一次，
    # 预期耗时 ≈ 90 + 3 + 90 = 183 秒 —— 已经超过「单次 HTTP 超时 180 秒」
    # 给人的心理预期，用户会以为卡死了。
    # 所以重试前先算：**剩下够不够再跑一次**，不够就直接认输、把错误如实报出去，
    # 而不是硬试一轮让总等待翻倍。
    #
    # 估算「再跑一次要多久」用已经跑掉的时间当参考（第一次就失败时用 timeout 的一半，
    # 因为实测成功耗时约为超时的一半左右）。
    deadline = time.time() + float(cfg.timeout) * 1.15

    for attempt in range(1, total + 1):
        attempt_started = time.time()
        try:
            global _last_api_tokens
            _last_api_tokens = 0
            raw = await generate(
                session, cfg, prompt, size, seed, init_image=online_ref
            )
        except ImageGenError as exc:
            last = exc
            # 限流等久一点，其它可重试错误等短一点；不可重试的直接认输。
            if exc.cooldown:
                wait = COOLDOWN_WAIT
            elif exc.retryable:
                wait = RETRY_WAIT
            else:
                wait = None
            if wait is not None and attempt < total:
                spent = attempt_started - (deadline - float(cfg.timeout) * 1.15)
                est = max(spent, float(cfg.timeout) * 0.5)
                if time.time() + wait + est > deadline:
                    log.warning(
                        "生图失败（%s）；估算再试一次要 %.0f 秒、已超出时间预算，"
                        "本次不再重试（避免让用户白等更久）",
                        exc, wait + est,
                    )
                    raise
                log.info("生图失败（%s），%.0f 秒后重试", exc, wait)
                await asyncio.sleep(wait)
                continue
            raise

        # 解码 + 裁切 + 重编码都是 CPU 活，丢线程跑，别堵事件循环。
        prepared = await asyncio.to_thread(prepare, raw, trim)
        if prepared is not None:
            mime, data = prepared
            # **只在真的出图成功后记账**。失败的那几次不计费
            # （方舟对未成功输出的图片不计费），记了会让额度虚高、提前封闸。
            _record_online_use(cfg, size, tokens=_last_api_tokens)
            total_n, _ = usage_total()
            quota = int(getattr(cfg, "api_free_quota", 0) or 0)
            log.info(
                "生图校验通过：%s %d 字节（原始 %d 字节）；"
                "本次 %d token，累计 %d 张%s",
                mime, len(data), len(raw), _last_api_tokens, total_n,
                f"（免费额度 {quota} 张）" if quota > 0 else "",
            )
            return mime, data

        last = ImageGenError("生成的图解不开（响应可能被截断）", retryable=True)
        if attempt < total:
            log.info("生成的图解不开，%.1f 秒后重试", RETRY_WAIT)
            await asyncio.sleep(RETRY_WAIT)
            continue

    raise last or ImageGenError("生图失败")
