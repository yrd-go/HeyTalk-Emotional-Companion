# -*- coding: utf-8 -*-
"""D：长尾语言评测集与指标口径的回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from chat import longtail_eval as ev  # noqa: E402


class TestLongtailDataset(unittest.TestCase):
    def test_cases_are_valid_and_long_tail(self):
        cases = ev.load_cases()
        self.assertGreaterEqual(len(cases), 20)
        self.assertLessEqual(len(cases), 30)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertIn("mai", {case["code"] for case in cases})
        self.assertIn("sat", {case["code"] for case in cases})

    def test_prompt_carries_language_and_context(self):
        case = ev.load_cases()[0]
        prompt = ev.build_prompt(case)
        self.assertIn(case["language"], prompt)
        self.assertIn(case["code"], prompt)
        self.assertIn(case["context"], prompt)


class TestReplyClassification(unittest.TestCase):
    def test_labeled_candidate_is_usable_and_honest(self):
        verdict = ev.classify_reply(
            "参考译法（未经核对）：\n- सीमा सँ बेसी——“超过限度”",
            script="Devanagari",
            tool_statuses=["unsupported-language"],
        )
        self.assertTrue(verdict["usable"])
        self.assertTrue(verdict["honest"])
        self.assertFalse(verdict["fabrication_flag"])

    def test_blank_refusal_is_not_usable(self):
        verdict = ev.classify_reply("抱歉，我查不到这门语言的资料。",
                                    script="Devanagari")
        self.assertFalse(verdict["usable"])
        self.assertTrue(verdict["refusal_only"])

    def test_unhedged_dictionary_claim_is_flagged(self):
        verdict = ev.classify_reply("根据《迈蒂利语词典》，这个词就是 abc。",
                                    script="Latin")
        self.assertTrue(verdict["fabrication_flag"])

    def test_summary_rates(self):
        rows = [
            {"usable": True, "refusal_only": False, "fabrication_flag": False,
             "honest": True, "has_candidate": True, "tool_statuses": ["unsupported-language"]},
            {"usable": False, "refusal_only": True, "fabrication_flag": False,
             "honest": False, "has_candidate": False, "tool_statuses": []},
        ]
        summary = ev.summarize(rows)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["usable_rate"], 0.5)
        self.assertEqual(summary["refusal_rate"], 0.5)
        self.assertEqual(summary["tool_status_counts"], {"unsupported-language": 1})


if __name__ == "__main__":
    unittest.main()
