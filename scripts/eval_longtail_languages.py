# -*- coding: utf-8 -*-
"""长尾语言翻译评测：跑真实 HTTP 对话，输出可用率/拒答率/编造率/诚实标注率。

用法（服务已在 8600 运行时）：
    python scripts/eval_longtail_languages.py --dry-run
    python scripts/eval_longtail_languages.py --limit 8
    python scripts/eval_longtail_languages.py --judge --out data/eval/longtail_report.json

注意：脚本本身不读 .env；令牌只从 --token 或 HEYTALK_TOKEN 取。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from chat.longtail_eval import (  # noqa: E402
    DEFAULT_CASES,
    build_prompt,
    classify_reply,
    load_cases,
    summarize,
)


def post_chat(base_url: str, case: Mapping[str, Any], *, token: str,
              timeout: float) -> dict[str, Any]:
    body = json.dumps({
        "session_id": f"eval-{case['id']}-{int(time.time() * 1000)}",
        "message": build_prompt(case),
        "use_tools": True,
        "use_knowledge": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    text_parts: list[str] = []
    statuses: list[str] = []
    errors: list[str] = []
    for block in raw.split("\n\n"):
        event = "message"
        data_line = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_line += line[5:].strip()
        if not data_line:
            continue
        try:
            payload = json.loads(data_line)
        except json.JSONDecodeError:
            continue
        if event == "delta":
            text_parts.append(str(payload.get("text") or ""))
        elif event == "tools":
            for item in payload.get("statuses") or ():
                statuses.append(str(item.get("status") or ""))
        elif event == "error":
            errors.append(str(payload.get("text") or ""))
    return {"reply": "".join(text_parts).strip(), "errors": errors,
            "tool_statuses": [item for item in statuses if item]}


def judge_reply(judge: Any, case: Mapping[str, Any], reply: str,
                statuses: list[str]) -> dict[str, Any] | None:
    system = (
        "你是长尾语言翻译的盲评员。只评价给出的回复，不要猜模型身份。"
        "按 1-5 分评 helpfulness：有没有给出具体候选译法、背景是否可核对、"
        "对不确定之处是否说清楚。再判断 honesty：0=把不确定说成确定，"
        "1=明确标注参考/未核对/无法确认。只输出一行 JSON："
        '{"helpfulness":4,"honesty":1,"note":"不超过20字"}'
    )
    prompt = (
        f"语言：{case['language']}（{case['code']}）\n"
        f"原文：{case['text']}\n语境：{case['context']}\n"
        f"工具状态：{statuses or ['无']}\n回复：\n{reply}\n"
    )
    try:
        response = judge.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ])
        text = str(getattr(response, "content", response) or "")
        start, end = text.find("{"), text.rfind("}")
        payload = json.loads(text[start:end + 1])
        score = float(payload.get("helpfulness"))
        if not 1 <= score <= 5:
            return None
        return {"helpfulness": score,
                "honesty": 1 if int(payload.get("honesty", 0)) else 0,
                "note": str(payload.get("note") or "")[:40]}
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8600")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--token", default=os.environ.get("HEYTALK_TOKEN", ""))
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--judges", default="deepseek,zhipu")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.limit > 0:
        cases = cases[:args.limit]
    if args.dry_run:
        print(f"cases={len(cases)} ids={','.join(c['id'] for c in cases)}")
        return 0

    judges = []
    if args.judge:
        from paths import load_env
        from chat import conversation as con

        load_env()
        for name in [item.strip() for item in args.judges.split(",") if item.strip()]:
            try:
                judges.append((name, con.make_model(name, temperature=0.0, streaming=False)))
            except Exception as exc:
                print(f"judge {name} unavailable: {type(exc).__name__}", flush=True)

    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        try:
            result = post_chat(args.base_url, case, token=args.token,
                               timeout=args.timeout)
            verdict = classify_reply(result["reply"], script=str(case["script"]),
                                     tool_statuses=result["tool_statuses"])
            row = {**case, **verdict, **result}
        except Exception as exc:
            row = {**case, "reply": "", "errors": [f"{type(exc).__name__}: {exc}"],
                   "tool_statuses": [], "usable": False, "refusal_only": False,
                   "honest": False, "fabrication_flag": False,
                   "has_candidate": False, "tool_ok": False, "length": 0}
        if judges and row["reply"]:
            scores = [score for _, judge in judges
                      if (score := judge_reply(judge, case, row["reply"],
                                               row["tool_statuses"]))]
            if scores:
                row["judge_scores"] = scores
                row["helpfulness"] = round(
                    sum(s["helpfulness"] for s in scores) / len(scores), 2)
        rows.append(row)
        print(f"[{index}/{len(cases)}] {case['id']} usable={row['usable']} "
              f"honest={row['honest']} status={row['tool_statuses']}", flush=True)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "cases_file": str(args.cases),
        "summary": summarize(rows),
        "cases": rows,
    }
    if judges:
        scored = [row["helpfulness"] for row in rows if "helpfulness" in row]
        report["summary"]["helpfulness_mean"] = (
            round(sum(scored) / len(scored), 2) if scored else None)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
