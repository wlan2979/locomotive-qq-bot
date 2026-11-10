"""她自己的表情包：本机 ComfyUI 现画，近写实大头。

## 和 `sticker.py` 的分工

`sticker.py` 管的是**现成素材**（用户攒的真表情包 + QQ 内置小黄脸兜底），
这个模块管的是**现场画**。两者是同一个工具的两条路，不是两套功能：

    现场画（本模块） → 本地素材（sticker.py） → 内置小黄脸

顺序是用户 2026-10-19 定的：「AI 生成优先，素材只做兜底」。

## 为什么这次能做，而 sticker.py 里说「AI 表情包没梗」

`sticker.py` 那段结论针对的是**铺底**：拿 AI 图去冒充传播出来的梗，那当然不行。
但现在这件事不一样 —— 要的不是梗，是**她此刻的反应**。表情包的信息量
可以来自两个地方：「这张图我知道什么意思」（梗，AI 造不出），
或者「她刚才那个反应就长这样」（状态，AI 造得出，而且比素材库贴切）。

所以两条路并存：素材库留着兜底和接梗，现画负责贴合当下。

## 三件必须做对的事

**① 像表情包，不是像插画。**
白底、留白、轮廓清楚、构图锁在胸像。**2026-10-19 23:51 走过一轮大改**：
从 `chibi + sticker + thick outline + flat color`（Q 版玩具贴纸）换成
`bust, portrait + soft shading + detailed skin`（近写实大头）。
依据是用户给的参考图 + 一句「不要太q版」。逐项对照见 `STICKER_STYLE` 上方的表。

**② 是她本人。**
LoRA（`taotao_lora.safetensors`，触发词 `taotao`）管脸型、创可贴脸、
徽章、服装姿势；prompt 补 `black hair, brown eyes, blunt bangs` ——
**训练标注漏了发色瞳色**，不补的话每次采样发色随机
（实测一次黑发内层蓝挑染、一次灰白发）。服装标签逐字对齐训练集
（是 `grey vest`，不是 `grey sweater vest`）。

**③ 快。**
本机 RTX 5060 Laptop 上 animagine-xl-3.1（28 步）+ LoRA，768² 实测
20~40 秒一张（排队时到 60~70 秒）。**这个速度不可能让人等**，所以流程是：
池子里有图 → 秒发；没有 → 同步最多等 12 秒 → 超时转后台补画、本轮先降级。
慢只发生在后台，用户侧感知不到。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import random
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from .imagegen import ImageGenError, _generate_comfyui, probe_comfyui
from .sticker import MOODS

log = logging.getLogger(__name__)

# 生成的表情包落在这里，和用户手攒的 assets/stickers/ 分开放 ——
# 混在一起的话「删掉 AI 画的」就得一张张认，分开就一个目录整个删。
STICKER_ROOT = Path(__file__).resolve().parent.parent / "assets" / "stickers_gen"

# --------------------------------------------------------------------------- #
# 情绪 → 表情标签
#
# 只覆盖 sticker.MOODS 里那 16 个 key —— 那张表是**唯一的情绪字典**，
# 这里多一个少一个都会出现「能选但画不出来」或「画得出但选不到」。
# 缺 key 时 build_prompt 会退成中性微笑，不会报错。
#
# 写法上刻意用 **Danbooru 标签**而不是自然语言：anima 系虽然换了 Qwen 编码器，
# 但底模是在标签语料上训的，标签的命中率实测更高（同一句意思写成英文句子，
# 表情经常不到位）。每个情绪给 3~5 个标签，太多会互相打架。
MOOD_EXPRESSION: Dict[str, str] = {
    # ---- 正面 ----
    "开心": "happy, smile, closed eyes, blush, sparkle",
    "大笑": "laughing, open mouth, wide eyes, :d, tears of joy",
    "得意": "smug, confident smile, half-closed eyes, hand on hip",
    "兴奋": "excited, sparkling eyes, wide smile, arms up, star-shaped pupils",
    "赞": "thumbs up, wink, confident smile, sparkle",
    "加油": "determined, clenched fist, energetic, fired up",
    "感动": "touched, teary eyes, gentle smile, hand on chest",
    "感谢": "grateful, bowing slightly, hands together, closed eyes, smile",
    "安慰": "comforting, gentle smile, warm expression, patting motion",
    "卖萌": "cute, puppy eyes, blush, head tilt, hands together, sparkle",
    "爱心": "love, heart, blush, hands on cheeks, hearts around",
    "害羞": "blush, shy, covering face, looking away, steam",

    # ---- 认知 ----
    "疑问": "confused, tilted head, question mark, raised eyebrow",
    "惊讶": "surprised, wide eyes, open mouth, :o, exclamation mark",
    "思考": "thinking, hand on chin, looking up, closed mouth, serious",
    "恍然": "realization, wide eyes, open mouth, index finger up, lightbulb",
    "无语": "expressionless, half-closed eyes, sigh, sweatdrop, blank stare",
    "尴尬": "embarrassed, forced smile, sweatdrop, averting eyes, nervous",
    "摊手": "shrugging, spread arms, helpless smile, tilted head",
    "偷看": "peeking, half-closed eyes, hiding behind hands, mischievous",

    # ---- 负面 ----
    "生气": "angry, frown, pout, anger vein, crossed arms",
    "嫌弃": "disgusted, frown, narrowed eyes, leaning back, squint",
    "嘲讽": "smirk, condescending, half-closed eyes, raised eyebrow, sneer",
    "崩溃": "breakdown, holding head, wide eyes, tears, screaming",
    "害怕": "scared, trembling, wide eyes, cold sweat, backing away",
    "焦虑": "anxious, biting nails, cold sweat, worried, nervous",
    "疲惫": "tired, half-closed eyes, dark circles, slouching, sigh",
    "摆烂": "dead eyes, exhausted, limp, flat expression, arms hanging",
    "拒绝": "refusing, crossing arms, x gesture, frowning, turning away",
    "失望": "disappointed, sad, looking down, sigh, drooping shoulders",
    "抱歉": "apologetic, bowing, sweatdrop, forced smile, hands together",
    "哭": "crying, streaming tears, open mouth, sad, trembling",
    "委屈": "pout, teary eyes, trembling lips, sad, puppy eyes",

    # ---- 社交 ----
    "调戏": "smirk, mischievous, one eye closed, tongue out, evil grin",
    "收到": "saluting, ok sign, confident smile, one eye closed",
    "再见": "waving, smile, one hand up, goodbye",
    "睡觉": "sleeping, closed eyes, zzz, peaceful",
    "打哈欠": "yawning, hand over mouth, closed eyes, sleepy, tears",
    "摸鱼": "slacking off, relaxed, lazy smile, hands behind head",
    "吃饭": "eating, holding chopsticks, cheeks full, happy",
}

# 构图。默认胸像 —— 见下面 STICKER_FRAMING 的说明。
# 只有**结构上装不下胸像**的动作才覆盖它（躺平、睡觉这类全身姿势），
# 硬套会把动作砍掉，画出来是「站起来发呆」。
MOOD_FRAMING: Dict[str, str] = {
    "摊手": "cowboy shot, portrait",
    "摆烂": "full body",
    "摸鱼": "cowboy shot, portrait",
    "睡觉": "full body",
    "打哈欠": "cowboy shot, portrait",
    "吃饭": "cowboy shot, portrait",
}
DEFAULT_FRAMING = ""

# 情绪表里没有的说法走这里：目录名固定成「自由」，prompt 完全由 hint 决定。
#
# 为什么留这条后门：情绪表是**枚举**，而聊天里的反应不是枚举。
# 「被自己的话噎住」「一边笑一边心虚」这类状态永远塞不进格子。
# 卡死只会让模型把所有东西往最近的格子里挤 —— 那正是「表情看着程式化」的根。
FREE_MOOD_KEY = "自由"

# --------------------------------------------------------------------------- #
# 画风与构图
#
# ## 2026-10-19 23:51 大改：去掉 Q 版贴纸感，改近写实大头
#
# 用户给的参考图（原话「风格能不能改成这样的」+「不要太q版」）和最初那套
# 是两回事。逐项对比：
#
# | 维度 | 原来的（Q 版贴纸） | 现在（近写实） |
# |---|---|---|
# | 头身比 | `chibi` 2 头身以下 | 接近真实比例的大头 |
# | 边缘 | `sticker` die-cut 硬白边 | 无贴纸切割边 |
# | 轮廓 | `thick outline` 玩具感 | 细线 + 轻 `outline` |
# | 上色 | `flat color` 平涂 | `soft shading` 层次 |
# | 构图 | `upper body` 半身 | `bust` 胸像 |
#
# 实测四种组合后选 `bust` 而不是 `close-up`：close-up 极端特写偶尔会把
# 五官切得失衡，而表情包要缩到 200px 显示，脸挤成一团就看不出表情了。
# bust 构图稳，还能露出创可贴徽章这类**角色辨识点**（那是 LoRA 学的东西，
# 切太近就浪费了）。
#
# ⚠️ **旧池子的图全会失效**（文件名带 prompt 指纹），这是预期的 ——
# 换风格等于换了一批图，让它重画就行。
# --------------------------------------------------------------------------- #

# 画风标签。**不含构图** —— 构图见 `STICKER_FRAMING` / `MOOD_FRAMING`，
# 因为宽动作必须覆盖它，写死在一处会互相打架。
#
# ## 这串是 2026-10-20 绕了一大圈之后**还原**回来的（2026-10-21 00:15）
#
# 用户 10-20 说「效果还是不好」，回头看：他认可的那三张参考图
# （`A_chibi_happy` / `A_chibi_deadpan` / `E_sticker_768`）全是
# **anima-turbo + 无 LoRA** 用下面这串画的。
#
# 中间我为了两个诉求把它拆了：
#   · 「不要太Q版」→ 删了 `chibi`
#   · 「删脸上贴纸」→ 删了 `sticker`
# 拆完只剩 `flat color, outline`，效果就崩了 —— 双人、红领结、白边消失。
# 实测对比（`tests/_exp_restore_recipe.py`，同底模同参数）：
#
#   | 配方 | 结果 |
#   |---|---|
#   | 原始（下面这串） | 贴纸白边清晰、Q 版适度、单角色、脸上干净 ✅ |
#   | 拆过之后 | 双人、红蝴蝶结、白边只剩一条淡线 ❌ |
#
# 两个教训：
#   ① 「不要太Q版」要调的是**程度**，不是把 `chibi` 删干净 —— 删了就没 Q 版约束，
#      模型反而开始乱加人。
#   ② 脸上贴纸是 **LoRA** 带来的（训练集里大半张脸有贴纸）。切回无 LoRA 的
#      anima 之后这个问题自动消失，`sticker` 标签也就不需要防了。
STICKER_STYLE = (
    "simple background, white background, thick outline, flat color"
)

# 「Q 版 + 贴纸感」这两个标签**必须排在构图之前**（2026-10-21 实测）。
#
# 用户认可的那三张参考图，prompt 顺序是
#     ... chibi, sticker, upper body, simple background, ...
# 而拆散之后变成了
#     ... upper body, chibi, sticker, simple background, ...
# 后者出来的图会**多画一个全身小人**。
#
# 原因：SDXL/Anima 对 prompt 前段的权重更高。`chibi` 排在 `upper body` 后面
# 就被稀释了，模型收到的是「半身」这个强指令 + 「Q 版」这个弱提示，
# 它干脆两个都给 —— 一个半身特写当主体，一个 Q 版全身当装饰。
# 把 `chibi` 提到构图前面，双人现象消失。
STICKER_CHIBI = "chibi, sticker"

# 默认构图：**upper body**。
#
# 用户认可的那三张参考图全是半身 —— 人物占画面 70% 左右，四周留白，
# 白边才有地方画。`close-up` 会把白边直接裁掉（画面里没有"外缘"概念），
# 而且构图一近，LoRA 时代更容易触发「脸特写=带贴纸」的组合。
STICKER_FRAMING = "upper body"

# 手绘装饰元素，按情绪配。
#
# ## 现在是**空的**（2026-10-21 00:15）
#
# 原来给「开心/大笑」配了 `music note, heart`，实测 `music note` 会诱发
# 「第二个人在唱歌」的画面（anima 的 CFG=1，negative 拦不住多角色）。
# 而用户认可的那三张参考图，装饰全靠 `MOOD_EXPRESSION` 里的 `sparkle`
# 加模型自由发挥 —— 不需要额外标签。
#
# 留着这个空字典是为了以后想按情绪加装饰时有地方写（比如「生气」配 `anger vein`）。
MOOD_DECOR: Dict[str, str] = {}

# 服装。
#
# ## 2026-10-21 改回只写上装（V1 配方）
#
# 之前为了对齐 LoRA 训练集，这串是
# `school uniform, white shirt, grey vest, pleated skirt, black thighhighs`。
# 但表情包是 `upper body` 构图，**下半身根本看不见** —— 而写进去之后
# 模型会跟构图打架：既想展示裙子和大腿袜，又被告知只画上半身，
# 结果它画**两个角色**（一个半身特写 + 一个全身展示）。实测 5 个角色同框。
#
# 用户认可的那三张参考图，服装串里只有上装，所以没这个问题。
# 去掉下半身标签之后双人现象消失。
STICKER_UNIFORM = "school uniform, white shirt, grey sweater vest"

# --------------------------------------------------------------------------- #
# 手部强化：**正面 tag 这条路已证伪，别再试**（2026-10-20）
#
# 情绪标签里大量带手部动作（hand on hip / clenched fist / holding chopsticks /
# hands together / holding head ...），而 `upper body, portrait` 构图下手部
# 像素很少，animagine-xl-3.1 画出来是**一坨肉**：没有指缝、没有指节、
# 五指粘在一起。用户 2026-10-20 的原话是「生成的手问题很大」。
#
# ## 试过什么，结果如何（同 seed 对照，脚本 `2026-10-20-21-51-53/hand_fix.py`）
#
# | 干预 | 结果 |
# |---|---|
# | 什么都不做 | 手指糊成一坨，无指缝 |
# | 只开 hand detailer（旧参数 denoise 0.5） | **等于没开**，像素 diff 仅 10% |
# | detailer denoise 0.8 + SAM | 手部结构明显出来 ← **本次采用** |
# | prompt 加 `detailed hands, five fingers, visible fingers, ...` | 单图测试好看，**上真实链路翻车** |
#
# ## 为什么正面 tag 翻车（关键教训）
#
# 单独测（不挂 STICKER_STYLE、不加 detailer）时那串 tag 确实让指缝清楚了。
# 但放进真实链路后：
#   · 「吃饭」→ 背景长出**一片彩色手掌**图案，白底丢了，构图从胸像变全身
#   · 「崩溃」→ 画面里出现**巨大的棕色手**（别人的手抓着她），主体崩坏
#   · 「加油」→ 构图散掉，出现莫名橙色块
#
# 根因：SDXL 对 `hands` / `fingers` 的理解是**「画面里要有手」**，
# 不是「已有的手要画好」。这串词放在表情之后、构图之前（高权重位），
# 模型就开始往画面里塞额外的手。手部描述越具体（`five fingers`），
# 激发越强。
#
# 所以结论：**手部质量只能靠负面词 + detailer 重绘来管，不能靠正面词喊。**
# 负面词不会激发「画手」，只会削掉画坏的手；detailer 是局部重绘，可控。
# 这条经验对任何「想用正面 tag 强化某个局部」的改动都成立 —— 局部质量
# 靠 inpaint，不靠 prompt。
#
# 负面词见 `STICKER_NEGATIVE`，detailer 见 `imagegen.py` 的 hand_detailer 段。

# 正向质量前缀。**必须覆盖 config 里的那一串**：
# `imagegen.comfyui_positive` 默认带 `very aesthetic, absurdres`（追求极致画质和
# 超高分辨率），那是给插画用的。用在表情包上会把白底 Q 版往「精致大图」方向带 ——
# 边缘变锐、层次变多、背景里长出东西，反而更不像贴纸。
# 表情包只需要「干净」。SDXL 系（含 animagine）吃这个前缀；anima-turbo 由它自己的
# preset 覆盖成 `masterpiece, best quality`，这里不生效也不冲突。
STICKER_POSITIVE = "masterpiece, best quality, sticker"

# negative。
#
# ## 脸上贴纸：能压到约一半，压不干净（2026-10-20 实测）
#
# LoRA 是从 30 张训练图学的人物，那批图里**大半张脸上挂着贴纸**
# （小熊、云朵、星星造型）。原参考图 `assets/taotao/ref_*.jpg` 里没有这个特征 ——
# 是训练数据自带的，被 LoRA 当角色特征学进去了。用户 10-20 明确要求删掉。
#
# 实测四组（`tests/_exp_sticker_bandaid.py`，同 prompt 各 2 张）：
#
# | negative | 结果 |
# |---|---|
# | 裸词 `bandaid, bandage, plaster, medical tape, sticker on face` | **完全无效**，还长出黑熊和双人 |
# | `(bandaid:1.6)` | 2 张里 1 张干净 ← **最好** |
# | `(bandaid:1.5), (sticker:1.15)` | 仍残留（头上云朵贴纸） |
# | `(bandaid:1.6), (bandage:1.3), (plaster:1.2)` | 仍残留（两颗星） |
#
# 两个结论：
#   1. **必须用权重语法**，裸词在 CFG 6 下压不住 LoRA 的强特征。
#   2. **只能降到约 50% 概率，不能根除。** 因为训练标注里根本没有
#      `bandaid` 这个概念（`tag_frequency` 里没有），模型对这个特征没有
#      干净的「标签锚点」，负面词只能削边缘。
#
# **根治要重训 LoRA**：把 30 张训练图脸上的贴纸 inpaint 掉再训一遍。
# 在那之前，池子机制会吸收这个概率 —— 每个情绪 6 张，凑几张干净的很容易。
#
# ⚠️ **绝不写裸 `sticker`**：positive 里有 `sticker`（贴纸白边），
# negative 里写裸词会把白边一起削掉，模型失去约束就开始乱长东西
# （实测长出黑熊、双人、装饰满天飞）。这是第一轮失败的主因。
STICKER_NEGATIVE = (
    "lowres, bad anatomy, "
    # 手部负向（2026-10-20）。带权重是照着上面 bandaid 的经验来的：
    # CFG 6 下裸词压不住，`(x:1.2)` 才有实际效果。
    # 只挑最致命的三个（融合手指 / 多指 / 畸形手），不铺开 —— 这一串已经
    # 够长了，再加会把白边和 outline 一起削掉（上面 10-19 踩过）。
    "(bad hands:1.2), (extra fingers:1.2), (fused fingers:1.2), "
    "text, watermark, signature, "
    "multiple views, comic, panels, realistic, 3d, "
    "necktie, red bowtie, ribbon, bow, "
    "2girls, multiple girls, duplicate, clone, "
    "(bandaid:1.6)"
)

OUTPUT_SIDE = 512

# 贴纸白边的宽度（像素，基于 OUTPUT_SIDE=512）。
# 参考图里那圈边大约是画面短边的 2.5%，512 × 2.5% ≈ 13px。
STICKER_BORDER_PX = 13


def _add_sticker_border(
    img: Any, width: int = STICKER_BORDER_PX, margin_ratio: float = 0.06
) -> Any:
    """把主体缩进画布、四周留白，再画一圈白色贴纸边。

    ## 为什么不用 prompt 画

    2026-10-20 实测过 `sticker` / `outline` 两个标签：同一 prompt 下
    白边**时有时无**，而且 `sticker` 还会顺手激活 LoRA 学到的
    「脸上装饰贴纸」特征。用标签画边等于同时买了个副作用。

    后处理这条路的好处：
      · 宽度精确（13px 就是 13px，不随采样漂）
      · 形状可控（跟主体轮廓走，不会画出歪歪扭扭的切割线）
      · 零副作用（不参与生成）

    ## 为什么必须先缩进画布（这是第一版失败的原因）

    第一版直接对原图做膨胀，结果**白边根本看不见** —— 因为模型画出来的
    人物是**贴着画面边缘**的，四周没有背景像素，膨胀出来的环全被画布裁掉了。

    参考图里能看到白边，正是因为人物只占画面 85% 左右、四周留了空隙。
    所以这里先把主体缩到 88%、居中放到白底上，腾出边距，再画边。

    ## 实现

    白底图上「主体」= 非白像素：
      1. 二值化：亮度 < 245 算主体（245 而不是 255，容忍 JPEG 噪点）
      2. 膨胀 mask，膨胀量 = 白边宽度
      3. 膨胀后减去原 mask = 只属于白边的那一圈
      4. 先铺白底 + 白边，再把主体按 mask 贴上去

    阈值 245 是权衡：太高会把浅色头发丝/反光算成背景（白边会啃掉头发），
    太低会把背景噪点算成主体（白边糊成一团）。245 在动漫图上实测干净。
    """
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter

        if img.mode != "RGB":
            img = img.convert("RGB")

        side = img.width

        # ---- ① 先把背景刷成纯白 ----
        #
        # 模型给的「白底」并不纯：实测有淡粉、浅灰的渐变。直接拿亮度阈值判主体，
        # 这些淡色背景会被算进主体，白边就画在画面最外沿、被裁掉看不见。
        #
        # 用 **flood fill 从四角出发** 而不是全图阈值判断，是为了不误伤
        # 头发高光：高光虽然也亮，但它被深色头发包围、和边缘不连通，
        # 四角填充够不到它。全图阈值会把高光一起刷白，头发就秃了。
        work = img.copy()
        for corner in ((0, 0), (side - 1, 0), (0, side - 1), (side - 1, side - 1)):
            try:
                ImageDraw.floodfill(work, corner, (255, 255, 255), thresh=28)
            except Exception:  # noqa: BLE001
                pass  # 某个角刷不动不影响其他角

        # ---- ② 主体缩进画布，留出白边的位置 ----
        #
        # 第一版没做这步，白边直接看不见：模型画的人物**贴着画面边缘**，
        # 四周没有背景像素，膨胀出来的环全被画布裁掉。
        # 参考图里能看到白边，正是因为人物只占画面 85% 左右。
        inner = max(64, int(side * (1.0 - 2 * margin_ratio)))
        scaled = work.resize((inner, inner), Image.LANCZOS)
        canvas = Image.new("RGB", (side, side), (255, 255, 255))
        off = (side - inner) // 2
        canvas.paste(scaled, (off, off))
        work = canvas

        # ---- ③ 画白边 ----
        gray = work.convert("L")
        # 非白 = 主体。point 的 lambda 对每个像素值判一次，比逐像素循环快得多
        mask = gray.point(lambda v: 255 if v < 245 else 0)

        # MaxFilter 的 size 必须是奇数，膨胀半径 = (size - 1) / 2
        size = max(3, int(width) * 2 + 1)
        grown = mask.filter(ImageFilter.MaxFilter(size))

        # 白边 = 膨胀后 - 原 mask（ImageChops.subtract 会把负值钳到 0）
        ring = ImageChops.subtract(grown, mask)

        # 白底 → 贴白边 → 贴主体
        out = Image.new("RGB", work.size, (255, 255, 255))
        out.paste(Image.new("RGB", work.size, (255, 255, 255)), mask=ring)
        out.paste(work, mask=mask)
        return out
    except Exception as exc:  # noqa: BLE001
        # 画边失败不该让整张图作废 —— 没有白边也是能发的表情包
        log.warning("贴纸白边处理失败（不影响发送）：%s", exc)
        return img


# 每个情绪池子里最多留几张。超了删最旧的。
# 6 张足够「不重复」，又不至于把磁盘塞满（40×6×250KB ≈ 60MB）。
DEFAULT_CACHE_PER_MOOD = 6


def _character_tags(lora_on: bool = False) -> str:
    """角色固定特征。**延迟导入** core —— 那边也 import 本模块，顶层导入会成环。

    ## 有 LoRA 时：LoRA 管它学过的，prompt 管它没学的

    LoRA 只会学到**标注里出现过的东西**。桃桃这次训练集（30 张，
    `D:/DS/lora-data/taotao/*.txt`）的标注覆盖了：

        发型（long hair / low twintails / hair bun / ponytail）
        服装（各种）、姿势、场景、构图、表情

    **但完全没有发色和瞳色** —— WD tagger 那轮漏了（tag_frequency 里
    一个 `hair` 结尾的都没有）。于是发色对 LoRA 是自由变量，
    实测同一个 prompt 两次出图，一次黑发内层蓝挑染、一次灰白发。

    所以 LoRA 版不是「什么都不写」，而是**只写它没学的**：
        `taotao`    —— 激活角色（脸型、气质、创可贴脸这些全在里面）
        `black hair, brown eyes, blunt bangs` —— 补上标注漏掉的三项

    为什么发型**不写**：训练集里发型本来就是多样化的（20 张 low twintails、
    4 个 hair bun、2 个 ponytail、2 个 side ponytail），LoRA 主动学了「她会换发型」。
    写死 `low twintails` 等于跟这个学习成果对着干。
    """
    if lora_on:
        return "taotao, black hair, brown eyes, blunt bangs, 1girl, solo"

    try:
        from .core import SELF_CHARACTER_TAGS, SELF_DEFAULT_HAIR

        return f"{SELF_CHARACTER_TAGS}, {SELF_DEFAULT_HAIR}, {STICKER_UNIFORM}"
    except Exception:  # noqa: BLE001
        # core 导入失败（单测直接跑本模块时会这样）—— 用一份等价的兜底，
        # 保证「画出来还是她」，而不是直接不画。
        log.debug("拿不到 core 的角色标签，用内置兜底")
        return (
            "1girl, solo, black hair, brown eyes, petite, small build, "
            f"blunt bangs, sidelocks, long hair, low twintails, {STICKER_UNIFORM}"
        )


def clean_hint(raw: object) -> str:
    """清洗模型给的动作描述。

    只收 ASCII：底模吃的是英文标签，中文进去基本等于没写，还会污染整串 prompt
    （实测中文夹在标签串里会让后半段权重掉下来）。宁可丢掉。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if not s.isascii():
        log.info("表情包 hint 含非 ASCII 字符，忽略：%r", s[:60])
        return ""
    # 太长会盖过角色标签，掐掉
    return s[:160]


def _dedupe_tags(prompt: str) -> str:
    """按逗号切开去重，保留首次出现的顺序。

    为什么需要：`hint` 很可能和情绪表里的标签重了（崩溃表里已经有
    `holding head`，模型照样会写一遍）。重复的标签会被**加权两次** ——
    SDXL 系对重复 token 的权重是叠加的，实测会让那个特征过冲
    （同一个词出现两次，边框变粗一倍、眼睛大一圈）。
    """
    seen: set = set()
    out: List[str] = []
    for tag in str(prompt or "").split(","):
        t = tag.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return ", ".join(out)


def build_prompt(mood_key: str, hint: str = "", lora: bool = False) -> str:
    """拼出这张表情包的完整 prompt。

    顺序有讲究：**角色在最前**（底模对开头权重最高），然后表情、然后动作、
    然后装饰，最后才是构图和画风。反过来的话构图和画风标签会压过角色特征，
    画出来是个「白底路人」。

    `lora=True` 时角色部分退化成触发词（见 `_character_tags`），但**服装照旧拼** ——
    服装不是 LoRA 学的，是「她日常的样子」这个约定，得由 prompt 说话。
    """
    expr = MOOD_EXPRESSION.get(mood_key, "")
    char = _character_tags(lora)
    if lora:
        char = f"{char}, {STICKER_UNIFORM}"
    parts = [char]
    # 自由情绪（情绪表外的说法）没有固定表情标签 —— 全部交给 hint。
    # 这里不塞一个默认微笑：那会让「自由」这个后门退化成「又一个开心」。
    if expr:
        parts.append(expr)
    if hint:
        parts.append(hint)
    # 手绘装饰（音符/星星/爱心）。放在构图之前、表情之后 ——
    # 它属于「这张图的气氛」，不该抢角色的权重。
    decor = MOOD_DECOR.get(mood_key, "")
    if decor:
        parts.append(decor)
    # `chibi, sticker` 必须在构图之前 —— 顺序错了会多画一个全身小人，
    # 见 `STICKER_CHIBI` 的说明。
    parts.append(STICKER_CHIBI)
    parts.append(MOOD_FRAMING.get(mood_key) or DEFAULT_FRAMING or STICKER_FRAMING)
    parts.append(STICKER_STYLE)
    return _dedupe_tags(", ".join(p for p in parts if p))


# --------------------------------------------------------------------------- #


def _digest_for(mood_key: str, hint: str, lora_on: bool) -> str:
    """当前 prompt 组合的指纹。用于池子过滤。

    ## 为什么必须有这个东西（2026-10-19 踩到的真 bug）

    挂上 LoRA 之后实测，8 个情绪里 7 个都是 **0.0 秒**返回 —— 全命中了
    换 LoRA 之前存的旧图。根因：`_pool()` 只按情绪目录找图，不看图是
    用什么 prompt 画的。于是「换了 LoRA / 改了服装标签 / 换了底模」之后，
    池子里躺着旧图，**新 prompt 一张都用不上**，而日志里看着一切正常。

    这类 bug 最阴的地方：它不报错，只是让你以为改生效了。
    所以文件名里带指纹，取图时按当前指纹过滤 —— 配置变了旧图自动失效，
    不用手工清目录。
    """
    return hashlib.md5(build_prompt(mood_key, hint, lora=lora_on).encode("utf-8")).hexdigest()[:6]


class StickerArtist:
    """现场画表情包 + 管理生成出来的池子。

    选图规则（`draw`）：
      池子里有**这个会话没发过**的 → 直接复用，零延迟
      池子空 / 全发过 → 现画一张（9 秒），画完进池子

    这条规则的好处是「越用越快」：新情绪第一次要等，之后基本都是秒发。
    坏处是复用时会话可能已经换话题了 —— 但表情包本来就是按**情绪**发的，
    同一情绪下换张图照样贴切，不至于出戏。
    """

    def __init__(
        self,
        cfg: object,
        image_cfg: object,
        session: aiohttp.ClientSession,
    ) -> None:
        self.cfg = cfg
        self.image_cfg = image_cfg
        self.session = session
        # ComfyUI 是单队列，同时提交两张只会让两张都变慢。串行化。
        self._lock = asyncio.Lock()
        self._recent: "OrderedDict[str, List[str]]" = OrderedDict()
        self._max_sessions = 200
        self._drawn = 0
        self._failed = 0
        self._last_error = ""

    # ------------------------------------------------------------------ #

    def _seen(self, session_key: str) -> List[str]:
        seen = self._recent.get(session_key)
        if seen is None:
            seen = []
            self._recent[session_key] = seen
            while len(self._recent) > self._max_sessions:
                self._recent.popitem(last=False)
        else:
            self._recent.move_to_end(session_key)
        return seen

    def _lora_name(self) -> str:
        """当前生效的 LoRA 文件名。

        ## 底模不支持 LoRA 时**必须返回空**（2026-10-21 改）

        原来这里无条件 fallback 到 `imagegen.comfyui_lora`。现在分工是
        「表情包用 anima、生图用 animagine+LoRA」，而 anima 架构（Cosmos-Predict2
        底座 + Qwen3 编码器）**吃不了 SDXL LoRA** —— 它会在 `_generate_comfyui`
        的架构分支里被丢弃，但每张图都会记一条警告日志，看着像出错了。

        所以按底模名判断：anima 系直接返回空，别让它 fallback 上来。
        """
        ckpt = str(getattr(self.cfg, "checkpoint", "") or "").lower()
        if "anima-turbo" in ckpt or "anima-base" in ckpt or "anima-aesthetic" in ckpt:
            return ""
        return (
            str(getattr(self.cfg, "lora", "") or "").strip()
            or str(getattr(self.image_cfg, "comfyui_lora", "") or "").strip()
        )

    def _pool(self, mood_key: str, digest: Optional[str] = None) -> List[Path]:
        """池子里的图。给了 `digest` 就只认**用同一套 prompt 画的**。

        不给 digest = 「这个情绪有没有任何图」（预热和统计用）；
        给 digest = 「这个情绪有没有现在能用的图」（发表情包用）。
        两者必须分开，否则换了 LoRA 之后旧图会一直被复用（见 `_digest_for`）。
        """
        folder = STICKER_ROOT / mood_key
        if not folder.is_dir():
            return []
        try:
            files = sorted(p for p in folder.glob("*.png") if p.stat().st_size > 0)
        except OSError as exc:
            log.warning("读不到生成的表情包目录 %s：%s", folder, exc)
            return []
        if digest:
            files = [p for p in files if digest in p.name]
        return files

    def _mark(self, session_key: str, name: str) -> None:
        seen = self._seen(session_key)
        seen.append(name)
        limit = 8
        if len(seen) > limit:
            del seen[: len(seen) - limit]

    # ------------------------------------------------------------------ #

    def pick_only(self, session_key: str, mood_key: str, hint: str = "") -> Optional[Path]:
        """只从池子里挑，**绝不现画**。

        `sticker_gen.generate=false` 时这是主路径（2026-11-04 用户要求
        停止生成新表情包）。指纹照常按当前配置算 —— 换了配置旧图自动失效，
        这个规则跟 `draw()` 保持一致。
        """
        digest = _digest_for(mood_key, hint, bool(self._lora_name()))
        return self.pick_from_pool(session_key, mood_key, digest)

    def pick_from_pool(self, session_key: str, mood_key: str, digest: str) -> Optional[Path]:
        """只看池子，不画。零延迟。

        单独拆出来给调用方用：它要先判断「有没有现成的」，再决定是等还是走降级 ——
        这两件事的后果完全不同，混在一个 `draw()` 里就没法分辨了。

        `digest` 是当前 prompt 组合的指纹，只有同一套配置画的图才算「现成的」。
        """
        pool = self._pool(mood_key, digest)
        if not pool:
            return None
        seen = self._seen(session_key)
        fresh = [p for p in pool if p.name not in seen]
        if not fresh:
            return None
        chosen = random.choice(fresh)
        self._mark(session_key, chosen.name)
        log.info(
            "表情包复用池子：%s（情绪 %s，池子 %d 张里挑一张没发的）",
            chosen.name, mood_key, len(pool),
        )
        return chosen

    # ------------------------------------------------------------------ #

    async def draw(
        self,
        session_key: str,
        mood_key: str,
        hint: str = "",
        timeout: Optional[float] = None,
    ) -> Optional[Path]:
        """拿一张她自己的表情包。返回 None = 拿不到（调用方去走降级链）。

        `timeout` 是这个调用愿意**等多久**：
          · 有数 → 同步路径。超时就放弃，不等（换慢底模时靠这个保体验）
          · None  → 后台路径，用配置里的 `timeout`（animagine 要 50 秒，正常）

        为什么同步路径宁可放弃也不硬等：**群聊里没人愿意为一颗表情等 50 秒。**
        池子里有图就秒发，没有的话「先降级应付这轮 + 后台慢慢把池子补上」，
        比「干等 50 秒」和「直接不发」都强。
        """
        digest = _digest_for(mood_key, hint, bool(self._lora_name()))
        cached = self.pick_from_pool(session_key, mood_key, digest)
        if cached is not None:
            return cached

        path = await self._draw_new(mood_key, hint, timeout=timeout, digest=digest)
        if path is not None:
            self._mark(session_key, path.name)
        return path

    def _pending_dirs(self, digest: str = "") -> List[str]:
        """还没有可用图的情绪。给预热和状态显示用。

        带 digest（当前配置）时，旧配置留下的图不算「有」—— 否则换了 LoRA
        之后预热会以为自己干完了，一张新的都不画。
        """
        return [m.key for m in MOODS if not self._pool(m.key, digest)]

    async def _draw_new(
        self,
        mood_key: str,
        hint: str,
        timeout: Optional[float] = None,
        digest: Optional[str] = None,
    ) -> Optional[Path]:
        if not getattr(self.cfg, "enable", True):
            return None

        # 先探活。ComfyUI 没开是**常态**（机器关了就没了），不该在这里刷错误日志。
        try:
            if not await probe_comfyui(self.session, self.image_cfg):
                log.info("ComfyUI 没在跑，这次跳过表情包生成")
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("探测 ComfyUI 失败：%s", exc)
            return None

        # 挂在 LoRA 之前先定底模：`sticker_gen.checkpoint` 优先，
        # 空着才退回 `imagegen.comfyui_checkpoint`。
        # **这两者必须是同一个** —— LoRA 是按 animagine-xl-3.1 训的，
        # 换底模它就不认识了（图像上表现为「角色不像、但也没报错」）。
        checkpoint = (
            str(getattr(self.cfg, "checkpoint", "") or "").strip()
            or str(getattr(self.image_cfg, "comfyui_checkpoint", "") or "").strip()
        )

        # LoRA：`sticker_gen.lora` 优先，空着才跟 imagegen 共用。
        # 共用是合理的默认 —— 角色 LoRA 训出来就是给「画她」用的，
        # 表情包和普通生图本来都该是同一张脸。
        lora = self._lora_name()
        # 挂了 LoRA → prompt 只写触发词（外貌交给权重，别两头下注）
        prompt = build_prompt(mood_key, hint, lora=bool(lora))
        if digest is None:
            digest = _digest_for(mood_key, hint, bool(lora))
        local_cfg = replace(
            self.image_cfg,
            comfyui_negative=STICKER_NEGATIVE,
            comfyui_positive=STICKER_POSITIVE,
            comfyui_lora=lora,
            # `_generate_comfyui` 读的是这个名字，别跟 `sticker_gen.lora_strength`
            # 混 —— 两边都留着，这里是透传的桥。
            comfyui_lora_strength=float(
                getattr(self.cfg, "lora_strength", 1.0) or 1.0
            ),
        )

        limit = float(getattr(self.cfg, "timeout", 70.0))
        if timeout is not None:
            limit = float(timeout)
        t0 = time.time()
        async with self._lock:
            try:
                raw = await asyncio.wait_for(
                    _generate_comfyui(
                        self.session,
                        local_cfg,
                        prompt,
                        str(getattr(self.cfg, "size", "768x768")),
                        checkpoint=checkpoint or None,
                    ),
                    timeout=limit,
                )
            except asyncio.TimeoutError:
                # 用 **limit** 不是 timeout —— 后者的默认值是 None（后台
                # 不限时），None 不能拿去做格式化（实测 TypeError）。
                self._failed += 1
                self._last_error = f"超时（{limit:.0f}s）"
                log.warning("表情包生成超时（%.0f 秒），这次走降级", limit)
                return None
            except ImageGenError as exc:
                self._failed += 1
                self._last_error = str(exc)
                log.warning("表情包生成失败：%s", exc)
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._failed += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("表情包生成时发生未预期异常")
                return None

        elapsed = time.time() - t0
        path = self._save(raw, mood_key, prompt, digest)
        if path is None:
            self._failed += 1
            return None
        self._drawn += 1
        log.info(
            "表情包已生成：%s（情绪 %s，%.1fs，%.0fKB）prompt=%r",
            path.name, mood_key, elapsed, path.stat().st_size / 1024, prompt[:90],
        )
        return path

    def _save(
        self, raw: bytes, mood_key: str, prompt: str, digest: str = ""
    ) -> Optional[Path]:
        """缩放 + 存盘。缩放出问题就存原图 —— 能发出去比尺寸完美重要。"""
        folder = STICKER_ROOT / mood_key
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("建不了表情包目录 %s：%s", folder, exc)
            return None

        # 文件名带 prompt 指纹 —— `_pool(digest=...)` 靠它把旧配置的图排除掉。
        # 少这一段，换 LoRA / 换底模之后池子里躺着的旧图会被继续复用，
        # 而日志里看着一切正常（2026-10-19 实测踩到：8 个情绪 7 个 0 秒返回）。
        if not digest:
            digest = _digest_for(mood_key, "", bool(self._lora_name()))
        name = f"{mood_key}-{int(time.time())}-{digest}.png"
        path = folder / name

        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                # 先按短边居中裁成正方形：底模偶尔会给出非方图，
                # 直接 resize 会把脸压扁。
                side = min(img.size)
                left = (img.width - side) // 2
                top = (img.height - side) // 2
                img = img.crop((left, top, left + side, top + side))
                img = img.resize((OUTPUT_SIDE, OUTPUT_SIDE), Image.LANCZOS)
                # 白边：**只在 prompt 没让模型自己画的时候补**
                #
                # `STICKER_STYLE` 里带 `sticker` 时，模型会画出一圈贴纸切割边，
                # 这时候再叠一层后处理就是双重白边（实测更糊、边缘发毛）。
                # 反过来如果哪天把 `sticker` 从画风里去掉，这里自动接管。
                if "sticker" not in STICKER_STYLE:
                    img = _add_sticker_border(img)
                img.save(path, "PNG", optimize=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("表情包缩放失败，存原图：%s", exc)
            try:
                path.write_bytes(raw)
            except OSError as exc2:
                log.warning("表情包存盘失败：%s", exc2)
                return None

        self._trim(mood_key, digest, keep=path)
        return path

    def _trim(self, mood_key: str, digest: str = "", keep: Optional[Path] = None) -> None:
        """池子超上限就删最旧的。刚存的那张永远不删。

        `digest` 只在**当前配置**的图之间做淘汰。旧配置留下的图不参与计数 ——
        否则换 LoRA 之后它们会把额度全占满，新图一张都存不进来。
        （旧图本身不删，用户可能想留着对比；`print_stats` 会单独报数量。）
        """
        limit = int(getattr(self.cfg, "cache_per_mood", DEFAULT_CACHE_PER_MOOD) or 0)
        if limit <= 0:
            return
        files = self._pool(mood_key, digest)
        if len(files) <= limit:
            return
        # 按修改时间排，最旧的先走
        try:
            files = sorted(files, key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in files[: max(0, len(files) - limit)]:
            if keep is not None and path == keep:
                continue
            try:
                path.unlink()
                log.info("表情包池子超上限，删掉最旧的：%s", path.name)
            except OSError as exc:
                log.warning("删旧表情包失败 %s：%s", path.name, exc)

    # ------------------------------------------------------------------ #

    async def prewarm(self, mood_keys: Optional[List[str]] = None) -> int:
        """把还没有图的情绪各画一张。

        为什么值得做：不预热的话，**每个情绪第一次用都要等很久**。
        群聊里 40 个情绪铺开用，前几周会一直在等。后台慢慢画完，用户永远碰不到那个等待。

        ## 慢底模下这才是主力路径（2026-10-19 换成 animagine 之后）

        animagine-xl-3.1 是 28 步，768² 一张约 50 秒 —— 群聊里根本等不了。
        所以流程改成：

            池子有图  → 秒发（常态）
            池子没有  → 同步等一小会儿（sticker_gen.sync_timeout）
                       └ 超时 → 后台接着画 + 本轮先走降级（素材/小黄脸）

        也就是说 **animagine 的慢几乎全被吸收在后台**。预热就是把「迟早要等的」
        提前挪到没人看的时段。40 个情绪跑完约 30 分钟，期间每次启动只补缺失的。

        启动后延迟执行（由调用方控制），避免和机器人自己的启动自检抢显卡。
        """
        # 按**当前配置**的指纹算「哪些情绪还缺图」。不带这个的话，换 LoRA 之后
        # 旧配置留下的图会被当成「已经有了」，于是预热的图一张都不画。
        current = _digest_for("", "", bool(self._lora_name()))
        keys = list(mood_keys or [m.key for m in MOODS])
        keys = [k for k in keys if not self._pool(k, _digest_for(k, "", bool(self._lora_name())))]
        if current and not keys:
            log.info("表情包预热：当前配置下各情绪都已有图，无事可做")
            return 0
        made = 0
        for i, key in enumerate(keys):
            t0 = time.time()
            log.info("表情包预热 %d/%d：开始画「%s」", i + 1, len(keys), key)
            if await self._draw_new(key, ""):
                made += 1
                log.info(
                    "表情包预热「%s」完成（%.1fs，累计 %d 张）",
                    key, time.time() - t0, made,
                )
            # 每张之间歇一下：连打会让 ComfyUI 队列一直满着，
            # 这时如果有人 @ 她要画普通图，会排在预热后面白等。
            await asyncio.sleep(1.5)
        if made:
            log.info("表情包预热完成：新画了 %d 张", made)
        return made

    def stats(self) -> Dict[str, object]:
        total = sum(len(self._pool(m.key)) for m in MOODS)
        pending = self._pending_dirs()
        return {
            "pool": total,
            "pending": len(pending),
            "drawn": self._drawn,
            "failed": self._failed,
            "last_error": self._last_error,
        }


# --------------------------------------------------------------------------- #
# 命令行：看一眼池子里有什么


def print_stats() -> int:
    total = 0
    empty: List[str] = []
    for mood in MOODS:
        folder = STICKER_ROOT / mood.key
        count = len(list(folder.glob("*.png"))) if folder.is_dir() else 0
        total += count
        if count == 0:
            empty.append(mood.key)
        print(f"  {mood.key}：{count} 张")
    print()
    print(f"  合计 {total} 张（{STICKER_ROOT}）")
    if empty:
        print(f"  还空着：{'、'.join(empty)}")
        print("  空着的情绪第一次发表情时要现画，约 9 秒。")
    return 0


if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO)
    _sys.exit(print_stats())
