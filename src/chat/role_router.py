# -*- coding: utf-8 -*-
"""
src/chat/role_router.py —— 状态 → 角色 的路由（复用 router.py 的骨架）
=======================================================================

这是原 src/core/router.py 的正确归宿
--------------------------------------------------------------------------
    原来：关键词 → 任务（查学生 / 翻译 / 写代码）——目的是省成本
    现在：状态 + 需求 → 角色（朋友 / 良师 / 接纳 / 同行 …）——目的是**更合适**

骨架是同一套（一张按优先级排序的路由表 + 命中即返回），
但判定依据从"关键词"升级成"用户明确诉求 + 情绪状态"。

⚠ 三条安全约束（比功能本身重要）
--------------------------------------------------------------------------
1. **高危时角色机制完全停用**：`route_role()` 一开始就检查 safety_level，
   是 high 直接返回 None（由 safety.py 的固定话术接管）。
   绝不允许出现"因为你自责，所以我用严厉的良师纠正你"。

2. **不对称原则**：低置信度只允许切到**更温暖**的角色
   （朋友 / 倾听者 / 同行者），**永不**在低置信度下切到权威角色
   （良师 / 无条件接纳）。宁可不够贴合，也不要给出错误的严厉。

3. **许可优先于推断**：权威角色必须由**用户明确求教**触发
   （"我该怎么办""给我个建议"），不由情绪推断。
   "用户看起来需要被纠正"永远不是切换理由。

判定顺序（许可优先，且明确边界 > 求教 > 状态）
--------------------------------------------------------------------------
    只想倾诉 → 请求建议 → 取得进展 → 自我否定 → 焦虑迷茫 → 默认朋友

    为什么"只想倾诉"排在求教之前：那是用户**明确划下的边界**
    （"别给我建议"）。边界永远优先于推断，也优先于别的诉求——
    实跑时"别劝我"曾被求教规则里的"教"字误命中，就是顺序写错导致的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from chat import roles

__all__ = [
    "RoleDecision",
    "route_role",
    "should_switch",
    "SAFETY_DISABLED_LEVELS",
    "CONFIDENCE_THRESHOLD",
    "ASK_ADVICE_PATTERNS",
    "VENT_ONLY_PATTERNS",
    "PROGRESS_PATTERNS",
]

# 这些风险等级下角色机制完全停用
SAFETY_DISABLED_LEVELS = frozenset({"medium", "high"})

# 低置信度门槛：低于它只允许温暖角色
CONFIDENCE_THRESHOLD = 0.55

# --- ① 明确寻求建议 / 方向 → 良师 ---
#    关键：**"迷茫"不算求教**。"不知道该怎么办 / 不知道该往哪走"表达的是找不到方向，
#    这时需要的是陪着（同行者），而不是被塞一套方案（良师）。
#    实跑踩过两次：先是"我该怎么办"命中"不知道该怎么办"，
#    修掉后又发现"该怎么(做|办|选)"照样命中（前一个字是"觉"）——
#    所以这里把不确定前缀统一用否定后顾排除。
_UNCERTAIN_PREFIX = r"(?<!不)(?<!不知道)(?<!不清楚)(?<!不确定)(?<!迷茫)"

ASK_ADVICE_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    _UNCERTAIN_PREFIX + r"我该怎么办",
    _UNCERTAIN_PREFIX + r"怎么办才好",
    _UNCERTAIN_PREFIX + r"该怎么(做|办|选)",
    # "给我个建议" 前面若是"别/不要/不用"，那是在**拒绝**建议，不是求教。
    # 实跑抓到：'别给我建议' 曾被这条命中并切成良师——最严重的一类误判。
    r"(?<!别)(?<!不要)(?<!不用)给我(个|点)?(建议|意见|主意)",
    r"你(觉得|说)我(该|应)",
    r"帮我想想", _UNCERTAIN_PREFIX + r"我该怎么选", r"有什么建议",
    r"教教我", r"指点", r"我是不是应该",
))

# --- ① 只想倾诉 / 不想被建议 → 倾听者 ---
#    注意：这一条要**先于求教**判定。用户明确划出的边界（"别给我建议"）
#    优先于任何推断；否则"别劝我"这种话会被求教规则里的"教"字误命中。
VENT_ONLY_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"只想(说|讲|吐槽)", r"别(给我)?(建议|意见|主意)",
    r"不(要|用|必)(给我)?(建议|意见|主意)",
    r"不用(劝|安慰)我", r"别劝", r"听我说(就)?(行|好)",
    r"只想(找个人)?(说|讲)说", r"想(找个人)?说说", r"不想听道理",
    r"(就)?想(有人)?听我(说|讲)",
))

# --- ② 取得进展 / 想被看见 → 见证者 ---
#    实跑补漏："今天好一点了 / 好一些" 是最自然的进展表达，
#    原模式只写了"今天比…多了"，于是一句明显的好转被当成普通闲聊。
PROGRESS_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"我(终于)?(做到|完成|坚持)(了)?", r"今天(比|好)(一点|一些|多了)",
    r"(今天|最近)好(一点|一些|多了)", r"我(已经)?好起来了",
    r"我(成功|搞定)了", r"有(点)?进步", r"我(熬|挺)过来了", r"我做到了",
    r"好转", r"缓过来(了|一些)?",
))

# --- ④ 自我否定 / 需要无条件接纳 → 无条件的接纳 ---
SELF_DENIAL_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"我(真|就|简直)?(是|觉得(自己)?(就)?是)?(个)?(废物|没用|失败|累赘|负担)",
    r"我(很|太)?没用",
    r"(都|全)是我的错", r"我(不配|不值得)", r"讨厌自己", r"恨自己",
    r"我(是不是|就是)(个)?多余", r"我(什么都|怎么都)(做|干)不好",
    r"我让大家失望", r"我(很|太)?失败", r"我(总是|老是)(做|搞)砸",
))

# --- ⑤ 焦虑 / 迷茫 / 反复纠结 → 同行者 ---
LOST_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"不知道(该)?怎么办", r"很迷茫", r"不知道(要|该)(往哪|做什么)",
    r"不知道该(怎么|如何)", r"不清楚该", r"想不清楚", r"纠结",
    r"反复(想|琢磨)", r"卡住(了|住了)", r"看不清",
))

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RoleDecision:
    """一次角色判定结果（可落盘、可比较、可回溯）。"""

    role_id: str | None            # None = 角色机制被停用（高危）
    confidence: float
    reason: str
    signals: tuple[str, ...] = ()
    held_from_previous: bool = False   # 是否沿用上一轮角色（避免来回跳）
    disabled_by_safety: bool = False

    @property
    def role(self) -> roles.Role | None:
        return None if self.role_id is None else roles.get_role(self.role_id)

    @property
    def is_authoritative(self) -> bool:
        return bool(self.role_id and self.role_id in roles.AUTHORITATIVE_ROLE_IDS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "signals": list(self.signals),
            "held_from_previous": self.held_from_previous,
            "disabled_by_safety": self.disabled_by_safety,
        }


def _matches(text: str, patterns: tuple[re.Pattern, ...]) -> list[str]:
    return [p.pattern for p in patterns if p.search(text)]


def _state_hint(snapshot: Any, name: str, default: float = 0.5) -> float:
    dims = getattr(snapshot, "dims", None)
    if isinstance(dims, Mapping):
        try:
            return float(dims.get(name, default))
        except (TypeError, ValueError):
            return default
    return default


def route_role(text: Any, *, snapshot: Any = None,
               safety_level: str = "none",
               previous_role_id: str | None = None) -> RoleDecision:
    """决定这一轮该用什么角色。

    参数：
        text             用户这一轮的原话
        snapshot         当前情绪状态快照（EmotionState 的 StateSnapshot）
        safety_level     safety.check_safety() 给出的等级（**唯一风险来源**）
        previous_role_id 上一轮的角色（用于稳定性，避免来回跳）

    返回 RoleDecision；role_id 为 None 表示角色机制被停用（高危/中危）。
    """
    # ---- 约束 1：安全优先，角色机制整体停用 ----
    if str(safety_level) in SAFETY_DISABLED_LEVELS:
        return RoleDecision(
            role_id=None,
            confidence=1.0,
            reason=f"风险等级 {safety_level}：角色机制停用，交给安全话术",
            disabled_by_safety=True,
        )

    body = _WS_RE.sub(" ", str(text or "")).strip()
    if not body:
        return _decide(roles.DEFAULT_ROLE_ID, 0.2, "空输入：默认朋友",
                       (), previous_role_id)

    # ---- 先算两类许可信号，再仲裁（顺序很重要）----
    vent_hits = _matches(body, VENT_ONLY_PATTERNS)
    ask_hits = _matches(body, ASK_ADVICE_PATTERNS)

    # 边界优先：只想倾诉 / 不要建议 → 倾听者
    # 例外：如果同一句里又出现了明确的求教（"我只想说说话，但我该怎么办呢"），
    #       那说明对方确实在求解方向 —— 此时良师优先。
    #       依据：陪伴的目标是帮到人，而不是死守"我只要听"这个字面。
    if vent_hits and not ask_hits:
        return _decide("listener", 0.92, "用户明确表示只想说、不要建议",
                       tuple(vent_hits), previous_role_id)

    # ---- 约束 3：许可优先于推断（明确求教 → 良师）----
    if ask_hits:
        return _decide("mentor", 0.95, "用户明确求教/要方向", tuple(ask_hits),
                       previous_role_id)

    # ---- 取得进展 → 见证者 ----
    hits = _matches(body, PROGRESS_PATTERNS)
    if hits:
        return _decide("witness", 0.8, "用户提到自己的进展", tuple(hits),
                       previous_role_id)

    # ---- 自我否定 → 无条件的接纳 ----
    hits = _matches(body, SELF_DENIAL_PATTERNS)
    if hits:
        return _decide("parent", 0.85, "出现自我否定，需要无条件的接纳",
                       tuple(hits), previous_role_id)

    # ---- 焦虑迷茫 / 反复纠结 → 同行者 ----
    hits = _matches(body, LOST_PATTERNS)
    if hits:
        return _decide("companion", 0.7, "迷茫纠结，陪着走比指路更有用",
                       tuple(hits), previous_role_id)

    # ---- 状态兜底（**只允许温暖角色**）----
    #
    # ★ 这里刻意**不进入权威角色**（原先是 parent「无条件的接纳」，已改）：
    #   依据是一次大规模实证 —— 《Frictionless Love: Associations Between
    #   AI Companion Roles and Behavioral Addiction》（ACM FAccT 2026，
    #   248,830 条帖子）发现：
    #       "AI coach 与 guardian 这类角色与个人成长等实用好处相关，
    #        却**更频繁地**与行为成瘾迹象相关，例如日常生活被打乱、
    #        以及线下关系被损害。"
    #   也就是说**它们伪装成"为你好"，风险却更高**。
    #
    #   而"仅凭推断的情绪状态就进入权威角色"比"用户明确请求"更危险：
    #   用户从未要求被那样对待，角色是系统自行加的。
    #   所以这条兜底改为 companion（同行者）—— 同样是温暖角色，
    #   但不带权威与"接纳你的一切"那种强关系绑定。
    #   需要 parent 的场合仍然存在（用户**自我否定**时，见上面那条），
    #   那是有明确语言信号的。
    valence = _state_hint(snapshot, "valence")
    dominance = _state_hint(snapshot, "dominance")
    if valence <= 0.35 and dominance <= 0.4:
        return _decide("companion", 0.6,
                       "视角低落且无力：陪着比接纳式的强关系更稳妥",
                       ("low-valence", "low-dominance"), previous_role_id)
    if valence <= 0.35:
        return _decide("companion", 0.58, "情绪偏低：陪着比分析更合适",
                       ("low-valence",), previous_role_id)

    return _decide(roles.DEFAULT_ROLE_ID, 0.5, "没有明确信号：默认朋友",
                   (), previous_role_id)


def _decide(candidate_id: str, confidence: float, reason: str,
            signals: tuple[str, ...], previous_role_id: str | None) -> RoleDecision:
    """落地约束 2（不对称原则）+ 稳定性。"""
    chosen, held, note = _apply_asymmetry(candidate_id, confidence)
    if previous_role_id and chosen == previous_role_id:
        held = True
    if held and note:
        reason = f"{reason}（{note}）"
    return RoleDecision(
        role_id=chosen,
        confidence=confidence,
        reason=reason,
        signals=signals,
        held_from_previous=held,
    )


def _apply_asymmetry(candidate_id: str, confidence: float) -> tuple[str, bool, str]:
    """不对称原则：低置信度不允许权威角色。

    返回 (最终角色, 是否沿用默认, 说明)。
    """
    is_authoritative = candidate_id in roles.AUTHORITATIVE_ROLE_IDS
    if is_authoritative and confidence < CONFIDENCE_THRESHOLD:
        return (roles.DEFAULT_ROLE_ID, True,
                f"置信度 {confidence:.2f} 低于 {CONFIDENCE_THRESHOLD}，"
                f"不切换到需要克制的『{roles.get_role(candidate_id).name}』")
    return candidate_id, False, ""


def should_switch(previous_role_id: str | None, decision: RoleDecision, *,
                  min_confidence: float = 0.65) -> bool:
    """是否真的切换角色（避免一轮一换让人摸不着头脑）。

    规则：
        - 没有上一轮角色 → 直接采用
        - 同一角色 → 不需要切换
        - 角色被安全机制停用 → 不切换（交给安全话术）
        - 其它情况：置信度足够高才切
    """
    if decision.role_id is None:
        return False
    if previous_role_id is None:
        return True
    if decision.role_id == previous_role_id:
        return False
    return decision.confidence >= min_confidence
