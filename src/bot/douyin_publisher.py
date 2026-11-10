"""把一条视频发到抖音。

## 这个模块存在的理由

桃桃是**独立存在的人**，发视频不需要人批准（2026-10-24 用户明确说了）。
所以「发布」不能是一个等人点按钮的半成品流程，必须是一条能自己跑完的链路。

之前这条路卡在「选视频」上，根因是 MediaStore 记录的 `is_pending=1`：
`adb push` 进去的文件只有 shell 能看见，抖音查相册直接过滤掉，
界面上显示「暂无相应的内容」。`content query` 查得到 ≠ 其他 App 看得见。
修法见 `_fix_pending()`。

## 为什么这么多坐标

抖音的发布流程里，**三个页面读不到 UI 树**：

- 拍摄页的「+」：底部导航栏只有图标，没有文字
- 预览页 / 编辑页：视频在播，uiautomator 永远拿不到 idle，
  `uiautomator dump` 直接不写文件
- 相册的视频网格：缩略图上只有时长 `00:05`，没有文件名

这些地方只能按坐标点。坐标**一律用比例存**，不要写死像素 ——
换一台分辨率不同的手机就全错位了。

标题页反而是完整的（静止页面），能用 `tap_text` 就别用坐标。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """发布链路上任何一步没走通。"""


@dataclass
class PublishResult:
    ok: bool
    title: str = ""
    desc: str = ""
    error: str = ""
    steps: List[str] = field(default_factory=list)
    # 结果不确定：点了「发作品」，但现场证据和主页复核都判不了发没发出去。
    # scheduler 会把这种结果**不重试、不记 failed，但照样占当天额度** ——
    # 宁可少发一条，不可重复发。
    unsure: bool = False

    def line(self) -> str:
        if self.ok:
            return f"（发出去了：{self.title}）"
        if self.unsure:
            return f"（不确定发没发出去：{self.error}）"
        return f"（没发成：{self.error}）"


# 界面上出现这些词，说明发布流程已经不在正常路径上，停手。
# 不是安全红线（那是 hands.DANGER_WORDS 的事），是「这条路走歪了」。
_ABORT_HINTS = (
    "账号异常", "验证码", "实名", "封禁", "违规", "限制发布", "审核不通过",
)

# 发布成功后能读到的特征。
_SUCCESS_HINTS = ("发布成功", "作品已发布", "已发布")

# 个人主页的标志：这三样同时出现才算「真的到「我」页了」，否则后面
# 点第一条作品就是瞎点（d1 2026-11-06 实测个人页是静态页，dump 可读）。
PROFILE_MARKERS = ("获赞", "作品", "私密作品")

# --------------------------------------------------------------------------- #
# 相册清理 / 校验（2026-11-07 发错文件事故之后加的硬闸）
#
# 事故：模拟器相册里堆着 6 条旧测试视频，发布器按固定坐标点相册**第一格**，
# 结果把 10/28 的旧视频当成今天的成片发了出去 —— 新生成的 20.27 秒那条
# 反而留在相册里。所以发布前必须把相册清成「只剩这一条待发」并校验。
#
# 要清的两种文件：
# - `taotao_*.mp4`：发布流水线自己 push 的（push_video 生成的名字）；
# - `TG-日期-时间.mp4`：**抖音发布成功后会自己往 DCIM/Camera 存一份本地副本**
#   （d1 2026-11-07 实测，20.1 秒），不清会逐日堆积、把「只剩一条」卡死。
PIPELINE_PREFIX = "taotao_"
DOUYIN_COPY_PREFIX = "TG-"

# 相册里算「视频」的扩展名。发布器选的是抖音相册的「视频」筛选，
# 所以除了 mp4 也要防 3gp/mkv 这些 —— 它们同样会挤到网格第一格。
_VIDEO_EXTS = (".mp4", ".3gp", ".mkv", ".mov", ".webm", ".avi")

# MediaStore 里视频表的地址（查询都带 where，见 _find_media_id 的教训：
# 不带 where 的全表查询会让 MediaProvider 卡死、adb 连接直接断）。
_MEDIA_VIDEO_URI = "content://media/external/video/media"

# 标点/空白，用来把标题和作品列表里的文字压成同一种形态做匹配。
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]…·—\-_]+")


def _flat_text(text: str) -> str:
    """去掉标点和空白。作品列表里的标题可能被重排/加后缀，但对字匹配要稳。"""
    return _PUNCT_RE.sub("", text or "")


def _probe_text(text: str, limit: int = 8) -> str:
    """从标题/描述里取一段用来认「这条作品」的文字。

    作品列表里的标题可能被截断（「翻到旧照片，突…」），所以取前 8 个字；
    短于 4 个字没有辨识度，返回空串（调用方只靠标题/描述认作品，不靠时间）。
    """
    flat = _flat_text(text)
    probe = flat[:limit]
    return probe if len(probe) >= 4 else ""


def _fresh_time_hit(text: str) -> str:
    """OCR 里的「刚发时间」。命中返回时间词，否则空串。

    只认三种：刚刚 / X秒前 / X分钟前且 X≤10。
    旧作品是「2天前 / X小时前」，不会被当成刚发 —— d1 2026-11-06 实测，
    旧作品播放页显示「· 2天前」，刚发的那条显示「· 2分钟前」。
    """
    flat = _flat_text(text)
    if "刚刚" in flat:
        return "刚刚"
    hit = re.search(r"(\d+)秒前", flat)
    if hit:
        return f"{hit.group(1)}秒前"
    hit = re.search(r"(\d+)分钟前", flat)
    if hit:
        try:
            minutes = int(hit.group(1))
        except ValueError:
            return ""
        if minutes <= 10:
            return f"{minutes}分钟前"
    return ""


def _brief_text(text: str, limit: int = 120) -> str:
    """把一屏 OCR 压成一行塞进日志，方便判断卡在了哪。"""
    parts = [s.strip() for s in (text or "").splitlines() if s.strip()]
    return " / ".join(parts)[:limit] or "（空）"


@dataclass
class VideoArtifact:
    """一条可以发出去的视频。

    **`first_frame` 是必填的，不是形式主义。**

    2026-10-24 用户看到一条「压根不是桃桃」的视频差点被发出去 ——
    那是一条没喂首帧的纯文生视频，seedance 自由发挥出了一个不相干的路人。

    发布环节看不到视频内容，没法验证「这是不是桃桃」。所以只能卡住
    **可验证的源头**：这条视频有没有首帧。没有首帧 = 文生视频 =
    画不出桃桃 = 不许发。

    这个字段存在，就是为了让「忘了喂首帧」在构造对象时就露馅，
    而不是等视频躺到抖音上才发现。
    """

    video: Path
    first_frame: Path
    prompt: str = ""

    def check(self) -> None:
        video = Path(self.video)
        frame = Path(self.first_frame)
        if not video.exists():
            raise PublishError(f"视频文件不存在：{video}")
        if video.stat().st_size < 10 * 1024:
            raise PublishError(f"视频文件太小，不像成片：{video}（{video.stat().st_size} 字节）")
        if not frame.exists():
            raise PublishError(f"首帧文件不存在：{frame}")


class DouyinPublisher:
    """操作她的手机（雷电模拟器，2026-11-04 起没有真机了），把视频发到抖音。

    只依赖 `PhoneHands` 的 adb 通道，不重新实现一套。
    """

    PACKAGE = "com.ss.android.ugc.aweme"
    REMOTE_DIR = "/sdcard/DCIM/Camera"

    # 坐标全部是相对屏幕宽/高的比例。换分辨率时不用改 ——
    # `_xy()` 会按实际尺寸换算。
    #
    # **2026-11-04 起没有真机了，她只有雷电模拟器**，所以固定用模拟器
    # 那套坐标（EMU_*），不再按序列号切换 —— 判定和切换本身就是个
    # 失败点。真机坐标（720x1600 OPPO 实测）已随真机一起撤掉。
    #
    # 雷电模拟器（Android 14，override 720x1600）实测 2026-10-26，
    # 手动走通整条流程，作品 5→6 条。布局和当年真机差得远：
    # - 相册页多了「AI形象」「添加可访问照片」，缩略图被挤到中间
    # - 底部按钮整体更靠下（y 比例大约多 0.06~0.07）
    # - 「下一步」在预览页和编辑页是**同一个位置**
    RATIO_BOTTOM_PLUS = (0.514, 0.939)    # 底部导航「+」（和真机当年一样）
    RATIO_ALBUM_FIRST = (0.506, 0.350)    # 相册视频网格第一格（缩略图在中间）
    RATIO_NEXT_PREVIEW = (0.646, 0.9625)  # 预览页「下一步」
    RATIO_NEXT_EDIT = (0.646, 0.9625)     # 编辑页「下一步」（和预览页同位置）
    RATIO_PUBLISH = (0.778, 0.959)        # 标题页「发作品」
    # 底部导航「我」。d1 2026-11-06 实测：节点 bounds [630,1525,664,1575]，
    # center ≈ (647,1550)，ratio (0.90, 0.966)。
    # **只在 UI 树读不到时才用** —— 底部导航是静态 chrome，不依赖 dump，
    # 视频在播导致 uiautomator 拿不到 idle 时也能点。
    RATIO_ME_TAB = (0.90, 0.966)

    # ---- AI 生成标注 ---------------------------------------------------
    #
    # 「作品声明」入口。抖音不同版本叫法不一样，按顺序试，第一个命中的就用。
    #
    # 2026-10-30 在模拟器（抖音 40.8.0 / Android 14）实测：
    # 标题页下半屏那一行叫 **「添加自主声明」**，前面几个名字都不存在。
    # 它在第一屏看不到，要往下滑一屏 —— `_mark_ai()` 里有滚动兜底。
    # 点开后的弹层里选项叫「内容由AI生成」，确认按钮是「保存」。
    #
    # 官方规范写的路径是「作品发布页 - 添加自主声明 - 内容由AI生成」，
    # 和实测一致。但版本一改措辞就变，所以还是按一串候选名试，不写死。
    _DECLARE_ENTRY = (
        "添加自主声明", "作品声明", "添加作品声明", "内容声明", "自主声明",
        "高级设置",
    )

    # 声明弹层里 AI 那一项。**必须选到它** —— 这是合规要求，不是可选项。
    _AI_OPTIONS = (
        "内容由AI生成", "内容由 AI 生成", "内容由人工智能生成",
        "AI生成内容", "AI 生成内容", "AI生成", "AI 生成", "人工智能生成",
    )
    # 弹层的确认按钮。实测是「保存」，其他写法留着兼容别的版本。
    _CONFIRM_WORDS = ("保存", "确定", "完成", "确认")

    def __init__(
        self,
        hands: Any,
        ai_label: str = "strict",
        ai_label_text: str = "本视频由 AI 生成",
    ) -> None:
        self.hands = hands
        self._size: Optional[tuple] = None
        # "strict" = UI 声明必须选到 AI 项，否则不发
        # "desc"   = 只在描述里加文字
        # "off"    = 不标
        self.ai_label = (ai_label or "strict").strip().lower()
        self.ai_label_text = (ai_label_text or "").strip()

        # 坐标固定用模拟器那套（见类常量处的说明：2026-11-04 起没有真机）。
        # 这里不再做序列号判定和切换 —— 少一个分支就少一个点错坐标的机会。
        log.info("发布：使用模拟器坐标（只有模拟器，没有真机）")

    # ------------------------------------------------------------------ #
    # 基础

    async def _size_cached(self) -> tuple:
        if self._size is None:
            self._size = await self.hands.screen_size()
        return self._size

    async def _xy(self, ratio: tuple) -> tuple:
        """比例 → 像素。夹在屏幕内，防止算出屏幕外的坐标。"""
        w, h = await self._size_cached()
        x = int(round(ratio[0] * w))
        y = int(round(ratio[1] * h))
        x = max(1, min(w - 2, x))
        y = max(1, min(h - 2, y))
        return x, y

    async def _tap_ratio(self, ratio: tuple, what: str) -> None:
        x, y = await self._xy(ratio)
        await self.hands.tap_xy(x, y)
        log.debug("点%s @(%d,%d)", what, x, y)

    async def _ensure(self, what: str = "") -> None:
        """每步之前确认手机还在。掉了自动重连，重连不上才抛。

        为什么每步都查：adb 在 **push 大文件之后特别容易掉** ——
        2026-10-25 和 10-26 两次发布失败都是同一个模式：
        `已推到手机` 之后 20 秒内变成 offline。只在开头查一次的话，
        后面每一步都会「找不到按钮」，报出来的错误指向完全错误的环节。
        """
        try:
            await self.hands.ensure_ready()
        except Exception as exc:  # noqa: BLE001
            where = f"（{what}）" if what else ""
            raise PublishError(f"手机掉线了{where}：{exc}") from exc

    async def _labels(self) -> List[str]:
        """读当前屏幕的文字标签。掉线时**先重连再重试一次**。

        空列表有两种原因，必须分开：
        ① 这屏是视频/播放页，UI 树天生读不到 —— 正常，返回空继续走
        ② 手机掉线了 —— 后面每一步都会「找不到」，重连后还不行就得报错
        """
        for attempt in (1, 2):
            nodes = await self.hands.ui()
            labels = [n.label for n in nodes if n.label]
            if labels:
                return labels

            err = getattr(self.hands, "_last_adb_error", "")
            if not err:
                # 没报错也没节点 = 视频页，正常情况
                return labels

            if attempt == 1:
                log.info("发布：读不到屏幕（%s），重连后重试一次", err)
                try:
                    await self.hands.ensure_ready()
                except Exception:  # noqa: BLE001
                    pass
                continue

            raise PublishError(
                f"读不到屏幕：adb 报「{err}」—— 手机掉线了，不是界面变了"
            )
        return []

    def _check_abort(self, labels: Sequence[str]) -> None:
        blob = " ".join(labels)
        for word in _ABORT_HINTS:
            if word in blob:
                raise PublishError(f"界面上出现「{word}」，这条路走不通了，停手")

    @staticmethod
    def _brief(labels: Sequence[str], limit: int = 14) -> str:
        """把屏幕上的文字压成一行，塞进报错信息里。

        报「找不到作品声明」时必须附上这屏到底有什么 ——
        否则没法判断是界面改版、还是卡在了别的页面。
        """
        parts = [s.strip() for s in labels if s and s.strip()]
        return " / ".join(parts[:limit])[:240] or "（空）"

    # ------------------------------------------------------------------ #
    # AI 生成标注

    async def _mark_ai(self, steps: List[str]) -> None:
        """在「作品声明」里勾上「内容由 AI 生成」。

        **这是合规动作，不是优化项。** 抖音对 AI 生成内容有强制声明要求，
        没声明的会被限流甚至下架；国内《人工智能生成合成内容标识办法》
        也要求显式标识。所以 `ai_label="strict"` 时选不到就直接不发。

        为什么按文字点、不用坐标：标题页和声明弹层都是静态列表，
        UI 树能读到文字（不像预览/编辑页那样视频在播拿不到 idle）。
        文字比坐标抗改版。

        为什么描述里还要再加一句：平台声明是挂在抖音那条记录上的，
        视频被人下载、转发、搬到别的平台时声明带不走，文字会跟着走。
        两层都做才叫「标住了」。
        """
        # --- 1. 找到并点开「作品声明」------------------------------
        # 入口常在标题页下半屏，第一屏可能没露出来 —— 找不到就往下翻一屏再找。
        opened = ""
        for attempt in (1, 2):
            for word in self._DECLARE_ENTRY:
                r = await self.hands.tap_text(word)
                if "找不到" in r or "读不到" in r:
                    continue
                opened = word
                break
            if opened:
                break
            if attempt == 1:
                log.info("发布：第一屏没看到声明入口，往下翻一屏再找")
                await self.hands.swipe("up")
                await asyncio.sleep(2)

        if not opened:
            labels = await self._labels()
            raise PublishError(
                "标题页上找不到「作品声明」入口，没法标注 AI 生成。"
                f"这屏的文字：{self._brief(labels)}"
            )
        log.info("发布：点开了「%s」", opened)
        await asyncio.sleep(2.5)
        steps.append(f"点开{opened}")

        # --- 2. 在弹层里选 AI 那一项 --------------------------------
        picked = ""
        for word in self._AI_OPTIONS:
            r = await self.hands.tap_text(word)
            if "找不到" in r or "读不到" in r:
                continue
            picked = word
            break

        if not picked:
            labels = await self._labels()
            # 弹层还开着就退出来，别把发布页留在半截状态
            await self._close_sheet(labels)
            raise PublishError(
                "作品声明里没有 AI 生成那一项。"
                f"弹层的文字：{self._brief(labels)}"
            )
        log.info("发布：已勾选「%s」", picked)
        await asyncio.sleep(2)
        steps.append(f"声明「{picked}」")

        # --- 3. 关掉弹层 -------------------------------------------
        # 有的版本点选项就自动关，有的要按「确定」。
        for word in self._CONFIRM_WORDS:
            r = await self.hands.tap_text(word)
            if "找不到" not in r and "读不到" not in r:
                log.info("发布：声明弹层已确认（点了「%s」）", word)
                await asyncio.sleep(2)
                break
        else:
            await self._close_sheet(await self._labels())

        # --- 4. 确认真的回到标题页了 --------------------------------
        # 这一步不能省：声明没生效就点发作品，等于发了一条没标注的 AI 视频。
        await asyncio.sleep(1.5)
        labels = await self._labels()
        if not any("发作品" in s for s in labels):
            raise PublishError(
                "选完声明后没回到标题页（找不到「发作品」）。"
                f"这屏的文字：{self._brief(labels)}"
            )

    async def _close_sheet(self, labels: Sequence[str]) -> None:
        """关掉声明弹层，但**只在它确实还开着的时候**关。

        盲按 back 有风险：弹层已经关了的话，back 会直接退出发布页，
        前面填的标题描述全丢。

        判据用「发作品」在不在 —— 它是标题页的固定元素，
        任何盖在上面的弹层/面板都会把它挡住（UI 树读不到）。
        比「看到某个选项文字」更通用：高级设置面板里没有 AI 选项文字，
        但它照样需要被关掉。
        """
        if any("发作品" in s for s in labels):
            return
        await self.hands._adb("shell", "input keyevent 4")
        await asyncio.sleep(1.5)
        log.info("发布：按 back 收掉盖在上面的面板")

    # ------------------------------------------------------------------ #
    # 送视频上手机

    async def _ensure_media_permission(self) -> None:
        """确保抖音能看见**整个**相册，不是只有用户挑的那几张。

        Android 14 的「部分照片访问」（READ_MEDIA_VISUAL_USER_SELECTED）
        是 2026-10-30 卡住一整轮的原因，而且症状极具误导性：

        ```
        相册页：「暂无相应的内容」
        MediaStore：_id=1000000080, is_pending=0, _size=15561645   ← 完全正常
        dumpsys：READ_MEDIA_VISUAL_USER_SELECTED granted=true       ← 看着有权限
                  READ_MEDIA_VIDEO / READ_MEDIA_IMAGES  granted=false ← 真凶
        ```

        权限面板上显示「已允许」，但那是「只能看用户选中的照片」。
        `adb push` 进去的新文件不在选中列表里，抖音就是看不见 ——
        很容易误判成 `is_pending` 又没修掉，白查一圈。

        修法：补授两个完整权限。授完必须 force-stop，权限变更才会生效。
        """
        want = ("READ_MEDIA_VIDEO", "READ_MEDIA_IMAGES")
        code, out = await self.hands._adb(
            "shell",
            f"dumpsys package {self.PACKAGE} | grep -E 'READ_MEDIA_VIDEO:|READ_MEDIA_IMAGES:'",
            timeout=20.0,
        )
        blob = out or ""
        if not blob:
            # 读不到权限信息就别瞎授，继续走 —— 真看不到相册会在后面报出来
            log.debug("发布：读不到抖音的权限状态，跳过检查")
            return

        missing = [p for p in want if f"{p}: granted=true" not in blob]
        if not missing:
            return

        log.info("发布：抖音只有部分相册权限，补授 %s", ", ".join(missing))
        for perm in missing:
            await self.hands._adb(
                "shell",
                f"pm grant {self.PACKAGE} android.permission.{perm}",
                timeout=20.0,
            )
        # 权限变更不重启 App 不生效
        await self.hands._adb("shell", f"am force-stop {self.PACKAGE}")
        await asyncio.sleep(1.5)

    async def push_video(self, local: Path) -> str:
        """把视频推到手机相册，并**修掉 is_pending**。

        不修这一步，抖音相册里就是空的 —— 这是 2026-10-24 踩了一整轮
        才找到的根因，别删。
        """
        local = Path(local)
        if not local.exists():
            raise PublishError(f"本地没有这个文件：{local}")
        if local.stat().st_size < 1024:
            raise PublishError(f"文件太小，不像视频：{local}（{local.stat().st_size} 字节）")

        # 文件名带时间戳：抖音相册按时间倒序，最新的排第一格。
        name = f"taotao_{int(time.time())}.mp4"
        remote = f"{self.REMOTE_DIR}/{name}"

        code, out = await self.hands._adb(
            "push", str(local).replace("\\", "/"), remote, timeout=180.0
        )
        if code != 0:
            raise PublishError(f"推文件失败：{(out or '').strip()[:120]}")
        log.info("已推到手机：%s", remote)

        # MediaStore 里可能还没有这条记录，等它入库。
        media_id = ""
        for _ in range(10):
            media_id = await self._find_media_id(name)
            if media_id:
                break
            await asyncio.sleep(1.0)
        if not media_id:
            raise PublishError(f"MediaStore 里找不到 {name}，视频没真正入库")

        await self._fix_pending(media_id)

        # **push 完不要主动 reconnect。** 实测 `adb reconnect offline`
        # 会让设备短暂从列表消失（`device not found`），反而把好好的连接搞断。
        # 只需要等一下让 USB 缓冲区排空，然后轻量确认一次 ——
        # 真掉了的话 `_ensure` 里那套「重连 + 轮询等回来」会救。
        await asyncio.sleep(2.0)
        await self._ensure("推完视频")

        return name

    async def _find_media_id(self, display_name: str) -> str:
        """按文件名查 MediaStore 的 _id。

        **`--where` 不能省。** 这是 2026-10-26 挖了很久才找到的根因：

        不带 where 的 `content query` 会让 MediaProvider 遍历**全库**媒体记录
        （这台手机相册里有几千张照片）。查询期间手机卡住，adb shell 阻塞到超时，
        连接直接断成 `device not found` / `offline`。
        表现是「push 完就掉线」，害得我以为是 USB 供电问题查了半天线。

        带 where 只命中一条，耗时从「卡死」变成毫秒级。
        """
        # display_name 是我们自己生成的时间戳文件名，没有引号风险。
        code, out = await self.hands._adb(
            "shell",
            "content query --uri content://media/external/video/media "
            "--projection _id:_display_name "
            f'--where "_display_name=\'{display_name}\'"',
            timeout=15.0,
        )
        if code != 0 or not out:
            return ""
        for line in out.splitlines():
            if display_name not in line:
                continue
            m = re.search(r"_id=(\d+)", line)
            if m:
                return m.group(1)
        return ""

    async def _fix_pending(self, media_id: str) -> None:
        """把 is_pending 置 0，让别的 App 能看见这个文件。

        Android 10+ scoped storage：pending 文件只有创建者（shell）可读。
        置 0 之后 MediaProvider 会自动回填 _size / duration / width / height。
        """
        await self.hands._adb(
            "shell",
            f"content update --uri content://media/external/video/media/{media_id} "
            f"--bind is_pending:i:0",
            timeout=15.0,
        )
        await asyncio.sleep(1.5)

        # 验证：_size 还是 NULL 就说明没生效，别装作成功。
        # 同样必须带 where 限定到这一条 —— 全表查询会把手机卡死（见 _find_media_id）。
        code, out = await self.hands._adb(
            "shell",
            "content query --uri content://media/external/video/media "
            "--projection _id:_size:duration:is_pending "
            f'--where "_id={media_id}"',
            timeout=15.0,
        )
        for line in (out or "").splitlines():
            if f"_id={media_id}," in line or f"_id={media_id} " in line:
                if "_size=NULL" in line:
                    raise PublishError("is_pending 改了但元数据还是空的，抖音看不到")
                log.info("MediaStore 记录已就绪：%s", line.strip())
                return
        raise PublishError(f"改完 is_pending 后查不到 _id={media_id} 的记录")

    # ------------------------------------------------------------------ #
    # 相册清理 / 校验（防「发错文件」）
    #
    # 2026-11-07 事故：模拟器相册里堆着 6 条旧测试视频，发布器按固定坐标点
    # **第一格**，结果把 10/28 的旧视频当成今天的成片发了出去，今天新生成的
    # 20.27 秒那条反而留在相册里。所以现在的顺序是：
    # push 前清掉流水线自己的旧视频 → push → 校验相册/MediaStore 里只剩这一条
    # 且名字/大小/时长都对得上 → 才允许点缩略图；任何一步不对就拒绝发布。

    @staticmethod
    def _is_pipeline_file(name: str) -> bool:
        """这个文件是不是发布流水线自己产生的（可以安全删）。

        两种：我们 push 的 `taotao_*`；抖音发布成功后**自己存进相册的
        `TG-日期-时间.mp4` 本地副本**（d1 2026-11-07 实测，20.1 秒）。
        """
        text = name or ""
        return text.startswith(PIPELINE_PREFIX) or text.upper().startswith(
            DOUYIN_COPY_PREFIX
        )

    async def _album_video_files(self) -> List[str]:
        """`ls DCIM/Camera` 里的视频文件名。

        直接看文件系统是为了抓到还没被 MediaStore 收录的文件 ——
        只信 MediaStore 的话，刚 push 的新文件可能还没入库。
        """
        code, out = await self.hands._adb(
            "shell", f"ls -1 {self.REMOTE_DIR}", timeout=15.0
        )
        names: List[str] = []
        for line in (out or "").splitlines():
            item = line.strip()
            if not item or "/" in item:
                continue
            if item.lower().endswith(_VIDEO_EXTS):
                names.append(item)
        return names

    async def _album_media_rows(self) -> List[Dict[str, str]]:
        """MediaStore 里 `DCIM/Camera` 的视频记录。

        **必须带 `--where` 限定目录**：不带 where 的全表查询会让 MediaProvider
        遍历全库、卡死到 adb 超时（见 `_find_media_id` 的教训）。
        """
        code, out = await self.hands._adb(
            "shell",
            f"content query --uri {_MEDIA_VIDEO_URI} "
            "--projection _id:_display_name:_data:_size:duration:is_pending "
            "--where \"_data LIKE '%/DCIM/Camera/%'\"",
            timeout=20.0,
        )
        rows: List[Dict[str, str]] = []
        if code != 0 or not out:
            return rows
        for line in out.splitlines():
            if not line.startswith("Row:"):
                continue
            row: Dict[str, str] = {}
            for hit in re.finditer(r"(\w+)=([^,]+)", line):
                row[hit.group(1)] = hit.group(2).strip()
            if row.get("_id"):
                rows.append(row)
        return rows

    async def _delete_media_row(self, media_id: str) -> None:
        if not media_id:
            return
        await self.hands._adb(
            "shell",
            f"content delete --uri {_MEDIA_VIDEO_URI} --where \"_id={media_id}\"",
            timeout=15.0,
        )

    @staticmethod
    def _mp4_duration_ms(path: Path) -> int:
        """读本地 mp4 的时长（毫秒），用来和 MediaStore 的 duration 对账。

        这台机器没有 ffprobe，发布器也不该依赖 ffmpeg，所以直接解析 mp4 的
        `moov/mvhd`（纯 Python）。读不到返回 0，调用方退化成只对大小。
        """
        def _find(fh: Any, start: int, end: int, target: bytes):
            pos = start
            while pos + 8 <= end:
                fh.seek(pos)
                head = fh.read(8)
                if len(head) < 8:
                    return None
                size = int.from_bytes(head[:4], "big")
                typ = head[4:8]
                header = 8
                if size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        return None
                    size = int.from_bytes(ext, "big")
                    header = 16
                elif size == 0:
                    size = end - pos
                if size < header or pos + size > end:
                    return None
                if typ == target:
                    return pos + header, pos + size
                if typ in (b"moov", b"trak", b"mdia"):
                    found = _find(fh, pos + header, pos + size, target)
                    if found:
                        return found
                pos += size
            return None

        try:
            path = Path(path)
            total = path.stat().st_size
            with open(path, "rb") as fh:
                hit = _find(fh, 0, total, b"mvhd")
                if not hit:
                    return 0
                start, end = hit
                fh.seek(start)
                data = fh.read(min(end - start, 256))
                if len(data) < 20:
                    return 0
                version = data[0]
                if version == 0:
                    timescale = int.from_bytes(data[12:16], "big")
                    duration = int.from_bytes(data[16:20], "big")
                else:
                    if len(data) < 32:
                        return 0
                    timescale = int.from_bytes(data[20:24], "big")
                    duration = int.from_bytes(data[24:32], "big")
                if timescale <= 0:
                    return 0
                return int(round(duration / timescale * 1000))
        except Exception as exc:  # noqa: BLE001
            log.debug("发布：读不到本地视频时长（%s）", exc)
            return 0

    async def _clear_old_pipeline_videos(self) -> None:
        """push 前清掉发布流水线自己的旧视频，让相册只剩待发这一条。

        只删两种前缀的文件（`taotao_*` + 抖音自动存的 `TG-*` 副本），
        **别人的视频不替用户删**；清完还有残留就抛错拒绝发布。
        """
        rows = await self._album_media_rows()
        own_rows = []
        for row in rows:
            row_name = (row.get("_display_name") or "").strip() or (
                (row.get("_data") or "").rsplit("/", 1)[-1]
            )
            if self._is_pipeline_file(row_name):
                own_rows.append((row_name, row))
        for row_name, row in own_rows:
            await self._delete_media_row(row.get("_id") or "")
            path = (row.get("_data") or "").strip() or f"{self.REMOTE_DIR}/{row_name}"
            await self.hands._adb("shell", f'rm -f "{path}"', timeout=15.0)
            log.info("发布：清掉旧流水线视频 %s", row_name)

        # 文件系统里再扫一遍（MediaStore 可能漏掉还没入库的文件）。
        for name in await self._album_video_files():
            if not self._is_pipeline_file(name):
                continue
            await self.hands._adb(
                "shell", f'rm -f "{self.REMOTE_DIR}/{name}"', timeout=15.0
            )
            log.info("发布：清掉旧流水线视频（文件）%s", name)

        left = [
            name
            for name in await self._album_video_files()
            if self._is_pipeline_file(name)
        ]
        if left:
            raise PublishError(
                "相册里的旧流水线视频清不掉（还在："
                f"{'、'.join(left[:5])}），发布器会点错第一格，先手工清"
            )
        log.info("发布：相册清理完成（清掉 %d 条流水线旧视频）", len(own_rows))

    async def _other_dirs_video_files(self) -> List[str]:
        """其它常见目录里的视频文件。

        抖音相册读的是全设备 MediaStore，`DCIM/Camera` 之外有视频一样会
        出现在「视频」筛选里、挤到第一格。这里只 `ls` 几个常见目录
        （不做全表查询 —— 那会把 adb 卡死）。
        """
        found: List[str] = []
        for folder in ("/sdcard/Movies", "/sdcard/Download", "/sdcard/DCIM"):
            code, out = await self.hands._adb("shell", f"ls -1 {folder}", timeout=10.0)
            for line in (out or "").splitlines():
                item = line.strip()
                if not item or "/" in item:
                    continue
                if item.lower().endswith(_VIDEO_EXTS):
                    found.append(f"{folder}/{item}")
        return found

    async def _verify_single_pending_video(self, name: str, local: Path) -> None:
        """push 后硬校验：相册里**只有这一条**待发视频，且名字/大小/时长都对。

        发布器按固定坐标点「相册第一格」，相册里只要还有别的视频就可能发错。
        任何一条不满足就抛错，**绝不盲点第一格**。
        """
        local = Path(local)

        files = await self._album_video_files()
        others = [item for item in files if item != name]
        if others:
            raise PublishError(
                "相册里还有别的视频，发布器会盲点第一格、可能发错文件："
                + "、".join(others[:5])
                + ("…" if len(others) > 5 else "")
                + "（先清掉再发）"
            )
        if name not in files:
            raise PublishError(f"相册里没看到刚推上去的 {name}，不能选缩略图")

        rows = await self._album_media_rows()
        if len(rows) != 1:
            names = [
                (row.get("_display_name") or row.get("_data") or "?").strip()
                for row in rows
            ]
            raise PublishError(
                f"MediaStore 里相册视频有 {len(rows)} 条（{'、'.join(names[:5])}），"
                "不是唯一一条，拒绝盲点第一格"
            )
        row = rows[0]
        row_name = (row.get("_display_name") or "").strip()
        row_path = (row.get("_data") or "").strip()
        if row_name != name or not row_path.endswith(name):
            raise PublishError(
                f"MediaStore 唯一那条不是刚推的 {name}"
                f"（实际 {row_name or row_path}），拒绝发布"
            )
        if str(row.get("is_pending") or "").strip() not in ("0", ""):
            raise PublishError(
                f"待发视频还没入库完毕（is_pending={row.get('is_pending')}）"
            )

        expected_size = local.stat().st_size
        size_raw = str(row.get("_size") or "").strip()
        if size_raw and size_raw.upper() != "NULL":
            try:
                actual_size = int(size_raw)
            except ValueError:
                actual_size = -1
            if actual_size != expected_size:
                raise PublishError(
                    "相册里那条视频大小对不上（MediaStore "
                    f"{actual_size} ≠ 本地 {expected_size}），可能不是这条，拒绝发布"
                )

        expected_ms = self._mp4_duration_ms(local)
        try:
            actual_ms = int(str(row.get("duration") or "").strip())
        except ValueError:
            actual_ms = 0
        if expected_ms and actual_ms:
            if abs(actual_ms - expected_ms) > 1500:
                raise PublishError(
                    "相册里那条视频时长对不上（MediaStore "
                    f"{actual_ms}ms ≠ 本地 {expected_ms}ms），可能不是这条，拒绝发布"
                )
        else:
            log.info(
                "发布：时长对账跳过（media=%s ms / local=%s ms），已用大小兜底",
                actual_ms or "?", expected_ms or "?",
            )

        other_dirs = await self._other_dirs_video_files()
        if other_dirs:
            raise PublishError(
                "手机其它目录还有视频，抖音相册会把它们列进来："
                + "、".join(other_dirs[:5])
                + "（先清掉再发）"
            )

        log.info(
            "发布：相册校验通过 —— 只有这一条待发视频（%s，%.2fs，%d 字节）",
            name,
            (expected_ms or actual_ms) / 1000.0,
            expected_size,
        )

    # ------------------------------------------------------------------ #
    # 发布

    async def publish(
        self,
        artifact: VideoArtifact,
        title: str,
        desc: str,
        topics: Sequence[str] = (),
        dry_run: bool = False,
    ) -> PublishResult:
        """从「视频在本地」开始，一路走到「发作品」。

        `dry_run=True` 会走到标题页就停下（内容都填好，但不点发作品），
        用来验证 UI 链路而不真的发东西出去。
        """
        steps: List[str] = []
        title = (title or "").strip()
        desc = (desc or "").strip()
        if not title and not desc:
            raise PublishError("标题和描述都是空的，发什么")

        # AI 标注第一层：描述文字。
        #
        # UI 上的「作品声明」才是主手段（见 `_mark_ai`），这句是兜底 ——
        # 视频被下载、转发、搬到别的平台时，抖音那条声明带不走，文字会跟着走。
        #
        # 用空格连接，不用换行：ADBKeyBoard 的 `input text` 打不进换行，
        # 硬塞 \n 会变成一串乱码或者直接截断。
        if self.ai_label != "off" and self.ai_label_text:
            if self.ai_label_text not in desc:
                desc = f"{desc} {self.ai_label_text}".strip()

        # 首帧缺失在这一步就炸，不要等到视频躺到抖音上才发现不是桃桃。
        artifact.check()

        try:
            # 发布前先确认手机在。掉线时后面每一步都会「找不到」，
            # 报出来的错误会指向**错误的环节** —— 2026-10-25 就栽过一次：
            # adb 掉成 `device still authorizing`，dump 全空，
            # 最后报「相册页上找不到『视频』筛选」，看着像抖音改版。
            await self._ensure("发布前")

            # --- 0. 上手机 -------------------------------------------
            # 先确认抖音看得见整个相册 —— 只有「部分照片访问」权限时，
            # push 进去的文件它读不到，相册显示「暂无相应的内容」。
            await self._ensure_media_permission()
            # 2026-11-07 事故：相册里的旧测试视频没清，发布器盲点第一格，
            # 把旧视频发了出去。push 前先把流水线自己的旧视频清干净
            # （`taotao_*` + 抖音自动存的 `TG-*` 副本）。
            await self._clear_old_pipeline_videos()
            name = await self.push_video(artifact.video)
            steps.append(f"推送 {Path(artifact.video).name}")
            # push 后硬校验：相册里只剩这一条，且名字/大小/时长都对得上。
            # 不满足就抛错 —— **绝不盲点第一格缩略图**。
            await self._verify_single_pending_video(name, artifact.video)
            steps.append("相册校验：只有这一条待发视频")

            # **push 大文件之后 adb 最容易掉**（两次失败都是这个模式）。
            # 进抖音之前必须再确认一次，否则后面全白做。
            await self._ensure("推完视频")

            # --- 1. 进相册 -------------------------------------------
            # 必须先 force-stop：抖音会缓存相册列表，不重启就看不到新视频。
            await self.hands._adb("shell", f"am force-stop {self.PACKAGE}")
            await asyncio.sleep(2)
            await self.hands.open_app("aweme")
            await asyncio.sleep(9)
            steps.append("打开抖音")

            labels = await self._labels()
            self._check_abort(labels)

            # 点「+」进拍摄页。
            #
            # **不要靠 UI 树判断「现在是不是首页」。** 2026-10-30 实测：
            # 首页信息流在播视频时 uiautomator 拿不到 idle，节点直接是空的，
            # 「读不到『推荐』= 不在首页」这个判据会把好好的首页误判成播放页，
            # 然后连按几次 back 把抖音退出去了。
            #
            # 改成「点完看结果」：能读到「相册」就算进了拍摄页，否则退一层重来。
            # force-stop 之后重开本来就是首页，需要重试的只有两种情况：
            #   ① 抖音自己弹的浮层（作品数据、活动弹窗）
            #   ② 上次发布失败留下的草稿弹窗
            entered = False
            for attempt in range(3):
                await self._tap_ratio(self.RATIO_BOTTOM_PLUS, "底部+")
                # 拍摄页要 6 秒才出来。真机 4 秒够，模拟器不够
                # （ARM 转译慢），统一给 6 秒。
                await asyncio.sleep(6)

                labels = await self._labels()
                if any("相册" in s for s in labels):
                    entered = True
                    break

                # 草稿弹窗：关掉之后抖音会**退回首页**，不是拍摄页，
                # 所以下一轮还得重新点「+」。
                if any("继续编辑" in s for s in labels):
                    log.info("发布：撞上草稿弹窗，先关掉它")
                    for word in ("取消", "存草稿"):
                        r = await self.hands.tap_text(word)
                        if "找不到" not in r and "读不到" not in r:
                            log.info("发布：草稿弹窗已关闭（点了「%s」）", word)
                            await asyncio.sleep(2)
                            break
                    continue

                # 其他原因没进拍摄页 —— 退一层再试。
                # 首页按 back 会退出抖音，所以退完要确认还在抖音里，
                # 退过头就重新拉起来（比死循环稳）。
                log.info(
                    "发布：点了+没进拍摄页（第 %d 次），这屏是：%s",
                    attempt + 1, self._brief(labels),
                )
                await self.hands._adb("shell", "input keyevent 4")
                await asyncio.sleep(2)
                pkg = await self.hands.current_package()
                if "aweme" not in (pkg or ""):
                    log.info("发布：退过头了（前台是 %s），重新打开抖音", pkg or "未知")
                    await self.hands.open_app("aweme")
                    await asyncio.sleep(9)

            if not entered:
                raise PublishError("点了三次「+」都没进到拍摄页")

            steps.append("点+")

            r = await self.hands.tap_text("相册")
            if "找不到" in r or "读不到屏幕" in r:
                raise PublishError(r.strip("（）") or "拍摄页上找不到「相册」")
            await asyncio.sleep(5)
            steps.append("进相册")

            # --- 2. 筛成视频，只留一个 -------------------------------
            # 不筛的话相册里几百张照片混在一起，网格上没有文件名，
            # 只能靠时长猜 —— 实测猜错过两次。
            r = await self.hands.tap_text("视频")
            if "找不到" in r or "读不到屏幕" in r:
                # 透传 hands 的说明：它会区分「界面没有这个按钮」
                # 和「手机掉线了所以读不到」，这两件事修法完全不同。
                raise PublishError(r.strip("（）") or "相册页上找不到「视频」筛选")
            await asyncio.sleep(4)
            labels = await self._labels()
            if any("暂无相应" in s or "暂无" in s for s in labels):
                raise PublishError("相册里没有视频 —— is_pending 大概又没修掉")
            steps.append("筛选视频")

            # --- 3. 选第一个 → 预览 → 编辑 ---------------------------
            await self._tap_ratio(self.RATIO_ALBUM_FIRST, "视频缩略图")
            await asyncio.sleep(5)
            steps.append("选视频")

            await self._tap_ratio(self.RATIO_NEXT_PREVIEW, "下一步(预览)")
            await asyncio.sleep(6)
            await self._tap_ratio(self.RATIO_NEXT_EDIT, "下一步(编辑)")
            await asyncio.sleep(7)
            steps.append("进标题页")

            # --- 4. 填内容 -------------------------------------------
            labels = await self._labels()
            if not any("发作品" in s for s in labels):
                raise PublishError("没进到标题页（找不到「发作品」）")
            self._check_abort(labels)

            await self._fill(title, desc, topics, steps)

            # --- 5. 收尾 ---------------------------------------------
            await self.hands.use_adb_keyboard(False)
            await asyncio.sleep(1.5)
            # 键盘弹着会挡住「发作品」。back 在键盘弹出时只收键盘，不退出页面。
            await self.hands._adb("shell", "input keyevent 4")
            await asyncio.sleep(2)
            steps.append("填完内容")

            # --- 5.5 标注 AI 生成 -------------------------------------
            # 放在收完键盘之后：声明弹层从底部升起，键盘还开着会被挡掉一半，
            # 选项点不中。而且这里**不能再按 back** —— 键盘已经收了，
            # 再按一次就是退出发布页，标题描述全丢。
            if self.ai_label == "strict":
                await self._mark_ai(steps)
            elif self.ai_label == "desc":
                steps.append("AI 标注只在描述里")


            if dry_run:
                log.info("dry_run：停在标题页，没点发作品")
                steps.append("dry_run 停在标题页")
                return PublishResult(True, title, desc, "dry_run", steps)

            # --- 6. 发 -------------------------------------------------
            await self._ensure("发作品前")
            labels = await self._labels()
            if not any("发作品" in s for s in labels):
                raise PublishError("要发的时候找不到「发作品」按钮")

            await self._tap_ratio(self.RATIO_PUBLISH, "发作品")
            await asyncio.sleep(10)
            steps.append("点了发作品")

            # 发布成功后抖音会跳到播放页（或回主页）。判据有三个，任一命中就算成功：
            #
            #   ① 出现「发布成功」一类提示
            #   ② 屏幕上出现刚写的标题**或描述**
            #   ③ 出现「X秒前」—— 播放页作者行是「@桃桃 · 2秒前」，
            #      这是刚发出去才有的特征
            #
            # **播放页的 UI 树读不到**（视频在播，uiautomator 拿不到 idle），
            # 所以 `_labels()` 返回空时必须退到 OCR。
            #
            # 2026-10-30 就是没做这一步：视频明明发出去了（播放页显示
            # 「@桃桃 · 2秒前」+ 描述），但代码只认标题、而且 UI 树是空的，
            # 于是报「点完发作品但没看到成功迹象」—— 差点让她以为失败，
            # 当天不再发，还白丢一条配额。
            for _ in range(6):
                labels = await self._labels()
                blob = " ".join(labels)

                if not blob and not getattr(self.hands, "_last_adb_error", ""):
                    # 空且没有 adb 错误 = 视频页，不是掉线。用 OCR 读画面。
                    blob = await self.hands.read_screen_ocr()
                    if blob:
                        log.info("发布：UI 树读不到，OCR 读到：%s", blob[:80].replace("\n", " "))

                if any(h in blob for h in _SUCCESS_HINTS):
                    steps.append("看到发布成功提示")
                    return PublishResult(True, title, desc, "", steps)

                for probe in (title, desc[:12]):
                    if probe and probe in blob:
                        steps.append("在播放页看到自己的内容")
                        return PublishResult(True, title, desc, "", steps)

                if re.search(r"\d+\s*秒前", blob):
                    steps.append("播放页显示刚发出（X秒前）")
                    return PublishResult(True, title, desc, "", steps)

                await asyncio.sleep(3)

            # 没确认到 —— **不能直接判失败**：抖音有时候点完发作品会跳到
            # 「日常/旧作品播放页」，18 秒证据循环一个都不命中，但作品其实
            # 已经发出去了（2026-11-06 d1 真发测试的假阴性：作品都上了主页，
            # 这里却返回失败，scheduler 会记 failed、白丢一天配额还误报）。
            # 所以先去个人主页作品列表复核一次。
            log.info("发布：18 秒内没确认到，去个人主页复核一次")
            evidence = await self._recheck_published(title, desc)
            if evidence:
                steps.append(evidence)
                return PublishResult(True, title, desc, "", steps)

            # 复核也判不了：降级成 unsure。scheduler 会「不重试、不记 failed、
            # 但照样占当天额度」—— 宁可少发一条，不可重复发。
            return PublishResult(
                False,
                title,
                desc,
                "点完发作品但没看到成功迹象，复核也没确认",
                steps,
                unsure=True,
            )

        except PublishError as exc:
            log.warning("发布失败：%s", exc)
            return PublishResult(False, title, desc, str(exc), steps)
        except Exception as exc:  # noqa: BLE001
            log.exception("发布时发生未预期异常")
            return PublishResult(False, title, desc, f"{type(exc).__name__}: {exc}", steps)

    # ------------------------------------------------------------------ #
    # 复核（发布判据的兜底）

    async def _recheck_published(self, title: str, desc: str) -> str:
        """点完「发作品」没确认到时，去个人主页**点开第一条作品**复核。

        为什么需要：2026-11-06 d1 真发测试，点完发作品后抖音跳到的是
        「日常/旧作品播放页」，原来的 18 秒证据循环一个都不命中
        （没发布提示、没标题、也没有「X秒前」），于是返回失败 ——
        但作品其实已经发出去了。假阴性会让 scheduler 记 failed、
        触发「今天失败过不再重试」，实际又已经发了：白丢配额还误报。

        流程是 d1 2026-11-06 真机 3 轮实测校准的：
        force-stop 重开 → 处理「继续编辑作品吗？」草稿弹窗 → 点底部「我」→
        **不要点「作品」tab**（会弹「双列视图」提示吃掉后续点击；「我」页
        默认就是作品列表）→ 作品网格读不到标题/时间、只有每格底部的播放数，
        所以点开第一条（最上排最左播放数标签 → 点 (中心x, 标签顶−140)）→
        播放页 UI 树经常是空的，标题/时间**只能靠 OCR**。

        判据：OCR 里同时出现本次标题/描述（去标点、前 8 字容错）
        **和**刚发的时间（刚刚 / X秒前 / X分钟前且 X≤10）才算命中 ——
        旧作品的时间是「2天前 / X小时前」，不会误命中。
        命中返回证据；判不了（读不到、页面不对、账号异常）返回空串，
        调用方降级成 unsure。
        """
        hands = self.hands
        try:
            await self._ensure("复核前")
            # 先回干净首屏：不然可能停在播放页/发布页的半截状态。
            await hands._adb("shell", f"am force-stop {self.PACKAGE}")
            await asyncio.sleep(2)
            await hands.open_app("aweme")
            await asyncio.sleep(8)
            # 首页在播视频时 UI 树读不到 → 循环「dump → 空就按 127 → 等」
            # 直到能读。d1 2026-11-06 实测：ensure_paused 只盲按一次 127
            # 有时没停住（127 是 toggle），之后所有 dump 全是空的，
            # `tap_text("我")` 直接「找不到」，复核白跑一遍。
            if not await self._wait_feed_readable():
                log.info("复核：重开抖音后 UI 树一直读不到，改用固定比例点底部导航")
            # force-stop 重开后可能先弹「继续编辑作品吗？」草稿框，
            # 不点掉它盖在首页上，点「我」会点不到（d1 实测）。
            await self._dismiss_draft_dialog()

            # 点「我」并**确认真的到了个人主页**（UI 树点不到就用固定比例）。
            if not await self._open_profile_page():
                return ""
            await asyncio.sleep(1.0)

            # 进「我」页默认就是作品列表 —— **不要点「作品」tab**，
            # 点了会弹「双列视图」提示把后续点击吃掉（d1 实测踩过）。
            point = None
            for _ in range(3):
                point = await self._first_work_point()
                if point is not None:
                    break
                await asyncio.sleep(1.5)
            if point is None:
                log.info("复核：「我」页里找不到作品列表的播放数标签，放弃复核")
                return ""
            x, y = point
            await hands.tap_xy(x, y)
            log.info("复核：点开第一条作品 @(%d,%d)", x, y)
            await asyncio.sleep(3.5)

            blob = await self._read_play_page()
            if not blob:
                log.info("复核：播放页读不到任何文字（UI 树空、OCR 也空），判不了")
                return ""
            # 账号异常/验证码：这不是「没发出去」，是复核不了，别当失败证据。
            self._check_abort([blob])
            evidence = self._match_published(blob, title, desc)
            if evidence:
                log.info("复核：%s", evidence)
            else:
                log.info(
                    "复核：播放页 OCR 里没同时确认「标题 + 刚发时间」，判不了（OCR：%s）",
                    _brief_text(blob),
                )
            return evidence
        except PublishError as exc:
            log.info("复核没走通（%s），按「结果不确定」处理", exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("复核异常（%s），按「结果不确定」处理", exc)
            return ""

    async def _wait_feed_readable(self, attempts: int = 3) -> bool:
        """等首页 UI 树能读。读不到就按一次媒体键（127）再试，最多 3 次。

        d1 2026-11-06 实测：`ensure_paused()` 盲按一次 127 有时没把视频停住
        （127 是 toggle，可能被反转），之后所有 dump 都是空的，
        `tap_text("我")` 直接「找不到」。所以这里循环确认，而不是盲按一次。
        adb 已经报错（掉线）就不再盲试，直接交给比例坐标兜底。
        """
        for index in range(1, max(1, attempts) + 1):
            try:
                labels = await self._labels()
            except PublishError as exc:
                # `_labels()` 自己会区分「视频页读不到」和「手机掉线」，
                # 掉线时它抛异常 —— 这时候盲按媒体键没意义，直接退兜底。
                log.info("复核：读不到屏幕（%s），不盲试媒体键", exc)
                return False
            if labels:
                return True
            err = getattr(self.hands, "_last_adb_error", "")
            if err:
                log.info("复核：读不到屏幕且 adb 报错（%s），不盲试媒体键", err)
                return False
            log.info(
                "复核：首页 UI 树空（第 %d/%d 次），按一次媒体键再试",
                index, attempts,
            )
            try:
                await self.hands.pause_media()
            except Exception:  # noqa: BLE001
                return False
            await asyncio.sleep(1.5)
        try:
            return bool(await self._labels())
        except PublishError:
            return False

    async def _open_profile_page(self) -> bool:
        """点开底部导航「我」，并确认真的到了个人主页。到不了返回 False。

        两个实测坑（d1 2026-11-06）：
        - 视频在播时 UI 树是空的，`tap_text` 会「找不到」；但底部导航是静态
          chrome，可以直接按比例点（RATIO_ME_TAB）。
        - 「我」的精确 label 是「我」，同屏视频作者名也可能带「我」字，
          所以 UI 树能读时**只认精确命中**，不认包含匹配。
        点完必须校验个人页标志（见 PROFILE_MARKERS），否则后面点第一条
        作品就是瞎点 —— 宁可按不了（返回 unsure），也不点错内容。
        """
        labels = await self._labels()
        if labels:
            r = await self.hands.tap_text("我")
            if "点了「我」" in (r or ""):
                log.info("复核：点了底部「我」（UI 树精确命中）")
            else:
                log.info("复核：UI 树里没能精确点到「我」（%s），改用固定比例", r)
        else:
            log.info("复核：UI 树读不到，直接用固定比例点底部「我」")

        await asyncio.sleep(2.5)
        if await self._wait_profile_page():
            return True

        # 兜底：按固定比例再点一次（底部导航静态，不依赖 UI 树）。
        try:
            width, height = await self._size_cached()
        except Exception:  # noqa: BLE001
            return False
        x = int(round(self.RATIO_ME_TAB[0] * width))
        y = int(round(self.RATIO_ME_TAB[1] * height))
        await self.hands.tap_xy(x, y)
        log.info("复核：用固定比例再点一次底部「我」@(%d,%d)", x, y)
        await asyncio.sleep(2.5)
        return await self._wait_profile_page()

    async def _wait_profile_page(self, attempts: int = 3) -> bool:
        """等个人主页出现：获赞 / 作品 / 私密作品 三个标志同时出现。"""
        labels: List[str] = []
        for _ in range(max(1, attempts)):
            labels = await self._labels()
            blob = " ".join(labels)
            if all(word in blob for word in PROFILE_MARKERS):
                return True
            await asyncio.sleep(1.2)
        log.info("复核：没确认到个人主页（拿到的文字：%s）", self._brief(labels))
        return False

    async def _dismiss_draft_dialog(self) -> None:
        """收掉 force-stop 重开后可能弹的「继续编辑作品吗？」草稿弹窗。"""
        labels = await self._labels()
        if not any("继续编辑" in s for s in labels):
            return
        for word in ("取消", "放弃"):
            r = await self.hands.tap_text(word)
            if "点了" in (r or ""):
                log.info("复核：关掉了草稿弹窗（点了「%s」）", word)
                await asyncio.sleep(2)
                return
        log.info("复核：遇到草稿弹窗但没点到「取消」，这轮复核大概要黄")

    async def _first_work_point(self) -> Optional[tuple]:
        """「我」页作品列表里第一条作品的点击位置。找不到返回 None。

        d1 2026-11-06 实测：作品网格读不到标题/时间，只有每格底部的播放数
        （7 / 1790 / 45）；第一排最左那个标签 bounds(12,1529,238,1561)，
        点 (标签中心x, 标签顶−140) 能准确打开第一条，3 轮都准。
        这里不写死坐标：按「下半屏、整格宽度的纯数字标签」动态找最上排最左。
        """
        try:
            width, height = await self._size_cached()
        except Exception:  # noqa: BLE001
            return None

        cand: List[tuple] = []  # (top, left, center_x)
        try:
            nodes = await self.hands.ui() or []
        except Exception:  # noqa: BLE001
            nodes = []
        for node in nodes:
            label = (getattr(node, "label", "") or "").strip()
            if not re.fullmatch(r"\d+", label):
                continue
            bounds = getattr(node, "bounds", None)
            if not bounds:
                continue
            left, top, right, _bottom = bounds
            # 头部「获赞 521 / 粉丝 13」的数字不在下半屏，按位置+宽度排除；
            # 播放数标签是整格宽度、贴着屏幕下半部分（d1 实测 y≈1529）。
            if top < height * 0.5:
                continue
            if (right - left) < width * 0.15:
                continue
            cand.append((top, left, (left + right) // 2))

        if not cand:
            # 「我」页 UI 树偶发读不到时退 OCR。注意 OCR 框是「文字本身」的宽度
            # （播放数「7」只有二十来像素），不能像 UI 节点那样按格子宽度过滤，
            # 只按「下半屏的纯数字」取最上排最左。
            try:
                hits = await self.hands.ocr_boxes()
            except Exception:  # noqa: BLE001
                hits = []
            for hit in hits or []:
                text = (hit.text or "").strip()
                if not re.fullmatch(r"\d+", text):
                    continue
                if hit.y < height * 0.5:
                    continue
                cand.append((hit.y, hit.x, hit.x + hit.w // 2))

        if not cand:
            return None
        cand.sort(key=lambda item: (item[0], item[1]))  # 最上排 → 最左
        top, _left, center_x = cand[0]
        offset = int(round(140 * height / 1600.0))  # d1 实测：标签顶 −140（720×1600）
        return center_x, max(1, top - offset)

    async def _read_play_page(self) -> str:
        """播放页读文字。

        d1 2026-11-06 实测：点开第一条后，**3 轮里有 2 轮 UI 树是空的**
        （视频在播，uiautomator 拿不到 idle），所以 OCR 才是主判据，
        UI 树只当顺便试试。
        """
        labels = await self._labels()
        blob = " ".join(labels)
        if blob.strip():
            return blob
        for attempt in (1, 2):
            ocr = await self.hands.read_screen_ocr()
            if ocr and ocr.strip():
                return ocr
            if attempt == 1:
                await asyncio.sleep(1.5)
        return ""

    @staticmethod
    def _match_published(blob: str, title: str, desc: str) -> str:
        """播放页 OCR 里同时有「本次标题/描述」和「刚发的时间」才算命中。

        只认标题不认时间，旧作品（同标题重发/标题相似）会误判；
        只认时间不认标题，跳到别的播放页也会误判。所以两个条件同时成立：
        - 标题/描述去标点后前 8 字容错（OCR 会把标题拆成两行，
          `_flat_text` 去空白后能跨行匹配）；
        - 时间必须是刚发：刚刚 / X秒前 / X分钟前且 X≤10
          （旧作品是「2天前 / X小时前」）。
        """
        flat = _flat_text(blob)
        if not flat:
            return ""
        probe = ""
        source_name = ""
        for source, name in ((title, "标题"), (desc, "描述")):
            for limit in (10, 8, 6, 5):
                p = _probe_text(source, limit)
                if p and p in flat:
                    probe, source_name = p, name
                    break
            if probe:
                break
        if not probe:
            return ""
        fresh = _fresh_time_hit(blob)
        if not fresh:
            return ""
        return f"复核：播放页读到{source_name}「{probe}」+ 时间「{fresh}」"

    async def _fill(
        self,
        title: str,
        desc: str,
        topics: Sequence[str],
        steps: List[str],
    ) -> None:
        """填标题、描述、话题。

        `input_text` **不会自己切输入法** —— 打中文前必须手动切 ADBKeyBoard，
        否则 `input text` 会抛 NullPointerException，一个字符都进不去。
        """
        await self.hands.use_adb_keyboard(True)
        await asyncio.sleep(1.5)

        if title:
            r = await self.hands.tap_text("添加标题")
            if "找不到" in r:
                raise PublishError("标题页找不到「添加标题」")
            await asyncio.sleep(2)
            r = await self.hands.input_text(title)
            if "没打进去" in r:
                raise PublishError(f"标题没打进去：{r}")
            await asyncio.sleep(2)
            steps.append(f"标题「{title}」")

        if desc:
            r = await self.hands.tap_text("添加作品描述")
            if "找不到" in r:
                raise PublishError("标题页找不到「添加作品描述」")
            await asyncio.sleep(2)
            r = await self.hands.input_text(desc)
            if "没打进去" in r:
                raise PublishError(f"描述没打进去：{r}")
            await asyncio.sleep(2)
            steps.append(f"描述「{desc[:20]}…」")

        # 话题：点抖音推荐的标签，比自己拼 # 靠谱 ——
        # 推荐的标签有真实流量，手打的容易打成死话题。
        if not topics:
            return
        nodes = await self.hands.ui()
        for want in topics:
            hit = None
            for n in nodes:
                if not n.clickable:
                    continue
                # 推荐按钮的 label 长这样：`漫系少女日常,,按钮`
                if want in n.label:
                    hit = n
                    break
            if hit is None:
                log.debug("推荐话题里没有「%s」，跳过", want)
                continue
            x, y = hit.center
            await self.hands.tap_xy(x, y)
            await asyncio.sleep(2)
            steps.append(f"话题#{want}")
            nodes = await self.hands.ui()
