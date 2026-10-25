# -*- coding: utf-8 -*-
"""Verify the patched persona against the saved pre-patch prompt."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import eval_human_texture as ext
from chat import persona

OLD_PROMPT = ROOT / "data/eval/prompt_before_human_texture_20261019.txt"
CASES = ROOT / "data/eval/human_texture_12_20261018.json"
FOCUS = {
    "tired_after_cant_move", "tired", "joke", "no_advice",
    "ferrari", "m_selfworth_ferrari",
}


def main() -> None:
    old = OLD_PROMPT.read_text(encoding="utf-8")
    new = persona.SYSTEM_PROMPT
    if not old.strip():
        raise SystemExit("saved baseline prompt is empty")

    ext.PROMPTS = {"previous": old, "final": new}
    ext.VARIANTS = ("previous", "final")
    ext.COMPARISONS = (("final_vs_previous", "final", "previous"),)
    ext.CASES_PATH = CASES
    cases = [case for case in ext.load_cases(CASES) if case["id"] in FOCUS]
    raw = ext.run_generation(cases, "finalcheck", 3)
    ext.run_judging(raw, "finalcheck", 2)


if __name__ == "__main__":
    main()
