# -*- coding: utf-8 -*-
"""
tests/test_chat_safety.py —— 安全闸的回归保护（P0，纯标准库）

这是本项目**风险最高**的一组测试：其余模块出错顶多是答案不准，
而安全闸漏报的代价不可逆。因此断言刻意分成两类：

    ① 高危**零漏报**：一批真实口语化的危机表述，必须一条不落地被判为 HIGH；
    ② 误报控制：日常口语（"笑死""累并快乐着""不想上班"）必须不被判成危机。

另外钉住三条设计不变量，防止以后被"优化"掉：
    - 高危必须阻断自由发挥（`blocks_freeform_llm`）；
    - 判定结果**不得携带用户原文**（`to_dict()` 里不许出现输入句子）；
    - 高危/中危话术必须包含可拨打的求助通道与"非专业帮助"声明。

    python tests/run_all.py
    python -m unittest discover -s tests -v
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

from chat import safety  # noqa: E402
from chat.safety import (  # noqa: E402
    CRISIS_DISCLAIMER,
    EMERGENCY_LINE,
    HOTLINES,
    SafetyLevel,
    build_safe_reply,
    check_safety,
    format_hotlines,
    normalize_text,
    validate_rules,
)


# ---------------------------------------------------------------------------
# ① 高危零漏报：这些句子必须全部判为 HIGH
# ---------------------------------------------------------------------------
MUST_BE_HIGH = (
    # 意念
    "我不想活了",
    "活着没意思",
    "我想死",
    "觉得活着没意义，不如死了算了",
    "可能结束自己的生命会好一点",
    "不想再活下去了",
    # 自伤
    "我有时候会自残",
    "昨晚又割腕了",
    "一直在伤害自己",
    # 计划 / 告别 / 手段
    "我已经写好遗书了",
    "准备把后事交代一下",
    "东西都送人了，想跟你们告别一下",
    "攒了很多安眠药",
    "想从天台上跳下去",
    "谢谢大家一直以来的照顾",
)


# ---------------------------------------------------------------------------
# ② 日常口语：不得被判为 HIGH / MEDIUM（这套规则的全部价值就在这条边界上）
# ---------------------------------------------------------------------------
MUST_NOT_BE_CRISIS = (
    "笑死我了",
    "累并快乐着",
    "烦死了，这个需求又改了",
    "不想上班",
    "今天好累啊，先睡了",
)


class TestHighRiskRecall(unittest.TestCase):
    """① 高危零漏报。"""

    def test_all_must_be_high(self):
        missed = [text for text in MUST_BE_HIGH
                  if check_safety(text).level is not SafetyLevel.HIGH]
        self.assertEqual(missed, [], "以下高危表述被漏报（必须修复规则）：\n" + "\n".join(missed))

    def test_high_blocks_freeform_llm(self):
        for text in MUST_BE_HIGH:
            hit = check_safety(text)
            self.assertTrue(hit.blocks_freeform_llm,
                            f"高危必须阻断模型自由发挥：{text!r} -> {hit.to_dict()}")

    def test_high_reply_contains_help_channels(self):
        hit = check_safety("我不想活了")
        reply = build_safe_reply(hit)
        self.assertIsNotNone(reply)
        self.assertIn("12356", reply, "高危话术必须给出全国统一心理援助热线")
        self.assertIn("010-82951332", reply, "高危话术必须给出 24 小时热线")
        self.assertIn("120", reply, "高危话术必须包含急救引导")
        # 高危必须引导到真人，而不是承诺"我陪你就能解决"
        self.assertIn("真人", reply)


class TestNegationHandling(unittest.TestCase):
    """否定词必须能把高危压回——否则真正的求助者会被误报淹没。"""

    def test_negated_ideation_is_not_high(self):
        for text in ("我绝对不会自杀的",
                     "我从来没有想过自杀",
                     "我不是想死，我只是太累了",
                     "谈不上不想活，就是有点丧"):
            hit = check_safety(text)
            self.assertIsNot(hit.level, SafetyLevel.HIGH,
                             f"否定表述被误判为高危：{text!r} -> {hit.to_dict()}")

    def test_negation_is_local_not_global(self):
        """否定只应作用于它紧邻的那处命中，不能把整句话的危机信号一起压掉。"""
        hit = check_safety("我不会自杀，但我真的不想活了")
        self.assertIs(hit.level, SafetyLevel.HIGH,
                      "局部队列里的高危信号被否定词整体压掉了")

    def test_negated_count_is_reported(self):
        hit = check_safety("我绝对不会自杀")
        self.assertGreaterEqual(hit.negation_filtered, 1)
        self.assertEqual(hit.hit_count, 0)


class TestExpandedSuicidePattern(unittest.TestCase):
    """回归：「绝对不会自杀」里的"不"必须被记为一次否定过滤，而不是完全没匹配。

    背景：旧规则只认「想自杀」，而"绝对不会自杀"里"绝对不会"后面**没有**"想"，
    于是整条规则没匹配上，否定过滤计数恒为 0——否定逻辑看起来在，
    实际上从未在这类句子上生效过。这是靠实跑才发现的。
    """

    def test_bare_suicide_word_is_scanned_and_filtered(self):
        hit = check_safety("我绝对不会自杀")
        self.assertIsNot(hit.level, SafetyLevel.HIGH)
        self.assertGreaterEqual(hit.negation_filtered, 1,
                                "否定过滤必须真的被触发过，否则等于没有否定逻辑")

    def test_bare_suicide_word_without_negation_is_high(self):
        for text in ("自杀", "我想过自杀这件事", "关于自杀的念头一直有"):
            self.assertIs(check_safety(text).level, SafetyLevel.HIGH,
                          f"缺少「想自杀」形态的真实表述被漏报：{text!r}")


class TestMethodAndPlanSignals(unittest.TestCase):
    """回归：提到「方法 / 具体怎么结束」属于计划信号，不能被低估成普通负向情绪。

    注意：本测试刻意**不**包含任何具体手段的描述——系统不需要、
    也不应该去识别"哪种方式更不痛苦"这类内容，那只会带来风险而没有收益。
    """

    def test_method_language_is_high(self):
        for text in ("查过自杀的方法",
                     "想过具体怎么结束",
                     "已经在想用什么方式了"):
            self.assertIs(check_safety(text).level, SafetyLevel.HIGH,
                          f"计划/手段类表述被低估：{text!r}")


class TestLowRiskAdverbVariants(unittest.TestCase):
    """回归：中文里副词可以插在中间（压力**也**很大），规则必须覆盖。

    背景（实跑抓到）：原规则写成「压力(?:很|好|太)?大」，
    于是「很焦虑，压力也很大」被判成 NONE，「压力很大」才是 LOW ——
    同一个意思，只差一个"也"字，关心就丢了。
    """

    def test_adverb_between_word_and_intensifier(self):
        for text in ("压力也很大", "压力都大", "压力还挺大", "压力很大"):
            self.assertIs(check_safety(text + "，很焦虑").level, SafetyLevel.LOW,
                          f"副词变体未被识别：{text}")

    def test_single_low_word_still_needs_two_hits(self):
        """补漏不能把噪音控制搞坏：单个词仍然不触发。"""
        for text in ("焦虑", "有点焦虑", "今天很焦虑"):
            self.assertIs(check_safety(text).level, SafetyLevel.NONE,
                          f"单个低危词不该触发：{text}")


class TestFalsePositiveControl(unittest.TestCase):
    """② 误报控制。"""

    def test_casual_phrases_are_not_high(self):
        for text in MUST_NOT_BE_CRISIS:
            hit = check_safety(text)
            self.assertIsNot(hit.level, SafetyLevel.HIGH,
                             f"日常口语被误判为高危：{text!r} -> {hit.to_dict()}")

    def test_casual_phrases_are_not_medium(self):
        for text in ("笑死我了", "累并快乐着", "不想上班", "今天好累啊，先睡了"):
            hit = check_safety(text)
            self.assertIsNot(hit.level, SafetyLevel.MEDIUM,
                             f"日常口语被误判为中危：{text!r} -> {hit.to_dict()}")

    def test_single_low_word_triggers_low(self):
        """低危需要 ≥2 次命中；单个"焦虑"恰好是 1 次，故显式传 low_min_hits=1 验证边界。"""
        self.assertIs(check_safety("焦虑", low_min_hits=1).level, SafetyLevel.LOW)

    def test_low_hits_below_threshold_are_ignored(self):
        # "有点焦虑"：只有 1 次命中 + 减弱词，按噪音控制口径不触发低危
        self.assertIs(check_safety("有点焦虑").level, SafetyLevel.NONE)

    def test_neutral_text_is_none(self):
        for text in ("你好", "今天下午三点开会", "帮我查一下明天的天气", ""):
            self.assertIs(check_safety(text).level, SafetyLevel.NONE,
                          f"中性文本被误判：{text!r}")


class TestMediumRisk(unittest.TestCase):
    def test_medium_cases(self):
        for text in ("最近真的撑不下去了",
                     "我觉得自己就是个累赘，拖累大家",
                     "对什么都没有感觉，像行尸走肉",
                     "整天睡不着，也吃不下饭"):
            self.assertIs(check_safety(text).level, SafetyLevel.MEDIUM,
                          f"中危信号未被识别：{text!r}")

    def test_medium_reply_contains_channels_and_disclaimer(self):
        hit = check_safety("我是不是个累赘，拖累大家")
        self.assertIs(hit.level, SafetyLevel.MEDIUM)
        reply = build_safe_reply(hit)
        self.assertIn("12356", reply)
        self.assertIn(CRISIS_DISCLAIMER, reply)
        self.assertFalse(hit.blocks_freeform_llm, "中危允许模型参与，但必须带求助通道")

    def test_low_reply_has_disclaimer(self):
        hit = check_safety("很焦虑，压力很大")
        self.assertIs(hit.level, SafetyLevel.LOW)
        self.assertIn(CRISIS_DISCLAIMER, build_safe_reply(hit))

    def test_none_has_no_safe_reply(self):
        self.assertIsNone(build_safe_reply(check_safety("你好")))


class TestOutputIsSafeToPersistOrRender(unittest.TestCase):
    """判定结果不得携带用户原文——它可能被写日志或渲染到公网页面。"""

    def test_to_dict_does_not_contain_user_text(self):
        secret = "我不想活了，我的手机号是13800000000"
        hit = check_safety(secret)
        payload = hit.to_dict()
        blob = repr(payload)
        self.assertNotIn("我不想活了", blob)
        self.assertNotIn("13800000000", blob)
        self.assertIn("H-INTENT-SUICIDE", payload["rule_ids"])

    def test_hit_has_no_raw_text_field(self):
        hit = check_safety("我不想活了")
        for attr in ("text", "raw", "matched", "snippet", "content"):
            self.assertFalse(hasattr(hit, attr), f"SafetyHit 不应暴露原文属性：{attr}")


class TestRuleTableIntegrity(unittest.TestCase):
    def test_validate_rules_passes(self):
        self.assertEqual(validate_rules(), [])

    def test_rule_ids_unique(self):
        ids = [rule.rule_id for rule in safety.iter_rules()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_levels_covered(self):
        levels = {rule.level for rule in safety.iter_rules()}
        self.assertEqual(levels, {SafetyLevel.HIGH, SafetyLevel.MEDIUM, SafetyLevel.LOW})

    def test_severity_ordering(self):
        self.assertGreater(SafetyLevel.HIGH.severity, SafetyLevel.MEDIUM.severity)
        self.assertGreater(SafetyLevel.MEDIUM.severity, SafetyLevel.LOW.severity)
        self.assertGreater(SafetyLevel.LOW.severity, SafetyLevel.NONE.severity)


class TestHotlineResources(unittest.TestCase):
    def test_every_hotline_has_source(self):
        self.assertTrue(HOTLINES)
        for item in HOTLINES:
            self.assertTrue(item["number"].strip())
            self.assertTrue(item["source"].strip(), "热线必须记录出处，便于复核")

    def test_cn_and_other_region(self):
        cn = format_hotlines("cn")
        self.assertIn("12356", cn)
        other = format_hotlines("other")
        self.assertNotIn("12356", other)
        self.assertIn("急救", other)

    def test_emergency_line_mentions_120(self):
        self.assertIn("120", EMERGENCY_LINE)


class TestNormalizeText(unittest.TestCase):
    def test_fullwidth_punctuation_is_normalized(self):
        self.assertEqual(normalize_text("你好，世界！"), "你好,世界!")

    def test_ellipsis_is_normalized(self):
        self.assertIn("...", normalize_text("我……不知道"))

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize_text("a   \n b"), "a b")

    def test_empty_inputs(self):
        for value in (None, "", "   "):
            self.assertEqual(normalize_text(value), "")


class TestIntensityWeighting(unittest.TestCase):
    def test_diminisher_lowers_intensity(self):
        weak = check_safety("有点难过")
        strong = check_safety("非常难过")
        self.assertLess(weak.intensity, strong.intensity)

    def test_plain_word_has_neutral_intensity(self):
        self.assertAlmostEqual(check_safety("难过").intensity, 1.0, places=3)

    def test_check_safety_never_raises_on_odd_input(self):
        for value in (None, "", 123, "🙂", "a" * 5000, ["列表"]):
            hit = check_safety(value)
            self.assertIsInstance(hit.level, SafetyLevel)


if __name__ == "__main__":
    unittest.main()
