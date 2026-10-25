# -*- coding: utf-8 -*-
"""Evaluate session-local continuity planning without changing the base prompt.

Arms:
  current       DeepSeek Pro, no planner
  planner       DeepSeek Pro, with planner instruction when eligible
  flash_planner DeepSeek Flash, same planner path

Judging reuses the audited blind protocol from eval_human_texture.py:
two judges, both A/B orders, seven quality metrics.
"""
from __future__ import annotations

import argparse
import json
import sys
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
import eval_human_texture as ext
from chat import dialogue_planner, persona, role_router, roles, scaffold

CASES_PATH = ROOT / "data/eval/dialogue_planner_8_20261019.json"
OVERLAY_PATH = ROOT / "data/eval/texture_wechat.txt"
ARMS = (
    ("current", "deepseek-pro", False),
    ("planner", "deepseek-pro", True),
    ("flash_current", "deepseek", False),
)
VARIANTS = tuple(arm for arm, _, _ in ARMS)
COMPARISONS = (
    ("planner_vs_current", "planner", "current"),
    ("flash_vs_pro_current", "flash_current", "current"),
)

EDITOR_SYSTEM = """你是中文对话的“去咨询腔”编辑器。你会看到一段对话和一条待改写回复。

任务：把回复改得像真人在微信里说话，但不能丢掉原文最重要的信息。

必须保留：
1. 原文里的关键具体信息：数字、具体事件、用户刚用过的词。
2. 如果原文把当前句和更早的感受连在一起，保留这条连接；用自然的一句话带过，不要写“你之前说过”。
3. 真正有用的问题最多保留一个。

必须删除：
1. 心理总结、格言、解释机制的长句。
2. “这种时候人容易…”“这感觉一上来…”“不是X而是Y”这类分析腔。
3. 只为了显得会聊天而追加的第二个问题。

不要新增事实，不诊断，不说教。多数 2–3 句；原文很短时可以更短，但不能只剩空泛安慰。
只输出改写后的回复，不要解释，不要加引号。"""

ext.VARIANTS = VARIANTS
ext.COMPARISONS = COMPARISONS
ext.CASES_PATH = CASES_PATH


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load_cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"no cases in {path}")
    return cases


def messages_for(case: dict, *, with_planner: bool) -> tuple[list[dict], dict]:
    text = str(case["text"])
    history = base_eval.history_messages(case)
    decision = role_router.route_role(text, snapshot=None, safety_level="none")
    role_id = getattr(decision, "role_id", None) or roles.DEFAULT_ROLE_ID
    role_instruction = roles.build_role_instruction(role_id)
    knowledge_block = base_eval.handbook_block(text)
    scaffold_block = scaffold.build_scaffold(text)
    plan = dialogue_planner.build_turn_plan(text, history) if with_planner else None

    parts = [persona.SYSTEM_PROMPT, role_instruction]
    if knowledge_block:
        parts.append(knowledge_block)
    if plan is not None:
        parts.append(plan.to_instruction())
    if scaffold_block:
        parts.append(scaffold_block)

    messages = [{"role": "system", "content": "\n\n".join(parts)}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    meta = {
        "role_id": role_id,
        "role_reason": getattr(decision, "reason", ""),
        "knowledge": bool(knowledge_block),
        "scaffold": scaffold_block,
        "plan_signals": list(plan.signals) if plan is not None else [],
        "plan_instruction": plan.to_instruction() if plan is not None else "",
    }
    return messages, meta


def generate_one(case: dict, arm: str, engine: str, with_planner: bool) -> dict:
    messages, meta = messages_for(case, with_planner=with_planner)
    reply, error, elapsed = base_eval.invoke_with_retry(engine, messages)
    item = {
        "arm": arm,
        "engine": engine,
        "reply": reply,
        "error": error,
        "elapsed_s": elapsed,
        "meta": meta,
    }
    item.update(base_eval.reply_metrics(reply))
    return item


def assert_all_planner_cases_trigger(cases: list[dict]) -> None:
    missing = []
    for case in cases:
        history = base_eval.history_messages(case)
        plan = dialogue_planner.build_turn_plan(str(case["text"]), history)
        if plan is None:
            missing.append(str(case.get("id") or "<no-id>"))
    if missing:
        raise SystemExit("planner did not trigger for: " + ", ".join(missing))


def run_generation(cases: list[dict], tag: str, workers: int) -> Path:
    output = ROOT / "data/eval" / f"dialogue_planner_raw_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases_file": str(CASES_PATH),
        "prompt_chars": {"base": len(persona.SYSTEM_PROMPT)},
        "arms": {
            arm: {"engine": engine, "planner": with_planner}
            for arm, engine, with_planner in ARMS
        },
        "cases": [],
    }
    for index, case in enumerate(cases, 1):
        base = {key: value for key, value in case.items() if key != "history"}
        base["history"] = base_eval.history_messages(case)
        with ThreadPoolExecutor(max_workers=min(workers, len(ARMS))) as pool:
            futures = {
                pool.submit(generate_one, case, arm, engine, with_planner): arm
                for arm, engine, with_planner in ARMS
            }
            results = {}
            for future in as_completed(futures):
                arm = futures[future]
                try:
                    results[arm] = future.result()
                except Exception as exc:
                    engine = next(e for name, e, _ in ARMS if name == arm)
                    results[arm] = {
                        "arm": arm,
                        "engine": engine,
                        "reply": "",
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_s": 0,
                        "meta": {},
                        **base_eval.reply_metrics(""),
                    }
        base["arms"] = {arm: results[arm] for arm in VARIANTS}
        document["cases"].append(base)
        base_eval.checkpoint(output, document)
        brief = " | ".join(
            f"{arm}:{results[arm]['chars']}字/{results[arm]['elapsed_s']}s"
            + (" ERR" if results[arm]["error"] else "")
            for arm in VARIANTS
        )
        print(f"[{index:02d}/{len(cases)}] {case['id']}  {brief}", flush=True)
    print("saved", output, flush=True)
    return output


def editor_messages(case: dict, draft: str) -> list[dict]:
    messages = [{"role": "system", "content": EDITOR_SYSTEM}]
    messages.extend(base_eval.history_messages(case))
    messages.append({"role": "user", "content": str(case["text"])})
    messages.append({"role": "assistant", "content": str(draft)})
    messages.append({
        "role": "user",
        "content": "请把上一条回复改写得自然、像真人，只输出最终回复。",
    })
    return messages


def run_editor(raw_path: Path, tag: str, workers: int) -> Path:
    source = json.loads(raw_path.read_text(encoding="utf-8"))
    output = ROOT / "data/eval" / f"reply_editor_raw_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_raw": str(raw_path),
        "prompt_chars": {"base": len(persona.SYSTEM_PROMPT)},
        "cases": [],
    }

    def edit_case(case: dict) -> dict:
        draft = str(case["arms"]["current"].get("reply") or "")
        if not draft:
            raise ValueError(f"empty draft for {case.get('id')}")
        edited, error, elapsed = base_eval.invoke_with_retry(
            "deepseek", editor_messages(case, draft)
        )
        item = {
            "arm": "edited",
            "engine": "deepseek",
            "reply": edited,
            "error": error,
            "elapsed_s": elapsed,
            "meta": {"source_arm": "current", "editor": "deepseek-flash"},
        }
        item.update(base_eval.reply_metrics(edited))
        return item

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(edit_case, case): case for case in source["cases"]}
        edited_by_id = {}
        for done, future in enumerate(as_completed(futures), 1):
            case = futures[future]
            try:
                edited_by_id[str(case["id"])] = future.result()
            except Exception as exc:
                edited_by_id[str(case["id"])] = {
                    "arm": "edited",
                    "engine": "deepseek",
                    "reply": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": 0,
                    "meta": {"source_arm": "current", "editor": "deepseek-flash"},
                    **base_eval.reply_metrics(""),
                }
            print(f"[editor {done:02d}/{len(futures)}] {case['id']}", flush=True)

    for case in source["cases"]:
        base = {key: value for key, value in case.items() if key != "history"}
        base["history"] = case.get("history") or []
        base["arms"] = {
            "current": case["arms"]["current"],
            "edited": edited_by_id[str(case["id"])],
        }
        document["cases"].append(base)
    base_eval.checkpoint(output, document)
    print("saved", output, flush=True)
    return output


def overlay_messages(case: dict) -> list[dict]:
    text = str(case["text"])
    history = base_eval.history_messages(case)
    overlay = OVERLAY_PATH.read_text(encoding="utf-8")
    prompt = ext.insert_overlay(persona.SYSTEM_PROMPT, overlay)
    decision = role_router.route_role(text, snapshot=None, safety_level="none")
    role_id = getattr(decision, "role_id", None) or roles.DEFAULT_ROLE_ID
    role_instruction = roles.build_role_instruction(role_id)
    knowledge_block = base_eval.handbook_block(text)
    scaffold_block = scaffold.build_scaffold(text)
    parts = [prompt, role_instruction]
    if knowledge_block:
        parts.append(knowledge_block)
    if scaffold_block:
        parts.append(scaffold_block)
    messages = [{"role": "system", "content": "\n\n".join(parts)}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    return messages


def run_overlay(raw_path: Path, tag: str, workers: int) -> Path:
    source = json.loads(raw_path.read_text(encoding="utf-8"))
    output = ROOT / "data/eval" / f"prompt_overlay_raw_{tag}_{now_tag()}.json"
    document = {
        "tag": tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_raw": str(raw_path),
        "prompt_chars": {
            "base": len(persona.SYSTEM_PROMPT),
            "overlay": len(ext.insert_overlay(
                persona.SYSTEM_PROMPT,
                OVERLAY_PATH.read_text(encoding="utf-8"),
            )),
        },
        "cases": [],
    }

    def generate_overlay(case: dict) -> dict:
        messages = overlay_messages(case)
        reply, error, elapsed = base_eval.invoke_with_retry("deepseek-pro", messages)
        item = {
            "arm": "wechat",
            "engine": "deepseek-pro",
            "reply": reply,
            "error": error,
            "elapsed_s": elapsed,
            "meta": {"overlay": str(OVERLAY_PATH)},
        }
        item.update(base_eval.reply_metrics(reply))
        return item

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(generate_overlay, case): case for case in source["cases"]}
        results = {}
        for done, future in enumerate(as_completed(futures), 1):
            case = futures[future]
            try:
                results[str(case["id"])] = future.result()
            except Exception as exc:
                results[str(case["id"])] = {
                    "arm": "wechat",
                    "engine": "deepseek-pro",
                    "reply": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": 0,
                    "meta": {"overlay": str(OVERLAY_PATH)},
                    **base_eval.reply_metrics(""),
                }
            print(f"[overlay {done:02d}/{len(futures)}] {case['id']}", flush=True)

    for case in source["cases"]:
        base = {key: value for key, value in case.items() if key != "history"}
        base["history"] = case.get("history") or []
        base["arms"] = {
            "current": case["arms"]["current"],
            "wechat": results[str(case["id"])],
        }
        document["cases"].append(base)
    base_eval.checkpoint(output, document)
    print("saved", output, flush=True)
    return output


def main() -> None:
    global CASES_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage", choices=("generate", "judge", "editor", "overlay", "all"), default="all"
    )
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--raw", default="")
    parser.add_argument("--tag", default="dialogue_planner")
    parser.add_argument("--gen-workers", type=int, default=3)
    parser.add_argument("--judge-workers", type=int, default=4)
    args = parser.parse_args()

    CASES_PATH = Path(args.cases)
    ext.CASES_PATH = CASES_PATH
    cases = load_cases(CASES_PATH)
    assert_all_planner_cases_trigger(cases)

    raw_path = Path(args.raw) if args.raw else None
    if args.stage == "editor":
        if raw_path is None:
            raise SystemExit("--stage editor requires --raw")
        edited_raw = run_editor(raw_path, args.tag, args.gen_workers)
        ext.VARIANTS = ("current", "edited")
        ext.COMPARISONS = (("edited_vs_current", "edited", "current"),)
        ext.run_judging(edited_raw, args.tag, args.judge_workers)
        return
    if args.stage == "overlay":
        if raw_path is None:
            raise SystemExit("--stage overlay requires --raw")
        overlay_raw = run_overlay(raw_path, args.tag, args.gen_workers)
        ext.VARIANTS = ("current", "wechat")
        ext.COMPARISONS = (("wechat_vs_current", "wechat", "current"),)
        ext.run_judging(overlay_raw, args.tag, args.judge_workers)
        return
    if args.stage in {"generate", "all"}:
        raw_path = run_generation(cases, args.tag, args.gen_workers)
    if args.stage in {"judge", "all"}:
        if raw_path is None:
            raise SystemExit("--stage judge requires --raw")
        ext.run_judging(raw_path, args.tag, args.judge_workers)


if __name__ == "__main__":
    main()
