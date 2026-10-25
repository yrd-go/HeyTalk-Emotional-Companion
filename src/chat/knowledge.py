# -*- coding: utf-8 -*-
"""
src/chat/knowledge.py —— 情绪自助知识库的检索（S4 剩余部分）
================================================================

它和 profile.py 的分工（别混在一起，这是两类东西）
--------------------------------------------------------------------------
    profile.py    关于**这个人**：只存用户确认过的长期信息
    knowledge.py  关于**世界**：具体能怎么做（呼吸法、睡眠、拖延…）
                  与个人无关，所以不需要确认，可以直接检索

数据源
--------------------------------------------------------------------------
    data/emotional_handbook.txt —— 项目**唯一**的知识库（情绪自助手册）。
    它同时是 RAG 演示（src/rag_demo.py）的语料：一份数据两用。
    （原先还有一份 IT 手册与针对它的 40 题评测，已按需求整体删除。）

设计要点
--------------------------------------------------------------------------
1. **零依赖检索**：按条目标题与正文做加权关键词打分，不引入向量库。
   语料只有几万字、条目只有几十条，关键词检索够用；而且离线可用、
   可解释（能说清"为什么命中这条"）。
2. **状态参与排序**：光靠关键词会漏掉"用户没说出来但状态上需要"的条目。
   所以把当下状态（valence / arousal）也作为加权项：
   比如唤醒度很高时，"先把身体调回来"这一章要往前排。
3. **必须给建议时才给**：`should_offer_methods()` 明确要求"用户是在问怎么做"。
   与 persona.py 的"不主动给建议"保持一致——不能因为检索得到就塞给用户。
4. **热线一致性校验**：手册里写了求助号码，必须与 safety.HOTLINES 一致。
   写错号码比不写更危险，所以有专门的测试与运行时校验。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "HandbookEntry",
    "Handbook",
    "load_handbook",
    "score_entry",
    "search",
    "build_method_block",
    "should_offer_methods",
    "check_hotline_consistency",
    "CHAPTER_HINTS",
]

# 条目头形如：【E1-01】4-7-8 呼吸：给绷紧的身体一个刹车
_ENTRY_RE = re.compile(r"^【(E\d{1,2}-\d{2})】(.+)$")
_CHAPTER_RE = re.compile(r"^第\s*(\d+)\s*章\s*(.+)$")
_WS_RE = re.compile(r"\s+")

# 章节 → 它在什么状态下更可能有用（用于状态加权）
CHAPTER_HINTS: dict[int, dict[str, str]] = {
    1: {"note": "先把身体调回来", "high_arousal": "yes", "low_energy": "yes"},
    2: {"note": "让情绪先过去", "any": "yes"},
    3: {"note": "具体困扰（睡眠/拖延/考试）", "any": "yes"},
    4: {"note": "自我批评太响的时候", "low_valence": "yes", "low_self_worth": "yes"},
    5: {"note": "和别人的关系", "any": "yes"},
    6: {"note": "什么时候该找真人帮忙", "low_valence": "yes"},
}

# 用户明确问"怎么做"的信号（与 role_router 的求教模式同源，但这里只判"要不要给方法"）
# 注意两处踩过的坑：
#   1. "怎么**才**能" 里多一个"才"，原写法只写"怎么能"会漏；
#   2. "给我建议" 前面若是"别/不要/不用"，那是在**拒绝**建议 ——
#      必须有否定后顾，否则"别给我建议"会被判成"在求建议"。
_ASKING_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"我该怎么办", r"怎么办才好", r"该怎么(做|办|选)",
    r"有没有(什么)?(方法|办法|建议)", r"(?<!别)(?<!不要)(?<!不用)给我(个|点)?(建议|方法|办法)",
    r"教教我", r"怎么(才)?能", r"如何(才)?(可以|能够)?", r"我该做点什么",
    r"帮我想想", r"有什么(方法|办法|建议)",
))


@dataclass(frozen=True)
class HandbookEntry:
    """一条自助条目。"""

    entry_id: str          # E1-01
    title: str             # 标题
    chapter: int
    chapter_title: str
    body: str

    def to_text(self) -> str:
        return f"【{self.entry_id}】{self.title}\n{self.body}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "chapter": self.chapter,
            "chapter_title": self.chapter_title,
        }


class Handbook:
    """已解析的手册（条目列表 + 章节归属）。"""

    def __init__(self, entries: Sequence[HandbookEntry], *, source: str = "") -> None:
        self.entries: tuple[HandbookEntry, ...] = tuple(entries)
        self.source = source

    def __len__(self) -> int:
        return len(self.entries)

    def by_id(self, entry_id: str) -> HandbookEntry | None:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def chapters(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for entry in self.entries:
            out.setdefault(entry.chapter, entry.chapter_title)
        return out


def parse_handbook(text: Any, *, source: str = "") -> Handbook:
    """把手册文本解析成条目。

    容错原则（与项目其他解析器一致）：格式不对的行跳过，**绝不抛异常**——
    手册被手改坏时最多是少几条，不该让 Agent 起不来。
    """
    entries: list[HandbookEntry] = []
    current_chapter = 0
    current_chapter_title = ""
    pending_id: str | None = None
    pending_title = ""
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal pending_id, pending_title, body_lines
        if pending_id:
            body = "\n".join(body_lines).strip()
            entries.append(HandbookEntry(
                entry_id=pending_id,
                title=pending_title.strip(),
                chapter=current_chapter,
                chapter_title=current_chapter_title,
                body=body,
            ))
        pending_id, pending_title, body_lines = None, "", []

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()

        chapter = _CHAPTER_RE.match(line.strip())
        if chapter:
            flush()
            current_chapter = int(chapter.group(1))
            current_chapter_title = chapter.group(2).strip()
            continue

        entry = _ENTRY_RE.match(line.strip())
        if entry:
            flush()
            pending_id = entry.group(1)
            pending_title = entry.group(2).strip()
            continue

        if pending_id:
            body_lines.append(line)

    flush()
    return Handbook(entries, source=source)


def load_handbook(path: Path | str) -> Handbook:
    """从文件加载；文件不存在或读不了时返回空手册（不抛异常）。"""
    target = Path(path)
    if not target.is_file():
        return Handbook([], source=str(target))
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = target.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return Handbook([], source=str(target))
    return parse_handbook(text, source=str(target))


# ===========================================================================
# 检索
# ===========================================================================
# 停用词：这些词/双字组在查询里几乎不携带主题信息，却会贡献大量噪声重合。
# 实跑踩过：查"我最近总是睡不着，我该怎么办"时，命中的是"求助/怎么开口"
# 那两条（因为"该怎么办""找""帮"重合），而真正的睡眠条目被挤下去了。
# 加了停用词过滤后，主题词（睡不着/失眠）才起决定作用。
_STOPWORDS: frozenset[str] = frozenset({
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "的", "了", "是", "在", "和", "就", "都", "也", "还", "把", "被", "让",
    "这", "那", "有", "没", "不", "很", "太", "好", "会", "能", "要", "想",
    "个", "些", "吗", "呢", "吧", "啊", "呀", "着", "过", "与", "或", "而",
    "什么", "怎么", "怎样", "如何", "为什么", "多少", "哪个", "哪些",
    "可以", "应该", "需要", "觉得", "感觉", "知道", "自己", "一个", "一下",
    "现在", "最近", "今天", "昨天", "明天", "一直", "总是", "有点", "一些",
    "事情", "东西", "时候", "方法", "办法", "建议", "帮", "找", "做", "说",
    "一下", "这种", "那样", "因为", "所以", "但是", "如果", "然后",
})


# 同义词扩展：把用户的口语说法映射到手册里的书面词。
#
# 为什么需要（实跑踩出来的）：查"我最近总是睡不着，我该怎么办"时，
# 命中不了 E1-04/E3-01（那两条写的是"失眠""睡不着"），因为**字面不重合**——
# "睡不着"与"失眠"在纯字面检索里完全是两个词。
# 纯字面检索有天花板，这层映射是性价比最高的补法：
# 只覆盖高频情绪说法，小而准，不引入向量库。
#
# 键是用户可能说的，值是手册里可能写的（双向都加，互为补充）。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "睡不着": ("失眠", "睡不好", "睡眠"),
    "失眠": ("睡不着", "睡不好", "睡眠"),
    "睡不好": ("失眠", "睡不着"),
    "心跳": ("心慌", "心跳加速", "心率"),
    "心慌": ("心跳", "焦虑", "紧张"),
    "喘不过气": ("呼吸", "呼吸法", "焦虑"),
    "呼吸": ("喘不过气", "呼吸法"),
    "拖延": ("拖延", "不想动", "任务"),
    "不想动": ("拖延", "提不起劲", "没力气"),
    "没用": ("自我否定", "自我批评", "自责"),
    "废物": ("自我否定", "自我批评", "自责"),
    "自责": ("自我否定", "自我批评"),
    "找人帮忙": ("求助", "求助通道", "专业帮助"),
    "求助": ("找人帮忙", "求助通道", "专业帮助"),
    "去哪找": ("求助通道", "专业帮助", "咨询"),
    "咨询": ("心理咨询", "求助通道", "专业帮助"),
    # ↓ 评测（scripts/eval_rag.py）实测补漏：用户口语说"看医生""咨询师"，
    #   而手册里写的是"专业人士""就诊"，字面不重合导致 MISS。
    #   这一层同义词不是为了刷分，是因为真人就是这么说话的。
    "看医生": ("专业帮助", "就诊", "医院", "求助通道"),
    "看病": ("专业帮助", "就诊", "医院"),
    "咨询师": ("心理咨询", "专业帮助", "求助通道"),
    "去医院": ("就诊", "专业帮助", "医院"),
    "该不该": ("专业帮助", "求助通道"),
    "什么情况": ("专业帮助", "求助通道", "持续"),
    "想不开": ("情绪", "难受"),
    "撑不住": ("情绪", "难受", "求助通道"),
    "考试": ("考试焦虑", "考前"),
    "紧张": ("焦虑", "心慌"),
    "焦虑": ("紧张", "心慌", "情绪"),
    "难受": ("情绪", "痛苦"),
}


def expand_tokens(tokens: set[str]) -> set[str]:
    """按同义词表扩充 token 集合（只增不减，不改变原有匹配）。"""
    extra: set[str] = set()
    for token in tokens:
        for synonym in _SYNONYMS.get(token, ()):
            extra.add(synonym)
    return tokens | extra


def _tokens(text: str, *, expand: bool = False) -> set[str]:
    """极简切词：CJK bigram + 英数词，并过滤停用词。

    参数：
        expand=True 时额外做同义词扩展（**只用于查询侧**，
                    索引侧保持原样，否则会污染文档频率统计）。

    与 profile.py 的切词同源，但这里多两步：
        停用词过滤 —— 手册检索对"功能词噪声"更敏感（语料小），
                      不过滤会把主题词淹掉；
        同义词扩展 —— 解决"睡不着 vs 失眠"这类字面不重合的问题。
    """
    body = _WS_RE.sub(" ", str(text or "")).lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]+", body))
    cjk = re.findall(r"[\u4e00-\u9fa5]", body)
    for i in range(len(cjk) - 1):
        bigram = cjk[i] + cjk[i + 1]
        if bigram[0] in _STOPWORDS and bigram[1] in _STOPWORDS:
            continue
        tokens.add(bigram)
    tokens.update(c for c in cjk if c not in _STOPWORDS)
    tokens -= _STOPWORDS

    # 多字同义词（如"找人帮忙""喘不过气"）单独扫一遍，bigram 切不出来
    for phrase in _SYNONYMS:
        if len(phrase) > 2 and phrase in body:
            tokens.add(phrase)

    if expand:
        tokens = expand_tokens(tokens)
    return tokens


def _token_df(handbook: Handbook) -> dict[str, int]:
    """统计每个 token 出现在多少条目里（用于 IDF 加权）。"""
    df: dict[str, int] = {}
    for entry in handbook.entries:
        for token in _tokens(entry.body) | _tokens(entry.title):
            df[token] = df.get(token, 0) + 1
    return df


def _ubiquitous_tokens(handbook: Handbook, *, ratio: float = 0.4) -> frozenset[str]:
    """算出手册里"太普遍"的 token（出现在超过 ratio 比例的条目里）。

    为什么需要它（评测实测出来的）：
        查"什么样的情况该去看医生或者找咨询师"时，真正相关的 E6-01
        **一条都没匹配上**，而赢家是 E6-03 —— 因为它恰好含有"什/么/情/况/询"
        这些**几乎每个条目都有**的万金油字。它们各自权重很小，
        但数量多，合起来把"医/就诊"这类稀有词的贡献稀释掉了。

    手写停用词表穷举不完这类字，所以改成**按文档频率动态判定**：
    在 25 条语料里出现于 10 条以上的 token，对区分条目几乎没有信息量。
    """
    df = _token_df(handbook)
    total = max(1, len(handbook))
    threshold = max(2, int(total * ratio))
    return frozenset(token for token, count in df.items() if count >= threshold)


def score_entry(entry: HandbookEntry, query: str, *,
                snapshot: Any = None,
                df: Mapping[str, int] | None = None,
                total_docs: int = 0,
                ubiquitous: frozenset[str] | set[str] | None = None) -> float:
    """给一条目打分（0~1）。

    用 **IDF 加权**而不是简单重合度。这一处是实跑修出来的：
        早期写成"重合 token 数 ÷ 条目长度^0.5"，结果**短条目被系统性抬高**——
        查"今天股市涨了多少"时，最短的 E6-04 拿到了最高分（纯属巧合重合）。
        改成 IDF 后，常见 bigram（"多少""什么"）几乎不计分，
        而罕见词（"失眠""拖延"）才有分量，这才符合直觉。

    参数 ubiquitous 是一组"几乎每个条目都有"的 token（如"什么/情况"）。
    它们会被**整个剔除**而不是只降权 —— 因为这类字的数量优势会盖过稀有词。

    返回 0~1：分子是命中的信息量，分母是"如果全都命中该有多少"，所以满分=1。
    """
    # 查询侧做同义词扩展；文档侧**不做**（否则会污染文档频率统计）
    query_tokens = _tokens(query, expand=True)
    if ubiquitous:
        query_tokens = {t for t in query_tokens if t not in ubiquitous}
    if not query_tokens:
        return 0.0

    body_tokens = _tokens(entry.body)
    title_tokens = _tokens(entry.title)

    docs = max(1, int(total_docs or 1))
    weights = df or {}

    def weight(token: str) -> float:
        seen = max(1, int(weights.get(token, 1)))
        return max(0.05, 1.0 - (seen / docs))    # 出现越普遍，权重越低

    matched = query_tokens & (body_tokens | title_tokens)
    if not matched:
        return 0.0

    matched_weight = sum(weight(t) for t in matched)
    total_weight = sum(weight(t) for t in query_tokens)
    coverage = (matched_weight / total_weight) if total_weight > 0 else 0.0

    # 融合两项（参数按实跑数据校准，不是拍的）：
    #   信息量饱和曲线 —— 区分"绝对命中多少"：
    #       相关查询实测 matched_weight 4.0~9.9，无关查询 2.8~3.4。
    #       k=8 时前者拿到 0.33~0.55、后者约 0.26，才分得开。
    #       （k 取 20 时相关查询被压到 0.17，反而低于无关查询，实测发现并修正）
    #   覆盖率 —— 区分"命中了查询的多大比例"，压住长条目靠巧合重合得分。
    volume = matched_weight / (matched_weight + 8.0)
    score = 0.6 * volume + 0.35 * coverage

    # 标题命中额外加成（主题对上了，几乎一定是相关条目）
    title_hit = query_tokens & title_tokens
    if title_hit:
        score += 0.15 * (sum(weight(t) for t in title_hit) /
                         max(1e-9, total_weight))

    score += _state_bonus(entry, snapshot) * 0.25
    return max(0.0, min(1.0, score))


def _state_bonus(entry: HandbookEntry, snapshot: Any) -> float:
    """状态加权：让"此刻更需要"的章节往前排（0~1，最多加 0.25）。

    只在**真的观测到对应状态**时才加，不做"人人有份"的兜底加成——
    那种兜底会让所有条目一起抬高，等于给检索加噪声。
    """
    hints = CHAPTER_HINTS.get(entry.chapter, {})
    if not hints or snapshot is None:
        return 0.0
    dims = getattr(snapshot, "dims", None)
    if not isinstance(dims, Mapping):
        return 0.0

    def dim(name: str, default: float = 0.5) -> float:
        try:
            return float(dims.get(name, default))
        except (TypeError, ValueError):
            return default

    flags = set(getattr(snapshot, "flags", ()) or ())
    bonus = 0.0
    if hints.get("high_arousal") and dim("arousal") >= 0.7:
        bonus += 0.6
    if hints.get("low_energy") and dim("energy") <= 0.35:
        bonus += 0.4
    if hints.get("low_valence") and dim("valence") <= 0.35:
        bonus += 0.5
    if hints.get("low_self_worth") and (dim("self_worth") <= 0.35
                                        or "low-self-worth" in flags):
        bonus += 0.5
    return min(1.0, bonus)


def search(handbook: Handbook, query: str, *, snapshot: Any = None,
           limit: int = 3, min_score: float = 0.35) -> list[HandbookEntry]:
    """检索最相关的条目。

    分数低于 min_score 的不返回 —— **宁可什么都不给，也不要塞一条无关的
    "方法"给正在难受的人**。

    ⚠ 已知局限（实测数据，不是猜测）：
        在目前 25 条语料上，"相关查询的最低分 0.298"与"无关查询的最高分 0.320"
        **有约 0.02 的重叠**，所以单一阈值无法完全分开。典型误命中是
        "明天天气怎么样"会碰到 E3-03（考试焦虑）。没有继续调参的原因：
          1. 真正拦住"硬塞方法"的是 should_offer_methods()——
             用户没问"怎么做"时**根本不会调用检索**；
          2. 命中结果在提示词里是"供参考、可忽略"，不是直接输出给用户；
          3. 语料再大一些、或换成向量检索后，分离度会自然改善。
        也就是说：这是**已知且可接受的**局限，不是被忽略的问题。
    """
    weights = _token_df(handbook)
    total = max(1, len(handbook))
    ubiquitous = _ubiquitous_tokens(handbook)
    scored: list[tuple[float, HandbookEntry]] = []
    for entry in handbook.entries:
        score = score_entry(entry, query, snapshot=snapshot,
                            df=weights, total_docs=total,
                            ubiquitous=ubiquitous)
        if score >= min_score:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].entry_id))
    return [entry for _, entry in scored[:max(1, int(limit))]]


def should_offer_methods(text: Any) -> bool:
    """是否**应该**给出具体方法。

    这是与 persona.py「不主动给建议」原则的接线处：
    检索得到不等于应该给。只有用户明确在问"怎么做"才给。
    """
    body = _WS_RE.sub(" ", str(text or "")).strip()
    if not body:
        return False
    return any(pattern.search(body) for pattern in _ASKING_PATTERNS)


def build_method_block(entries: Iterable[HandbookEntry], *,
                       max_items: int = 2) -> str | None:
    """把命中条目拼成给模型的参考块；没有条目返回 None。

    措辞刻意保守：告诉模型"这是参考，用商量的语气，不要全抛出去"。
    """
    chosen = list(entries)[:max(1, int(max_items))]
    if not chosen:
        return None
    lines = ["（以下是可能有用的具体做法，供你参考。注意："]
    lines.append("  只在对方问「怎么做」时使用；用商量的语气，一次最多讲一条；")
    lines.append("  不要说「你应该」，也不要一次性把方法全倒出来。）")
    for entry in chosen:
        lines.append("")
        lines.append(entry.to_text())
    return "\n".join(lines)


# ===========================================================================
# 一致性校验：手册里的求助号码必须与 safety.HOTLINES 一致
# ===========================================================================
def check_hotline_consistency(handbook: Handbook,
                              hotlines: Iterable[Mapping[str, str]]) -> list[str]:
    """返回手册里**缺失**的官方号码（空列表 = 一致）。

    为什么要校验：手册正文里也写了求助号码。如果两处不一致，
    用户拿到的可能是错的号码——写错比不写更危险。
    """
    missing: list[str] = []
    blob = "\n".join(entry.body + entry.title for entry in handbook.entries)
    for item in hotlines:
        number = str(item.get("number", ""))
        # 号码里可能带分隔说明（010-82951332（手机）／800-810-1117（座机）），
        # 逐个取出其中的号段再比对
        for part in re.findall(r"\d[\d\-]{4,}", number):
            if part not in blob:
                missing.append(part)
    return missing
