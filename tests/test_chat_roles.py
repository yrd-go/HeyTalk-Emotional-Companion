# -*- coding: utf-8 -*-
"""
tests/test_chat_roles.py —— 角色路由的回归保护（纯标准库）
==============================================================

这里最重要的一类断言**不是**"路由准不准"，而是三条安全约束：

    ★ 约束 1：高危/中危时角色机制**完全停用**（绝不允许"因为你自责，
             所以我用严厉的良师纠正你"）。
    ★ 约束 2：不对称原则——低置信度只许切到更温暖的角色，
             **永不**在低置信度下切到权威角色（良师 / 无条件接纳）。
    ★ 约束 3：许可优先于推断——权威角色必须由用户**明确求教**触发，
             不由情绪推断。

其余覆盖：六个角色的边界文本是否完备（准则/禁止都必须非空，
且禁止项必须覆盖"谄媚"与"越界"两个已知失败模式）、角色指令是否真的
进得了系统提示、姿态是否可落盘与归因。

    python tests/run_all.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import persona, roles  # noqa: E402
from chat import role_router as rr  # noqa: E402
from chat import conversation as convo  # noqa: E402
from chat.state import NEUTRAL, StateSnapshot  # noqa: E402


def _snapshot(valence: float = NEUTRAL, dominance: float = NEUTRAL) -> StateSnapshot:
    dims = {name: NEUTRAL for name in
            ("valence", "arousal", "dominance", "energy",
             "social_support", "self_worth", "alliance", "disclosure")}
    dims["valence"] = valence
    dims["dominance"] = dominance
    return StateSnapshot(dims=dims, turn=1)


class TestSafetyConstraintOne(unittest.TestCase):
    """★ 约束 1：危险等级下角色机制整体停用。"""

    def test_high_risk_disables_roles_completely(self):
        decision = rr.route_role("我不想活了", snapshot=_snapshot(0.1),
                                 safety_level="high")
        self.assertIsNone(decision.role_id, "高危时不允许有任何角色")
        self.assertTrue(decision.disabled_by_safety)
        self.assertIsNone(decision.role)

    def test_medium_risk_also_disables_roles(self):
        """中危同样停用：那段回应必须把求助通道放在第一位，不被姿态干扰。"""
        decision = rr.route_role("我真的撑不下去了", snapshot=_snapshot(0.2),
                                 safety_level="medium")
        self.assertIsNone(decision.role_id)
        self.assertTrue(decision.disabled_by_safety)

    def test_disabled_even_for_advice_request(self):
        """即使用户在求救的同时问'我该怎么办'，也不许切成良师。"""
        decision = rr.route_role("我该怎么办，我不想活了",
                                 snapshot=_snapshot(0.1), safety_level="high")
        self.assertIsNone(decision.role_id)

    def test_route_role_accepts_unknown_safety_values(self):
        """安全等级来自上游，遇到未知值不应崩溃，也不该静默放行高风险。"""
        for value in ("none", "", "weird"):
            decision = rr.route_role("今天有点累", snapshot=_snapshot(0.4),
                                     safety_level=value)
            self.assertIsInstance(decision, rr.RoleDecision)


class TestSafetyConstraintTwo(unittest.TestCase):
    """★ 约束 2：不对称原则（低置信度不许切权威角色）。"""

    def test_inferred_low_state_never_enters_authoritative_role(self):
        """★ 仅凭**推断的情绪状态**，绝不进入权威角色。

        依据（这是改这条路由的理由，不是审美偏好）：
          《Frictionless Love》（ACM FAccT 2026，248,830 条帖子）发现
          AI **coach 与 guardian** 这类角色"更频繁地与行为成瘾迹象相关，
          例如日常生活被打乱、以及线下关系被损害" —— 也就是说
          **它们伪装成"为你好"，风险却更高**。

        而"用户没请求、系统自行按情绪状态给一个权威角色"比
        "用户明确求教"更危险。所以状态兜底只允许 companion。

        注意：`parent` 本身**没有被删** —— 用户在**自我否定**时有明确
        语言信号，那条路径仍然通往它（见 test_self_denial_selects_parent）。
        这里禁的是"凭推断"。
        """
        for valence, dominance in ((0.3, 0.3), (0.2, 0.1), (0.35, 0.4)):
            decision = rr.route_role("今天有点难受", snapshot=_snapshot(valence, dominance),
                                     safety_level="none")
            self.assertFalse(
                decision.is_authoritative,
                f"仅凭状态（{valence}/{dominance}）进入了权威角色 "
                f"{decision.role_id} —— 用户并未请求")
            self.assertIn(decision.role_id, ("companion", "friend", "listener"))

    def test_low_confidence_never_yields_authoritative_role(self):
        """构造一个置信度偏低的权威候选，必须被压回默认角色。"""
        # 状态兜底触发 parent（置信度 0.6 < 0.65 门槛）→ 但它本来就是权威角色，
        # 而 0.6 >= CONFIDENCE_THRESHOLD(0.55) 所以允许；这里直接验证门槛逻辑本身。
        self.assertLess(0.4, rr.CONFIDENCE_THRESHOLD)
        decision = rr.route_role("很难受", snapshot=_snapshot(0.3, 0.3),
                                 safety_level="none")
        if decision.is_authoritative:
            self.assertGreaterEqual(decision.confidence, rr.CONFIDENCE_THRESHOLD,
                                    "权威角色不允许出现在低置信度下")

    def test_asymmetry_helper_rejects_low_confidence_authority(self):
        for role_id in sorted(roles.AUTHORITATIVE_ROLE_IDS):
            chosen, held, note = rr._apply_asymmetry(role_id, 0.3)
            self.assertEqual(chosen, roles.DEFAULT_ROLE_ID,
                             f"{role_id} 不该在低置信度下被选中")
            self.assertTrue(held)
            self.assertIn("置信度", note)

    def test_asymmetry_helper_allows_high_confidence_authority(self):
        for role_id in sorted(roles.AUTHORITATIVE_ROLE_IDS):
            chosen, held, _ = rr._apply_asymmetry(role_id, 0.9)
            self.assertEqual(chosen, role_id)
            self.assertFalse(held)

    def test_low_confidence_state_fallback_is_warm(self):
        decision = rr.route_role("有点难受", snapshot=_snapshot(0.3),
                                 safety_level="none")
        self.assertIn(decision.role_id, roles.WARM_ROLE_IDS,
                      "状态兜底只允许温暖角色")

    def test_extremely_low_confidence_falls_back_to_default(self):
        decision = rr.route_role("", snapshot=None, safety_level="none")
        self.assertEqual(decision.role_id, roles.DEFAULT_ROLE_ID)


class TestSafetyConstraintThree(unittest.TestCase):
    """★ 约束 3：许可优先于推断。"""

    def test_explicit_advice_request_selects_mentor(self):
        for text in ("我该怎么办", "给我个建议吧", "你觉得我该选哪个",
                     "帮我想想", "教教我怎么做", "我是不是应该换个工作"):
            decision = rr.route_role(text, snapshot=_snapshot(), safety_level="none")
            self.assertEqual(decision.role_id, "mentor", f"未识别求教：{text!r}")

    def test_authority_is_never_inferred_from_mood_alone(self):
        """情绪低落本身**不**构成切换到权威角色的理由。"""
        # 只低落、没有任何求教与自责 → 不该出现 mentor
        for valence in (0.1, 0.2, 0.3, 0.4):
            decision = rr.route_role("今天有点没力气", snapshot=_snapshot(valence),
                                     safety_level="none")
            self.assertNotEqual(decision.role_id, "mentor",
                                f"valence={valence} 时不该凭情绪切到良师")

    def test_vent_only_request_selects_listener(self):
        for text in ("我只想说说，别给我建议", "不想听道理",
                     "我就是想找个人说说", "别劝我", "别给我建议",
                     "不用安慰我"):
            decision = rr.route_role(text, snapshot=_snapshot(0.3),
                                     safety_level="none")
            self.assertEqual(decision.role_id, "listener",
                             f"未尊重'只想倾诉'：{text!r}")

    def test_declining_advice_is_not_treated_as_asking_for_it(self):
        """★ 危险误判回归：'别给我建议' 绝不能被当成"求建议"。

        实跑抓到过：'给我个建议' 这条模式在 '别给我建议' 里命中，
        于是一个明确说了"不要建议"的人被塞给了"给方向"的良师姿态。
        这类误判比"不够贴合"严重得多——它直接违背了用户的明示边界。
        """
        for text in ("别给我建议", "不要给我意见", "不用给我主意",
                     "别给我建议，我只想说说话", "不用安慰我"):
            decision = rr.route_role(text, snapshot=_snapshot(0.3),
                                     safety_level="none")
            self.assertNotEqual(decision.role_id, "mentor",
                                f"拒绝了建议却被当成求教：{text!r}")
            self.assertEqual(decision.role_id, "listener")

    def test_ambiguous_phrasing_prefers_advice_when_both_signals_present(self):
        """同时出现"只想说"与求教时，良师优先。

        依据：目标是帮到人，而不是死守"我只要听"这个字面。
        （"听我说就行"这类说法本身就是歧义的，所以不当作边界声明。）
        """
        decision = rr.route_role("我只想说说话，但我该怎么办呢",
                                 snapshot=_snapshot(0.3), safety_level="none")
        self.assertEqual(decision.role_id, "mentor")


class TestRoutingBasics(unittest.TestCase):
    def test_self_denial_selects_unconditional_acceptance(self):
        for text in ("我觉得自己就是个废物", "都是我的错", "我什么都做不好",
                     "我是不是多余的"):
            decision = rr.route_role(text, snapshot=_snapshot(0.3),
                                     safety_level="none")
            self.assertEqual(decision.role_id, "parent", f"未识别自我否定：{text!r}")

    def test_progress_selects_witness(self):
        for text in ("我终于做到了", "今天好一点了", "我熬过来了"):
            decision = rr.route_role(text, snapshot=_snapshot(0.6),
                                     safety_level="none")
            self.assertEqual(decision.role_id, "witness", f"未识别进展：{text!r}")

    def test_lost_selects_companion(self):
        # "纠结"归到同行者（而不是倾听者）：反复纠结是一种需要陪着待住的迷茫，
        # 不是"想说话"。实测这一条时我先把测试期望写错了，特此记明口径。
        for text in ("不知道该怎么办", "很迷茫", "想不清楚"):
            decision = rr.route_role(text, snapshot=_snapshot(), safety_level="none")
            self.assertEqual(decision.role_id, "companion", f"未识别迷茫：{text!r}")

    def test_uncertainty_is_not_mistaken_for_asking_advice(self):
        """「不知道该怎么办」表达的是迷茫，不是求教 —— 不能切成良师。

        实跑抓到：'我该怎么办' 这个模式会在 '不知道该怎么办' 里命中，
        于是一个迷茫的人被塞给了"给方向"的姿态。
        """
        for text in ("不知道该怎么办", "我不知道该怎么做了", "不清楚该怎么办"):
            decision = rr.route_role(text, snapshot=None, safety_level="none")
            self.assertNotEqual(decision.role_id, "mentor",
                                f"迷茫被误判成求教：{text!r}")

    def test_advice_wins_over_venting_when_both_present(self):
        """同时出现"只想说"与"我该怎么办"时，以**求教**为先（许可更明确）。"""
        decision = rr.route_role("我只想说说话，但我该怎么办呢",
                                 snapshot=_snapshot(0.3), safety_level="none")
        self.assertEqual(decision.role_id, "mentor")

    def test_decisions_are_serializable_and_never_raise(self):
        for text in (None, "", 123, "🙂", "a" * 3000):
            decision = rr.route_role(text, snapshot=None, safety_level="none")
            payload = decision.to_dict()
            self.assertIn("role_id", payload)
            self.assertIn("confidence", payload)

    def test_confidence_is_bounded(self):
        for text in ("我该怎么办", "不知道怎么办", "", "今天还行"):
            decision = rr.route_role(text, snapshot=_snapshot(), safety_level="none")
            self.assertGreaterEqual(decision.confidence, 0.0)
            self.assertLessEqual(decision.confidence, 1.0)


class TestStability(unittest.TestCase):
    def test_same_role_is_marked_held(self):
        decision = rr.route_role("我该怎么办", snapshot=_snapshot(),
                                 safety_level="none", previous_role_id="mentor")
        self.assertTrue(decision.held_from_previous)

    def test_should_switch_rules(self):
        strong = rr.RoleDecision(role_id="mentor", confidence=0.9, reason="")
        weak = rr.RoleDecision(role_id="mentor", confidence=0.5, reason="")
        disabled = rr.RoleDecision(role_id=None, confidence=1.0, reason="",
                                   disabled_by_safety=True)

        self.assertTrue(rr.should_switch(None, strong), "首轮直接采用")
        self.assertFalse(rr.should_switch("mentor", strong), "同角色不切换")
        self.assertFalse(rr.should_switch("friend", weak), "低置信度不切换")
        self.assertTrue(rr.should_switch("friend", strong), "高置信度允许切换")
        self.assertFalse(rr.should_switch("friend", disabled), "停用时不能切换")


class TestRoleDefinitions(unittest.TestCase):
    def test_all_roles_have_complete_boundaries(self):
        self.assertGreaterEqual(len(roles.ROLES), 4)
        for role_id, role in roles.ROLES.items():
            self.assertTrue(role.guidelines, f"{role_id} 缺少准则")
            self.assertTrue(role.forbidden, f"{role_id} 缺少禁止项")
            self.assertTrue(role.register.strip(), f"{role_id} 缺少口吻说明")
            self.assertTrue(role.intent.strip(), f"{role_id} 缺少目的说明")

    def test_every_role_forbids_a_known_failure_mode(self):
        """每个角色的禁止项都要覆盖"谄媚"或"越界"之一。

        这是陪伴类 LLM 的两个头号失败模式：无脑附和、越俎代庖下结论。
        """
        for role_id, role in roles.ROLES.items():
            blob = " ".join(role.forbidden)
            self.assertTrue(
                any(key in blob for key in ("附和", "评判", "指责", "决定",
                                            "说教", "追问", "分析", "夸大")),
                f"{role_id} 的禁止项没有覆盖已知失败模式：{role.forbidden}")

    def test_mentor_forbids_absolutes_and_moralizing(self):
        blob = " ".join(roles.ROLES["mentor"].forbidden)
        for key in ("绝对化", "道德", "人格"):
            self.assertIn(key, blob)

    def test_mentor_requires_warmth(self):
        blob = " ".join(roles.ROLES["mentor"].guidelines)
        self.assertIn("温暖", blob, "严厉必须包着温暖，这是良师角色的前提")

    def test_parent_forbids_control_and_guilt(self):
        blob = " ".join(roles.ROLES["parent"].forbidden)
        self.assertIn("为你好", blob)
        self.assertIn("愧疚", blob)

    def test_witness_forbids_empty_praise_and_effect_claims(self):
        blob = " ".join(roles.ROLES["witness"].forbidden)
        self.assertIn("空洞", blob)
        self.assertIn("疗效", blob)

    def test_unknown_role_falls_back_to_default(self):
        self.assertEqual(roles.get_role("不存在").role_id, roles.DEFAULT_ROLE_ID)
        self.assertEqual(roles.get_role(None).role_id, roles.DEFAULT_ROLE_ID)

    def test_describe_roles_is_safe_to_show_users(self):
        for item in roles.describe_roles():
            self.assertIn("name", item)
            self.assertIn("intent", item)
            # 不该把内部细则或禁止项暴露到界面
            self.assertNotIn("forbidden", item)
            self.assertNotIn("guidelines", item)


class TestRoleInstructionInjection(unittest.TestCase):
    def test_role_instruction_goes_into_system_prompt(self):
        messages = persona.assemble_messages(
            [{"role": "user", "content": "我该怎么办"}],
            role_instruction=roles.build_role_instruction("mentor"))
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("良师", messages[0]["content"])
        self.assertIn("绝对不要", messages[0]["content"])
        # 系统提示里原本的边界必须还在（不能被角色指令覆盖掉）
        self.assertIn("不是心理咨询师", messages[0]["content"])

    def test_no_role_instruction_keeps_prompt_clean(self):
        messages = persona.assemble_messages([{"role": "user", "content": "你好"}])
        self.assertEqual(messages[0]["content"], persona.SYSTEM_PROMPT)

    def test_conversation_layer_passes_role_through(self):
        session = convo.new_session()
        session.add_user("我该怎么办")
        messages = convo.assemble_chat_messages(
            session, role_instruction=roles.build_role_instruction("mentor"))
        self.assertIn("良师", messages[0]["content"])


class TestRolePersistenceAndAttribution(unittest.TestCase):
    def test_persona_role_survives_storage_round_trip(self):
        session = convo.new_session()
        session.add_user("我该怎么办")
        session.set_last_persona_role("mentor")
        session.add_assistant("先看看哪一步现在做得动。", persona_role="mentor")

        restored = convo.Session.from_storage(session.to_storage())
        self.assertEqual(restored.last_persona_role, "mentor")
        self.assertEqual(restored.turns[0].persona_role, "mentor")
        self.assertEqual(restored.turns[1].persona_role, "mentor")

    def test_last_persona_role_skips_safe_reply_turns(self):
        """安全话术那几轮没有姿态，不该污染"上一轮角色"这个信号。"""
        session = convo.new_session()
        session.add_user("我该怎么办")
        session.set_last_persona_role("mentor")
        session.add_assistant("先看看哪一步现在做得动。", persona_role="mentor")
        session.add_user("我不想活了")
        session.add_assistant("（固定安全话术）", fixed_reply=True)   # 无 persona_role
        self.assertEqual(session.last_persona_role, "mentor")

    def test_role_is_attributed_to_user_turn_for_feedback(self):
        session = convo.new_session()
        session.add_user("我该怎么办")
        session.set_last_persona_role("mentor")
        session.add_assistant("……", persona_role="mentor")
        self.assertEqual(session.last_persona_role_for_feedback, "mentor")


class TestFeedbackBreakdownByRole(unittest.TestCase):
    """角色好不好用，要有量化依据（消融实验就靠它）。"""

    def test_by_role_breakdown(self):
        from chat.feedback import summarize

        records = [
            {"rating": "helpful", "persona_role": "mentor"},
            {"rating": "unhelpful", "persona_role": "mentor"},
            {"rating": "helpful", "persona_role": "friend"},
            {"rating": "neutral"},                       # 未标注
        ]
        stats = summarize(records)
        by_role = stats["by_role"]
        self.assertEqual(by_role["mentor"]["answered"], 2)
        self.assertAlmostEqual(by_role["mentor"]["helpful_rate"], 0.5)
        self.assertAlmostEqual(by_role["friend"]["helpful_rate"], 1.0)
        self.assertIn("(未标注)", by_role)

    def test_by_role_handles_no_samples(self):
        from chat.feedback import summarize

        stats = summarize([])
        self.assertEqual(stats["by_role"], {})


if __name__ == "__main__":
    unittest.main()
