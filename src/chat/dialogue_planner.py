# -*- coding: utf-8 -*-
"""会话内的未收束线索规划（不写长期档案）。

为什么需要这一层
--------------------------------------------------------------------------
角色路由只读取**当轮原话**。用户说“感觉自己没什么用”时，它知道该接住；
下一轮用户解释“因为我没有法拉利”，这句话本身没有自我否定词，角色会回到
普通朋友，模型也就容易把它当成一个新车话题。长期档案不能解决这个问题：
档案只保存用户确认过的长期事实，而这里需要记住的是**一段对话里的临时线索**。

所以这一层只做三件事：
    1. 在最近 6 次用户发言里找一条明确的自我否定；
    2. 判断当前这句是在解释、延续、回答上一问，还是已经换题；
    3. 只在确实相关时给模型一条很短的“先接回前文”的规划提示。

它不诊断、不推断隐藏创伤、不写盘。找不到明确线索时返回 None，保持原行为。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = ["OPEN_THREAD_WINDOW", "TurnPlan", "build_turn_plan"]

# 只看最近几次用户发言。再久之前的事可能已经聊完，强行接回会像翻旧账。
OPEN_THREAD_WINDOW = 6

_WS_RE = re.compile(r"\s+")

_SELF_WORTH_RE = re.compile(
    r"(?:感觉|觉得)(?:我|自己)?[^。！？\n]{0,4}"
    r"(?:没什么用|没有用|没用|很失败|不配|多余|没价值|没出息|拖累)"
    r"|我(?:感觉|觉得)?(?:自己)?(?:没什么用|没有用|没用)"
    r"|我(?:真|很|太|简直)?(?:是|就是)?(?:个)?(?:废物|失败|累赘|负担|多余)"
    r"|我(?:不配|不值得)"
    r"|自己[^。！？\n]{0,10}(?:不重要|不配|不值得)"
    r"|(?:讨厌|恨)自己"
    r"|我(?:什么都|怎么都)(?:做|干)不好"
    r"|我(?:总是|老是)(?:做|搞)砸"
)

_REASON_RE = re.compile(r"^\s*(?:因为|就是因为|还不是因为|由于|全是|都怪)")
_DESIRE_RE = re.compile(r"(想要|想买|真想|喜欢|羡慕|要是|如果|等我|买不起|拿不出|没钱|没有钱)")
_LEAD_RE = re.compile(
    r"^\s*(?:嗯|对|是|不是|也不算|就是|其实|只是|所以|可是|但是|还是|真的|确实|反正|而且)"
)
_EXPLICIT_CLOSE_RE = re.compile(
    r"(换个话题|不说这个|不想说|别问了|跳过这个|聊点别的|我没事|我好了|已经没事了|不谈了|算了)"
)
_TASK_SHIFT_RE = re.compile(
    r"(翻译|译成|翻成|天气|气温|下雨|代码|编程|bug|报错|数学题|新闻|股票|汇率|几点了)"
)


@dataclass(frozen=True)
class TurnPlan:
    """一条可解释的会话连续性规划。"""

    anchor: str
    current: str
    relation: str                 # reason / desire / answer / continuation
    answering_question: bool = False

    @property
    def signals(self) -> tuple[str, ...]:
        out = ["open-thread:self-worth", f"relation:{self.relation}"]
        if self.answering_question:
            out.append("answering-previous-question")
        return tuple(out)

    def to_instruction(self) -> str:
        relation_text = {
            "reason": "是在解释那条感受的背景，不是换题",
            "desire": "仍和前面的自我评价连着",
            "answer": "是在回答上一问",
            "continuation": "仍在延续前面那条线",
        }.get(self.relation, "仍在延续前面那条线")
        return (
            "【对话背景｜内部参考】\n"
            f"用户此前说过“{self.anchor}”，当前“{self.current}”{relation_text}。"
            "自然带着这个背景回应，不换题，也别复述原句。"
        )


def _clean(text: Any) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _clip(text: str, limit: int = 36) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _last_assistant_message(history: Sequence[Mapping[str, Any]]) -> str:
    for item in reversed(history):
        if str(item.get("role") or "") == "assistant":
            return _clean(item.get("content"))
    return ""


def _ends_with_question(text: str) -> bool:
    return text.rstrip().endswith(("?", "？"))


def build_turn_plan(text: Any, history: Sequence[Mapping[str, Any]]) -> TurnPlan | None:
    """返回本轮连续性规划；没有明确线索或已经换题时返回 None。"""
    current = _clean(text)
    if not current:
        return None
    # 当轮自己就是自我否定，模型直接看得到；不再重复注入一层解释。
    if _SELF_WORTH_RE.search(current):
        return None
    if _EXPLICIT_CLOSE_RE.search(current) or _TASK_SHIFT_RE.search(current):
        return None

    prior_user = [
        _clean(item.get("content"))
        for item in history
        if str(item.get("role") or "") == "user" and _clean(item.get("content"))
    ]
    if not prior_user:
        return None

    anchor = ""
    for candidate in reversed(prior_user[-OPEN_THREAD_WINDOW:]):
        if _SELF_WORTH_RE.search(candidate):
            anchor = candidate
            break
    if not anchor:
        return None

    last_assistant = _last_assistant_message(history)
    answering_question = bool(
        _ends_with_question(last_assistant) and len(current) <= 40
    )

    if _REASON_RE.search(current):
        relation = "reason"
    elif _DESIRE_RE.search(current):
        relation = "desire"
    elif answering_question:
        relation = "answer"
    elif _LEAD_RE.search(current) and len(current) <= 40:
        relation = "continuation"
    else:
        return None

    return TurnPlan(
        anchor=_clip(anchor),
        current=_clip(current),
        relation=relation,
        answering_question=answering_question,
    )
