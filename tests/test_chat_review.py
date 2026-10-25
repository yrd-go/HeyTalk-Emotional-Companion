# -*- coding: utf-8 -*-
"""
tests/test_chat_review.py —— 回顾功能的四道闸门
================================================================

这个功能是项目里**唯一**会"主动出现在用户面前"的东西，所以它的每一条
约束都必须被测试钉住，不能只写在注释里。

四道闸门：
    内容闸：必须有值得追问的**已确认**记忆（且类型适合追问）
    时间闸：距上次 ≥ 7 天
    注意力闸：★ 上次没被点开 → 间隔拉长到 30 天（防习惯化的核心）
    永久闸：用户关掉 → 永不出现

另有一条**文案属性**测试：句子必须以**他的事**为主语，
不能以"我记得"为主语 —— 后者把落点放在产品身上，正是依恋机制。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chat import review as rv  # noqa: E402


@dataclass
class FakeEntry:
    kind: str = "goal"
    value: str = "我想考研"
    entry_id: str = "e1"
    times_used: int = 0
    created_at: str = "2026-09-01T00:00:00+00:00"


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class TestContentGate(unittest.TestCase):
    """内容闸：没有值得追问的东西就**不显示** —— 不为显示而硬凑。"""

    def test_shows_with_one_askable_entry(self):
        d = rv.decide_review([FakeEntry()], rv.ReviewState(), now=NOW)
        self.assertTrue(d.show)
        self.assertIn("我想考研", d.text)

    def test_no_entries_means_no_review(self):
        d = rv.decide_review([], rv.ReviewState(), now=NOW)
        self.assertFalse(d.show)
        self.assertIn("没有值得追问", d.reason)

    def test_facts_and_preferences_are_not_asked_about(self):
        """★ fact / preference 不问 —— 它们后面没有"后来"。

        硬问（"你之前说过「我喜欢安静」。后来怎么样了？"）会显得在硬凑话题，
        而"为了显示而显示"正是要避免的。
        """
        entries = [
            FakeEntry(kind="fact", value="我住在广州", entry_id="f1"),
            FakeEntry(kind="preference", value="我喜欢安静", entry_id="p1"),
        ]
        d = rv.decide_review(entries, rv.ReviewState(), now=NOW)
        self.assertFalse(d.show, "fact/preference 不该被追问")

    def test_all_three_askable_kinds_work(self):
        for kind in rv.ASKABLE_KINDS:
            d = rv.decide_review([FakeEntry(kind=kind)], rv.ReviewState(), now=NOW)
            self.assertTrue(d.show, f"{kind} 应当可以追问")
            self.assertTrue(d.text.endswith("？"), "应当是个问句")

    def test_empty_value_is_skipped(self):
        d = rv.decide_review([FakeEntry(value="   ")], rv.ReviewState(), now=NOW)
        self.assertFalse(d.show)

    def test_already_asked_entry_is_not_repeated(self):
        """提过的不再提 —— 反复问同一件事会显得没在听。"""
        state = rv.ReviewState(asked_entry_ids=("e1",))
        d = rv.decide_review([FakeEntry(entry_id="e1")], state, now=NOW)
        self.assertFalse(d.show)


class TestTimeGate(unittest.TestCase):
    def test_first_time_shows(self):
        d = rv.decide_review([FakeEntry()], rv.ReviewState(), now=NOW)
        self.assertTrue(d.show)
        self.assertEqual(d.state.shown_count, 1)

    def test_too_soon_after_a_seen_review(self):
        state = rv.ReviewState(
            last_shown_at=rv._iso(NOW - timedelta(days=3)),
            last_opened=True)
        d = rv.decide_review([FakeEntry()], state, now=NOW)
        self.assertFalse(d.show)
        self.assertIn("还没到", d.reason)

    def test_after_the_gap_it_shows_again(self):
        state = rv.ReviewState(
            last_shown_at=rv._iso(NOW - timedelta(days=8)),
            last_opened=True)
        d = rv.decide_review([FakeEntry()], state, now=NOW)
        self.assertTrue(d.show)


class TestAttentionGate(unittest.TestCase):
    """★ 注意力闸 —— 这是防习惯化的核心，也是这个功能最重要的一条。"""

    def test_ignored_review_backs_off_instead_of_retrying(self):
        """上次没被点开 → 用**更长**的间隔，而不是"再试一次"。

        商业产品在这里会做相反的选择（缩短间隔、加大推送）。
        我们让用户的沉默成为"少打扰我"的信号。
        """
        # 8 天前（已超过常规 7 天），但上次没被点开 → 应当仍然不显示
        state = rv.ReviewState(
            last_shown_at=rv._iso(NOW - timedelta(days=8)),
            last_opened=False)
        d = rv.decide_review([FakeEntry()], state, now=NOW)
        self.assertFalse(d.show, "被忽视后不该按常规间隔重来")
        self.assertIn("拉长", d.reason)

    def test_ignored_review_returns_after_the_backoff(self):
        state = rv.ReviewState(
            last_shown_at=rv._iso(NOW - timedelta(days=31)),
            last_opened=False)
        d = rv.decide_review([FakeEntry()], state, now=NOW)
        self.assertTrue(d.show, "拉长后的间隔到了就该再给一次")

    def test_backoff_is_longer_than_normal_gap(self):
        self.assertGreater(rv.REVIEW_BACKOFF_DAYS, rv.REVIEW_MIN_GAP_DAYS)


class TestDismissGate(unittest.TestCase):
    def test_dismissed_never_shows_again(self):
        state = rv.ReviewState(dismissed=True)
        d = rv.decide_review([FakeEntry()], state, now=NOW)
        self.assertFalse(d.show)
        self.assertIn("关闭", d.reason)

    def test_dismiss_survives_a_round_trip(self):
        state = rv.dismiss(rv.ReviewState())
        again = rv.ReviewState.from_dict(state.to_dict())
        self.assertTrue(again.dismissed)

    def test_mark_opened_resets_to_normal_gap(self):
        state = rv.ReviewState(last_shown_at=rv._iso(NOW), last_opened=False)
        rv.mark_opened(state)
        self.assertTrue(state.last_opened)
        # 点开过之后，常规间隔就够
        d = rv.decide_review([FakeEntry()],
                             rv.ReviewState(
                                 last_shown_at=rv._iso(NOW - timedelta(days=8)),
                                 last_opened=True),
                             now=NOW)
        self.assertTrue(d.show)


class TestWording(unittest.TestCase):
    """★ 文案属性：落点在**他的事**，不在"我记得"。"""

    def test_quotes_his_own_words(self):
        """用引号引用原话 —— 这样第一人称才不会人称错乱。

        value 保留的是用户的第一人称原话（"我妈总给我打电话"）。
        内嵌会变成"你之前提过我妈总打电话"（人称错），加引号就对了。
        """
        d = rv.decide_review(
            [FakeEntry(kind="relationship", value="我妈总给我打电话")],
            rv.ReviewState(), now=NOW)
        self.assertIn("「我妈总给我打电话」", d.text)

    def test_subject_is_his_life_not_my_memory(self):
        """★ 不能以"我记得"为主语。

        依据：《Frictionless Love》(ACM FAccT 2026) —— "它比谁都记得我"
        正是导致**强烈依恋**的机制。所以句子要指向他的真实生活，
        而不是这个产品记性多好。
        """
        d = rv.decide_review([FakeEntry()], rv.ReviewState(), now=NOW)
        for bad in ("我记得", "我一直记着", "我一直想着", "我没有忘记",
                    "我还记得", "我一直都在"):
            self.assertNotIn(bad, d.text, f"不该出现以产品为主语的措辞：{bad}")

    def test_question_matches_the_kind(self):
        goal = rv.decide_review([FakeEntry(kind="goal")], rv.ReviewState(), now=NOW)
        rel = rv.decide_review([FakeEntry(kind="relationship")],
                               rv.ReviewState(), now=NOW)
        self.assertNotEqual(goal.question, rel.question,
                            "不同类型该问不同的话（问法是按 kind 选的）")


class TestReviewStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-review-")
        self.store = rv.ReviewStore(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_is_a_clean_state(self):
        state = self.store.load()
        self.assertFalse(state.dismissed)
        self.assertEqual(state.shown_count, 0)

    def test_round_trip(self):
        state = rv.ReviewState(last_shown_at=rv._iso(NOW), last_opened=True,
                               shown_count=3, asked_entry_ids=("e1", "e2"))
        self.assertTrue(self.store.save(state))
        again = self.store.load()
        self.assertEqual(again.shown_count, 3)
        self.assertTrue(again.last_opened)
        self.assertEqual(again.asked_entry_ids, ("e1", "e2"))

    def test_corrupt_file_does_not_raise(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{ 这不是 json", encoding="utf-8")
        state = self.store.load()
        self.assertFalse(state.dismissed, "坏文件应当退回干净状态，而不是崩")

    def test_decision_state_is_recorded_as_not_yet_opened(self):
        """显示出来时先记"未点开"；用户真点开了才置为已接受。"""
        d = rv.decide_review([FakeEntry()], rv.ReviewState(), now=NOW)
        self.assertTrue(d.show)
        self.assertFalse(d.state.last_opened,
                         "刚显示时不算已接受 —— 否则注意力闸永远不生效")
        self.assertEqual(d.state.shown_count, 1)
        self.assertIn("e1", d.state.asked_entry_ids)


if __name__ == "__main__":
    unittest.main()
