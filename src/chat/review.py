# -*- coding: utf-8 -*-
"""
src/chat/review.py —— "回顾":偶尔提一件他之前说过的事
================================================================

这个模块只回答一个问题：**这一次打开，要不要提那件事？**
（要不要在界面上显示、显示成什么样，由服务端与前端负责。）

================================================================
设计取向：为什么是这样，而不是"不可预测地发一封信"
================================================================

最初的想法是"在不可预测的时间点送达一封慰问信/陌生来信"（茨威格式的）。
查过证据之后改成了现在这样。四条理由，按重要性排：

1. **"智能择时"在实证上没有优势。**
   Morrison et al. (2017), PLOS ONE, doi 10.1371/journal.pone.0169162
   —— 随机对照（n=77，三组：智能择时 / 每日定时 / 偶尔）发现：
     · **"智能组与非智能组之间没有显著差异"**
     · **"自适应的通知时机定制并不总能提升其使用效果"**
   也就是说：为"不可预测的时机"付出的全部复杂度，**可测量的收益是零**。
   附带一条反直觉的：该研究**没有**发现频繁通知会抑制参与度
   （"frequent notifications may encourage greater exposure ...
   without deterring engagement"）—— 所以"发多了会被厌弃"这个直觉，
   证据并不支持；真正的代价不在参与度上。

2. **"不可预测"在机制上是变动比率强化** —— 老虎机的机制。
   而既然它没有收益（第 1 条），那就是**没有对价的风险**。
   所以这里的时间条件是**可预测**的：距上次 ≥ N 天，且被忽视时会**拉长**。

3. **真正的风险源是"它记得我"，不是时机。**
   《Frictionless Love》(ACM FAccT 2026, arXiv:2604.20011,
   248,830 条帖子) 发现 AI 灵魂伴侣角色"提供情感支持，但也引入情感操控
   与痛苦，并最终导致**强烈的依恋**"。所以本模块刻意：
     · 句子**以他的事为主语**，不以"我记得"为主语
       （"你之前说过「…」。后来呢？" 而不是 "我一直记得你说过…"）；
     · 目标是**指向他的真实生活**，不是指向这个产品。

4. **遗忘曲线不支持"不可预测"。**
   它的实用产物是间隔重复（Anki/SuperMemo）：间隔**可预测、规则、逐渐拉长**。
   而且它描述的是"人对学习材料的记忆衰减"，**不是"人想不想收到你的消息"** ——
   拿它决定发信时机是范畴错误。

================================================================
四道闸门（全部通过才显示）
================================================================
    内容闸：必须有一条**用户自己确认过**的记忆，且类型值得追问
            （goal / event / relationship；fact / preference 不问 —— 
              "我喜欢安静"后面没有"后来"）
    时间闸：距上次 ≥ REVIEW_MIN_GAP_DAYS 天
    场景闸：只在**新会话空屏**上调用（调用方负责，不在本模块）
    注意力闸：★ **上次没被点开 → 改用更长的间隔**（REVIEW_BACKOFF_DAYS）
              这是防习惯化的核心：用户用脚投票说"不需要"，就真的退场。
              商业产品在这里会做相反的选择。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ReviewState", "ReviewDecision", "ReviewStore",
    "decide_review", "REVIEW_MIN_GAP_DAYS", "REVIEW_BACKOFF_DAYS",
]

# 正常间隔：至少一周才提一次
REVIEW_MIN_GAP_DAYS = 7
# 上次被忽视后的间隔：**拉长**，不是"再试一次"
REVIEW_BACKOFF_DAYS = 30

# 哪些类型值得追问。刻意不含 fact / preference：
#   fact（"我住在广州"）没有"后来"；preference（"我喜欢安静"）也没有。
#   硬问会显得在硬凑话题 —— 那正是"为了显示而显示"。
ASKABLE_KINDS: tuple[str, ...] = ("goal", "event", "relationship")

# 按类型选问法。**问的是他的事，不是"我还记得"**。
_QUESTIONS: Mapping[str, str] = {
    "goal": "后来有进展吗？",
    "event": "后来怎么样了？",
    "relationship": "最近还好吗？",
}
_DEFAULT_QUESTION = "后来怎么样了？"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ReviewState:
    """回顾的进度状态（存盘，跨会话保持）。"""

    last_shown_at: str = ""
    # 上次显示后**有没有被点开**。这是注意力闸的输入。
    last_opened: bool = False
    shown_count: int = 0
    # 用户主动关掉 → 永久不再出现（可撤销性：用户说了就算）
    dismissed: bool = False
    # 已经提过的条目，避免反复问同一件事（那会显得没在听）
    asked_entry_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_shown_at": self.last_shown_at,
            "last_opened": bool(self.last_opened),
            "shown_count": int(self.shown_count),
            "dismissed": bool(self.dismissed),
            "asked_entry_ids": list(self.asked_entry_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ReviewState":
        if not isinstance(payload, Mapping):
            return cls()
        asked = payload.get("asked_entry_ids") or []
        if not isinstance(asked, (list, tuple)):
            asked = []
        return cls(
            last_shown_at=str(payload.get("last_shown_at") or ""),
            last_opened=bool(payload.get("last_opened")),
            shown_count=int(payload.get("shown_count") or 0),
            dismissed=bool(payload.get("dismissed")),
            asked_entry_ids=tuple(str(x) for x in asked if str(x).strip()),
        )


@dataclass
class ReviewDecision:
    """这次要不要提，以及提什么。"""

    show: bool
    text: str = ""
    entry_id: str = ""
    question: str = ""
    reason: str = ""
    # 供状态更新用（调用方在本回合结束后落盘）
    state: ReviewState = field(default_factory=ReviewState)


class ReviewStore:
    """回顾状态的落盘（一个 JSON 文件，按用户分目录由调用方决定）。"""

    def __init__(self, base_dir: Path | str, *, filename: str = "review.json") -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / filename

    def load(self) -> ReviewState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ReviewState()
        return ReviewState.from_dict(raw if isinstance(raw, Mapping) else None)

    def save(self, state: ReviewState) -> bool:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            # 回顾是**可选**功能 —— 存不下来就不显示，绝不因为写盘失败
            # 把用户挡在聊天之外
            return False


def _eligible(entries: Iterable[Any],
              asked: Sequence[str]) -> list[Any]:
    """内容闸：挑出**值得追问**的条目。"""
    out = []
    for entry in entries:
        kind = str(getattr(entry, "kind", "") or "")
        value = str(getattr(entry, "value", "") or "").strip()
        entry_id = str(getattr(entry, "entry_id", "") or "")
        if kind not in ASKABLE_KINDS or not value:
            continue
        if entry_id and entry_id in asked:
            continue      # 提过的不再提
        out.append(entry)
    return out


def decide_review(entries: Iterable[Any], state: ReviewState, *,
                  now: datetime | None = None,
                  min_gap_days: int = REVIEW_MIN_GAP_DAYS,
                  backoff_days: int = REVIEW_BACKOFF_DAYS) -> ReviewDecision:
    """四道闸门依次判断。

    `entries`：用户**已确认**的记忆条目（调用方从 ProfileStore.entries() 取）。
    `now`：可注入，便于测试。
    """
    moment = now or _now()

    # 闸门 0：用户主动关掉了 → 永不出现
    if state.dismissed:
        return ReviewDecision(False, reason="用户已关闭回顾", state=state)

    # 闸门 1：内容闸 —— 没有值得追问的东西就不显示
    candidates = _eligible(entries, state.asked_entry_ids)
    if not candidates:
        return ReviewDecision(False, reason="没有值得追问的记忆条目", state=state)

    # 闸门 2/4：时间闸 + 注意力闸
    last = _parse(state.last_shown_at)
    if last is not None:
        # 上次被忽视 → 用更长的间隔。这是防习惯化的核心：
        # 用户的沉默被当作"少打扰我"的信号，而不是"再推一次试试"。
        gap = backoff_days if not state.last_opened else min_gap_days
        elapsed = moment - last
        if elapsed < timedelta(days=gap):
            waited = f"{elapsed.days} 天"
            return ReviewDecision(
                False,
                reason=f"距上次 {waited}，还没到 {gap} 天"
                       + ("（上次没被点开，间隔已拉长）" if not state.last_opened else ""),
                state=state)

    # 挑一条：优先"更久没被用过"的（用 times_used 做代理，越小越新鲜）
    candidates.sort(key=lambda e: (int(getattr(e, "times_used", 0) or 0),
                                   str(getattr(e, "created_at", "") or "")))
    chosen = candidates[0]
    kind = str(getattr(chosen, "kind", "") or "")
    value = str(getattr(chosen, "value", "") or "").strip()
    question = _QUESTIONS.get(kind, _DEFAULT_QUESTION)

    # ★ 文案：**引用他自己的原话**，而不是内嵌。
    #   为什么用引号：value 保留的是**第一人称**原话（"我妈总给我打电话"），
    #   直接内嵌会变成"你之前提过我妈总打电话"—— 人称就错了。
    #   加引号后第一人称是引语，语法上正确、读起来也自然。
    # ★ 而且主语是**他的事**，不是"我记得"：
    #   "你之前说过「…」。后来呢？" 而不是 "我一直记得你说过…"。
    #   后者把落点放在这个产品身上，那正是依恋机制（见模块顶部第 3 条）。
    text = f"你之前说过「{value}」。{question}"

    new_state = ReviewState(
        last_shown_at=_iso(moment),
        # 新显示的一次，先记为"未点开"；用户点开时由接口置为 True
        last_opened=False,
        shown_count=state.shown_count + 1,
        dismissed=state.dismissed,
        asked_entry_ids=tuple(list(state.asked_entry_ids)
                              + [str(getattr(chosen, "entry_id", "") or "")]),
    )
    return ReviewDecision(True, text=text,
                          entry_id=str(getattr(chosen, "entry_id", "") or ""),
                          question=question,
                          reason="四道闸门都通过", state=new_state)


def mark_opened(state: ReviewState) -> ReviewState:
    """用户点开了这次回顾 → 记为"被接受"，下次恢复常规间隔。"""
    state.last_opened = True
    return state


def dismiss(state: ReviewState) -> ReviewState:
    """用户主动关闭 → 永久不再出现。"""
    state.dismissed = True
    return state
