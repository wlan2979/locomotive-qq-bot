"""DeepSeek 调用层。

- OpenAI 兼容协议，base_url = https://api.deepseek.com
- 模型 deepseek-flash（V4.1 Flash，原生多模态）
- 支持：文本对话、图片输入（base64 data URI）、function calling
- 瞬时错误（429/5xx/网络）自动重试，指数退避
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import aiohttp

from .config import DeepSeekConfig

log = logging.getLogger(__name__)

ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[str]]

# DeepSeek 视觉支持格式：JPEG / PNG / GIF / WebP
SUPPORTED_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp")


class LLMError(RuntimeError):
    """调用大模型失败。"""


# --------------------------------------------------------------------------- #
# 消息内容构造


def build_user_content(text: str, image_urls: Optional[Sequence[str]] = None) -> Any:
    """构造 user 消息的 content。

    没有图片就返回纯字符串（省 token、兼容性最好）；
    有图片才用 block 数组。
    """
    if not image_urls:
        return text

    blocks: List[Dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for url in image_urls:
        blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
    if not blocks:
        blocks.append({"type": "text", "text": "（用户发来一张图片）"})
    return blocks


def prepend_speaker(content: Any, speaker: str) -> Any:
    """给 user 消息的 content 前面加 `[昵称] ` 前缀。

    ## 为什么需要

    2026-10-27 用户报「她不认识人，会把两个群友当成一个人」。

    根因不在人设、也不在 system prompt 里的「对方昵称」—— 那个只说明了
    **此刻**是谁在说话。问题在**历史**：`MemoryStore` 只存 `role` + `content`，
    群聊里几个人的发言在历史里是一串没有主语的裸文本。而 OpenAI 协议的
    `user` 角色天然暗示「同一个人」，模型只能把历史里所有 user 消息
    都算到当前说话人头上。

    表现就是：A 说「我考砸了」，B 回「哈哈哈」，机器人跟着笑；下一轮 A 问
    「你笑什么」，她完全不知道刚才那句是 B 说的。

    ## 格式为什么是 `[昵称] 内容`

    · 短、像聊天记录，不占多少 token；
    · 方括号不会和正文里的中文标点混淆；
    · **不用「昵称：内容」** —— 那个跟「对话脚本」太像，模型会开始模仿，
      回复也带名字前缀。方括号更像元信息，配合 system prompt 里那句
      「回复不要带前缀」能压住。

    兼容纯字符串和多模态 block 数组两种 content。
    """
    tag = f"[{speaker}] "
    if isinstance(content, str):
        return tag + content
    if isinstance(content, list):
        out: List[Any] = []
        placed = False
        for block in content:
            # 前缀只加在**第一个 text block** 上。图片 block 前面塞文本
            # 有些兼容层会处理不好，而且读起来也别扭。
            if not placed and isinstance(block, dict) and block.get("type") == "text":
                out.append({**block, "text": tag + str(block.get("text") or "")})
                placed = True
            else:
                out.append(block)
        if not placed:
            # 整条只有图、没有文字 —— 补一个纯前缀 block。
            out.insert(0, {"type": "text", "text": tag.strip()})
        return out
    return content


def flatten_content(content: Any) -> str:
    """把多模态 content 压成纯文本，用于写入历史 —— 避免把 base64 存进记忆。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "image_url":
                parts.append("[图片]")
        return "".join(parts).strip()
    if content is None:
        return ""
    return str(content)


def guess_mime(data: bytes) -> str:
    """按文件头判断图片类型。DeepSeek 也是按实际内容判断，不看后缀。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


# --------------------------------------------------------------------------- #


class DeepSeekClient:
    def __init__(self, cfg: DeepSeekConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session

    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_executor: Optional[ToolExecutor] = None,
        max_tool_rounds: int = 3,
    ) -> str:
        """发起一轮对话，自动处理工具调用循环，返回最终文本。"""
        convo: List[Dict[str, Any]] = [dict(m) for m in messages]

        for _ in range(max_tool_rounds):
            data = await self._post(self._build_payload(convo, tools))
            message = self._first_message(data)
            tool_calls = message.get("tool_calls")

            if not tool_calls or tool_executor is None:
                return self._content_of(message)

            convo.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = self._parse_args(function.get("arguments"))
                log.info("模型请求工具：%s(%s)", name, arguments)
                try:
                    result = await tool_executor(name, arguments)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("工具 %s 执行失败", name)
                    result = f"（工具执行失败：{exc}）"
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": str(result),
                    }
                )

        # 工具轮次用尽：摘掉工具，强制它给个答案
        log.warning("工具调用达到上限，强制生成最终回复")
        data = await self._post(self._build_payload(convo, None))
        return self._content_of(self._first_message(data))

    # ------------------------------------------------------------------ #

    def _build_payload(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": float(self.cfg.temperature),
            "max_tokens": int(self.cfg.max_tokens),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        # 关键：deepseek-flash 的思维链是【默认开启】的（`GET /models` 里
        # `effort.default_level` 就是 high）。所以**两个方向都必须显式发参数**，
        # 靠省略是控制不了的 —— 不传就等于让它按默认走。
        #
        # 2026-09-30 重新实测（temperature 1.0，同一套 system prompt）：
        #   · 关掉确实省 token（3~31 vs 37~138），但**连基础算术都会错** ——
        #     「8:00 出门 + 步行 12 分 + 地铁 25 分 + 步行 8 分」答 **8:53**，
        #     开推理才是 **8:45**。
        #   · 延迟**没有变慢**（关 0.9~2.1s / 开 1.3~1.8s）。旧注释里
        #     「明显变慢，群聊场景体感很差」是更早的结论，已经不成立。
        #   · 开推理也不会磨掉人设：语气词全在，只是内容实了一截。
        #   · max_tokens 被推理吃光时 content 会返回空字符串 → 默认给 4096。
        # 因此默认 thinking=True。要省 token 再关掉。
        if urlsplit(self.cfg.base_url).hostname != "api.deepseek.com":
            return payload
        if self.cfg.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}

        return payload

    @staticmethod
    def _parse_args(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("工具参数不是合法 JSON：%s", raw[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _first_message(data: Dict[str, Any]) -> Dict[str, Any]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"响应里没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMError("响应里没有 message")
        return message

    @staticmethod
    def _content_of(message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, list):
            content = flatten_content(content)
        text = str(content or "").strip()
        if not text:
            finish = message.get("reasoning_content")
            if finish:
                log.warning("模型只返回了推理内容，没有正文")
            return "（模型没有返回内容）"
        return text

    # ------------------------------------------------------------------ #

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.cfg.api_key:
            raise LLMError("未配置模型 API key")

        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=float(self.cfg.timeout))
        attempts = 3
        last_error = "未知错误"
        address = urlsplit(self.cfg.endpoint)
        proxy = None
        if address.hostname not in ('localhost', '127.0.0.1', '::1') and not proxy_bypass(address.hostname):
            proxy = getproxies().get(address.scheme)

        for attempt in range(attempts):
            try:
                async with self.session.post(
                    self.cfg.endpoint, json=payload, headers=headers, timeout=timeout, allow_redirects=False, proxy=proxy
                ) as resp:
                    body = await resp.text()

                    if resp.status == 200:
                        try:
                            return json.loads(body)
                        except json.JSONDecodeError as exc:
                            raise LLMError(f"响应不是合法 JSON：{body[:200]}") from exc

                    last_error = f"HTTP {resp.status}: {body[:400]}"
                    retryable = resp.status in (408, 429, 500, 502, 503, 504)
                    if retryable and attempt < attempts - 1:
                        wait = 2 ** attempt
                        log.warning("DeepSeek 返回 %s，%.1fs 后重试", resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise LLMError(f"DeepSeek 请求失败：{last_error}")

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    wait = 2 ** attempt
                    log.warning("DeepSeek 网络异常（%s），%.1fs 后重试", last_error, wait)
                    await asyncio.sleep(wait)
                    continue
                raise LLMError(f"DeepSeek 网络异常：{last_error}") from exc

        raise LLMError(f"DeepSeek 请求失败：{last_error}")

    # ------------------------------------------------------------------ #

    async def ping(self) -> str:
        """连通性自检，启动时用。

        注意：这里必须显式关掉思维链，否则 max_tokens 会被推理过程吃光，
        content 返回空字符串 —— 看起来像「接口通了但模型没输出」。
        """
        data = await self._post(
            {
                "model": self.cfg.model,
                "messages": [{"role": "user", "content": "回复两个字：就绪"}],
                "max_tokens": 32,
                "stream": False,
                "thinking": {"type": "disabled"},
            }
        )
        return self._content_of(self._first_message(data))
