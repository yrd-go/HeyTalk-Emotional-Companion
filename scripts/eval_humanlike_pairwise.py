# -*- coding: utf-8 -*-
"""Blind pairwise evaluation: current prompt vs humanized candidate."""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys
import time
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

from paths import load_env
load_env()
from chat import conversation as con

BASELINE = ROOT / "data/eval/ab_prompt_human-base_20261018-120730.json"
CANDIDATE = ROOT / "data/eval/ab_prompt_human-candidate-v2-full_20261018-121732.json"
if "--baseline" in sys.argv:
    BASELINE = Path(sys.argv[sys.argv.index("--baseline") + 1])
if "--candidate" in sys.argv:
    CANDIDATE = Path(sys.argv[sys.argv.index("--candidate") + 1])
RUN_TAG = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "pairwise"
JUDGES = [("deepseek", "DeepSeek Flash"), ("zhipu", "GLM-4 Flash")]
METRICS = ("natural", "understanding", "specific", "non_template", "overall")

JUDGE_SYSTEM = """你是对话质量的盲评员。根据用户原话，分别评价两条回复。
不要猜模型身份，不要因为回复更长或用了 Markdown 就加分。按 1-5 分评价：
natural：像具体的人在自然说话，不像模板或客服。
understanding：准确接住用户的具体内容与情绪，不空泛。
specific：提供了具体理解、判断或可执行的下一步。
non_template：没有反复使用固定骨架、套话或机械追问。
overall：综合自然度、理解、具体性和对话推进。
必须分别给 A、B 两条回复打分。最后选 overall 更高的一条；只有两条几乎无差别时才选 tie。
只输出一行 JSON：
{"A":{"natural":4,"understanding":4,"specific":4,"non_template":4,"overall":4},"B":{"natural":4,"understanding":4,"specific":4,"non_template":4,"overall":4},"winner":"A","reason":"不超过30字"}
"""


def parse_json(text: str):
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def normalize_scores(value):
    if not isinstance(value, dict):
        return None
    try:
        scores = {metric: float(value.get(metric, 0) or 0) for metric in METRICS}
    except (TypeError, ValueError):
        return None
    if any(score < 1 or score > 5 for score in scores.values()):
        return None
    return scores


def judge_once(model, user: str, first: str, second: str):
    prompt = (
        f"用户原话：\n{user}\n\n候选 A：\n{first}\n\n候选 B：\n{second}\n\n"
        "请分别给 A、B 打分并输出 JSON。"
    )
    response = model.invoke(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    )
    return parse_json(str(getattr(response, "content", response) or ""))


def main():
    baseline_doc = json.loads(BASELINE.read_text(encoding="utf-8"))
    candidate_doc = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    baseline_by = {case["id"]: case for case in baseline_doc["cases"]}
    candidate_by = {case["id"]: case for case in candidate_doc["cases"]}
    case_ids = [case_id for case_id in baseline_by if case_id in candidate_by]
    raw = []

    for engine, label in JUDGES:
        try:
            judge = con.make_model(engine, temperature=0.0, streaming=False)
        except Exception as exc:
            print(f"judge {label} unavailable: {type(exc).__name__}: {exc}", flush=True)
            continue

        for case_id in case_ids:
            user = baseline_by[case_id]["text"]
            texts = {
                "baseline": baseline_by[case_id]["reply"],
                "candidate": candidate_by[case_id]["reply"],
            }
            for order in (("baseline", "candidate"), ("candidate", "baseline")):
                parsed = None
                error = None
                for attempt in range(3):
                    try:
                        parsed = judge_once(judge, user, texts[order[0]], texts[order[1]])
                        scores_a = normalize_scores((parsed or {}).get("A"))
                        scores_b = normalize_scores((parsed or {}).get("B"))
                        if scores_a and scores_b:
                            break
                        error = "incomplete-json"
                        parsed = None
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    time.sleep(0.4 * (attempt + 1))

                if not parsed:
                    raw.append(
                        {
                            "judge": engine,
                            "judge_label": label,
                            "case": case_id,
                            "order": list(order),
                            "error": error or "no-json",
                        }
                    )
                    continue

                verdict = str(parsed.get("winner", "")).strip().upper()
                actual_winner = "tie"
                if verdict == "A":
                    actual_winner = order[0]
                elif verdict == "B":
                    actual_winner = order[1]

                raw.append(
                    {
                        "judge": engine,
                        "judge_label": label,
                        "case": case_id,
                        "order": list(order),
                        "scores_by_candidate": {
                            order[0]: scores_a,
                            order[1]: scores_b,
                        },
                        "winner": actual_winner,
                        "reason": parsed.get("reason", ""),
                    }
                )
            print(f"  {label} {case_id}: done", flush=True)

    wins = collections.Counter()
    wins_by_judge = collections.defaultdict(collections.Counter)
    verdicts_by_case = collections.defaultdict(collections.Counter)
    score_samples = collections.defaultdict(lambda: collections.defaultdict(list))
    votes = 0

    for row in raw:
        if "winner" not in row:
            continue
        votes += 1
        winner = row["winner"]
        wins[winner] += 1
        wins_by_judge[row["judge"]][winner] += 1
        verdicts_by_case[row["case"]][winner] += 1
        for candidate, scores in row["scores_by_candidate"].items():
            if not scores:
                continue
            for metric in METRICS:
                score_samples[(row["judge"], candidate)][metric].append(scores[metric])
                score_samples[("all", candidate)][metric].append(scores[metric])

    mean_scores = {
        f"{judge}/{candidate}": {
            metric: round(statistics.mean(values), 3)
            for metric, values in metrics.items()
        }
        for (judge, candidate), metrics in sorted(score_samples.items())
    }

    summary = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_file": str(BASELINE),
        "candidate_file": str(CANDIDATE),
        "cases": len(case_ids),
        "votes": votes,
        "wins": dict(wins),
        "wins_by_judge": {judge: dict(counts) for judge, counts in wins_by_judge.items()},
        "verdicts_by_case": {case: dict(counts) for case, counts in verdicts_by_case.items()},
        "mean_scores": mean_scores,
        "valid_raw": sum(1 for row in raw if row.get("scores_by_candidate")),
        "invalid_raw": sum(1 for row in raw if not row.get("scores_by_candidate")),
    }

    output = ROOT / "data/eval" / (
        f"humanlike_{RUN_TAG}_" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    )
    output.write_text(
        json.dumps({"summary": summary, "raw": raw}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("saved", output)


if __name__ == "__main__":
    main()
