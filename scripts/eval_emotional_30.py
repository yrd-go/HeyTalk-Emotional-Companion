# -*- coding: utf-8 -*-
"""30-case emotional dialogue evaluation.

Arms:
  full-pro      current persona + role routing + handbook retrieval + scaffold, DeepSeek Pro
  minimal-pro   neutral short prompt, same history, DeepSeek Pro
  full-flash    current full stack, DeepSeek Flash

The script generates replies, then runs two blind judges in both A/B orders.
No API key is printed or stored.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from paths import EMOTIONAL_HANDBOOK, load_env

load_env()

from chat import conversation as con
from chat import knowledge as kb
from chat import persona, role_router, roles, scaffold

CASES_PATH = ROOT / "data/eval/emotional_30_20261018.json"
MINIMAL_PROMPT = (
    "你是一个自然、直接、愿意认真听人说话的对话伙伴。"
    "不要套话，不要空头保证，不要诊断对方。"
)
ARMS = ("full-pro", "minimal-pro", "full-flash")
ARM_CONFIG = {
    "full-pro": ("deepseek-pro", True),
    "minimal-pro": ("deepseek-pro", False),
    "full-flash": ("deepseek", True),
}
JUDGES = (("deepseek", "DeepSeek Flash"), ("zhipu", "GLM-4 Flash"))
COMPARISONS = (
    ("stack_effect", "full-pro", "minimal-pro"),
    ("model_effect", "full-pro", "full-flash"),
)
METRICS = ("natural", "understanding", "specific", "non_template", "restraint", "overall")
BANNED = (
    "会好起来的", "都会过去的", "你要相信自己", "我理解你的感受",
    "多喝热水", "你不是一个人", "我永远", "加油",
)

JUDGE_SYSTEM = """你是对话质量的盲评员。你会看到一段对话历史、用户最后说的话，以及两个候选回复 A/B。
按 1-5 分分别评价：
natural：像具体的人在自然说话，不像模板或客服。
understanding：准确接住用户的具体内容与情绪，不空泛，不把自己的猜测当成事实。
specific：给出具体理解、关键判断，或一个可执行的下一步；不是只说“我听到了”。
non_template：没有固定骨架、套话、机械追问、无脑附和。
restraint：长度和留白合适；不一次倒完，不连续追问，不说教，不越界承诺。过长、清单倾倒、连续问句要扣分。
overall：综合自然度、理解、具体性、克制与对话推进。

重要规则：
1. 不要因为回复更长、用了 Markdown、或“看起来更专业”就加分。
2. 只依据对话历史与用户最后一句话评分。
3. 两个回复几乎无差别时才选 tie；有可说明的差异就选 A 或 B。
4. 只输出一行 JSON，不要代码块：
{"A":{"natural":1,"understanding":1,"specific":1,"non_template":1,"restraint":1,"overall":1},"B":{"natural":1,"understanding":1,"specific":1,"non_template":1,"restraint":1,"overall":1},"winner":"A","reason":"不超过40字"}
"""


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load_cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"no cases in {path}")
    return cases


def history_messages(case: dict) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for item in case.get("history") or []:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content})
    return history


def handbook_block(text: str) -> str | None:
    try:
        if not kb.should_offer_methods(text):
            return None
        handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)
        return kb.build_method_block(kb.search(handbook, text))
    except Exception:
        return None


def full_messages(case: dict) -> tuple[list[dict[str, str]], dict]:
    text = str(case["text"])
    decision = role_router.route_role(text, snapshot=None, safety_level="none")
    role_id = getattr(decision, "role_id", None) or roles.DEFAULT_ROLE_ID
    role_instruction = roles.build_role_instruction(role_id)
    knowledge_block = handbook_block(text)
    scaffold_block = scaffold.build_scaffold(text)
    history = history_messages(case) + [{"role": "user", "content": text}]
    messages = persona.assemble_messages(
        history,
        role_instruction=role_instruction,
        knowledge_block=knowledge_block,
        scaffold_instruction=scaffold_block,
    )
    meta = {
        "role_id": role_id,
        "role_reason": getattr(decision, "reason", ""),
        "knowledge": bool(knowledge_block),
        "scaffold": scaffold_block,
    }
    return messages, meta


def minimal_messages(case: dict) -> tuple[list[dict[str, str]], dict]:
    history = history_messages(case) + [{"role": "user", "content": str(case["text"])}]
    return [{"role": "system", "content": MINIMAL_PROMPT}] + history, {}


def messages_for(case: dict, arm: str) -> tuple[list[dict[str, str]], dict]:
    return full_messages(case) if ARM_CONFIG[arm][1] else minimal_messages(case)


def reply_metrics(reply: str) -> dict:
    chars = len(reply)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", reply) if part.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [part.strip() for part in reply.splitlines() if part.strip()]
    return {
        "chars": chars,
        "paragraphs": len(paragraphs),
        "questions": reply.count("？") + reply.count("?"),
        "banned_hits": [phrase for phrase in BANNED if phrase in reply],
        "markdown": bool(re.search(r"(\*\*|^[-*]\s|^#+\s)", reply, re.M)),
        "bucket": "short" if chars < 90 else "medium" if chars < 200 else "long",
    }


def invoke_with_retry(engine: str, messages: list[dict[str, str]], *, attempts: int = 4):
    last_error = ""
    started = time.time()
    for attempt in range(attempts):
        try:
            model = con.make_model(engine, temperature=0.7, streaming=False)
            response = model.invoke(messages)
            text = str(getattr(response, "content", response) or "").strip()
            if text:
                return text, "", round(time.time() - started, 3)
            last_error = "empty-reply"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(8.0, 1.2 * (2 ** attempt)) + random.random())
    return "", last_error, round(time.time() - started, 3)


def generate_one(case: dict, arm: str) -> dict:
    engine, _ = ARM_CONFIG[arm]
    messages, meta = messages_for(case, arm)
    reply, error, elapsed = invoke_with_retry(engine, messages)
    item = {
        "arm": arm,
        "engine": engine,
        "reply": reply,
        "error": error,
        "elapsed_s": elapsed,
        "meta": meta,
    }
    item.update(reply_metrics(reply))
    return item


def checkpoint(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def run_generation(cases: list[dict], tag: str, workers: int) -> Path:
    output = ROOT / "data/eval" / f"emotional30_raw_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "cases_file": str(CASES_PATH),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "arms": {
            arm: {"engine": ARM_CONFIG[arm][0], "full_stack": ARM_CONFIG[arm][1]}
            for arm in ARMS
        },
        "cases": [],
    }
    for index, case in enumerate(cases, 1):
        base = {key: value for key, value in case.items() if key != "history"}
        base["history"] = history_messages(case)
        with ThreadPoolExecutor(max_workers=min(workers, len(ARMS))) as pool:
            futures = {pool.submit(generate_one, case, arm): arm for arm in ARMS}
            results = {}
            for future in as_completed(futures):
                arm = futures[future]
                try:
                    results[arm] = future.result()
                except Exception as exc:
                    results[arm] = {
                        "arm": arm,
                        "engine": ARM_CONFIG[arm][0],
                        "reply": "",
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_s": 0,
                        "meta": {},
                        **reply_metrics(""),
                    }
        base["arms"] = {arm: results[arm] for arm in ARMS}
        document["cases"].append(base)
        checkpoint(output, document)
        brief = " | ".join(
            f"{arm}:{results[arm]['chars']}字/{results[arm]['elapsed_s']}s"
            + (" ERR" if results[arm]["error"] else "")
            for arm in ARMS
        )
        print(f"[{index:02d}/{len(cases)}] {case['id']}  {brief}", flush=True)
    print("saved", output, flush=True)
    return output


def parse_json(text: str):
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def normalize_scores(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        scores = {metric: float(value.get(metric, 0) or 0) for metric in METRICS}
    except (TypeError, ValueError):
        return None
    if any(score < 1 or score > 5 for score in scores.values()):
        return None
    return scores


def format_context(case: dict) -> str:
    lines = []
    for item in history_messages(case):
        label = "用户" if item["role"] == "user" else "助手"
        lines.append(f"{label}：{item['content']}")
    lines.append(f"用户：{case['text']}")
    return "\n".join(lines)


def judge_once(engine: str, case: dict, first: str, second: str):
    prompt = (
        f"对话历史与用户最后一句话：\n{format_context(case)}\n\n"
        f"候选 A：\n{first}\n\n候选 B：\n{second}\n\n"
        "请分别评分并输出指定的一行 JSON。"
    )
    last_error = ""
    for attempt in range(4):
        try:
            model = con.make_model(engine, temperature=0.0, streaming=False)
            response = model.invoke(
                [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ]
            )
            parsed = parse_json(str(getattr(response, "content", response) or ""))
            scores_a = normalize_scores((parsed or {}).get("A"))
            scores_b = normalize_scores((parsed or {}).get("B"))
            if scores_a and scores_b:
                return parsed, scores_a, scores_b, ""
            last_error = "incomplete-json"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(8.0, 1.0 * (2 ** attempt)) + random.random())
    return None, None, None, last_error


def judge_vote(comparison: str, arm_a: str, arm_b: str, case: dict, judge: str) -> list[dict]:
    arm_data = case["arms"]
    replies = {arm_a: arm_data[arm_a]["reply"], arm_b: arm_data[arm_b]["reply"]}
    if not replies[arm_a] or not replies[arm_b]:
        return [{
            "comparison": comparison, "case": case["id"], "judge": judge,
            "error": "missing-candidate-reply",
        }]
    rows = []
    for order in ((arm_a, arm_b), (arm_b, arm_a)):
        parsed, scores_first, scores_second, error = judge_once(
            judge, case, replies[order[0]], replies[order[1]]
        )
        row = {
            "comparison": comparison, "case": case["id"],
            "category": case["category"], "judge": judge, "order": list(order),
        }
        if not parsed:
            row["error"] = error or "no-json"
            rows.append(row)
            continue
        verdict = str(parsed.get("winner", "")).strip().upper()
        winner = "tie"
        if verdict.startswith("A"):
            winner = order[0]
        elif verdict.startswith("B"):
            winner = order[1]
        row["winner"] = winner
        row["scores_by_arm"] = {order[0]: scores_first, order[1]: scores_second}
        row["reason"] = str(parsed.get("reason", ""))[:120]
        rows.append(row)
    return rows


def mean_scores_for(votes: list[dict], arm: str) -> dict:
    samples = collections.defaultdict(list)
    for vote in votes:
        scores = (vote.get("scores_by_arm") or {}).get(arm)
        if not scores:
            continue
        for metric in METRICS:
            samples[metric].append(scores[metric])
    return {
        metric: round(statistics.mean(values), 3) if values else None
        for metric, values in samples.items()
    }


def bootstrap_ci(values: list[float], *, rounds: int = 2000, seed: int = 42):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = [
        statistics.mean(rng.choice(values) for _ in values)
        for _ in range(rounds)
    ]
    means.sort()
    return [round(means[int(rounds * 0.025)], 3), round(means[int(rounds * 0.975)], 3)]


def summarize(raw: dict, votes: list[dict], comparisons) -> dict:
    cases = raw["cases"]
    base = {}
    for arm in ARMS:
        items = [case["arms"][arm] for case in cases]
        valid = [item for item in items if item.get("reply")]
        base[arm] = {
            "valid": len(valid),
            "errors": len(items) - len(valid),
            "mean_chars": round(statistics.mean([item["chars"] for item in valid]), 1) if valid else None,
            "mean_paragraphs": round(statistics.mean([item["paragraphs"] for item in valid]), 2) if valid else None,
            "mean_questions": round(statistics.mean([item["questions"] for item in valid]), 2) if valid else None,
            "banned_rate": round(sum(bool(item["banned_hits"]) for item in valid) / len(valid), 3) if valid else None,
            "mean_elapsed_s": round(statistics.mean([item["elapsed_s"] for item in valid]), 1) if valid else None,
        }

    comparison_summaries = {}
    for label, arm_a, arm_b in comparisons:
        rows = [row for row in votes if row.get("comparison") == label and "winner" in row]
        wins = collections.Counter(row["winner"] for row in rows)
        by_judge = collections.defaultdict(collections.Counter)
        by_category = collections.defaultdict(collections.Counter)
        per_case_delta = collections.defaultdict(list)
        for row in rows:
            by_judge[row["judge"]][row["winner"]] += 1
            by_category[row.get("category", "")][row["winner"]] += 1
            scores = row.get("scores_by_arm") or {}
            if scores.get(arm_a) and scores.get(arm_b):
                per_case_delta[row["case"]].append(
                    scores[arm_a]["overall"] - scores[arm_b]["overall"]
                )
        deltas = [statistics.mean(values) for values in per_case_delta.values() if values]
        decisive = wins.get(arm_a, 0) + wins.get(arm_b, 0)
        comparison_summaries[label] = {
            "arms": [arm_a, arm_b],
            "votes": len(rows),
            "wins": {
                arm_a: wins.get(arm_a, 0),
                arm_b: wins.get(arm_b, 0),
                "tie": wins.get("tie", 0),
            },
            "win_rate_excluding_ties": {
                arm_a: round(wins.get(arm_a, 0) / max(1, decisive), 3),
                arm_b: round(wins.get(arm_b, 0) / max(1, decisive), 3),
            },
            "mean_scores": {arm: mean_scores_for(rows, arm) for arm in (arm_a, arm_b)},
            "mean_overall_delta": round(statistics.mean(deltas), 3) if deltas else None,
            "overall_delta_ci95": bootstrap_ci(deltas),
            "by_judge": {judge: dict(counts) for judge, counts in by_judge.items()},
            "by_category": {
                category: dict(counts)
                for category, counts in sorted(by_category.items())
            },
            "invalid_votes": sum(
                1 for row in votes
                if row.get("comparison") == label and "winner" not in row
            ),
        }
    return {
        "cases": len(cases),
        "arm_summary": base,
        "comparisons": comparison_summaries,
        "judge_votes": len([row for row in votes if "winner" in row]),
        "invalid_votes": len([row for row in votes if "winner" not in row]),
    }


def write_markdown(path: Path, raw: dict, summary: dict) -> None:
    lines = [
        "# 30 条情绪对话盲评",
        "",
        f"- 生成时间：{raw.get('generated_at', '')}",
        f"- 用例数：{summary['cases']}",
        f"- 有效评委票：{summary['judge_votes']}；无效：{summary['invalid_votes']}",
        "",
        "## 客观指标",
        "",
        "| 组 | 有效回复 | 错误 | 平均字数 | 平均段数 | 平均问号 | 禁用套话率 | 平均耗时(s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        row = summary["arm_summary"][arm]
        lines.append(
            f"| {arm} | {row['valid']} | {row['errors']} | {row['mean_chars']} | "
            f"{row['mean_paragraphs']} | {row['mean_questions']} | "
            f"{row['banned_rate']} | {row['mean_elapsed_s']} |"
        )
    lines += ["", "## 盲评结果", ""]
    for label, data in summary["comparisons"].items():
        arm_a, arm_b = data["arms"]
        lines += [
            f"### {label}: {arm_a} vs {arm_b}",
            "",
            f"- 票数：{data['wins']}",
            f"- 去平局胜率：{arm_a} {data['win_rate_excluding_ties'][arm_a]:.1%} "
            f"vs {arm_b} {data['win_rate_excluding_ties'][arm_b]:.1%}",
            f"- overall 均分差（{arm_a} - {arm_b}）："
            f"{data['mean_overall_delta']}，95% CI {data['overall_delta_ci95']}",
        ]
        for arm in (arm_a, arm_b):
            lines.append(f"- {arm} 均分：{data['mean_scores'][arm]}")
        lines.append("")

    examples = []
    for case in raw["cases"]:
        replies = case["arms"]
        if not all(replies[arm]["reply"] for arm in ("full-pro", "minimal-pro")):
            continue
        delta = abs(replies["full-pro"]["chars"] - replies["minimal-pro"]["chars"])
        examples.append((delta, case["id"], case["text"], replies))
    examples.sort(reverse=True, key=lambda item: item[0])
    lines += ["## 典型差异", ""]
    for _, case_id, text, replies in examples[:5]:
        lines += [f"### {case_id}", "", f"用户：{text}", ""]
        for arm in ("full-pro", "minimal-pro", "full-flash"):
            item = replies[arm]
            lines += [
                f"**{arm}**（{item['chars']} 字，{item['questions']} 问）",
                "",
                item["reply"] or f"[生成失败] {item['error']}",
                "",
            ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_judging(raw_path: Path, tag: str, workers: int, judges):
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    cases = raw["cases"]
    tasks = []
    for label, arm_a, arm_b in COMPARISONS:
        for case in cases:
            for engine, _ in judges:
                tasks.append((label, arm_a, arm_b, case, engine))
    votes = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(judge_vote, label, arm_a, arm_b, case, engine):
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
    summary = summarize(raw, votes, COMPARISONS)
    output = ROOT / "data/eval" / f"emotional30_judged_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "raw_file": str(raw_path),
        "judged_at": datetime.now().isoformat(timespec="seconds"),
        "judges": [{"engine": engine, "label": label} for engine, label in judges],
        "summary": summary,
        "votes": votes,
    }
    checkpoint(output, document)
    report = ROOT / "reports" / f"emotional30_{tag}_{now_tag()}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(report, raw, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("saved", output, flush=True)
    print("report", report, flush=True)
    return output, report, summary


def main() -> None:
    global CASES_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("generate", "judge", "all"), default="all")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--raw", default="", help="existing raw JSON for --stage judge")
    parser.add_argument("--tag", default="emotional30")
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
        run_judging(raw_path, args.tag, args.judge_workers, JUDGES)


if __name__ == "__main__":
    main()
