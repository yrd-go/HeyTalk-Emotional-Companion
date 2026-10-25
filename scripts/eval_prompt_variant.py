# -*- coding: utf-8 -*-
"""Generate replies for one prompt variant on a fixed case set."""
from __future__ import annotations

import argparse
import json
import re
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
from chat import scaffold as scaffold_layer

DEFAULT_CASES = ROOT / "data/eval/ab_prompt_human-base_20261018-120730.json"
TEMPLATE_HITS = ("先分清", "你是哪一种", "这个阶段", "真正的", "优先级的改变")


def classify(length: int) -> str:
    return "short" if length < 90 else "medium" if length < 180 else "long"


def make_metrics(reply: str):
    return {
        "chars": len(reply),
        "bucket": classify(len(reply)),
        "asks_question": "？" in reply or "?" in reply,
        "template_hits": [hit for hit in TEMPLATE_HITS if hit in reply],
        "contrastive_hits": len(re.findall(r"不是.{0,18}而是|真正的.{0,18}是", reply)),
        "opening": reply[:16],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--engine", default="deepseek-pro")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--out", default="")
    parser.add_argument("--scaffold", action="store_true",
                        help="按每个 case 的文本动态追加本轮支架")
    args = parser.parse_args()

    prompt_path = Path(args.prompt)
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    cases_doc = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    model = con.make_model(args.engine, temperature=0.7, streaming=False)
    results = []

    for case in cases_doc["cases"]:
        scaffold_block = scaffold_layer.build_scaffold(case["text"]) if args.scaffold else None
        system_content = prompt
        if scaffold_block:
            system_content = f"{prompt}\n\n{scaffold_block}"
        reply = ""
        error = None
        for attempt in range(3):
            try:
                response = model.invoke(
                    [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": case["text"]},
                    ]
                )
                reply = str(getattr(response, "content", response) or "").strip()
                if reply:
                    break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.6 * (attempt + 1))

        item = {
            "id": case["id"],
            "expect": case.get("expect"),
            "text": case["text"],
            "reply": reply,
            "error": error,
            "scaffold": scaffold_block,
        }
        item.update(make_metrics(reply))
        results.append(item)
        print(f"  {case['id']}: {item['chars']} chars", flush=True)

    output = Path(args.out) if args.out else ROOT / "data/eval" / (
        f"ab_prompt_{args.tag}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    document = {
        "tag": args.tag,
        "engine": args.engine,
        "prompt_chars": len(prompt),
        "prompt_source": str(prompt_path),
        "scaffold_enabled": bool(args.scaffold),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": results,
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", output)


if __name__ == "__main__":
    main()
