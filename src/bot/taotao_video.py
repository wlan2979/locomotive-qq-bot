"""桃桃的视频生产线：首帧 → seedance（多段）→ 拼接 → 配音 → 对齐 → 合成。

产出 `VideoArtifact`，直接喂给 `douyin_publisher.DouyinPublisher`。

## 为什么是多段

seedance 单段上限 10 秒，而用户 2026-11-06 的要求是「视频时长无限制，
尽量大于 15 秒」。所以按 `seedance.clips` 段生成，每段约 10 秒，
2 段 ≈ 20 秒。多段之间**必须接得上**：

1. 第一段：ComfyUI 出的首帧 → seedance。
2. 第 N 段（N≥2）：从上一条的**最后一帧**接着生成 —— 抽末帧当首帧，
   人物、场景、光线才是连贯的，不是两段不相干的镜头。
3. ffmpeg concat 拼接。同一模型、同一参数产出的片段参数一致，`-c copy` 直接拼，
   不重编码，画质不损失。
4. 配音后按音频长度对齐：
   - 配音比画面短 → `apad` 补静音（和原来一样）。
   - 配音比画面长 → 用最后一条的末帧造一个「定格段」接在后面，
     **绝不截断人声**。
5. 成片低于 `seedance.min_seconds`（默认 15 秒）直接报错，不发。

## 这台机器的 ffmpeg 是精简版（2026-11-06 实测，别按完整版写）

B 站客户端自带的 **ffmpeg 3.0.1**：

- 有 libx264（`-encoders` 里确认过），所以定格段可以编码。
- **没有 `tpad` 滤镜**（报 `No such filter: 'tpad'`）。所以补尾不能用
  `tpad=stop_mode=clone`，只能「末帧定格段 + concat」—— 实测 20 秒素材
  加 3 秒定格段拼出来 23 秒，`-c copy` 可用。
- 没有 ffprobe，时长只能从 `ffmpeg -i` 的 stderr 里抠（见 `probe_duration`）。
- 历史 seedance 产物规格：704×1248、24fps、H.264 Constrained Baseline、
  **没有音轨**。定格段按同样的 24fps / baseline 编，拼接才不花。

## 为什么每一环都卡得这么死

2026-10-24 差点把一个**不相干的黑发路人**发到桃桃的抖音账号上 ——
那次是纯文生视频（没喂首帧），seedance 自由发挥。用户看到直接问
「这次生成的压根不是桃桃」。

所以这个模块的设计原则是：**宁可不发，也不发一个不是桃桃的视频。**
具体表现：

- 首帧**只走本机 ComfyUI + 桃桃 LoRA**，不走 `imagegen.generate()` 的分派
  （那个默认 `provider=openai`，走在线通道，画不出桃桃）。
  ComfyUI 没开就直接失败，不降级。
- 首帧步数**低于 20 直接拒绝**：8 步出的腿是崩的、手机悬空（实测）。
- `seedance.generate` 的 `first_frame` **必传**，不给默认值。
- 段数配了但不给够运动描述、或者总时长注定低于下限，直接拒绝，不做半截视频。

## 参数标定（2026-10-24 实测，别乱改）

- 首帧 20 步起，服装词要重复三遍才听话
- turbo 底模 cfg=1.0，**负面提示词完全不生效**，只能靠正面堆词
- seedance prompt 必须写「保持日式二维动画风格，赛璐璐上色」，否则往写实跑
- pro-fast **不出音频**（generate_audio 无效），配音必须自己做
- ffmpeg 合成必须 `-af apad -shortest`，否则视频被截到音频长度
- 2 段约 20 秒，口播词 70～90 字（中文语速约 4 字/秒），太短会留一大段静音
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .douyin_publisher import VideoArtifact
from .tts import MAX_CHARS, split_for_speech

log = logging.getLogger(__name__)

# 首帧步数下限。8 步出的腿是崩的、手机悬空，20 步才站得住。
MIN_FRAME_STEPS = 20

# 服装词要说三遍。turbo 模型 cfg=1.0，负面提示词不生效，
# 只能靠正面重复把权重堆上去。
_CLOTHES_HINT = "beige knit sweater, oversized sweater, long sleeves"

# 每一段 seedance prompt 都带上的画风约束。不带会往写实跑。
_STYLE_PREFIX = "保持日式二维动画风格，赛璐璐上色。"

# 定格段帧率。seedance 输出实测 704×1248@24fps（2026-11-06 复核历史 raw 产物）。
_CLIP_FPS = 24

# 配音比画面长时，定格段多留的余量（秒）。多留一点，保证人声尾巴不被吞。
_PAD_TAIL_SECONDS = 0.3


class VideoError(RuntimeError):
    """生产线任何一环失败。"""


@dataclass
class Scene:
    """一条视频的内容。

    `frame_prompt` 是给 ComfyUI 的**标签串**（英文，逗号分隔）。
    **不要写发色瞳色** —— 挂了 LoRA 时 `_generate_comfyui` 会自动补
    `comfyui_lora_appearance`，两边都写会打架。

    `motion_prompt` / `motion_prompt_2` 是给 seedance 的**中文句子**，
    描述每一段画面怎么动。这个模型吃句子不吃标签。
    `motion_prompt_2` 是第二段（约 10～20 秒）的动作，多段生成时必须给。
    """

    frame_prompt: str
    motion_prompt: str
    voice_text: str
    title: str
    desc: str
    topics: Sequence[str] = field(default_factory=tuple)
    motion_prompt_2: str = ""

    def check(self) -> None:
        if not self.frame_prompt.strip():
            raise VideoError("没给首帧的画面描述")
        if not self.motion_prompt.strip():
            raise VideoError("没给 seedance 的运动描述")
        if not self.voice_text.strip():
            raise VideoError("没给口播词")
        if len(split_for_speech(self.voice_text)) > 1:
            raise VideoError(f"视频口播词超过 {MAX_CHARS} 字，请缩短后再制作。")
        if not self.title.strip() and not self.desc.strip():
            raise VideoError("标题和描述都是空的")


@dataclass
class VideoResult:
    artifact: VideoArtifact
    raw: Path          # 拼接后、无音轨的素材
    voice: Path        # 配音 wav
    final: Path        # 合成后的成片
    duration: str = ""
    volume: str = ""
    # 每一段 seedance 的原始产物（调试/清理用）
    clips: List[Path] = field(default_factory=list)
    # 中间产物：多段之间抽的末帧、补尾用的定格段（调试/清理用）
    extras: List[Path] = field(default_factory=list)

    def line(self) -> str:
        return f"（做好了 {self.final.name}，{self.duration}，{self.volume}）"


def _duration_seconds(stamp: str) -> float:
    """把 ffmpeg 的 `00:00:20.08` 换成秒。读不出来返回 0。"""
    try:
        hours, minutes, seconds = (stamp or "").strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0


class TaotaoVideoMaker:
    """把一条 `Scene` 做成能直接发的视频。"""

    def __init__(
        self,
        cfg: Any,
        out_dir: Path,
        ffmpeg: Path,
        session: Any = None,
    ) -> None:
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.ffmpeg = Path(ffmpeg)
        self._session = session
        self._owns_session = session is None

    # ------------------------------------------------------------------ #
    # ffmpeg

    def _ffmpeg(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.ffmpeg), "-y", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def probe_duration(self, path: Path) -> str:
        """读时长。

        **这台机器没有 ffprobe**（B 站客户端自带的 ffmpeg 是精简版），
        只能从 `ffmpeg -i` 的 stderr 里抠 Duration 那一行。
        """
        r = self._ffmpeg(["-i", str(path)])
        for line in (r.stderr or "").splitlines():
            if "Duration:" in line:
                return line.split("Duration:")[1].split(",")[0].strip()
        return "?"

    def check_volume(self, path: Path) -> str:
        """音量检查。全静音的话抖音会限流，这一步不能省。"""
        r = self._ffmpeg(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
        bits = []
        for line in (r.stderr or "").splitlines():
            if "mean_volume" in line or "max_volume" in line:
                bits.append(line.split("]")[-1].strip())
        return " / ".join(bits) if bits else "读不到音量"

    # ------------------------------------------------------------------ #
    # 拼接 / 补尾

    def extract_last_frame(self, video: Path, out: Path) -> Path:
        """抽一段视频的最后一帧，给下一段当首帧。

        `-sseof -0.5` 从结尾前 0.5 秒开始解码，取一帧就是末帧附近。
        这台 ffmpeg 3.0.1 支持（2026-11-06 实测）。
        """
        out.parent.mkdir(parents=True, exist_ok=True)
        r = self._ffmpeg(
            [
                "-sseof", "-0.5",
                "-i", str(video),
                "-frames:v", "1",
                "-q:v", "2",
                str(out),
            ]
        )
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
            raise VideoError(
                f"抽不到「{Path(video).name}」的末帧：{(r.stderr or '')[-300:]}"
            )
        return out

    def freeze_segment(self, frame: Path, seconds: float, out: Path) -> Path:
        """把一张图做成 N 秒的「定格段」，接在视频尾巴上补时长。

        为什么不用 `tpad`：这台 ffmpeg 3.0.1 **没有 tpad 滤镜**
        （实测报 `No such filter: 'tpad'`）。只能先编一段定格视频再 concat。
        帧率 / 像素格式对齐 seedance 产物，拼接才不花。
        """
        seconds = max(0.2, float(seconds))
        out.parent.mkdir(parents=True, exist_ok=True)
        r = self._ffmpeg(
            [
                "-loop", "1",
                "-i", str(frame),
                "-t", f"{seconds:.2f}",
                "-r", str(_CLIP_FPS),
                "-c:v", "libx264",
                "-profile:v", "baseline",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-an",
                str(out),
            ]
        )
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
            raise VideoError(f"定格段生成失败：{(r.stderr or '')[-300:]}")
        return out

    def concat(self, parts: Sequence[Path], out: Path) -> Path:
        """把几段视频拼起来。参数一致时直接 `-c copy`，不重编码、不损失画质。

        用 ffmpeg 的 concat demuxer：它只认清单文件，每行 `file '<路径>'`。
        路径统一成 `/` —— Windows 的反斜杠在清单里会被当转义。

        只拼视频流：seedance pro-fast 的产物**没有音轨**（2026-11-06 复核历史
        raw 产物确认）。哪天模型开始吐音频，这里会因为流不一致拼失败并报错 ——
        到时候要一起改，不能装没看见。
        """
        parts = [Path(p) for p in parts if p]
        if not parts:
            raise VideoError("没有可拼接的片段")
        if len(parts) == 1:
            return parts[0]

        out.parent.mkdir(parents=True, exist_ok=True)
        listing = out.with_suffix(".txt")
        listing.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in parts),
            encoding="utf-8",
        )
        r = self._ffmpeg(
            [
                "-f", "concat",
                "-safe", "0",
                "-i", str(listing),
                "-c", "copy",
                "-fflags", "+genpts",
                str(out),
            ]
        )
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 10 * 1024:
            raise VideoError(f"拼接失败：{(r.stderr or '')[-400:]}")
        return out

    # ------------------------------------------------------------------ #
    # 首帧

    async def make_frame(self, session: Any, scene: Scene, out: Path) -> Path:
        """出首帧。**只走本机 ComfyUI**，不降级到在线通道。

        为什么不用 `imagegen.generate()`：它的分派看 `cfg.resolved_provider`，
        这台机器上配的是 `openai`，会走在线接口 —— 那条路没有桃桃的 LoRA，
        出来的就是路人。这里直接调 `_generate_comfyui`，绕开分派。
        """
        from .imagegen import ImageGenError, _generate_comfyui, probe_comfyui

        gen = self.cfg.imagegen
        if not getattr(gen, "comfyui_enable", False):
            raise VideoError("ComfyUI 通道是关的（imagegen.comfyui_enable=false），出不了首帧")
        if not (getattr(gen, "comfyui_lora", "") or "").strip():
            raise VideoError(
                "没配桃桃的 LoRA（imagegen.comfyui_lora 是空的）—— "
                "不挂 LoRA 画出来的不是她，这种视频不能发"
            )

        steps = int(getattr(gen, "comfyui_steps", 0) or 0)
        if steps and steps < MIN_FRAME_STEPS:
            raise VideoError(
                f"首帧步数是 {steps}，低于 {MIN_FRAME_STEPS} —— "
                "步数不够腿是崩的、手机会悬空，先改配置"
            )

        if not await probe_comfyui(session, gen):
            raise VideoError(
                f"本机 ComfyUI 没开（{getattr(gen, 'comfyui_url', '')}）—— "
                "出不了首帧。这种情况不降级，直接失败"
            )

        prompt = scene.frame_prompt.strip()
        log.info("出首帧：steps=%d lora=%s", steps, gen.comfyui_lora)
        t0 = time.time()
        try:
            raw = await asyncio.wait_for(
                # `portrait_9x16` = 704×1248，和手机竖屏、seedance 的 9:16 一致。
                # 别用 `portrait`（832×1216，0.68）—— 那个比例不是 9:16，
                # 当首帧喂给 seedance 会被裁掉两侧，构图偏一点人就被裁出去了。
                _generate_comfyui(session, gen, prompt, "portrait_9x16"),
                timeout=float(getattr(gen, "timeout", 300.0) or 300.0),
            )
        except asyncio.TimeoutError as exc:
            raise VideoError("首帧出图超时（ComfyUI 太慢或排队太多）") from exc
        except ImageGenError as exc:
            raise VideoError(f"首帧出图失败：{exc}") from exc

        if not raw or len(raw) < 1024:
            raise VideoError("首帧出图返回了空数据")

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        log.info("首帧好了：%s（%d KB，%.0fs）", out.name, len(raw) // 1024, time.time() - t0)
        return out

    # ------------------------------------------------------------------ #
    # 主流程

    @staticmethod
    def _data_uri(path: Path) -> str:
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    async def make(self, scene: Scene) -> VideoResult:
        scene.check()
        import aiohttp
        from . import seedance
        from .tts import VoiceError, VoiceUnavailable, synthesize

        seed_cfg = self.cfg.seedance
        duration_cfg = max(1, int(getattr(seed_cfg, "duration", 10) or 10))
        try:
            clips_want = max(1, min(4, int(getattr(seed_cfg, "clips", 1) or 1)))
        except (TypeError, ValueError):
            clips_want = 1
        min_seconds = float(getattr(seed_cfg, "min_seconds", 0) or 0)

        # 每一段的运动描述。第一段必给，第二段多段时必给。
        beats = [scene.motion_prompt.strip()]
        if scene.motion_prompt_2.strip():
            beats.append(scene.motion_prompt_2.strip())
        if len(beats) < clips_want:
            raise VideoError(
                f"配置要 {clips_want} 段（seedance.clips={clips_want}），"
                f"但只给了 {len(beats)} 拍运动描述 —— "
                "`motion_prompt_2` 是第二段的动作，多段时必须给"
            )
        beats = beats[:clips_want]

        planned = clips_want * duration_cfg
        if min_seconds and planned + 0.5 < min_seconds:
            raise VideoError(
                f"配置自相矛盾：{clips_want} 段 × {duration_cfg} 秒 = {planned} 秒，"
                f"低于成片下限 {min_seconds:.0f} 秒 —— 要么加段数，要么加单段时长"
            )

        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        session = self._session
        own = False
        if session is None:
            session = aiohttp.ClientSession()
            own = True

        try:
            # ---- 1. 首帧 -------------------------------------------
            frame = await self.make_frame(
                session, scene, self.out_dir / f"frame_{stamp}.png"
            )
            frame_uri = self._data_uri(frame)
            extras: List[Path] = []

            # ---- 2. seedance：按拍数生成多段，后一段接前一段的末帧 ----
            clip_paths: List[Path] = []
            for index, beat in enumerate(beats):
                prompt = beat
                if "赛璐璐" not in prompt:
                    prompt = _STYLE_PREFIX + prompt
                raw_video = self.out_dir / f"raw_{stamp}_{index + 1}.mp4"
                log.info(
                    "seedance 生成第 %d/%d 段（%ds）…",
                    index + 1, len(beats), duration_cfg,
                )
                t0 = time.time()
                await seedance.generate(
                    session,
                    self.cfg,
                    prompt,
                    raw_video,
                    first_frame=frame_uri,
                    duration=duration_cfg,
                    ratio=getattr(seed_cfg, "ratio", "9:16"),
                    resolution=getattr(seed_cfg, "resolution", "720p"),
                )
                if not raw_video.exists() or raw_video.stat().st_size < 10 * 1024:
                    raise VideoError(f"第 {index + 1} 段 seedance 说成功但产物是空的")
                clip_paths.append(raw_video)
                log.info(
                    "第 %d 段好了：%s（%.0fs）",
                    index + 1, raw_video.name, time.time() - t0,
                )

                # 下一段的首帧 = 这一段的末帧。不这么做两段会像两个视频。
                if index + 1 < len(beats):
                    next_frame = self.out_dir / f"frame_{stamp}_clip{index + 1}.png"
                    self.extract_last_frame(raw_video, next_frame)
                    extras.append(next_frame)
                    frame_uri = self._data_uri(next_frame)

            # ---- 3. 拼接 -------------------------------------------
            raw_all = self.concat(
                clip_paths, self.out_dir / f"raw_{stamp}.mp4"
            )
            video_sec = _duration_seconds(self.probe_duration(raw_all))
            if video_sec <= 0:
                raise VideoError(f"读不到拼接后素材的时长（{raw_all.name}）")
            log.info("拼接完成：%s（%.2f 秒）", raw_all.name, video_sec)

            # ---- 4. 配音 -------------------------------------------
            voice = self.out_dir / f"voice_{stamp}.wav"
            try:
                data = await synthesize(session, self.cfg.tts, scene.voice_text)
            except VoiceUnavailable as exc:
                raise VideoError(f"VoiceStudio 连不上（应用没开？）：{exc}") from exc
            except VoiceError as exc:
                raise VideoError(f"配音失败：{exc}") from exc
            if not data:
                raise VideoError("配音返回了空数据")
            voice.write_bytes(data)

            voice_sec = _duration_seconds(self.probe_duration(voice))
            if voice_sec <= 0:
                raise VideoError("读不到配音时长")
            log.info("配音好了：%s（%.2f 秒）", voice.name, voice_sec)

            # ---- 5. 配音对齐 ---------------------------------------
            # 配得比画面短 → 下面的 `apad` 补静音（视频长度为准）。
            # 配得比画面长 → 用末帧定格把画面补到人声长度，**绝不截断人声**。
            video_for_mux = raw_all
            if voice_sec > video_sec + 0.15:
                need = voice_sec - video_sec + _PAD_TAIL_SECONDS
                tail_frame = self.out_dir / f"frame_{stamp}_tail.png"
                self.extract_last_frame(clip_paths[-1], tail_frame)
                extras.append(tail_frame)
                frozen = self.freeze_segment(
                    tail_frame, need, self.out_dir / f"freeze_{stamp}.mp4"
                )
                extras.append(frozen)
                video_for_mux = self.concat(
                    [raw_all, frozen], self.out_dir / f"padded_{stamp}.mp4"
                )
                log.info(
                    "配音比画面长 %.2f 秒，用末帧定格补上（不截人声）",
                    voice_sec - video_sec,
                )

            # ---- 6. 合成 -------------------------------------------
            #
            # `apad` 不能省：音频通常比视频短，不补静音的话 `-shortest`
            # 会把视频截到音频长度，后半段画面直接没了。
            final = self.out_dir / f"taotao_{stamp}.mp4"
            r = self._ffmpeg(
                [
                    "-i", str(video_for_mux), "-i", str(voice),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-af", "apad", "-shortest", str(final),
                ]
            )
            if r.returncode != 0:
                raise VideoError(f"ffmpeg 合成失败：\n{(r.stderr or '')[-500:]}")
            if not final.exists() or final.stat().st_size < 10 * 1024:
                raise VideoError("合成后的文件是空的")

            duration = self.probe_duration(final)
            final_sec = _duration_seconds(duration)

            # 总时长下限：低于 `seedance.min_seconds` 宁可不发。
            if min_seconds and final_sec and final_sec < min_seconds:
                raise VideoError(
                    f"成片只有 {final_sec:.1f} 秒，低于下限 {min_seconds:.0f} 秒 —— 不发"
                )

            volume = self.check_volume(final)

            # 全静音 = 发出去会被限流。宁可失败，别发一条没声音的。
            if "mean_volume: -91" in volume or "mean_volume: -inf" in volume:
                raise VideoError(f"成片几乎是静音的（{volume}），不发")

            return VideoResult(
                artifact=VideoArtifact(
                    video=final,
                    first_frame=frame,
                    prompt="\n".join(beats),
                ),
                raw=raw_all,
                voice=voice,
                final=final,
                duration=duration,
                volume=volume,
                clips=clip_paths,
                extras=extras,
            )

        finally:
            if own:
                await session.close()

    # ------------------------------------------------------------------ #
    # 文案

    @staticmethod
    def describe(scene: Scene) -> str:
        """给日志/调试用的一行摘要。"""
        beats = 1 + (1 if scene.motion_prompt_2.strip() else 0)
        return (
            f"标题「{scene.title}」| 口播 {len(scene.voice_text)} 字 | "
            f"{beats} 拍运动 | 话题 {' '.join('#' + t for t in scene.topics) or '无'}"
        )
