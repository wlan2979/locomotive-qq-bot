"""自主发抖音：决定什么时候发、发什么。

桃桃是独立存在的人，发视频不需要人批准（2026-10-24 用户明确说的）。
但「不需要批准」不等于「可以随便发」—— 公开账号，无人审核，
所以这里的护栏全是**默认收紧**的：

| 护栏 | 值 | 为什么 |
|---|---|---|
| 一天几条 | 1 | 发多了像营销号，不像人 |
| 什么时段 | 9:00～23:00 | 凌晨没人看，还容易被判定成机器行为 |
| 失败怎么办 | 当天不再试 | 重试会连着发同样的内容，比不发更糟 |
| 依赖没就绪 | 跳过这一轮 | ComfyUI 没开 / 手机没接是环境问题，不算「失败」 |
| 视频长度 | 2 段 ≈ 20 秒 | seedance 单段上限 10 秒；用户 2026-11-06 要求「尽量大于 15 秒」 |
| 成片下限 | 15 秒 | 低于 `seedance.min_seconds` 直接不发 |
| 内容重复 | 当天不发 | 标题或**场景指纹**与最近 5 条重复（2026-11-06 用户删过一条「画面配音一样、只有文案变了」的视频） |

## 一轮 tick 干什么

选模板 → 让 LLM 写成 `Scene`（两拍动作 + 70～90 字口播）→ **查重** →
`TaotaoVideoMaker.make()`（多段生成、拼接、配音对齐）→ `DouyinPublisher.publish()`


失败分两类，处理方式不同：

- **环境问题**（ComfyUI 没开、手机没接、不在时段）→ 跳过，下一轮再看。
  这类不是她的错，记成「失败」会导致一整天都不发了。
- **真的失败**（出图崩了、发布没成功）→ 记一次，当天不再试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .douyin_publisher import DouyinPublisher, PublishResult
from .taotao_video import Scene, TaotaoVideoMaker, VideoError

log = logging.getLogger(__name__)


# 让模型把「今天拍什么」写成一个 Scene。
#
# 每条约束都是踩出来的，别删：
# - 不写发色瞳色：LoRA 已经固定长相，再写会打架（seed 一换就变金发）
# - 服装词重复：turbo 底模 cfg=1.0，负面提示词不生效，只能正面堆权重
# - motion 开头那句：不写会往写实跑
# - 口播 70~90 字：2 段约 20 秒，中文语速约 4 字/秒，太短会留一大段静音
# - 两拍动作：视频是多段拼的（seedance.clips=2），第二段接着第一段的末帧，
#   所以要求模型把动作拆成「前半段怎么动、后半段怎么动」，且要接得上
VOICE_MIN_CHARS = 70
VOICE_MAX_CHARS = 90
# 目标区间是 70~90，但**最终接受区间放宽到 65~100**：
# 差几个字就整条退回兜底太浪费（2026-11-06 d1 的视频线 dry_run 就是 69 字被退）。
# 65 字 ≈ 16 秒（补静音）、100 字 ≈ 24 秒（定格段补尾），成片质量都能接受。
VOICE_ACCEPT_MIN = 65
VOICE_ACCEPT_MAX = 100

# 发布器报这些词 = **环境问题**（手机/连接），不是「真的发布失败」。
# 2026-11-07 用户启动那天模拟器夜里掉过一次：如果第一轮发布失败被记 failed，
# 「今天失败过不再重试」闸门会把当天配额锁死一整天，实际只是手机没连上。
# 这类错误只跳过这一轮、不计数，重连后下一轮还能正常发。
_ENV_ERROR_HINTS = (
    "手机掉线",
    "手机连不上",
    "手机没授权",
    "没有连上模拟器",
    "读不到屏幕：adb 报",
    "device offline",
    "device still authorizing",
)

SCENE_SYSTEM = """你是桃桃，一个住在日本、喜欢窝在家里的二次元少女。
你要发一条抖音短视频，内容是你自己的日常。
这条视频由**两段镜头**拼成（每段约 10 秒，共约 20 秒），第二段接着第一段往下演。

输出**一个 JSON 对象**，不要解释，不要 markdown 代码块，不要任何多余的字。

{
  "frame_prompt": "英文逗号标签串",
  "motion_prompt": "第一段的中文句子",
  "motion_prompt_2": "第二段的中文句子",
  "voice_text": "中文口播词",
  "title": "抖音标题",
  "desc": "抖音描述",
  "topics": ["话题1", "话题2"]
}

硬要求，逐条遵守：

1. `frame_prompt` 必须以 `taotao, 1girl, solo,` 开头。
   只写服装、姿势、场景、光线、构图、景别（如 `cowboy shot` / `upper body`）。
   **绝对不要写发色和瞳色**（不要 black hair / black eyes）——
   角色 LoRA 已经固定了长相，再写会打架。
   服装词要重复堆：例如 `beige knit sweater, oversized sweater, long sleeves`。
2. `motion_prompt` 是第一段（0～10 秒）的动作和镜头，50~80 字；
   `motion_prompt_2` 是第二段（10～20 秒）的动作和镜头，50~80 字。
   **两段要接得上**：第二段从第一段的最后一帧开始，动作顺势往下走，
   不换场景、不跳镜头。两段开头都原样写「保持日式二维动画风格，赛璐璐上色。」
3. `voice_text` 70~90 字，是这一整条视频的口播词：一句话说前半段，
   一句话说后半段，连起来像一段自然的碎碎念。口语，别书面语。
4. `title` 不超过 16 字。`desc` 不超过 40 字，**不要带 # 号**。
5. `topics` 两个中文词，不带 # 号，要贴内容。
6. `title`、`desc`、`voice_text` 里**不要**出现「AI」「虚拟主播」「模型」「生成」
   这类词。AI 生成的标注由发布程序统一加，你写了会和它重复，
   而且读起来像免责声明，不像日常。
"""

# LLM 挂了、或者输出解析不出来时的兜底。
#
# 为什么要有兜底：发布链路已经全自动了，文案这一环不该成为单点故障。
#
# **为什么是一个池子而不是一条场景**（2026-11-06 用户踩的坑）：
# 原来兜底只有一条场景，dry_run 那几天每次都落回它，结果发出去的视频
# 「画面、动作、配音和旧作品一模一样，只有标题描述变了」—— 用户只能删掉。
# 所以兜底至少 3~5 个**三项都不同**的场景（画面/两拍动作/口播一起换，
# 不能只换文案），然后按最近发布记录轮换（`_choose_fallback_scene()`）。
FALLBACK_SCENES: tuple = (
    Scene(
        frame_prompt=(
            "taotao, 1girl, solo, sitting on bed, cowboy shot, centered composition, "
            "beige knit sweater, oversized sweater, long sleeves, "
            "holding a pink phone, cozy bedroom, warm afternoon sunlight, "
            "looking at camera, best quality"
        ),
        motion_prompt=(
            "保持日式二维动画风格，赛璐璐上色。"
            "女孩坐在床边低头看手机，拇指轻轻滑动屏幕，然后抬起头看向镜头，"
            "嘴角微微上扬。镜头缓慢推近，窗外的光落在她脸上。"
        ),
        motion_prompt_2=(
            "保持日式二维动画风格，赛璐璐上色。"
            "镜头继续推近到她的侧脸，她把手机放下，伸手去够窗台上的杯子，"
            "喝了一口，然后靠在床头轻轻笑了一下，窗外的光慢慢变暖。"
        ),
        voice_text=(
            "今天翻到去年夏天的照片，突然有点想去海边。那时候的我穿着薄薄的衬衫，"
            "风一吹头发就乱。等天气再暖一点，我们一起去吧，我想看看傍晚的海，"
            "再捡几颗小石头回来。"
        ),
        title="想去海边",
        desc="翻到旧照片，忽然有点想去海边。",
        topics=("少女日常", "动漫少女"),
    ),
    Scene(
        frame_prompt=(
            "taotao, 1girl, solo, standing in kitchen, upper body, centered composition, "
            "cream cardigan, fluffy cardigan, long sleeves, "
            "holding a mug of hot cocoa, small kitchen, warm evening light, "
            "looking at camera, best quality"
        ),
        motion_prompt=(
            "保持日式二维动画风格，赛璐璐上色。"
            "女孩站在厨房里把热可可倒进杯子，低头吹了吹热气，然后抬头看向镜头笑。"
            "镜头从侧面慢慢推近，暖黄的灯光在杯口晃了一下。"
        ),
        motion_prompt_2=(
            "保持日式二维动画风格，赛璐璐上色。"
            "她双手捧着杯子走到窗边，窗外的雨声透过玻璃，她缩了缩肩膀，"
            "又低头喝了一口，眼睛弯起来，杯口腾起淡淡的白色热气。"
        ),
        voice_text=(
            "今天外面下了好大的雨，我给自己煮了一杯热可可，糖放多了一点点，"
            "喝下去整个胸口都暖起来。下雨天最适合窝在家里，什么都不想做，"
            "就这样安安静静地待着。"
        ),
        title="热可可的一天",
        desc="下雨天给自己煮了杯热可可。",
        topics=("宅家日常", "少女日常"),
    ),
    Scene(
        frame_prompt=(
            "taotao, 1girl, solo, sitting at desk, cowboy shot, centered composition, "
            "white blouse, pleated skirt, long sleeves, holding a pen and notebook, "
            "cozy study room, soft lamp light, looking down, best quality"
        ),
        motion_prompt=(
            "保持日式二维动画风格，赛璐璐上色。"
            "女孩坐在书桌前翻开笔记本，笔尖在纸上慢慢划过，她停笔想了想，"
            "托着脸颊看向窗外。镜头从斜上方轻轻推近，台灯的光落在纸页上。"
        ),
        motion_prompt_2=(
            "保持日式二维动画风格，赛璐璐上色。"
            "她低头继续写，写完合上本子，把纸页抚平，然后靠回椅背伸了个懒腰，"
            "台灯的光晕慢慢散开，她揉了揉眼睛。"
        ),
        voice_text=(
            "今天想把最近的事情都写下来，可是坐到桌前又不知道从哪句开始。"
            "笔在纸上停了很久，最后只写了一句：今天也很好。剩下的那些，"
            "就留给明天慢慢说吧，反正也不急。"
        ),
        title="写给今天",
        desc="坐到桌前写了几行字。",
        topics=("手账日常", "少女日常"),
    ),
    Scene(
        frame_prompt=(
            "taotao, 1girl, solo, standing on balcony, full body, centered composition, "
            "light blue dress, white cardigan, holding a watering can, potted plants, "
            "spring afternoon sunlight, looking at camera, best quality"
        ),
        motion_prompt=(
            "保持日式二维动画风格，赛璐璐上色。"
            "女孩站在阳台上给花浇水，水珠顺着叶子滑下来，她歪头看了看花苞，"
            "抬起手挡了挡太阳。镜头从远处慢慢推近，风把晾着的衣服吹得轻轻晃。"
        ),
        motion_prompt_2=(
            "保持日式二维动画风格，赛璐璐上色。"
            "她放下水壶，指尖轻轻碰了碰新长出来的嫩叶，然后转身看向镜头，"
            "风把裙摆和发梢都吹起来，阳光落在她的肩膀上。"
        ),
        voice_text=(
            "阳台上的花终于开了一点点，我早上浇完水蹲下来看了很久。"
            "风把窗帘吹得鼓起来，太阳也不刺眼，感觉今天会是很温柔的一天，"
            "适合慢慢过完，什么都不用着急。"
        ),
        title="阳台上的花",
        desc="早上给花浇了水。",
        topics=("慢生活", "少女日常"),
    ),
    Scene(
        frame_prompt=(
            "taotao, 1girl, solo, sitting on sofa, upper body, centered composition, "
            "gray hoodie, oversized hoodie, long sleeves, wrapped in a blanket, "
            "holding a novel, living room, late night lamp light, looking at camera, best quality"
        ),
        motion_prompt=(
            "保持日式二维动画风格，赛璐璐上色。"
            "女孩裹着毯子窝在沙发里翻书，看到一半停下来揉了揉眼睛，"
            "然后抱住膝盖把下巴搁上去。镜头轻轻推到她的肩膀，灯光有点暗。"
        ),
        motion_prompt_2=(
            "保持日式二维动画风格，赛璐璐上色。"
            "她打了个小小的哈欠，把书签夹进书页合上书，抱着毯子往沙发深处缩了缩，"
            "灯光慢慢暗下来，她闭上眼。"
        ),
        voice_text=(
            "今晚本来只打算看一章就睡，结果一翻开就停不下来。灯有点暗，字也有点糊，"
            "可我舍不得合上书。再一章，就最后一章，看完真的就睡了，真的很晚了，晚安。"
        ),
        title="再看一章",
        desc="窝在沙发上看书到很晚。",
        topics=("夜读", "少女日常"),
    ),
)

# 兼容旧引用（日志/老测试里用这个名字）—— 取池子第一个。
FALLBACK_SCENE = FALLBACK_SCENES[0]


def _norm_scene_text(text: str) -> str:
    """场景指纹用：去掉空白和常见标点，避免同一句话排版不同被当成两条。"""
    return re.sub(
        r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]…·—\-_]+", "", text or ""
    ).lower()


def scene_fp(scene: Scene) -> str:
    """场景指纹 = 画面 + 两拍动作 + 口播，归一化后哈希。

    **不含标题和描述。** 用户 2026-11-06 遇到的就是「画面、动作、配音
    和旧作品一模一样，只有文案变了」—— 只查标题根本挡不住这种重复。
    """
    parts = [
        _norm_scene_text(scene.frame_prompt),
        _norm_scene_text(scene.motion_prompt),
        _norm_scene_text(scene.motion_prompt_2),
        _norm_scene_text(scene.voice_text),
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]


MAKE_DOUYIN_VIDEO_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "make_douyin_video",
        "description": (
            "做一条短视频发到自己的抖音账号上。"
            "**这个工具不会当场做完** —— 做一条要三分钟左右，"
            "调用后它会立刻返回「开始做了」，你可以先把话说完，"
            "做完之后会有人回来告诉你结果。所以不要等，也不要反复调用。"
            "一天只能发一条，已经发过了就别再试。"
            "什么时候用：你自己想发点什么的时候，或者别人让你发的时候。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "idea": {
                    "type": "string",
                    "description": (
                        "想拍什么，一句话就够，比如「刚买的杯子」「今天下雨了」"
                        "「想吐槽一下我的猫」。不填就自己挑一个。"
                    ),
                },
            },
            "required": [],
        },
    },
}


@dataclass
class State:
    """当天发了没、失败没；外加最近发布过的场景（跨天保留，用来查重）。"""

    day: str = ""
    published: int = 0
    failed: int = 0
    last_error: str = ""
    last_at: float = 0.0
    last_title: str = ""
    # 今天因为「内容重复」被跳过：当天不再发。**不算失败、也不算发布**，
    # 单独记一笔是为了别让「重复」把「失败」的语义搅混。
    duplicate: int = 0
    # 最近发布过的标题 / 场景指纹（新的在前，各保留 5 条）。
    # **跨天不清空** —— 只在自然日内查重的话，第二天照样会发一模一样的。
    recent_titles: List[str] = field(default_factory=list)
    recent_fps: List[str] = field(default_factory=list)

    @staticmethod
    def _str_list(raw: Any, limit: int = 5) -> List[str]:
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()][:limit]

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(
                day=str(raw.get("day") or ""),
                published=int(raw.get("published") or 0),
                failed=int(raw.get("failed") or 0),
                last_error=str(raw.get("last_error") or ""),
                last_at=float(raw.get("last_at") or 0.0),
                last_title=str(raw.get("last_title") or ""),
                duplicate=int(raw.get("duplicate") or 0),
                recent_titles=cls._str_list(raw.get("recent_titles")),
                recent_fps=cls._str_list(raw.get("recent_fps")),
            )
        except Exception:  # noqa: BLE001
            # 状态文件坏了就当今天还没发过 —— 最坏结果是多发一条，
            # 比「永久不发」轻。
            return cls()

    def save(self, path: Path) -> None:
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.__dict__, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音状态保存失败：%s", exc)


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出那个 JSON 对象。

    模型经常包一层 ```json 或者前后加句废话，所以先剥围栏，
    再退到「第一个 { 到最后一个 }」。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


class DouyinScheduler:
    """决定什么时候发、发什么。一轮 `tick()` 最多发一条。"""

    def __init__(
        self,
        cfg: Any,
        hands: Any,
        llm: Any,
        store: Any,
        state_path: Optional[Path] = None,
    ) -> None:
        self.cfg = cfg
        self.hands = hands
        self.llm = llm
        self.store = store
        self.state_path = Path(
            state_path or (Path(cfg.base_dir) / "data" / "douyin_state.json")
        )
        self.state = State.load(self.state_path)
        self._maker: Optional[TaotaoVideoMaker] = None
        self._publisher: Optional[DouyinPublisher] = None
        # 一条正在做的时候不许开第二条。定时 tick 和她手动要做可能撞车，
        # 不加这个锁就会两条一起做、一起发，当天配额白设。
        self._busy = False

    # ------------------------------------------------------------------ #
    # 懒构建

    def _ensure_tools(self) -> None:
        if self._maker is None:
            self._maker = TaotaoVideoMaker(
                self.cfg,
                Path(self.cfg.douyin.out_dir),
                Path(self.cfg.douyin.ffmpeg),
            )
        if self._publisher is None:
            self._publisher = DouyinPublisher(
                self.hands,
                ai_label=getattr(self.cfg.douyin, "ai_label", "strict"),
                ai_label_text=getattr(
                    self.cfg.douyin, "ai_label_text", "本视频由 AI 生成"
                ),
            )

    # ------------------------------------------------------------------ #
    # 闸门

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.state.day:
            # 跨天只重置当天计数（发了几条/失败/重复），**最近发布记录要留着** ——
            # 否则查重只在一个自然日内有效，第二天照样会发一模一样的视频。
            self.state = State(
                day=today,
                recent_titles=list(self.state.recent_titles or []),
                recent_fps=list(self.state.recent_fps or []),
            )
            self.state.save(self.state_path)

    def _blocked_reason(self, *, check_hours: bool = True) -> str:
        """现在能不能发。返回空串表示可以，否则是「为什么不能」。"""
        cfg = self.cfg.douyin
        if not cfg.enable:
            return "抖音自主发布是关的"
        if check_hours and not cfg.in_hours():
            return f"不在时段（{cfg.hours[0]}-{cfg.hours[1]} 点）"
        if self.state.published >= int(cfg.daily_limit):
            return f"今天已经发过 {self.state.published} 条（上限 {cfg.daily_limit}）"
        if self.state.failed > 0:
            return f"今天失败过（{self.state.last_error[:40]}），不再重试"
        if self.state.duplicate > 0:
            return f"今天出过重复内容（{self.state.last_error[:40]}），不再发"
        if self.hands is None:
            return "手机没接上"
        return ""

    # ------------------------------------------------------------------ #
    # 查重 / 兜底轮换

    def _choose_fallback_scene(self) -> Scene:
        """从兜底池里挑一个最近没用过的场景。

        2026-11-06 用户踩的坑：dry_run 每次都用唯一那个兜底场景，结果连着发的
        视频「画面、动作、配音一模一样，只有文案变了」，用户只能删掉。
        所以兜底必须按最近发布记录排除轮换。全都用过时取「最久没用的」那个，
        它会被 `_run_one()` 的查重拦下（当天不发）—— 宁可少一条，不发重复作品。
        """
        recent_fps = list(self.state.recent_fps or [])
        recent_titles = set(self.state.recent_titles or [])
        for scene in FALLBACK_SCENES:
            if scene_fp(scene) not in recent_fps and scene.title not in recent_titles:
                return scene

        log.warning("抖音：兜底池里的场景最近都用过了，取最久没用的那个（会被查重拦）")
        best, best_pos = FALLBACK_SCENES[0], -1
        for scene in FALLBACK_SCENES:
            fp = scene_fp(scene)
            # recent_fps 新的在前：索引越大 = 越久没用。
            pos = recent_fps.index(fp) if fp in recent_fps else len(recent_fps)
            if pos > best_pos:
                best, best_pos = scene, pos
        return best

    def _duplicate_reason(self, scene: Scene) -> str:
        """这条和最近发布的作品撞不撞。撞了返回原因，没撞返回空串。

        两个都要查：**标题相同**或**场景指纹相同**都算撞。
        用户 2026-11-06 那条「标题文案变了、画面动作配音一模一样」的视频，
        只查标题是挡不住的。
        """
        title = (scene.title or "").strip()
        if title and title in set(self.state.recent_titles or []):
            return f"标题「{title}」最近发过"
        fp = scene_fp(scene)
        if fp in list(self.state.recent_fps or []):
            return "画面/动作/口播和最近一条一模一样"
        return ""

    def _remember_scene(self, scene: Scene) -> None:
        """发布成功（或结果不确定）后记一笔，供以后查重。标题/指纹各留最近 5 条。"""
        fp = scene_fp(scene)
        self.state.recent_fps = (
            [fp] + [x for x in (self.state.recent_fps or []) if x != fp]
        )[:5]
        title = (scene.title or "").strip()
        if title:
            self.state.recent_titles = (
                [title] + [x for x in (self.state.recent_titles or []) if x != title]
            )[:5]

    # ------------------------------------------------------------------ #
    # 文案

    async def _write_scene(self, template: str) -> Scene:
        """让 LLM 把模板写成一条具体的内容。

        不合格会**让模型按目标重写一次**，再不合格才用兜底 ——
        2026-11-06 d1 的视频线 dry_run 里，LLM 口播写了 69 字（差一个字）
        就被整条退回兜底，白瞎一条视频。一次重写比退回兜底便宜。
        """
        if self.llm is None:
            log.info("没有 LLM，用兜底场景")
            return self._choose_fallback_scene()

        user = f"今天这条用这个结构：{template}\n\n按要求的 JSON 格式输出。"
        reply_text = ""
        for attempt in (1, 2):
            try:
                reply = await self.llm.chat(
                    [
                        {"role": "system", "content": SCENE_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    tools=None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("写文案失败（%s），用兜底场景", exc)
                return self._choose_fallback_scene()

            reply_text = reply or ""
            data = _parse_json_object(reply_text)
            # 第一遍按**目标区间**（70~90）卡；重写那一遍才放宽到可接受区间，
            # 这样既能让模型尽量写到目标字数，又不至于差几个字就退回兜底。
            scene = self._scene_from_data(data, strict_voice=(attempt == 1))
            if scene is not None:
                return scene

            if attempt == 1:
                log.info("文案这一版不合格，让模型按字数/字段要求重写一次")
                user = self._rewrite_hint(data)

        log.warning(
            "文案重写一次还是不合格，用兜底场景：%r", reply_text[:100]
        )
        return self._choose_fallback_scene()

    @staticmethod
    def _rewrite_hint(data: Any) -> str:
        """第二遍的 user 消息：指出上一版哪里不合格，让模型按目标重写。"""
        voice = str((data or {}).get("voice_text") or "").strip()
        if voice:
            head = (
                f"上一版 voice_text 是 {len(voice)} 字，"
                f"必须落在 {VOICE_MIN_CHARS}~{VOICE_MAX_CHARS} 字之间。\n"
            )
        else:
            head = "上一版缺少 voice_text，或者整段输出解析不出来。\n"
        return (
            head
            + "所有字段（frame_prompt / motion_prompt / motion_prompt_2 / "
            "voice_text / title / desc / topics）都必须有，两拍动作都要写。\n"
            + "内容的意思尽量保留，只把字数和字段调整到要求范围内，"
            "重新输出**完整**的 JSON 对象。"
        )

    def _scene_from_data(self, data: Any, *, strict_voice: bool = True) -> Optional[Scene]:
        """校验模型输出并拼成 Scene。不合格返回 None（不拼半截参数）。

        `strict_voice=True` 用目标区间 70~90 卡（第一遍）；
        `False` 用可接受区间 65~100 卡（重写那一遍）。
        """
        if not data:
            log.warning("文案解析不出来")
            return None

        frame = str(data.get("frame_prompt") or "").strip()
        motion = str(data.get("motion_prompt") or "").strip()
        motion2 = str(data.get("motion_prompt_2") or "").strip()
        voice = str(data.get("voice_text") or "").strip()
        title = str(data.get("title") or "").strip()
        desc = str(data.get("desc") or "").strip()
        topics = data.get("topics") or []

        # 逐项校验。少一项就整体退回兜底 —— 半截参数做出来的视频不能发。
        # 多段视频没有第二拍动作，第二段就接不上第一段。
        if not frame or not motion or not motion2 or not voice or not (title or desc):
            log.warning("文案字段不全（frame/motion/motion2/voice/title|desc）")
            return None

        # 口播词长度。目标 70~90 字（2 段约 20 秒，中文语速约 4 字/秒），
        # 重写版放宽到 65~100（见常量处的说明）。
        low = VOICE_MIN_CHARS if strict_voice else VOICE_ACCEPT_MIN
        high = VOICE_MAX_CHARS if strict_voice else VOICE_ACCEPT_MAX
        if not (low <= len(voice) <= high):
            log.warning(
                "口播词 %d 字，不在 %d~%d 内（%s）",
                len(voice), low, high,
                "第一遍按目标区间" if strict_voice else "重写版按可接受区间",
            )
            return None
        if not strict_voice and not (
            VOICE_MIN_CHARS <= len(voice) <= VOICE_MAX_CHARS
        ):
            log.info(
                "口播词 %d 字，不在目标 %d~%d 内，但在可接受区间，放行",
                len(voice), VOICE_MIN_CHARS, VOICE_MAX_CHARS,
            )

        # 模型有时会自作主张写发色。那会和 LoRA 打架，直接剃掉。
        frame = re.sub(
            r"\b(black|brown|blonde|silver|white|pink|blue|red)\s+hair\b", "", frame,
            flags=re.I,
        )
        frame = re.sub(
            r"\b(black|brown|blue|red|green|golden|amber)\s+eyes\b", "", frame,
            flags=re.I,
        )
        frame = re.sub(r"\s*,\s*(?=,)", ",", frame).strip(" ,")

        # 两拍的画风约束都是硬要求，模型漏了就补上。
        # 第二段是**独立的一次 seedance 调用**，不补的话第二段会往写实跑。
        if "赛璐璐" not in motion:
            log.info("motion_prompt 没写画风约束，补上")
            motion = "保持日式二维动画风格，赛璐璐上色。" + motion
        if "赛璐璐" not in motion2:
            log.info("motion_prompt_2 没写画风约束，补上")
            motion2 = "保持日式二维动画风格，赛璐璐上色。" + motion2

        return Scene(
            frame_prompt=frame,
            motion_prompt=motion,
            motion_prompt_2=motion2,
            voice_text=voice,
            title=title,
            desc=desc,
            topics=tuple(str(t).strip().lstrip("#") for t in topics if str(t).strip())[:3],
        )

    # ------------------------------------------------------------------ #
    # 主流程

    def _cleanup(self, result: Any) -> None:
        """发布成功后删中间产物，只留成片和首帧。

        **首帧永远不删。** 它是「这条视频是桃桃」的唯一可追溯凭证 ——
        抖音上那条视频已经发出去改不了了，至少本地要留得住证据。
        而且首帧才 1 MB，删了省不了多少。

        现在一条视频的中间产物不止两个文件了（多段生成）：
        每段的 `raw_*.mp4`、段间抽的末帧、补尾的定格段、配音 wav。
        全删掉，每条能省几十 MB。**第一段的首帧不在此列**（见上）。

        `keep_intermediate=True`（默认）时什么都不做 —— 调链路阶段
        得靠这些文件判断是哪一环坏的：画面崩了看 raw，配音错了听 voice。
        """
        if getattr(self.cfg.douyin, "keep_intermediate", True):
            return

        targets: list = [
            getattr(result, "raw", None),
            getattr(result, "voice", None),
            *list(getattr(result, "clips", []) or []),
            *list(getattr(result, "extras", []) or []),
        ]
        seen = set()
        for path in targets:
            if not path:
                continue
            try:
                p = Path(path)
                if p in seen:
                    continue
                seen.add(p)
                if p.exists():
                    p.unlink()
                    log.info("抖音：已删中间产物 %s", p.name)
            except Exception as exc:  # noqa: BLE001
                # 删不掉不是错误，磁盘满了而已，别影响发布结果
                log.debug("删 %s 失败：%s", path, exc)

    async def _comfyui_ready(self) -> bool:
        """ComfyUI 活着吗。没开是环境问题，不算失败 —— 跳过就行。"""
        from .imagegen import probe_comfyui

        import aiohttp

        async with aiohttp.ClientSession() as session:
            return await probe_comfyui(session, self.cfg.imagegen)

    def _pick_template(self) -> str:
        """从灵感库挑一个没用过的结构。挑不到就返回空串。"""
        if self.store is None:
            return ""
        try:
            picked = self.store.pick(exclude=[self.state.last_title])
            return picked.pattern if picked is not None else ""
        except Exception as exc:  # noqa: BLE001
            log.debug("取模板失败（%s），用兜底结构", exc)
            return ""

    async def _hands_ready(self) -> bool:
        """生成前先确认手机可用。**不可用 = 环境问题，不记失败。**

        为什么要提前查：做一条视频要 6 分钟（ComfyUI + 两段 seedance + 配音），
        手机这时候不在的话，白烧半天算力最后还是发不出去。
        而且模拟器掉线是环境问题，不是她的错 —— 记 failed 会把「今天失败过
        不再重试」闸门锁死，明明重连就能发。

        `ensure_ready` 不存在时（测试替身/老实现）按「可用」处理：
        生产里 `hands` 一定是 `PhoneHands`，不会走到这个分支。
        """
        if self.hands is None:
            return False
        probe = getattr(self.hands, "ensure_ready", None)
        if not callable(probe):
            return True
        try:
            await probe()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "抖音：手机不可用（%s），这一轮按环境问题跳过，不记失败", exc
            )
            return False

    @staticmethod
    def _is_env_error(text: str) -> bool:
        """这个错误是「环境问题」（手机/连接）还是真的发布失败。"""
        blob = text or ""
        return any(hint in blob for hint in _ENV_ERROR_HINTS)

    async def tick(self, dry_run: bool = False) -> str:
        """定时检查。返回一句人话，说明这轮做了什么（或为什么没做）。

        `dry_run=True` 会走完「写文案 → 做视频 → 走到发布页」，但**不点发作品**，
        也不消耗当天配额。用来验证整条链路而不真的发东西出去。
        """
        if self._busy:
            return "上一条还在做，这轮跳过"
        self._busy = True
        try:
            self._roll_day()
            reason = self._blocked_reason()
            if reason:
                log.debug("抖音：跳过（%s）", reason)
                return reason
            # 生成前先确认手机可用 —— 不可用是环境问题，跳过不计数。
            if not await self._hands_ready():
                return "手机不可用（环境问题），这一轮跳过"
            if not await self._comfyui_ready():
                return "ComfyUI 没开，这一轮跳过"
            template = self._pick_template()
            log.info("抖音：这轮用结构「%s」", template or "（无，直接让模型发挥）")
            return await self._run_one(template, dry_run)
        finally:
            self._busy = False

    async def run_once(self, idea: str = "", dry_run: bool = False) -> str:
        """她自己想做一条。给「她在聊天里说想发视频」这条路用。

        和 `tick()` 的区别：

        - `tick()` 是定时检查，**看时段**（凌晨不打扰），一天一条。
        - `run_once()` 是明确有人要做，**不看时段** —— 她想现在做就现在做。
          但配额照样算，一天还是一条：主动想做 ≠ 可以刷屏。

        `_busy` 锁是必须的：定时 tick 和她手动要做可能撞在同一分钟，
        不加锁就会两条一起做、一起发，当天配额也白设了。
        """
        if self._busy:
            return "上一条还在做，等这条做完再说"
        self._busy = True
        try:
            self._roll_day()
            reason = self._blocked_reason(check_hours=False)
            if reason:
                return reason
            # 生成前先确认手机可用 —— 不可用是环境问题，跳过不计数。
            if not await self._hands_ready():
                return "手机不可用（环境问题），等手机连上再做"
            if not await self._comfyui_ready():
                return "ComfyUI 没开，做不了视频"
            template = self._pick_template()
            log.info("抖音：她主动要做一条，想法「%s」，结构「%s」", idea or "（无）", template or "（无）")
            return await self._run_one(template, dry_run, idea=idea)
        finally:
            self._busy = False

    async def _run_one(self, template: str, dry_run: bool, idea: str = "") -> str:
        """写文案 → 做视频 → 发布。tick 和 run_once 共用的核心。

        `idea` 是她自己给的想法，优先级高于灵感库的模板 ——
        她说了「想拍刚买的杯子」，就不该还套「努力半天成果被毁」那套结构。
        """
        hint = (idea or "").strip() or template or "她自己在家的一个小片段"
        scene = await self._write_scene(hint)

        # ---- 查重：宁可少发一条，也不发重复作品 --------------------
        # 2026-11-06 用户删掉了一条「画面/动作/配音和旧作品一模一样、
        # 只有标题描述变了」的视频。只查标题挡不住这个，要查场景指纹。
        # 撞了先重写一次；重写还撞就当天不发（记 duplicate，不记 failed）。
        reason = self._duplicate_reason(scene)
        if reason:
            log.warning("抖音：这条和最近的作品重复（%s），让模型重写一次", reason)
            scene = await self._write_scene(hint)
            reason = self._duplicate_reason(scene)
            if reason:
                self.state.duplicate += 1
                self.state.last_error = f"内容重复，今天不再发：{reason}"
                self.state.last_at = time.time()
                self.state.save(self.state_path)
                log.warning("抖音：重写后还是重复（%s），今天不再发", reason)
                return self.state.last_error

        # ---- 做视频 -----------------------------------------------
        self._ensure_tools()
        try:
            result = await self._maker.make(scene)
        except VideoError as exc:
            self.state.failed += 1
            self.state.last_error = f"做视频失败：{exc}"
            self.state.last_at = time.time()
            self.state.save(self.state_path)
            log.warning("抖音：%s", self.state.last_error)
            return self.state.last_error

        # ---- 发 ---------------------------------------------------
        pub: PublishResult = await self._publisher.publish(
            result.artifact,
            scene.title,
            scene.desc,
            topics=scene.topics,
            dry_run=dry_run,
        )

        self.state.last_at = time.time()
        if dry_run:
            # 演练不消耗配额、不改「上次发了什么」—— 否则一次演练
            # 就会让她今天真的发不了。
            log.info("抖音：dry_run 走完整条链路，停在发布页（没点发作品）")
            return f"dry_run 完成：{scene.title}（{result.duration}，{result.volume}）"

        if pub.ok:
            self.state.published += 1
            self.state.last_title = scene.title
            self.state.last_error = ""
            # 只在**真的发出去**之后才删中间产物。发布失败时留着，
            # 万一要手工重发还得靠它们。
            self._cleanup(result)
            # 模板用过就记一笔，下次优先挑没用过的（同一个结构连拍三条很假）
            if template and self.store is not None:
                try:
                    for t in getattr(self.store, "items", []):
                        if t.pattern == template:
                            self.store.mark_used(t)
                            break
                except Exception:  # noqa: BLE001
                    pass
            log.info("抖音：发出去了「%s」", scene.title)
            self._remember_scene(scene)
        elif getattr(pub, "unsure", False):
            # 发布结果不确定（点了发作品，现场证据和主页复核都判不了）。
            #
            # **不重试、也不记 failed，但照样占当天额度。**
            # 为什么：这条很可能已经发出去了（d1 2026-11-06 真发测试就是
            # 已经发出、判据假阴性）。记 failed 会触发「今天失败过不再重试」
            # 闸门，实际又已经发布了 —— 白丢配额还误报；而不占额度的话，
            # 下个小时她可能再发一条，就真重复了。宁可少发一条，不可重复发。
            self.state.published += 1
            self.state.last_title = scene.title
            self.state.last_error = f"发布结果不确定：{pub.error}"
            log.warning(
                "抖音：%s（按已用掉今天额度处理，不重试、不记失败）",
                self.state.last_error,
            )
            # 也记一笔查重记录：这条很可能已经发出去了，下一条不能跟它重复。
            self._remember_scene(scene)
        elif self._is_env_error(getattr(pub, "error", "")):
            # 发布阶段才掉线 = 环境问题，**不记 failed**：
            # 记了会触发「今天失败过不再重试」把当天配额锁死，
            # 而实际只是手机没连上，重连后下一轮还能正常发。
            log.warning(
                "抖音：发布阶段手机不可用（%s），按环境问题跳过，不记失败",
                pub.error,
            )
            self.state.last_error = (
                f"环境问题（发布时手机不可用），这轮跳过：{(pub.error or '')[:80]}"
            )
        else:
            self.state.failed += 1
            self.state.last_error = f"发布失败：{pub.error}"

        self.state.save(self.state_path)
        return pub.line() if pub.ok else self.state.last_error
