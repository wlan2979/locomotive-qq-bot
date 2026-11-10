"""豆包 Seedance 视频生成（火山方舟）。

## 为什么加这个：H3 的图生视频会把二次元重绘成写实

2026-10-23 实测对比，**同一张首帧、同一段描述**：

| | MiniMax H3 | Seedance 1.0 pro fast |
|---|---|---|
| 出片耗时 | 113~184 秒 | **16 秒** |
| 画风 | **写实照片**（真人皮肤、真实毛衣） | **日式二维动画** |
| 角色保持 | **完全丢失**（黑发双马尾 → 棕色长发绿毛衣） | **高度保留**（发色、校服、过膝袜、乐福鞋全在） |
| 文件 | 1.8 MB / 768x1344 | 4.8 MB / 704x1248 |

H3 的 `first_frame_image` 是「照这张图重画」，不是「让这张图动」，
所以画风和角色都会漂。Seedance 首帧是真正的首帧 —— 第一帧就是喂进去的那张。

**这是「视频里有桃桃本人」唯一跑通的方案。**

## 接口和 H3 完全不同

- 鉴权：`Bearer <Ark API Key>`（就是 config 里 `imagegen.api_key` 那个，同一个账号）
- 建任务：`POST /api/v3/contents/generations/tasks`
- 查任务：`GET  /api/v3/contents/generations/tasks/{id}`
- 首帧：放在 `content` 数组里，`{"type":"image_url","role":"first_frame"}`
- 结果：`content.video_url`

## 模型名

`doubao-seedance-1-0-pro-fast-251015`（已开通）。
`doubao-seedance-2-5-260628` 存在但账号没开通。
这两个之外的写法一律 404 `InvalidEndpointOrModel.NotFound` ——
模型名带日期后缀，写错一个数字就找不到。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

BASE = "https://ark.cn-beijing.volces.com/api/v3"
CREATE = f"{BASE}/contents/generations/tasks"


class SeedanceError(RuntimeError):
    pass


@dataclass
class VideoResult:
    task_id: str
    status: str
    url: str = ""
    duration: int = 0
    ratio: str = ""
    resolution: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "succeeded" and bool(self.url)


def _headers(cfg: Config) -> Dict[str, str]:
    key = (cfg.imagegen.api_key or "").strip()
    if not key:
        raise SeedanceError("没配 imagegen.api_key（火山方舟的 key）")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def build_payload(
    cfg: Config,
    prompt: str,
    first_frame: Optional[str] = None,
    duration: int = 5,
    ratio: str = "9:16",
    resolution: str = "720p",
    generate_audio: bool = True,
) -> Dict[str, Any]:
    """拼请求体。

    `first_frame` 是 data URI。给了就是图生视频，不给就是文生视频。

    **画风约束要写进 prompt。** Seedance 比 H3 稳，但也不是完全不动画风 ——
    实测首帧是二次元时它会保持，但如果 prompt 里出现「真实」「照片」这类词，
    它会跟着往写实走。所以调用方应该显式写「保持日式二维动画风格，赛璐璐上色」。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise SeedanceError("提示词是空的")

    content: list = [{"type": "text", "text": prompt}]
    if first_frame:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": first_frame},
                "role": "first_frame",
            }
        )

    return {
        "model": cfg.seedance.model,
        "content": content,
        "ratio": ratio,
        "duration": int(duration),
        "resolution": resolution,
        "generate_audio": bool(generate_audio),
        "watermark": False,
    }


def parse_result(task_id: str, data: Dict[str, Any]) -> VideoResult:
    status = str(data.get("status") or "").strip().lower()
    content = data.get("content") or {}
    return VideoResult(
        task_id=task_id,
        status=status,
        url=str(content.get("video_url") or ""),
        duration=int(data.get("duration") or 0),
        ratio=str(data.get("ratio") or ""),
        resolution=str(data.get("resolution") or ""),
        error="" if status in ("running", "succeeded") else str(data.get("error") or ""),
    )


async def create_task(
    session: aiohttp.ClientSession, cfg: Config, payload: Dict[str, Any]
) -> str:
    status = 0
    data: Any = {}
    try:
        async with session.post(
            CREATE,
            json=payload,
            headers=_headers(cfg),
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            status = resp.status
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        raise SeedanceError(f"创建任务失败：{exc}") from exc

    if status != 200:
        err = (data or {}).get("error") or data
        raise SeedanceError(f"创建任务 HTTP {status}：{str(err)[:300]}")

    task_id = str((data or {}).get("id") or "")
    if not task_id:
        raise SeedanceError(f"没拿到任务 id：{str(data)[:300]}")
    log.info("Seedance：任务已创建 %s", task_id)
    return task_id


async def query_task(
    session: aiohttp.ClientSession, cfg: Config, task_id: str
) -> VideoResult:
    try:
        async with session.get(
            f"{CREATE}/{task_id}",
            headers=_headers(cfg),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        raise SeedanceError(f"查询任务失败：{exc}") from exc
    return parse_result(task_id, data or {})


async def wait_task(
    session: aiohttp.ClientSession, cfg: Config, task_id: str
) -> VideoResult:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(cfg.seedance.timeout)
    interval = max(3.0, float(cfg.seedance.poll_interval))

    last = VideoResult(task_id=task_id, status="unknown")
    while loop.time() < deadline:
        await asyncio.sleep(interval)
        last = await query_task(session, cfg, task_id)
        if last.status == "succeeded":
            if not last.url:
                raise SeedanceError(f"成功但没给视频地址：{task_id}")
            return last
        if last.status == "failed":
            raise SeedanceError(f"生成失败（{task_id}）：{last.error or '没给原因'}")

    raise SeedanceError(f"等超时了（{cfg.seedance.timeout:.0f} 秒），任务还在 {last.status}")


async def download(session: aiohttp.ClientSession, url: str, path: Path) -> Path:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            if resp.status != 200:
                raise SeedanceError(f"下载失败 HTTP {resp.status}")
            data = await resp.read()
    except SeedanceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SeedanceError(f"下载失败：{exc}") from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    log.info("Seedance：已保存 %s（%.1f MB）", path.name, len(data) / 1048576)
    return path


async def generate(
    session: aiohttp.ClientSession,
    cfg: Config,
    prompt: str,
    out_path: Path,
    first_frame: Optional[str] = None,
    duration: int = 5,
    ratio: str = "9:16",
    resolution: str = "720p",
) -> VideoResult:
    """一条龙：建任务 → 等 → 下载。实测 5 秒 720p 约 16 秒出片。"""
    if not cfg.seedance.enable:
        raise SeedanceError("seedance.enable 是关的")
    payload = build_payload(cfg, prompt, first_frame, duration, ratio, resolution)
    task_id = await create_task(session, cfg, payload)
    result = await wait_task(session, cfg, task_id)
    await download(session, result.url, out_path)
    return result
