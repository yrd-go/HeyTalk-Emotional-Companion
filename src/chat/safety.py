# -*- coding: utf-8 -*-
"""
src/chat/safety.py —— 情绪陪伴 Agent 的安全闸（P0，任何 LLM 调用之前）
=====================================================================

设计立场（这是全项目唯一不接受妥协的一块）
--------------------------------------------------------------------------
陪伴类 Agent 的**漏报代价不可逆**：模型把一次真实的危机信号当成普通抱怨，
用户得到的是一段温柔的废话，而真正的求助窗口已经关掉了。因此：

  1. **硬规则前置**：本模块是纯确定性实现，零第三方依赖、零网络、零 LLM。
     它在任何模型调用之前跑完，且**不依赖模型是否可用**（对齐 rag_demo.py 的
     `--keyword-only` 思路：断网、无 Key、模型挂了，安全闸照样工作）。
  2. **高危不交给模型自由发挥**：命中 HIGH 时调用方必须使用本模块的固定话术
     （`build_safe_reply`），让模型改写危机回应是明确的错误用法。
  3. **不把命中的原文回显进上下文**：`SafetyHit` 只保留规则 id、等级、位置与
     处理指令，**刻意不保存命中的原文片段**，避免自伤/自杀表述被二次拼进
     Prompt、写进日志或渲染到公网页面（与 paths.py 的 mask_secrets 同一层意图）。
     界面若要解释"为什么被识别"，请只展示 `rule_id`。
  4. **宁可误报**：日常口语（"烦死了""笑死""不想上班"）通过**词组边界 + 否定
     检测**压低误报，但接受少量误报——安全场景下误报只是多说一句关心。
  5. **不做诊断、不做疗效承诺**：本模块只输出"关注等级 + 求助通道"，
     绝不输出任何疾病名称或严重程度判定。

与其余模块的关系
--------------------------------------------------------------------------
    safety.py   只做三件事：判定等级 / 给处理指令 / 给固定话术与资源
    state.py    情绪状态（valence / arousal ...），**风险等级只从本模块取**，
                不允许由 LLM 或词典打分推断
    chat_app.py 渲染：按 `level` 选择 UI 分支；HIGH 必须走人机接力提示

使用：
    from chat.safety import check_safety, build_safe_reply, HOTLINES

    hit = check_safety(user_text)
    if hit.level is SafetyLevel.HIGH:
        reply = build_safe_reply(hit, region="cn")   # 固定话术，不改写

    python src/chat/safety.py "我不想活了"     # 命令行自检（不打印用户原文）
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

__all__ = [
    "SafetyLevel",
    "SafetyHit",
    "SafetyRule",
    "HOTLINES",
    "CRISIS_DISCLAIMER",
    "HIGH_RULES",
    "MEDIUM_RULES",
    "LOW_RULES",
    "normalize_text",
    "check_safety",
    "build_safe_reply",
    "contains_resource_reference",
    "iter_rules",
    "validate_rules",
]


# ===========================================================================
# 0. 等级
# ===========================================================================
class SafetyLevel(str, Enum):
    """三档关注等级（不是诊断，只决定"这次该怎么接"）。"""

    NONE = "none"       # 没有危机信号：可以正常走 LLM 对话
    LOW = "low"         # 一般负向情绪：正常对话 + 轻量关心
    MEDIUM = "medium"   # 显著痛苦 / 无望感，但无明确自伤意图：回应里必须给出求助通道
    HIGH = "high"       # 有自伤/自杀意图或计划：**禁止模型自由发挥**，走固定话术 + 人机接力

    @property
    def severity(self) -> int:
        return {"none": 0, "low": 1, "medium": 2, "high": 3}[self.value]

    @property
    def blocks_freeform_llm(self) -> bool:
        """HIGH 必须由确定性话术接管；MEDIUM 允许模型参与但必须带资源。"""
        return self is SafetyLevel.HIGH


# ===========================================================================
# 1. 规则定义
#
#    用「短语」而不是单个字/词：单字匹配（如 "死"、"累"）会把
#    「笑死」「烦死了」「累并快乐着」全部误判。
# ===========================================================================
@dataclass(frozen=True)
class SafetyRule:
    rule_id: str                      # 稳定标识：界面与测试都靠它，不要随意改
    level: SafetyLevel
    pattern: re.Pattern
    action: str                       # 给调用方的处理指令
    note: str = ""                    # 给维护者看的说明（不面向用户）

    @property
    def regex(self) -> str:
        return self.pattern.pattern


def _rule(rule_id: str, level: SafetyLevel, pattern: str, action: str, note: str = "") -> SafetyRule:
    return SafetyRule(rule_id, level, re.compile(pattern), action, note)


# --- 高危：明确的自伤/自杀意图、计划、手段、告别 -------------------------
HIGH_RULES: tuple[SafetyRule, ...] = (
    _rule("H-INTENT-SUICIDE", SafetyLevel.HIGH,
          r"想自杀|要自杀|去自杀|自杀(?:的)?(?:念头|想法)|"
          # 单独出现"自杀"也算（实测漏报：「我绝对不会自杀」里"绝对不会"后面
          # 没有"想"，旧写法只认"想自杀"于是整句都没匹配上，
          # 否定过滤也就永远不会被触发）。否定由 _is_negated 负责压回。
          r"自杀|"
          r"想死|想死去|不如死|"
          r"不想活(?:了|着)?|活不下去|不想(?:再)?活|活着没(?:有)?(?:意思|意义)|"
          r"活着(?:也)?没(?:有)?(?:意思|意义)|死了算了|去死算了|"
          r"结束(?:自己的)?生命|了结(?:自己|此生)|解脱(?:了)?$",
          "立即进入危机响应：固定话术 + 高危资源 + 建议人机接力"),
    _rule("H-INTENT-SELFHARM", SafetyLevel.HIGH,
          r"自残|自伤|割腕|划(?:自己的)?(?:手腕|手臂)|伤害自己|惩罚自己到",
          "立即进入危机响应：固定话术 + 高危资源 + 建议人机接力"),
    _rule("H-PLAN", SafetyLevel.HIGH,
          r"(?:准备|打算|计划|决定)(?:要)?(?:自杀|去死|了结)|"
          r"遗书|写(?:好|完)(?:了)?(?:遗书|告别)|"
          r"交代(?:一下)?(?:我的)?后事|把后事交代|后事(?:都)?交代|"
          r"告别(?:一下|世界)|"
          r"最后(?:一条|一次)(?:消息|说话)|已经买(?:好|了)(?:安眠药|药|绳|刀)",
          "疑似已有计划：立即危机响应，不要追问手段细节",
          note="覆盖「交代后事」的多种语序：交代后事 / 把后事交代一下 / 后事都交代了"),
    _rule("H-ACCESS", SafetyLevel.HIGH,
          r"手里有(?:药|刀|绳)|攒了(?:很多)?(?:安眠药)|(?:安眠药|农药)(?:都)?准备好了|"
          r"找到(?:了)?(?:方法|办法)(?:了)?$|(?:想|要)?(?:从)?(?:楼|天台|桥)(?:上)?跳",
          "疑似具备手段/场所：立即危机响应，不要追问细节"),
    _rule("H-FAREWELL", SafetyLevel.HIGH,
          r"(?:谢谢|感谢)(?:大家|你们|你)(?:一直(?:以来)?的)?(?:照顾|陪伴|关心)|"
          r"(?:以后|接下来)(?:就)?(?:拜托|麻烦)(?:你|你们|大家)了|"
          r"再也(?:不用|不会)(?:麻烦|拖累)(?:你|你们|大家)?(?:了)?|"
          r"我(?:走|离开)(?:了)?以后",
          "疑似告别/交代：按高危处理（告别式表达是高危信号）"),
    _rule("H-METHOD", SafetyLevel.HIGH,
          r"自杀的?方法|自杀方式|查(?:过|了)?(?:自杀|怎么死|如何死)|"
          r"怎么(?:样)?(?:才能)?死|如何(?:自杀|结束)|哪种(?:死法|方式)|"
          r"想过(?:具体)?怎么结束|想(?:用)?(?:什么|哪种)方式(?:结束|走)|"
          r"(?:在)?想用什么方式|用什么方式(?:结束|走)|"
          r"走那条路|一了百了",
          "出现方法/计划类语言：立即危机响应，**不要**追问或讨论任何具体手段",
          note=("提到「方法」是计划性的强信号。注意本规则刻意只做识别，"
                "绝不追问、不比较、不提供任何手段细节。")),
)

# --- 中危：显著痛苦、无望、成为负担感、情感麻木 -------------------------
MEDIUM_RULES: tuple[SafetyRule, ...] = (
    _rule("M-HOPELESS", SafetyLevel.MEDIUM,
          r"撑不下去|坚持不住|熬不下去|扛不住|无望|没(?:有)?希望|看不到希望|绝望|"
          r"没有出路|怎么办都(?:没用|不行)|怎么努力都没用|"
          r"活着(?:好|很|太)?累|活着太辛苦|生无可恋|"
          # 「活着没什么意思」/「活着没意思」是很常见的说法，原规则漏了它
          # （实测：这句话当时被判成 NONE，连求助通道都不会给）
          r"活着(?:也|真|挺|很)?没(?:什么|啥)?(?:意思|意义|劲)",
          "回应必须包含：情绪反映 + 求助通道（不急于给建议）"),
    _rule("M-BURDEN", SafetyLevel.MEDIUM,
          # 允许"我是"前后插入常见修饰：感觉/觉得/真的；别人的/大家的。
          # 实跑发现的漏报缺口：人说的是「**感觉自己是负担**」
          # 「觉得自己是别人的负担」，而原规则要求"我是"紧挨着，
          # 这类最自然的说法一条都匹配不上。
          r"(?:感觉|觉得|总|一直|真的)?我(?:就)?是"
          r"(?:(?:别人|大家|家人|他们)的)?(?:个|一个)?(?:累赘|负担|废物|多余的人)|"
          r"(?:感觉|觉得)?自己(?:就)?是"
          r"(?:(?:别人|大家|家人|他们)的)?(?:个|一个)?(?:累赘|负担|废物|多余的人)|"
          r"拖累(?:别人|大家|家人)|"
          r"没有我(?:大家|你们|他们|别人)(?:会)?更好|没有我(?:会)?更好|"
          r"我(?:的)?存在(?:就是)?(?:多余|错误)|我(?:很|太)?没用",
          "「成为负担」是独立的高危近邻信号：必须给出求助通道并核对安全"),
    _rule("M-NUMB", SafetyLevel.MEDIUM,
          r"麻木|感受不到(?:任何)?(?:情绪|感觉)|行尸走肉|什么都(?:不想|提不起)|"
          r"对什么(?:都)?没(?:有)?(?:兴趣|感觉)|空(?:空)的|像(?:个)?空壳",
          "情感麻木：不追问原因，先陪伴，给出求助通道"),
    _rule("M-ANHEDONIA", SafetyLevel.MEDIUM,
          r"睡不(?:着|好)|整夜(?:睡不|没睡)|吃不下|吃不下饭|不想吃(?:东西|饭)|"
          r"一直哭|哭了(?:一整天|好久)|控制不住(?:地)?哭",
          "持续睡眠/进食/哭泣问题：给出求助通道 + 建议寻求专业支持"),
    _rule("M-VANISH", SafetyLevel.MEDIUM,
          r"突然消失|消失(?:掉)?(?:好了|算了)|不想(?:再)?(?:联系|打扰)(?:任何|所有)?人|"
          r"谁都不想见|想(?:一个人)?躲起来(?:不联系)",
          "社交撤离倾向：核对安全，给出求助通道"),
)

# --- 低危：一般负向情绪（不触发危机流程，只做轻量关心） -----------------
LOW_RULES: tuple[SafetyRule, ...] = (
    _rule("L-NEGATIVE", SafetyLevel.LOW,
          # 实跑补漏：原写成「压力(?:很|好|太)?大」，漏掉了中文里常见的
          # 「压力**也**很大」「压力**都**大」——副词插在中间就匹配不到。
          # 这类说法在日常表达里非常自然，漏掉等于白白丢掉一次关心机会。
          r"难过|伤心|委屈|焦虑|紧张|害怕|恐惧|烦躁|烦死|郁闷|压抑|崩溃|"
          r"压力[也都还]?(?:很|好|太|挺)?大|"
          r"受不了|心累|(?:好|很|太|挺)累|疲惫|"
          r"孤独|孤单|没(?:有)?人(?:懂|理解)我|被(?:否定|忽视)|内耗",
          "正常对话 + 轻量情绪反映；不要急于给建议",
          note="只触发低危，不进入危机流程；单个词命中不算（见 LOW_RULE_MIN_HITS）"),
)


# ===========================================================================
# 2. 否定与强度修饰
#
#    只有 HIGH 允许被否定词压回（「我绝对不会自杀」不该拉响最高警报）；
#    MEDIUM 一律不压——「我不觉得自己是累赘」本身仍在讨论负担感，
#    交给模型/人去接更安全。这是刻意的非对称设计。
# ===========================================================================
_NEGATION_CHARS = "不没无别莫勿非未"
_NEGATION_PHRASES = (
    "不会", "不想", "没有", "从未", "从不", "绝不", "绝对不", "根本没有",
    "谈不上", "不至于", "不可能", "从来没", "没想过", "不会是", "不是",
)

# 程度副词：用于给情绪打分加权（"有点难过" != "非常难过"）
_INTENSIFIERS = {
    "非常": 1.4, "特别": 1.3, "极其": 1.5, "超级": 1.4, "太": 1.3,
    "很": 1.15, "好": 1.15, "真": 1.2, "真的": 1.25, "越来越": 1.3,
    "一直": 1.25, "总是": 1.25, "反复": 1.2, "严重": 1.4, "彻底": 1.4,
}
_DIMINISHERS = {
    "有点": 0.6, "稍微": 0.5, "一点点": 0.5, "还好": 0.5, "算是": 0.7,
    "偶尔": 0.6, "有时候": 0.7, "不太": 0.7,
}

# 否定判定的作用域上限（见 _is_negated）：
#   向前只看到**当前分句内**最多 6 个字。
# 为什么不是 10：实测「我不会自杀，但我真的不想活了」——窗口给到 10 时，
# 逗号前的"不"会越过分句去压掉后半句真实的求救信号（hits 直接变 0）。
# 危险场景下"否定跨句生效"是最坏的一类漏报。
# 6 足够覆盖「绝对不会自杀」（不→自 间隔 6）与「从来没有想过自杀」，
# 又短到不会跨过一个逗号。
_NEG_SCOPE = 6
_NEG_LOOKAHEAD = 5  # 向后看几个字符（覆盖「自杀是不会的」这类倒装）

# 中文标点 + 空白归一化；同时保留感叹/问号用于唤醒度估计
_WS_RE = re.compile(r"\s+")

LOW_RULE_MIN_HITS = 2   # 单个"焦虑"不算低危信号，避免噪音
MAX_HITS_PER_RULE = 3   # 每条规则最多记几次，防止长文本刷爆 context


def normalize_text(text: Any) -> str:
    """归一化：统一省略号/引号/破折号，去多余空白。

    只做「等价字符统一」，不做同义词替换、不做繁体转换——安全判定要可预测、
    可回归，任何语义级改写都必须显式写在规则里。
    """
    if not text:
        return ""
    s = str(text)
    s = (s.replace("\u3000", " ")
          .replace("…", "...").replace("．", ".")
          .replace("　", " "))
    # 全角标点统一为半角，便于规则书写
    for full, half in (("，", ","), ("。", "."), ("！", "!"), ("？", "?"),
                       ("；", ";"), ("：", ":"), ("、", ","),
                       ("（", "("), ("）", ")"), ("“", "\""), ("”", "\""),
                       ("‘", "'"), ("’", "'")):
        s = s.replace(full, half)
    return _WS_RE.sub(" ", s).strip()


_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;:!?、。；：！？…\s]")


def _negation_scope(text: str, start: int) -> str:
    """取「当前分句内」的否定窗口：从上一个标点/空白之后开始，最多 _NEG_SCOPE 字。

    分句边界是这一步的关键：没有它，「我不会自杀，但我真的不想活了」里的
    "不" 会跨过逗号把后半句的真实求救吞掉。
    """
    window_start = max(0, start - _NEG_SCOPE)
    before = text[window_start:start]
    boundary = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(before):
        boundary = match.end()
    before = before[boundary:]
    return before


def _is_negated(text: str, start: int, end: int) -> bool:
    """判断 [start, end) 命中的短语是否被否定词包裹（**限当前分句**）。"""
    before = _negation_scope(text, start)
    after = text[end:end + _NEG_LOOKAHEAD]
    if any(ch in before for ch in _NEGATION_CHARS):
        return True
    return any(phrase in (before + after) for phrase in _NEGATION_PHRASES)


def _intensity(text: str, start: int, end: int) -> float:
    """取命中点附近的程度修饰，返回分值乘数（默认 1.0）。"""
    window_before = text[max(0, start - 4):start]
    window_after = text[end:end + 3]
    factor = 1.0
    for word, value in _INTENSIFIERS.items():
        if word in window_before or word in window_after:
            factor = max(factor, value)
    for word, value in _DIMINISHERS.items():
        if word in window_before:
            factor = min(factor, value)
    return factor


# ===========================================================================
# 3. 检测结果
# ===========================================================================
@dataclass(frozen=True)
class SafetyHit:
    """一次安全判定的结果。

    刻意**不包含命中的原文片段**：
        - 自伤/自杀表述不应被二次拼进 LLM 的 Prompt；
        - 不应写进日志（本模块也不打印它）；
        - 不应渲染到公网页面（对齐 dashboard.py 的 mask_secrets 出口纪律）。
    界面要解释"为什么被识别"，请只展示 `hits`（规则 id 列表）。
    """

    level: SafetyLevel
    rule_ids: tuple[str, ...]
    actions: tuple[str, ...]
    hit_count: int
    negation_filtered: int
    intensity: float
    text_length: int

    @property
    def triggered(self) -> bool:
        return self.level is not SafetyLevel.NONE

    @property
    def blocks_freeform_llm(self) -> bool:
        return self.level.blocks_freeform_llm

    def to_dict(self) -> dict[str, Any]:
        """可安全落盘/上页面的结构（无原文）。"""
        return {
            "level": self.level.value,
            "rule_ids": list(self.rule_ids),
            "actions": list(self.actions),
            "hit_count": self.hit_count,
            "negation_filtered": self.negation_filtered,
            "intensity": round(self.intensity, 3),
            "text_length": self.text_length,
        }

    def label(self) -> str:
        """给界面用的短标签（只含等级与规则 id）。"""
        if not self.triggered:
            return "未触发安全规则"
        return f"{self.level.value.upper()}｜{'、'.join(self._short(self.rule_ids))}"

    @staticmethod
    def _short(rule_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(rid for rid in rule_ids[:4])


def iter_rules() -> tuple[SafetyRule, ...]:
    """全部规则（按等级从高到低），供自检与测试遍历。"""
    return HIGH_RULES + MEDIUM_RULES + LOW_RULES


def validate_rules() -> list[str]:
    """规则表自检：id 唯一、正则可编译、等级与所在表一致。

    返回问题列表（空列表 = 全部通过）。挂进测试，防止以后加规则时手滑。
    """
    problems: list[str] = []
    seen: set[str] = set()
    for group, expected in ((HIGH_RULES, SafetyLevel.HIGH),
                            (MEDIUM_RULES, SafetyLevel.MEDIUM),
                            (LOW_RULES, SafetyLevel.LOW)):
        for rule in group:
            if rule.rule_id in seen:
                problems.append(f"规则 id 重复：{rule.rule_id}")
            seen.add(rule.rule_id)
            if rule.level is not expected:
                problems.append(f"{rule.rule_id} 等级与所在规则表不一致：{rule.level}")
            try:
                re.compile(rule.regex)
            except re.error as exc:  # pragma: no cover - 除非有人写坏正则
                problems.append(f"{rule.rule_id} 正则无法编译：{exc}")
    return problems


# ===========================================================================
# 4. 主判定
# ===========================================================================
def _scan(text: str, rules: tuple[SafetyRule, ...],
          respect_negation: bool) -> tuple[list[str], list[str], int, int, float]:
    """扫描一组规则，返回 (rule_ids, actions, hit_count, negated, 强度峰值)。"""
    rule_ids: list[str] = []
    actions: list[str] = []
    hits = 0
    negated = 0
    peak = 1.0

    for rule in rules:
        count = 0
        for match in rule.pattern.finditer(text):
            if respect_negation and _is_negated(text, match.start(), match.end()):
                negated += 1
                continue
            count += 1
            peak = max(peak, _intensity(text, match.start(), match.end()))
            if count >= MAX_HITS_PER_RULE:
                break
        if count:
            hits += count
            if rule.rule_id not in rule_ids:
                rule_ids.append(rule.rule_id)
                actions.append(rule.action)

    return rule_ids, actions, hits, negated, peak


def check_safety(text: Any, *, low_min_hits: int = LOW_RULE_MIN_HITS) -> SafetyHit:
    """对一段用户输入做安全判定。纯函数、无副作用、可离线。

    等级规则（从高到低短路）：
        1. HIGH 命中（且未被否定词压回）→ HIGH
        2. MEDIUM 命中                    → MEDIUM
        3. LOW 命中数 >= low_min_hits     → LOW
        4. 其余                            → NONE

    注意 HIGH 一旦命中就不再关心 MEDIUM/LOW：危机响应优先，
    也避免把用户的痛苦细节堆进上下文。
    """
    normalized = normalize_text(text)

    high_ids, high_actions, high_hits, negated, peak = _scan(normalized, HIGH_RULES, respect_negation=True)
    if high_hits:
        return SafetyHit(SafetyLevel.HIGH, tuple(high_ids), tuple(high_actions),
                         high_hits, negated, peak, len(normalized))

    med_ids, med_actions, med_hits, med_negated, med_peak = _scan(normalized, MEDIUM_RULES, respect_negation=False)
    negated += med_negated
    peak = max(peak, med_peak)
    if med_hits:
        return SafetyHit(SafetyLevel.MEDIUM, tuple(med_ids), tuple(med_actions),
                         med_hits, negated, peak, len(normalized))

    low_ids, low_actions, low_hits, low_negated, low_peak = _scan(normalized, LOW_RULES, respect_negation=False)
    negated += low_negated
    peak = max(peak, low_peak)
    if low_hits >= max(1, int(low_min_hits)):
        return SafetyHit(SafetyLevel.LOW, tuple(low_ids), tuple(low_actions),
                         low_hits, negated, peak, len(normalized))

    return SafetyHit(SafetyLevel.NONE, (), (), 0, negated, peak, len(normalized))


# ===========================================================================
# 5. 资源清单（只写权威来源已公布的号码，附出处便于复核）
# ===========================================================================
HOTLINES: tuple[dict[str, str], ...] = (
    {
        "name": "全国统一心理援助热线",
        "number": "12356",
        "hours": "以当地接听安排为准",
        "scope": "中国内地",
        "source": "国家卫生健康委（2025 年统一启用）",
    },
    {
        "name": "北京市心理援助热线（北京心理危机研究与干预中心）",
        "number": "010-82951332（手机）／800-810-1117（座机）",
        "hours": "全年 7×24 小时",
        "scope": "全国可拨，北京地区为主",
        "source": "https://www.crisis.org.cn/lists/5.html",
    },
)

EMERGENCY_LINE = "如有立即的生命危险，请拨打 120（急救）或 110（报警），或直接前往最近医院急诊。"

CRISIS_DISCLAIMER = (
    "我不是心理咨询师或医生，无法做诊断，也不能替代专业帮助。"
    "我能做的是在这里陪着你、听你说。"
)


# ===========================================================================
# 6. 固定话术（按等级）
#
#    这些文本**不允许被模型改写**。原因：危机回应里最容易翻车的三件事
#    ——说教、承诺疗效、把责任推回给用户——全靠在确定性文本里避免。
# ===========================================================================
_HIGH_REPLY = """我听到你说的了。谢谢你把这句话说出来——这需要很大的力气。

我不敢也不该假装自己能替代专业帮助。**你现在的处境值得被真人认真接住**，
请现在就联系下面的通道（有人 24 小时接听）：

{lines}

{emergency}

如果你愿意，我也可以先陪你在这里待一会儿。你可以只告诉我：
**现在这一刻，你身边有人吗？** 不用回答别的。
"""

_MEDIUM_REPLY = """我听到你说的了，这些话听起来真的很沉。

我不急着给你建议——先确认一件事：**你现在还安全吗？**
如果脑子里出现过伤害自己的念头，请一定联系下面任意一个通道，有人会接：

{lines}

{disclaimer}
"""

_LOW_REPLY = """我在听。

{disclaimer}
"""


def format_hotlines(region: str = "cn", hotlines: Iterable[dict[str, str]] | None = None) -> str:
    """把资源清单渲染成纯文本（不依赖 Markdown，终端与网页都能用）。

    region 目前只区分 "cn"（内地）与 "other"；未收录地区返回通用引导，
    **绝不编造号码**。
    """
    items = list(hotlines if hotlines is not None else HOTLINES)
    if region != "cn":
        return ("- 请拨打你所在国家/地区的心理危机干预热线或急救电话（例如当地急救号码）。\n"
                "- 也可以联系你信任的人，让他们陪着你。")
    return "\n".join(
        f"- {item['name']}：{item['number']}（{item['hours']}｜{item['scope']}）"
        for item in items
    )


def build_safe_reply(hit: SafetyHit, *, region: str = "cn",
                     hotlines: Iterable[dict[str, str]] | None = None) -> str | None:
    """按等级返回**固定话术**。

    返回 None 表示"这一级不需要专用话术"，可以正常走 LLM 对话（NONE）。
    """
    if hit.level is SafetyLevel.NONE:
        return None

    if hit.level is SafetyLevel.HIGH:
        return _HIGH_REPLY.format(
            lines=format_hotlines(region, hotlines),
            emergency=EMERGENCY_LINE,
        )

    if hit.level is SafetyLevel.MEDIUM:
        return _MEDIUM_REPLY.format(
            lines=format_hotlines(region, hotlines),
            disclaimer=CRISIS_DISCLAIMER,
        )

    return _LOW_REPLY.format(disclaimer=CRISIS_DISCLAIMER)


def contains_resource_reference(text: Any,
                                hotlines: Iterable[dict[str, str]] | None = None) -> bool:
    """模型回复里是否已经给出可用的求助通道。

    用途：MEDIUM 等级允许模型参与回应（比固定话术更自然），但**必须**把求助
    通道给到用户。这里做出口校验，没给就由上层补上资源块——
    "让模型自由发挥"和"必须给资源"两件事都要成立，就靠这个函数兜底。
    """
    if not text:
        return False
    body = str(text)
    if "120" in body or "110" in body:
        return True
    items = list(hotlines if hotlines is not None else HOTLINES)
    for item in items:
        number = str(item.get("number", ""))
        # 号码里可能带分隔符（如 010-82951332），取主号码再比对
        primary = re.split(r"[（(／/]", number)[0].strip()
        if primary and primary in body:
            return True
    return False


# ===========================================================================
# 7. 命令行自检（刻意不打印用户原文）
# ===========================================================================
def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    if len(argv) < 2 or not argv[1].strip():
        print('用法：python src/chat/safety.py "一句话"')
        print("（为了不产生心理敏感日志，命中内容不会打印原文）")
        return 2

    hit = check_safety(argv[1])
    print("=" * 64)
    print(f"关注等级 ：{hit.level.value.upper()}")
    print(f"命中规则 ：{list(hit.rule_ids)}")
    print(f"命中次数 ：{hit.hit_count}（被否定词压回 {hit.negation_filtered} 次）")
    print(f"强度修饰 ：{hit.intensity:.2f}")
    print(f"阻断自由发挥：{'是' if hit.blocks_freeform_llm else '否'}")
    print(f"处理指令 ：{list(hit.actions)}")
    print("=" * 64)

    reply = build_safe_reply(hit)
    if reply:
        print("固定话术预览：")
        print(reply)
    else:
        print("未触发安全话术：可正常走 LLM 对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
