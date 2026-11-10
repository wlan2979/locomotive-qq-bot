"""灵感：她刷到的东西怎么变成「下次拍什么」。

## 为什么要这一层

直接让模型「想一个日常场景」，它会给你「樱花树下的少女」「阳光洒进窗户」——
那是模型认为「该拍」的，不是她真的想拍的。而**她刷视频时看到的东西**才是
真实的内容土壤：什么标题有人看、什么结构让人想点。

所以链路是：

    刷 B站 → 卡片进日记 → 从标题里提取「内容结构」→ 存成模板
        ↓
    生成新内容时，随机抽一个模板当骨架，再填她自己的日常

## 模板是什么，不是什么

模板是**结构**，不是内容。比如：

    好的模板：{"pattern": "满怀期待地做一件事 → 被无视/失败", "why": "反差"}
    不是模板：{"title": "逗猫被猫嫌弃"}   ← 这是内容，抄了就是盗

这条界线必须守住。抄结构是创作，抄内容就是搬运。

## 为什么要 LLM 提取而不是关键词

标题里的结构是隐含的。「蹲下拿饭团逗猫，猫闻了一下扭头走了」——
关键词只能看出「猫」「饭团」，看不出「期待落空」这个骨架。
只有语义模型能抽出来。
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


TEMPLATE_SYSTEM = """你在分析短视频标题，提取「可复用的内容结构」。

给一串标题，先**筛掉**不能用的，再从能用的里面抽 3~5 个结构。

## 第一步：筛掉 AI 生成不了的（这一步最重要）

用户是用 AI 画图 + AI 图生视频做内容的，所以只保留**一个角色、一个场景、
5 秒内一个动作**能拍出来的东西。下面这些全部筛掉：

- 真人实拍、vlog、探店、街拍、素人日常
- 游戏录屏、影视剪辑、动漫片段、解说
- 多人互动、对话、群体活动
- 需要长镜头或复杂物理的：做饭全过程、运动比赛、开箱评测、教程
- 需要真实人脸、真实地标、真实商品的
- 依赖声音/台词/字幕才能成立的笑点

留下的必须是：**单个人物 + 简单场景 + 一个动作 + 一个小转折**。

## 第二步：抽结构（不是内容）

什么叫结构：
- 「满怀期待地做一件事，结果被无视/失败」← 结构
- 「逗猫被猫嫌弃」← 这是内容，不要这种

更多结构例子：
- 「日常小意外，越着急越出错」
- 「以为很简单，动手才发现不行」
- 「认真做一件没用的小事」
- 「被小动物或小孩打断计划」
- 「努力了半天，成果被自己毁掉」

要求：
- 每条 15 字以内，写「怎么起、怎么转」
- **必须是抽象结构**，不能出现具体物品、人物、地点
- 不要「搞笑」「治愈」这种标签词
- 不要重复

输出 JSON 数组，每项是字符串，不要别的。
一条都筛不出来就输出 []。"""


@dataclass
class Template:
    pattern: str
    source: str = ""      # 从哪条标题看出来的，只为追溯
    used: int = 0
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        return self.pattern


# 内置模板。**必须有兜底** ——
# B站首页实测大量是游戏录屏、影视剪辑、美食探店、真人 vlog，
# 按「AI 画得出来」筛完之后经常一条不剩。没有兜底的话，
# 灵感链路一旦筛空，整个生成流程就没了骨架。
DEFAULT_TEMPLATES: Tuple[str, ...] = (
    "满怀期待地做一件事，结果被无视",
    "越着急越出错，小事变成小灾难",
    "以为很简单，动手才发现完全不行",
    "认真做一件毫无意义的小事",
    "努力半天，成果被自己毁掉",
    "想装一下，结果当场翻车",
    "被小动物或小孩打断计划",
)


class InspirationStore:
    """模板库。存 data/inspirations.json。"""

    def __init__(self, path: Path, keep: int = 60) -> None:
        self.path = Path(path)
        self.keep = max(10, int(keep))
        self.items: List[Template] = []
        self.load()
        # 空库时灌入内置模板 —— 不能让她在「还没刷到可用结构」时无骨架可套。
        if not self.items:
            self.items = [
                Template(pattern=p, source="builtin") for p in DEFAULT_TEMPLATES
            ]
            self.save()
            log.info("模板库为空，已载入 %d 个内置结构", len(self.items))

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("模板库读取失败（%s），当空的用", exc)
            return
        out: List[Template] = []
        for row in raw.get("items") or []:
            try:
                out.append(
                    Template(
                        pattern=str(row["pattern"]),
                        source=str(row.get("source") or ""),
                        used=int(row.get("used") or 0),
                        at=float(row.get("at") or 0),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        self.items = out[-self.keep :]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "pattern": t.pattern,
                                "source": t.source,
                                "used": t.used,
                                "at": t.at,
                            }
                            for t in self.items[-self.keep :]
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("模板库保存失败：%s", exc)

    def add(self, patterns: Sequence[str], source: str = "") -> List[Template]:
        """加新模板，按文本去重。返回真正新增的。"""
        existing = {t.pattern for t in self.items}
        fresh: List[Template] = []
        for p in patterns:
            p = (p or "").strip()
            if not p or p in existing:
                continue
            existing.add(p)
            t = Template(pattern=p, source=source)
            self.items.append(t)
            fresh.append(t)
        self.items = self.items[-self.keep :]
        if fresh:
            self.save()
        return fresh

    def pick(self, exclude: Sequence[str] = ()) -> Optional[Template]:
        """随机挑一个，优先挑用得少的。

        为什么优先用得少：同一个结构连拍三条，账号会显得只会一个套路。
        """
        pool = [t for t in self.items if t.pattern not in exclude]
        if not pool:
            return None
        pool.sort(key=lambda t: (t.used, t.at))
        head = pool[: max(1, len(pool) // 3)]
        return random.choice(head)

    def mark_used(self, template: Template) -> None:
        for t in self.items:
            if t.pattern == template.pattern:
                t.used += 1
                t.at = time.time()
        self.save()


def parse_patterns(text: str) -> List[str]:
    """解析模型返回的 JSON 数组。解析不出来返回空。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        arr = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(arr, list):
        return []
    out: List[str] = []
    for v in arr:
        if isinstance(v, str):
            s = v.strip().strip('"').strip("「」")
            if 4 <= len(s) <= 30:
                out.append(s)
    return out


async def extract_templates(
    llm: Any, titles: Sequence[str], source: str = ""
) -> List[str]:
    """从一批标题里抽结构。标题少于 3 条就没意义，直接返回空。"""
    titles = [t.strip() for t in titles if t and t.strip()]
    if len(titles) < 3:
        return []

    user = "标题列表：\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(titles[:40], 1))
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": TEMPLATE_SYSTEM},
                {"role": "user", "content": user},
            ],
            tools=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("模板提取失败：%s", exc)
        return []
    return parse_patterns(reply)
