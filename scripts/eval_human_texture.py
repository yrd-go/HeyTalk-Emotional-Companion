# -*- coding: utf-8 -*-
"""Focused 12-case test for conversational human texture.

Arms all use the current full stack and DeepSeek Pro; only the base prompt changes:
  current     persona.SYSTEM_PROMPT
  wechat      current + colloquial, no-summary, shorter-turn overlay
  hypothesis  current + offer-a-correctable-guess overlay

Two judges score every pair in both orders. The script reuses the audited
generation/judging helpers from eval_emotional_30.py.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import eval_emotional_30 as base_eval
from chat import conversation as con
from chat import persona, role_router, roles, scaffold

CASES_PATH = ROOT / "data/eval/human_texture_12_20261018.json"
WECHAT_OVERLAY = ROOT / "data/eval/texture_wechat.txt"
HYPOTHESIS_OVERLAY = ROOT / "data/eval/texture_hypothesis.txt"
COMBINED_OVERLAY = ROOT / "data/eval/texture_combined.txt"
VARIANTS = ("current", "wechat", "hypothesis", "combined")
JUDGES = (("deepseek", "DeepSeek Flash"), ("zhipu", "GLM-4 Flash"))
COMPARISONS = (
    ("wechat_vs_current", "wechat", "current"),
    ("hypothesis_vs_current", "hypothesis", "current"),
    ("combined_vs_current", "combined", "current"),
    ("hypothesis_vs_wechat", "hypothesis", "wechat"),
    ("combined_vs_wechat", "combined", "wechat"),
    ("combined_vs_hypothesis", "combined", "hypothesis"),
)
METRICS = (
    "natural", "human_texture", "understanding", "specific",
    "non_template", "restraint", "overall",
)

JUDGE_SYSTEM = """你是对话质量的盲评员。你会看到一段对话历史、用户最后说的话，以及两个候选回复 A/B。
按 1-5 分分别评价：
natural：像具体的人在自然说话，不像模板、客服或文章。
human_texture：有人味。能接住刚说过的具体词，反应具体，节奏像微信聊天；不把对方当成需要被分析的案例；不每轮都总结+追问。
understanding：准确接住用户的具体内容与情绪，不空泛，不把自己的猜测当成事实。
specific：给出具体理解、关键判断，或一个可执行的下一步；不是只说“我听到了”。
non_template：没有固定骨架、套话、机械追问、无脑附和。
restraint：长度和留白合适；不一次倒完，不连续追问，不说教，不越界承诺。过长、清单倾倒、连续问句要扣分。
overall：综合自然度、人味、理解、具体性、克制与对话推进。

重要规则：
1. 不要因为回复更长、用了 Markdown、或“看起来更专业”就加分。
2. 只依据对话历史与用户最后一句话评分。
3. 两个回复几乎无差别时才选 tie；有可说明的差异就选 A 或 B。
4. 只输出一行 JSON，不要代码块：
{"A":{"natural":1,"human_texture":1,"understanding":1,"specific":1,"non_template":1,"restraint":1,"overall":1},"B":{"natural":1,"human_texture":1,"understanding":1,"specific":1,"non_template":1,"restraint":1,"overall":1},"winner":"A","reason":"不超过40字"}
"""

base_eval.METRICS = METRICS
base_eval.JUDGE_SYSTEM = JUDGE_SYSTEM


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def insert_overlay(prompt: str, overlay: str) -> str:
    marker = "\n\n**你不只聊情绪。**"
    text = overlay.strip()
    if marker in prompt:
        head, tail = prompt.split(marker, 1)
        return f"{head}\n\n{text}{marker}{tail}"
    return f"{text}\n\n{prompt}"


def build_prompts() -> dict[str, str]:
    current = persona.SYSTEM_PROMPT
    wechat = insert_overlay(
        current, WECHAT_OVERLAY.read_text(encoding="utf-8")
    )
    hypothesis = insert_overlay(
        current, HYPOTHESIS_OVERLAY.read_text(encoding="utf-8")
    )
    combined = insert_overlay(
        current, COMBINED_OVERLAY.read_text(encoding="utf-8")
    )
    return {
        "current": current,
        "wechat": wechat,
        "hypothesis": hypothesis,
        "combined": combined,
    }


PROMPTS = build_prompts()


def load_cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"no cases in {path}")
    return cases


def messages_for(case: dict, variant: str) -> tuple[list[dict], dict]:
    text = str(case["text"])
    decision = role_router.route_role(text, snapshot=None, safety_level="none")
    role_id = getattr(decision, "role_id", None) or roles.DEFAULT_ROLE_ID
    role_instruction = roles.build_role_instruction(role_id)
    knowledge_block = base_eval.handbook_block(text)
    scaffold_block = scaffold.build_scaffold(text)
    parts = [PROMPTS[variant], role_instruction]
    if knowledge_block:
        parts.append(knowledge_block)
    if scaffold_block:
        parts.append(scaffold_block)
    history = base_eval.history_messages(case) + [{"role": "user", "content": text}]
    messages = [{"role": "system", "content": "\n\n".join(parts)}] + history
    meta = {
        "variant": variant,
        "prompt_chars": len(PROMPTS[variant]),
        "role_id": role_id,
        "role_reason": getattr(decision, "reason", ""),
        "knowledge": bool(knowledge_block),
        "scaffold": scaffold_block,
    }
    return messages, meta


def generate_one(case: dict, variant: str) -> dict:
    messages, meta = messages_for(case, variant)
    reply, error, elapsed = base_eval.invoke_with_retry("deepseek-pro", messages)
    item = {
        "arm": variant,
        "engine": "deepseek-pro",
        "reply": reply,
        "error": error,
        "elapsed_s": elapsed,
        "meta": meta,
    }
    item.update(base_eval.reply_metrics(reply))
    return item


def run_generation(cases: list[dict], tag: str, workers: int) -> Path:
    output = ROOT / "data/eval" / f"texture_raw_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases_file": str(CASES_PATH),
        "prompt_chars": {name: len(prompt) for name, prompt in PROMPTS.items()},
        "cases": [],
    }
    for index, case in enumerate(cases, 1):
        base = {key: value for key, value in case.items() if key != "history"}
        base["history"] = base_eval.history_messages(case)
        with ThreadPoolExecutor(max_workers=min(workers, len(VARIANTS))) as pool:
            futures = {pool.submit(generate_one, case, v): v for v in VARIANTS}
            results = {}
            for future in as_completed(futures):
                variant = futures[future]
                try:
                    results[variant] = future.result()
                except Exception as exc:
                    results[variant] = {
                        "arm": variant, "engine": "deepseek-pro", "reply": "",
                        "error": f"{type(exc).__name__}: {exc}", "elapsed_s": 0,
                        "meta": {}, **base_eval.reply_metrics(""),
                    }
        base["arms"] = {v: results[v] for v in VARIANTS}
        document["cases"].append(base)
        base_eval.checkpoint(output, document)
        brief = " | ".join(
            f"{v}:{results[v]['chars']}字/{results[v]['elapsed_s']}s"
            + (" ERR" if results[v]["error"] else "")
            for v in VARIANTS
        )
        print(f"[{index:02d}/{len(cases)}] {case['id']}  {brief}", flush=True)
    print("saved", output, flush=True)
    return output


def summarize(raw: dict, votes: list[dict]) -> dict:
    arm_summary = {}
    for variant in VARIANTS:
        items = [case["arms"][variant] for case in raw["cases"]]
        valid = [item for item in items if item.get("reply")]
        arm_summary[variant] = {
            "valid": len(valid),
            "errors": len(items) - len(valid),
            "mean_chars": round(statistics.mean([x["chars"] for x in valid]), 1) if valid else None,
            "mean_paragraphs": round(statistics.mean([x["paragraphs"] for x in valid]), 2) if valid else None,
            "mean_questions": round(statistics.mean([x["questions"] for x in valid]), 2) if valid else None,
            "mean_elapsed_s": round(statistics.mean([x["elapsed_s"] for x in valid]), 1) if valid else None,
        }

    comparisons = {}
    for label, arm_a, arm_b in COMPARISONS:
        rows = [r for r in votes if r.get("comparison") == label and "winner" in r]
        wins = collections.Counter(r["winner"] for r in rows)
        per_case = collections.defaultdict(list)
        per_judge = collections.defaultdict(collections.Counter)
        metric_deltas = collections.defaultdict(list)
        for row in rows:
            per_judge[row["judge"]][row["winner"]] += 1
            per_case[row["case"]].append(row["winner"])
            scores = row["scores_by_arm"]
            for metric in METRICS:
                metric_deltas[metric].append(scores[arm_a][metric] - scores[arm_b][metric])
        decisive = wins.get(arm_a, 0) + wins.get(arm_b, 0)
        comparisons[label] = {
            "arms": [arm_a, arm_b],
            "votes": len(rows),
            "wins": {arm_a: wins.get(arm_a, 0), arm_b: wins.get(arm_b, 0), "tie": wins.get("tie", 0)},
            "win_rate_excluding_ties": {
                arm_a: round(wins.get(arm_a, 0) / max(1, decisive), 3),
                arm_b: round(wins.get(arm_b, 0) / max(1, decisive), 3),
            },
            "mean_scores": {arm: base_eval.mean_scores_for(rows, arm) for arm in (arm_a, arm_b)},
            "metric_delta": {
                metric: round(statistics.mean(values), 3) if values else None
                for metric, values in metric_deltas.items()
            },
            "by_judge": {judge: dict(counts) for judge, counts in per_judge.items()},
            "by_case": {case: dict(collections.Counter(winners)) for case, winners in per_case.items()},
            "invalid_votes": sum(
                1 for r in votes
                if r.get("comparison") == label and "winner" not in r
            ),
        }
    return {
        "cases": len(raw["cases"]),
        "arm_summary": arm_summary,
        "prompt_chars": raw.get("prompt_chars", {}),
        "comparisons": comparisons,
        "valid_votes": len([r for r in votes if "winner" in r]),
        "invalid_votes": len([r for r in votes if "winner" not in r]),
    }


def write_report(path: Path, raw: dict, summary: dict) -> None:
    lines = [
        "# 12 条人味聚焦盲评",
        "",
        f"- 生成时间：{raw.get('generated_at', '')}",
        f"- Prompt 长度：{summary['prompt_chars']}",
        f"- 有效票：{summary['valid_votes']}；无效：{summary['invalid_votes']}",
        "",
        "## 客观指标",
        "",
        "| 版本 | 平均字数 | 平均段数 | 平均问号 | 平均耗时(s) |",
        "|---|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        row = summary["arm_summary"][variant]
        lines.append(
            f"| {variant} | {row['mean_chars']} | {row['mean_paragraphs']} | "
            f"{row['mean_questions']} | {row['mean_elapsed_s']} |"
        )
    lines += ["", "## 盲评", ""]
    for label, data in summary["comparisons"].items():
        arm_a, arm_b = data["arms"]
        lines += [
            f"### {arm_a} vs {arm_b}",
            "",
            f"- 票数：{data['wins']}",
            f"- 去平局胜率：{arm_a} {data['win_rate_excluding_ties'][arm_a]:.1%} "
            f"vs {arm_b} {data['win_rate_excluding_ties'][arm_b]:.1%}",
            f"- 指标差（{arm_a} - {arm_b}）：{data['metric_delta']}",
            f"- 按评委：{data['by_judge']}",
            "",
        ]

    focus = {"tired_after_cant_move", "tired", "ferrari", "m_selfworth_ferrari", "joke", "no_advice"}
    lines += ["## 截图相关案例", ""]
    for case in raw["cases"]:
        if case["id"] not in focus:
            continue
        lines += [f"### {case['id']}", "", f"用户：{case['text']}", ""]
        for variant in VARIANTS:
            item = case["arms"][variant]
            lines += [
                f"**{variant}**（{item['chars']} 字，{item['questions']} 问）",
                "",
                item["reply"] or f"[生成失败] {item['error']}",
                "",
            ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_judging(raw_path: Path, tag: str, workers: int) -> tuple[Path, Path, dict]:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    tasks = []
    for label, arm_a, arm_b in COMPARISONS:
        for case in raw["cases"]:
            for engine, _ in JUDGES:
                tasks.append((label, arm_a, arm_b, case, engine))
    votes = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(base_eval.judge_vote, label, arm_a, arm_b, case, engine):
                (label, case["id"], engine)
            for label, arm_a, arm_b, case, engine in tasks
        }
        done = 0
        for future in as_completed(futures):
            label, case_id, engine = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                rows = [{
                    "comparison": label, "case": case_id, "judge": engine,
                    "error": f"{type(exc).__name__}: {exc}",
                }]
            votes.extend(rows)
            done += 1
            print(f"[judge {done:03d}/{len(tasks)}] {label} {case_id} {engine}", flush=True)
    summary = summarize(raw, votes)
    output = ROOT / "data/eval" / f"texture_judged_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "raw_file": str(raw_path),
        "judged_at": datetime.now().isoformat(timespec="seconds"),
        "judges": [{"engine": engine, "label": label} for engine, label in JUDGES],
        "summary": summary,
        "votes": votes,
    }
    base_eval.checkpoint(output, document)
    report = ROOT / "reports" / f"texture_{tag}_{now_tag()}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, raw, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("saved", output, flush=True)
    print("report", report, flush=True)
    return output, report, summary


def main() -> None:
    global CASES_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("generate", "judge", "all"), default="all")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--raw", default="")
    parser.add_argument("--tag", default="texture")
    parser.add_argument("--gen-workers", type=int, default=3)
    parser.add_argument("--judge-workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    CASES_PATH = Path(args.cases)
    cases = load_cases(CASES_PATH)
    if args.limit > 0:
        cases = cases[:args.limit]
    raw_path = Path(args.raw) if args.raw else None
    if args.stage in {"generate", "all"}:
        raw_path = run_generation(cases, args.tag, args.gen_workers)
    if args.stage in {"judge", "all"}:
        if raw_path is None:
            raise SystemExit("--stage judge requires --raw")
        run_judging(raw_path, args.tag, args.judge_workers)


if __name__ == "__main__":
    main()
