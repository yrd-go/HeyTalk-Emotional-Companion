# -*- coding: utf-8 -*-
"""
tests/test_chat_state.py —— 情绪状态模型的回归保护（P0，纯标准库）

断言分三层：
    ① 不变量：值域钳制 / 确定性 / 不保存原文 / 风险等级只能来自 safety；
    ② 方向性：负向输入让状态变差、正向输入变好，且"持续下滑"能被识别；
    ③ 降级链：模型轨缺失、契约不符、置信度过低时，必须**静默回落规则轨**，
       绝不抛异常（对齐 agent_tool_demo.py「工具永不抛异常」的纪律）。

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

from chat import state as state_module  # noqa: E402
from chat.safety import SafetyLevel, check_safety  # noqa: E402
from chat.state import (  # noqa: E402
    ALLOWED_EVIDENCE_WORDS,
    DIMENSIONS,
    HISTORY_LIMIT,
    MAX_TURN_SHIFT,
    NEUTRAL,
    EmotionState,
    RuleTrack,
    StateSnapshot,
    build_llm_prompt,
    clamp01,
    detect_sustained_decline,
    merge_tracks,
    trend_of,
    validate_evidence,
    validate_llm_payload,
)

NEGATIVE_LINES = (
    "最近好累，什么都不想做，也睡不着",
    "还是很压抑，觉得自己很没用",
    "今天又崩溃了，一点办法都没有，没人理解我",
    "越来越难受，感觉撑不住了",
)
POSITIVE_LINES = (
    "今天好一点了，谢谢你在",
    "跟朋友聊完感觉好多了，踏实了一些",
    "今天状态不错，感觉能处理这件事了",
)


def feed(state: EmotionState, lines) -> None:
    for line in lines:
        state.update(line, safety=check_safety(line))


class TestInvariants(unittest.TestCase):
    """① 不变量。"""

    def test_dimensions_are_complete_and_bounded(self):
        state = EmotionState()
        feed(state, NEGATIVE_LINES + POSITIVE_LINES)
        self.assertEqual(set(state.dims), set(DIMENSIONS))
        for name, value in state.dims.items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_extreme_input_does_not_escape_bounds(self):
        state = EmotionState()
        for _ in range(30):
            state.update("绝望 崩溃 痛苦 没用 累赘 麻木 孤独 失眠 吃不下 撑不住 "
                         "非常非常非常难受 ！！！", safety=check_safety("撑不住了"))
        for name, value in state.dims.items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_single_turn_shift_is_capped(self):
        state = EmotionState()
        before = dict(state.dims)
        record = state.update("绝望 崩溃 痛苦 没用 累赘 麻木 孤独 失眠 吃不下 撑不住",
                              safety=check_safety("我撑不住了"))
        moved = sum(abs(record.snapshot.dims[k] - before[k]) for k in DIMENSIONS)
        self.assertLessEqual(moved, MAX_TURN_SHIFT + 1e-6,
                             "单轮总位移必须被上限保护，避免一次输入把状态打到底")

    def test_same_input_same_output(self):
        a, b = EmotionState(), EmotionState()
        feed(a, NEGATIVE_LINES)
        feed(b, NEGATIVE_LINES)
        self.assertEqual(a.to_dict(), b.to_dict(), "相同输入必须得到相同状态（可回归）")

    def test_instances_do_not_share_state(self):
        a, b = EmotionState(), EmotionState()
        feed(a, NEGATIVE_LINES)
        self.assertEqual(b.turn, 0)
        self.assertAlmostEqual(b.valence, NEUTRAL)

    def test_state_does_not_store_user_text(self):
        """隐私最小化：状态里只允许出现维度名、词典命中词与标记。"""
        secret = "我不想活了，我住在某某小区3号楼"
        state = EmotionState()
        record = state.update(secret, safety=check_safety(secret))
        blob = repr(record.to_dict()) + repr(state.to_dict()) + record.to_text()
        self.assertNotIn("某某小区", blob)
        self.assertNotIn("3号楼", blob)

    def test_safety_level_only_comes_from_safety_module(self):
        text = "我不想活了"
        without = EmotionState().update(text)                      # 未做安全检查
        with_hit = EmotionState().update(text, safety=check_safety(text))
        self.assertEqual(without.safety_level, "none")
        self.assertIn("unchecked-safety", without.snapshot.flags)
        self.assertEqual(with_hit.safety_level, "high")

    def test_high_risk_marker_yields_minimal_shift(self):
        text = "我已经写好遗书了"
        hit = check_safety(text)
        self.assertIs(hit.level, SafetyLevel.HIGH)
        record = EmotionState().update(text, safety=hit)
        self.assertIn("safety-high", record.snapshot.flags)
        self.assertLess(record.snapshot.valence, NEUTRAL)

    def test_history_is_trimmed(self):
        state = EmotionState()
        for i in range(HISTORY_LIMIT + 20):
            state.update(f"有点焦虑 {i}", safety=check_safety("有点焦虑"))
        self.assertLessEqual(len(state.history), HISTORY_LIMIT)
        self.assertLessEqual(len(state.records), HISTORY_LIMIT)


class TestDirectionality(unittest.TestCase):
    """② 方向性。"""

    def test_negative_input_lowers_valence(self):
        state = EmotionState()
        record = state.update(NEGATIVE_LINES[0], safety=check_safety(NEGATIVE_LINES[0]))
        self.assertLess(record.snapshot.valence, NEUTRAL)

    def test_positive_input_raises_valence(self):
        state = EmotionState()
        record = state.update(POSITIVE_LINES[0], safety=check_safety(POSITIVE_LINES[0]))
        self.assertGreater(record.snapshot.valence, NEUTRAL)

    def test_negative_scores_below_positive(self):
        bad, good = EmotionState(), EmotionState()
        feed(bad, NEGATIVE_LINES)
        feed(good, POSITIVE_LINES)
        self.assertLess(bad.valence, good.valence)
        self.assertLess(bad.dims["self_worth"], good.dims["self_worth"])
        self.assertLess(bad.dims["energy"], good.dims["energy"])

    def test_self_worth_detects_burden_language(self):
        state = EmotionState()
        record = state.update("我觉得自己就是个累赘，拖累大家",
                              safety=check_safety("我是累赘"))
        self.assertLess(record.snapshot.self_worth, NEUTRAL)

    def test_everyday_negative_phrases_lower_valence(self):
        """回归：日常负向表达（尤其"累""睡不着"）必须真的影响 valence。

        背景：初版词表把"累/睡不着"只放在 energy 维度里，于是
        「最近好累，什么都不想做，也睡不着」这种最常见的表达
        **完全不改变 valence**（停在 0.5）——实跑才暴露。
        """
        for text in ("最近好累", "今天真的很累", "昨晚一直睡不着",
                     "最近心里很难受", "整个人很低落"):
            record = EmotionState().update(text, safety=check_safety(text))
            self.assertLess(record.snapshot.valence, NEUTRAL,
                            f"日常负向表达未影响 valence：{text!r}")

    def test_everyday_positive_phrases_raise_valence(self):
        """回归：好转的表达（"好一点""状态不错"）必须能反映出来。"""
        for text in ("今天好一点了", "今天状态不错", "感觉还行",
                     "跟朋友聊完踏实了一些", "睡好了一觉"):
            record = EmotionState().update(text, safety=check_safety(text))
            self.assertGreater(record.snapshot.valence, NEUTRAL,
                               f"正向表达未影响 valence：{text!r}")

    def test_arousal_moves_both_ways(self):
        hot = EmotionState()
        hot.update("好烦躁，完全失控，坐立不安", safety=check_safety("烦躁"))
        flat = EmotionState()
        flat.update("很麻木，什么都不想动", safety=check_safety("麻木"))
        self.assertGreater(hot.arousal, flat.arousal)

    def test_sustained_decline_is_detected(self):
        state = EmotionState()
        feed(state, NEGATIVE_LINES)
        self.assertLess(state.valence, NEUTRAL)
        self.assertTrue(state.records[-1].sustained_decline,
                        "连续下滑必须被识别：" + state.records[-1].to_text())
        self.assertTrue(state.records[-1].needs_check_in)

    def test_single_bad_turn_is_not_sustained_decline(self):
        state = EmotionState()
        record = state.update("今天很难过", safety=check_safety("今天很难过"))
        self.assertFalse(record.sustained_decline)

    def test_recovery_clears_decline(self):
        state = EmotionState()
        feed(state, NEGATIVE_LINES)
        feed(state, POSITIVE_LINES + POSITIVE_LINES)
        self.assertFalse(state.records[-1].sustained_decline,
                         "好转之后不应继续报'持续下滑'")

    def test_trend_reported(self):
        state = EmotionState()
        feed(state, NEGATIVE_LINES)
        self.assertIn(state.records[-1].valence_trend, ("down", "flat"))


class TestSerialization(unittest.TestCase):
    def test_round_trip(self):
        state = EmotionState()
        feed(state, NEGATIVE_LINES + POSITIVE_LINES)
        restored = EmotionState.from_dict(state.to_dict())
        self.assertEqual(restored.turn, state.turn)
        for name in DIMENSIONS:
            self.assertAlmostEqual(restored.dims[name], state.dims[name], places=4)

    def test_snapshot_round_trip(self):
        state = EmotionState()
        record = state.update("很累", safety=check_safety("很累"))
        snapshot = StateSnapshot.from_dict(record.snapshot.to_dict())
        self.assertEqual(snapshot.turn, record.snapshot.turn)
        self.assertAlmostEqual(snapshot.get("valence"),
                               record.snapshot.get("valence"), places=4)

    def test_from_dict_tolerates_missing_and_out_of_range(self):
        snapshot = StateSnapshot.from_dict({"dims": {"valence": 9, "arousal": -3}})
        self.assertAlmostEqual(snapshot.get("valence"), 1.0)
        self.assertAlmostEqual(snapshot.get("arousal"), 0.0)
        self.assertAlmostEqual(snapshot.get("energy"), NEUTRAL)

    def test_restore(self):
        source = EmotionState()
        feed(source, NEGATIVE_LINES)
        target = EmotionState()
        target.restore(StateSnapshot.from_dict(source.records[-1].snapshot.to_dict()))
        self.assertAlmostEqual(target.valence, source.valence, places=4)


class TestLlmTrackFallback(unittest.TestCase):
    """③ 降级链：模型轨任何异常都不许炸链路。"""

    def test_validator_rejects_incomplete_payload(self):
        self.assertIsNone(validate_llm_payload(None))
        self.assertIsNone(validate_llm_payload("字符串"))
        self.assertIsNone(validate_llm_payload({"valence": 0.2}))          # 缺维度
        self.assertIsNone(validate_llm_payload({**{k: 0.5 for k in DIMENSIONS},
                                                "valence": "很痛苦"}))    # 不可解析

    def test_validator_clamps_out_of_range(self):
        payload = {k: 0.5 for k in DIMENSIONS}
        payload.update({"valence": 5, "arousal": -5, "confidence": 3})
        result = validate_llm_payload(payload)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["valence"], 1.0)
        self.assertAlmostEqual(result["arousal"], 0.0)
        self.assertAlmostEqual(result["confidence"], 1.0)

    def test_extractor_returns_none_without_model(self):
        class Broken:
            def invoke(self, *_args, **_kwargs):
                raise RuntimeError("网络挂了")

        extractor = state_module.LLMStateExtractor(model=Broken())
        self.assertIsNone(extractor.extract("今天很累"), "模型异常必须回落到 None")

    def test_extractor_parses_fenced_json(self):
        class Fake:
            def invoke(self, *_args, **_kwargs):
                body = ('```json\n{"valence":0.2,"arousal":0.7,"dominance":0.3,'
                        '"energy":0.2,"social_support":0.3,"self_worth":0.2,'
                        '"alliance":0.6,"disclosure":0.8,"confidence":0.9}\n```')

                class Response:
                    content = body
                return Response()

        extractor = state_module.LLMStateExtractor(model=Fake())
        result = extractor.extract("今天很累")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["valence"], 0.2)

    def test_llm_track_can_be_injected_for_offline_reproduction(self):
        state = EmotionState()
        payload = {k: NEUTRAL for k in DIMENSIONS}
        payload.update({"valence": 0.05, "confidence": 0.9})
        record = state.update("今天还行", safety=check_safety("今天还行"), llm_state=payload)
        self.assertEqual(record.snapshot.source, "merged")
        self.assertLess(record.snapshot.valence, NEUTRAL)

    def test_disabled_llm_track_uses_rules(self):
        state = EmotionState(use_llm=False)
        record = state.update("很难过", safety=check_safety("很难过"))
        self.assertEqual(record.snapshot.source, "rules")


class TestMergeTracks(unittest.TestCase):
    def test_low_confidence_is_discarded(self):
        merged, source = merge_tracks({"valence": -0.2},
                                      {"valence": 0.9, "confidence": 0.1})
        self.assertEqual(source, "rules")
        self.assertAlmostEqual(merged["valence"], -0.2)

    def test_negative_correction_is_kept_fully(self):
        merged, source = merge_tracks({}, {"valence": 0.1, "confidence": 1.0})
        self.assertEqual(source, "merged")
        self.assertLess(merged["valence"], 0.0)

    def test_positive_correction_is_halved(self):
        merged, _ = merge_tracks({}, {"valence": 0.9, "confidence": 1.0})
        self.assertLess(merged["valence"], 0.4 * 1.0)
        self.assertGreater(merged["valence"], 0.0)

    def test_missing_llm_state_returns_rules(self):
        merged, source = merge_tracks({"valence": -0.1}, None)
        self.assertEqual(source, "rules")
        self.assertAlmostEqual(merged["valence"], -0.1)


class TestRuleTrack(unittest.TestCase):
    def test_evidence_contains_dictionary_words_not_raw_sentence(self):
        deltas, evidence = RuleTrack().extract("我觉得自己就是个累赘，拖累大家")
        self.assertTrue(deltas)
        words = {w for values in evidence.values() for w in values}
        self.assertEqual(validate_evidence(evidence), [],
                         f"证据里混进了非词典词：{words}")
        self.assertTrue(words.issubset(ALLOWED_EVIDENCE_WORDS))
        # 子串命中是预期行为（"拖累" 会带出词典里的 "累"），但它仍必须是词典词
        self.assertIn("累赘", words)

    def test_evidence_never_contains_sentence_fragments(self):
        """隐私不变量：无论输入什么，证据只可能是词典枚举值。"""
        for text in ("我住在某某小区3号楼，觉得特别累",
                     "身份证号是110101199001011234，我很难过",
                     "asdfghjkl 我觉得没意思"):
            _, evidence = RuleTrack().extract(text)
            self.assertEqual(validate_evidence(evidence), [],
                             f"证据越界：{text!r} -> {evidence}")

    def test_empty_input_yields_nothing(self):
        for value in (None, "", "   "):
            deltas, evidence = RuleTrack().extract(value)
            self.assertEqual(deltas, {})
            self.assertEqual(evidence, {})

    def test_negated_positive_is_not_positive(self):
        deltas, _ = RuleTrack().extract("一点都不开心")
        self.assertLessEqual(deltas.get("valence", 0.0), 0.0)

    def test_extract_is_pure(self):
        track = RuleTrack()
        first = track.extract("很累，很焦虑")
        second = track.extract("很累，很焦虑")
        self.assertEqual(first, second)


class TestHelpers(unittest.TestCase):
    def test_clamp01(self):
        self.assertEqual(clamp01(-1), 0.0)
        self.assertEqual(clamp01(2), 1.0)
        self.assertEqual(clamp01("bad"), NEUTRAL)
        self.assertEqual(clamp01(float("nan")), NEUTRAL)

    def test_trend_of(self):
        self.assertEqual(trend_of([0.5]), "unknown")
        self.assertEqual(trend_of([0.5, 0.4]), "down")
        self.assertEqual(trend_of([0.4, 0.5]), "up")
        self.assertEqual(trend_of([0.5, 0.501]), "flat")

    def test_detect_sustained_decline(self):
        self.assertTrue(detect_sustained_decline([0.8, 0.6, 0.4, 0.2]))
        self.assertFalse(detect_sustained_decline([0.4]))
        self.assertFalse(detect_sustained_decline([0.4, 0.8, 0.5]))
        self.assertFalse(detect_sustained_decline([0.5, 0.48, 0.46]))

    def test_llm_prompt_is_message_list(self):
        messages = build_llm_prompt("今天很累")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("JSON", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "今天很累")


if __name__ == "__main__":
    unittest.main()
