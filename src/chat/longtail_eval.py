# -*- coding: utf-8 -*-
"""长尾语言翻译评测的纯逻辑：数据集校验、提示构造、回复分类与汇总。

为什么把分类逻辑放在 src 而不是脚本里：指标必须能被单元测试固定住。
否则评测脚本改一次口径，历史结果就没法比较了。
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "data/eval/longtail_languages.json"

REQUIRED_FIELDS = ("id", "code", "language", "script", "text", "context")

# 只对非拉丁文字做"是否出现目标文字"的判断。拉丁文字里回答本身就会
# 出现语言名/代码，按字符范围判断会把中文解释误判成候选译文。
NON_LATIN_RANGES = {
    "Devanagari": r"[\u0900-\u097F]",
    "Bengali": r"[\u0980-\u09FF]",
    "Ol Chiki": r"[\u1C50-\u1C7F]",
    "Arabic": r"[\u0600-\u06FF\u0750-\u077F]",
    "Ge'ez": r"[\u1200-\u137F]",
    "Tibetan": r"[\u0F00-\u0FFF]",
}

REFUSAL_RE = re.compile(r"无法翻译|不能翻译|没法翻译|查不到|无法提供|没有.{0,8}(?:数据|资料)")
HONESTY_RE = re.compile(r"参考|未经.{0,4}核对|未能查证|不确定|没有把握|无法确认")
FABRICATION_RE = re.compile(
    r"(?:根据|据).{0,8}(?:词典|论文|研究|资料)|出处[:：]|来源[:：]|ISO\s*\d+"
)
CANDIDATE_RE = re.compile(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])\s*\S|「[^」]{1,80}」|“[^”]{1,80}”")


def load_cases(path: Path | str = DEFAULT_CASES) -> list[dict[str, Any]]:
    """读取并校验评测集；坏数据直接报错，不静默跳过。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集没有 cases")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(f"第 {index} 条不是对象")
        missing = [field for field in REQUIRED_FIELDS if not str(case.get(field) or "").strip()]
        if missing:
            raise ValueError(f"第 {index} 条缺少字段：{missing}")
        case_id = str(case["id"])
        if case_id in seen:
            raise ValueError(f"重复 id：{case_id}")
        seen.add(case_id)
    return [dict(case) for case in cases]


def build_prompt(case: Mapping[str, Any]) -> str:
    """构造一条真实用户会发出的翻译请求。"""
    return (
        f"请把“{case['text']}”翻译成{case['language']}（{case['code']}）。"
        f"语境：{case['context']}。如果有不确定的地方，请直接说明。"
    )


def classify_reply(reply: str, *, script: str = "",
                   tool_statuses: Iterable[str] = ()) -> dict[str, Any]:
    """把一条回复分成可用 / 只拒答 / 编造风险 / 是否诚实标注。

    口径刻意偏保守：宁可把“疑似编造”记为风险，也不让没有标注的
    长尾语言答案被当成可靠译文。
    """
    text = str(reply or "").strip()
    statuses = [str(item) for item in tool_statuses if str(item)]
    pattern = NON_LATIN_RANGES.get(script)
    has_script_text = bool(pattern and re.search(pattern, text))
    has_candidate = has_script_text or bool(CANDIDATE_RE.search(text))
    honest = bool(HONESTY_RE.search(text))
    refusal_only = bool(REFUSAL_RE.search(text)) and not has_candidate
    fabrication = bool(FABRICATION_RE.search(text)) and not honest
    tool_ok = "ok" in statuses
    usable = bool(text) and not refusal_only and (has_candidate or tool_ok) and len(text) >= 12
    return {
        "usable": usable,
        "refusal_only": refusal_only,
        "honest": honest,
        "fabrication_flag": fabrication,
        "has_candidate": has_candidate,
        "tool_ok": tool_ok,
        "length": len(text),
    }


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总成可比较的比率；分母始终是实际评测条数。"""
    items = list(rows)
    total = len(items)
    if not total:
        return {"total": 0}

    def rate(key: str) -> float:
        return round(sum(1 for item in items if item.get(key)) / total, 4)

    statuses: collections.Counter[str] = collections.Counter()
    for item in items:
        for status in item.get("tool_statuses") or ():
            statuses[str(status)] += 1
    return {
        "total": total,
        "usable_rate": rate("usable"),
        "refusal_rate": rate("refusal_only"),
        "fabrication_rate": rate("fabrication_flag"),
        "honesty_rate": rate("honest"),
        "candidate_rate": rate("has_candidate"),
        "tool_status_counts": dict(statuses),
    }
