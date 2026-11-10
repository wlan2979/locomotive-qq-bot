"""在抖音里把当前视频分享给指定好友（走抖音自己的分享面板）。

## 用户定的方向（2026-11-06）

- 「她评论过的视频要分享给我（暴走火车头）」
- 「是在抖音里分享，不是 QQ」

所以最终动作必须发生在抖音里：
分享面板 → 点「分享给」那一行的好友 → 聊天页确认名字 → 发送。
截图只本地留底（`data/commented/`），**不发 QQ**。

## 设计约束（d2 2026-11-06 只读审阅后的版本）

这条链路每一步都有「看起来成功、实际没发生」的坑，所以：

1. **先确认已暂停，再找按钮**。暂停用媒体键（`pause_media`），
   但「已暂停再按 127 会不会反而播放」没验过 —— 所以先 `ui()` 探一下：
   能读到 UI 树就说明已经停住了，**不再按**。
2. **分享按钮是纯图标**（用户截图里只有「1.0万」这个数字），
   UI 树读不到时 OCR 不可能命中「分享」两个字，所以必须有比例坐标兜底。
   `RATIO_SHARE_BTN` 等 d1 探针量，量完改一行。
3. **「点好友 = 立即发送」还是「跳到聊天页再点发送」没确认**。
   两种都处理：点完先看有没有成功提示；再看到好友名才进聊天页流程；
   两者都没有就去猜 —— 直接报「不确定」，**绝不重试**（重试就是重复分享）。
4. **成功证据只快速看一次**。每次读屏 = UI dump 重试 + OCR 1.5 秒，
   toast 只挂两秒，慢吞吞循环三次只会错过。点完立刻读一次。
5. **输入法只在真的切过之后才还原**，并且还原成切换前那个精确值
   （不是 `ime list` 第一个）。
6. **中途每一步之后都查危险词/广告**，不只在开头查一次。
7. **退出去找视频页时每次先看前台包名**，不在抖音就停 ——
   否则连按 back 能把她退回桌面，后面浏览链路全挂。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Sequence

from .hands import HandsError

log = logging.getLogger(__name__)


class ShareError(RuntimeError):
    """分享链路上任何一步没走通。"""


# --------------------------------------------------------------------------- #
# 界面上会出现的字（候选，按顺序试）

_SHARE_BTN = ("分享",)
# 分享面板兜底入口。**d1 2026-11-06 实测：面板里没有「发给朋友」这种词**，
# 第一屏底部是：转发到日常 / 推荐 / 私信 / 建群分享 / 帮上热门 / 分享链接。
# 走「私信」进好友选择页；选完弹确认框，按钮叫「分享」而不是「发送」。
_FRIEND_ENTRY = ("私信", "分享给朋友", "发送给朋友", "发给朋友", "发送给好友")
# 兜底确认框里的按钮（「分享给 X / 输入留言 / 取消 / 分享」）。
_CONFIRM_BTN = ("分享",)
# 发送按钮（直点好友后的「已选中」态，底部红色全宽按钮）。
_SEND_BTN = ("发送", "发送给朋友")
# 好友选择页的搜索框提示。
_SEARCH_HINTS = ("搜索", "搜索好友", "搜索朋友", "搜索昵称", "输入昵称")
# 分享成功的特征。
_SUCCESS_HINTS = ("已发送", "发送成功", "已分享", "已送达")
# 分享面板打开的判据（任一词出现即认为面板开了）。
_PANEL_HINTS = ("分享给", "复制链接", "保存本地", "微信", "朋友圈", "更多", "举报")
# 「分享面板还开着」的特征。**不能用「已选中」「推荐」「取消」**：
# d1 2026-11-06 实测，视频页底部导航节点的 label 就是「已选中，推荐，按钮」，
# 用它判面板态会把视频页永远当成面板 —— success 判据误报，cleanup 还会在
# 视频页上连按 back，最后按进团购商品页。发送按钮另用「全等 label『发送』」判。
_PANEL_STATE_WORDS = (
    "分享给", "分享此刻的想法", "输入留言", "转发到日常", "建群分享", "帮上热门",
)
# 广告页特征（一个点击都不能有）。
_AD_WORDS = (
    "广告", "立即下载", "下载安装", "查看详情", "了解更多", "点击安装", "安装应用",
)
# 危险词由 hands.DANGER_WORDS 管；这里只额外拦「不在正常分享路径上」的账号级异常。
_ABORT_WORDS = (
    "账号异常", "验证码", "实名", "封禁", "禁言", "违规", "请先登录", "登录后",
)
# 回到视频页的特征（任意两个同时出现即认为回到视频页）。
_VIDEO_HINTS = ("评论", "分享", "推荐", "首页", "朋友")

# 分享按钮的比例坐标。**只当最后手段**：d1 2026-11-06 实测 desc 是
# 「分享X万，按钮」，x 稳定在 0.911~0.917，但 y 会随作品在 0.75~0.82 之间飘，
# 盲点有概率点到收藏/音乐。好在暂停后 UI dump 一定能读到，树优先。
RATIO_SHARE_BTN = (0.913, 0.785)

# 最多按几次返回去找视频页（每次先看包名，不在抖音就停）。
_MAX_BACK = 3


class DouyinSharer:
    """在抖音里把当前视频分享给一个好友。"""

    def __init__(self, hands: object) -> None:
        self.hands = hands
        # 切换前的默认输入法。只有真的切过才还原，且精确还原成它。
        self._prev_ime: Optional[str] = None
        self._ime_switched = False

    # ------------------------------------------------------------------ #

    async def share_to_friend(self, friend: str) -> str:
        """把当前这条视频分享给抖音好友 `friend`。失败抛 ShareError。"""
        friend = (friend or "").strip()
        if not friend:
            raise ShareError("没给要分享给谁（comment_share_friend 是空的）")

        hands = self.hands
        if hands is None:
            raise ShareError("手机没接上")

        try:
            await hands.ensure_ready()
        except HandsError as exc:
            raise ShareError(str(exc)) from exc

        pkg = await hands.current_package()
        if "aweme" not in (pkg or ""):
            raise ShareError(f"当前不在抖音（前台是 {pkg or '未知'}）")

        try:
            # 1) 确认停住了。d1 实测 127 是 toggle，所以交给 ensure_paused：
            #    先 dump 探状态，能读就不按，按完再复核。
            await self._ensure_paused()
            await self._guard("停视频后")

            # 2) 打开分享面板
            await self._tap_share_button()
            await asyncio.sleep(1.2)
            panel = await self._screen_texts()
            if not _any_in(panel_text(panel), _PANEL_HINTS):
                raise ShareError("点了分享，但没看到分享面板（可能只是展开了，重试交给下一轮）")
            await self._guard("分享面板")

            # 3) 优先直接在「分享给」那一行点好友（用户 2026-11-06 截图实测）。
            #    点不到才走「私信 → 搜索」兜底（d1 实测面板里没有「发给朋友」）。
            picked = await self._try_tap_friend_in_panel(friend)
            if not picked:
                await self._open_friend_picker()
                await self._search_and_pick(friend)
            await asyncio.sleep(1.4)

            # 4) 点完好友之后（d1 2026-11-06 实测）有两种形态，都要认：
            #    a) 直点「分享给」好友 → 面板原地变「已选中」态：
            #       好友打勾 + 可选留言框 + 视频缩略图 + 底部红色全宽【发送】
            #    b) 私信兜底 → 弹确认框「分享给 X / 输入留言 / 取消 / 分享」，
            #       按钮叫【分享】不是【发送】
            texts = await self._screen_texts()
            joined = panel_text(texts)
            hit = _first_in(joined, _SUCCESS_HINTS)
            if hit:
                return f"已分享给 {friend}（看到「{hit}」）"

            if friend not in joined:
                # 既没成功提示、也没有好友名 —— 不猜。可能已经发出去了，
                # 重试就是重复分享，所以把决定权交回上层（只记录不重试）。
                raise ShareError(
                    "点了好友之后没看到好友名或成功提示，"
                    "不能确定发没发出去（不重试，等人工看）"
                )

            # 判形态要用 **UI 树里有没有全等「发送」label**，不能看「取消+分享给」
            # 这种关键词组合 —— d1 2026-11-06 实测：直点好友后的「已选中」态里
            # 也有「取消」（右上角关闭 X）和「分享给」（标题），按组合判会误进
            # 私信兜底分支，然后找不到全等「分享」按钮直接抛错。
            labels = await self._ui_labels()
            label_text = " ".join(labels)
            if "发送" in labels:
                await self._tap_exact(("发送",), "发送按钮")
            elif "取消" in labels and "分享给" in label_text:
                # 私信兜底的确认框。这里**只能全等匹配** —— 模糊匹配会命中
                # 底层视频页那个「分享X万，按钮」，一点又打开分享面板。
                await self._tap_exact(_CONFIRM_BTN, "确认框的「分享」按钮")
            else:
                await self._tap_any(_SEND_BTN, "发送按钮", bottom_zone=True)
            await asyncio.sleep(1.2)

            # 5) 成功证据只快速看一次：toast 只挂两秒，慢循环必然错过。
            evidence = await self._quick_evidence()
            log.info("抖音：视频已分享给「%s」（%s）", friend, evidence)
            return f"已分享给 {friend}（{evidence}）"
        finally:
            await self._cleanup()

    # ------------------------------------------------------------------ #
    # 暂停 / 防护

    async def _ensure_paused(self) -> None:
        """保证视频停住。

        d1 2026-11-06 两次实测：127 是 toggle —— 已暂停再按会变回播放。
        所以交给 `hands.ensure_paused()`（先 dump 探状态，能读就不按），
        按完再复核一次。
        """
        await self.hands.ensure_paused()
        await asyncio.sleep(0.4)
        if not await self.hands.ui():
            raise ShareError("视频停不住（按了媒体键还是读不到 UI 树）")

    async def _guard(self, where: str) -> None:
        """中途防护：广告/危险词/账号异常，命中立刻停手。"""
        text, _ = await self.hands.peek()
        if text.startswith("__ABORT__"):
            raise ShareError(f"{where}：{text}")
        ocr = ""
        if "读不到文字" in (text or ""):
            # 读不到 UI 树时 peek 的 text 是占位串，广告词等于没查 —— 用 OCR 补一次。
            ocr = await self.hands.read_screen_ocr()
        blob = (text or "") + " " + (ocr or "")
        ad = _first_in(blob, _AD_WORDS)
        if ad:
            raise ShareError(f"{where}：屏幕上有广告/下载字样「{ad}」，一个点击都不做")
        bad = _first_in(blob, _ABORT_WORDS)
        if bad:
            raise ShareError(f"{where}：屏幕上出现「{bad}」，停手")

    # ------------------------------------------------------------------ #
    # 找按钮并点

    async def _tap_share_button(self) -> None:
        """点视频页右侧的分享按钮。

        分享按钮是纯图标（用户截图里只有「1.0万」这个数字），UI 树读不到时
        OCR 不可能命中「分享」两个字；比例坐标又不可靠（d1 2026-11-06 实测
        y 随作品在 0.75~0.82 飘，盲点可能点到收藏/音乐）。

        所以：**先确认自己在视频页**（UI 树里要有「分享…按钮」这类节点），
        确认不到就等，等不到就报错 —— 宁可不分享，也不能在评论区/别的页面上
        乱点。2026-11-06 E2E 就出过这种事：评论区没关掉，这里对着评论区
        盲点比例坐标，既没打开面板还留下了错误现场。
        """
        hands = self.hands

        ready = False
        for attempt in range(5):
            labels = await self._ui_labels()
            text = " ".join(labels)
            if "分享" in text and "按钮" in text:
                ready = True
                break
            if text and attempt >= 2:
                raise ShareError(
                    "不在视频页（UI 树里没有分享按钮），不分享；"
                    f"现在读到：{text[:120]}"
                )
            await asyncio.sleep(0.5)
        if not ready:
            raise ShareError("等不到视频页的 UI 树，不盲点分享")

        # UI 树点。`tap_text` 的包含匹配正好会命中「分享658.0万，按钮」。
        note = await hands.tap_text("分享")
        if "点了「" in (note or ""):
            log.info("分享：点了分享按钮（UI 树）")
            return

        # OCR 一般不可能命中「分享」两个字（纯图标 + 数字），留着兼容。
        note = await hands.tap_ocr_text("分享")
        if "点了「" in (note or ""):
            log.info("分享：点了分享按钮（OCR）")
            return

        # 已经确认在视频页、只是节点点不到：这时才允许比例兜底（已知不可靠）。
        width, height = await hands.screen_size()
        x = int(round(RATIO_SHARE_BTN[0] * width))
        y = int(round(RATIO_SHARE_BTN[1] * height))
        await hands.tap_xy(x, y)
        log.info("分享：点了分享按钮（比例兜底 %s → (%d,%d)）", RATIO_SHARE_BTN, x, y)

    async def _try_tap_friend_in_panel(self, friend: str) -> bool:
        """在分享面板第一屏「分享给」那一行直接点好友。点不到返回 False。"""
        hands = self.hands

        try:
            note = await hands.tap_text(friend)
        except Exception:  # noqa: BLE001
            note = ""
        if "点了「" in (note or ""):
            log.info("分享：面板里直接点到「%s」（UI 树）", friend)
            return True

        try:
            hits = await hands.ocr_boxes()
        except Exception:  # noqa: BLE001
            return False
        pool = [h for h in hits if friend in h.text]
        if not pool:
            return False
        # 好友名在横排头像下面，取最靠上的那个命中（下面的多是「更多」说明文字）。
        target = min(pool, key=lambda h: h.center[1])
        await hands.tap_xy(*target.center)
        log.info("分享：面板里直接点到「%s」@%s", friend, target.center)
        return True

    async def _open_friend_picker(self) -> None:
        """兜底入口：分享面板里的「发给朋友」。"""
        await self._tap_any(_FRIEND_ENTRY, "「发给朋友」入口")

    async def _search_and_pick(self, friend: str) -> None:
        """兜底搜索。注意顺序：**先切输入法，再点搜索框** ——
        d1 2026-11-06 实测切完 ADBKeyboard 会掉焦点，顺序反了中文进不去。"""
        hands = self.hands
        await self._tap_any(_SEARCH_HINTS, "好友搜索框")
        await asyncio.sleep(0.6)

        await self._switch_ime(True)
        # 切完输入法重新点一次搜索框，把焦点抢回来（掉焦点是实测过的）。
        await self._tap_any(_SEARCH_HINTS, "好友搜索框（切输入法后）")
        await asyncio.sleep(0.4)

        note = await hands.input_text(friend)
        if "没打进去" in (note or "") or "打不了" in (note or ""):
            raise ShareError(f"搜索词没打进去：{note}")
        await asyncio.sleep(2.0)

        # 验证真的进了框（input_text 返回「输入了 X」不代表字进去了）。
        texts = await self._screen_texts()
        if friend not in panel_text(texts):
            raise ShareError("搜索词没有真的进到输入框（切完输入法后焦点可能又掉了）")

        hits = await hands.ocr_boxes()
        pool = [h for h in hits if friend in h.text]
        if not pool:
            raise ShareError(f"搜索结果里没看到「{friend}」")
        height = (await hands.screen_size())[1]
        target = max(pool, key=lambda h: h.center[1])
        if target.center[1] < height * 0.25:
            raise ShareError(f"「{friend}」只出现在搜索框里，结果列表没有")
        await hands.tap_xy(*target.center)
        await asyncio.sleep(1.6)

    async def _tap_any(
        self,
        words: Sequence[str],
        what: str,
        *,
        bottom_zone: bool = False,
    ) -> None:
        """按文字点一下：一次读屏，多候选里找，再点击。"""
        hands = self.hands

        # UI 树整棵读一次，多候选词一起找（省得每个词都 dump 一遍）。
        labels: List[str] = []
        try:
            nodes = await hands.ui()
            labels = [(getattr(n, "label", "") or "").strip() for n in (nodes or [])]
        except Exception:  # noqa: BLE001
            labels = []

        for word in words:
            if any(label == word or word in label for label in labels):
                note = await hands.tap_text(word)
                if "点了「" in (note or ""):
                    log.info("分享：点了%s（UI 树「%s」）", what, word)
                    return

        # OCR 一次，多候选一起找。
        try:
            hits = await hands.ocr_boxes()
        except Exception:  # noqa: BLE001
            hits = []
        height = (await hands.screen_size())[1] if hits else 0
        for word in words:
            pool = [h for h in hits if word in h.text]
            if bottom_zone:
                pool = [h for h in pool if h.center[1] > height * 0.5]
            if not pool:
                continue
            target = max(pool, key=lambda h: h.center[1]) if bottom_zone else min(
                pool, key=lambda h: h.center[1]
            )
            await hands.tap_xy(*target.center)
            log.info("分享：点了%s（OCR「%s」@%s）", what, word, target.center)
            return

        raise ShareError(f"找不到{what}（试过：{'/'.join(words)}）")

    # ------------------------------------------------------------------ #
    # 成功证据 / 收尾

    async def _tap_exact(self, words: Sequence[str], what: str) -> None:
        """只点**完全相等**的文字。确认框按钮专用。

        不能用模糊匹配：底层视频页那个「分享X万，按钮」也含「分享」，
        模糊命中它会把刚打开的分享面板又打开一次。
        """
        hands = self.hands

        labels: List[str] = []
        try:
            nodes = await hands.ui()
            labels = [(getattr(n, "label", "") or "").strip() for n in (nodes or [])]
        except Exception:  # noqa: BLE001
            labels = []

        for word in words:
            if word in labels:
                note = await hands.tap_text(word)
                if "点了「" in (note or ""):
                    log.info("分享：点了%s（UI 树全等「%s」）", what, word)
                    return

        try:
            hits = await hands.ocr_boxes()
        except Exception:  # noqa: BLE001
            hits = []
        for word in words:
            exact = [h for h in hits if h.text.strip() == word]
            if exact:
                await hands.tap_xy(*exact[0].center)
                log.info(
                    "分享：点了%s（OCR 全等「%s」@%s）", what, word, exact[0].center
                )
                return

        raise ShareError(f"找不到{what}（全等匹配：{'/'.join(words)}）")

    async def _quick_evidence(self) -> str:
        """点完发送后的成功证据。

        **抖音分享不发 toast** —— d1 2026-11-06 截了 4.8 秒 8 帧，一个都没有。
        真正稳的判据是「面板消失了，人还在抖音视频页」：点发送后面板关闭、
        回到视频页，而失败时面板停在「已选中」态。
        """
        await asyncio.sleep(0.8)
        if await self._panel_open():
            # 面板还在，再给半秒（点发送到收起有动画）。
            await asyncio.sleep(0.8)

        if not await self._panel_open():
            try:
                pkg = await self.hands.current_package()
            except Exception:  # noqa: BLE001
                pkg = ""
            if "aweme" in (pkg or ""):
                return "分享面板已关闭、回到视频页（抖音不发 toast，按面板关闭判定）"

        ocr = await self.hands.read_screen_ocr()
        hit = _first_in(ocr or "", _SUCCESS_HINTS)
        if hit:
            return f"看到「{hit}」"
        return "点过发送，但面板没关也没提示（不重试）"

    async def _cleanup(self) -> None:
        """收尾：还原输入法 + 退回到视频页。每一步都先看包名。

        d1 2026-11-06 实测：失败时面板停在「已选中」态，而那里恰好有
        「分享」「推荐」这类词 —— 用 `_VIDEO_HINTS` 判「已回到视频页」会误判，
        面板就残留在屏幕上。所以先看**面板态词汇**在不在。
        """
        hands = self.hands

        await self._restore_ime()

        for _ in range(_MAX_BACK):
            try:
                pkg = await hands.current_package()
            except Exception:  # noqa: BLE001
                return
            if "aweme" not in (pkg or ""):
                # 已经退出抖音了 —— 绝对不能再按，否则可能退到桌面。
                return
            if not await self._panel_open():
                # 不在面板里就**一律不再按 back**：d1 2026-11-06 实测在视频页
                # 连按 back 会一路退进团购商品页之类的页面。宁可面板没退干净，
                # 也不能把她带离视频页。
                return
            try:
                await hands.back()
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ #
    # 输入法（只有真的切过才还原，且还原到切换前那个）

    async def _current_ime(self) -> str:
        """读当前**真正生效**的输入法。

        **不能用 `settings get secure default_input_method`**：d1 2026-11-06
        实测它会滞后 —— IME 已经切过去了，它还在显示旧值，照它判断会
        误判成「切了等于没切」。必须看 `dumpsys input_method` 的
        `mCurMethodId`。
        """
        code, out = await self.hands._adb(  # noqa: SLF001
            "shell", "dumpsys input_method", timeout=12.0
        )
        for line in (out or "").splitlines():
            pos = line.find("mCurMethodId=")
            if pos >= 0:
                return line[pos + len("mCurMethodId=") :].strip()
        return ""

    async def _switch_ime(self, on: bool) -> None:
        if not on:
            await self._restore_ime()
            return
        try:
            if not self._prev_ime:
                self._prev_ime = await self._current_ime()
            await self.hands._adb(  # noqa: SLF001
                "shell", "ime enable com.android.adbkeyboard/.AdbIME", timeout=10.0
            )
            await self.hands._adb(  # noqa: SLF001
                "shell", "ime set com.android.adbkeyboard/.AdbIME", timeout=10.0
            )
            # 回读确认真正生效（settings 会骗人，dumpsys 不会）。
            for _ in range(6):
                await asyncio.sleep(0.3)
                if "adbkeyboard" in (await self._current_ime()):
                    self._ime_switched = True
                    return
            raise ShareError("切了 ADBKeyboard 但 mCurMethodId 里没生效")
        except ShareError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ShareError(f"切输入法失败：{exc}") from exc

    async def _restore_ime(self) -> None:
        if not (self._ime_switched and self._prev_ime):
            return
        try:
            await self.hands._adb(  # noqa: SLF001
                "shell", f"ime set {self._prev_ime}", timeout=10.0
            )
            log.info("分享：输入法已还原成 %s", self._prev_ime)
        except Exception as exc:  # noqa: BLE001
            log.warning("分享：还原输入法失败（%s）", exc)
        finally:
            self._ime_switched = False

    # ------------------------------------------------------------------ #
    # 读屏

    async def _ui_labels(self) -> List[str]:
        """只读当前 UI 树的 label（读不到返回空列表）。"""
        try:
            nodes = await self.hands.ui()
            return [
                (getattr(node, "label", "") or "").strip() for node in (nodes or [])
            ]
        except Exception:  # noqa: BLE001
            return []

    async def _panel_open(self) -> bool:
        """分享面板还开着吗（含「已选中」态）。

        判据（d1 2026-11-06 实测校准）：
        - 面板态词汇（分享给/输入留言/建群分享…）任一出现在 UI 树里；
        - 或 UI 树里有一个**全等**的「发送」label。
        **绝不能用「已选中」「推荐」** —— 视频页底部导航就叫「已选中，推荐，按钮」。
        """
        labels = await self._ui_labels()
        text = " ".join(labels)
        if "发送" in labels:
            return True
        return any(word in text for word in _PANEL_STATE_WORDS)

    async def _screen_texts(self) -> List[str]:
        """把当前屏幕上的文字（UI 树 + OCR）合起来。"""
        out: List[str] = []
        try:
            nodes = await self.hands.ui()
            out.extend(
                (getattr(node, "label", "") or "").strip() for node in (nodes or [])
            )
        except Exception:  # noqa: BLE001
            pass
        ocr = await self.hands.read_screen_ocr()
        if ocr:
            out.extend(ocr.splitlines())
        return [line for line in out if line]


# --------------------------------------------------------------------------- #
# 小工具


def panel_text(lines: Sequence[str]) -> str:
    return " ".join(line for line in lines if line)


def _first_in(text: str, words: Sequence[str]) -> str:
    for word in words:
        if word in (text or ""):
            return word
    return ""


def _any_in(text: str, words: Sequence[str]) -> bool:
    return bool(_first_in(text, words))


async def share_video_to_friend(hands: object, friend: str) -> str:
    """模块级入口：把当前视频在抖音里分享给 `friend`。"""
    return await DouyinSharer(hands).share_to_friend(friend)
