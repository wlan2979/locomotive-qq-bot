"""表情包：QQ 内置小黄脸 + 本地表情包图片。

## 两条路，为什么要并存

OneBot 里「发表情」是两件完全不同的事：

* `face` 段 —— QQ **内置小黄脸**，id 是固定的（14=微笑、4=得意）。
  发个数字就行，**零素材依赖**。
* `image` 段 —— 本地图片，走 base64。这才是通常说的「表情包」。

本地素材得用户自己攒，而**在攒起来之前功能不能是死的**。所以内置表情
做兜底：`send_sticker(mood="无语")` 在本地目录为空时自动退成小黄脸；
用户往 `assets/stickers/无语/` 丢几张图之后，**同一句调用**就自动升级成
真表情包。对模型来说接口没变，对用户来说不用改任何配置。

这个「有则用、无则退」的形状和生图通道是一致的（本机优先、连不上降级）。

## 为什么不用 AI 生成表情包来铺底

试过的方向，放弃了：AI 生成的表情包**没有梗**。表情包的信息量不在画面上，
在「这张图我知道什么意思」这件事上 —— 那是传播出来的共识，模型生成不出来。
所以本地素材这条路只能靠攒，不能靠造。
"""

from __future__ import annotations

import base64
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 本地表情包根目录：assets/stickers/<情绪>/xxx.gif
STICKER_ROOT = Path(__file__).resolve().parent.parent / "assets" / "stickers"

# 支持的图片后缀。**GIF 必须在** —— 表情包的灵魂一半在动图上，
# 压成静帧就废了。所以这里绝不走 bot/image.py 那套「统一转 JPEG」，
# 那条路是给「喂给模型看」用的，发表情包要的是原样保真。
IMAGE_SUFFIXES = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")

# 单张上限。
#
# 2026-10-04 从 3MB 提到 8MB。原来那个值定得没道理 —— 发表情包走的是
# **本地** OneBot 通道（读文件 → base64 → 发给同机的 NapCat），
# 既不进模型 token、也不出网，唯一成本是 base64 膨胀 33% 加一次本地传输。
# 实测用户第一张被挡下的图是 3.2MB 的一张动态表情包，那个尺寸在 QQ
# 表情包里很常见，挡掉纯属自找麻烦。
# 8MB 留足余量，又能挡住误放进来的截图 / 长图。
MAX_BYTES = 8 * 1024 * 1024

# 已经警告过「超限」的文件。见 local_files 里的说明。
_warned_oversize: set = set()

# 每个会话记多少张「最近发过的」用来防重复
RECENT_LIMIT = 8


@dataclass(frozen=True)
class Mood:
    """一种情绪。

    `key` 同时是**本地素材的目录名** —— 这样用户想加素材时，
    看一眼代码里的表就知道该建哪个文件夹，不用查文档。
    """

    key: str
    face_id: int  # 本地没素材时用的 QQ 内置表情 id
    label: str  # 内置表情的中文名，写进 prompt 给模型看
    aliases: Tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 情绪表
#
# ## 从 16 个扩到 40 个（2026-10-19）
#
# 原来那 16 个只够覆盖「最粗的一层反应」。真实群聊里大量的中间态没有位置：
# 想吐槽又不到生气（嫌弃）、想夸又不到「牛」（加油）、想说话又不想接茬（摊手）、
# 被逗到不行但还没笑出来（崩溃）—— 模型只能往最近的格子里挤，发出来的图
# 就跟当下的情绪差一档，看着像「程式化地发表情」。
#
# 扩表是有代价的：这张表整个写进 system prompt，每多一个就多占 token。
# 40 个大约 240 字符 ≈ 150 token，对 64K 上下文的 DeepSeek 可以忽略。
# 「选项多了模型挑不准」这个担心在 16 → 40 这一档不成立：模型的语义理解
# 远比这张表细，真正的瓶颈一直是**格子不够**，不是格子太多。
#
# ## 排列顺序是有意义的，不是随手排
#
# `_BY_ALIAS` 用 `setdefault` 建索引，**先注册的赢**。所以别名撞车时，
# 排在前面的情绪会拿走那个词。比如「绷不住」给尴尬、「绷不住笑」给大笑
# （长别名优先，不冲突），但「懂了」只能给一个 —— 这里给了「恍然」，
# 因为「收到」已经有「明白了 / 了解 / ok」覆盖。
#
# 分组只是给人看的（正面 / 认知 / 负面 / 社交），代码不依赖它。
MOODS: Tuple[Mood, ...] = (
    # ---- 正面 ----
    Mood("开心", 14, "微笑", ("笑", "高兴", "愉快", "开森", "嘻嘻", "好耶")),
    Mood("大笑", 13, "呲牙", ("大笑", "哈哈", "爆笑", "笑死", "乐", "笑不活了", "绷不住笑")),
    Mood("得意", 4, "得意", ("得瑟", "骄傲", "嘿嘿", "小得意")),
    Mood("兴奋", 100, "激动", ("激动", "兴奋", "太棒了", "冲鸭", "起飞", "芜湖", "好耶耶")),
    Mood("赞", 79, "强", ("牛", "厉害", "棒", "强", "666", "顶", "太强了")),
    Mood("加油", 30, "奋斗", ("加油", "打气", "你可以的", "干就完了", "鼓励", "冲")),
    Mood("感动", 50, "快哭了", ("感动", "好暖", "戳心", "泪目了", "被治愈")),
    Mood("感谢", 83, "抱拳", ("谢谢", "多谢", "感谢", "谢了", "感恩", "谢啦")),
    Mood("安慰", 78, "拥抱", ("摸摸", "抱抱", "不哭", "没事的", "安慰", "别难过")),
    # 「可爱」在 label 和 aliases 里都出现，**不是笔误**：
    # label 是内置表情的中文名（给模型看的），aliases 才是匹配用的。
    # 而「可爱」恰好两边都算 —— 漏掉 alias 的话「好可爱」就解析不出来。
    Mood("卖萌", 21, "可爱", ("可爱", "撒娇", "萌", "求夸", "装可爱")),
    Mood("爱心", 66, "爱心", ("喜欢", "爱", "比心", "心动", "么么", "爱你")),
    Mood("害羞", 6, "害羞", ("脸红", "不好意思", "羞", "捂脸", "害羞")),

    # ---- 认知 ----
    Mood("疑问", 32, "疑问", ("疑惑", "问号", "不解", "啥", "嗯？", "不懂", "不理解", "为什么")),
    Mood("惊讶", 0, "惊讶", ("震惊", "卧槽", "诶", "哇", "不敢信", "好家伙")),
    Mood("思考", 3, "发呆", ("思考", "沉思", "让我想想", "琢磨", "深思")),
    Mood("恍然", 42, "鼓掌", ("原来如此", "恍然大悟", "懂了", "啊哈", "秒懂", "悟了")),
    Mood("无语", 22, "白眼", ("翻白眼", "服了", "麻了", "沉默", "汗颜")),
    Mood("尴尬", 10, "尴尬", ("尬", "社死", "绷不住", "脚趾扣地")),
    Mood("摊手", 27, "流汗", ("摊手", "无奈", "随便吧", "耸肩", "爱咋咋地")),
    Mood("偷看", 20, "偷笑", ("偷看", "暗中观察", "瞄一眼", "吃瓜")),

    # ---- 负面 ----
    Mood("生气", 11, "发怒", ("怒", "气死", "恼火", "火大", "气鼓鼓")),
    Mood("嫌弃", 19, "吐", ("嫌弃", "鄙视", "辣眼睛", "呕", "恶心", "看不上")),
    Mood("嘲讽", 48, "鄙视", ("嘲讽", "阴阳", "就这", "呵呵", "冷笑", "讽刺")),
    Mood("崩溃", 18, "抓狂", ("崩溃", "破防", "心态崩了", "寄了", "想死", "绷不住了")),
    Mood("害怕", 26, "惊恐", ("害怕", "恐怖", "吓人", "好怕", "瑟瑟发抖")),
    Mood("焦虑", 17, "冷汗", ("焦虑", "紧张", "完了", "要死", "来不及")),
    Mood("疲惫", 25, "困", ("疲惫", "好累", "累了", "困了", "想睡")),
    Mood("摆烂", 34, "晕", ("摆烂", "躺平", "算了", "毁灭吧", "不管了")),
    Mood("拒绝", 88, "NO", ("拒绝", "不要", "不行", "no", "达咩")),
    Mood("失望", 15, "难过", ("失望", "唉", "白期待", "难过", "心累")),
    Mood("抱歉", 40, "擦汗", ("抱歉", "对不起", "我的锅", "不好意思啊")),
    Mood("哭", 5, "流泪", ("流泪", "呜呜", "哭了", "想哭", "emo", "泪目")),
    Mood("委屈", 49, "委屈", ("可怜", "求求", "冤枉", "别这样")),

    # ---- 社交 ----
    # ⚠️ 「调戏」= **开玩笑地逗弄**（坏笑、起哄、贫嘴），**不是**暧昧调情。
    #
    # 2026-10-04 用户明确：「带有开玩笑的意思就行，不要真的有性暗示」。
    # 所以这一条刻意做了两处收紧：
    #   · face_id 从 84 换成 44 —— 84 的官方名叫「勾引」，那个名字本身就带暗示，
    #     跟人设底线（persona.txt：「不许有任何暗示性的回应」）直接打架；
    #     44 是「坏笑」，逗弄的意思，干净。
    #   · 别名剔掉「挑逗」「勾搭」「撩」这类暧昧词，换成起哄 / 打趣一类 ——
    #     别名是**匹配**用的，留着它们等于给暗示性说法开了后门。
    Mood("调戏", 44, "坏笑", ("逗你", "逗弄", "打趣", "起哄", "拱火", "整活")),
    Mood("收到", 89, "OK", ("收到", "明白", "明白了", "了解", "ok")),
    Mood("再见", 39, "再见", ("拜拜", "走了", "溜了", "告辞", "下了")),
    Mood("睡觉", 8, "睡", ("睡觉", "晚安", "睡了", "去睡了")),
    Mood("打哈欠", 47, "哈欠", ("打哈欠", "好困", "无聊")),
    Mood("摸鱼", 28, "憨笑", ("摸鱼", "划水", "偷懒", "上班摸鱼")),
    Mood("吃饭", 24, "饥饿", ("吃饭", "饿了", "干饭", "恰饭", "好饿")),
)

_BY_KEY: Dict[str, Mood] = {m.key: m for m in MOODS}

_BY_ALIAS: Dict[str, Mood] = {}
for _m in MOODS:
    # key 本身也进索引 —— 否则「有点无语」匹配不上：包含匹配只翻别名表，
    # 而「无语」是 key，不在里面。这个坑是实测踩到的。
    _BY_ALIAS.setdefault(_m.key, _m)
    for _a in _m.aliases:
        # setdefault：别名撞车时**先注册的赢**。表是按重要度排的，
        # 所以这个顺序是刻意的，不要改成覆盖。
        _BY_ALIAS.setdefault(_a, _m)

# 用于**包含匹配**的别名，按长度降序（「想大笑」要先撞「大笑」而不是「笑」）。
#
# 只收长度 >= 2 的。单字别名只在精确匹配时用 —— 拿它们做子串匹配会满世界
# 误伤：「乐」命中「音乐」，「爱」命中「可爱」，「累」命中「积累」。
# 而模型填情绪时几乎不会只给一个字，所以这个限制没有代价。
_ALIASES_SORTED: List[str] = sorted(
    (a for a in _BY_ALIAS if len(a) >= 2), key=len, reverse=True
)


def resolve_mood(raw: Any) -> Optional[Mood]:
    """把模型填的任意说法解析成一个情绪。

    三级匹配：精确命中 key → 精确命中别名 → 包含匹配（长别名优先）。

    为什么要做模糊匹配：模型经常填「有点无语」「想笑」这种带修饰的说法，
    为这个判失败很冤 —— 它表达的意图是清楚的。宁可宽容一点。
    """
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s in _BY_KEY:
        return _BY_KEY[s]
    if s in _BY_ALIAS:
        return _BY_ALIAS[s]
    for alias in _ALIASES_SORTED:
        if alias in s:
            return _BY_ALIAS[alias]
    return None


_CATALOG_TTL = 5.0
_catalog_cache: Tuple[float, str] = (0.0, "")


def mood_catalog() -> str:
    """给模型看的情绪清单。

    ## 2026-10-19 改：不再显示「有几张本地素材」

    原来带数量是「模型看得见这个情绪有图，就会更愿意用」。但发表情包已经改成
    **现画优先**（bot/sticker_gen.py），本地素材退成兜底 —— 这时数量不仅没用，
    还会误导：模型会以为「×8 的比 ×1 的优先」，于是拼命往老情绪上挤，
    那正是「表情看着程式化」的来源。

    现在只列名字。缓存仍然保留（这份清单每轮要进两次 prompt，
    40 个情绪每 5 秒扫一遍目录也不值当）。
    """
    global _catalog_cache
    now = time.time()
    cached_at, cached = _catalog_cache
    if cached and now - cached_at < _CATALOG_TTL:
        return cached

    result = "、".join(mood.key for mood in MOODS)
    _catalog_cache = (now, result)
    return result


# --------------------------------------------------------------------------- #
# 本地素材


def local_files(mood_key: str) -> List[Path]:
    """列出某个情绪下所有可用的本地表情包。

    读不出大小、大小为零、超过上限的一律跳过 —— 表情包发不出去是小事，
    因为一张坏图把整轮回复搞崩是大事。
    """
    folder = STICKER_ROOT / mood_key
    if not folder.is_dir():
        return []

    out: List[Path] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        log.warning("读不到表情包目录 %s：%s", folder, exc)
        return []

    for path in entries:
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        if size > MAX_BYTES:
            # **只警告一次**。`mood_catalog()` 每 5 秒重扫一遍所有情绪目录，
            # 每次都喊的话日志会被同一张图刷满（实测同一张刷了 4 遍），
            # 真正的新问题反而被淹掉。
            if path not in _warned_oversize:
                _warned_oversize.add(path)
                log.warning(
                    "表情包 %s 有 %.1fMB，超过 %dMB 上限，跳过",
                    path.name, size / 1024 / 1024, MAX_BYTES // 1024 // 1024,
                )
            continue
        out.append(path)
    return out


def has_any_local() -> bool:
    """本地到底有没有素材。用来在启动时打一行日志。"""
    return any(local_files(m.key) for m in MOODS)


# --------------------------------------------------------------------------- #


class StickerPicker:
    """选一张表情，并避免同一个会话连着发同一张。

    为什么要防重复：**同一张图连着出现两次，比文字复读更刺眼。**
    文字还能解释成语气，图片重复就是明摆着的机器行为 —— 它没有「忘了刚说过」
    这个人类借口。所以宁可换一张差的，也别重发。
    """

    def __init__(self, recent_limit: int = RECENT_LIMIT, max_sessions: int = 200) -> None:
        self._limit = max(1, int(recent_limit))
        self._max_sessions = max(1, int(max_sessions))
        # session_key -> 最近发过的文件名。OrderedDict 兼做会话数的 LRU。
        self._recent: "OrderedDict[str, List[str]]" = OrderedDict()

    def _seen(self, session_key: str) -> List[str]:
        seen = self._recent.get(session_key)
        if seen is None:
            seen = []
            self._recent[session_key] = seen
            while len(self._recent) > self._max_sessions:
                self._recent.popitem(last=False)
        else:
            self._recent.move_to_end(session_key)
        return seen

    def pick(self, session_key: str, mood: Mood) -> Optional[Path]:
        """挑一张本地表情包。这个情绪没有素材就返回 None（调用方退成内置表情）。"""
        files = local_files(mood.key)
        if not files:
            return None

        seen = self._seen(session_key)
        # 优先挑没发过的；全发过一轮了就放开限制，否则表情包库会「用完」
        fresh = [f for f in files if f.name not in seen]
        chosen = random.choice(fresh or files)

        seen.append(chosen.name)
        if len(seen) > self._limit:
            del seen[: len(seen) - self._limit]
        return chosen


# --------------------------------------------------------------------------- #
# 消息段构造


def build_face_segment(mood: Mood) -> Dict[str, Any]:
    """QQ 内置小黄脸。零素材兜底。"""
    return {"type": "face", "data": {"id": str(mood.face_id)}}


def build_sticker_segment(path: Path) -> Optional[Dict[str, Any]]:
    """本地表情包 → image 段。

    走 base64 而不是 `file://`：和生图那条路保持一致，少一个「NapCat
    读不到这个路径」的失败模式（路径里的空格、中文都踩过）。

    **不转码、不压尺寸** —— GIF 动图是表情包的灵魂，重编码就成静帧了。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("读表情包失败 %s：%s", path, exc)
        return None
    if not raw:
        return None
    return {
        "type": "image",
        "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")},
    }


# --------------------------------------------------------------------------- #
# 工具声明


def build_send_sticker_tool() -> Dict[str, Any]:
    """工具声明。

    做成函数而不是模块常量，因为描述里要嵌 `mood_catalog()` ——
    用户往目录里加了素材之后，模型得看得见新的数量。

    **措辞要两头都够力，不能只踩刹车。**
    最早的版本「什么时候别用」写得比「什么时候用」重得多（甚至 docstring 里
    就把这当成设计意图），理由是「用滥比用少更假」。这个担心是对的，但实测
    （2026-10-04 群日志）走到了另一个极端：**模型从不主动发** ——
    只有被问「你发表情包会根据场景选吗」或被催「你怎么不发表情包」时才发。
    真人在群里斗图是常态，一张不发反而不像人。

    所以现在：正面场景写成同等强度（什么时候发、怎么发更自然），
    抑制那条保留但收窄成真正的边界（别当标点符号），不再说「尽量别发」。

    ## 2026-10-19：加了 `hint`，情绪表不再是唯一入口

    原来写死「只能从这里挑，别自己造」，结果是模型把**所有**反应往 16 个
    格子里挤 —— 「被自己的话噎住」塞进尴尬，「想吐槽又没到生气」塞进无语。
    图是发了，但总差一档，看着就是程式化地发表情。

    现在给了第二条路：`hint` 直接用英文标签描述要画什么。情绪表降级成
    「快捷方式」，不是「白名单」。这条对**现画**有效；退到本地素材 /
    内置小黄脸时仍然需要情绪（那两个没有自由发挥的余地）。
    """
    return {
        "type": "function",
        "function": {
            "name": "send_sticker",
            "description": (
                "发一张**你自己**的表情包来表达情绪。"
                "**这是日常说话方式的一部分，不是特殊操作。**\n"
                "图是按你的 Q 版形象画的（白底贴纸风），所以每张都是你本人 ——"
                "这也是为什么它比随手甩个通用表情更像「你的反应」。\n"
                "**频率：平均每六七条回复一张就够，别太密** —— 攒着好几轮不发也行，"
                "**场合对了才发**，发得太勤就不值钱了。\n"
                "什么时候用（常见，别犹豫）：情绪上来了就发 —— 无语、笑死、委屈、"
                "得意、害羞、卖萌、生气这类，一张图比打一串字更像真人。"
                "别人发了图或表情、你想接茬时，回一张也正好。"
                "**没什么特别的理由、但你就是想甩一张，那就发。**\n"
                "怎么发更自然：可以**单独发**（不配文字），通常比「文字 + 表情」更像人。\n"
                "什么时候别用（这是边界，不是「尽量别发」）：说正经事、对方在求助、"
                "气氛不对的时候；这轮已经发过一张了。"
                "**别把它当标点符号** —— 不是每条都配一张。\n"
                "---\n"
                "**怎么描述要哪张**（两个参数，至少填一个）：\n"
                f"  · `mood` —— 有合适的就从这里挑：{mood_catalog()}\n"
                "  · `hint` —— **表里没有合适的就用它**。用英文标签写表情和动作，"
                "例如 `forced smile, eye twitching, cold sweat`、"
                "`holding back laughter, hand over mouth`、`dead eyes, staring at viewer`。"
                "hint 比情绪更准，因为它不受固定列表限制。\n"
                "  · 两个都给最好：`mood` 定大方向，`hint` 补细节。\n"
                "  · **`mood` 填了表外的词时必须同时给 `hint`**，否则画不出来。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "description": (
                            "情绪名，从上面「可用的情绪」里挑一个。"
                            "**表里没有合适的可以留空**，改用 hint 描述。"
                        ),
                    },
                    "hint": {
                        "type": "string",
                        "description": (
                            "这张表情包具体要什么表情 / 动作，**英文 Danbooru 标签**，"
                            "逗号分隔，例如 `teary eyes, pouting, looking away`。\n"
                            "作用是让这张图贴合**当下这句话**，而不是只贴合一个粗情绪。\n"
                            "**不要写中文**（底模不认，会被丢掉）；不要写画风 / 构图 / 背景"
                            "（那些系统已经固定了）；不要写角色外貌（发色瞳色服装是写死的）。"
                        ),
                    },
                },
                "required": [],
            },
        },
    }


# --------------------------------------------------------------------------- #
# 命令行：看一眼每个情绪现有多少张
#
# 给桌面那个「表情包素材.bat」用的 —— 双击它会打开素材文件夹，顺便把这份
# 清单打出来，好知道哪个情绪还空着。也可以直接跑：
#     python -m bot.sticker
#
# 刻意不做对齐排版：情绪名是中文（全角），cmd 里按字符数补空格对不齐，
# 还不如老老实实「名字：N 张」。


def oversized_files() -> List[Path]:
    """列出所有因为超限而被跳过的文件。

    单独抽出来是为了让 `print_stats()` 能报给用户看 —— 否则
    「我明明放了图，它怎么不用」只能靠翻日志才知道。
    """
    out: List[Path] = []
    for mood in MOODS:
        folder = STICKER_ROOT / mood.key
        if not folder.is_dir():
            continue
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_BYTES:
                    out.append(path)
            except OSError:
                continue
    return out


def print_stats() -> int:
    total = 0
    empty: List[str] = []
    for mood in MOODS:
        count = len(local_files(mood.key))
        total += count
        if count == 0:
            empty.append(mood.key)
            print(f"  {mood.key}：0 张   ← 还空着，会退成 QQ 内置表情")
        else:
            print(f"  {mood.key}：{count} 张")

    print()
    print(f"  合计 {total} 张")
    if empty:
        print(f"  空着的：{'、'.join(empty)}")

    over = oversized_files()
    if over:
        print()
        print(f"  ⚠️ 有 {len(over)} 张超过 {MAX_BYTES // 1024 // 1024}MB 上限，不会被用到：")
        for path in over:
            try:
                mb = path.stat().st_size / 1024 / 1024
            except OSError:
                mb = 0.0
            print(f"      {path.parent.name}/{path.name}  {mb:.1f}MB")
        print("      压一下再放进来，或者换一张。")

    print()
    print("  把图片拖进对应文件夹就行 —— 最多 5 秒生效，不用重启机器人。")
    print("  每个情绪建议 5~10 张，太少会重复。")
    print()
    # 生成池子在另一个目录（assets/stickers_gen/）。延迟导入 ——
    # sticker_gen 顶层 import 本模块，这里反过来会成环；
    # 而 __main__ 只在脚本直接跑时执行，那时候模块早就加载完了。
    #
    # **按当前配置的指纹数**，不按文件数。池子里的图文件名带 prompt 指纹，
    # 换 LoRA / 换底模之后旧图不会被复用 —— 但文件还在目录里。
    # 直接 glob 会把它们算进去，报出一个虚高的数字（实测这么干时明明
    # 5 张能用，却显示 13 张）。
    try:
        from .sticker_gen import STICKER_ROOT as GEN_ROOT
        from .sticker_gen import _digest_for as _gf

        # LoRA 开没开要问 config —— 指纹是跟着它变的
        lora = ""
        try:
            import json

            _cfgp = Path(__file__).resolve().parent.parent / "config.json"
            if _cfgp.is_file():
                lora = str(
                    (json.loads(_cfgp.read_text(encoding="utf-8")).get("sticker_gen") or {}).get("lora") or ""
                ).strip()
        except Exception:  # noqa: BLE001
            lora = ""

        usable = stale = 0
        for mood in MOODS:
            folder = GEN_ROOT / mood.key
            if not folder.is_dir():
                continue
            digest = _gf(mood.key, "", bool(lora))
            for p in folder.glob("*.png"):
                if digest in p.name:
                    usable += 1
                else:
                    stale += 1

        print(f"  她自己画的（当前配置可用）：{usable} 张，在 {GEN_ROOT}")
        if stale:
            print(f"  另有 {stale} 张是旧配置留下的，不会再被用到 —— "
                  f"确认不需要就删掉 {GEN_ROOT} 里的内容。")
        if usable == 0 and stale == 0:
            print("    池子还是空的，发表情包时会现画。")
        print("    —— 发表情包时现画优先，本地素材退成兜底。")
    except Exception as exc:  # noqa: BLE001
        print(f"  （读不到生成池子：{exc}）")
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(print_stats())
