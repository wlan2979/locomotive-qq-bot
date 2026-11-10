"""桃桃的手：让她真的能操作一台 Android 手机。

不是「能调 API」，是「能点屏幕」。

## 设计上最重要的一条：按文字点，不按坐标点

让模型看截图然后吐一个 `(x, y)`，是最容易想到也最容易翻车的做法 ——
它得先认出「这个按钮在哪」，再换算成像素，任何一步偏了就是点空。
实测这类方案的失败率高得离谱，而且**失败时看起来像成功**
（点了、没反应、模型以为点了、继续往下走）。

`uiautomator dump` 给的是结构化 UI 树，每个节点自带文本和 bounds。
所以这里的原语是 `tap_text("搜索")` —— 找到文本等于「搜索」的那个节点，
取它 bounds 的中心去点。模型只需要说「点搜索」，不需要知道搜索在屏幕哪儿。

代价是：视频类 App（抖音、小红书的信息流）大量是自绘/Flutter，
dump 出来常常只有一个空壳。那种情况才退到截图 + 视觉。

## 为什么不给她 `adb shell` 的完全能力

她是个十几岁的小丫头，任务是「玩手机」。给她一台能执行任意 shell 的
Android，等于把整台设备（相册、聊天记录、支付）交出去。
所以动作是**白名单原语**：看、点文字、滑、返回、回桌面、打字、开 App。
没有「执行命令」这个动作。

## 中文输入是残缺的

`adb shell input text` **只吃 ASCII**。中文会直接丢字符或报错。
绕过需要往手机上装输入法 APK（ADBKeyBoard）或者走剪贴板广播，
两条路都要额外装东西，而且都不可靠。

所以：英文数字能打，中文打不了。这不是没做完，是这条路的真实边界。
需要中文输入时，让她用 App 自带的搜索推荐词、或者干脆别搜。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 给模型看的工具声明
#
# 只暴露一个工具、五个动作。不给 `adb shell` 这种万能口子 ——
# 她要的是「玩手机」，不是「拥有这台设备」。

USE_PHONE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "use_phone",
        "description": (
            "操作你自己的手机（一台安卓模拟器）。"
            "这是真的在动手机屏幕，不是模拟 —— 点错了就是真的点错了。"
            "动作很慢，每次要等一两秒，所以别拿它当搜索用，"
            "要查事实还是用 web_search。"
            "什么时候用：有人让你去手机上看看、你想刷会儿视频、"
            "或者要确认某个 App 里有什么。"
            "规矩：一次只看一个界面；刷信息流用 scroll；"
            "看到支付、密码、卸载这类东西会被立刻拦下，那时候就停手别硬来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "look", "scroll", "tap", "type", "back", "home"],
                    "description": (
                        "open=打开一个 App；look=看看当前屏幕有什么；"
                        "scroll=往下刷信息流；tap=点屏幕上某个文字；"
                        "type=在当前输入框里打字（中文也行）；"
                        "back=返回；home=回桌面"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "action=open 时填 App 名（如「抖音」「bilibili」）；"
                        "action=tap 时填要点的那个文字，**照抄屏幕上的原文**；"
                        "action=type 时填要打进去的内容（中英文都行）；"
                        "其他动作不用填"
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "action=scroll 时刷几条，默认 3，最多 8",
                },
            },
            "required": ["action"],
        },
    },
}


# --------------------------------------------------------------------------- #
# 安全

# 屏幕上出现这些词，整台手机立刻停手。
#
# 为什么是「出现就停」而不是「点到才停」：她可能已经点进支付页了，
# 这时候再让她「看着办」太危险。发现即中止，把控制权交回给人。
DANGER_WORDS: Tuple[str, ...] = (
    "支付", "付款", "确认支付", "立即支付", "输入密码", "支付密码", "免密",
    "转账", "收款码", "付款码", "扫码付款", "钱包余额", "提现", "绑定银行卡",
    "卸载", "删除应用", "清除数据", "格式化", "恢复出厂", "重置手机",
    "退出登录", "注销账号", "授权登录", "获取权限",
    # 权限弹窗。**这是实测漏掉的**：抖音弹「抖音需要使用位置信息权限」，
    # 按钮写的是「使用时允许」，原来只拦「允许访问」根本拦不住 ——
    # 等于她自己能把位置权限授出去。系统权限一旦给出去是**持久**的，
    # 比点错一个按钮严重得多，所以这里宁可拦宽一点。
    "权限", "使用时允许", "仅本次使用时允许", "始终允许", "允许访问",
    "位置信息", "读取通讯录", "读取短信", "麦克风", "摄像头",
    "root", "超级用户", "开发者选项", "USB 调试",
)

# 只读类界面特征。这些不是危险，是「她该待的地方」。
# 不强制，只用于给她提示，别在设置页里迷路。
BORING_WORDS: Tuple[str, ...] = ("设置", "应用管理", "权限管理", "隐私")


def danger_hit(text: str) -> Optional[str]:
    """屏幕文本里有没有危险词。返回命中的那个，没有就 None。"""
    if not text:
        return None
    low = text.lower()
    for word in DANGER_WORDS:
        if word.lower() in low:
            return word
    return None


# 权限弹窗单独处理 —— **不再一律中止**。
#
# 2026-10-23 用户明确授权：「凡是要权限的，你自己就可以直接点」。
# 原因是发布视频时抖音必须要「存储空间权限」（读相册），
# 原来把「权限」写进 DANGER_WORDS 导致整页被拦，流程走不下去。
#
# 但**不是全放行**：下面这几个跟「发视频」毫无关系，
# 放行没有任何收益、只有风险，所以仍然中止。
# 这不是不听话，是不拿用户隐私去换一个用不到的功能。
HIGH_RISK_PERMISSIONS: Tuple[str, ...] = (
    "通讯录", "联系人", "短信", "通话记录", "通话信息",
    "身体传感器", "后台弹出界面", "读取应用列表",
)

# 权限弹窗的特征：同时出现「权限」和「允许/拒绝」。
PERMISSION_HINTS: Tuple[str, ...] = ("权限", "允许", "拒绝")


def is_permission_prompt(text: str) -> bool:
    """这一屏是不是系统权限请求弹窗。

    判据是「权限」+「允许」或「拒绝」同时出现 ——
    正常界面不会把这两个词凑在一起。
    """
    if "权限" not in (text or ""):
        return False
    return "允许" in text or "拒绝" in text


# --------------------------------------------------------------------------- #
# UI 树


@dataclass
class UiNode:
    text: str
    desc: str
    cls: str
    bounds: Tuple[int, int, int, int]   # left, top, right, bottom
    clickable: bool
    package: str

    @property
    def label(self) -> str:
        """给人看/给模型看的那串字。文本优先，没有就退到 content-desc。"""
        return (self.text or self.desc or "").strip()

    @property
    def center(self) -> Tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _parse_bounds(raw: str) -> Tuple[int, int, int, int]:
    m = _BOUNDS_RE.search(raw or "")
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def parse_ui_xml(xml_text: str) -> List[UiNode]:
    """把 uiautomator 的 XML 解析成节点列表。

    只保留**有字**的节点 —— 没有文本的容器对「按文字点」毫无用处，
    留着只会把上下文撑爆（一个信息流页面能 dump 出几百个空 FrameLayout）。
    """
    if not xml_text or "<hierarchy" not in xml_text:
        return []

    # 有的设备/系统版本会 dump 出带 BOM 或前置垃圾的 XML，先截到第一个 <
    start = xml_text.find("<")
    if start > 0:
        xml_text = xml_text[start:]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("UI 树解析失败：%s", exc)
        return []

    out: List[UiNode] = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        if not text and not desc:
            continue
        out.append(
            UiNode(
                text=text,
                desc=desc,
                cls=(node.get("class") or "").split(".")[-1],
                bounds=_parse_bounds(node.get("bounds") or ""),
                clickable=(node.get("clickable") or "false") == "true",
                package=node.get("package") or "",
            )
        )
    return out


def ui_digest(nodes: List[UiNode]) -> str:
    """当前屏幕的指纹。用来判断「点了但没变化」。"""
    blob = "|".join(f"{n.label}@{n.bounds}" for n in nodes)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class FeedItem:
    """一条信息流卡片（B站首页那种）。

    从 UI 树的 content-desc 里扒出来的。实测原始串长这样：

        竖版视频,兄弟如手足,254.4万观看,1845弹幕,时长3分钟11秒,UP主脱缰凯Kk,竖屏,
        视频,魔鬼辣云南米线，配料全加一遍，抱盆爽吃！,49.8万观看,723弹幕,时长11分钟38秒,UP主小紧张的虫虫,

    标题里可能有**中文逗号**（上面第一条就是），所以不能简单 split(",")，
    用正则从右边锚定播放量/弹幕/时长/UP主，剩下的全算标题。
    """

    title: str
    views: str = ""
    danmaku: str = ""
    duration: str = ""
    uploader: str = ""
    vertical: bool = False

    def line(self) -> str:
        bits = []
        if self.uploader:
            bits.append(f"UP主{self.uploader}")
        if self.views:
            bits.append(f"{self.views}播放")
        if self.duration:
            bits.append(self.duration)
        if self.vertical:
            bits.append("竖屏")
        return f"{self.title}（{' · '.join(bits)}）" if bits else self.title


# 抖音信息流里的 UI 控件前缀。用来把「文案」和「按钮」分开。
_DOUYIN_UI_PREFIXES: Tuple[str, ...] = (
    "未点赞", "已点赞", "未选中", "已选中", "评论", "收藏", "分享",
    "音乐", "发弹幕", "暂停视频", "播放视频", "全屏观看", "团购",
    "商城", "关注", "已关注", "推荐", "精选", "首页", "朋友",
    "消息", "我", "拍摄", "图片", "收起", "展开",
)


def parse_douyin_feed(nodes: List[UiNode], limit: int = 20) -> List[str]:
    """从抖音信息流里扒「文案」。

    **抖音没有标题，只有一句文案**，所以没法像 B站那样按固定格式解析。
    只能反着来：把明显是 UI 控件的剔掉，剩下的长句就是文案。

    实测一条视频的节点长这样：

        @一乖                                    ← 作者，太短，被长度过滤
        未点赞，喜欢253.4万，按钮                    ← 前缀命中，剔掉
        评论316，按钮                             ← 剔掉
        音乐，@狐栖鑫云汀创作的原声，按钮              ← 剔掉
        弟媳在这@甜妹 祝大家都幸福                    ← 这句是文案，留下
        秦皇岛，按钮                              ← 后缀是「按钮」，剔掉

    剔错一两条无所谓 —— 文案会喂给模型抽「结构」，不是直接抄。
    """
    out: List[str] = []
    seen = set()
    for node in nodes:
        label = (node.label or "").strip()
        if not label or label in seen:
            continue
        # 长度过滤：太短是作者名，太长是长文/广告
        if len(label) < 6 or len(label) > 100:
            continue
        # 以「按钮」结尾的都是控件
        if label.endswith("按钮"):
            continue
        # 纯数字或数字+单位（点赞数之类）
        if re.fullmatch(r"[\d.]+[万亿]?", label):
            continue
        if any(label.startswith(p) for p in _DOUYIN_UI_PREFIXES):
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out


# 从右往左锚定：标题贪婪吃掉前面所有内容，后面的字段格式固定。
# 这样标题里有多少逗号都不影响。
_FEED_RE = re.compile(
    r"^(?:竖版视频|视频),"
    r"(?P<title>.+),"
    r"(?P<views>[\d.]+万?)观看,"
    r"(?P<danmaku>[\d.]+万?)弹幕,"
    r"时长(?P<duration>[^,]*),"
    r"UP主(?P<uploader>[^,]*)"
)


def parse_feed(nodes: List[UiNode]) -> List[FeedItem]:
    """从 UI 树里扒出信息流卡片。读不到就返回空列表（抖音大概率是空）。"""
    out: List[FeedItem] = []
    seen = set()
    for node in nodes:
        for raw in (node.desc, node.text):
            if not raw or ("视频," not in raw[:8]):
                continue
            m = _FEED_RE.match(raw)
            if not m:
                continue
            title = m.group("title").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            out.append(
                FeedItem(
                    title=title,
                    views=m.group("views"),
                    danmaku=m.group("danmaku"),
                    duration=m.group("duration"),
                    uploader=m.group("uploader"),
                    vertical=raw.startswith("竖版视频"),
                )
            )
    return out


def describe_screen(nodes: List[UiNode], limit: int = 40) -> str:
    """把 UI 树压成给模型看的几行。

    只给文字 + 位置感（上/中/下），不给坐标 —— 她不需要坐标，
    给坐标反而会诱导它去算，算错就点空。
    """
    if not nodes:
        return "（屏幕上没读到文字，可能是视频/游戏类自绘界面）"

    total = max((n.bounds[3] for n in nodes), default=1) or 1
    lines: List[str] = []
    seen = set()
    for n in nodes:
        label = n.label
        if not label or label in seen:
            continue
        seen.add(label)
        top = n.bounds[1]
        where = "上" if top < total / 3 else ("中" if top < total * 2 / 3 else "下")
        tag = "可点" if n.clickable else ""
        lines.append(f"[{where}]{label} {tag}".rstrip())
        if len(lines) >= limit:
            lines.append("…（还有更多，先看这些）")
            break
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 手


# 中文 App 名 / 常见叫法 → 包名。
#
# 为什么必须有这张表：`USE_PHONE_TOOL` 的 schema 里明写了
# 「action=open 时填 App 名（如「抖音」「bilibili」）」，模型就老老实实
# 传中文。但原来 open_app 只做 `keyword in pkg` 的子串匹配 ——
# 中文永远不可能出现在包名里，所以**每一次 open 都被拒绝**。
#
# 更阴的是：连 `bilibili` 也匹配不上 `tv.danmaku.bili`（少一个 i）。
# 只有 `aweme` / `bili` 这种恰好是包名片段的才碰巧能用。
#
# 症状是 2026-10-25 用户报「她好像没办法自己连接手机」：
# 日志里模型连着四次 `use_phone(open, target='抖音')` 全失败，
# 但 adb devices 明明正常 —— 锅在匹配，不在连接。
APP_ALIASES: Dict[str, str] = {
    "抖音": "com.ss.android.ugc.aweme",
    "douyin": "com.ss.android.ugc.aweme",
    "aweme": "com.ss.android.ugc.aweme",
    "tiktok": "com.ss.android.ugc.aweme",
    "b站": "tv.danmaku.bili",
    "b 站": "tv.danmaku.bili",
    "bilibili": "tv.danmaku.bili",
    "哔哩哔哩": "tv.danmaku.bili",
    "哔哩": "tv.danmaku.bili",
    "bili": "tv.danmaku.bili",
}


# --------------------------------------------------------------------------- #
# OCR 兜底
#
# 为什么需要这个：抖音里**视频在播的页面读不到 UI 树**
# （uiautomator 报 `could not get idle state`，连文件都不写）。
# 而视频页、评论区、创作者中心恰恰是她最常待的地方。
# 没有 OCR 兜底，她在抖音里就是个瞎子 —— 2026-10-25 用户报「她说她看不见」。
#
# 为什么不是「截图给她看」：模型是纯文本的（deepseek-flash），
# 给它一个图片路径等于什么都没给。必须转成文字。

_OCR_ENGINE: Any = None
_OCR_UNAVAILABLE = False


def _get_ocr() -> Any:
    """懒加载 OCR 引擎。没装就返回 None，**不抛异常**。

    没装 OCR 是正常情况（不是每个部署都装了），不能因此让 look() 崩掉。
    """
    global _OCR_ENGINE, _OCR_UNAVAILABLE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    if _OCR_UNAVAILABLE:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
        log.info("手部：OCR 引擎已就绪（读不到 UI 树时用它兜底）")
    except Exception as exc:  # noqa: BLE001
        _OCR_UNAVAILABLE = True
        log.info("手部：没装 OCR（%s），读不到字的界面只能报空", exc)
    return _OCR_ENGINE


def _ocr_image_sync(png: bytes) -> str:
    """把截图 OCR 成文字。**同步函数**，调用方要丢到线程里跑。

    按 y 坐标聚成行 —— OCR 只给「文本 + 左上角坐标」，不给行结构，
    不聚类的话读出来是一堆乱序碎片。
    """
    engine = _get_ocr()
    if engine is None or not png:
        return ""
    try:
        import io

        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        result, _ = engine(np.array(img))
    except Exception as exc:  # noqa: BLE001
        log.warning("手部：OCR 失败（%s）", exc)
        return ""

    rows: List[Tuple[int, int, str]] = []
    for item in result or []:
        try:
            box, text, score = item[0], str(item[1]), float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if score < 0.5 or not text.strip():
            continue
        rows.append((int(box[0][1]), int(box[0][0]), text.strip()))
    if not rows:
        return ""

    rows.sort()
    lines: List[str] = []
    cur: List[Tuple[int, str]] = []
    last_y: Optional[int] = None
    for y, x, text in rows:
        # 24 像素以内算同一行（720 宽的屏，字号大约 30px）
        if last_y is None or abs(y - last_y) <= 24:
            cur.append((x, text))
        else:
            lines.append(" ".join(t for _, t in sorted(cur)))
            cur = [(x, text)]
        last_y = y
    if cur:
        lines.append(" ".join(t for _, t in sorted(cur)))

    # 上限 40 行：一屏本来也放不下更多，再长只会把上下文撑爆。
    return "\n".join(lines[:40])


@dataclass
class OcrHit:
    """OCR 出来的一块文字 + 它在屏幕上的位置（像素）。"""

    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _ocr_boxes_sync(png: bytes) -> List[OcrHit]:
    """把截图 OCR 成「文字 + 坐标」。**同步函数**，调用方丢线程里跑。

    和 `_ocr_image_sync` 的唯一区别：这里保留 box 坐标。
    为什么需要：抖音评论区读不到 UI 树（见本文件顶部 OCR 兜底的说明），
    而评论输入框、发送按钮都在那片读不到的区域里 —— 只有「看得见坐标」
    才点得着。原来把 box 扔了，等于看得见字、点不着按钮。
    """
    engine = _get_ocr()
    if engine is None or not png:
        return []
    try:
        import io

        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        result, _ = engine(np.array(img))
    except Exception as exc:  # noqa: BLE001
        log.warning("手部：OCR 取坐标失败（%s）", exc)
        return []

    hits: List[OcrHit] = []
    for item in result or []:
        try:
            box, text, score = item[0], str(item[1]), float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if score < 0.5 or not text.strip():
            continue
        xs = [int(point[0]) for point in box]
        ys = [int(point[1]) for point in box]
        hits.append(
            OcrHit(
                text=text.strip(),
                x=min(xs),
                y=min(ys),
                w=max(xs) - min(xs),
                h=max(ys) - min(ys),
            )
        )
    return hits


class HandsError(RuntimeError):
    pass


class PhoneHands:
    """她的手（雷电模拟器，2026-11-04 起没有真机了）。所有动作都是白名单原语。"""

    # dump 落在这个路径。固定一个，方便「删了再 dump」时不会漏。
    UI_PATH = "/sdcard/_wb_ui.xml"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.adb = str(cfg.adb_path or "adb")
        self.serial = str(cfg.device or "").strip()
        self._size: Optional[Tuple[int, int]] = None
        # 一次任务的步数。由调用方 reset_task() 重置。
        self.steps = 0
        self._last_digest = ""
        self._same_count = 0
        self.aborted_reason = ""
        # 最近一次 adb 失败的原文。用来区分两件完全不同的事：
        # 「这个界面真的没有那个按钮」和「手机掉线了所以什么都读不到」。
        #
        # 2026-10-25 就是被这个骗的：adb 掉成 `device still authorizing`，
        # uiautomator dump 全空，最后报出来是「相册页上找不到『视频』筛选」——
        # 看起来像抖音改版，实际是线松了。
        self._last_adb_error = ""
        # 「插 USB 常亮」只配一次，见 _configure_keep_awake()
        self._awake_configured = False

    # ---------------------------------------------------------------- #

    def _base(self) -> List[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    async def _adb(self, *args: str, timeout: float = 15.0) -> Tuple[int, str]:
        """跑一条 adb 命令。返回 (退出码, 输出)。

        超时**不抛异常**，返回 (124, "") —— 调用方按「这步失败」处理就行。
        uiautomator dump 在某些机型上会卡十秒以上，抛异常会把整轮任务打断。
        """
        cmd = self._base() + list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning("adb 超时：%s", " ".join(args)[:80])
                return 124, ""
            text = (out or b"").decode("utf-8", errors="replace")
            if proc.returncode != 0:
                errtext = (err or b"").decode("utf-8", errors="replace")
                self._last_adb_error = errtext.strip()[:160]
                log.warning("adb 失败(%s)：%s", proc.returncode, errtext.strip()[:160])
            else:
                # 成功了就清掉，否则一次掉线会让后面所有的「找不到」
                # 都误报成连接问题。
                self._last_adb_error = ""
            return proc.returncode or 0, text
        except FileNotFoundError:
            raise HandsError(
                f"找不到 adb：{self.adb}。在 config.json 的 hands.adb_path 里填绝对路径。"
            ) from None
        except Exception as exc:  # noqa: BLE001
            log.warning("adb 调用异常：%s", exc)
            return 1, ""

    # ---------------------------------------------------------------- #
    # 设备

    async def raw_devices(self) -> Dict[str, str]:
        """序列号 -> 状态（`device` / `offline` / `unauthorized`）。

        要保留 offline 的设备，因为「线插着但连不上」和「根本没插」
        是两种完全不同的问题，处理方式也不一样。
        """
        code, out = await self._adb("devices", timeout=8.0)
        if code != 0:
            return {}
        found: Dict[str, str] = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0]] = parts[1]
        return found

    async def devices(self) -> List[str]:
        return [s for s, st in (await self.raw_devices()).items() if st == "device"]

    async def _configure_keep_awake(self) -> None:
        """保持屏幕常亮（防灭屏掐 adb）。

        现在是模拟器（2026-11-04 起没有真机），但原理一样：
        灭屏后 adb 的 input/uiautomator 都会变得不可靠。
        模拟器上这些 settings 一样能设，设置一次持久生效；
        重启模拟器可能丢，所以每次进程起来配一遍就够。
        """
        try:
            # 3 = 插着 USB 时保持唤醒（1=充电器 2=USB 4=无线）
            await self._adb(
                "shell", "settings put global stay_on_while_plugged_in 3", timeout=10.0
            )
            # 兜底：30 分钟无操作才灭屏
            await self._adb(
                "shell", "settings put system screen_off_timeout 1800000", timeout=10.0
            )
            log.info("手部：已设置屏幕常亮（防灭屏掐 adb）")
        except Exception as exc:  # noqa: BLE001
            # 设不上不是致命问题，只是连接会更脆
            log.debug("设置屏幕常亮失败：%s", exc)

    async def _connect_emulator(self) -> bool:
        """对模拟器重新发起 TCP connect。返回有没有连上一个 device。

        她的手机就是雷电模拟器（2026-11-04 起真机已撤），adb 走 TCP。
        模拟器掉线时设备是**整个从列表里消失**，`reconnect offline`
        对它毫无作用 —— 2026-11-04 19:40 的发布失败就是这个坑
        （重连跑了但没救回来）。唯一有效的重连方式是重新
        `adb connect 127.0.0.1:5555`（雷电默认 adb 端口 5555，
        控制台端口 5554）。
        """
        try:
            code, out = await self._adb("connect", "127.0.0.1:5555", timeout=10.0)
            text = (out or "").strip() or f"exit={code}"
            log.info("手部：模拟器重连 connect 结果：%s", text)
            return "connected" in text.lower()
        except Exception as exc:  # noqa: BLE001
            log.debug("手部：模拟器 connect 失败：%s", exc)
            return False

    async def ensure_ready(self) -> None:
        """确认手机连上了。没连上抛异常 —— 这是唯一该抛的地方。

        **会自动重连，并且会等设备回来。**

        她的手机是雷电模拟器（2026-11-04 起真机已撤）。掉线时设备整个
        从列表里消失，`reconnect offline` 救不了，必须重新
        `adb connect 127.0.0.1:5555`（见 `_connect_emulator`）。
        connect 成功后设备会以 TCP 别名（127.0.0.1:5555）出现，不是配置里
        写的 emulator-5554，所以要顺带把 serial 换成实际可用的那个。

        所以这里是「重连 → 轮询等它回来 → 再判断」，不是一个固定的 sleep。
        """
        last_raw: Dict[str, str] = {}

        for attempt in range(2):
            raw = await self.raw_devices()
            last_raw = raw
            live = [s for s, st in raw.items() if st == "device"]

            if live and (not self.serial or self.serial in live):
                if not self.serial:
                    self.serial = live[0]
                # 第一次连上时把「屏幕常亮」配好 —— 不配的话
                # 模拟器一灭屏，后面每一步都不可靠。
                if not self._awake_configured:
                    await self._configure_keep_awake()
                    self._awake_configured = True
                return

            if attempt == 0:
                log.info(
                    "手机不在（%s），尝试重连", raw or "设备列表为空"
                )
                # 只有模拟器，掉线一律走 TCP connect。
                await self._connect_emulator()
                # connect 之后设备要几秒才枚举回来 ——
                # 固定 sleep 3 秒实测不够，轮询等。
                for _ in range(8):
                    await asyncio.sleep(1.5)
                    raw = await self.raw_devices()
                    last_raw = raw
                    if any(st == "device" for st in raw.values()):
                        # connect 重连成功时设备以 TCP 别名出现
                        # （127.0.0.1:5555），配置里写的 emulator-5554
                        # 不在列表里 —— 把 serial 换成实际可用的那个，
                        # 否则下一步会误报「指定的设备不在列表里」。
                        live_now = [
                            s for s, st in raw.items() if st == "device"
                        ]
                        if self.serial not in live_now:
                            self.serial = live_now[0]
                            log.info("手部：设备以别名 %s 回来了，已切换", self.serial)
                        break

        # 到这里还没成，说明不是软掉线，报准确的原因
        live = [s for s, st in last_raw.items() if st == "device"]
        if not live:
            if last_raw:
                states = ", ".join(f"{s}={st}" for s, st in last_raw.items())
                if "unauthorized" in last_raw.values():
                    raise HandsError(
                        f"手机没授权（{states}）。看手机屏幕，勾「始终允许」再点「允许」。"
                    )
            raise HandsError(f"手机连不上（{states}）。试试重启一下模拟器。")
        raise HandsError(
            "没有连上模拟器。检查：① 雷电模拟器是不是开着 "
            "② 模拟器设置里「ADB 调试」有没有开 "
            "③ 模拟器重启一下再等半分钟"
        )

        if self.serial and self.serial not in live:
            raise HandsError(
                f"配置里指定的设备 {self.serial} 不在列表里，当前有：{live}"
            )
        if not self.serial:
            self.serial = live[0]

    async def screen_size(self) -> Tuple[int, int]:
        if self._size:
            return self._size
        code, out = await self._adb("shell", "wm", "size", timeout=8.0)
        text = out or ""
        # `wm size` 在改过分辨率时会输出两行：
        #   Physical size: 1920x1080
        #   Override size: 720x1600
        # **有 override 就必须用它** —— 抓第一个数字会拿到物理尺寸，
        # 按比例算出来的坐标全歪（2026-10-26 在雷电模拟器上踩到）。
        m = re.search(r"Override size:\s*(\d+)x(\d+)", text)
        if not m:
            m = re.search(r"Physical size:\s*(\d+)x(\d+)", text)
        if not m:
            m = re.search(r"(\d+)x(\d+)", text)
        self._size = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        return self._size

    async def current_package(self) -> str:
        """当前前台的包名。

        整条命令作为一个字符串传给 `adb shell` —— 管道在**手机端**执行。
        分开传参的话管道符号会被当成普通参数，grep 跑到电脑端去了。
        """
        code, out = await self._adb(
            "shell", "dumpsys window | grep mCurrentFocus", timeout=10.0
        )
        m = re.search(r"mCurrentFocus=.*?([\w.]+)/", out or "")
        return m.group(1) if m else ""

    # ---------------------------------------------------------------- #
    # 看

    async def ui(self, retries: int = 2) -> List[UiNode]:
        """dump 当前界面的 UI 树。

        **每次都必须先删旧文件。** 这不是洁癖，是踩出来的：

        界面在动的时候（抖音全屏播视频），`uiautomator` 会报
        `ERROR: could not get idle state.` 并且**根本不写文件**。
        这时候如果直接 `cat`，读到的是**上一次**的 dump ——
        症状是「屏幕明明换了，读到的还是旧界面」，而且不报任何错。
        实测第一遍就是被这个骗过去的：`ls` 显示文件 107KB，以为成功了，
        其实是十分钟前的旧文件。

        所以流程是：删 → dump → 检查输出里有没有 "dumped to" → 没有就重试。
        """
        out = ""
        for attempt in range(retries + 1):
            await self._adb("shell", f"rm -f {self.UI_PATH}", timeout=8.0)
            code, out = await self._adb(
                "shell", f"uiautomator dump {self.UI_PATH}", timeout=20.0
            )
            if "dumped to" not in (out or ""):
                # 视频播放时界面永远不 idle。等半秒再试，有时能赶上渲染间隙。
                if attempt < retries:
                    await asyncio.sleep(0.8)
                continue

            code, xml = await self._adb(
                "exec-out", f"cat {self.UI_PATH}", timeout=15.0
            )
            if code != 0 or not xml:
                if attempt < retries:
                    await asyncio.sleep(0.8)
                continue

            # **传输被截断的典型症状：XML 不以 `</hierarchy>` 结尾。**
            # 2026-10-26 实测：push 12MB 视频之后 adb 变得不稳，dump 传到
            # 28351 字符处断了，`ET.fromstring` 报 `unclosed token`，
            # 上层拿到空节点列表，最后报成「界面上没有这个按钮」——
            # 又一次把线的问题伪装成界面问题。
            if "</hierarchy>" not in xml:
                log.warning(
                    "UI dump 被截断（%d 字符，没有收尾标签），重试", len(xml)
                )
                if attempt < retries:
                    await asyncio.sleep(1.0)
                continue

            return parse_ui_xml(xml)

        log.debug("UI dump 失败（试了 %d 次）", retries + 1)
        return []

    async def screenshot(self) -> Optional[bytes]:
        """截一张图（PNG 原始字节）。UI 树读不到时的兜底。"""
        cmd = self._base() + ["exec-out", "screencap -p"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            return out if out and out.startswith(b"\x89PNG") else None
        except Exception as exc:  # noqa: BLE001
            log.warning("截图失败：%s", exc)
            return None

    # ---------------------------------------------------------------- #
    # 动

    def reset_task(self) -> None:
        """开始一次新任务。步数、卡死计数、中止原因全部清零。"""
        self.steps = 0
        self._last_digest = ""
        self._same_count = 0
        self.aborted_reason = ""

    def _tick(self) -> None:
        self.steps += 1
        if self.cfg.max_steps and self.steps > int(self.cfg.max_steps):
            self.aborted_reason = f"步数超过上限（{self.cfg.max_steps}）"

    async def note_screen(self, nodes: List[UiNode]) -> None:
        """记一次屏幕状态，用来发现「点了没反应」。"""
        digest = ui_digest(nodes)
        if digest == self._last_digest:
            self._same_count += 1
        else:
            self._same_count = 0
        self._last_digest = digest
        if self._same_count >= 3:
            self.aborted_reason = "连续三步屏幕没变化，判断为卡住"

    async def tap_text(self, label: str, index: int = 0) -> str:
        """按文字找控件并点它。这是最主要的动作。"""
        label = (label or "").strip()
        if not label:
            return "（没给要点的文字）"

        hit = danger_hit(label)
        if hit:
            self.aborted_reason = f"要点的东西里有危险词「{hit}」"
            return f"（不能点「{label}」——里面有「{hit}」，这种事我不做）"

        nodes = await self.ui()
        # 精确匹配优先，其次包含匹配。为什么分两档：
        # 「搜索」和「搜索历史」都包含「搜索」，但意图明显是前者。
        exact = [n for n in nodes if n.label == label]
        fuzzy = [n for n in nodes if label in n.label and n.label != label]
        pool = exact or fuzzy
        if not pool:
            # 先分清是「界面没有」还是「读不到屏幕」——
            # 这两种情况的处理方式完全不同，报错信息不能一样。
            if self._last_adb_error:
                return (
                    f"（读不到屏幕，所以找不到「{label}」。"
                    f"adb 报：{self._last_adb_error} —— 先看手机是不是掉线了）"
                )
            return f"（屏幕上找不到「{label}」，先看看现在是什么界面）"

        pool.sort(key=lambda n: (not n.clickable, -n.area))
        if index >= len(pool):
            index = 0
        node = pool[index]
        x, y = node.center
        self._tick()
        await self._adb("shell", f"input tap {x} {y}", timeout=10.0)
        await asyncio.sleep(float(self.cfg.tap_delay))
        return f"（点了「{node.label}」）"

    async def tap_xy(self, x: int, y: int) -> str:
        """兜底：按坐标点。只在她从截图里看准了位置时用。"""
        self._tick()
        await self._adb("shell", f"input tap {int(x)} {int(y)}", timeout=10.0)
        await asyncio.sleep(float(self.cfg.tap_delay))
        return f"（点了 ({x}, {y})）"

    async def swipe(self, direction: str = "up", ms: int = 0) -> str:
        """滑动。up = 手指往上推 = 内容往下翻。

        `ms` 是滑动耗时，**不同 App 的胃口完全不一样**（实测）：

        - 抖音：必须**快**，50 毫秒。300 毫秒会被当成「拖拽视频进度」
          或者干脆不响应，表现是「滑了但画面没动」。
        - B站：300 毫秒正常翻页。

        所以 ms=0（默认）时按当前前台 App 自动选。写死一个值必然有一边不好用。
        """
        w, h = await self.screen_size()
        cx, cy = w // 2, h // 2

        if ms <= 0:
            pkg = await self.current_package()
            # 抖音系（含极速版）走快滑。
            ms = 50 if "aweme" in pkg or "snssdk" in pkg else 300
            log.debug("滑动：%s -> %dms", pkg or "未知", ms)

        span = int(h * 0.28)
        direction = (direction or "up").lower()
        coords = {
            "up": (cx, cy + span, cx, cy - span),
            "down": (cx, cy - span, cx, cy + span),
            "left": (cx + span, cy, cx - span, cy),
            "right": (cx - span, cy, cx + span, cy),
        }
        if direction not in coords:
            return "（方向只能是 up/down/left/right）"
        x1, y1, x2, y2 = coords[direction]
        self._tick()
        await self._adb(
            "shell", f"input swipe {x1} {y1} {x2} {y2} {int(ms)}", timeout=10.0
        )
        await asyncio.sleep(float(self.cfg.tap_delay))
        return f"（向{direction}滑了一下）"

    async def back(self) -> str:
        self._tick()
        await self._adb("shell", "input keyevent 4", timeout=8.0)
        await asyncio.sleep(float(self.cfg.tap_delay))
        return "（按了返回）"

    async def pause_media(self) -> str:
        """用媒体键切一下播放状态，**完全不点屏幕**。

        为什么需要（2026-11-06 d1 实测）：抖音全屏视频 dump 不到 UI 树，
        原来的救法是在屏幕中心点一下暂停 —— 但那一屏如果是广告，
        这一下就把广告点下去了，模拟器直接被装了个推广游戏。

        ⚠️ **127 是 toggle，不是「暂停」**：d1 两次实测，已暂停状态下再按
        127 会变回播放。所以别把它当幂等操作反复按，要暂停请用
        `ensure_paused()`。
        """
        await self._adb("shell", "input keyevent 127", timeout=8.0)
        await asyncio.sleep(0.6)
        return "（按了媒体键）"

    async def ensure_paused(self) -> bool:
        """保证视频停住。返回 True 表示这次按过媒体键。

        判断方式（d1 2026-11-06 实测）：**能 dump 出 UI 树 = 视频没在播**，
        播放中 uiautomator 永远等不到 idle。所以先探一次，能读就什么都不做 ——
        127 是 toggle，盲补会把已经暂停的视频按成播放。
        """
        nodes = await self.ui()
        if nodes:
            return False
        await self.pause_media()
        return True

    async def paste(self) -> str:
        """把剪贴板内容粘到当前焦点输入框（KEYCODE_PASTE=279）。

        为什么需要：Android 14 的 `adb shell cmd clipboard` 没有实现
        （实测 No shell command implementation），`service call clipboard`
        又要系统权限。想读抖音「复制链接」写进剪贴板的内容，只能
        粘到输入框里再从 UI 树读出来。
        """
        self._tick()
        await self._adb("shell", "input keyevent 279", timeout=8.0)
        await asyncio.sleep(0.8)
        return "（粘贴了）"

    async def home(self) -> str:
        self._tick()
        await self._adb("shell", "input keyevent 3", timeout=8.0)
        await asyncio.sleep(float(self.cfg.tap_delay))
        return "（回到桌面）"

    async def input_text(self, text: str) -> str:
        """打字。

        **中文必须走 ADBKeyBoard 广播，不能用 `input text`。**
        实测 `adb shell input text '桃桃测试'` 直接抛
        `java.lang.NullPointerException`（退出码 1），一个字符都不进去 ——
        因为 Android 的 `input text` 只处理 ASCII。

        英文数字仍然走 `input text`（不依赖输入法，更可靠）。
        中文走 `ADB_INPUT_B64` 广播：base64 编码后整串丢过去，
        空格、引号、emoji 都不用担心被 shell 吃掉。
        """
        text = (text or "").strip()
        if not text:
            return "（没给要打的内容）"

        self._tick()

        if text.isascii():
            # 空格要换成 %s，单引号会破坏命令，直接去掉。
            safe = text.replace(" ", "%s").replace("'", "")
            await self._adb("shell", f"input text '{safe}'", timeout=10.0)
            await asyncio.sleep(float(self.cfg.tap_delay))
            return f"（输入了 {text}）"

        if not await self.has_adb_keyboard():
            return (
                "（打不了中文：手机上没装 ADBKeyBoard 输入法。"
                "装上之后才能发中文标题、回中文评论）"
            )

        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        code, out = await self._adb(
            "shell", f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", timeout=15.0
        )
        await asyncio.sleep(float(self.cfg.tap_delay))
        if "Broadcast completed" not in (out or ""):
            return f"（中文没打进去：{(out or '').strip()[:60]}）"
        return f"（输入了 {text}）"

    async def has_adb_keyboard(self) -> bool:
        """手机上有没有 ADBKeyBoard。"""
        code, out = await self._adb("shell", "pm list packages", timeout=12.0)
        return "com.android.adbkeyboard" in (out or "")

    async def use_adb_keyboard(self, on: bool = True) -> str:
        """切换输入法。

        自动化结束要切回去 —— 留着 ADBKeyBoard 当默认输入法的话，
        人自己打字时会发现键盘弹不出来（它没有界面）。
        """
        target = "com.android.adbkeyboard/.AdbIME"
        if not on:
            code, out = await self._adb("shell", "ime list -s", timeout=10.0)
            others = [
                line.strip()
                for line in (out or "").splitlines()
                if line.strip() and "adbkeyboard" not in line
            ]
            if not others:
                return "（没有别的输入法可以切回去）"
            target = others[0]

        await self._adb("shell", f"ime enable {target}", timeout=10.0)
        code, out = await self._adb("shell", f"ime set {target}", timeout=10.0)
        ok = "selected" in (out or "")
        return f"（输入法切到 {target}{'' if ok else '，但可能没生效'}）"

    async def open_app(self, keyword: str) -> str:
        """打开一个 App。keyword 可以是包名，也可以是「抖音」这种人话。

        解析顺序：别名表 → 包名 → 包名子串。**别名表必须排第一**，
        因为模型拿到的 schema 明确告诉它「填 App 名」，它就会传中文。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return "（没说要开什么）"

        allowed = list(self.cfg.allow_packages or [])
        low = keyword.lower()

        # 1. 中文名 / 常见叫法
        target = APP_ALIASES.get(low, "")

        # 2. 直接给了包名
        if not target and "." in keyword:
            target = keyword

        # 3. 退一步：包名子串（`aweme` / `bili` 这类）
        if not target:
            for pkg in allowed:
                if low in pkg.lower():
                    target = pkg
                    break

        # 能开的 App 用中文名报回去，模型下次就知道该传什么了。
        # 只报包名的话它还是会传中文。
        openable = []
        for pkg in allowed:
            names = [n for n, p in APP_ALIASES.items() if p == pkg and not n.isascii()]
            openable.append(f"{names[0]}（{pkg}）" if names else pkg)
        hint = "、".join(openable) if openable else "（白名单是空的，一个都不许开）"

        if not target:
            return f"（「{keyword}」对不上任何 App。能开的：{hint}）"

        # 白名单非空时，只允许开白名单里的 —— 防止她乱翻设置、乱开支付。
        if allowed and target not in allowed:
            return f"（「{target}」不在允许的 App 列表里。能开的：{hint}）"

        self._tick()
        code, out = await self._adb(
            "shell",
            f"monkey -p {target} -c android.intent.category.LAUNCHER 1",
            timeout=12.0,
        )
        blob = f"{out or ''}"

        # 有些精简系统没有 monkey 命令（雷电模拟器就是），
        # 直接退回 am start —— 效果一样，只是少了随机事件注入。
        #
        # 注意：报错在 **stderr**，`_adb` 只返回 stdout，所以要看
        # `_last_adb_error`。只看 blob 会漏掉（2026-10-26 踩过）。
        err_text = self._last_adb_error or ""
        if "monkey" in err_text and (
            "not found" in err_text or "inaccessible" in err_text
        ):
            log.debug("没有 monkey 命令，改用 am start 启动 %s", target)
            # 先解析出 launcher activity，再用 `am start -n` 显式启动。
            #
            # **`am start -p <包名>` 在 Android 14 上不起作用** —— 它返回
            # "Starting: Intent {...}" 看着成功，实际什么都不发生
            # （2026-10-26 在雷电模拟器上踩到，白等 25 秒）。
            code, out = await self._adb(
                "shell", f"cmd package resolve-activity --brief {target}", timeout=20.0
            )
            acts = [ln.strip() for ln in (out or "").splitlines() if "/" in ln]
            if not acts:
                return f"（{target} 没打开：解析不到启动 Activity）"
            act = acts[-1]
            code, out = await self._adb("shell", f"am start -n {act}", timeout=20.0)
            blob = f"{out or ''}"
            if "Error" in blob or "does not exist" in blob:
                return f"（{target} 没打开：{blob.strip()[:80]}）"
            await asyncio.sleep(float(self.cfg.launch_delay))
            return f"（打开了 {target}）"

        # monkey 打不开时不会返回非零退出码，只会在这两句话里抱怨。
        if "No activities found" in blob or "monkey aborted" in blob:
            return f"（{target} 没打开：手机上说找不到这个 App）"
        if code != 0:
            return f"（{target} 没打开：adb 返回 {code}）"

        await asyncio.sleep(float(self.cfg.launch_delay))
        return f"（打开了 {target}）"

    # ---------------------------------------------------------------- #
    # 给模型看的一屏

    async def _grant_permission(self, nodes: List[UiNode]) -> bool:
        """在权限弹窗上点「允许」。

        优先级：「允许」>「使用时允许」>「仅本次使用时允许」。
        为什么先挑「允许」：一次给到位，免得每次发布都要点一遍。
        这三个都没有就返回 False，让调用方按普通危险页处理。
        """
        for want in ("允许", "使用时允许", "仅本次使用时允许"):
            for node in nodes:
                if node.label.strip() == want:
                    x, y = node.center
                    await self._adb("shell", f"input tap {x} {y}", timeout=10.0)
                    return True
        return False

    async def observe(self) -> Tuple[str, List[UiNode]]:
        """看一屏，返回 (给模型的描述, 原始节点)。

        单独拆出来是因为「刷信息流」需要**原始节点**去扒卡片，
        而 `look()` 只想要那行文字。两者共用同一套安全检查 ——
        危险词、卡死、步数，一个都不能漏。
        """
        if self.aborted_reason:
            return f"__ABORT__（{self.aborted_reason}）", []

        nodes = await self.ui()

        if not nodes:
            # 抖音这类全屏视频，**播放时界面永远不 idle，dump 一定失败**。
            # 实测点一下屏幕（暂停视频）之后画面稳定，立刻就能 dump 出来
            # （0 节点 → 42 节点）。所以这里自动点一下救回来。
            #
            # 单击是暂停，不是点赞 —— 双击才点赞，所以这一步是安全的。
            # 不自动恢复播放：画面停着反而更好读，翻页也不受影响。
            pkg = await self.current_package()
            if "aweme" in pkg or "snssdk" in pkg:
                w, h = await self.screen_size()
                log.debug("抖音 dump 失败，点一下暂停再试")
                await self._adb("shell", f"input tap {w // 2} {h // 2}", timeout=8.0)
                await asyncio.sleep(1.0)
                nodes = await self.ui()

        await self.note_screen(nodes)

        if self.aborted_reason:
            return f"__ABORT__（{self.aborted_reason}）", nodes

        if not nodes:
            # UI 树是空的：视频/游戏类自绘界面。这里**不自动截图**，
            # 因为截图要额外几秒、还要多花一次视觉调用。
            # 交给她自己决定要不要看（见 screenshot）。
            return (
                "（这屏读不到文字，可能是视频或游戏界面。要看得更清楚就截个图）",
                nodes,
            )

        joined = "\n".join(n.label for n in nodes)
        hit = danger_hit(joined)
        if hit:
            # ---- 权限弹窗单独处理 ----
            # 用户 2026-10-23 授权：「凡是要权限的，你自己就可以直接点」。
            # 发布视频时抖音必须要存储权限，原来一律中止导致流程卡死。
            if is_permission_prompt(joined):
                risky = next((k for k in HIGH_RISK_PERMISSIONS if k in joined), "")
                if risky:
                    self.aborted_reason = f"要「{risky}」权限"
                    return (
                        f"__ABORT__（它要「{risky}」权限 —— 跟发视频没关系，不批）",
                        nodes,
                    )
                if getattr(self.cfg, "auto_grant_permissions", True):
                    ok = await self._grant_permission(nodes)
                    if ok:
                        log.info("手部：自动同意了权限请求")
                        await asyncio.sleep(1.5)
                        nodes2 = await self.ui()
                        await self.note_screen(nodes2)
                        if nodes2:
                            return describe_screen(nodes2), nodes2
                        return "（已同意权限，界面还在加载）", nodes2

            self.aborted_reason = f"屏幕上出现了「{hit}」"
            return f"__ABORT__（屏幕上出现了「{hit}」，这种页面我不碰）", nodes

        head = f"（第 {self.steps} 步）\n" if self.steps else ""
        return head + describe_screen(nodes), nodes

    async def peek(self) -> Tuple[str, List[UiNode]]:
        """只看一眼，**绝不动屏幕**。后台自主刷抖音专用。

        和 `observe()` 的唯一区别：dump 失败时**不点屏幕中心去暂停视频**。
        2026-11-06 实测，在抖音广告上点那一下，直接把广告点下去了 ——
        模拟器被装进一个推广游戏。后台任务里不能有「以为是暂停、实际是
        点了广告」的动作，所以单独给一个零点击版本：读不到就返回空，
        让调用方退到 OCR。

        危险词照样查；但**不自动同意权限弹窗** —— 后台没人看着她，
        权限这种事不能自己批。
        """
        if self.aborted_reason:
            return f"__ABORT__（{self.aborted_reason}）", []

        nodes = await self.ui()
        await self.note_screen(nodes)
        if self.aborted_reason:
            return f"__ABORT__（{self.aborted_reason}）", []

        if not nodes:
            return "（这屏读不到文字）", []

        joined = "\n".join(n.label for n in nodes)
        hit = danger_hit(joined)
        if hit:
            self.aborted_reason = f"屏幕上出现了「{hit}」"
            return f"__ABORT__（屏幕上出现了「{hit}」，这种页面我不碰）", []

        head = f"（第 {self.steps} 步）\n" if self.steps else ""
        return head + describe_screen(nodes), nodes

    async def read_screen_ocr(self) -> str:
        """截图 + OCR，把屏幕上的字读出来。读不到就返回空串。

        OCR 是 CPU 密集的（实测 1.5 秒/张），必须丢到线程里 ——
        直接在事件循环里跑会把整个 bot 卡住，QQ 那边会以为掉线了。
        """
        shot = await self.screenshot()
        if not shot:
            return ""
        try:
            return await asyncio.to_thread(_ocr_image_sync, shot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("手部：OCR 兜底失败（%s）", exc)
            return ""

    async def ocr_boxes(self) -> List[OcrHit]:
        """截图 + OCR，返回带坐标的文字块。读不到就返回空列表。

        什么时候用：UI 树读不到、但要点某个按钮的界面（抖音评论区最典型）。
        `look()` 只给文字，这个给「文字 + 能点的位置」。
        """
        shot = await self.screenshot()
        if not shot:
            return []
        try:
            return await asyncio.to_thread(_ocr_boxes_sync, shot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("手部：OCR 取坐标失败（%s）", exc)
            return []

    async def tap_ocr_text(self, label: str, index: int = 0) -> str:
        """按 OCR 读到的文字点屏幕。

        匹配规则和 `tap_text` 一致：精确优先，其次包含匹配。
        找不到就明说「这屏没有这几个字」并把读到的字带回去 ——
        绝不能猜坐标乱点，评论区里点错一下就是点进别人主页或者点了个赞。
        """
        label = (label or "").strip()
        if not label:
            return "（没给要点的文字）"

        hit = danger_hit(label)
        if hit:
            self.aborted_reason = f"要点的东西里有危险词「{hit}」"
            return f"（不能点「{label}」——里面有「{hit}」，这种事我不做）"

        hits = await self.ocr_boxes()
        if not hits:
            return "（OCR 没读出字来：截图失败，或者这屏确实没有文字）"

        exact = [h for h in hits if h.text == label]
        fuzzy = [h for h in hits if label in h.text and h.text != label]
        pool = exact or fuzzy
        if not pool:
            preview = " / ".join(h.text for h in hits[:8])
            return f"（屏幕上找不到「{label}」，现在这屏读到的是：{preview}）"
        if index >= len(pool):
            index = 0

        x, y = pool[index].center
        self._tick()
        await self._adb("shell", f"input tap {x} {y}", timeout=10.0)
        await asyncio.sleep(float(self.cfg.tap_delay))
        return f"（点了「{pool[index].text}」@({x},{y})）"

    async def look(self) -> str:
        """看一眼当前屏幕，返回给模型的文字描述。

        同时做三件检查：危险词、卡死、步数。任何一个触发就返回中止说明，
        调用方看到 `__ABORT__` 就该收手。

        **UI 树读不到时会退到 OCR。** 抖音的视频页、评论区、创作者中心
        全都是「读不到 UI 树」的界面，不兜底她就看不见自己正待在哪。
        """
        text, nodes = await self.observe()
        if nodes:
            return text
        if text.startswith("__ABORT__"):
            # 危险页面已经在 observe() 里判掉了，别再 OCR 一遍把危险词读回来
            return text

        ocr = await self.read_screen_ocr()
        if ocr:
            head = f"（第 {self.steps} 步）\n" if self.steps else ""
            return (
                f"{head}（这屏读不到控件，用 OCR 认了一下，按从上到下排）\n{ocr}"
            )
        return text
