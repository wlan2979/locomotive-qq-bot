"""图片预处理：把 QQ 图床吐出来的东西整理成模型能稳定吃下的格式。

为什么需要这一层
----------------
QQ 图床返回的格式基本没法预测，实测（2026-09-29）：

  * 文件后缀是 `.jpg`，实际内容是 PNG —— 同一个表情包，本地缓存是
    254KB 的 JPEG，CDN 返回的却是 1.88MB 的 PNG；
  * 另一张 `.jpg`，CDN 返回 GIF；
  * 腾讯在大规模使用 AVIF，而 `guess_mime()` 会把不认识的格式一律
    兜底成 `image/jpeg` —— **mime 和内容对不上，服务端直接 400**，
    错误信息还是用户看不懂的 "unsupported image"。

排查结论：JPEG / PNG / GIF / APNG / 16 位灰度 / 带透明 / 4000x4000 /
5.7MB 这些维度**单独测都能过**。所以 400 的成因不在常见维度上，而是
那张图本身有问题。

最可疑的是**动画格式**：QQ 表情包大量使用 APNG 和动画 WebP，
而这两种的文件头跟静态版本一模一样（APNG 就是 PNG，动画 WebP 就是
WebP）。`guess_mime()` 只能看文件头，判出来是 `image/png` /
`image/webp`，服务端一解析发现是动画 → 拒绝。
这一层天然治得了它：只要不是 GIF，一律解成静帧重编码。

所以这里不再猜「哪种格式会被拒」，而是：

  解得开 → 图是好的 → 重编码成 JPEG，彻底消除源图隐患
  解不开 → 图是坏的 → 明确丢弃并留证，而不是发出去换个 400

唯一的例外是 GIF 动图（DeepSeek 明确支持），但体积或帧数失控时
也退化成静态 JPEG，避免请求体撑爆。

顺带的好处：QQ 原图动辄 2MB，base64 之后请求体还要膨胀三分之一。
统一压到 JPEG（最长边 1568）之后，请求体和 token 都降一个数量级。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageStat, UnidentifiedImageError

from .llm import guess_mime

log = logging.getLogger(__name__)

# 最长边上限。超过就等比缩小 —— 既省 token，也避开超大图被服务端拒绝。
MAX_SIDE = 1568
JPEG_QUALITY = 88

# 动图（GIF）原样放行的上限。超了就退化成首帧静态 JPEG：
# 一张 10MB / 300 帧的 GIF，base64 之后请求体 13MB+，服务端要么拒要么超时，
# 而且 token 成本高得离谱。动图的意义在于「看到动作」，量级失控时不如保静态。
GIF_MAX_BYTES = 4 * 1024 * 1024
GIF_MAX_FRAMES = 150

# 坏图留证目录（相对项目根），只在解码失败时写入，平时不产生文件
_FAILED_DIR = "failed_images"


def _is_animated(img: Image.Image) -> bool:
    try:
        return bool(getattr(img, "is_animated", False))
    except Exception:  # noqa: BLE001
        return False


def _frame_count(img: Image.Image) -> int:
    try:
        return int(getattr(img, "n_frames", 1))
    except Exception:  # noqa: BLE001
        return 1


def _gif_ok(raw: bytes, img: Image.Image) -> bool:
    """动图值不值得原样发。太大或帧太多就退化。"""
    if len(raw) > GIF_MAX_BYTES:
        log.info("GIF 体积 %.1fMB 超过 %.1fMB 上限，退化成静态图", len(raw) / 1024 / 1024, GIF_MAX_BYTES / 1024 / 1024)
        return False
    frames = _frame_count(img)
    if frames > GIF_MAX_FRAMES:
        log.info("GIF 帧数 %d 超过 %d 上限，退化成静态图", frames, GIF_MAX_FRAMES)
        return False
    return True


def _pick_backdrop(rgba: Image.Image) -> tuple:
    """给带透明通道的图挑一个底色。

    ## 为什么不能一律铺白

    这是 2026-10-22 实测踩到的：**表情包上的字看不见了**。

    QQ 表情包大量是「透明底 + 单色文字 + 描边」的 PNG。
    原来一律铺白底，于是「白色字 + 透明底」那种表情包 ——
    白字铺到白底上，**整个字直接消失**，模型只能看到描边的空壳，
    自然读不出写了什么。用户看到的就是「她看不见表情包上的字」。

    同理铺黑底会让「黑字 + 透明底」的消失。所以底色必须按**字本身**选：

    - 字偏亮（浅色/白色）→ 铺黑底
    - 字偏暗（深色/黑色）→ 铺白底
    - 中间调（彩色图/照片）→ 铺白底（默认，最常见也最安全）

    「字色」用 alpha 加权平均亮度估 —— 只统计真正可见的像素，
    透明区域不参与，否则一张字只占 5% 的图会被大片透明拉偏。
    """
    try:
        gray = rgba.convert("L")
        alpha = rgba.split()[-1]
        # mask=alpha 让统计只落在不透明的地方。
        mean = ImageStat.Stat(gray, alpha).mean[0]
    except Exception:  # noqa: BLE001
        return (255, 255, 255)

    if mean > 170:
        # 字很亮 → 黑底才看得见
        return (0, 0, 0)
    # 其余一律白底。中间调铺灰的话，白字黑字都不够清楚。
    return (255, 255, 255)


def _to_jpeg(img: Image.Image) -> Optional[bytes]:
    """缩放 + 铺底 + 编码 JPEG。"""
    try:
        width, height = img.size
        if max(width, height) > MAX_SIDE:
            scale = MAX_SIDE / max(width, height)
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            img = img.resize(new_size, Image.LANCZOS)

        # JPEG 没有透明通道，必须先铺底。底色按内容选，见 _pick_backdrop。
        if img.mode in ("RGBA", "LA", "P", "PA"):
            rgba = img.convert("RGBA")
            backdrop = _pick_backdrop(rgba)
            canvas = Image.new("RGB", rgba.size, backdrop)
            canvas.paste(rgba, mask=rgba.split()[-1])
            img = canvas
        else:
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("图片转码 JPEG 失败：%s", exc)
        return None


def _trim_bottom(img: Image.Image, px: int) -> Image.Image:
    """从底部裁掉一条。

    用途只有一个：抹掉生图服务盖在右下角的广告水印
    （Pollinations 的 `nologo=true` 实测无效，只能物理裁掉）。
    用户发来的图永远传 0，不会走到这里。
    """
    width, height = img.size
    # 兜底：最多裁掉高度的 15%，且必须留下足够的内容。
    # 参数写错了也不该把整张图裁没。
    cut = min(int(px), int(height * 0.15))
    if cut <= 0 or height - cut < 16:
        return img
    log.debug("底部裁掉 %d 像素（原高 %d）", cut, height)
    return img.crop((0, 0, width, height - cut))


def _keep_evidence(raw: bytes, reason: str) -> None:
    """解码失败的图存一份，方便事后查 —— 不然下次还是只能靠猜。"""
    try:
        base = Path(__file__).resolve().parent.parent
        target = base / _FAILED_DIR
        target.mkdir(parents=True, exist_ok=True)
        import hashlib

        digest = hashlib.md5(raw).hexdigest()[:16]
        path = target / f"{digest}.bin"
        if not path.exists():
            path.write_bytes(raw)
            log.error("已把无法解析的图片留证：%s（%s，%d 字节）", path, reason, len(raw))
    except Exception as exc:  # noqa: BLE001
        log.warning("留证失败：%s", exc)


def prepare(raw: bytes, trim_bottom_px: int = 0) -> Optional[tuple[str, bytes]]:
    """把原始图片字节整理成可塞进 data URI 的 (mime, data)。

    返回 None 表示这张图救不了，调用方应当跳过它并告知用户，
    **不要**把坏数据发给模型 —— 那只会换来一个看不懂的 400。

    `trim_bottom_px` > 0 时先从底部裁掉一条，用来去掉生图服务的水印。
    用户发来的图不传这个参数，行为跟以前完全一致。
    """
    if not raw:
        return None

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # 真正解码。损坏的图在这一步就会抛出来。
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.warning("图片解不开（%s），丢弃", exc)
        _keep_evidence(raw, str(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("图片解码异常（%s），丢弃", exc)
        _keep_evidence(raw, str(exc))
        return None

    # 裁水印只在静态图上做。动图（GIF / APNG / 动画 WebP）一裁就只剩单帧，
    # 而生成图不会是动图 —— 所以直接跳过，别把动图能力裁没了。
    if trim_bottom_px > 0 and not _is_animated(img):
        img = _trim_bottom(img, trim_bottom_px)

    original = guess_mime(raw)
    width, height = img.size

    # 动图原样保留 —— 压成静帧会丢掉动作信息，而 DeepSeek 本身支持 GIF。
    # 但体积/帧数失控时退化，见 _gif_ok。
    if original == "image/gif" and _is_animated(img) and _gif_ok(raw, img):
        log.info(
            "图片：GIF 动图 %dx%d %d 帧 %d 字节，原样发送",
            width,
            height,
            _frame_count(img),
            len(raw),
        )
        return "image/gif", raw

    jpeg = _to_jpeg(img)
    if jpeg is None:
        _keep_evidence(raw, "转码失败")
        return None

    log.info(
        "图片：%s %dx%d %d 字节 → JPEG %d 字节",
        original,
        width,
        height,
        len(raw),
        len(jpeg),
    )
    return "image/jpeg", jpeg
