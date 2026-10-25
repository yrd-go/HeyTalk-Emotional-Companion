# -*- coding: utf-8 -*-
"""
src/chat/state.py —— 情绪/精神状态模型（P0：双轨制 + 确定性兜底）
=================================================================

为什么要有这个文件，而不是"让模型自己看着办"
--------------------------------------------------------------------------
陪伴类产品的头号失败模式是**状态漂移**：模型这次说"你很低落"，下次说
"你看起来不错"，而没有任何可复现的依据。所以本项目把"用户此刻的状态"
显式建模成一组**可回归测试的连续值**，而不是每次生成时临时发挥的情绪标签。

双轨制（对齐 rag_demo.py 的「主链路 + 零依赖兜底」思路）
--------------------------------------------------------------------------
    轨道 A：规则轨（本文件，纯标准库、零网络、零 LLM）
        - 词典 + 否定/强度修饰 + 会话内趋势；
        - **永远可用**：无 Key、断网、模型挂掉，状态照样更新；
        - 它是安全下限：任何情况下状态都不会因为"模型没回答"而丢掉。

    轨道 B：模型轨（`LLMStateExtractor`，**默认关闭**，显式开启）
        - 让 LLM 按严格 JSON 契约抽取更细的语义（反讽、隐含义、事件）；
        - 解析失败 / 越界 / 超时 → 自动回落轨道 A；
        - 两轨分歧时**以轨道 A 为下限**（见 `merge_tracks`），
          且风险等级**只允许来自 safety.py**，不接受词典打分或模型输出。

关键不变量（有测试钉住）
--------------------------------------------------------------------------
    1. 所有维度落在 [0, 1]，任何越界输入被钳制而不是抛错；
    2. 相同输入 → 相同输出（纯函数，无时间/随机依赖）；
    3. 风险等级**不由本模块推断**：`TurnRecord.safety_level` 必须由
       safety.check_safety() 的结果传入；本模块无权升降级；
    4. **本模块不保存用户原文**：`RuleTrack.extract()` 只输出命中的词与
       维度增量，不返回、不记录原始句子（隐私最小化，与 safety.py 同规则）；
    5. 持续下滑（连续下降且累计跌幅超阈值）会被显式标记，
       供上层决定"要不要主动关心一次"，而不是每轮都追问感受。

越用越懂你的基础（P1/P3 会接上）
--------------------------------------------------------------------------
    `EmotionState.to_dict()` 是稳定的序列化契约：P1 存进会话、
    P3 存进 Chroma 情景记忆并以"当下状态"为检索画像，都用这一个结构。

使用：
    from chat.state import EmotionState
    from chat.safety import check_safety

    state = EmotionState()
    record = state.update(user_text, safety=check_safety(user_text))
    print(record.snapshot.to_text())      # 给界面/日志看的可读摘要

    python src/chat/state.py "今天真的撑不住了，很累"    # 命令行自检
"""
from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DIMENSIONS",
    "StateSnapshot",
    "RuleTrack",
    "EmotionState",
    "TurnRecord",
    "LLMStateExtractor",
    "build_llm_prompt",
    "validate_llm_payload",
    "merge_tracks",
    "clamp01",
    "trend_of",
    "detect_sustained_decline",
    "validate_evidence",
    "ALLOWED_EVIDENCE_WORDS",
    "SELF_HARM_TERMS",
]

# ===========================================================================
# 0. 维度定义
# ===========================================================================
# 展示名与含义（写进文档，避免以后有人把 dominance 当成"支配欲"）
DIMENSIONS: dict[str, str] = {
    "valence": "愉悦度：0=很痛苦，0.5=中性，1=很好",
    "arousal": "唤醒度：0=迟钝麻木，0.5=平静，1=高度激动/焦躁",
    "dominance": "掌控感：0=完全无力，1=感觉能处理",
    "energy": "能量：0=耗竭，1=精力充足",
    "social_support": "社会支持感：0=孤立无援，1=有人可依靠",
    "self_worth": "自我价值感：0=自我否定，1=自我接纳",
    "alliance": "同盟感（对本 Agent）：0=疏离抗拒，1=信任敞开",
    "disclosure": "自我暴露深度：0=只谈事实，1=深谈内心",
}

NEUTRAL = 0.5                 # 未观测时的中性起点
SMOOTHING = 0.45              # 新证据权重：越大越敏感、越小越稳
STATE_MIN, STATE_MAX = 0.0, 1.0

# 趋势判定门槛
TREND_EPS = 0.005             # 相邻两轮差异小于它视为"持平"
DECLINE_MIN_TURNS = 2         # 连续不升的轮次
DECLINE_MIN_TOTAL = 0.20      # 该连续段内的累计跌幅
HISTORY_LIMIT = 200           # 轨迹最多留多少轮（防内存无限增长）

# 安全阀：任意单轮总偏移上限，防止一次极端文本把状态打到 0
MAX_TURN_SHIFT = 0.75

# 高危时需要清空的"内容维度"（状态不携带危机内容，交给 safety.py 决策）
_HIGH_RISK_DELTA = {"valence": -0.25, "arousal": 0.15, "dominance": -0.20}

# 自伤/危机词表：本模块**只用来做兜底标记**，真正的判定在 safety.py
SELF_HARM_TERMS: tuple[str, ...] = (
    "自杀", "自残", "自伤", "割腕", "轻生", "不想活", "想死", "活不下去",
)

# 与 safety.py 同源的否定字符（避免两处漂移）
_NEGATION_CHARS = "不没无别莫勿非未"
_NEGATION_PHRASES = (
    "不会", "不想", "没有", "从未", "从不", "绝不", "绝对不", "根本没有",
    "谈不上", "不至于", "不可能", "从来没", "没想过", "不会是", "不是",
)
_NEG_SCOPE = 6       # 否定只看当前分句内最多 6 字（与 safety.py 同一口径）
_NEG_LOOKAHEAD = 5
_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;:!?、。；：！？…\s]")

# 标点/字符级线索（不依赖任何词典）
_EXCLAIM_RE = re.compile(r"[!]{1,}")
_ELLIPSIS_RE = re.compile(r"\.{2,}")
_QUESTION_RE = re.compile(r"[?]")
_NUM_RE = re.compile(r"\d")
_WS_RE = re.compile(r"\s+")
_LAUGH_RE = re.compile(r"[哈嘿嘻呵]{2,}|哈哈|233+")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

# ===========================================================================
# 1. 词典（负面/正面/具体维度）
#
#    规则：只收「有信息量」的词，不收"死""累"这类单字——
#    单字匹配会把「笑死」「累并快乐着」全判成负向。
# ===========================================================================
NEGATIVE_WORDS: tuple[str, ...] = (
    "难过", "伤心", "委屈", "痛苦", "绝望", "无望", "崩溃", "压抑", "郁闷",
    "烦", "烦躁", "焦虑", "紧张", "害怕", "恐惧", "担心", "不安", "孤独",
    "孤单", "空落", "空虚", "失落", "沮丧", "颓", "丧", "想哭", "哭",
    "自责", "内疚", "羞耻", "后悔", "被否定", "被忽视", "被误解", "内耗",
    "受不了", "撑不住", "扛不住", "熬不住", "没意思", "没意义", "没希望",
    "无助", "无力", "没力气", "提不起劲", "emo",
    # ↓ 实跑补漏：这些词原先只在 energy 维度里，导致"最近好累"这种最常见的
    #   情绪表达**完全不影响 valence**（valence 停在 0.5），显然不对。
    #   它们同时影响 valence 与 energy，属于正常的多维命中。
    "累", "好累", "很累", "太累", "疲惫", "心累", "身心俱疲", "倦",
    "睡不着", "睡不好", "失眠", "吃不下", "没胃口", "难受", "不舒服",
    "低落", "消沉", "提不起精神", "烦躁不安", "恐慌", "绝望感",
)

POSITIVE_WORDS: tuple[str, ...] = (
    "开心", "高兴", "快乐", "愉快", "轻松", "放松", "平静", "安心", "踏实",
    "满意", "欣慰", "感动", "温暖", "有希望", "看到希望", "有动力", "有干劲",
    "有信心", "有把握", "感兴趣", "期待", "兴奋", "感激", "感谢",
    "好多了", "好起来", "缓过来", "缓过来一些", "想通了", "放下了",
    # ↓ 实跑补漏：这些是最日常的正向表达，原先一个都匹配不到，
    #   导致「今天好一点了」这类明显好转的输入完全不改变 valence。
    "好一点", "好一些", "好点", "不错", "还不错", "状态不错", "还行",
    "挺好的", "舒服", "顺利", "睡好了", "吃下了", "有精神", "有劲儿",
    "开心一点", "有改善", "进步了", "好转",
)

AROUSAL_HIGH_WORDS: tuple[str, ...] = (
    "崩溃", "焦躁", "烦躁", "爆炸", "失控", "慌", "害怕", "紧张", "激动",
    "兴奋", "愤怒", "生气", "气死", "抓狂", "坐立不安", "睡不着", "失眠",
    "心跳", "发抖", "喘不过气", "停不下来", "一直在想",
)

AROUSAL_LOW_WORDS: tuple[str, ...] = (
    "麻木", "没感觉", "迟钝", "不想动", "躺着", "发呆", "空心", "提不起劲",
    "很困", "睡了一天", "什么都不想干", "疲", "没力气", "懒得",
)

CONTROL_LOW_WORDS: tuple[str, ...] = (
    "没办法", "无能为力", "改变不了", "控制不了", "做不到", "不知道怎么办",
    "无力", "无助", "被推着", "由不得我", "没办法选择", "撑不住",
)

CONTROL_HIGH_WORDS: tuple[str, ...] = (
    "我可以", "我能", "有办法", "能处理", "应对", "能解决", "掌握", "掌控",
    "搞定了", "想清楚", "有头绪", "找到办法", "可以试试",
)

ENERGY_LOW_WORDS: tuple[str, ...] = (
    "累", "很累", "好累", "太累", "疲惫", "耗竭", "没力气", "没精力", "困",
    "睡不好", "失眠", "提不起", "不想动", "精力", "透支",
)

SUPPORT_LOW_WORDS: tuple[str, ...] = (
    "没人", "没有人", "没人懂", "没人理解", "孤立", "一个人", "孤独", "孤单",
    "无处可说", "不敢说", "没人问", "不会有人",
)

SUPPORT_HIGH_WORDS: tuple[str, ...] = (
    "朋友", "家人", "妈妈", "爸爸", "老婆", "老公", "男友", "女友", "同事",
    "有人陪", "有人懂", "有人听", "支持我", "理解我", "陪着我", "咨询师",
    "医生", "老师",
)

SELF_WORTH_LOW_WORDS: tuple[str, ...] = (
    "废物", "没用", "很失败", "失败", "不够好", "比不上", "丢人", "羞耻",
    "看不起自己", "讨厌自己", "恨自己", "不值得", "不配", "多余", "累赘",
    "负担", "拖累", "都是我的错",
)

SELF_WORTH_HIGH_WORDS: tuple[str, ...] = (
    "我做到了", "进步", "对自己好一点", "接纳", "原谅自己", "认可自己",
    "不是我的错", "我尽力了", "我值得",
)

ALLIANCE_HIGH_WORDS: tuple[str, ...] = (
    "谢谢你", "谢谢", "跟你说", "跟你聊", "还好有", "有你在", "愿意说",
    "想跟你说", "信任", "懂我",
)

ALLIANCE_LOW_WORDS: tuple[str, ...] = (
    "你根本不懂", "你不懂", "没用的", "说了也没用", "你也是程序", "别问了",
    "不想聊", "别管我", "闭嘴", "机器", "假", "敷衍",
)

OPENER_ALLIANCE_WORDS: tuple[str, ...] = (
    "你好", "在吗", "hi", "hello", "嗨", "早上好", "晚上好",
)

DISCLOSURE_HIGH_WORDS: tuple[str, ...] = (
    "我从来没跟别人说过", "第一次说", "其实我", "我一直", "我心里",
    "我害怕", "我不敢", "我恨", "我讨厌", "我的事", "说出来",
)

DISCLOSURE_LOW_WORDS: tuple[str, ...] = (
    "随便问问", "就是问问", "没事", "还行", "挺好的", "不用说", "不方便说",
)

_INTENSIFIERS: dict[str, float] = {
    "非常": 1.40, "特别": 1.30, "极其": 1.50, "超级": 1.40, "太": 1.30,
    "很": 1.15, "好": 1.15, "真": 1.20, "真的": 1.25, "越来越": 1.30,
    "一直": 1.25, "总是": 1.25, "反复": 1.20, "严重": 1.40, "彻底": 1.40,
}
_DIMINISHERS: dict[str, float] = {
    "有点": 0.60, "稍微": 0.50, "一点点": 0.50, "还好": 0.50, "算是": 0.70,
    "偶尔": 0.60, "有时候": 0.70, "不太": 0.70,
}


# ===========================================================================
# 2. 工具函数
# ===========================================================================
def clamp01(value: Any, *, default: float = NEUTRAL) -> float:
    """把任意输入钳制到 [0, 1]；不可解析时返回 default（不抛异常）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(number) or math.isinf(number):
        return float(default)
    return min(STATE_MAX, max(STATE_MIN, number))


def normalize_text(text: Any) -> str:
    """与 safety.py 保持同一套字符归一化（全角→半角、空白压缩）。"""
    if not text:
        return ""
    s = str(text).replace("\u3000", " ").replace("…", "...")
    for full, half in (("，", ","), ("。", "."), ("！", "!"), ("？", "?"),
                       ("；", ";"), ("：", ":"), ("、", ","),
                       ("（", "("), ("）", ")"), ("“", "\""), ("”", "\"")):
        s = s.replace(full, half)
    return _WS_RE.sub(" ", s).strip()


def _is_negated(text: str, start: int, end: int) -> bool:
    """否定判定，限当前分句（与 safety.py 保持同一套口径，避免两处漂移）。"""
    window_start = max(0, start - _NEG_SCOPE)
    before = text[window_start:start]
    boundary = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(before):
        boundary = match.end()
    before = before[boundary:]
    after = text[end:end + _NEG_LOOKAHEAD]
    if any(ch in before for ch in _NEGATION_CHARS):
        return True
    return any(phrase in (before + after) for phrase in _NEGATION_PHRASES)


def _intensity(text: str, start: int, end: int) -> float:
    before = text[max(0, start - 4):start]
    after = text[end:end + 3]
    factor = 1.0
    for word, value in _INTENSIFIERS.items():
        if word in before or word in after:
            factor = max(factor, value)
    for word, value in _DIMINISHERS.items():
        if word in before:
            factor = min(factor, value)
    return factor


# 证据白名单：所有出现在信号/证据里的词**必须**来自这些词典。
# 用途：`validate_evidence()` 把「证据只可能是词典词」这条隐私不变量变成可验证的契约
# ——只要这条成立，本模块就不可能把用户原句片段漏进快照、日志或页面。
_DICTIONARIES: tuple[tuple[str, ...], ...] = (
    NEGATIVE_WORDS, POSITIVE_WORDS, AROUSAL_HIGH_WORDS, AROUSAL_LOW_WORDS,
    CONTROL_LOW_WORDS, CONTROL_HIGH_WORDS, ENERGY_LOW_WORDS,
    SUPPORT_LOW_WORDS, SUPPORT_HIGH_WORDS,
    SELF_WORTH_LOW_WORDS, SELF_WORTH_HIGH_WORDS,
    ALLIANCE_HIGH_WORDS, ALLIANCE_LOW_WORDS,
    DISCLOSURE_HIGH_WORDS, DISCLOSURE_LOW_WORDS, SELF_HARM_TERMS,
)

# 字符级线索：不是词典词，但同样是固定枚举，不携带用户原文
_CHAR_CUES: tuple[str, ...] = ("!", "...", "笑声", "表情", "极短回答")

ALLOWED_EVIDENCE_WORDS: frozenset[str] = frozenset(
    _CHAR_CUES + tuple(word for words in _DICTIONARIES for word in words)
)


def validate_evidence(evidence: Mapping[str, Iterable[str]]) -> list[str]:
    """返回"不在词典白名单里"的证据词（空列表 = 合规）。

    实跑时发现 `拖累` 这类词会通过子串命中带出 `累`——它们确实都来自词典，
    属于正常现象；本函数的作用是证伪"证据里混进了用户原句"。
    """
    allowed = ALLOWED_EVIDENCE_WORDS
    return sorted({word for words in evidence.values() for word in words
                   if word not in allowed})


def _find_matches(text: str, words: Iterable[str],
                  respect_negation: bool = True) -> list[tuple[str, float]]:
    """返回 [(词, 强度)]，去重保序；可选的否定过滤。"""
    found: list[tuple[str, float]] = []
    seen: set[str] = set()
    for word in words:
        idx = text.find(word)
        while idx != -1:
            end = idx + len(word)
            if not (respect_negation and _is_negated(text, idx, end)):
                if word not in seen:
                    seen.add(word)
                    found.append((word, _intensity(text, idx, end)))
                break
            idx = text.find(word, idx + 1)
    return found


def trend_of(series: Sequence[float], eps: float = TREND_EPS) -> str:
    """由最近若干点判断趋势：up / down / flat / unknown。"""
    if len(series) < 2:
        return "unknown"
    last, prev = float(series[-1]), float(series[-2])
    delta = last - prev
    if delta > eps:
        return "up"
    if delta < -eps:
        return "down"
    return "flat"


def detect_sustained_decline(history: Sequence[float], *,
                             min_turns: int = DECLINE_MIN_TURNS,
                             min_total: float = DECLINE_MIN_TOTAL) -> bool:
    """是否出现「持续下滑」：末尾连续**不升**的轮次与累计跌幅同时达标。

    v1 的写法是"每步跌幅都要超过 TREND_EPS"，实跑发现它永远触发不了：
    状态经过平滑（0.45）与单轮位移上限后，每轮实际只会掉 0.06~0.10，
    而当时门槛是 0.03 —— 看着合理，事实上把"缓慢而持续的恶化"整体漏掉了。
    陪伴场景里**恰恰是这种缓慢下滑最危险**（不是断崖式崩溃）。

    现在的口径：
        - 只数末尾"连续不升"的段（delta <= eps），一步步慢慢掉也算；
        - 该段累计跌幅 >= min_total 才报警，避免正常波动被当成危机。
    """
    if len(history) < min_turns + 1:
        return False
    drop = 0.0
    count = 0
    for i in range(len(history) - 1, 0, -1):
        delta = float(history[i]) - float(history[i - 1])
        if delta <= TREND_EPS:
            drop += -delta
            count += 1
        else:
            break
    return count >= min_turns and drop >= min_total


# ===========================================================================
# 3. 快照
# ===========================================================================
@dataclass(frozen=True)
class StateSnapshot:
    """某一轮之后的状态切片（不可变，便于比较与落库）。"""

    dims: dict[str, float]
    turn: int
    safety_level: str = "none"
    signals: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    source: str = "rules"          # rules | llm | merged

    def get(self, name: str, default: float = NEUTRAL) -> float:
        return float(self.dims.get(name, default))

    # ---- 维度直读属性 ----
    # 为什么要有：实跑测试时发现 record.snapshot.valence 直接 AttributeError——
    # 快照只提供 get("valence")，而快照是这个模块对外的主接口，
    # 让调用方到处写 get("xxx") 既啰嗦又容易拼错。这里把 8 个维度显式暴露出来。
    @property
    def valence(self) -> float:
        return self.get("valence")

    @property
    def arousal(self) -> float:
        return self.get("arousal")

    @property
    def dominance(self) -> float:
        return self.get("dominance")

    @property
    def energy(self) -> float:
        return self.get("energy")

    @property
    def social_support(self) -> float:
        return self.get("social_support")

    @property
    def self_worth(self) -> float:
        return self.get("self_worth")

    @property
    def alliance(self) -> float:
        return self.get("alliance")

    @property
    def disclosure(self) -> float:
        return self.get("disclosure")

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "safety_level": self.safety_level,
            "signals": list(self.signals),
            "flags": list(self.flags),
            "source": self.source,
            "dims": {k: round(v, 4) for k, v in self.dims.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StateSnapshot":
        dims = {name: clamp01(payload.get("dims", {}).get(name, NEUTRAL))
                for name in DIMENSIONS}
        return cls(
            dims=dims,
            turn=int(payload.get("turn", 0)),
            safety_level=str(payload.get("safety_level", "none")),
            signals=tuple(payload.get("signals", ()) or ()),
            flags=tuple(payload.get("flags", ()) or ()),
            source=str(payload.get("source", "rules")),
        )

    def to_text(self) -> str:
        """给人看的摘要（终端/界面）；不含任何用户原文。"""
        head = (f"turn={self.turn} safety={self.safety_level} source={self.source}")
        body = " ".join(f"{k}={self.dims.get(k, NEUTRAL):.2f}" for k in DIMENSIONS)
        tail = ""
        if self.signals:
            tail += f"\n信号：{'、'.join(self.signals)}"
        if self.flags:
            tail += f"\n标记：{'、'.join(self.flags)}"
        return f"{head}\n{body}{tail}"


@dataclass(frozen=True)
class TurnRecord:
    """一轮分析的完整结果：本轮证据 + 更新后快照 + 趋势。"""

    snapshot: StateSnapshot
    deltas: dict[str, float]
    valence_trend: str
    arousal_trend: str
    sustained_decline: bool
    evidence: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def safety_level(self) -> str:
        return self.snapshot.safety_level

    @property
    def needs_check_in(self) -> bool:
        """是否建议主动关心一次（而不是每轮都追问感受）。"""
        return self.sustained_decline

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "deltas": {k: round(v, 4) for k, v in self.deltas.items()},
            "valence_trend": self.valence_trend,
            "arousal_trend": self.arousal_trend,
            "sustained_decline": self.sustained_decline,
            "evidence": {k: list(v) for k, v in self.evidence.items()},
        }

    def to_text(self) -> str:
        lines = [self.snapshot.to_text()]
        lines.append(f"趋势：愉悦度 {self.valence_trend}｜唤醒度 {self.arousal_trend}")
        if self.sustained_decline:
            lines.append("标记：持续下滑 → 建议主动关心一次")
        changed = {k: v for k, v in self.deltas.items() if abs(v) > 1e-9}
        if changed:
            lines.append("本轮位移：" + " ".join(
                f"{k}{v:+.3f}" for k, v in sorted(changed.items())))
        return "\n".join(lines)


# ===========================================================================
# 4. 轨道 A：规则抽取（永远可用）
# ===========================================================================
class RuleTrack:
    """词典 + 修饰 + 字符线索 → 维度增量。纯函数、无副作用、无网络。"""

    name = "rules"

    def extract(self, text: Any) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
        """返回 (维度增量, 证据词)。

        证据里保存的是**命中的词典词**，不是用户原句——隐私最小化，
        也让"这个状态是怎么来的"可被复核而不必留存对话原文。
        """
        raw = "" if text is None else str(text)
        s = normalize_text(raw)
        if not s:
            return {}, {}

        deltas: dict[str, float] = {}
        evidence: dict[str, tuple[str, ...]] = {}

        def collect(dim: str, words: Iterable[str], sign: float,
                    weight: float, respect_negation: bool = True) -> None:
            matches = _find_matches(s, words, respect_negation=respect_negation)
            if not matches:
                return
            # 单轮同维度最多累计 3 次，防止长文本刷分
            total = sum(w for _, w in matches[:3])
            deltas[dim] = deltas.get(dim, 0.0) + sign * weight * total
            evidence.setdefault(dim, ())
            evidence[dim] = tuple(dict.fromkeys((*evidence[dim], *(w for w, _ in matches))))

        # ---- 愉悦度：唯一一个从"中性 0.5"出发、双向对称的维度 ----
        collect("valence", POSITIVE_WORDS, +1.0, 0.16)
        collect("valence", NEGATIVE_WORDS, -1.0, 0.16)
        collect("valence", SELF_HARM_TERMS, -1.0, 0.12, respect_negation=False)

        # ---- 唤醒度：高唤醒与低唤醒都往上/下推 ----
        collect("arousal", AROUSAL_HIGH_WORDS, +1.0, 0.14)
        collect("arousal", AROUSAL_LOW_WORDS, -1.0, 0.14)

        # ---- 掌控感 / 能量 / 社会支持 / 自我价值 / 同盟 / 暴露 ----
        collect("dominance", CONTROL_HIGH_WORDS, +1.0, 0.16)
        collect("dominance", CONTROL_LOW_WORDS, -1.0, 0.16)
        collect("energy", ENERGY_LOW_WORDS, -1.0, 0.16)
        collect("social_support", SUPPORT_HIGH_WORDS, +1.0, 0.14)
        collect("social_support", SUPPORT_LOW_WORDS, -1.0, 0.16)
        collect("self_worth", SELF_WORTH_HIGH_WORDS, +1.0, 0.16)
        collect("self_worth", SELF_WORTH_LOW_WORDS, -1.0, 0.18)
        collect("alliance", ALLIANCE_HIGH_WORDS, +1.0, 0.14)
        collect("alliance", ALLIANCE_LOW_WORDS, -1.0, 0.18)
        collect("disclosure", DISCLOSURE_HIGH_WORDS, +1.0, 0.18)
        collect("disclosure", DISCLOSURE_LOW_WORDS, -1.0, 0.16)

        # ---- 字符级线索：不依赖词典，覆盖"只有标点也在传递情绪"的情况 ----
        self._char_cues(s, deltas, evidence)
        return deltas, evidence

    @staticmethod
    def _char_cues(text: str, deltas: dict[str, float],
                   evidence: dict[str, tuple[str, ...]]) -> None:
        bang = len(_EXCLAIM_RE.findall(text))
        if bang:
            deltas["arousal"] = deltas.get("arousal", 0.0) + min(0.12, 0.05 * bang)
            evidence.setdefault("arousal", ())
            evidence["arousal"] = tuple(dict.fromkeys((*evidence["arousal"], "!")))

        if _ELLIPSIS_RE.search(text):
            deltas["arousal"] = deltas.get("arousal", 0.0) - 0.06
            deltas["energy"] = deltas.get("energy", 0.0) - 0.06
            evidence.setdefault("energy", ())
            evidence["energy"] = tuple(dict.fromkeys((*evidence["energy"], "...")))

        if _LAUGH_RE.search(text):
            deltas["valence"] = deltas.get("valence", 0.0) + 0.06
            evidence.setdefault("valence", ())
            evidence["valence"] = tuple(dict.fromkeys((*evidence["valence"], "笑声")))

        if _EMOJI_RE.search(text):
            deltas["disclosure"] = deltas.get("disclosure", 0.0) + 0.05
            evidence.setdefault("disclosure", ())
            evidence["disclosure"] = tuple(dict.fromkeys((*evidence["disclosure"], "表情")))

        # 自陈程度（"很累"里的"很"已在强度里处理；这里处理极短句：只有 1~3 个字）
        if len(text) <= 3 and _find_matches(text, NEGATIVE_WORDS):
            deltas["disclosure"] = deltas.get("disclosure", 0.0) - 0.10
            evidence.setdefault("disclosure", ())
            evidence["disclosure"] = tuple(dict.fromkeys((*evidence["disclosure"], "极短回答")))


# ===========================================================================
# 5. 轨道 B：模型抽取（默认关闭）
# ===========================================================================
LLM_SYSTEM_PROMPT = (
    "你是一个情绪状态标注器。只输出 JSON，不要输出任何解释、不要 Markdown 代码块。\n"
    "JSON 结构：{\"valence\":0-1,\"arousal\":0-1,\"dominance\":0-1,\"energy\":0-1,"
    "\"social_support\":0-1,\"self_worth\":0-1,\"alliance\":0-1,\"disclosure\":0-1,"
    "\"confidence\":0-1,\"signals\":[\"简短中文标签\"],\"evidence\":[\"用户原话里的短词\"]}\n"
    "判定口径：valence 越低越痛苦；arousal 越高越激动焦躁、越低越麻木；"
    "dominance 越低越无力；confidence 表示你对本次标注的把握。\n"
    "严禁诊断任何疾病，严禁输出治疗方案，只做状态标注。"
)


def build_llm_prompt(user_text: str) -> list[dict[str, str]]:
    """构造模型轨的 messages。返回结构而不是字符串，避免拼接时的转义问题。"""
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": str(user_text or "")},
    ]


def validate_llm_payload(payload: Any) -> dict[str, float] | None:
    """校验模型轨输出：必须是 dict、维度必须可解析、越界钳制。

    返回 None 表示**不可用**（调用方应回落到规则轨），而不是抛异常——
    对齐 agent_tool_demo.py 的纪律：工具永远返回可读结果，绝不炸链路。
    """
    if not isinstance(payload, Mapping):
        return None
    result: dict[str, float] = {}
    for name in DIMENSIONS:
        if name not in payload:
            return None                      # 缺维度 = 契约不满足
        try:
            value = float(payload[name])     # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        result[name] = clamp01(value)        # 越界钳制而不是拒绝
    try:
        result["confidence"] = clamp01(payload.get("confidence", 0.5))
    except Exception:  # pragma: no cover
        result["confidence"] = 0.5
    return result


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象（容忍代码块围栏与前后废话）。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        loaded = json.loads(text)
        return dict(loaded) if isinstance(loaded, Mapping) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        loaded = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return dict(loaded) if isinstance(loaded, Mapping) else None


class LLMStateExtractor:
    """模型轨：把一段文本交给 LLM 抽状态，失败静默回落（返回 None）。

    **默认不启用**：`EmotionState(use_llm=False)`。
    启用后若依赖缺失 / 无 Key / 超时 / 契约不符，都会返回 None，
    由 `EmotionState.update()` 回落规则轨——降级链与 rag_demo.py 一致。

    说明：本类只做标注、不做回应生成；temperature 默认 0.0，
    因为标注任务要的是稳定可复现，而不是文采（回应生成另设较高温度）。
    """

    name = "llm"

    def __init__(self, model: Any = None, *, temperature: float = 0.0,
                 max_retries: int = 1) -> None:
        self._model = model
        self.temperature = temperature
        self.max_retries = max(0, int(max_retries))

    # -- 模型惰性构造：没有依赖/没有 Key 时返回 None，绝不抛错 ----
    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import os

            from langchain_openai import ChatOpenAI
        except Exception:
            return None
        api_key = os.environ.get("ZHIPU_API_KEY")
        if not api_key:
            return None
        base_url = (os.environ.get("ZHIPU_BASE_URL")
                    or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        model_name = (os.environ.get("ZHIPU_CHAT_MODEL")
                      or os.environ.get("ZHIPU_MODEL")
                      or "glm-4-flash")
        try:
            self._model = ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=self.temperature,
                timeout=20,
            )
        except Exception:
            return None
        return self._model

    def extract(self, text: Any) -> dict[str, float] | None:
        """返回 {维度: 值, confidence: 值}，任何失败一律 None（可安全调用）。"""
        if not text or not str(text).strip():
            return None
        model = self._ensure_model()
        if model is None:
            return None
        messages = build_llm_prompt(str(text))
        for _ in range(self.max_retries + 1):
            try:
                response = model.invoke(messages)
            except Exception:
                continue
            content = getattr(response, "content", response)
            payload = _parse_json_object(content)
            validated = validate_llm_payload(payload)
            if validated is not None:
                return validated
        return None


# ===========================================================================
# 6. 双轨融合
# ===========================================================================
def merge_tracks(rule_deltas: Mapping[str, float],
                 llm_state: Mapping[str, float] | None, *,
                 llm_weight: float = 0.6) -> tuple[dict[str, float], str]:
    """把两轨合成一份"目标状态增量"，返回 (增量, 来源标记)。

    融合口径（刻意的保守设计）：
        - 模型轨只提供**朝中性回归的修正**，不允许把状态推到规则轨的反方向；
        - 模型置信度低（< 0.3）时直接丢弃；
        - 负向修正优先保留（安全侧不对称）：模型说"其实没这么糟"可以被削弱，
          模型说"比看起来更糟"必须被采信。
    """
    if not llm_state:
        return dict(rule_deltas), "rules"

    confidence = float(llm_state.get("confidence", 0.5))
    if confidence < 0.3:
        return dict(rule_deltas), "rules"

    weight = max(0.0, min(1.0, llm_weight)) * confidence
    merged = dict(rule_deltas)
    for name in DIMENSIONS:
        if name not in llm_state:
            continue
        target = clamp01(llm_state[name])
        current_hint = NEUTRAL + rule_deltas.get(name, 0.0)
        proposed = (target - current_hint) * weight
        existing = merged.get(name, 0.0)
        if proposed < 0:
            merged[name] = existing + proposed          # 负向：全额采信
        elif proposed > 0:
            merged[name] = existing + proposed * 0.5    # 正向：折半
    return merged, "merged"


# ===========================================================================
# 7. 会话状态机
# ===========================================================================
class EmotionState:
    """跨轮次持有的情绪状态。

    线程安全说明：本类是**有状态**对象，按会话/用户各自持有一个实例；
    内部没有全局共享，不引入锁（对齐项目"单会话单进程"的现状）。
    """

    def __init__(self, *, smoothing: float = SMOOTHING,
                 use_llm: bool = False,
                 llm_extractor: LLMStateExtractor | None = None,
                 snapshot: StateSnapshot | None = None) -> None:
        self.smoothing = min(1.0, max(0.05, float(smoothing)))
        self.dims: dict[str, float] = {name: NEUTRAL for name in DIMENSIONS}
        self.turn = 0
        self.history: list[float] = []          # valence 轨迹
        self.arousal_history: list[float] = []
        self.records: list[TurnRecord] = []
        self.llm = (llm_extractor if isinstance(llm_extractor, LLMStateExtractor)
                    else (LLMStateExtractor() if use_llm else None))
        # 有抽取器才谈得上走模型轨；显式注入 extractor 等价于开启
        self.use_llm: bool = self.llm is not None
        if snapshot is not None:
            self.restore(snapshot)

    # ---- 序列化（P1 存会话、P3 存情景记忆共用这一个契约）----
    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "smoothing": self.smoothing,
            "dims": {k: round(v, 4) for k, v in self.dims.items()},
            "valence_history": [round(v, 4) for v in self.history[-HISTORY_LIMIT:]],
            "arousal_history": [round(v, 4) for v in self.arousal_history[-HISTORY_LIMIT:]],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], **kwargs: Any) -> "EmotionState":
        state = cls(**kwargs)
        state.turn = int(payload.get("turn", 0))
        for name in DIMENSIONS:
            state.dims[name] = clamp01(payload.get("dims", {}).get(name, NEUTRAL))
        state.history = [clamp01(v) for v in payload.get("valence_history", [])]
        state.arousal_history = [clamp01(v) for v in payload.get("arousal_history", [])]
        return state

    def restore(self, snapshot: StateSnapshot) -> None:
        self.dims = {name: clamp01(snapshot.dims.get(name, NEUTRAL)) for name in DIMENSIONS}
        self.turn = int(snapshot.turn)

    # ---- 只读视图 ----
    @property
    def valence(self) -> float:
        return self.dims["valence"]

    @property
    def arousal(self) -> float:
        return self.dims["arousal"]

    def trend(self, name: str = "valence") -> str:
        series = self.history if name == "valence" else self.arousal_history
        return trend_of(series)

    # ---- 主更新 ----
    def update(self, text: Any, *, safety: Any = None,
               llm_state: Mapping[str, float] | None = None) -> TurnRecord:
        """分析一轮输入并推进状态。

        参数：
            text      : 用户这一轮的输入
            safety    : safety.check_safety() 的结果（**风险等级的唯一来源**）；
                        传 None 表示调用方未做安全检查，此时本模块会在 flags 里
                        打上 'unchecked-safety' 以示警告，而不是假装安全。
            llm_state : 已经拿到的模型轨结果（可注入，便于测试与离线复现）；
                        为 None 且 use_llm=True 时，内部调用 LLMStateExtractor。
        """
        safety_level = "none"
        safety_rule_ids: tuple[str, ...] = ()
        if safety is not None:
            safety_level = getattr(getattr(safety, "level", None), "value",
                                   str(getattr(safety, "level", "none")))
            safety_rule_ids = tuple(getattr(safety, "rule_ids", ()) or ())

        # ---- 轨道 A ----
        rule_deltas, evidence = RuleTrack().extract(text)

        # ---- 高危：状态只做最小位移，具体处置完全交给 safety.py ----
        if safety_level == "high":
            rule_deltas = dict(_HIGH_RISK_DELTA)
            evidence = {"safety": safety_rule_ids}

        # ---- 轨道 B（可选）----
        if llm_state is None and self.use_llm and self.llm is not None:
            llm_state = self.llm.extract(text)
        deltas, source = merge_tracks(rule_deltas, llm_state)

        # ---- 单轮总偏移上限 ----
        deltas = self._limit_shift(deltas)

        # ---- 平滑更新 ----
        applied: dict[str, float] = {}
        for name in DIMENSIONS:
            delta = deltas.get(name, 0.0) * self.smoothing
            if abs(delta) < 1e-9:
                continue
            before = self.dims[name]
            self.dims[name] = clamp01(before + delta)
            applied[name] = self.dims[name] - before

        self.turn += 1
        # 先裁剪再追加：原写法在 append 之后才裁，实测 records 会稳定多留 1 条
        # （201 > HISTORY_LIMIT），内存上限就失去了意义。
        self._trim_history()
        self.history.append(self.dims["valence"])
        self.arousal_history.append(self.dims["arousal"])

        flags: list[str] = []
        if safety is None:
            flags.append("unchecked-safety")
        if detect_sustained_decline(self.history):
            flags.append("sustained-decline")
        if self.dims["valence"] < 0.25:
            flags.append("low-valence")
        if self.dims["self_worth"] < 0.3:
            flags.append("low-self-worth")
        if self.dims["social_support"] < 0.3:
            flags.append("isolated")
        if safety_level in ("medium", "high"):
            flags.append(f"safety-{safety_level}")

        snapshot = StateSnapshot(
            dims=dict(self.dims),
            turn=self.turn,
            safety_level=safety_level,
            signals=tuple(sorted({w for words in evidence.values() for w in words})),
            flags=tuple(flags),
            source=source,
        )
        record = TurnRecord(
            snapshot=snapshot,
            deltas=applied,
            valence_trend=trend_of(self.history),
            arousal_trend=trend_of(self.arousal_history),
            sustained_decline="sustained-decline" in flags,
            evidence={k: tuple(v) for k, v in evidence.items()},
        )
        self.records.append(record)
        return record

    # ---- 内部 ----
    @staticmethod
    def _limit_shift(deltas: Mapping[str, float]) -> dict[str, float]:
        total = sum(abs(v) for v in deltas.values())
        if total <= MAX_TURN_SHIFT or total == 0:
            return dict(deltas)
        scale = MAX_TURN_SHIFT / total
        return {k: v * scale for k, v in deltas.items()}

    def _trim_history(self) -> None:
        """保证 append 之后长度仍不超过 HISTORY_LIMIT（故此处留一格余量）。"""
        limit = HISTORY_LIMIT - 1
        if len(self.history) > limit:
            self.history = self.history[-limit:]
        if len(self.arousal_history) > limit:
            self.arousal_history = self.arousal_history[-limit:]
        if len(self.records) > limit:
            self.records = self.records[-limit:]


# ===========================================================================
# 8. 命令行自检
# ===========================================================================
def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    # 直接运行本文件（python src/chat/state.py）时，脚本所在目录 <root>/src/chat
    # 会被加进 sys.path，但 <root>/src 不会 —— 于是 `from chat.safety import ...`
    # 直接 ModuleNotFoundError。这是实跑才暴露的问题，这里按项目惯例补路径引导。
    _here = Path(__file__).resolve()
    for _candidate in (str(_here.parents[2]), str(_here.parents[1])):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)

    from chat.safety import check_safety

    def feed(state: EmotionState, text: str) -> None:
        hit = check_safety(text)
        record = state.update(text, safety=hit)
        print(f"\n输入长度 {len(text)} 字｜安全等级 {hit.level.value.upper()}")
        print(record.to_text())

    if len(argv) > 1 and argv[1].strip():
        state = EmotionState()
        feed(state, argv[1])
        return 0

    print(__doc__.split("使用：")[0].strip()[:400])
    print("=" * 70)
    state = EmotionState()
    demo = [
        "最近工作压力很大，有点焦虑，睡不太好",
        "还是很累，感觉做什么都提不起劲",
        "今天又失眠了，什么都做不好，觉得自己很没用",
        "谢谢你听我说这些，我从来没跟别人说过",
    ]
    for line in demo:
        feed(state, line)

    print("\n" + "=" * 70)
    print("会话结束快照：")
    print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
    print(f"\n是否建议主动关心一次：{state.records[-1].needs_check_in}")
    print("（本模块不保存用户原文，以上仅为维度数值与命中词）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
