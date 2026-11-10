"""联网搜索工具（博查 AI 开放平台）。

以 function calling 的形式暴露给模型，由模型自己决定要不要搜。
博查文档：https://open.bochaai.com/
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import aiohttp

from .config import SearchConfig

log = logging.getLogger(__name__)

# 给模型看的工具声明
WEB_SEARCH_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索，获取实时或你不确定的信息。"
            "当用户询问新闻、时事、天气、股价、比赛结果、最新版本号、"
            "或者任何可能超出你知识范围的事实性问题时，调用这个工具。"
            "日常闲聊、常识、写代码不需要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。用简洁的关键词，不要写成一整句话。",
                }
            },
            "required": ["query"],
        },
    },
}


class SearchError(RuntimeError):
    pass


async def bocha_search(
    session: aiohttp.ClientSession,
    cfg: SearchConfig,
    query: str,
    max_chars: int = 4000,
) -> str:
    """调用博查 Web Search，返回给模型看的纯文本摘要。"""
    if not cfg.api_key:
        return "（联网搜索不可用：未配置 search.api_key）"

    query = (query or "").strip()
    if not query:
        return "（搜索关键词为空）"

    payload = {
        "query": query,
        "freshness": "noLimit",
        "summary": True,
        "count": int(cfg.count),
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with session.post(
            cfg.endpoint,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=float(cfg.timeout)),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                log.warning("博查搜索返回 %s：%s", resp.status, body[:300])
                return f"（联网搜索失败：HTTP {resp.status}）"
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                import json as _json

                data = _json.loads(body)
    except aiohttp.ClientError as exc:
        log.warning("博查搜索网络错误：%s", exc)
        return f"（联网搜索失败：{exc}）"
    except Exception as exc:  # noqa: BLE001
        log.exception("博查搜索异常")
        return f"（联网搜索异常：{exc}）"

    results = _extract_results(data)
    if not results:
        return f"（没有搜到「{query}」相关结果）"

    lines: List[str] = [f"以下是「{query}」的搜索结果："]
    for idx, item in enumerate(results, 1):
        title = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        date = str(item.get("datePublished") or "").strip()[:10]
        summary = str(item.get("summary") or item.get("snippet") or "").strip()
        if len(summary) > 600:
            summary = summary[:600] + "…"

        lines.append(f"\n[{idx}] {title}")
        if date:
            lines.append(f"    时间：{date}")
        if url:
            lines.append(f"    链接：{url}")
        if summary:
            lines.append(f"    摘要：{summary}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（结果过长已截断）"
    return text


def _extract_results(data: Any) -> List[Dict[str, Any]]:
    """兼容博查可能出现的几种返回结构。"""
    if not isinstance(data, dict):
        return []

    pages = data.get("webPages") or data.get("web_pages")
    if isinstance(pages, dict):
        value = pages.get("value")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]

    value = data.get("value")
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]

    return []
