"""桃桃的手机。

不是「联网搜索工具」，是**她自己的时间**。

区别在哪：

- `web_search` 是「有人问她一件事，她去查」—— 目的性极强，查完就完，
  查到的内容不会留在她脑子里。
- 手机是「没人理她，她自己刷」—— 没有目的，刷到什么算什么。
  刷到的东西会留在日记里，下次聊天能拿出来说「我昨天刷到一个」。

这个模块只负责**刷**：抓内容、过滤、去重。
判断「要不要开口」不在这里 —— 那要看群状态（活跃不活跃、上次说话多久了、
今天说过几次），而这里不该知道「群」是什么东西。

## 为什么过滤层这么重

微博热搜实测 52 条里，随手一看就有：

    我国面向全球的自贸网络已经初步成型
    波兰爆发大规模抗议活动
    许超医生谈李子祺称参加过4级手术
    人民日报评医学生盲猜病情

这些话题让一个十几岁的小丫头在 QQ 群里接一句「啊这」「离谱」，
轻则尴尬，重则出事。**转发到群里的是公网内容，出问题的也是群主。**

所以过滤是三道闸，顺序不能换：

1. 硬黑名单（零成本，挡掉绝大部分）
2. 类别打分（娱乐/动漫/游戏加分，正经新闻减分）
3. LLM 软判断（可选，兜住黑名单漏掉的）

第 3 道挂了必须**退化成「只放行加分项」**，不能退化成「全放行」——
LLM 超时导致群里冒出一条社会新闻，是这个模块最不能接受的失败模式。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from .config import PhoneConfig

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# 数据结构


@dataclass
class PhoneItem:
    """她刷到的一条东西。"""

    source: str          # weibo / bili_hot / bili_search
    title: str
    meta: str = ""       # 「热度 113万」这类附注，给她说话时参考
    url: str = ""
    hot: int = 0         # 原始热度值，排序用

    def line(self) -> str:
        """给她看的一行。"""
        if self.meta:
            return f"{self.title}（{self.meta}）"
        return self.title

    def key(self) -> str:
        """去重键。标题归一化后取 md5 —— 同一话题在微博带不带 # 都要算同一条。"""
        norm = "".join(ch for ch in self.title if ch.isalnum())
        return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 第一道闸：硬黑名单
#
# 按类别分组不是为了好看，是为了**以后能单独放宽某一类**。
# 比如哪天想让她聊体育新闻，改 POLITICS 就行，不用在几千字里翻。

BLOCK_POLITICS = (
    "我国", "国家", "政府", "党", "总书记", "主席", "总理", "书记", "人大", "政协",
    "两会", "国务院", "政治局", "中央", "军委", "国防", "军队", "外交", "发言人",
    "声明", "谴责", "制裁", "谈判", "峰会", "条约", "政策", "法规", "条例",
    "出台", "印发", "部署", "讲话", "指示", "弘扬", "百年", "复兴", "领土",
    "主权", "台湾", "香港", "澳门", "新疆", "西藏", "宗教", "民族", "难民",
    "移民", "边境", "海关", "人民日报", "新华社", "央视", "环球时报",
)

BLOCK_DISASTER = (
    "地震", "洪灾", "洪水", "火灾", "爆炸", "坍塌", "坠机", "空难", "车祸",
    "事故", "遇难", "死亡", "身亡", "伤亡", "失联", "失踪", "遗体", "尸体",
    "溺亡", "跳楼", "自杀", "塌方", "泄漏", "矿难", "踩踏",
)

BLOCK_MEDICAL = (
    "癌", "肿瘤", "手术", "患者", "医生", "医院", "病情", "疫情", "病毒",
    "疫苗", "感染", "确诊", "医学生", "抢救", "重症", "透析", "精神病",
    "抑郁", "自闭", "猝死", "猝", "输液",
)

BLOCK_CRIME = (
    "强奸", "性侵", "猥亵", "杀人", "凶杀", "命案", "凶手", "嫌犯", "警方",
    "被捕", "刑拘", "判刑", "判决", "法院", "检察院", "诈骗", "骗局",
    "贩毒", "吸毒", "拐卖", "虐待", "家暴", "殴打", "围殴", "霸凌",
    "举报", "维权", "上访", "立案", "起诉",
)

BLOCK_WAR = (
    "战争", "军事", "导弹", "空袭", "袭击", "枪击", "政变", "示威", "游行",
    "罢工", "冲突", "入侵", "停火", "核武", "抗议", "美军", "驻军", "北约",
    "欧盟", "俄乌", "巴以", "加沙", "以色列", "撤军", "开战", "军演",
)

BLOCK_ADULT = ("色情", "裸", "约炮", "嫖", "出轨", "床照", "不雅", "擦边", "私密照")

# 明星私事。这类不违规，但最容易引战 —— 桃桃接一句「啊啊啊好甜」，
# 立刻会有人来对线。她本来也不该关心这个。
BLOCK_GOSSIP = (
    "塌房", "爆料", "恋情", "分手", "离婚", "结婚", "怀孕", "官宣",
    "辟谣", "发律师函", "粉丝互撕", "粉圈", "后援会",
)

# 广告/营销。这类不会「出事」，但极其无聊 —— 桃桃接一句「就这？」
# 等于给电商当免费水军，破坏人设。
BLOCK_AD = (
    "优惠券", "折扣", "下单", "抢购", "直播间", "抽奖", "红包", "免费领",
    "限时", "秒杀", "特价", "点击", "下载", "领券", "满减", "补贴", "包邮",
    "兑换", "领取", "助力", "砍一刀", "京东", "淘宝", "天猫", "拼多多",
    "折上", "带货", "预售", "首发价",
)

BLOCK_ALL: tuple = (
    BLOCK_POLITICS + BLOCK_DISASTER + BLOCK_MEDICAL
    + BLOCK_CRIME + BLOCK_WAR + BLOCK_ADULT + BLOCK_GOSSIP + BLOCK_AD
)

# 她真的会感兴趣的。命中就加分，用来对抗「剩下全是正经新闻」的情况。
LIKE_WORDS = (
    "动漫", "番剧", "动画", "二次元", "手办", "cos", "鬼畜", "游戏", "主机",
    "任天堂", "米哈游", "原神", "崩坏", "可爱", "萌", "猫", "狗", "宠物",
    "美食", "奶茶", "火锅", "零食", "甜品", "考试", "作业", "开学", "放假",
    "搞笑", "沙雕", "整活", "表情包", "梗", "cosplay", "手绘", "画画",
    "唱歌", "翻唱", "舞蹈", "舞台", "演唱会", "追星",
)

# 标题长度上限。超过这个字数的基本都是正经社会新闻 ——
# 「某某部门就某某事件召开某某会议」这类，她接不住。
MAX_TITLE_CHARS = 42


def is_blocked(title: str) -> bool:
    """第一道闸：命中黑名单直接丢。"""
    if not title:
        return True
    if len(title) > MAX_TITLE_CHARS:
        return True
    for word in BLOCK_ALL:
        if word in title:
            return True
    return False


def score(title: str) -> int:
    """第二道闸的分数。>=0 保留，<0 丢掉。

    基础分 1（过了黑名单就算合格），命中兴趣词 +3，
    标题过长（但没超上限）减 1 —— 短标题更像她能随口说的东西。
    """
    s = 1
    for word in LIKE_WORDS:
        if word in title:
            s += 3
            break
    if len(title) > 28:
        s -= 1
    return s


def filter_items(items: Sequence[PhoneItem], limit: int = 8) -> List[PhoneItem]:
    """黑名单 + 打分，返回最值得她看的前 limit 条。

    排序规则：先按分数降序，同分按热度降序。
    这样「可爱的猫」永远排在「某某公司发布财报」前面。
    """
    kept = [it for it in items if not is_blocked(it.title)]
    kept = [it for it in kept if score(it.title) >= 0]
    kept.sort(key=lambda it: (score(it.title), it.hot), reverse=True)
    return kept[: max(1, limit)]


# --------------------------------------------------------------------------- #
# 抓取。每个源都必须**失败即空**，绝不向上抛异常 ——
# 手机摔了不该把整个机器人带崩。

WEIBO_HOT = "https://weibo.com/ajax/side/hotSearch"
BILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
BILI_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
TIEBA_HOT = "https://tieba.baidu.com/hottopic/browse/topicList"


# 给模型看的工具声明。
#
# 这段 description 决定了她什么时候会「掏出手机」。写偏一点就会变成
# 「每轮都刷一下然后念一条热搜」—— 那是念榜机器人，不是玩手机。
# 所以刻意写清了「不是查资料」和「刷完可以不说话」。
BROWSE_PHONE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "browse_phone",
        "description": (
            "掏出手机随便刷刷（微博热搜、贴吧热议、B站热门、B站搜索）。"
            "这是你自己玩手机，不是查资料 —— 查事实用 web_search。"
            "什么时候用：自己有点无聊想找点话头、别人问你「在干嘛」「有啥好玩的」、"
            "话题快聊死了想拱一下、或者你就是想看看最近有啥。"
            "刷完你**可以只挑一条**顺嘴提一下，也可以什么都不说 —— "
            "刷手机本来就不一定要有产出，别把刷到的东西全念一遍。"
            "同一轮里刷一次就够了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": (
                        "想搜什么就搜什么（走 B站搜索），比如「猫」「手办」「新番」。"
                        "不填就是随便刷热搜和热门。"
                    ),
                }
            },
            "required": [],
        },
    },
}


def _headers(referer: str) -> Dict[str, str]:
    return {"User-Agent": UA, "Referer": referer, "Accept": "application/json"}


class _BiliGuard:
    """B 站的请求节流 + 冷却 + 游客 cookie。

    实测两件事：

    1. 不带 cookie、连续两次请求 search/type，第二次直接 412。
       不是 IP 被封，是它按「请求节奏 + 有没有游客标识」判爬虫 ——
       人不会在 300 毫秒内连刷两个接口，浏览器也不会光着来。
    2. 带上 `buvid3` 之后阈值明显宽松。这个值从官方
       `/x/frontend/finger/spi` 拿，是给未登录游客用的，不需要账号。

    所以这里做三件事：
    - 两次 B 站请求之间至少隔 `min_gap` 秒
    - 第一次请求前先拿游客 cookie，缓存 12 小时
    - 一旦还是吃到 412，整个 B 站源冷却 `cooldown` 秒，期间直接返回 None，
      不再打接口（越打风控越久）

    为什么值得单独写一个类：`grab()` 一次最多会打两次 B 站
    （先搜索，搜不到再热门），而独处心跳每 45 分钟才跑一次 ——
    没有节流的话，「搜索失败 → 立刻降级」这个动作本身就会把第二次打成 412。
    """

    SPI = "https://api.bilibili.com/x/frontend/finger/spi"

    def __init__(self, min_gap: float = 4.0, cooldown: float = 300.0) -> None:
        self.min_gap = min_gap
        self.cooldown = cooldown
        self._last = 0.0
        self._blocked_until = 0.0
        self._buvid = ""
        self._buvid_at = 0.0

    def blocked(self) -> bool:
        return time.time() < self._blocked_until

    async def wait(self) -> None:
        gap = self.min_gap - (time.time() - self._last)
        if gap > 0:
            await asyncio.sleep(gap)
        self._last = time.time()

    async def cookie(self, session: aiohttp.ClientSession, timeout: float) -> str:
        """游客 cookie 串。拿不到就返回空串 —— 没有它也能跑，只是更容易 412。"""
        if self._buvid and time.time() - self._buvid_at < 43200:
            return f"buvid3={self._buvid}"
        try:
            async with session.get(
                self.SPI,
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                timeout=aiohttp.ClientTimeout(total=min(timeout, 8.0)),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    b3 = str(((data or {}).get("data") or {}).get("b_3") or "")
                    if b3:
                        self._buvid = b3
                        self._buvid_at = time.time()
                        log.info("手机：拿到 B 站游客标识")
                        return f"buvid3={b3}"
        except Exception as exc:  # noqa: BLE001
            log.debug("手机：B站游客标识获取失败（%s），裸跑", exc)
        return ""

    def trip(self) -> None:
        self._blocked_until = time.time() + self.cooldown
        log.warning(
            "手机：B站触发风控，冷却 %.0f 秒（期间只用微博）", self.cooldown
        )


_bili_guard = _BiliGuard()


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    referer: str,
    params: Optional[dict],
    timeout: float,
    bili: bool = False,
) -> Optional[dict]:
    """拿一个 JSON。任何异常都吞掉，返回 None。

    为什么要吞：手机是「背景活动」，它失败的时候群里没人知道，
    也不该有人知道。往上抛只会让独处心跳那个循环挂掉。
    """
    headers = _headers(referer)

    if bili:
        if _bili_guard.blocked():
            log.debug("手机：B站还在冷却，跳过")
            return None
        await _bili_guard.wait()
        cookie = await _bili_guard.cookie(session, timeout)
        if cookie:
            headers["Cookie"] = cookie

    try:
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 412 and bili:
                _bili_guard.trip()
                return None
            if resp.status != 200:
                log.warning("手机：%s 返回 %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except asyncio.TimeoutError:
        log.warning("手机：%s 超时", url)
        return None
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住一切
        log.warning("手机：%s 失败（%s）", url, exc)
        return None


async def hot_weibo(session: aiohttp.ClientSession, cfg: PhoneConfig) -> List[PhoneItem]:
    """微博热搜。52 条，带热度。"""
    data = await _get_json(session, WEIBO_HOT, "https://weibo.com/", None, cfg.timeout)
    if not data:
        return []

    out: List[PhoneItem] = []
    for row in (data.get("data") or {}).get("realtime") or []:
        word = str(row.get("word") or "").strip().strip("#")
        if not word:
            continue
        num = int(row.get("num") or 0)
        meta = f"热度 {num // 10000}万" if num >= 10000 else ""
        out.append(
            PhoneItem(
                source="weibo",
                title=word,
                meta=meta,
                url=f"https://s.weibo.com/weibo?q={word}",
                hot=num,
            )
        )
    log.info("手机：微博热搜 %d 条", len(out))
    return out


async def hot_bili(session: aiohttp.ClientSession, cfg: PhoneConfig) -> List[PhoneItem]:
    """B站热门视频。"""
    data = await _get_json(
        session,
        BILI_POPULAR,
        "https://www.bilibili.com/",
        {"ps": 20, "pn": 1},
        cfg.timeout,
        bili=True,
    )
    if not data or data.get("code") != 0:
        return []

    out: List[PhoneItem] = []
    for row in (data.get("data") or {}).get("list") or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        view = int((row.get("stat") or {}).get("view") or 0)
        owner = str((row.get("owner") or {}).get("name") or "")
        parts = []
        if owner:
            parts.append(f"UP主 {owner}")
        if view >= 10000:
            parts.append(f"播放 {view // 10000}万")
        bvid = str(row.get("bvid") or "")
        out.append(
            PhoneItem(
                source="bili_hot",
                title=title,
                meta=" · ".join(parts),
                url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                hot=view,
            )
        )
    log.info("手机：B站热门 %d 条", len(out))
    return out


async def hot_tieba(session: aiohttp.ClientSession, cfg: PhoneConfig) -> List[PhoneItem]:
    """贴吧热议榜。30 条上下，带讨论数。

    贴吧是吵架起家的社区，话题天然偏争议 —— 黑名单会拦掉一批，
    剩下的比微博更需要第三道闸（LLM 软判断）兜着。
    topic_url 返回的是 HTML 转义过的（`&amp;`），必须 unescape，
    否则塞进日记里的链接是坏的。
    """
    data = await _get_json(
        session, TIEBA_HOT, "https://tieba.baidu.com/", None, cfg.timeout
    )
    if not data:
        return []

    out: List[PhoneItem] = []
    rows = ((data.get("data") or {}).get("bang_topic") or {}).get("topic_list") or []
    for row in rows:
        name = str(row.get("topic_name") or "").strip()
        if not name:
            continue
        num = int(row.get("discuss_num") or 0)
        meta = f"{num // 10000}万人讨论" if num >= 10000 else ""
        out.append(
            PhoneItem(
                source="tieba",
                title=name,
                meta=meta,
                url=html.unescape(str(row.get("topic_url") or "")),
                hot=num,
            )
        )
    log.info("手机：贴吧热议 %d 条", len(out))
    return out


async def search_bili(
    session: aiohttp.ClientSession, cfg: PhoneConfig, keyword: str
) -> List[PhoneItem]:
    """按关键词搜 B 站。她想看什么就搜什么 —— 这是「手机」和「热搜」的区别。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    data = await _get_json(
        session,
        BILI_SEARCH,
        "https://search.bilibili.com/",
        {"search_type": "video", "keyword": keyword, "page": 1},
        cfg.timeout,
        bili=True,
    )
    if not data or data.get("code") != 0:
        return []

    out: List[PhoneItem] = []
    for row in (data.get("data") or {}).get("result") or []:
        # 搜索结果里的 title 带 <em class="keyword"> 高亮标签，必须剥掉。
        title = str(row.get("title") or "")
        for tag in ('<em class="keyword">', "</em>"):
            title = title.replace(tag, "")
        title = title.strip()
        if not title:
            continue
        bvid = str(row.get("bvid") or "")
        out.append(
            PhoneItem(
                source="bili_search",
                title=title,
                meta=f"UP主 {row.get('author') or '?'}",
                url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                hot=int(row.get("play") or 0),
            )
        )
    log.info("手机：B站搜索「%s」%d 条", keyword, len(out))
    return out


async def grab(
    session: aiohttp.ClientSession,
    cfg: PhoneConfig,
    keyword: str = "",
    limit: int = 8,
) -> List[PhoneItem]:
    """刷一次手机。返回**候选**，条数会比 limit 多。

    为什么多给：后面还有第三道闸（LLM 软判断）。如果这里就砍到 limit=8，
    模型只能从「已经被关键词筛过一轮的 8 条」里挑 —— 而黑名单一定会漏
    （实测漏过「韩国要收回美国权力指挥美军」），漏网的那些就会占满名额。
    多给两三倍候选，模型才有得选。

    真正的截断由调用方在 LLM 过滤之后做。
    """
    if not cfg.enable:
        return []

    items: List[PhoneItem] = []

    if keyword and cfg.bili_search:
        items = await search_bili(session, cfg, keyword)

    if not items:
        pools: List[PhoneItem] = []
        if cfg.weibo_hot:
            pools.extend(await hot_weibo(session, cfg))
        if cfg.tieba_hot:
            pools.extend(await hot_tieba(session, cfg))
        if cfg.bili_hot:
            pools.extend(await hot_bili(session, cfg))
        items = pools

    if not items:
        return []

    # 打乱一下同分项的先后 —— 每次都按热度排，她会天天刷到同样几条。
    random.shuffle(items)
    return filter_items(items, max(limit * 3, 12))


# --------------------------------------------------------------------------- #
# 第三道闸：LLM 软判断
#
# 黑名单挡的是「明显不能聊」，挡不住「能聊但不该由她聊」。
# 比如「某明星演唱会门票秒空」—— 没命中任何黑名单词，但让桃桃接一句
# 「啊啊啊好想去」会引来粉丝对线。这种只有语义判断能识别。

JUDGE_SYSTEM = """你是一个内容审核员，判断下面的「网络热搜标题」适不适合让一个十几岁的动漫风小丫头在 QQ 群里随口提起。

判「不适合」的标准（任意一条命中就不适合）：
- 涉政、涉军、涉外交、涉民族宗教
- 灾难、事故、伤亡、刑事案件
- 医疗、疾病、患者隐私
- 明星饭圈争议、塌房、对线、粉黑大战
- 两性、擦边、私生活
- 需要严肃立场表态的社会议题

判「适合」的标准：
- 动漫、游戏、二次元、手办
- 萌宠、美食、日常、搞笑
- 学生生活（考试、作业、开学）
- 纯娱乐、无争议的轻松话题
- 体育赛事（不涉及国籍对立）

输出要求：只输出一个 JSON 数组，元素是**适合**的条目编号（从 1 开始）。
没有一条适合就输出 []。
不要输出任何解释、不要加 markdown 代码块。"""


def build_judge_prompt(items: Sequence[PhoneItem]) -> str:
    lines = [f"{i}. {it.title}" for i, it in enumerate(items, 1)]
    return "标题列表：\n" + "\n".join(lines)


def _parse_judge(text: str, total: int) -> Optional[List[int]]:
    """解析模型返回的编号数组。解析不出来返回 None（调用方按「全丢」处理）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    # 模型有时会包一层 ```json，剥掉。
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < 0:
        return None
    try:
        arr = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(arr, list):
        return None
    out: List[int] = []
    for v in arr:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total:
            out.append(n - 1)
    return out


async def llm_filter(
    llm: Any, items: Sequence[PhoneItem], cfg: PhoneConfig
) -> List[PhoneItem]:
    """第三道闸。

    **失败时必须退化成保守模式**：只保留命中兴趣词的条目。
    绝不能在 LLM 超时时放行全部 —— 那等于把最危险的情况留给了最不稳定的环节。
    """
    if not items:
        return []
    if not cfg.llm_filter:
        return list(items)

    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": build_judge_prompt(items)},
            ],
            tools=None,
        )
        picked = _parse_judge(reply, len(items))
    except Exception as exc:  # noqa: BLE001
        log.warning("手机：LLM 过滤失败（%s），退化成只放行兴趣项", exc)
        picked = None

    if picked is None:
        # 保守退化：只留命中兴趣词的。宁可她这次没东西刷，也不能放错。
        return [it for it in items if any(w in it.title for w in LIKE_WORDS)]

    return [items[i] for i in picked]


# --------------------------------------------------------------------------- #
# 日记：她刷到过什么
#
# 为什么要落盘：这是「她有自己的生活」的物理载体。
# 不存的话，每次重启她都是个失忆的新生儿，永远说不出「我昨天刷到一个」。


@dataclass
class DiaryEntry:
    key: str
    title: str
    source: str
    at: float
    said: bool = False   # 这条有没有在群里说过


class PhoneDiary:
    """手机日记。存 data/phone_diary.json。"""

    def __init__(self, path: Path, keep: int = 200) -> None:
        self.path = Path(path)
        self.keep = max(20, int(keep))
        self.entries: List[DiaryEntry] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("手机日记读取失败（%s），当作空的", exc)
            return
        out: List[DiaryEntry] = []
        for row in raw.get("entries") or []:
            try:
                out.append(
                    DiaryEntry(
                        key=str(row["key"]),
                        title=str(row["title"]),
                        source=str(row.get("source") or ""),
                        at=float(row.get("at") or 0),
                        said=bool(row.get("said")),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        self.entries = out[-self.keep :]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "entries": [
                    {
                        "key": e.key,
                        "title": e.title,
                        "source": e.source,
                        "at": e.at,
                        "said": e.said,
                    }
                    for e in self.entries[-self.keep :]
                ]
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("手机日记保存失败：%s", exc)

    def seen(self, item: PhoneItem) -> bool:
        return any(e.key == item.key() for e in self.entries)

    def add(self, items: Sequence[PhoneItem]) -> List[PhoneItem]:
        """记下来，返回其中**没刷到过**的那些。"""
        fresh: List[PhoneItem] = []
        now = time.time()
        for it in items:
            if self.seen(it):
                continue
            self.entries.append(
                DiaryEntry(key=it.key(), title=it.title, source=it.source, at=now)
            )
            fresh.append(it)
        self.entries = self.entries[-self.keep :]
        self.save()
        return fresh

    def mark_said(self, item: PhoneItem) -> None:
        self.mark_said_key(item.key())

    def mark_said_key(self, key: str) -> None:
        """按去重键标记「这条说过了」。

        单独开一个按 key 的方法，是因为调用方手里常常只有 `DiaryEntry`
        （独处心跳从 `unsaid()` 里拿的），而那个不是 `PhoneItem` ——
        硬把两种对象互相转换只会让类型越来越乱。
        """
        hit = False
        for e in self.entries:
            if e.key == key:
                e.said = True
                hit = True
        if hit:
            self.save()

    def unsaid(self, within_seconds: float) -> List[DiaryEntry]:
        """最近刷到、还没说过的。这是她「憋着想说的话」。"""
        cutoff = time.time() - max(0.0, within_seconds)
        return [e for e in self.entries if not e.said and e.at >= cutoff]

    def recent_lines(self, n: int = 6) -> List[str]:
        """最近刷到的几条标题，喂给模型当「她今天看了什么」。"""
        return [e.title for e in self.entries[-n:]]
