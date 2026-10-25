# -*- coding: utf-8 -*-
"""
src/chat/profile.py —— 动态知识点档案（S4）
================================================================

它解决什么问题
--------------------------------------------------------------------------
陪伴的"连续性"来自记得：上次说过在准备考研、和家里关系紧张、不喜欢被追问原因。
不记这些，每次对话都从零开始，用户会觉得"跟谁说都一样"。

⚠ 但这里有一条不可妥协的红线
--------------------------------------------------------------------------
    **只保存用户确认过的内容。**

不要保存"系统推断出来的关于你的事"。原因有两条，都很硬：

1. **会持续放大错误**：LLM 从对话里抽的"用户似乎很需要在被否定时得到肯定"
   很容易猜错；一旦写进长期档案，这个错误会**在之后的每一次对话里生效**，
   而用户不知道为什么这个 AI 变得怪怪的。
2. **像监视**：用户没说过、却被记下来的东西，读起来就是"它在背后分析我"。

所以流程是：**提取候选 → 回述给用户确认 → 才写入**。
`accept_candidate()` / `reject_candidate()` / `edit_candidate()` 三个函数
构成那道门，任何写入路径都必须经过它们（有测试钉住）。

双轨制（与 state.py 一致的思路）
--------------------------------------------------------------------------
    轨道 A：规则抽（本文件，纯标准库、零网络）
        —— 覆盖"我在准备考研""我在广州上学"这类**明确的第一人称陈述**，
           且永远可用（无 Key、断网都不影响）。
    轨道 B：模型抽（可选注入）
        —— 抽取更细的语义；契约不符时静默回落轨道 A。

两类内容分开存（这是有意的）
--------------------------------------------------------------------------
    档案（本文件，关于**这个人**）  → 必须确认才写入
    通用知识（knowledge.py，关于**世界**）→ 直接检索，不涉及个人

隐私
--------------------------------------------------------------------------
    只写本机 data/profile/profile.json（已在 .gitignore 忽略），永不联网。
    用户可查看、可编辑、可单条删除、可全部删除。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from chat.history import replace_with_retry

__all__ = [
    "KINDS",
    "MAX_ACTIVE_ENTRIES",
    "ProfileEntry",
    "ProfileDecision",
    "ProfileExtractor",
    "ProfileStore",
    "extract_candidates",
    "score_relevance",
    "select_relevant",
    "build_profile_block",
    "new_entry_id",
]

SCHEMA_VERSION = 1

# 档案条目的类别：刻意**不含"情绪"**——情绪是状态（state.py 管），
# 不是需要长期记住的事实。把"他很低落"写进档案是最典型的误用。
KINDS: tuple[str, ...] = ("fact", "preference", "goal", "relationship", "event")

# 最多同时激活多少条（防止档案无限膨胀，也避免 Prompt 被撑爆）
MAX_ACTIVE_ENTRIES = 60

# 注入 Prompt 时最多带几条
MAX_PROMPT_ENTRIES = 12

_WS_RE = re.compile(r"\s+")


def new_entry_id() -> str:
    return str(uuid.uuid4())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ===========================================================================
# 1. 条目
# ===========================================================================
@dataclass
class ProfileEntry:
    """一条**用户已确认**的档案（或一条待确认的候选）。"""

    kind: str                        # KINDS 之一
    key: str                         # 稳定的键：考研 / 广州 / 母亲 …
    value: str                       # 用户的说法（保持他自己的措辞）
    entry_id: str = field(default_factory=new_entry_id)
    source: str = "confirmed"        # confirmed / edited
    created_at: str = ""
    updated_at: str = ""
    times_used: int = 0
    confidence: float = 0.8

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "fact"
        self.key = _WS_RE.sub(" ", str(self.key or "")).strip()
        self.value = _WS_RE.sub(" ", str(self.value or "")).strip()
        now = _utc_now_iso()
        self.created_at = self.created_at or now
        self.updated_at = self.updated_at or self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "times_used": int(self.times_used),
            "confidence": round(float(self.confidence), 3),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProfileEntry | None":
        if not isinstance(payload, Mapping):
            return None
        value = _WS_RE.sub(" ", str(payload.get("value") or "")).strip()
        key = _WS_RE.sub(" ", str(payload.get("key") or "")).strip()
        if not value or not key:
            return None
        entry = cls(
            kind=str(payload.get("kind") or "fact"),
            key=key,
            value=value,
            entry_id=str(payload.get("entry_id") or new_entry_id()),
            source=str(payload.get("source") or "confirmed"),
        )
        entry.created_at = str(payload.get("created_at") or entry.created_at)
        entry.updated_at = str(payload.get("updated_at") or entry.updated_at)
        try:
            entry.times_used = int(payload.get("times_used") or 0)
        except (TypeError, ValueError):
            entry.times_used = 0
        try:
            entry.confidence = float(payload.get("confidence") or 0.8)
        except (TypeError, ValueError):
            entry.confidence = 0.8
        return entry

    def to_sentence(self) -> str:
        """给用户看/进 Prompt 的自然说法。"""
        return self.value

    def describe(self) -> str:
        """确认卡上显示的一行。"""
        return f"[{self.kind}] {self.value}"


# ===========================================================================
# 2. 确认门控（红线实现）
# ===========================================================================
@dataclass
class ProfileDecision:
    """一次用户对候选的处置（确认 / 修正 / 跳过）。

    记录这个是为了**让"确认门控"变成可量化的变量**：
    没有它就没法做"有门控 vs 无门控"的消融实验。
    """

    action: str                   # accepted / edited / rejected
    entry: ProfileEntry | None = None
    original_value: str = ""
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "original_value": self.original_value,
            "final_value": self.entry.value if self.entry else "",
            "kind": self.entry.kind if self.entry else "",
            "created_at": self.created_at,
        }


# ===========================================================================
# 3. 规则抽取（轨道 A）
# ===========================================================================
# 每条 = (kind, key, 正则)。正则必须带第一人称，避免把别人或假设句抽进来。
# 写法刻意保持简单（不用捕获组、不用嵌套可选）：这类规则最容易在括号上出错，
# 而一旦出错就是整个模块无法导入——写简单点更稳。
_RULES: tuple[tuple[str, str, re.Pattern], ...] = (
    ("goal", "考研", re.compile(r"在准备考研|正在考研|准备考研")),
    ("goal", "考公", re.compile(r"在考公|在考公务员|准备考公务员")),
    ("goal", "找工作", re.compile(r"在找工作|在投简历|正在找工作")),
    ("goal", "毕业", re.compile(r"快毕业|要毕业|即将毕业")),
    ("fact", "学校", re.compile(r"在[\u4e00-\u9fa5]{2,8}(?:大学|学院)")),
    ("fact", "专业", re.compile(r"[\u4e00-\u9fa5]{2,8}专业")),
    ("fact", "城市", re.compile(r"在[\u4e00-\u9fa5]{2,6}(?:上学|上班|工作|生活)")),
    ("fact", "工作", re.compile(r"是[\u4e00-\u9fa5]{2,8}(?:工程师|老师|护士|医生|程序员|会计|设计师)")),
    ("relationship", "母亲", re.compile(r"[妈母][妈亲]?[^。！？,，]{0,8}(?:吵架|关系|不理解|生病|住院)")),
    ("relationship", "父亲", re.compile(r"[爸父][爸亲]?[^。！？,，]{0,8}(?:吵架|关系|不理解|生病|住院)")),
    ("relationship", "伴侣", re.compile(r"(?:男朋友|女朋友|对象|老公|老婆|伴侣)[^。！？,，]{0,8}(?:分手|吵架|冷战|不理解)")),
    # 反复出现的情境，比一次性情绪更值得在用户确认后记住。
    # 只匹配明确写成“朋友…不回消息…我做错”和“家里…催婚…”的陈述，
    # 不把普通的“今天有点累”升级成长期档案。
    ("relationship", "朋友不回消息", re.compile(
        r"朋友[^。！？,，]{0,10}(?:不回消息|不回我|不理我)"
        r"[^。！？\n]{0,16}(?:做错|说错|有问题|自责)")),
    ("relationship", "催婚", re.compile(
        r"(?:家里|父母|家人)[^。！？,，]{0,10}催婚"
        r"(?:[^。！？\n]{0,20}(?:回去|被问|烦|压力))?")),
    ("preference", "不喜欢", re.compile(r"我不喜欢[\u4e00-\u9fa5]{2,10}|我不太喜欢[\u4e00-\u9fa5]{2,10}")),
    ("preference", "喜欢", re.compile(r"我喜欢[\u4e00-\u9fa5]{2,10}|我很喜欢[\u4e00-\u9fa5]{2,10}|我特别喜欢[\u4e00-\u9fa5]{2,10}")),
)

# 这些词命中说明是"当下的情绪"而不是长期事实 —— 一律不抽
_TRANSIENT_MARKERS = ("今天", "刚才", "现在", "此刻", "这会儿")
# 疑问/假设句不抽（那不是陈述事实）
_QUESTION_MARKERS = ("？", "?", "吗", "呢", "如果", "要是", "假如")


def _looks_like_statement(text: str) -> bool:
    """判断像不像"陈述一条事实"，而不是提问或假设。"""
    if any(marker in text for marker in _QUESTION_MARKERS):
        return False
    return True


def extract_candidates(text: Any, *, max_candidates: int = 2) -> list[ProfileEntry]:
    """轨道 A：从一句话里抽出**候选**档案（未经确认，不能直接写入）。

    返回的候选只是"提议"，最终是否进档案由用户决定。

    只抽明确的长期陈述：
        - 不抽带"今天/刚才"这类当下词的内容（那是情绪，不是事实）
        - 不抽疑问句与假设句
        - 一句话最多两条，避免把长文本刷成一堆候选
    """
    body = _WS_RE.sub(" ", str(text or "")).strip()
    if not body or not _looks_like_statement(body):
        return []
    if any(marker in body for marker in _TRANSIENT_MARKERS):
        return []

    found: list[ProfileEntry] = []
    seen_keys: set[str] = set()
    for kind, key, pattern in _RULES:
        if key in seen_keys:
            continue
        match = pattern.search(body)
        if not match:
            continue
        # 有捕获组就用捕获到的片段，否则用命中的整句（保持用户措辞）
        captured = match.group(1) if match.groups() else ""
        value = (captured or match.group(0)).strip()
        # 保持用户自己的说法：把命中片段换成更短的键名意义不大，
        # 这里直接用原句片段，读起来最自然。
        found.append(ProfileEntry(
            kind=kind,
            key=key,
            value=match.group(0).strip(),
            source="candidate",
            confidence=0.7,
        ))
        seen_keys.add(key)
        if len(found) >= max(1, int(max_candidates)):
            break
    return found


# ===========================================================================
# 4. 检索（按当下情境挑相关条目）
# ===========================================================================
def _tokens(text: str) -> set[str]:
    """极简切词：CJK 按 bigram + 英文数字按词。零依赖，够用于小档案。"""
    body = _WS_RE.sub(" ", str(text or "")).lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]+", body))
    cjk = re.findall(r"[\u4e00-\u9fa5]", body)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    tokens.update(cjk)
    return tokens


def score_relevance(entry: ProfileEntry, context: str) -> float:
    """条目与当前语境的简单相关度（0~1）。"""
    ctx = _tokens(context)
    if not ctx:
        return 0.0
    entry_tokens = _tokens(f"{entry.key} {entry.value}")
    if not entry_tokens:
        return 0.0
    overlap = len(ctx & entry_tokens)
    return overlap / max(1, len(entry_tokens))


def select_relevant(entries: Iterable[ProfileEntry], context: str, *,
                    max_items: int = MAX_PROMPT_ENTRIES) -> list[ProfileEntry]:
    """挑出与当前语境最相关的若干条；相关的排前面，其余按最近更新兜底。

    为什么要有"其余兜底"：完全不相关时也要带一点档案，
    否则"记得你"这件事就只在提到关键词时才生效，显得很假。
    """
    items = list(entries)
    if not items:
        return []
    scored = [(score_relevance(e, context), e.updated_at, e) for e in items]
    # 相关的排前面（分数降序），同分时最近更新的优先
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    relevant = [entry for score, _, entry in scored if score > 0]
    if not relevant:
        # 完全不相关时也要带一点：否则"记得你"只在提到关键词时才生效，显得很假
        relevant = [entry for _, _, entry in scored]
    return relevant[:max(1, int(max_items))]


def build_profile_block(entries: Iterable[ProfileEntry], context: str = "", *,
                        max_items: int = MAX_PROMPT_ENTRIES) -> str | None:
    """把档案拼成给模型的提示块；没有档案时返回 None（不打扰模型）。

    措辞刻意保守：明确告诉模型"这是用户确认过的、可能过时、不要生硬引用"。
    """
    chosen = select_relevant(entries, context, max_items=max_items)
    if not chosen:
        return None
    lines = ["（以下是用户之前确认过的关于他自己的信息，可用于更好地理解他："]
    lines.extend(f"  - {entry.value}" for entry in chosen)
    lines.append("  注意：这些可能已经过时；自然地带入即可，不要生硬复述，"
                 "也不要说『根据我的记录』。）")
    return "\n".join(lines)


# ===========================================================================
# 5. 存储（本机 JSON；确认过的条目 + 处置记录）
# ===========================================================================
class ProfileStore:
    """档案存储。

    文件结构（schema 1）：
        {
          "schema": 1,
          "entries": [ {...}, ... ],      # **只有确认/修正过的**
          "decisions": [ {...}, ... ]     # 确认门控的处置记录（用于消融统计）
        }

    单文件而非目录：档案量小（最多几十条），看/改/删都最省事。
    """

    def __init__(self, base_dir: Path | str, *, filename: str = "profile.json") -> None:
        self.base_dir = Path(base_dir)
        self.file = self.base_dir / filename

    # ---- 基础 ----
    def ensure_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    def _read_raw(self) -> dict[str, Any]:
        if not self.file.is_file():
            return {"schema": SCHEMA_VERSION, "entries": [], "decisions": []}
        try:
            payload = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema": SCHEMA_VERSION, "entries": [], "decisions": []}
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_VERSION:
            return {"schema": SCHEMA_VERSION, "entries": [], "decisions": []}
        return {
            "schema": SCHEMA_VERSION,
            "entries": list(payload.get("entries") or []),
            "decisions": list(payload.get("decisions") or []),
        }

    def _write_raw(self, payload: Mapping[str, Any]) -> None:
        self.ensure_dir()
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        replace_with_retry(tmp, self.file)

    # ---- 读 ----
    def entries(self) -> list[ProfileEntry]:
        """全部已确认条目（损坏行跳过，不让一条脏数据毁掉整个档案）。"""
        out: list[ProfileEntry] = []
        for item in self._read_raw()["entries"]:
            entry = ProfileEntry.from_dict(item)
            if entry is not None:
                out.append(entry)
        return out

    def decisions(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._read_raw()["decisions"] if isinstance(d, Mapping)]

    def find(self, key: str) -> ProfileEntry | None:
        wanted = str(key or "").strip()
        for entry in self.entries():
            if entry.key == wanted:
                return entry
        return None

    def exists(self) -> bool:
        return self.file.is_file()

    # ---- 确认门控（唯一写入路径）----
    def accept_candidate(self, candidate: ProfileEntry) -> ProfileEntry:
        """用户点"对，就是这样" → 写入档案。"""
        entry = ProfileEntry(
            kind=candidate.kind, key=candidate.key, value=candidate.value,
            source="confirmed", confidence=max(0.8, candidate.confidence),
        )
        self._upsert(entry)
        self._record(ProfileDecision("accepted", entry,
                                     original_value=candidate.value))
        return entry

    def edit_candidate(self, candidate: ProfileEntry, corrected: str) -> ProfileEntry | None:
        """用户改了措辞 → 以**用户的话为准**写入。"""
        cleaned = _WS_RE.sub(" ", str(corrected or "")).strip()
        if not cleaned:
            return self.reject_candidate(candidate)
        entry = ProfileEntry(
            kind=candidate.kind, key=candidate.key, value=cleaned,
            source="edited", confidence=0.95,     # 用户亲口纠正过，最可信
        )
        self._upsert(entry)
        self._record(ProfileDecision("edited", entry,
                                     original_value=candidate.value))
        return entry

    def reject_candidate(self, candidate: ProfileEntry) -> None:
        """用户点"不是这样" / 直接跳过 → **绝不写入**。"""
        self._record(ProfileDecision("rejected", None,
                                     original_value=candidate.value))

    # ---- 维护 ----
    def delete(self, entry_id: str) -> bool:
        payload = self._read_raw()
        keep = [e for e in payload["entries"]
                if str(e.get("entry_id") or "") != str(entry_id)]
        if len(keep) == len(payload["entries"]):
            return False
        payload["entries"] = keep
        self._write_raw(payload)
        return True

    def clear(self) -> int:
        """删除全部档案（保留门控统计？不——用户说清空就是清空）。"""
        payload = self._read_raw()
        removed = len(payload["entries"])
        self._write_raw({"schema": SCHEMA_VERSION, "entries": [], "decisions": []})
        return removed

    def mark_used(self, entry_ids: Iterable[str]) -> None:
        """标记这些条目被用进了 Prompt（用于观察哪些真正有用）。"""
        wanted = {str(x) for x in entry_ids}
        if not wanted:
            return
        payload = self._read_raw()
        changed = False
        for item in payload["entries"]:
            if str(item.get("entry_id") or "") in wanted:
                item["times_used"] = int(item.get("times_used") or 0) + 1
                changed = True
        if changed:
            self._write_raw(payload)

    # ---- 内部 ----
    def _upsert(self, entry: ProfileEntry) -> None:
        payload = self._read_raw()
        items = list(payload["entries"])
        for index, item in enumerate(items):
            if str(item.get("key") or "") == entry.key:
                # 同一个 key 只保留最新（比如从"在准备考研"变成"考上了"）
                entry.times_used = int(item.get("times_used") or 0)
                items[index] = entry.to_dict()
                break
        else:
            items.append(entry.to_dict())

        # 上限保护：超出时丢最久没更新、最少被用的
        if len(items) > MAX_ACTIVE_ENTRIES:
            items.sort(key=lambda e: (int(e.get("times_used") or 0),
                                      str(e.get("updated_at") or "")),
                       reverse=True)
            items = items[:MAX_ACTIVE_ENTRIES]

        payload["entries"] = items
        self._write_raw(payload)

    def _record(self, decision: ProfileDecision) -> None:
        payload = self._read_raw()
        payload["decisions"].append(decision.to_dict())
        self._write_raw(payload)

    # ---- 统计 ----
    def stats(self) -> dict[str, Any]:
        payload = self._read_raw()
        decisions = [d for d in payload["decisions"] if isinstance(d, Mapping)]
        counts = {"accepted": 0, "edited": 0, "rejected": 0}
        for item in decisions:
            action = str(item.get("action") or "")
            if action in counts:
                counts[action] += 1
        proposed = sum(counts.values())
        return {
            "path": str(self.file),
            "entries": len(self.entries()),
            "proposed": proposed,
            "accepted": counts["accepted"],
            "edited": counts["edited"],
            "rejected": counts["rejected"],
            # 用户认可率：确认 + 修正 都算"这条提议方向是对的"
            "accept_rate": ((counts["accepted"] + counts["edited"]) / proposed)
            if proposed else None,
            # 系统猜错率：这个数字是做消融实验时最该盯的
            "wrong_rate": (counts["rejected"] / proposed) if proposed else None,
        }


# ===========================================================================
# 6. 模型抽取（轨道 B，可选注入）
# ===========================================================================
EXTRACT_PROMPT = (
    "从这句话里找出**关于说话者本人的长期信息**，只输出 JSON，不要解释。\n"
    "只提取：长期事实（学校/专业/城市/工作）、长期偏好、目标计划、重要关系。\n"
    "不要提取：当下的情绪、临时的状态、别人的事、猜测。\n"
    "格式：{\"items\":[{\"kind\":\"fact|preference|goal|relationship\","
    "\"key\":\"简短的键\",\"value\":\"用他自己的话\"}],\"confidence\":0-1}\n"
    "没有可提取的就输出 {\"items\":[],\"confidence\":0}。"
)


def parse_extraction_payload(payload: Any) -> list[ProfileEntry]:
    """把模型输出解析成候选；契约不符返回空列表（回落规则轨）。"""
    if not isinstance(payload, Mapping):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: list[ProfileEntry] = []
    for item in items[:2]:                     # 一句话最多两条
        if not isinstance(item, Mapping):
            continue
        entry = ProfileEntry.from_dict({
            "kind": item.get("kind") or "fact",
            "key": item.get("key") or "",
            "value": item.get("value") or "",
            "source": "candidate",
        })
        if entry is not None:
            out.append(entry)
    return out


class ProfileExtractor:
    """轨道 B：用模型抽候选。默认不启用；任何失败都回落规则轨。"""

    def __init__(self, model: Any = None) -> None:
        self._model = model

    def extract(self, text: Any) -> list[ProfileEntry]:
        if self._model is None or not str(text or "").strip():
            return []
        try:
            response = self._model.invoke([
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": str(text)},
            ])
        except Exception:  # noqa: BLE001 - 抽取失败不影响对话
            return []
        content = getattr(response, "content", response)
        payload: Any = None
        if isinstance(content, Mapping):
            payload = content
        elif isinstance(content, str):
            start, end = content.find("{"), content.rfind("}")
            if start != -1 and end > start:
                try:
                    payload = json.loads(content[start:end + 1])
                except json.JSONDecodeError:
                    payload = None
        return parse_extraction_payload(payload)


def extract_with_fallback(text: Any, *, extractor: ProfileExtractor | None = None,
                          max_candidates: int = 2) -> list[ProfileEntry]:
    """先试模型轨，抽不到就用规则轨（保证永远有可用输出）。"""
    if extractor is not None:
        found = extractor.extract(text)
        if found:
            return found[:max_candidates]
    return extract_candidates(text, max_candidates=max_candidates)
