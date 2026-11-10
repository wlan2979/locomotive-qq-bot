"""配置加载与校验。

配置来源优先级：环境变量 > config.json 默认值。
字符串里支持 ${ENV_NAME} 占位符，方便把密钥放到环境变量里而不落盘。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    """递归展开 ${ENV} 占位符。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _build(cls: type, data: Any):
    """按 dataclass 字段名过滤后构造实例，多余字段忽略。"""
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _resolve_prompt(value: str, base_dir: Path) -> str:
    """system_prompt 支持 "@文件名"，从配置目录读取人设文本。

    人设动辄几百上千字，直接塞进 JSON 会变成一行超长的 \\n 转义串 ——
    想改一句话得在整行里翻，还容易把引号写坏。
    拆成独立文件后能用编辑器正常写，也方便备几套随时换。

    读不到或内容为空时回退到内置默认人设，不让机器人因为人设文件丢失而起不来。
    """
    text = (value or "").strip()
    if not text.startswith("@"):
        return value

    name = text[1:].strip()
    if not name:
        log.warning("system_prompt 是空引用（只有一个 @），沿用内置默认人设。")
        return BotConfig.system_prompt

    path = Path(name)
    if not path.is_absolute():
        path = base_dir / name

    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.error("人设文件读不到：%s（%s）—— 回退到内置默认人设。", path, exc)
        return BotConfig.system_prompt

    if not content:
        log.warning("人设文件是空的：%s —— 回退到内置默认人设。", path)
        return BotConfig.system_prompt

    log.info("已加载人设文件：%s（%d 字）", path, len(content))
    return content


# --------------------------------------------------------------------------- #


@dataclass
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    reconnect_interval: float = 3.0
    max_reconnect_interval: float = 60.0
    call_timeout: float = 30.0
    heartbeat_timeout: float = 90.0


@dataclass
class DeepSeekConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    temperature: float = 1.0
    # 开思维链之后 max_tokens 要留够 —— 推理 token 也算在这个上限里，
    # 被推理吃光的话 `content` 会返回**空字符串**（接口通了但没输出）。
    max_tokens: int = 4096
    timeout: float = 120.0
    # 默认开思维链。2026-09-30 实测对比（temperature 1.0，同一套 system prompt）：
    #
    #   关掉：连基础算术都会错 —— 「8:00 出门 + 步行 12 分 + 地铁 25 分 + 步行 8 分」
    #         答 **8:53**；开推理才是 **8:45**。
    #   延迟：关 0.9~2.1 秒 / 开 1.3~1.8 秒 —— **没有变慢**。
    #   代价：输出 token 涨 4~5 倍（推理 token 占大头）。
    #
    # 而且开推理**不会磨掉人设**：同一套 persona 下，语气词（「少来」「我又没出门，哪知道」）
    # 全在，只是内容实了一截。所以默认开。
    #
    # **别为了「让回复短一点」去关它。** 我这么干过一次，被上面的实测数据打回来了：
    # 回复长不长由**人设和长度上限**决定，不是推理造成的 —— 实测同一个问题
    # 关掉推理照样能写很长，而代价是无中生有的算错和不省时间。
    # 要缩短回复请改 `persona.txt` 和 `bot.max_reply_chars`。
    thinking: bool = True
    reasoning_effort: str = "high"

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass
class BotConfig:
    # 群聊触发方式：at（@机器人才响应） / prefix（前缀） / both / all（全部消息）
    group_trigger: str = "at"
    group_prefixes: List[str] = field(default_factory=lambda: ["/ai", "#ai"])
    # @ 过一次之后，多少分钟内该群的普通消息也响应。**0 = 关闭（每次都要 @）**。
    #
    # 默认 2 分钟：让「@ 一次之后能连着聊几句」成立，不用每句都 @。
    #
    # 但这会把「这段对话归我管」的范围放大 —— 实测开着窗口时 165 条群消息
    # 回了 51 条（回复率 31%），而且多数是**别人之间的闲聊**。
    # 所以窗口必须配两道约束一起用（两个都已经默认开着）：
    #   · `reply_min_gap`   —— 连发只回一次，硬间隔
    #   · 窗口内的「是不是在跟我说话」判断（见 core 里的 hint）
    # 想彻底不插嘴就设 0，代价是每句都得 @。
    #
    # 固定窗口，**不因别人说话而续期** —— 窗口内别人接着说话不会把窗口往后推。
    # 更早的版本是滑动窗口，群里一活跃就永远不过期，一次 @ 之后每条都接。
    #
    # 但**机器人自己回复时会续期**，见下面的 `awake_renew_on_reply`。
    group_activate_minutes: float = 2.0
    # 机器人**回复之后**要不要把激活窗口续满（2026-10-03 按用户要求加）。
    #
    # 为什么加：原来的固定窗口下，正聊到一半会因为窗口到期而突然不理人 ——
    # 用户得重新 @ 一次，对话硬生生断掉。续期让「连着聊几句」接得上。
    #
    # 为什么绑在**回复**上、而不是「每收到一条消息」：后者就是滑动窗口的
    # 老毛病 —— 群里只要一直有人说话，窗口就永不过期，一次 @ 之后再也停不下来。
    # 绑在回复上温和得多：机器人不说话 → 窗口不续 → 到点自己闭嘴。
    #
    # ⚠️ **代价：理论上可以一直续下去。** 群里持续有人跟它说话、它持续回，
    # 窗口就永不失效（频率受 `reply_cooldown` 的 30 秒约束）。
    # 觉得话多就把这项设 false，回到「只在被 @ 时续期」的旧行为。
    awake_renew_on_reply: bool = True
    # 同一群两次回复之间至少隔这么多秒（0 = 关闭）。
    # 窗口内每条消息都会触发回复，群里几个人连着说话就变成刷屏，用这个兜住。
    #
    # **两道豁免**（见 core 里的闸门判断）：
    #   ① 明确 @ 机器人 —— 点名必须回。
    #   ② **同一个人接着说** —— 那是连续对话，不是刷屏。
    #
    # ② 是后来补的。只有 ① 的时候实测漏得很惨：用户吐槽四句只听见第一句
    # （「手指出问题了」回了，「还有六指」「有问题吗」「明明给手部优化过了」
    # 全被冷却吃掉），越急着补充越没人理。
    #
    # 30 秒：实测群里出现「多人轮流说话」时，10 秒挡不住刷屏感；
    # 而「同一个人接着说」本来就走豁免 ②，所以加长到 30 秒不会漏掉连续对话。
    reply_cooldown: float = 30.0
    # 当前会话的回复间隔；同一人短时间连发会合并，间隔内的消息等待处理。
    reply_min_gap: float = 3.0
    # 机器人自己的名字（可多个）。用来在**窗口内没 @ 的消息**里判断
    # 「是不是在叫它」：消息里出现了这些词，就当成在跟它说话。
    #
    # 人设换了记得同步改（persona.txt 第一行「你叫「桃桃」」）。
    # 为什么不用回复引用（reply 段）来判断：那要跟踪自己发出的消息 ID，
    # 而 OneBot 的 `send_group_msg` 不回消息 ID，得动接口层 —— 不值得。
    # 名字这条信号已经够准，剩下的交给模型的上下文判断。
    self_names: List[str] = field(default_factory=lambda: ["桃桃"])
    # 主人（本人）的 QQ 号。**这里是整份配置里唯一影响「她怎么对待你」的东西。**
    #
    # 为什么要单独一层：她本来对所有人都只有一副面孔 —— 接梗、夸张、发癫，
    # 那是对外的社交外壳。对自己人不需要壳。名单里的号跟她说话时，
    # 她会卸下来：会抱怨、会嫌烦、会直接说不知道、会要东西。
    # 名单外的人，她继续演。
    #
    # 空列表 = 对所有人一个样（旧行为，不会崩）。
    owner_ids: List[int] = field(default_factory=list)
    private_enabled: bool = True
    reply_quote: bool = True
    max_reply_chars: int = 1200
    system_prompt: str = "你是一个在 QQ 里聊天的助手。说话自然、简洁、口语化。"
    history_max_turns: int = 20
    history_ttl_minutes: int = 120
    history_persist: bool = True
    # ---- 滚动摘要（见 bot/summarize.py）----
    #
    # 超出 history_max_turns 的消息本来会被直接丢掉，机器人对早先的对话就彻底
    # 失忆（"我不吃香菜" 说 20 轮之后它就当没听过）。摘要把那些**即将被丢掉**的
    # 内容压成几句话留下，所以是纯赚 —— 不会挤占还能留住的细节。
    summary_enabled: bool = True
    # 多久检查一次。压的触发条件是「历史真的超出上限」，所以调快调慢都只是
    # 检查频率，不会让摘要把还留得住的内容压掉。
    summary_interval_minutes: float = 10.0
    # 压缩时留几条不动（给摘要一点上下文余量，避免只压到半句话）
    summary_keep_turns: int = 4
    # 攒够几轮「尚未摘要」的新对话就压一次，不必等历史上限。
    # 为什么要这个：只按「达到上限」触发的话摘要永远慢一步 —— 等超限时最旧的
    # 内容已经被裁掉、没东西可压了；而且历史不足上限的会话永远不会被摘要。
    summary_batch_turns: int = 6
    concurrency: int = 4
    enable_vision: bool = True
    enable_search: bool = True
    max_image_mb: float = 20.0
    # 回复前缀，留空则不加
    reply_prefix: str = ""

    # ---- 分条发送 ----
    #
    # 真人聊天经常连着发几条短的（「诶在呢」/「你找我啥事」），不是把所有话
    # 塞进一条。所以让模型用分隔符自己标边界，发送时拆成多条。
    #
    # 注意 `max_reply_chars` 那条是**另一回事**：它只在消息超长时才切，
    # 正常长度永远是一条。这里管的是「正常长度也主动分条」。
    #
    # 关掉（false）之后分隔符会被当成普通文本发出去，回复退回「一条到底」。
    split_messages: bool = True
    # 分隔符。用 `|||` 是因为模型不会自然输出它，不会跟正文打架。
    # **别改成 \n\n** —— 模型现在就拿 \n\n 分段，那会变成「每条都拆」。
    split_separator: str = "|||"
    # 一次回复最多拆几条。超出的部分会并入最后一条（不丢内容）。
    # 上限别给太大：连着发五六条就是刷屏了。
    split_max_parts: int = 3
    # 条与条之间的间隔（秒），在这个区间里随机取。
    # 真人不是固定节奏 —— 固定 0.4 秒一条，看着像脚本在跑。
    split_gap_min: float = 0.5
    split_gap_max: float = 1.5


@dataclass
class SearchConfig:
    provider: str = "bocha"
    api_key: str = ""
    endpoint: str = "https://api.bochaai.com/v1/web-search"
    count: int = 6
    max_uses_per_turn: int = 3
    timeout: float = 20.0


@dataclass
class ImageGenConfig:
    """生图。三条通道，默认「本机显卡 > 有 key > 免费额度」。

    通道 A —— Pollinations（零配置，开箱可用）
      免费、免注册、不用 key。
      免费档的实情（2026-09-30 实测，别被文档骗了）：
        * 只有 `sana` 模型稳；`flux` / `turbo` 经常返回 HTTP 402。
        * 右下角有 pollinations.ai 水印，`nologo=true` **不生效** —— 只能裁掉。
        * **限流很紧**：连着打两三张就开始 402，隔十几秒到一分钟才恢复。
          这是「能用的免费额度」，不是「稳定的服务」，别指望它扛量。
        * 只吃英文提示词。
      相关字段：endpoint / model / negative_prompt / enhance。

    通道 B —— 任意 OpenAI 兼容接口（填了 api_key 自动启用）
      `POST {api_base}/images/generations`，头带 `Authorization: Bearer <key>`。
      常见三家：
        * 智谱      api_base=https://open.bigmodel.cn/api/paas/v4  api_model=cogview-3-flash
        * 硅基流动   api_base=https://api.siliconflow.cn/v1        api_model=Kwai-Kolors/Kolors
        * OpenAI   api_base=https://api.openai.com/v1             api_model=gpt-image-1
      `data[0]` 里给 `url`（智谱、硅基流动）还是 `b64_json`（OpenAI）代码自己认，
      不用配。相关字段：api_key / api_base / api_model / api_sizes。

    通道 C —— 本机 ComfyUI（默认优先尝试，不可用时自动降级）
      走本机显卡，二次元底模（Illustrious XL 这类）。不花钱、不限量、无水印。
      硬前提：**机器得开着、ComfyUI 得在跑**。所以这条通道带三重降级，
      任何一步出问题就退回 A / B，不让群里的人干等：
        * 探测不到服务（`GET /system_stats` 连不上）→ 直接走降级；
        * 提交或轮询超时 → 走降级；
        * 拿到的字节解不开 → 走降级。
      相关字段：comfyui_* 那一组。

    `provider` 决定 A / B 走哪条：
      * `auto`（默认）—— 填了 api_key 就走 B，否则走 A；
      * `pollinations` / `openai` —— 强制指定（比如手里有 key 但想先省着用）。

    通道 C 不占 `provider` 这个字段 —— 它由 `comfyui_enable` 单独开关，
    并且只在**运行时**探测决定用不用（它是「现在能不能用」，不是「配置写了什么」）。
    """

    enable: bool = True
    provider: str = "auto"

    # ---- 通道 A：Pollinations ----
    endpoint: str = "https://image.pollinations.ai/prompt"
    # 免费档只有 sana 稳，换 flux/turbo 会被 402 挡回来
    model: str = "sana"
    negative_prompt: str = ""
    enhance: bool = False

    # ---- 通道 B：OpenAI 兼容 ----
    api_key: str = ""
    api_base: str = ""
    api_model: str = ""
    # 画幅覆盖，例如 {"portrait": "768x1344"}。留空用内置的通用值。
    # 之所以留这个口子：各家支持的尺寸不一致（智谱认 768x1344 却不认 768x1024），
    # 与其猜，不如让用的人按自己那家的文档改一行。
    api_sizes: Dict[str, str] = field(default_factory=dict)
    # 在线通道的**参考图字段名**。填了才会把参考图发给接口。
    #
    # 各家叫法不一样，而且**多数接口收到不认识的参数会直接 400**
    # （智谱连 negative_prompt 都拒），所以默认**不发**这个字段。
    # 换到支持的接口时填上即可，不用改代码：
    #   · 火山方舟 / 豆包 Seedream 系列 → "image"
    #     （同一接口兼文生图与「在参考图基础上修改」）
    #   · 其它家可能叫 init_image / reference_image，按文档填对应的名字
    api_ref_field: str = ""
    # ---- 免费额度保护（只作用于**在线通道**，本机不花钱不受限） ----
    #
    # 为什么要它：在线通道是"来一张画一张"。群里有人连点、或者模型自己反复重试，
    # 就能把免费额度打光 —— 打光之后如果不巧**没开「安心体验模式」**，
    # 就会开始真扣费。
    #
    # 方舟的免费额度是**按张数**给的（实测 200 张）。
    # 配合控制台的「安心体验模式」（免费额度用完即停、不自动转付费）双保险：
    # 那个模式是服务端兜底，这里是我们这边的闸门，先一步拦住。
    #
    # 任一触发就**自动切回本机通道**（不花钱），本机也不可用才拒画。
    # 填 0 表示该维度不限制。
    api_hourly_limit: int = 0         # 最近 1 小时最多几张（0 = 不限制）
    # 最近 24 小时最多几张。
    #
    # **这个值必须高于真实用量，否则会挡住正常使用。** 实测踩过：
    # 最初设 20，结果用户正常聊天就被挡 —— 日志里
    #     「在线通道已达预算上限（最近 24 小时已画 20 张）」
    # 然后静默降级到本机通道，画风突变，用户以为「豆包通道被关了」。
    #
    # 实测用量（2026-10-02）：单小时峰值 6 张、平均 1.5 张/小时，
    # 外推单日约 36 张。所以 60 是「正常用不到、脚本跑飞时能兜住」的位置。
    #
    # 真正防超额的是 `api_free_quota`（累计 200 张），这个日上限只是防跑飞。
    api_daily_limit: int = 60
    # 免费额度总张数（方舟给的是 200 张）。**累计**计数，不随时间重置，
    # 用满就永久停用在线通道 —— 免费额度用完就是用完了。
    api_free_quota: int = 200
    # 每张的估算单价（元），**仅用于在日志里给个钱的参考**。
    # 免费额度内不花钱；这里只是让"如果开始计费大概是多少"有个概念。
    api_cost_per_image: float = 0.25

    # ---- 通道 C：本机 ComfyUI（二次元底模，走本地显卡） ----
    #
    # 为什么单独要这条通道：A 是免费额度（限流紧、768² 上限、带水印），
    # B 按张计费。本机有显卡时，二次元画风只有本地 SDXL 底模出得来，
    # 而且不花钱、不限量、无水印。
    #
    # 代价：**机器得开着**。所以这条通道**永远带降级** ——
    # 探测不到 ComfyUI、出图超时、图解不开，一律退回 A / B，不让用户干等。
    comfyui_enable: bool = False
    local_only: bool = False
    comfyui_url: str = "http://127.0.0.1:8188"
    # 采样参数。Illustrious XL 这套是社区常用值，效果不好就改。
    comfyui_steps: int = 28
    comfyui_cfg: float = 6.0
    # 采样器 / 调度器。**换底模后必须重新标定，别沿用旧值。**
    #
    # ⚠️ **这两个值现在只是「默认 / 兜底」** —— 真正生效的是
    # `imagegen.COMFYUI_MODEL_PRESETS` 里按底模配的那套（`/model` 切换时用）。
    # 只有认不出的底模才会落回这里。
    #
    # 下面这组是给 **WAI-illustrious** 标的。实测同 seed 对照
    # （832x1216、28 步、CFG 6）：
    #
    #   euler_ancestral + normal   细节好，但对比偏硬、肤色偏亮
    #   euler_ancestral + karras   略好，手的结构偏弱
    #   dpmpp_2m + karras          色彩更艳，但脸略僵、手偏弱
    #   dpmpp_2m_sde + karras      **肤色自然、眼睛层次好、画面干净、手最经得起看** ← 选它
    #
    # 代价：每张多约 4 秒（32s vs 28s）。画质值这个钱。
    #
    # ⚠️ **这套用在 animagine 上是错的**（2026-10-03 实测）：脸过曝发糊、
    # 合十的手糊成一团肉块。animagine 要 `euler_ancestral + normal`
    # —— 已写进预设表，别把这里的值改成通用的，它只对 Illustrious 成立。
    comfyui_sampler: str = "dpmpp_2m_sde"
    comfyui_scheduler: str = "karras"
    # 独立 VAE 文件名（放在 models/vae 下）。留空则用底模内置的那个。
    #
    # 为什么要单独给：Illustrious 系配 SDXL 专用 VAE 是通行做法。
    # 实测两者确有差异（约 49% 像素不同，平均差 0.3、最大 28~44）——
    # 外部 VAE 肤色略暖、暗部更实。差异不大，但标准做法且无副作用。
    # 换底模时如果内置 VAE 正常，这里留空也可以。
    comfyui_vae: str = ""
    # 底模文件名，留空则自动取 ComfyUI 里加载到的第一个 checkpoint。
    # 想固定用某个模型就把完整文件名写进来（含 .safetensors）。
    comfyui_checkpoint: str = ""
    # ---- LoRA（角色一致性）----
    #
    # 放在 models/loras 下的 LoRA 文件名（含 .safetensors）。留空 = 不挂 LoRA，
    # 出图路径和以前**逐字节一致**（不多带任何节点）。
    #
    # **这是目前唯一能做到「像素级同一张脸」的手段。** 别再往 IP-Adapter /
    # InstantID 上耗时间，那两条路已实测走不通（见 SKILL: comfyui-character-fidelity）：
    #   · IP-Adapter 传的是「风格与氛围」，不是「身份」—— 权重压到 0.2 都拉不住发色
    #   · InstantID 靠 InsightFace 提人脸特征，那是**真人脸**训练的，
    #     动漫图当输入会直接抛 `No face found in image`
    #
    # 挂了 LoRA 之后，**prompt 里不要再写这个角色的外貌标签** ——
    # LoRA 已经知道它长什么样，重复写会双重加权，把脸带偏。
    # 只写服装 / 姿势 / 场景（和 `draw_self` 那条规则同理）。
    comfyui_lora: str = ""

    # LoRA 的**触发词**。配了它之后，只有 prompt 里出现这个词才会挂 LoRA。
    #
    # ## 为什么需要（2026-10-22 的真实事故）
    #
    # `comfyui_lora` 原来是无条件挂的。用户训了桃桃的角色 LoRA 之后，
    # 画**宫园薰**（`kaori miyazono, blonde hair, blue eyes`）也照样挂上去，
    # 脸型/发色全被拽向桃桃 —— 用户的反馈是「animagine 模型效果不如之前了」。
    # 模型没变差，是 LoRA 污染了每一个角色。
    #
    # LoRA 本来就是靠触发词激活的。配了触发词之后：
    #   prompt 含 `taotao` → 挂（画她自己）
    #   prompt 不含        → 不挂（画别人）
    #
    # **留空 = 保持旧行为（总是挂）** —— 不悄悄改变已有配置的语义。
    comfyui_lora_trigger: str = ""
    # LoRA 强度，同时作用于 model 和 clip。
    #
    # 标定参考：**0.7~0.9 是常用区间**。低了不像（角色特征吃不住），
    # 高了会「过拟合式崩坏」—— 脸僵、构图死板、换个姿势就崩。
    # 底模不同响应也不同，换底模要重标，别沿用。
    # 允许负值（负强度 = 反向减去该特征），但正常用不到。
    comfyui_lora_strength: float = 0.8

    # 挂 LoRA 时**自动追加**到 prompt 末尾的外观描述（逗号分隔的 Danbooru 标签）。
    #
    # ## 为什么需要（2026-10-24 实测，别删）
    #
    # 打标时把 `black_hair` / `long_hair` / `black_eyes` 剔掉了 —— 这是角色 LoRA
    # 的标准做法，目的是让触发词**隐式**绑定外观，这样才换得动发色瞳色。
    #
    # 但实测发现触发词的绑定力不够，**换个 seed 角色就漂**：
    # 同一份 prompt、同一个 LoRA、strength 0.6/0.8/1.0 全试过 ——
    #   seed 777 / 888 → 黑长直（对）
    #   seed 20261024  → 银发蓝眼（错，服装倒是对的）
    # 说明 LoRA 学到了服装（标注里有），没学到发色（标注里被我剔了）。
    #
    # 修法就是这一项：挂 LoRA 时把这串外观词拼进 prompt。实测同一个坏种子
    # 20261024，加上 `black hair, long hair, black eyes` 立刻变回黑长直。
    #
    # 留空 = 不追加（旧行为）。
    # ⚠️ 只在**这一轮真的挂了 LoRA** 时才追加 —— 画别的角色不能加，
    # 否则会把人家也画成黑长直。判断在 imagegen 的架构分支之后做。
    comfyui_lora_appearance: str = ""
    # ---- 手部 detailer（Impact Pack）----
    #
    # 出图后自动检测手部（hand_yolov8s）并放大重绘一遍（0.5 denoise）。
    # 动机：负面词里的 bad hands 对手部崩坏基本无效（2026-10-03 实测
    # 「负面词救不回崩坏的手」），detailer 是目前唯一对症的手段。
    # 代价：每张 +5~10 秒；画面里检测不到手时 1~2 秒就跳过。
    # 只对 SDXL 系底模（animagine/wai）生效；Anima 分支暂不接。
    # 依赖：ComfyUI 侧需装 Impact Pack/Subpack + models/ultralytics/bbox/hand_yolov8s.pt
    # （2026-10-10 已装好并端到端验证）。
    comfyui_hand_detailer: bool = True
    # 外部工作流 JSON 路径。留空用内置的 SDXL 文生图工作流。
    # 内置工作流已支持：图生图 / IP-Adapter 风格转移 / 外部 VAE / **LoRA**。
    # 还想加放大 / ControlNet 之类，再导出一份自己的工作流填这里。
    comfyui_workflow: str = ""
    # ---- ControlNet（Anima LLLite，2026-10-25 加）----
    #
    # 只对 **Anima 系**生效：补丁是 `anima-lllite-*`，走 ComfyUI 原生的
    # `AnimaLLLiteApply` 节点（0.42 起内置，无需装插件）。
    # Z-Image / SDXL 用不了，配了也会被架构分支忽略。
    #
    # 取值：
    #   ""          不用（默认）
    #   "pose"      骨架姿态   anima-lllite-pose-1
    #   "lineart"   线稿       anima-lllite-lineart-1
    #   "depth"     深度       anima-lllite-depth-1
    #   "canny"     边缘       Canny 自动提取 + lineart 补丁
    #
    # ⚠️ **2026-10-25 实测结论：canny 模式无效，别用。**
    # 接线是对的（Canny → ImageInvert → AnimaLLLiteApply），也跑得通（17~24 秒），
    # 但出图**完全不受控制图约束** —— 画面里只多出几根细线，构图照旧跑偏。
    # 反相前更糟（整张图失控）。判断是 LLLite 的 lineart 补丁吃的是真正的
    # **线稿**（连续细线、白底黑线），而 Canny 边缘太粗太碎，形态对不上。
    #
    # 所以 pose / lineart / depth 三个模式**都要求用户提供处理好的控制图**：
    # 骨架图、线稿、深度图。直接传一张普通照片进来，效果大概率不对。
    # 这三个模式目前**同样未实测通过**（手头没有合格的骨架图样本）。
    #
    # **控制图从 `reference_image` 来**（用户发的那张图）。所以配了这个之后，
    # 参考图只当控制图用、**不再走图生图** —— 两者语义冲突：图生图要保留原图
    # 构图，ControlNet 要按骨架重画，同时开会互相打架。
    #
    # 自动从原图提骨架需要额外的 1.8 GB SDPose 模型（`SDPoseKeypointExtractor`
    # 要求 model 有 heatmap_head，Anima 主模型没有），8G 卡同时驻留会爆。
    #
    # 补丁位置：`ComfyUI/models/model_patches/`（不是 controlnet/ —— LLLite
    # 是 model patch，放错目录 ComfyUI 认不到）。
    comfyui_controlnet_mode: str = ""
    # 控制强度。1.0 = 官方默认。>1 更贴控制图但画面容易僵；<1 给模型更多自由。
    comfyui_controlnet_strength: float = 1.0
    # 正向标签前缀。二次元底模吃 Danbooru 标签体系，加质量标签是有效的
    # （这和 Pollinations 那边「别堆关键词」相反 —— 那边是 sana，会打架）。
    #
    # `perfect hands` / `detailed hands` 是 Danbooru 体系里真实存在的标签，
    # 针对的是 SDXL 系模型最弱的手部。**注意它只是辅助**：真正决定手好不好的是
    # prompt 选的手部姿势（见 core 里那段手部规则），加标签救不了含糊的姿势。
    comfyui_positive: str = (
        "masterpiece, best quality, very aesthetic, absurdres, perfect hands, detailed hands"
    )
    # 负面词。手部标签列得细，是因为每一种都对应实际会出现的崩坏形态：
    # 多指、并指、手指融合、畸形手。但实测（同 seed 对照）**负面词本身救不回
    # 崩坏的手** —— 它只在姿势本来就不含糊时起作用。别指望靠加词解决。
    comfyui_negative: str = (
        "lowres, bad anatomy, bad hands, poorly drawn hands, malformed hands, "
        "mutated hands, extra fingers, fused fingers, too many fingers, "
        "missing fingers, extra digit, fewer digits, distorted hands, "
        "text, error, cropped, worst quality, low quality, normal quality, "
        "jpeg artifacts, signature, watermark, username, blurry"
    )
    # 本地出图比云端慢（模型首次加载 + 采样），超时要单独给。
    # 群聊里等太久不如降级，所以默认 120 秒封顶。
    comfyui_timeout: float = 120.0
    # 存活探测结果的缓存秒数。机器关机时探测是毫秒级失败（连接被拒），
    # 但也不必每次出图都探一次。
    comfyui_probe_ttl: float = 20.0

    # ---- 风格转移（IP-Adapter）----
    #
    # 和上面的「参考图」是**两件事**，别混：
    #   · reference_image（图生图）：保构图、换细节。参考图决定画面长什么样。
    #   · style_reference（IP-Adapter）：只借**画风与氛围**，构图/人物由 prompt 决定。
    # 用户说「照这张重画」是前者，「用这张的画风画个 XX」是后者。
    #
    # 留空则自动从 ComfyUI 里扫到的文件里挑一个（和 checkpoint 同样的策略：
    # 不猜文件名，扫不到就直接失败，免得白等一轮采样）。
    comfyui_ipadapter_file: str = ""
    comfyui_clip_vision: str = ""
    # 风格转移（IP-Adapter）强度。**这个值跟着底模走，换底模必须重标定。**
    #
    # 实测：同一套参数在 animagine-xl-3.1 上 w=0.65 温和可用；
    # 换成 WAI-illustrious-v17 之后同样的 0.65 **冲爆**成彩虹色高饱和、
    # 五官崩坏。Illustrious 系对 IP-Adapter 的响应强得多。
    # 在新底模上扫过 0.2 / 0.35 / 0.5：0.5 和 0.35 都还有明显彩色溢出，
    # 只有 **0.2 干净**。所以默认压到 0.2，上限 0.3。
    # 想要更强的风格迁移就换回 animagine（底模可切），别在这里硬拉。
    comfyui_style_weight: float = 0.2

    # ---- 三条通道共用 ----
    timeout: float = 60.0
    # 一轮对话最多画几张。默认 1 —— 群里刷图比不说话更烦人。
    max_uses_per_turn: int = 1
    # 水印只有 Pollinations 有；通道 B 不裁。
    trim_watermark: bool = True
    watermark_px: int = 44

    @property
    def resolved_provider(self) -> str:
        """实际会走哪条通道：`pollinations` 或 `openai`。

        单独抽出来是因为「配置里写的 provider」和「实际走的通道」不是一回事 ——
        `auto` 要结合 api_key 有没有填才能定下来。
        """
        name = (self.provider or "").strip().lower()
        if name == "pollinations":
            return "pollinations"
        if name in ("openai", "openai-compatible", "compatible", "generic"):
            return "openai"
        # auto / 没填 / 不认识 —— 有 key 就走通用通道
        return "openai" if (self.api_key or "").strip() else "pollinations"


@dataclass
class StickerGenConfig:
    """她自己画的表情包（Q 版贴纸，本机 ComfyUI）。见 bot/sticker_gen.py。

    和 `imagegen` 是**两套参数**，不是一套 —— 虽然底层是同一个 ComfyUI：
    表情包要的是「9 秒内出图、白底 Q 版、服装固定」，
    生图要的是「画质优先、可以换装换场景」。混在一起改任何一边都会伤到另一边。

    所以这里只挑三个真正影响出图的东西（底模 / 尺寸 / 超时），
    其余（采样器、步数、CFG）跟着底模预设走，见 imagegen.COMFYUI_MODEL_PRESETS。

    `checkpoint` 默认 animagine-xl-3.1：28 步、768² 实测约 50 秒一张
    （2026-10-19 用户选定）。**慢归慢，但它是唯一支持 SDXL LoRA 的** ——
    角色一致性靠 prompt 标签压不住配饰漂移，LoRA 是正解。
    用 anima-turbo 的话 `sync_timeout` 可以压到 9 秒内，但 LoRA 吃不上。
    """

    enable: bool = True
    # 2026-11-04 用户要求：不再生成新表情包，只用本地素材 + 之前生成过的池子。
    # False = 只从池子挑（pick_only），不现画、不后台补画、不预热。
    # 想恢复现画把它改回 true 即可，池子机制不受影响。
    generate: bool = True
    checkpoint: str = "animagine-xl-3.1.safetensors"
    # 生成尺寸。768 出图再缩到 512 存盘（超采样，线条更干净）。
    size: str = "768x768"
    # 单张超时（**后台路径**用的）。正常 50 秒，首张含模型加载 60~80 秒。
    # 超过这个值就当 ComfyUI 卡住了，放弃这张 —— 池子里有旧图可复用，不会影响功能。
    timeout: float = 110.0

    # ---- 同步等待上限：这条决定「群里等不等」 ----
    #
    # 池子里没有可用图时，`send_sticker` 会尝试**当场画一张**。
    # 这个值是它愿意同步等的秒数：
    #   · anima-turbo（9 秒/张）→ 默认 12 秒就够，大多数情况当场出
    #   · animagine-xl-3.1（50 秒/张）→ 12 秒一定超时 → 自动转后台
    # 超时不等于失败：后台会接着把这张画完、直接发到群里，
    # 而本轮回复先降级到本地素材 / QQ 内置小黄脸，**对方永远不干等**。
    #
    # 调大 = 更贴内容但更容易让人等；调小 = 几乎不等待但表情包会滞后。
    # 12 秒是「刚好够 animagine 冷启动第一次之后的 nudge，又不会像死掉」的档。
    sync_timeout: float = 12.0

    # 角色 LoRA 文件名（放在 ComfyUI/models/loras/ 下）。
    # 留空 = 跟 imagegen.comfyui_lora 共用 —— 角色 LoRA 本来就应该同时喂给
    # 普通生图和表情包，两边画的是同一个人。
    # ⚠️ 只有 SDXL 底模吃 LoRA。animagine-xl-3.1 可以，
    # anima-turbo-v1.1 架构不同，填了也会被忽略（并记一条日志）。
    lora: str = ""

    # LoRA 强度。1.0 = 完全按训练时的权重；0.6~0.8 适合「想保留 LoRA 的五官但
    # 让表情/姿势自由些」。表情包的表情幅度大，如果发现五官被 LoRA 钉死了、
    # 笑不开，就往下调。
    #
    # 本次训练参数：dim 32 / alpha 16（比例 0.5，偏温和），10 epoch。
    # 这个比例本身不激进，1.0 不会过拟合 —— 这是按元数据判断的，还没实测。
    lora_strength: float = 1.0

    # 每个情绪池子里留几张。超了删最旧的。
    # 6 张足够「不重复」，又不至于把磁盘塞满（40 情绪 × 6 × 250KB ≈ 60MB）。
    # 0 = 不限制。
    cache_per_mood: int = 6
    # 启动后预热：把还没有图的情绪各画一张。
    prewarm: bool = True
    # 预热延迟。等机器人自己的启动自检跑完再动显卡，免得抢资源。
    prewarm_delay: float = 90.0


@dataclass
class LogConfig:
    level: str = "INFO"
    dir: str = "logs"


@dataclass
class TTSConfig:
    """本机语音合成（VoiceStudio）。

    默认 **关闭**。理由是它比表情包重得多：
    * 需要额外装一个几百 MB 的桌面应用，还要下几个 GB 的模型；
    * 首次合成要加载模型，前几秒是慢的；
    * 群里发语音的接受度远低于发表情 —— 不是每个群都想要。
    所以它是「要用才开」，不是「默认就开」。

    `voice` 留空 = 用后端当前活动音色。**换音色只改这一行**，
    不用动代码 —— 和 `comfyui_checkpoint` 是同一个设计。
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:3900"
    release_comfyui: bool = True
    api_key: str = ""
    # 音色 id：后端 /v1/audio/voices 里那串。空 = 用后端默认。
    voice: str = ""
    # 引擎 id。空 = 用后端当前活动引擎（推荐留空，换引擎在应用里点）。
    model: str = ""
    format: str = "wav"
    # 语言代码（ISO 639-1，如 "zh" / "ja" / "en"）。
    # **用外语素材克隆的音色来说中文时必须显式填 `zh`** —— 不填的话模型会跟着
    # 参考音频的语言走：实测拿日语参考合成中文，输出直接变成日语乱码。
    # 留空 = 不传该字段，由后端自己判断。
    language: str = ""
    # 迭代解掩码步数。**建议 32** —— 默认 16 会偶发吞字/糊句
    # （实测同一条长句跑 3 次有 1 次后半句糊掉），32 是模型文档值，实测稳定。
    # 0 = 不传该字段，用引擎默认值。
    num_step: int = 0
    # 优先用哪种写法发语音：uri（file:// URI）/ path（裸路径）/ base64。
    # 实际发送时会**依次试**三种，base64 兜底 —— 见 bot/tts.py 的 send_modes()。
    send_mode: str = "uri"
    # 同一句话重复合成纯浪费，聊天里短句重复率极高。
    cache: bool = True
    timeout: float = 120.0
    # 发送前在音频末尾补多少毫秒静音。
    #
    # **别改成 0。** 本机引擎的输出结尾直接停在字上，没有余音；QQ 语音走
    # SILK 编码，按 20ms 一帧切，解码器收尾会吃掉最后一帧 —— 末尾没有静音
    # 可吃，吃掉的最后一个字听起来就是「话没说完」。300~400 是实测够用的量。
    # 只对 wav 生效；mp3/opus 会被跳过（补字节会把容器搞坏）。
    tail_pad_ms: int = 350
    # 每轮最多发几条语音。
    max_uses_per_turn: int = 1
    # 合成缓存目录，由 Config.load 填成 <配置目录>/data。
    cache_dir: str = ""


@dataclass
class Config:
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    imagegen: ImageGenConfig = field(default_factory=ImageGenConfig)
    sticker_gen: StickerGenConfig = field(default_factory=StickerGenConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    log: LogConfig = field(default_factory=LogConfig)
    # 配置文件所在目录，用于定位 data/ 与 logs/
    base_dir: Path = field(default_factory=Path.cwd, repr=False)

    # ---------------------------------------------------------------- #

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        raw: Dict[str, Any] = {}
        if p.exists():
            text = p.read_text(encoding="utf-8")
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"config.json 不是合法 JSON：{exc}\n"
                    f"位置：{p}\n"
                    f"提示：JSON 不允许尾随逗号，也不允许 // 注释。"
                ) from exc
            log.info("已加载配置：%s", p)
        else:
            log.warning("配置文件不存在，使用默认值：%s", p)

        raw = _expand(raw)
        cfg = cls(
            onebot=_build(OneBotConfig, raw.get("onebot")),
            deepseek=_build(DeepSeekConfig, raw.get("deepseek")),
            bot=_build(BotConfig, raw.get("bot")),
            search=_build(SearchConfig, raw.get("search")),
            imagegen=_build(ImageGenConfig, raw.get("imagegen")),
            sticker_gen=_build(StickerGenConfig, raw.get("sticker_gen")),
            tts=_build(TTSConfig, raw.get("tts")),
            log=_build(LogConfig, raw.get("log")),
            base_dir=p.resolve().parent,
        )
        # system_prompt 支持 @文件名，从配置目录读人设文本
        cfg.bot.system_prompt = _resolve_prompt(cfg.bot.system_prompt, cfg.base_dir)
        # 语音合成缓存落在 data/ 下 —— 和记忆、日志同一个根，
        # 换机器时拷一个目录就够，不用到处找。
        cfg.tts.cache_dir = str(cfg.base_dir / "data")
        # 白名单群号统一成 int。手写 JSON 时很容易写成 "123456"，
        # 字符串和 int 比较永远不相等，症状是「配了群但从来不说话」——
        # 而且不报任何错，最难查的那种。
        # 主人 QQ 号也统一成 int。和群号同样的坑：
        # 手写 JSON 写成字符串 "2979759439"，跟 int 比较永远不相等，
        # 症状是「配了主人但她在私聊里照样演」—— 不报错，最难查。
        owners: List[int] = []
        for u in cfg.bot.owner_ids or []:
            try:
                owners.append(int(str(u).strip()))
            except (TypeError, ValueError):
                log.warning("bot.owner_ids 里有非法 QQ 号：%r，已忽略", u)
        cfg.bot.owner_ids = owners
        # 环境变量兜底（优先级最高）
        cfg.deepseek.api_key = os.environ.get("DEEPSEEK_API_KEY") or cfg.deepseek.api_key
        cfg.search.api_key = os.environ.get("BOCHA_API_KEY") or cfg.search.api_key
        cfg.imagegen.api_key = os.environ.get("IMAGE_API_KEY") or cfg.imagegen.api_key
        cfg.onebot.access_token = os.environ.get("ONEBOT_ACCESS_TOKEN") or cfg.onebot.access_token
        return cfg

    # ---------------------------------------------------------------- #

    @property
    def data_dir(self) -> Path:
        d = self.base_dir / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_dir(self) -> Path:
        d = self.base_dir / self.log.dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def validate(self) -> List[str]:
        """返回警告列表。不抛异常 —— 缺 key 也能起来，只是相关功能会提示。"""
        warns: List[str] = []

        if not self.deepseek.api_key:
            warns.append("deepseek.api_key 为空 —— 机器人无法回复任何消息。请去 platform.deepseek.com 申请。")
        elif not self.deepseek.api_key.startswith("sk-"):
            warns.append("deepseek.api_key 不像标准格式（通常以 sk- 开头），请确认没复制错。")

        if not self.onebot.ws_url.startswith(("ws://", "wss://")):
            warns.append(f"onebot.ws_url 必须是 ws:// 或 wss:// 开头，当前：{self.onebot.ws_url}")

        if self.bot.enable_search and not self.search.api_key:
            warns.append("bot.enable_search 已开启但 search.api_key 为空 —— 联网搜索会失败，建议关掉或补 key。")

        if self.bot.group_trigger not in ("at", "prefix", "both", "all"):
            warns.append(
                f"bot.group_trigger 取值非法：{self.bot.group_trigger}"
                "（可选 at / prefix / both / all），已按 at 处理。"
            )
            self.bot.group_trigger = "at"

        if self.bot.history_max_turns < 0:
            warns.append("bot.history_max_turns 不能为负，已按 0 处理。")
            self.bot.history_max_turns = 0

        if self.bot.concurrency < 1:
            warns.append("bot.concurrency 至少为 1，已按 1 处理。")
            self.bot.concurrency = 1

        # 分条发送的参数。都不致命，纠正一下继续跑。
        if self.bot.split_max_parts < 1:
            warns.append("bot.split_max_parts 至少为 1，已按 1 处理（等于不拆）。")
            self.bot.split_max_parts = 1
        if self.bot.split_messages and not str(self.bot.split_separator).strip():
            warns.append("bot.split_separator 为空，分条发送已关闭（否则会把正文切烂）。")
            self.bot.split_messages = False
        if self.bot.split_gap_min < 0:
            warns.append("bot.split_gap_min 不能为负，已按 0 处理。")
            self.bot.split_gap_min = 0.0
        if self.bot.split_gap_max < self.bot.split_gap_min:
            warns.append("bot.split_gap_max 小于 split_gap_min，已按 min 处理。")
            self.bot.split_gap_max = self.bot.split_gap_min

        if self.deepseek.thinking and self.deepseek.reasoning_effort not in (
            "low",
            "medium",
            "high",
            "max",
        ):
            warns.append(
                f"deepseek.reasoning_effort 取值非法：{self.deepseek.reasoning_effort}，已按 high 处理。"
            )
            self.deepseek.reasoning_effort = "high"

        if self.imagegen.enable:
            gen = self.imagegen

            if str(gen.provider or "").strip().lower() not in (
                "auto",
                "pollinations",
                "openai",
                "openai-compatible",
                "compatible",
                "generic",
            ):
                warns.append(
                    f"imagegen.provider 取值非法：{gen.provider}"
                    "（可选 auto / pollinations / openai），已按 auto 处理。"
                )
                gen.provider = "auto"

            if gen.resolved_provider == "pollinations":
                if not gen.endpoint.startswith(("http://", "https://")):
                    warns.append(
                        f"imagegen.endpoint 必须是 http(s) 地址，当前：{gen.endpoint} —— 生图会失败。"
                    )
                if gen.model not in ("sana",):
                    # 2026-09-30 实测：免费档里 flux / turbo 更容易吃 402，
                    # 而且同样的请求换个时间又能过 —— 所以是「容易失败」而不是「一定不行」。
                    warns.append(
                        f"imagegen.model = {gen.model} —— Pollinations 免费档里 "
                        "sana 最稳，flux / turbo 经常返回 402。生图可能时好时坏，建议改回 sana。"
                    )
            else:
                # 走通用通道时，这三样缺一个都发不出请求。
                if not (gen.api_key or "").strip():
                    warns.append(
                        "imagegen.provider = openai 但 imagegen.api_key 为空 —— 生图会失败。"
                        "（想用免费的就把 provider 改回 auto 或 pollinations）"
                    )
                if not gen.api_base.startswith(("http://", "https://")):
                    warns.append(
                        f"imagegen.api_base 必须是 http(s) 地址（要带版本号，如 "
                        f"https://api.siliconflow.cn/v1），当前：{gen.api_base or '空'} —— 生图会失败。"
                    )
                if not (gen.api_model or "").strip():
                    warns.append(
                        "imagegen.api_model 为空 —— 通用生图通道必须指定模型名，"
                        "例如 cogview-3-flash（智谱）/ Kwai-Kolors/Kolors（硅基流动）/ gpt-image-1（OpenAI）。"
                    )

            if not isinstance(gen.api_sizes, dict):
                warns.append("imagegen.api_sizes 必须是对象（如 {\"portrait\": \"768x1344\"}），已忽略。")
                gen.api_sizes = {}
            else:
                bad = [k for k, v in gen.api_sizes.items() if not re.fullmatch(r"\d+x\d+", str(v).strip())]
                if bad:
                    warns.append(
                        "imagegen.api_sizes 里这几项不是「宽x高」格式，已忽略："
                        + "、".join(sorted(bad))
                    )
                    gen.api_sizes = {
                        k: str(v).strip()
                        for k, v in gen.api_sizes.items()
                        if k not in bad
                    }
                else:
                    gen.api_sizes = {k: str(v).strip() for k, v in gen.api_sizes.items()}

            # 通道 C 是「能用就用」，所以这里只校验**配置写错了**的情况，
            # 「服务没起来」不在此列 —— 那是运行时探测的事，不该在启动时吓人。
            if gen.comfyui_enable:
                if not gen.comfyui_url.startswith(("http://", "https://")):
                    warns.append(
                        f"imagegen.comfyui_url 必须是 http(s) 地址，当前："
                        f"{gen.comfyui_url or '空'} —— 本地生图会跳过并走降级通道。"
                    )
                    gen.comfyui_enable = False
                if gen.comfyui_timeout <= 0:
                    warns.append("imagegen.comfyui_timeout 必须为正，已按 120 秒处理。")
                    gen.comfyui_timeout = 120.0
                if gen.comfyui_steps < 1:
                    warns.append("imagegen.comfyui_steps 至少为 1，已按 28 处理。")
                    gen.comfyui_steps = 28
                if gen.comfyui_cfg <= 0:
                    warns.append("imagegen.comfyui_cfg 必须为正，已按 6.0 处理。")
                    gen.comfyui_cfg = 6.0
                # LoRA 强度。**只在真的挂了 LoRA 时才管** —— 没挂的时候
                # 这个值完全不起作用，为它报一条警告只会让人困惑。
                gen.comfyui_lora = (gen.comfyui_lora or "").strip()
                if gen.comfyui_lora:
                    if not -2.0 <= gen.comfyui_lora_strength <= 2.0:
                        warns.append(
                            f"imagegen.comfyui_lora_strength 超出 [-2, 2]，"
                            f"当前 {gen.comfyui_lora_strength}，已按边界处理。"
                        )
                        gen.comfyui_lora_strength = max(
                            -2.0, min(2.0, gen.comfyui_lora_strength)
                        )
                    if not gen.comfyui_lora.lower().endswith(
                        (".safetensors", ".pt", ".ckpt")
                    ):
                        warns.append(
                            f"imagegen.comfyui_lora 看起来不是模型文件名："
                            f"{gen.comfyui_lora} —— LoRA 必须写成 models/loras 下的"
                            f"完整文件名（含扩展名），否则出图会直接报错。"
                        )

            if gen.max_uses_per_turn < 1:
                warns.append("imagegen.max_uses_per_turn 至少为 1，已按 1 处理。")
                gen.max_uses_per_turn = 1
            if gen.timeout <= 0:
                warns.append("imagegen.timeout 必须为正，已按 60 秒处理。")
                gen.timeout = 60.0
            if gen.watermark_px < 0:
                warns.append("imagegen.watermark_px 不能为负，已按 0 处理。")
                gen.watermark_px = 0

        return warns
