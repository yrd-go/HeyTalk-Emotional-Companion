# -*- coding: utf-8 -*-
"""
src/chat/feedback.py —— 对话内反馈埋点（S5）
================================================================

为什么需要它（这是最重要的一段理由）：
    这个项目一直用"我觉得这样更好"来推进——而 docs/PITFALLS.md 第 21 节
    已经证明过这条路有多危险：作者引以为傲的"查询改写 + 混合重排"，
    一上评测是**负优化**（hit@1 0.725 vs 纯向量 0.925）。
    所以"这个回应有没有帮到人"不能靠感觉，必须在对话里直接问、直接记。

但**不能每轮都问**。陪伴对话里频繁弹问卷会立刻毁掉体验，
所以反馈请求是**稀疏**的（每 N 轮一次），且永远可以跳过。

记录什么（这里的取舍很关键）
--------------------------------------------------------------------------
    记录：时间、会话标识、第几轮、主观评价、可选留言、
          **该轮前后的情绪状态变化**、该轮的安全等级
    不记录：用户的完整原话、模型回应全文、任何凭据

为什么带"状态变化"：这是**客观信号**，与主观评分互为交叉验证。
    主观说"有帮助"但状态没动，和主观说"还行"但状态明显回升，
    含义完全不同 —— 只靠主观评分会被客套话带偏。

隐私
--------------------------------------------------------------------------
    数据只写本机 <仓库根>/data/feedback/feedback.jsonl（已在 .gitignore 忽略），
    永不联网；留言会过一遍 mask_secrets 再落盘。
    本模块**不 import streamlit**，纯标准库，可被普通 unittest 全覆盖。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from paths import mask_secrets

__all__ = [
    "RATING_CHOICES",
    "DEFAULT_ASK_EVERY",
    "FeedbackEntry",
    "FeedbackStore",
    "feedback_due",
    "next_pending_turn",
    "summarize",
]

# 评价选项：刻意用"有 / 不太有 / 说不好"，而不是 1~5 星。
# 理由：情绪陪伴场景里"几星"很像在打分，容易让人不想答；
# 三选一足够回答"有没有用"这个核心问题。
RATING_CHOICES: tuple[tuple[str, str], ...] = (
    ("helpful", "有帮到"),
    ("neutral", "说不好"),
    ("unhelpful", "没帮到"),
)

RATING_VALUES = tuple(value for value, _ in RATING_CHOICES)

# 每几轮问一次。3 是一个折中：不至于烦，也能较快积累样本。
DEFAULT_ASK_EVERY = 3

MAX_COMMENT_CHARS = 500
SCHEMA_VERSION = 1

_WS_RE = re.compile(r"\s+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_comment(text: Any) -> str:
    """留言清洗：去多余空白、截断、并过一遍脱敏（防手滑贴进密钥）。"""
    if not text:
        return ""
    cleaned = _WS_RE.sub(" ", str(text)).strip()
    if len(cleaned) > MAX_COMMENT_CHARS:
        cleaned = cleaned[:MAX_COMMENT_CHARS] + "…"
    return mask_secrets(cleaned)


def feedback_due(assistant_turns: int, *, every: int = DEFAULT_ASK_EVERY) -> bool:
    """当前是否该请求反馈（纯函数，便于测试）。

    规则：每积累 `every` 轮问一次；every<=0 视为关闭该功能。
    """
    if every <= 0:
        return False
    return assistant_turns > 0 and assistant_turns % every == 0


def next_pending_turn(turns: Iterable[Any], recorded: Iterable[int],
                      *, every: int = DEFAULT_ASK_EVERY) -> int | None:
    """返回"应该请求反馈的第几轮"，没有则 None。

    参数：
        turns    ：会话的消息序列（每个元素有 .role 属性或 role 键）
        recorded ：已经记录过反馈的轮次索引

    为什么按"第几轮"而不是"最后一条"：反馈请求出现在界面后，
    用户可能先翻看、甚至直接关掉页面，用索引才能保证不会重复埋点。
    """
    if every <= 0:
        return None

    def role_of(item: Any) -> str:
        if isinstance(item, Mapping):
            return str(item.get("role") or "")
        return str(getattr(item, "role", "") or "")

    assistant_seen = 0
    done = {int(x) for x in recorded}
    for index, item in enumerate(turns):
        if role_of(item) != "assistant":
            continue
        assistant_seen += 1
        if feedback_due(assistant_seen, every=every) and index not in done:
            return index
    return None


@dataclass
class FeedbackEntry:
    """一条反馈记录。"""

    turn_index: int
    rating: str
    comment: str = ""
    safety_level: str = "none"
    valence_before: float | None = None
    valence_after: float | None = None
    session_id: str = ""
    persona_role: str = ""          # 这一轮用的姿态（用于按角色做消融统计）
    created_at: str = ""
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # 清洗必须在**构造时**做，而不是只写在 to_dict 里：
        # 早期版本定义了 _clean_comment 却从未调用，导致空白没压缩、超长没截断
        # —— 一个"看着像做了、其实没做"的典型（实跑测试才暴露）。
        self.comment = _clean_comment(self.comment)

    @property
    def valence_delta(self) -> float | None:
        if self.valence_before is None or self.valence_after is None:
            return None
        return round(self.valence_after - self.valence_before, 4)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "created_at": self.created_at or _utc_now().replace(microsecond=0).isoformat(),
            "session_id": self.session_id,
            "turn_index": int(self.turn_index),
            "rating": self.rating,
            "safety_level": self.safety_level,
        }
        if self.comment:
            payload["comment"] = self.comment
        if self.persona_role:
            payload["persona_role"] = self.persona_role
        if self.valence_before is not None:
            payload["valence_before"] = round(self.valence_before, 4)
        if self.valence_after is not None:
            payload["valence_after"] = round(self.valence_after, 4)
        return payload


class FeedbackStore:
    """反馈的追加式存储（JSONL：一行一条，便于人工查看与统计）。

    为什么用 JSONL 而不是 JSON 数组：追加写入简单、天然抗损
    （一行坏了不影响其它行），而且能直接用命令行工具过滤。
    """

    def __init__(self, base_dir: Path | str, *, filename: str = "feedback.jsonl") -> None:
        self.base_dir = Path(base_dir)
        self.file = self.base_dir / filename

    def ensure_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    def append(self, entry: FeedbackEntry) -> Path:
        self.ensure_dir()
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self.file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return self.file

    def read_all(self) -> list[dict[str, Any]]:
        """读全部记录；坏行跳过（不让一条脏数据毁掉统计）。"""
        if not self.file.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = self.file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                records.append(dict(payload))
        return records

    def recorded_turns(self, session_id: str) -> set[int]:
        """某条会话里已反馈过的轮次索引。"""
        return {
            int(item.get("turn_index", -1))
            for item in self.read_all()
            if str(item.get("session_id") or "") == str(session_id)
        }

    def clear(self) -> int:
        """删除反馈文件，返回删除条数（配合"彻底删除"用）。"""
        if not self.file.is_file():
            return 0
        count = len(self.read_all())
        try:
            self.file.unlink()
        except OSError:
            return 0
        return count

    def stats(self) -> dict[str, Any]:
        return summarize(self.read_all(), path=str(self.file))


def summarize(records: Iterable[Mapping[str, Any]], *,
              path: str = "") -> dict[str, Any]:
    """把反馈记录汇成统计结果。

    刻意把"有帮助"和"没帮助"分开数，并且**不把'说不好'算进分母以外的任何地方**
    —— 把"说不好"当"没帮助"是最常见的自欺式口径。

    另外按**姿态**（persona_role）分组统计：这是"角色路由到底有没有用"
    的量化依据（做消融实验时直接对比 friend 与 mentor 的好评率）。
    """
    items = [dict(r) for r in records]
    total = len(items)
    counts = {value: 0 for value in RATING_VALUES}
    deltas: list[float] = []
    by_role: dict[str, dict[str, Any]] = {}

    for item in items:
        rating = str(item.get("rating") or "")
        if rating in counts:
            counts[rating] += 1

        role = str(item.get("persona_role") or "").strip() or "(未标注)"
        bucket = by_role.setdefault(
            role, {"answered": 0, "helpful": 0, "neutral": 0, "unhelpful": 0})
        if rating in bucket:
            bucket[rating] += 1
            bucket["answered"] += 1

        before, after = item.get("valence_before"), item.get("valence_after")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            deltas.append(float(after) - float(before))

    for bucket in by_role.values():
        answered = bucket["answered"]
        bucket["helpful_rate"] = (bucket["helpful"] / answered) if answered else None

    answered = counts["helpful"] + counts["neutral"] + counts["unhelpful"]
    return {
        "path": path,
        "total": total,
        "counts": counts,
        "answered": answered,
        "helpful_rate": (counts["helpful"] / answered) if answered else None,
        "unhelpful_rate": (counts["unhelpful"] / answered) if answered else None,
        "mean_valence_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "paired_samples": len(deltas),
        "by_role": by_role,
    }
