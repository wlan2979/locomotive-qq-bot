"""把一条评论发到抖音。

## 为什么是一个独立模块，而不是多给模型几个手机动作

`use_phone`（见 `bot/hands.py`）一轮只给一次动作（`hands.max_uses_per_turn`），
而发一条评论至少要 6 个动作。让模型隔着六步编排等于闭着眼走：第一步点完
界面就变了，后面的动作全是猜的。`douyin_publisher` 已经把「一条多步的、
不可逆的操作」写成了固定流水线，这里照同一个模式做评论 —— 流程写死，
内容由调用方给。

## 一次评论的六步（一步都不能省）

0. **零点击预检。** 先 `hands.peek()` 看一眼（dump 失败也**不点屏幕**），
   再用 OCR 兜底，查广告词。2026-11-06 d1 实测：在抖音广告上点一下屏幕中心，
   直接把广告点下去了，模拟器被装进一个推广游戏。所以从这一刻起不许碰屏幕，
   广告页直接放弃 —— 一条评论不值得冒这个险。
1. **保证视频停住。** `hands.ensure_paused()`：先 dump 探一次，能读到 UI 树
   就说明视频没在播，**什么都不按**。127 是 **toggle 不是暂停**（d1 两次实测：
   已暂停再按 127 会变回播放），所以不能盲补媒体键。
2. **点开评论区。** 播放页那个按钮在 UI 树里叫「评论6852，按钮」，
   `tap_text("评论")` 的包含匹配能命中；命中不了再退坐标（0.913, 0.60，
   y 会随作品在 0.60~0.61 浮动）。
3. **点输入框。** 首选 UI 树里的 **EditText 节点**，按节点中心点 ——
   只是拿焦点，不会清掉已经有的字。UI 树没有 EditText 时才退到 OCR
   找占位文字（d1 实测占位有「分享你此刻的想法」「发条评论，说说你的感受」
   「爱评论的人，运气不会差」「期待你的评论」等写法，按子串收词表）。
   **找不到就放弃，绝不按固定比例盲点** —— d1 2026-11-06 真机事故：
   OCR 没认出占位文字时按着底部位置点，点进了别人评论的「回复」区，
   评论变成了挂在「雾屿雨」下面的回复。
4. **清草稿 → 切 ADBKeyboard → 打字。** 上次失败可能在输入框里留下草稿，
   先按 DEL 逐字删干净（不依赖坐标），再切输入法。
   中文只能走 `ADB_INPUT_B64` 广播，而广播**只有 ADBKeyBoard 是当前输入法时
   才生效**。判断「切生效了没有」必须看 `dumpsys input_method` 的
   `mCurMethodId` —— d1 实测 `settings get secure default_input_method`
   会滞后骗人（已经切过去了它还显示旧值）。
   打完字回读校验**走 UI 树的 EditText text**（OCR 只读得到底部
   「ADB Keyboard (ON)」，输入框里的字它读不到），而且要求**完全相等**、
   不能是回复模式 —— d1 那次发出去的内容是「[捂脸]这个视角挺有意思」，
   多的表情/前缀用「包含匹配」永远查不出来。
   **收尾要还原成切换前的那个输入法**，不是切到 `ime list` 里的第一个。
5. **切回原输入法，再点发送。** d1 实测：ADBKeyboard 没有软键盘，那种布局下
   评论区**根本没有「发送」按钮**；只有切回拼音、软键盘展开后才有
   `发送` Button。所以顺序是：输入完成 → 切回原输入法 → 重新读屏找「发送」。
   注意两点：**键盘没弹（`mInputShown=false`）也可能已经有发送按钮**；
   抖音表情面板偶发白屏会持续 8 秒以上（当天踩到 3 次），所以先
   `_wait_screen_readable()` 等屏幕可读；还不行就 **back 一次收表情面板 →
   点输入框重聚焦 → 再找一次**，仍不行才放弃。**绝不按比例盲点** ——
   无键盘布局里按 (0.894,0.919) 会落到评论列表上，可能误触别人的评论或点赞。
6. **确认真的发出去了。** 抖音评论区**没有 toast，面板也不会自己收**
   （d1 2026-11-06 实测：发送后 0.35/0.8/1.5/2.5/4.0 秒五次抓屏都没有成功提示，
   评论区一直开着、占位文字回到「期待你的评论」）。所以别等 toast、
   别等面板消失。硬证据只有 UI 树：
   - 出现自己的评论节点 `桃桃,我,<正文>,刚刚,回复 按钮,`；或
   - 评论区头部计数 +1（`评论 591 → 592`，只认头部，不认视频页右栏）。
   点过发送但两个证据都没有，不能算成功，也不能重试 —— 那会发出两条一样的评论。
7. **收掉评论区。** 评论成功后评论区不会自己关，必须按 back 收掉。
   不收的话下一个动作（分享）会在评论区页上找「分享」按钮，找不到可能点歪 ——
   d1 的全链路 E2E 就是这么被拖挂的。判面板用 UI 树词汇（「期待你的评论」
   「有爱评论」「放大评论区」「评论 N + 关闭」），不是 OCR。

## 护栏

- **广告页直接放弃。** 预检和停视频后各查一次：屏幕文字里有
  广告 / 立即下载 / 安装 / 了解更多 / 查看详情 就整条放弃，一个点击都不做。
- 屏幕上出现危险词（`hands.DANGER_WORDS`）、或账号层面的坏消息
  （验证码 / 实名 / 封禁 / 禁言 / 评论已关闭 / 请先登录）→ 立刻放弃，退出。
  危险词检查在**整屏**做，包括别人的评论文字 —— 宁可放弃一条评论。
- 只在自己的限频额度内发：每天最多 `daily_limit` 条、两条之间至少
  `min_gap_minutes` 分钟、只在 `hours` 时段内。默认 5 条 / 30 分钟 / 9-23 点
  （2026-11-06 用户定的：刷到有意思的才评，每天最多 5 条）。
- 失败计数，且**「发送后没确认」单独记一笔**：一旦有过这种不确定，
  今天不再发评论（防重复），等人工看一眼。
- **评论成功后必须把评论区收掉**：评论区不会自己关，留着会把后面的分享链路拖挂。
- 只操作抖音；前台不是抖音就拒绝，不在别的 App 里乱点。
- 停视频用 `ensure_paused()`（不是盲按 127 —— 那是 toggle）；
  点输入框只认 UI 树 EditText 或 OCR 占位文字，发送只认屏幕上真读到的「发送」，
  **没有比例盲点**。
- 全程用白名单原语（`peek` / `ensure_paused` / `tap_text` / `tap_xy` /
  `input_text` / `back`），没有 `adb shell` 这种万能口子。

## 坐标系

全部存**比例**，按 `hands.screen_size()` 的 override 尺寸换算（换分辨率不用改）。
三个 `RATIO_*` 是 2026-11-06 d1 在 720×1600 / 抖音 40.8.0 上量出来的：

- `RATIO_COMMENT_BUTTON` (0.913, 0.60)：x 很稳，y 随作品在 0.60~0.61 浮动。
- `RATIO_INPUT` (0.333, 0.965)：未聚焦输入框的位置。**只作记录，不再用于盲点** ——
  真机事故：OCR 没认出占位文字时按这个位置点，点进了别人评论的「回复」区。
- `RATIO_SEND` (0.894, 0.919)：**只对「拼音软键盘展开态」成立**，
  而且现在**不再用它盲点**（原因见第 5 步），留着只作记录/将来校准参考。

## 和 core 的接法

本模块不调 LLM，文案由调用方给（和 `DouyinPublisher.publish()` 一致）：

    commenter = DouyinCommenter(core._hands, daily_limit=5)
    reason = commenter.budget_reason()
    if not reason:
        result = await commenter.comment("这句评论")

`budget_reason()` 只读状态、不碰手机，适合在决定「要不要评论」之前调用。
`comment()` 内部也会再查一次额度，避免调用方漏查。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .hands import HandsError, danger_hit

log = logging.getLogger(__name__)


class CommentError(RuntimeError):
    """评论链路上任何一步没走通。"""


# --------------------------------------------------------------------------- #
# 屏幕上能读到什么

# 出现这些 = 这条路已经不能再往下走。不是「危险操作」（那是 hands 的事），
# 是账号 / 内容层面已经出问题，再点只会把事情弄大。
# 和 douyin_publisher._ABORT_HINTS 同一套思路，补上评论特有的几种。
_HARD_STOP_WORDS: Tuple[str, ...] = (
    "账号异常", "验证码", "实名", "封禁", "禁言", "违规",
    "限制评论", "评论已关闭", "暂时无法评论", "请先登录",
)

# 输入框空着时的占位文字。**版本/视频不同会换写法，所以按子串收**。
# 2026-11-06 d1 实测见到的几种：
#   发之前：「分享你此刻的想法」「发条评论，说说你的感受」「爱评论的人，运气不会差」
#   发之后：「期待你的评论」「有爱评论，说点儿好听的」
# 后几行是之前实测见过的写法，留着兼容。
_INPUT_PLACEHOLDER: Tuple[str, ...] = (
    "分享你此刻的想法", "分享此刻的想法", "发条评论", "说说你的感受",
    "爱评论的人", "运气不会差", "有爱评论", "说点儿好听的",
    "期待你的评论",
    "留下你的精彩评论", "留下你的神评论", "善语结善缘",
    "说点什么", "写评论", "评论一下",
)

# 评论区开着的判据（OCR）：占位文字，或者列表标题。
_PANEL_OPEN_HINTS: Tuple[str, ...] = _INPUT_PLACEHOLDER + ("全部评论",)

# 评论区开着的判据（UI 树）。`_close_panel()` 用这组 ——
# d1 2026-11-06 实测：发送后占位变成「期待你的评论」、底部也没有「发送」，
# 用 OCR 判据会误判成「面板没开」，一条 back 都不按，把后面的分享链路拖挂。
# 视频页的「已选中，推荐，按钮」不会撞上这些词。
_PANEL_UI_WORDS: Tuple[str, ...] = (
    "期待你的评论", "有爱评论", "爱评论的人", "放大评论区", "全部评论",
    "分享你此刻的想法", "分享此刻的想法", "发条评论", "说说你的感受",
    "留下你的", "评论一下",
)

# 发送按钮。只在底部右侧找，避免点到评论正文里的「发送」两个字。
_SEND_WORD = "发送"

# 评论区头部的评论数（`评论 591`）。**不要用视频页右栏的「评论580，按钮」** ——
# d1 实测两处会差几条，基线必须和发布后的读法同源。
_COUNT_RE = re.compile(r"评论\s*(\d+)")
_HEADER_COUNT_RE = re.compile(r"^评论\s*(\d+)$")

# 自己刚发的评论节点里的标记。d1 2026-11-06 实测节点长这样：
#   `桃桃,我,这个画面有点好看,刚刚,回复 按钮,`
_ACK_WORDS: Tuple[str, ...] = ("刚刚", "回复")

# 「回复某条评论」模式的输入框/标题特征。d1 2026-11-06 真机事故：
# 误点进别人评论的回复区，评论变成挂在别人下面的回复。
_REPLY_RE = re.compile(r"^回复\s*@")

# 成功证据不用 toast：d1 实测抖音发评论**没有任何 toast**，见 `_wait_sent()`。

# 成功证据不用 toast：d1 实测抖音发评论**没有任何 toast**，见 `_wait_sent()`。

# 输入框 / 发送按钮都贴在屏幕底部。判据里用这个比例划「底部区域」。
_BOTTOM_ZONE = 0.72

# 抖音评论最多一百来字。超过就别硬塞 —— 截断比报错体面。
_MAX_CHARS = 100

# 广告 / 下载页特征。见到就整条放弃：**一个点击都不许有**。
# 2026-11-06 d1 实测：广告页点屏幕中心会直接把广告点下去、装推广游戏，
# 所以哪怕只是停视频，只要这屏像广告就退出去。
_AD_WORDS: Tuple[str, ...] = (
    "广告", "立即下载", "安装", "了解更多", "查看详情",
)


# --------------------------------------------------------------------------- #
# OCR（带坐标）
#
# 本模块不自己跑 OCR：`hands.ocr_boxes()` 已经会截图 + OCR 并返回
# `OcrHit`（`.text/.x/.y/.w/.h/.center`）。这里只是把它转成内部统一的
# `OcrLine`（左/上/右/下边界），下面的几何判断（找底部、找右侧）好写。


@dataclass
class OcrLine:
    """OCR 出来的一行字，带屏幕坐标。"""

    text: str
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def center(self) -> Tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


# --------------------------------------------------------------------------- #
# 结果与状态


@dataclass
class CommentResult:
    ok: bool
    text: str = ""
    error: str = ""
    steps: List[str] = field(default_factory=list)

    def line(self) -> str:
        if self.ok:
            return f"（评论发出去了：{self.text[:30]}）"
        return f"（评论没发成：{self.error}）"


@dataclass
class CommentState:
    """今天评论了没。存 `data/douyin_comment_state.json`。

    `unsure` 单独记：「点了发送但没确认」的次数。这种状态**不能重试**，
    因为上一条可能已经发出去了 —— 重试就是两条一样的评论。
    """

    day: str = ""
    sent: int = 0
    failed: int = 0
    unsure: int = 0
    last_at: float = 0.0
    last_error: str = ""
    last_text: str = ""

    @classmethod
    def load(cls, path: Path) -> "CommentState":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(
                day=str(raw.get("day") or ""),
                sent=int(raw.get("sent") or 0),
                failed=int(raw.get("failed") or 0),
                unsure=int(raw.get("unsure") or 0),
                last_at=float(raw.get("last_at") or 0.0),
                last_error=str(raw.get("last_error") or ""),
                last_text=str(raw.get("last_text") or ""),
            )
        except Exception:  # noqa: BLE001
            # 状态文件坏了就当今天还没评论过。最坏结果是多发一条，
            # 比「永久不发」轻 —— 和 douyin_scheduler.State 同一个取舍。
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
            log.warning("评论状态保存失败：%s", exc)


# --------------------------------------------------------------------------- #
# 主类


class DouyinCommenter:
    """在抖音上发一条评论。只依赖 `PhoneHands` 的 adb 通道。"""

    PACKAGE = "com.ss.android.ugc.aweme"

    # 2026-11-06 d1 实测（720×1600，抖音 40.8.0），见模块 docstring「坐标系」。
    RATIO_COMMENT_BUTTON = (0.913, 0.60)  # 播放页右侧「评论6852，按钮」，y 0.60~0.61
    # 未聚焦输入框 center (240,1544)。**只作记录，不再用于盲点** ——
    # d1 2026-11-06 真机事故：OCR 没认出占位文字时按这个位置盲点，
    # 点进了别人评论的「回复」区，评论变成挂在别人下面的回复。
    RATIO_INPUT = (0.333, 0.965)
    # 只对「拼音软键盘展开态」成立（发送 Button center 约 (644,1471)）。
    # **不做盲点**：无键键盘布局下这里没有发送按钮，按下去会点进评论列表。
    RATIO_SEND = (0.894, 0.919)

    def __init__(
        self,
        hands: Any,
        *,
        daily_limit: int = 5,
        min_gap_minutes: float = 30.0,
        hours: Sequence[int] = (9, 23),
        failed_limit: int = 3,
        state_path: Optional[Path] = None,
    ) -> None:
        self.hands = hands
        self.daily_limit = max(0, int(daily_limit))
        self.min_gap_minutes = max(0.0, float(min_gap_minutes))
        try:
            self.hours = (int(hours[0]), int(hours[1]))
        except (TypeError, ValueError, IndexError):
            self.hours = (9, 23)
        self.failed_limit = max(0, int(failed_limit))
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else Path(__file__).resolve().parent.parent / "data" / "douyin_comment_state.json"
        )
        self.state = CommentState.load(self.state_path)

        self._size: Optional[Tuple[int, int]] = None
        # 切换前的默认输入法。收尾按它精确还原，不碰用户的设置。
        self._prev_ime: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 额度

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.state.day:
            self.state = CommentState(day=today)
            self.state.save(self.state_path)

    def budget_reason(self, *, check_hours: bool = True) -> str:
        """现在能不能评论。返回空串表示可以，否则是「为什么不能」。"""
        self._roll_day()

        if self.daily_limit <= 0:
            return "评论是关的（daily_limit=0）"
        if check_hours:
            start, end = self.hours
            if not (start <= time.localtime().tm_hour < end):
                return f"不在允许评论的时段（{start}-{end} 点）"
        if self.state.sent >= self.daily_limit:
            return f"今天已经评论过 {self.state.sent} 条（上限 {self.daily_limit}）"
        if self.state.unsure > 0:
            return (
                "今天有一次评论没确认发没发出去"
                f"（{self.state.last_error[:40]}），今天不再发，免得重复"
            )
        if self.failed_limit and self.state.failed >= self.failed_limit:
            return (
                f"今天失败过 {self.state.failed} 次"
                f"（{self.state.last_error[:40]}），不再试"
            )
        gap = self.min_gap_minutes * 60.0
        if gap and time.time() - self.state.last_at < gap:
            return f"距上次评论不足 {self.min_gap_minutes:.0f} 分钟"
        if self.hands is None:
            return "手机没接上"
        return ""

    def _mark_sent(self, text: str) -> None:
        self._roll_day()
        self.state.sent += 1
        self.state.last_at = time.time()
        self.state.last_text = (text or "")[:60]
        self.state.save(self.state_path)

    def _mark_failed(self, error: str, *, unsure: bool = False) -> None:
        self._roll_day()
        self.state.failed += 1
        if unsure:
            self.state.unsure += 1
        self.state.last_error = (error or "")[:120]
        self.state.save(self.state_path)

    # ------------------------------------------------------------------ #
    # 基础

    async def _size_cached(self) -> Tuple[int, int]:
        if self._size is None:
            self._size = await self.hands.screen_size()
        return self._size

    async def _xy(self, ratio: Tuple[float, float]) -> Tuple[int, int]:
        """比例 → 像素。夹在屏幕内，防止算出屏幕外的坐标。"""
        width, height = await self._size_cached()
        x = int(round(ratio[0] * width))
        y = int(round(ratio[1] * height))
        return max(1, min(width - 2, x)), max(1, min(height - 2, y))

    async def _tap_ratio(
        self, ratio: Tuple[float, float], what: str, *, wait: float = 1.5
    ) -> None:
        x, y = await self._xy(ratio)
        await self.hands.tap_xy(x, y)
        log.debug("评论：按比例点%s @(%d,%d)", what, x, y)
        await asyncio.sleep(wait)

    def _check_screen(self, blob: str) -> None:
        """屏幕上读到的东西里有没有该停手的。"""
        reason = str(getattr(self.hands, "aborted_reason", "") or "")
        if reason:
            raise CommentError(f"手机那一侧已经中止：{reason}")

        hit = danger_hit(blob)
        if hit:
            raise CommentError(f"屏幕上出现了「{hit}」，这种页面不碰，停手")

        for word in _HARD_STOP_WORDS:
            if word in blob:
                raise CommentError(f"界面上出现「{word}」，不能再往下走，停手")

    async def _nodes(self) -> List[Any]:
        """读 UI 树节点。读不到返回空列表（不是错误 —— 视频在播时本来就读不到）。"""
        try:
            return list(await self.hands.ui() or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("评论：读 UI 树失败（%s）", exc)
            return []

    async def _labels(self) -> List[str]:
        return [
            (getattr(node, "label", "") or "").strip()
            for node in await self._nodes()
            if (getattr(node, "label", "") or "").strip()
        ]

    @staticmethod
    def _edit_node(nodes: Sequence[Any]) -> Any:
        """UI 树里的输入框节点（class 是 EditText）。没有返回 None。"""
        for node in nodes:
            if "edittext" in (getattr(node, "cls", "") or "").lower():
                return node
        return None

    @staticmethod
    def _reply_mode(edit_text: str, labels: Sequence[str]) -> bool:
        """现在是不是「回复某条评论」模式。

        回复模式的输入框/标题会以「回复 @某人」开头。d1 2026-11-06 真机事故
        就是点进了别人的回复区，评论挂在了别人下面 —— 一旦看到就放弃。
        """
        if _REPLY_RE.match((edit_text or "").strip()):
            return True
        return any(_REPLY_RE.match((label or "").strip()) for label in labels)

    async def _ocr_boxes(self) -> List[OcrLine]:
        """截图 + OCR，返回带坐标的行。失败返回空列表。

        直接问 `hands.ocr_boxes()` 要 —— OCR 引擎、线程、截图的坑都在那边，
        本模块只做「转成左/上/右/下边界」这一件事。
        """
        try:
            hits = await self.hands.ocr_boxes()
        except Exception as exc:  # noqa: BLE001
            log.warning("评论：OCR 取坐标失败（%s）", exc)
            return []
        return [
            OcrLine(
                text=h.text,
                left=h.x,
                top=h.y,
                right=h.x + h.w,
                bottom=h.y + h.h,
            )
            for h in hits or []
            if getattr(h, "text", "").strip()
        ]

    async def _look(self) -> Tuple[List[str], List[OcrLine], str]:
        """视频页 / 未知页面的一眼：UI 树和 OCR 都读，顺便查危险词。"""
        labels = await self._labels()
        lines = await self._ocr_boxes()
        blob = "\n".join(labels + [line.text for line in lines])
        if not blob.strip():
            raise CommentError("这屏既读不到控件也 OCR 不出字（可能黑屏或掉线），不敢瞎点")
        self._check_screen(blob)
        return labels, lines, blob

    async def _look_ocr(self) -> Tuple[List[OcrLine], str]:
        """评论区专用的一眼：**只 OCR**。

        评论区的 UI 树常年读不到，`ui()` 每次要重试三轮、白等六七秒 ——
        所以这一屏不读它，少等一轮、失败也快。
        """
        lines = await self._ocr_boxes()
        blob = "\n".join(line.text for line in lines)
        if not blob.strip():
            raise CommentError("评论区 OCR 不出字（可能黑屏或掉线），不敢瞎点")
        self._check_screen(blob)
        return lines, blob

    @staticmethod
    def _brief(blob: str, limit: int = 12) -> str:
        """把一屏文字压成一行，塞进报错里 —— 不然没法判断停在了哪。"""
        parts = [part.strip() for part in (blob or "").splitlines() if part.strip()]
        return " / ".join(parts[:limit])[:200] or "（空）"

    def _find_line(
        self,
        lines: Sequence[OcrLine],
        words: Sequence[str],
        size: Tuple[int, int],
        *,
        bottom: bool = False,
        right: bool = False,
    ) -> Optional[OcrLine]:
        """在这些字里找一行。`bottom` / `right` 用来把范围收到屏幕底部/右侧。"""
        width, height = size
        floor = int(height * _BOTTOM_ZONE) if bottom else 0
        left_min = int(width * 0.55) if right else 0
        pool = [
            line
            for line in lines
            if line.text
            and any(word in line.text.replace(" ", "") for word in words)
            and line.top >= floor
            and line.left >= left_min
        ]
        if not pool:
            return None
        # 越靠底越好；同一高度时越靠右越好（发送按钮在右边）。
        pool.sort(key=lambda line: (line.top, line.left), reverse=True)
        return pool[0]

    async def _tap_line(self, line: OcrLine, what: str, *, wait: float = 1.5) -> None:
        x, y = line.center
        await self.hands.tap_xy(x, y)
        log.info(
            "评论：按 OCR 坐标点%s @(%d,%d)（读到的是「%s」）",
            what,
            x,
            y,
            line.text[:20],
        )
        await asyncio.sleep(wait)

    def _panel_open_from(
        self, lines: Sequence[OcrLine], blob: str, size: Tuple[int, int]
    ) -> bool:
        """评论区开没开。判据：占位文字/标题出现，或者底部有「发送」。"""
        if any(word in blob for word in _PANEL_OPEN_HINTS):
            return True
        _, height = size
        floor = int(height * _BOTTOM_ZONE)
        return any(_SEND_WORD in line.text for line in lines if line.top >= floor)

    # ------------------------------------------------------------------ #
    # 输入法

    async def _current_ime(self) -> str:
        """当前**真正生效**的输入法。

        用 `dumpsys input_method` 的 `mCurMethodId`，不用
        `settings get secure default_input_method` —— d1 2026-11-06 实测
        后者会滞后：IME 已经切过去了，它还在显示旧值。拿它判断「切没切成功」
        会得出完全相反的结论。
        """
        code, out = await self.hands._adb(
            "shell", "dumpsys input_method | grep mCurMethodId", timeout=12.0
        )
        text = out or ""
        hit = re.search(r"mCurMethodId=([^\s]+)", text)
        if hit:
            return hit.group(1).strip()
        # 极少数系统/权限下 dumpsys 读不到，退回 settings（只是兜底，可能有滞后）。
        code, out = await self.hands._adb(
            "shell", "settings get secure default_input_method", timeout=10.0
        )
        current = (out or "").strip()
        return "" if current.lower() in ("", "null") else current

    async def _ensure_ime(self, on: bool) -> None:
        """切 ADBKeyboard 前先记下原输入法；切回来时精确还原。

        `hands.use_adb_keyboard(False)` 是切到 `ime list` 里第一个别的输入法，
        那会把用户自己的默认输入法换掉 —— 这里改成还原切换前记下的那个。
        """
        current = await self._current_ime()
        has_adb = "adbkeyboard" in current.lower()

        if on:
            if self._prev_ime is None and not has_adb:
                self._prev_ime = current
            if has_adb:
                return
            try:
                installed = await self.hands.has_adb_keyboard()
            except Exception:  # noqa: BLE001
                installed = False
            if not installed:
                raise CommentError(
                    "手机上没装 ADBKeyBoard（com.android.adbkeyboard），中文打不进去"
                )
            note = await self.hands.use_adb_keyboard(True)
            current = await self._current_ime()
            if "adbkeyboard" not in current.lower():
                raise CommentError(
                    "输入法没切到 ADBKeyboard"
                    f"（当前={current or '读不到'}，{note}），中文会打不进去"
                )
            await asyncio.sleep(0.6)
            return

        # 切回来：没记录过原输入法就交给 hands 的老办法。
        if not self._prev_ime:
            if has_adb:
                await self.hands.use_adb_keyboard(False)
            return
        if not has_adb:
            return
        target = self._prev_ime
        await self.hands._adb("shell", f"ime set {target}", timeout=10.0)
        await asyncio.sleep(0.4)
        now = await self._current_ime()
        if "adbkeyboard" in now.lower():
            log.warning("评论：输入法没能切回 %s（当前还是 ADBKeyboard）", target)
        else:
            log.info("评论：输入法已还原成 %s", target)

    async def _restore_ime_for_send(self) -> None:
        """输入完成后切回原输入法，把软键盘和「发送」按钮带回来。

        d1 2026-11-06 实测：ADBKeyboard 没有软键盘，那种布局下评论区
        **没有「发送」按钮**；切回拼音、软键盘展开之后才有 `发送` Button。
        所以「先切回来，再找发送」是必经步骤，不是优化项。

        另外两条实测结论：
        - 切回拼音后**键盘不一定会弹**（`mInputShown=false`），但发送按钮
          **照样会在** —— 所以别把「没键盘」当成失败条件；
        - 正常 1 秒内就切好；但抖音的表情面板偶发白屏会拖到 8 秒以上，
          由 `_wait_screen_readable()` 兜。
        """
        await self._ensure_ime(False)
        for _ in range(5):
            await asyncio.sleep(0.4)
            current = await self._current_ime()
            if current and "adbkeyboard" not in current.lower():
                break
        await asyncio.sleep(0.6)

    async def _wait_screen_readable(self, timeout: float = 8.0) -> bool:
        """等评论区重新变成「读得到」的状态。

        d1 2026-11-06 实测：抖音的表情面板偶尔会展开成整屏白，持续 8 秒以上，
        这期间 UI dump 和 OCR 都是空的 —— 这时候判「找不到发送」是误判
        （当天已经踩到 3 次白屏，2 次靠重试/重跑才过）。

        判据：UI 树里出现 EditText 或「发送」节点，或者 OCR 能读出字。
        注意**键盘没弹也照样能读到发送按钮**（mInputShown=false 不影响）。
        """
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            for node in await self._nodes():
                cls = (getattr(node, "cls", "") or "").lower()
                label = (getattr(node, "label", "") or "").strip()
                if "edittext" in cls or label == _SEND_WORD:
                    return True
            try:
                lines = await self._ocr_boxes()
            except Exception:  # noqa: BLE001
                lines = []
            if lines:
                return True
            await asyncio.sleep(0.7)
        return False

    async def _dismiss_overlay(self) -> None:
        """back 一次，收掉盖在评论区上的临时层（表情面板白屏/软键盘）。

        **只按一次**：连按会把评论面板甚至抖音退出去。按完等它收。
        """
        await self.hands.back()
        await asyncio.sleep(1.0)
        log.info("评论：按了一次 back，收掉表情面板/键盘")


    # ------------------------------------------------------------------ #
    # 流程的每一步

    @staticmethod
    def _ad_hit(blob: str) -> str:
        """这屏像不像广告 / 下载页。返回命中的词，没有就空串。"""
        return next((word for word in _AD_WORDS if word in (blob or "")), "")

    async def _peek_view(self) -> Tuple[List[Any], List[OcrLine], str]:
        """**零点击**看一眼：`hands.peek()`（dump 失败也不点屏幕）+ OCR 兜底。

        返回 (节点, OCR 行, 拼起来的文字)。广告、危险词、账号异常的判断
        都基于这份文字。
        """
        text, nodes = await self.hands.peek()
        if text.startswith("__ABORT__"):
            raise CommentError(
                text.replace("__ABORT__", "").strip("（） ") or "手机侧中止了"
            )
        labels = [node.label for node in nodes if node.label]
        # UI 树读不到（视频在播）时 OCR 是唯一能看见的东西。
        lines = await self._ocr_boxes() if not nodes else []
        blob = "\n".join([text, *labels, *[line.text for line in lines]])
        return nodes, lines, blob

    async def _precheck_screen(self) -> None:
        """开头预检：停在广告页就放弃，别去停它的视频、更别点它。

        必须先做这一步再碰媒体键/屏幕：广告页上的任何一次点击都可能触发下载
        （2026-11-06 d1 实测，模拟器被装进过推广游戏）。
        """
        _nodes, _lines, blob = await self._peek_view()
        hit = self._ad_hit(blob)
        if hit:
            raise CommentError(
                f"这屏是广告 / 下载页（读到「{hit}」），一条点击都不做，放弃"
            )
        self._check_screen(blob)

    async def _stop_video(self) -> None:
        """保证视频停住，**一个点击都不要有**。

        用 `hands.ensure_paused()`：它先 dump 探一次，能读到 UI 树就说明视频
        没在播，**什么都不按**。因为媒体键 127 是 **toggle**（d1 两次实测：
        已暂停再按会变回播放），盲补会把评论面板下面的视频按成播放。
        停住之后 dump 才读得到，后面的按钮才找得着。

        原来的救法是在屏幕中心点一下暂停（`hands.observe()` 里那段），
        但 d1 2026-11-06 实测：那一屏如果是广告，这一点就把广告点下去了
        （模拟器被装过推广游戏），所以这条链路不许点屏幕。
        """
        pressed = await self.hands.ensure_paused()
        log.info("评论：视频状态已确认（%s）", "这次按了媒体键" if pressed else "本来就没在播")

        nodes, lines, blob = await self._peek_view()
        if not nodes and not lines:
            raise CommentError("视频停不下来也读不到任何文字（可能黑屏或掉线），不敢瞎点")

        # 停住之后再查一次广告：可能刚才在播、OCR 没读准，暂停后 UI 树才露出来。
        hit = self._ad_hit(blob)
        if hit:
            raise CommentError(
                f"这屏是广告 / 下载页（读到「{hit}」），一条点击都不做，放弃"
            )
        self._check_screen(blob)

        pkg = await self.hands.current_package()
        if "aweme" not in (pkg or ""):
            raise CommentError(f"已经不在抖音里了（前台是 {pkg or '未知'}）")

    async def _open_panel(self) -> None:
        """点开评论区。UI 树点得到就按文字点，点不到再退坐标。"""
        size = await self._size_cached()
        # 可能本来就开着 —— 先看一眼，别对着评论区再点一次「评论」。
        labels, lines, blob = await self._look()
        if self._panel_open_from(lines, blob, size):
            log.info("评论：评论区已经开着了")
            return

        note = await self.hands.tap_text("评论")
        tapped_by_text = "点了" in note
        if tapped_by_text:
            await asyncio.sleep(2.5)
        else:
            log.info("评论：UI 树里点不到「评论」（%s），改用坐标", note)
            await self._tap_ratio(self.RATIO_COMMENT_BUTTON, "评论按钮", wait=2.5)

        lines, blob = await self._look_ocr()
        if self._panel_open_from(lines, blob, size):
            log.info("评论：评论区已打开")
            return

        if tapped_by_text:
            # 文字点到了却没开：可能点错了同名节点，用坐标再来一次。
            log.info("评论：点完「评论」没看到评论区，换坐标再点一次")
            await self._tap_ratio(self.RATIO_COMMENT_BUTTON, "评论按钮", wait=2.5)
            lines, blob = await self._look_ocr()
            if self._panel_open_from(lines, blob, size):
                log.info("评论：评论区已打开（坐标）")
                return

        raise CommentError(f"点不开评论区。这屏读到的文字：{self._brief(blob)}")

    async def _focus_input(self) -> None:
        """找到并点中输入框。**绝不按固定比例盲点。**

        找法按可靠性排：
        1. UI 树里的 EditText 节点（最准）—— 点它的中心，只是拿焦点，
           已经聚焦、已经有字也不会清掉内容；
        2. OCR 读到占位文字那一行，按框中心点；
        3. 都找不到就放弃这条。

        为什么删掉了「退到底部固定位置 (240,1544)」：d1 2026-11-06 真机事故，
        OCR 没认出占位文字时按那个位置，点进了别人评论的「回复」区，
        评论最后变成挂在「雾屿雨」下面的回复，还带了个表情前缀。
        """
        nodes = await self._nodes()
        edit = self._edit_node(nodes)
        if edit is not None:
            x, y = edit.center
            await self.hands.tap_xy(x, y)
            log.info("评论：点中输入框（UI 树 EditText @(%d,%d)）", x, y)
            await asyncio.sleep(1.0)
            return

        size = await self._size_cached()
        lines, _blob = await self._look_ocr()
        line = self._find_line(lines, _INPUT_PLACEHOLDER, size)
        if line is not None:
            await self._tap_line(line, "输入框")
            return

        raise CommentError(
            "找不到评论输入框（UI 树里没有 EditText，OCR 也没读到占位文字）——"
            "不按固定比例盲点，否则会点进别人的回复区。放弃这条。"
        )

    async def _clear_input(self) -> None:
        """清掉输入框里可能残留的草稿（上次没发出去留下的）。

        用 DEL 键逐字删 —— 不依赖任何坐标。删完回读，清不掉就放弃这条：
        带着旧草稿打字，最后会发出一条内容不对的评论（d1 真机事故：
        「😆这个视角挺有意思」就是表情 + 正文粘在一起）。
        """
        for _ in range(3):
            nodes = await self._nodes()
            edit = self._edit_node(nodes)
            if edit is None:
                return  # 读不到输入框，交给后面的精确校验兜底
            current = (getattr(edit, "label", "") or "").strip()
            if not current or any(word in current for word in _INPUT_PLACEHOLDER):
                return
            log.info("评论：输入框里有残留草稿「%s」，先删掉", current[:20])
            count = min(len(current) + 2, 200)
            await self.hands._adb(
                "shell", "input keyevent " + " ".join(["67"] * count), timeout=20.0
            )
            await asyncio.sleep(0.6)
        raise CommentError("输入框里有清不掉的残留草稿，放弃这条，免得发出内容不对的评论")

    async def _verify_typed(self, text: str) -> List[str]:
        """确认文字**原样**进了输入框，并且不是「回复」模式。

        两个硬要求（d1 2026-11-06 真机事故之后加的）：
        1. **完全相等**，不是「包含」：那次发出去的是「[捂脸]这个视角挺有意思」，
           多出来的表情/前缀用包含匹配永远查不出来；
        2. 界面里不能出现「回复 @某人」，否则这条会挂在别人评论下面。

        用 UI 树里 EditText 的 text 读，不用 OCR（OCR 只读得到底部的
        「ADB Keyboard (ON)」，输入框里的字它读不到）。返回那一眼的 label，
        调用方顺手拿它取评论数基线。
        """
        want = "".join((text or "").split())
        if not want:
            raise CommentError("评论内容是空的")
        last = ""
        for _ in range(3):
            await asyncio.sleep(1.0)
            nodes = await self._nodes()
            labels = [
                (getattr(node, "label", "") or "").strip()
                for node in nodes
                if (getattr(node, "label", "") or "").strip()
            ]
            edit = self._edit_node(nodes)
            got = "".join(
                ((getattr(edit, "label", "") or "") if edit is not None else "").split()
            )
            if self._reply_mode(got, labels):
                raise CommentError(
                    "现在是「回复某条评论」的模式，不是发独立评论 —— 放弃这条"
                )
            if edit is not None:
                last = got
                if got == want:
                    return labels
        raise CommentError(
            f"输入框里的字和要评的内容对不上（期望「{want}」，实际「{last or '读不到'}」）"
            "—— 不盲发"
        )

    @staticmethod
    def _comment_count(labels: Sequence[str]) -> int:
        """评论区头部的评论数（`评论 591`）。读不到返回 -1。

        **不用视频页右栏的「评论580，按钮」**：d1 2026-11-06 实测两处会差几条，
        基线必须和发布后读的是同一处。
        """
        loose = -1
        for label in labels:
            text = (label or "").strip()
            if not text or "按钮" in text:
                continue
            header = _HEADER_COUNT_RE.match(text)
            if header:
                try:
                    return int(header.group(1))
                except ValueError:
                    continue
            hit = _COUNT_RE.search(text)
            if hit and loose < 0:
                try:
                    loose = int(hit.group(1))
                except ValueError:
                    continue
        return loose

    @staticmethod
    def _find_own_comment(labels: Sequence[str], probe: str) -> str:
        """UI 树里找自己刚发的那条评论节点。找不到返回空串。

        d1 2026-11-06 实测，发出去之后 UI 树里会出现这样的节点：
        `桃桃,我,这个画面有点好看,刚刚,回复 按钮,`
        """
        flat = "".join((probe or "").split())
        if not flat:
            return ""
        for label in labels:
            text = "".join((label or "").split())
            if flat in text and any(word in text for word in _ACK_WORDS):
                return label
        return ""

    async def _refocus_input(self) -> bool:
        """重新点一下输入框，把软键盘叫回来。

        切回原输入法不一定会自动弹出键盘（系统行为），没键盘就没「发送」按钮。
        点一下**已经聚焦、已经有字**的输入框是安全的：不会清掉已输入的文字，
        只是重新拿焦点。返回 False = 连输入框都从 UI 树里找不到。
        """
        try:
            nodes = await self.hands.ui()
        except Exception as exc:  # noqa: BLE001
            log.debug("评论：重新聚焦时读不到 UI 树（%s）", exc)
            return False
        for node in nodes or []:
            if "edittext" in (getattr(node, "cls", "") or "").lower():
                x, y = node.center
                await self.hands.tap_xy(x, y)
                log.info("评论：点了一下输入框 @(%d,%d)，等键盘回来", x, y)
                await asyncio.sleep(1.2)
                return True
        return False

    async def _tap_send(self) -> bool:
        """找「发送」并点它；找不到返回 False。**绝不按比例盲点。**

        d1 2026-11-06 实测：
        - ADBKeyboard（无软键盘）布局下评论区**没有「发送」按钮**；
        - 切回拼音、软键盘展开后才有 `发送` Button（center 约 (644,1471)）。
        无键盘布局里按固定比例 (0.894,0.919) 点下去会落到评论列表上，
        可能误触别人的评论或点赞 —— 所以只认屏幕上真的读到的「发送」。
        """
        # 1) UI 树：拼音展开态下它是真实 Button，dump 读得到。
        #    只认**精确命中**（点完的回报里 label 必须正好是「发送」），
        #    包含匹配可能命中「发送给…」之类的别的文字。
        note = await self.hands.tap_text(_SEND_WORD)
        if "点了「发送」" in (note or ""):
            log.info("评论：点了「发送」（UI 树精确命中）")
            await asyncio.sleep(2.0)
            return True

        # 2) OCR 兜底：只认文字**恰好是**「发送」、且落在底部右侧的那一个。
        #
        # d1 2026-11-06 实测：ADBKeyboard 激活时抖音评论区的表情面板会展开成
        # 整屏白（截图全白但 UI 树还在）。这时候 OCR 什么都读不到，
        # `_look_ocr()` 会抛 CommentError —— **必须吞掉返回 False**，
        # 让外面「点一下输入框把键盘叫回来」的兜底去救，不能把整条判死。
        size = await self._size_cached()
        try:
            lines, _blob = await self._look_ocr()
        except CommentError as exc:
            log.info("评论：找发送时读不到屏幕（%s），交给重新聚焦兜底", exc)
            return False
        width, height = size
        floor = int(height * _BOTTOM_ZONE)
        left_min = int(width * 0.55)
        target = next(
            (
                line
                for line in lines
                if line.text.strip() == _SEND_WORD
                and line.top >= floor
                and line.left >= left_min
            ),
            None,
        )
        if target is None:
            log.info("评论：屏幕上找不到「发送」按钮，不盲点（%s）", note)
            return False
        await self._tap_line(target, "发送", wait=2.0)
        return True

    async def _wait_sent(self, text: str, baseline_count: int = -1) -> str:
        """等评论出现的**硬证据**。不靠 toast，也不靠面板收。

        d1 2026-11-06 实测：抖音评论区发送成功**没有任何 toast**
        （发送后 0.35 / 0.8 / 1.5 / 2.5 / 4.0 秒五次抓屏都没有「评论成功」之类的字），
        面板也**不会收**（一直停在评论区，占位文字回到「有爱评论，说点儿好听的」）。
        所以能用的证据只有 UI 树：
          - 出现自己的评论节点 `桃桃,我,<正文>,刚刚,回复 按钮,`；或
          - 评论区计数 +1（评论 591 → 592）。
        """
        probe = "".join(text.split())[:8]
        last_count = baseline_count
        for _ in range(4):
            await asyncio.sleep(0.8)
            labels = await self._labels()
            own = self._find_own_comment(labels, probe)
            if own:
                return f"UI 树里出现了自己那条评论（{own[:40]}）"
            count = self._comment_count(labels)
            if count >= 0 and last_count >= 0 and count > last_count:
                return f"评论数涨了（{last_count} → {count}）"
            if count >= 0:
                last_count = count

        # UI 树一直读不到时最后退一次 OCR：必须**两个条件同时成立** ——
        # 占位文字回来了（输入框已清空）**且**正文出现在屏幕上（列表里有它）。
        # 只看正文可能是输入框还没清掉，会误判成功。
        try:
            _lines, blob = await self._look_ocr()
        except CommentError:
            blob = ""
        placeholder_back = any(word in blob for word in _INPUT_PLACEHOLDER)
        if placeholder_back and probe and probe in "".join(blob.split()):
            return "输入框清空（占位文字回来）且 OCR 读到评论正文"

        raise CommentError(
            "点了发送，但 UI 树里没出现自己的评论、评论数也没涨"
            f"（发送前计数 {baseline_count}）—— 不能确定发没发出去，今天不再发评论"
        )

    def _panel_open_in_ui(self, labels: Sequence[str]) -> bool:
        """用 UI 树判断评论区开没开。

        d1 2026-11-06 实测：评论**发送后**输入框占位变成「期待你的评论」、
        底部也没有「发送」按钮 —— 旧的 OCR 判据会判成「面板没开」，
        `_close_panel()` 一条 back 都不按，评论区留在屏幕上，
        把后面的分享链路整个拖挂。所以这里改用 UI 树词汇。
        """
        blob = " ".join(label or "" for label in labels)
        if any(word in blob for word in _PANEL_UI_WORDS):
            return True
        has_count = any(
            _HEADER_COUNT_RE.match((label or "").strip()) for label in labels
        )
        has_close = any("关闭" in (label or "") for label in labels)
        return bool(has_count and has_close)

    async def _close_panel(self) -> None:
        """收起评论区，退回视频页。**评论成功后必须收掉。**

        不收的话下一个动作（分享）会在评论区页上找「分享」按钮，
        找不到就可能点歪 —— d1 2026-11-06 的全链路 E2E 就是这么挂的。

        判据用 UI 树（见 `_panel_open_in_ui()`），不是 OCR —— 发完评论后
        OCR 判据会误判成「面板没开」。关完再用同一判据复核一次。
        软键盘弹着时第一下 back 只收键盘，所以要看一眼再按。
        """
        for _ in range(4):
            nodes = await self._nodes()
            labels = [
                (getattr(node, "label", "") or "").strip()
                for node in nodes
                if (getattr(node, "label", "") or "").strip()
            ]
            if labels:
                if not self._panel_open_in_ui(labels):
                    return
            else:
                # UI 树读不到时退 OCR 判据；两个都读不到就别按了，
                # 蒙着头按 back 会把抖音退出去。
                try:
                    lines, blob = await self._look_ocr()
                except CommentError:
                    log.warning("评论：收面板时读不到屏幕，不再按 back（避免退出抖音）")
                    return
                if not self._panel_open_from(lines, blob, await self._size_cached()):
                    return
            await self.hands.back()
            await asyncio.sleep(1.2)
            log.info("评论：按 back 收评论区/键盘")

        labels = await self._labels()
        if self._panel_open_in_ui(labels):
            log.warning("评论：评论区没收掉（UI 树里还是面板态），分享链路可能被拖挂")

    # ------------------------------------------------------------------ #
    # 入口

    async def comment(self, text: str, *, dry_run: bool = False) -> CommentResult:
        """在当前这条视频下发一条评论。

        `dry_run=True` 会走到「文字已经打进去」就停下，不点发送 ——
        用来验证 UI 链路。调用方给的是纯文本，本模块不生成内容。
        """
        steps: List[str] = []
        clean = " ".join((text or "").split())
        if not clean:
            return CommentResult(False, clean, "评论内容是空的")
        if len(clean) > _MAX_CHARS:
            log.info("评论：内容 %d 字，截到 %d 字", len(clean), _MAX_CHARS)
            clean = clean[:_MAX_CHARS]

        # 内容先过一遍。不先查的话，打进输入框之后「整屏危险词检查」会命中
        # 自己打的字，报错会指向「屏幕上出现了危险词」—— 查半天才知道是自己写的。
        hit = danger_hit(clean)
        if hit:
            return CommentResult(False, clean, f"评论内容里有「{hit}」，不评论")
        for word in _HARD_STOP_WORDS:
            if word in clean:
                return CommentResult(False, clean, f"评论内容里有「{word}」，不评论")

        reason = self.budget_reason()
        if reason:
            return CommentResult(False, clean, reason)

        hands = self.hands
        if hands is None:
            return CommentResult(False, clean, "手机没接上，评论不了")

        panel_opened = False
        send_phase = False
        try:
            try:
                await hands.ensure_ready()
            except HandsError as exc:
                return CommentResult(False, clean, str(exc))

            pkg = await hands.current_package()
            if "aweme" not in (pkg or ""):
                return CommentResult(
                    False,
                    clean,
                    f"当前不在抖音（前台是 {pkg or '未知'}），不在别的 App 里乱点",
                )

            hands.reset_task()

            # 零点击预检：广告页/危险页当场放弃，不碰屏幕。
            await self._precheck_screen()
            steps.append("预检通过（非广告）")

            await self._stop_video()
            steps.append("确认视频停住")

            await self._open_panel()
            panel_opened = True
            steps.append("点开评论区")

            await self._ensure_ime(True)
            steps.append("切 ADBKeyboard")

            await self._focus_input()
            steps.append("点输入框")

            # 上次失败可能留下草稿（d1 那次「😆这个视角挺有意思」就是草稿 + 正文
            # 粘在一起）。先把输入框清干净，再打字、再精确校验。
            await self._clear_input()

            note = await hands.input_text(clean)
            if "没打进去" in note or "打不了" in note:
                raise CommentError(f"文字没打进去：{note}")
            labels = await self._verify_typed(clean)
            baseline_count = self._comment_count(labels)
            log.info(
                "评论：文字已进输入框；发送前评论数=%s",
                baseline_count if baseline_count >= 0 else "读不到",
            )
            steps.append(f"输入「{clean[:12]}」")

            if dry_run:
                steps.append("dry_run 停在输入完成，没点发送")
                log.info("评论 dry_run：停在输入完成，没点发送")
                return CommentResult(True, clean, "", steps)

            # 输入完成 → 切回原输入法，让软键盘和「发送」按钮回来。
            # d1 实测：ADBKeyboard（无软键盘）布局下评论区根本没有发送按钮，
            # 只有拼音软键盘展开态才有。所以这一步不能省，也不能提前。
            await self._restore_ime_for_send()
            steps.append("切回输入法（软键盘回来）")

            # 白屏兜底：d1 2026-11-06 实测表情面板偶发展开成整屏白、持续 8 秒以上。
            # 先等屏幕可读，别把偶发白屏当成「找不到发送」直接判失败。
            readable = await self._wait_screen_readable()
            if not readable:
                log.info(
                    "评论：切回输入法后屏幕一直读不到（表情面板白屏），先按一次 back 收掉"
                )

            sent_tap = await self._tap_send() if readable else False
            if not sent_tap:
                if not readable:
                    # 兜底一：back 一次收掉表情面板，再给它 4 秒恢复可读。
                    await self._dismiss_overlay()
                    readable = await self._wait_screen_readable(timeout=4.0)
                # 兜底二：点一下自己的输入框重新聚焦，把键盘/发送按钮带回来。
                # 键盘没弹（mInputShown=false）时发送按钮也可能已经在，先试再点。
                if readable and await self._refocus_input():
                    sent_tap = await self._tap_send()
            if not sent_tap:
                raise CommentError(
                    "输入完成了，但屏幕上找不到「发送」按钮 —— 无软键盘布局下它就是没有，"
                    "按比例盲点会点到评论列表。放弃这条，不赌。"
                )
            # 到这儿那一下已经点下去了，之后失败才算「不确定发没发出去」。
            send_phase = True
            steps.append("点发送")

            evidence = await self._wait_sent(clean, baseline_count)
            steps.append(evidence)

            self._mark_sent(clean)
            log.info("评论已发出：%s（%s）", clean, evidence)
            return CommentResult(True, clean, "", steps)

        except CommentError as exc:
            log.warning("评论失败：%s", exc)
            self._mark_failed(str(exc), unsure=send_phase)
            return CommentResult(False, clean, str(exc), steps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("评论时发生未预期异常")
            self._mark_failed(f"{type(exc).__name__}: {exc}", unsure=send_phase)
            return CommentResult(False, clean, f"{type(exc).__name__}: {exc}", steps)
        finally:
            # 收尾两件事，哪件失败都不能盖住上面的结果。
            try:
                await self._ensure_ime(False)
            except Exception as exc:  # noqa: BLE001
                log.warning("评论：切回输入法失败（%s）", exc)
            if panel_opened:
                try:
                    await self._close_panel()
                except Exception as exc:  # noqa: BLE001
                    log.warning("评论：收评论区失败（%s）", exc)
