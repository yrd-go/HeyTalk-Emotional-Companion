# -*- coding: utf-8 -*-
"""
tests/test_chat_feedback.py —— 反馈埋点的回归保护（S5，纯标准库）
====================================================================

这一层的价值不在"能存能读"，而在把几条**容易自欺的统计口径**钉死：

    ① "说不好"绝不能算成"没帮助"—— 那是典型的自欺口径；
    ② 稀疏提问：每 N 轮问一次，不是每轮都弹问卷（否则毁掉体验）；
    ③ 同一轮不能重复埋点（用户刷新后会再看到那张卡片）；
    ④ 留言必须过脱敏（防手滑把 API Key 贴进去）；
    ⑤ 坏行不能毁掉统计（JSONL 的容错性要真的成立）。

测试写到 tests/.tmp_feedback/（.gitignore 已忽略 tests/.tmp_*/），不碰真实目录。

    python tests/run_all.py
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat.feedback import (  # noqa: E402
    DEFAULT_ASK_EVERY,
    MAX_COMMENT_CHARS,
    RATING_VALUES,
    FeedbackEntry,
    FeedbackStore,
    feedback_due,
    next_pending_turn,
    summarize,
)

TMP_ROOT = Path(__file__).resolve().parent / ".tmp_feedback"


class FeedbackTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = TMP_ROOT / self._testMethodName
        shutil.rmtree(self.base, ignore_errors=True)
        self.base.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def store(self) -> FeedbackStore:
        return FeedbackStore(self.base)


class TestSparseAsking(unittest.TestCase):
    """② 稀疏提问 + ③ 不重复埋点。"""

    def test_feedback_due_every_n(self):
        self.assertFalse(feedback_due(0, every=3))
        self.assertFalse(feedback_due(1, every=3))
        self.assertFalse(feedback_due(2, every=3))
        self.assertTrue(feedback_due(3, every=3))
        self.assertFalse(feedback_due(4, every=3))
        self.assertTrue(feedback_due(6, every=3))

    def test_every_zero_disables_feature(self):
        for count in (0, 1, 3, 99):
            self.assertFalse(feedback_due(count, every=0))
        self.assertIsNone(next_pending_turn([{"role": "assistant"}], [], every=0))

    def _turns(self, pairs: int):
        turns = []
        for _ in range(pairs):
            turns.append({"role": "user"})
            turns.append({"role": "assistant"})
        return turns

    def test_picks_third_assistant_turn(self):
        # 3 轮问答 → 第 3 个 assistant 在索引 5
        self.assertEqual(next_pending_turn(self._turns(3), [], every=3), 5)

    def test_returns_none_when_not_enough_turns(self):
        self.assertIsNone(next_pending_turn(self._turns(2), [], every=3))
        self.assertIsNone(next_pending_turn([], [], every=3))

    def test_recorded_turns_are_skipped(self):
        turns = self._turns(3)
        self.assertEqual(next_pending_turn(turns, [5], every=3), None)
        # 再聊到第 6 个 assistant（索引 11）才会再问
        self.assertEqual(next_pending_turn(self._turns(6), [5], every=3), 11)

    def test_works_with_objects_not_only_dicts(self):
        class Msg:
            def __init__(self, role):
                self.role = role

        turns = [Msg("user"), Msg("assistant")] * 3
        self.assertEqual(next_pending_turn(turns, [], every=3), 5)


class TestRatingChoices(unittest.TestCase):
    def test_three_choices_and_no_stars(self):
        self.assertEqual(set(RATING_VALUES), {"helpful", "neutral", "unhelpful"})

    def test_default_ask_every_is_three(self):
        self.assertEqual(DEFAULT_ASK_EVERY, 3)


class TestStore(FeedbackTestBase):
    def test_append_and_read(self):
        store = self.store()
        store.append(FeedbackEntry(turn_index=5, rating="helpful",
                                   safety_level="low", session_id="s1"))
        records = store.read_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rating"], "helpful")
        self.assertEqual(records[0]["turn_index"], 5)
        self.assertEqual(records[0]["schema"], 1)
        self.assertTrue(records[0]["created_at"])

    def test_jsonl_is_append_only_and_human_readable(self):
        store = self.store()
        store.append(FeedbackEntry(turn_index=1, rating="neutral"))
        store.append(FeedbackEntry(turn_index=2, rating="helpful"))
        lines = store.file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)          # 每行都是独立合法 JSON

    def test_recorded_turns_filters_by_session(self):
        store = self.store()
        store.append(FeedbackEntry(turn_index=5, rating="helpful", session_id="a"))
        store.append(FeedbackEntry(turn_index=7, rating="helpful", session_id="b"))
        self.assertEqual(store.recorded_turns("a"), {5})
        self.assertEqual(store.recorded_turns("b"), {7})
        self.assertEqual(store.recorded_turns("c"), set())

    def test_clear_deletes_file(self):
        store = self.store()
        store.append(FeedbackEntry(turn_index=1, rating="helpful"))
        self.assertEqual(store.clear(), 1)
        self.assertFalse(store.file.exists())
        self.assertEqual(store.clear(), 0)

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(self.store().read_all(), [])
        self.assertEqual(self.store().stats()["total"], 0)


class TestTolerantReading(FeedbackTestBase):
    """⑤ 坏行不能毁掉统计。"""

    def test_broken_lines_are_skipped(self):
        store = self.store()
        store.ensure_dir()
        store.file.write_text(
            '{"rating": "helpful", "turn_index": 1}\n'
            '这不是 JSON\n'
            '\n'
            '[1,2,3]\n'
            '{"rating": "unhelpful", "turn_index": 2}\n',
            encoding="utf-8",
        )
        records = store.read_all()
        self.assertEqual(len(records), 2)
        self.assertEqual([r["rating"] for r in records], ["helpful", "unhelpful"])


class TestPrivacy(FeedbackTestBase):
    """④ 留言脱敏 + 不落原文。"""

    def test_comment_is_masked(self):
        entry = FeedbackEntry(turn_index=1, rating="helpful",
                              comment="我用的是 sk-abcdefgh12345678 这个 key")
        payload = entry.to_dict()
        self.assertNotIn("sk-abcdefgh12345678", json.dumps(payload, ensure_ascii=False))
        self.assertIn("sk-***", payload["comment"])

    def test_comment_is_truncated(self):
        entry = FeedbackEntry(turn_index=1, rating="neutral",
                              comment="很" * (MAX_COMMENT_CHARS + 200))
        self.assertLessEqual(len(entry.to_dict()["comment"]), MAX_COMMENT_CHARS + 1)

    def test_comment_whitespace_is_collapsed(self):
        entry = FeedbackEntry(turn_index=1, rating="neutral",
                              comment="  说了   很多\n\n空白  ")
        self.assertEqual(entry.to_dict()["comment"], "说了 很多 空白")

    def test_empty_comment_is_omitted(self):
        payload = FeedbackEntry(turn_index=1, rating="helpful").to_dict()
        self.assertNotIn("comment", payload)

    def test_record_never_contains_conversation_text(self):
        """反馈记录里不该出现用户原话或模型回应。"""
        store = self.store()
        store.append(FeedbackEntry(turn_index=1, rating="helpful",
                                   comment="好一些了"))
        raw = store.file.read_text(encoding="utf-8")
        self.assertNotIn("我不想活了", raw)
        self.assertNotIn("valence_history", raw)


class TestValenceDelta(unittest.TestCase):
    """客观信号：该轮前后的情绪变化。"""
    def test_delta_is_computed(self):
        entry = FeedbackEntry(turn_index=1, rating="helpful",
                              valence_before=0.4, valence_after=0.46)
        self.assertAlmostEqual(entry.valence_delta, 0.06, places=4)
        payload = entry.to_dict()
        self.assertAlmostEqual(payload["valence_before"], 0.4, places=4)
        self.assertAlmostEqual(payload["valence_after"], 0.46, places=4)

    def test_delta_is_none_without_pair(self):
        self.assertIsNone(FeedbackEntry(turn_index=1, rating="neutral").valence_delta)
        self.assertIsNone(
            FeedbackEntry(turn_index=1, rating="neutral", valence_before=0.4).valence_delta)
        payload = FeedbackEntry(turn_index=1, rating="neutral").to_dict()
        self.assertNotIn("valence_before", payload)


class TestSummarize(FeedbackTestBase):
    """① 统计口径：'说不好'绝不能算成'没帮助'。"""

    def test_neutral_is_not_counted_as_unhelpful(self):
        records = [
            {"rating": "helpful"}, {"rating": "helpful"},
            {"rating": "neutral"}, {"rating": "unhelpful"},
        ]
        stats = summarize(records)
        self.assertEqual(stats["counts"]["neutral"], 1)
        self.assertEqual(stats["counts"]["unhelpful"], 1)
        self.assertEqual(stats["answered"], 4)
        self.assertAlmostEqual(stats["helpful_rate"], 0.5, places=4)
        self.assertAlmostEqual(stats["unhelpful_rate"], 0.25, places=4)

    def test_rates_are_none_without_samples(self):
        stats = summarize([])
        self.assertIsNone(stats["helpful_rate"])
        self.assertIsNone(stats["unhelpful_rate"])
        self.assertIsNone(stats["mean_valence_delta"])
        self.assertEqual(stats["total"], 0)

    def test_unknown_rating_is_ignored(self):
        stats = summarize([{"rating": "???"}, {"rating": "helpful"}])
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["answered"], 1)
        self.assertAlmostEqual(stats["helpful_rate"], 1.0, places=4)

    def test_valence_delta_average_only_over_paired(self):
        records = [
            {"rating": "helpful", "valence_before": 0.4, "valence_after": 0.5},
            {"rating": "helpful", "valence_before": 0.5, "valence_after": 0.5},
            {"rating": "neutral"},                      # 无配对，不参与均值
            {"rating": "helpful", "valence_before": 0.2},  # 半对，也不参与
        ]
        stats = summarize(records)
        self.assertEqual(stats["paired_samples"], 2)
        self.assertAlmostEqual(stats["mean_valence_delta"], 0.05, places=4)

    def test_stats_reports_path(self):
        store = self.store()
        store.append(FeedbackEntry(turn_index=1, rating="helpful"))
        self.assertIn("feedback.jsonl", store.stats()["path"])


if __name__ == "__main__":
    unittest.main()
