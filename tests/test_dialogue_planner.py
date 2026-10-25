# -*- coding: utf-8 -*-
"""会话内连续性规划器的纯规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat.dialogue_planner import OPEN_THREAD_WINDOW, build_turn_plan  # noqa: E402


def _turn(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


SELF_WORTH_HISTORY = [
    _turn("user", "今天有点累"),
    _turn("assistant", "累啊。是那种睡一觉也补不回来的累吗？"),
    _turn("user", "感觉自己没什么用"),
    _turn("assistant", "“没什么用”这感觉一上来，人容易整个陷进去。是今天有什么事勾出来的吗？"),
]


class TestBuildTurnPlan(unittest.TestCase):
    def test_reason_keeps_self_worth_thread(self):
        plan = build_turn_plan("因为我没有法拉利", SELF_WORTH_HISTORY)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.relation, "reason")
        self.assertEqual(plan.anchor, "感觉自己没什么用")
        self.assertTrue(plan.answering_question)
        text = plan.to_instruction()
        self.assertIn("自然带着这个背景回应", text)
        self.assertIn("也别复述原句", text)

    def test_desire_followup_stays_connected(self):
        history = SELF_WORTH_HISTORY + [
            _turn("user", "因为我没有法拉利"),
            _turn("assistant", "想要是真的，别拿它给自己打分。馋它哪点？"),
        ]
        plan = build_turn_plan("真想要一台", history)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.relation, "desire")
        self.assertIn("仍和前面的自我评价连着", plan.to_instruction())

    def test_answer_to_question_gets_acknowledgement_rule(self):
        plan = build_turn_plan("也不是工作，就是今天什么都不顺", SELF_WORTH_HISTORY)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.relation, "answer")
        self.assertIn("是在回答上一问", plan.to_instruction())

    def test_direct_self_worth_turn_does_not_duplicate_plan(self):
        self.assertIsNone(build_turn_plan("我现在觉得自己很没用", []))

    def test_unrelated_task_does_not_inject_continuity(self):
        self.assertIsNone(build_turn_plan("翻译成拉丁文：愿我们都能平安回家", SELF_WORTH_HISTORY))
        self.assertIsNone(build_turn_plan("北京明天天气怎么样", SELF_WORTH_HISTORY))

    def test_explicit_topic_change_is_respected(self):
        self.assertIsNone(build_turn_plan("换个话题吧，不说这个了", SELF_WORTH_HISTORY))

    def test_old_thread_expires_after_window(self):
        history = [_turn("user", "感觉自己没什么用")]
        for index in range(OPEN_THREAD_WINDOW):
            history.append(_turn("user", f"普通闲聊 {index}"))
            history.append(_turn("assistant", "嗯。"))
        self.assertIsNone(build_turn_plan("因为我没有法拉利", history))

    def test_without_self_worth_anchor_no_plan(self):
        history = [
            _turn("user", "我今天看了一辆车"),
            _turn("assistant", "哪一款？"),
        ]
        self.assertIsNone(build_turn_plan("真想要一台", history))

    def test_not_worthy_and_unimportant_are_recognized_anchors(self):
        history = [
            _turn("user", "我觉得自己不值得被喜欢"),
            _turn("assistant", "你是在某段关系里受了什么委屈吗？"),
        ]
        plan = build_turn_plan("因为一直没有人选我", history)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.anchor, "我觉得自己不值得被喜欢")

        history = [
            _turn("user", "我总觉得自己在别人那里一点都不重要"),
            _turn("assistant", "最近是谁让你有了这种感觉？"),
        ]
        plan = build_turn_plan("因为她们都不回我消息", history)
        self.assertIsNotNone(plan)

    def test_instruction_is_short_and_non_diagnostic(self):
        plan = build_turn_plan("因为我没有法拉利", SELF_WORTH_HISTORY)
        assert plan is not None
        text = plan.to_instruction()
        self.assertLess(len(text), 140)
        self.assertNotIn("抑郁", text)
        self.assertNotIn("焦虑症", text)


if __name__ == "__main__":
    unittest.main()
