# -*- coding: utf-8 -*-
"""
tests/test_chat_tools.py —— 工具层的回归保护（S2，纯标准库）
================================================================

本文件里**最重要**的一条不是"工具能不能用"，而是：

    ★ 高危路径绝不允许发生任何模型调用与工具调用。

因为工具调用会把用户原话交给模型与第三方接口，而自伤表述
绝不能被二次送进外部服务。这条红线用"模型被调用就爆炸"的替身来验证。

其余覆盖：
    ① 天气工具复用 MCP 服务端逻辑，且**永不抛异常**；
    ② 翻译的语言解析与错误处理（不真联网，只测纯函数与失败分支）；
    ③ 工具阶段编排：绑定失败退化、工具结果回填、超额截断、未知工具；
    ④ 工具说明对用户透明（能看出用了什么、结果是什么）。

只在确需联网的用例上跳过（沙箱/断网环境不伪装成通过）。

    python tests/run_all.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import conversation as convo  # noqa: E402
from chat import tools as tl  # noqa: E402


class TestSafetyRedLine(unittest.TestCase):
    """★ 高危 = 零模型调用、零工具调用。

    注意：这里**不能**只断言 plan_reply 的结果——那证明不了"没被调用"，
    写成那样就是一条自我安慰的假测试。真正的证明在页面级测试里
    （tests/test_chat_app_page.py::test_high_risk_never_calls_model），
    它给 make_model 装哨兵并跑完整的 HIGH 流程。
    """

    def test_high_risk_uses_fixed_reply_with_resources(self):
        plan = convo.plan_reply("我不想活了")
        self.assertTrue(plan.use_safe_reply)
        self.assertTrue(plan.hit.blocks_freeform_llm)
        self.assertIsNotNone(plan.safe_reply)
        self.assertIn("12356", plan.safe_reply)

    def test_run_tool_phase_defers_safety_to_caller(self):
        """run_tool_phase 不做安全判定（它只看得到已拼好的 messages）。

        所以调用方必须先判——这条注释是契约，页面级测试负责验证它被遵守。
        """
        class ExplodingModel:
            def bind_tools(self, *_a, **_k):
                raise AssertionError("不该绑定工具")

            def invoke(self, *_a, **_k):
                raise AssertionError("不该调用模型")

        plan = convo.plan_reply("我已经写好遗书了")
        phase = convo.ToolPhase()
        if not plan.use_safe_reply:                      # ← 真实代码里的同一个守卫
            phase = convo.run_tool_phase(ExplodingModel(), [])
        self.assertFalse(phase.used_tools)
        self.assertEqual(phase.calls, [])


class TestWeatherTool(unittest.TestCase):
    """① 天气：离线降级路径 + 永不抛异常。

    注意：默认天气源已经是 **Open-Meteo 全球真实数据**（需联网），
    所以这一层只测**离线降级路径**（offline_only=True），保证测试
    在没有网络时也能确定性地验证"工具永远给得出可读结果"。
    真实数据路径在 tests/test_chat_weather.py 里单独测（无网时 SKIP）。
    """

    def _offline(self, city):
        return tl.lookup_weather_text(city, offline_only=True)

    def test_normalize_city(self):
        self.assertEqual(tl.normalize_city("  上海 "), "上海")
        self.assertEqual(tl.normalize_city(None), "")
        self.assertEqual(tl.normalize_city(123), "123")

    def test_known_city_returns_description(self):
        text = self._offline("上海")
        self.assertIn("上海", text)
        self.assertNotIn("出错", text)

    def test_offline_result_is_labeled_as_mock(self):
        """降级数据必须**明确标注**是模拟的，不能让用户误以为是真实天气。"""
        text = self._offline("上海")
        self.assertIn("模拟", text, "离线降级必须标注这是模拟数据")

    def test_empty_city_asks_for_one(self):
        self.assertIn("城市", self._offline(""))

    def test_unknown_city_is_explained_not_raised(self):
        self.assertIn("不存在的城市xyz", self._offline("不存在的城市xyz"))

    def test_never_raises_on_weird_input(self):
        for value in (None, "", "   ", 0, [], {"a": 1}, "🔥", "a" * 500):
            text = self._offline(value)
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())


class TestTranslatePure(unittest.TestCase):
    """② 翻译：语言解析与输入校验（不联网）。"""

    def test_resolve_language_code(self):
        self.assertEqual(tl.resolve_language_code("日语"), "ja")
        self.assertEqual(tl.resolve_language_code("Japanese"), "ja")
        self.assertEqual(tl.resolve_language_code("JA"), "ja")
        self.assertEqual(tl.resolve_language_code("英语"), "en")
        self.assertIsNone(tl.resolve_language_code("火星语"))
        self.assertIsNone(tl.resolve_language_code(None))

    def test_detect_source_lang(self):
        self.assertEqual(tl.detect_source_lang("你好"), "zh")
        self.assertEqual(tl.detect_source_lang("hello"), "en")
        self.assertEqual(tl.detect_source_lang(""), "en")

    def test_empty_text_is_rejected_politely(self):
        self.assertIn("没有需要翻译", tl.translate_text(""))
        self.assertIn("没有需要翻译", tl.translate_text(None))

    def test_unknown_target_language_is_not_guessed(self):
        text = tl.translate_text("你好", "火星语")
        self.assertIn("火星语", text)
        self.assertIn("没有这门语言的可靠数据", text)
        self.assertIn("参考译法，未经母语者核对", text)

    def test_same_language_short_circuits(self):
        text = tl.translate_text("你好", "中文")
        self.assertIn("不需要翻译", text)

    def test_translate_never_raises(self):
        for target in (None, "", "ja", "火星语", 123):
            text = tl.translate_text("hello", target)
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())


class TestAgentTools(unittest.TestCase):
    def test_two_tools_with_expected_names(self):
        self.assertEqual(set(tl.AGENT_TOOL_NAMES),
                         {tl.WEATHER_TOOL_NAME, tl.TRANSLATE_TOOL_NAME})

    def test_tool_by_name(self):
        self.assertIsNotNone(tl.tool_by_name(tl.WEATHER_TOOL_NAME))
        self.assertIsNotNone(tl.tool_by_name(tl.TRANSLATE_TOOL_NAME))
        self.assertIsNone(tl.tool_by_name("不存在的工具"))

    def test_tools_have_description_for_the_model(self):
        for item in tl.AGENT_TOOLS:
            self.assertTrue(getattr(item, "description", "").strip(),
                            f"{item} 缺少 description，模型无法判断何时调用")


class TestToolPhaseOrchestration(unittest.TestCase):
    """③ 工具阶段编排（用替身模型，不真调 LLM）。"""

    def _model_that_calls(self, calls, *, fail_bind=False, exploding=False):
        class FakeModel:
            def bind_tools(self, _tools):
                if fail_bind:
                    raise RuntimeError("不支持绑定工具")
                return self

            def invoke(self, messages):
                if exploding:
                    raise AssertionError("不该调用模型")
                return {"content": "", "tool_calls": calls}

        return FakeModel()

    def test_bind_failure_degrades_to_plain_chat(self):
        model = self._model_that_calls([], fail_bind=True)
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "hi"}])
        self.assertFalse(phase.used_tools)
        self.assertEqual(phase.messages, [])

    def test_model_invoke_failure_degrades(self):
        model = self._model_that_calls([], exploding=True)
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "hi"}])
        self.assertFalse(phase.used_tools)

    def test_no_tool_call_means_no_phase(self):
        model = self._model_that_calls([])
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "你好"}])
        self.assertFalse(phase.used_tools)
        self.assertEqual(phase.extra_messages, [])

    def test_weather_call_is_executed_and_fed_back(self):
        model = self._model_that_calls([
            {"name": tl.WEATHER_TOOL_NAME, "args": {"city": "上海"}, "id": "c1"},
        ])
        base = [{"role": "user", "content": "上海天气怎么样"}]
        phase = convo.run_tool_phase(model, base)

        self.assertTrue(phase.used_tools)
        self.assertEqual(len(phase.calls), 1)
        call = phase.calls[0]
        self.assertEqual(call.name, tl.WEATHER_TOOL_NAME)
        self.assertEqual(call.arguments, {"city": "上海"})
        self.assertTrue(call.ok)
        self.assertIn("上海", call.result)

        # 工具结果必须回填进 messages，模型才能据此回答
        roles = [m.get("role") for m in phase.messages]
        self.assertEqual(roles[0], "user")
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        tool_msg = [m for m in phase.messages if m.get("role") == "tool"][0]
        self.assertIn("上海", tool_msg["content"])
        self.assertEqual(tool_msg["tool_call_id"], "c1")
        # extra_messages 不含基座消息
        self.assertEqual(len(phase.extra_messages), len(phase.messages) - 1)

    def test_call_count_is_capped(self):
        many = [{"name": tl.WEATHER_TOOL_NAME, "args": {"city": f"c{i}"}, "id": f"c{i}"}
                for i in range(10)]
        model = self._model_that_calls(many)
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "x"}],
                                     max_tools=2)
        self.assertEqual(len(phase.calls), 2)

    def test_unknown_tool_is_reported_not_raised(self):
        model = self._model_that_calls([
            {"name": "不存在的工具", "args": {}, "id": "c1"},
        ])
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "x"}])
        self.assertTrue(phase.used_tools)
        self.assertFalse(phase.calls[0].ok)
        self.assertIn("unknown-tool", phase.calls[0].error)

    def test_object_style_tool_calls_are_supported(self):
        """兼容 OpenAI 风格的对象（.name/.args/.id）而不是 dict。"""
        class Call:
            name = tl.WEATHER_TOOL_NAME
            args = {"city": "北京"}
            id = "obj-1"

        class FakeModel:
            def bind_tools(self, _tools):
                return self

            def invoke(self, _messages):
                return type("R", (), {"content": "", "tool_calls": [Call()]})()

        phase = convo.run_tool_phase(FakeModel(), [{"role": "user", "content": "x"}])
        self.assertEqual(len(phase.calls), 1)
        self.assertIn("北京", phase.calls[0].result)

    def test_disclosure_is_human_readable(self):
        model = self._model_that_calls([
            {"name": tl.WEATHER_TOOL_NAME, "args": {"city": "上海"}, "id": "c1"},
        ])
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "x"}])
        text = convo.tool_disclosure(phase)
        self.assertIsNotNone(text)
        self.assertIn(tl.WEATHER_TOOL_NAME, text)
        self.assertIn("上海", text)

    def test_disclosure_is_none_without_tools(self):
        self.assertIsNone(convo.tool_disclosure(convo.ToolPhase()))

    def test_disclosure_marks_failures(self):
        model = self._model_that_calls([
            {"name": "不存在的工具", "args": {}, "id": "c1"},
        ])
        phase = convo.run_tool_phase(model, [{"role": "user", "content": "x"}])
        text = convo.tool_disclosure(phase)
        self.assertIn("没能成功", text)

    def test_call_label_has_no_user_text(self):
        call = convo.ToolCall(name="t", arguments={"city": "上海"}, result="ok")
        self.assertEqual(call.label(), "t(city=上海)")


class TestBindToolsFallback(unittest.TestCase):
    def test_unbindable_model_returns_none(self):
        class NoBind:
            pass

        self.assertIsNone(convo.bind_tools(NoBind()))

    def test_empty_tool_list_returns_none(self):
        class Any:
            def bind_tools(self, _tools):  # pragma: no cover - 不该被调用
                return self

        self.assertIsNone(convo.bind_tools(Any(), tools=[]))


class TestStructuredToolStatus(unittest.TestCase):
    """C：工具状态必须能传到上层，而不是只剩一段文本。"""

    def test_translate_outcome_keeps_unsupported_status(self):
        outcome = tl.translate_outcome("超标", "迈蒂利语")
        self.assertEqual(outcome.status, "unsupported-language")
        self.assertIn("参考译法，未经母语者核对", outcome.text)

    def test_invoke_tool_returns_status_as_third_item(self):
        text, error, status = convo._invoke_tool(
            tl.TRANSLATE_TOOL_NAME,
            {"text": "超标", "target_lang": "迈蒂利语"},
        )
        self.assertIn("参考译法", text)
        self.assertEqual(error, "")
        self.assertEqual(status, "unsupported-language")

    def test_tool_call_dict_exposes_status(self):
        call = convo.ToolCall(name=tl.TRANSLATE_TOOL_NAME,
                              arguments={"text": "超标"},
                              result="参考", status="unsupported-language")
        self.assertEqual(call.to_dict()["status"], "unsupported-language")

    def test_tools_without_status_semantics_return_none(self):
        self.assertIsNone(tl.tool_outcome(tl.WEATHER_TOOL_NAME, {"city": "上海"}))


if __name__ == "__main__":
    unittest.main()
