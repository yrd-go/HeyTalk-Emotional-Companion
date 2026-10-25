# -*- coding: utf-8 -*-
"""
tests/test_chat_translation.py —— 语言覆盖与多引擎翻译（S2 升级，纯标准库）
================================================================================

分三层，刻意分开：

    语言层（永远可测）：90 种语言能被代码/中文名/英文名/当地写法解析；
        韩文、日文、阿拉伯文、泰文等**书写系统**能被认出来
        （旧版把除中文外的一切都当英语，这是"只局限某几个语言"的一部分根因）。

    引擎层（永远可测）：**降级链**用引擎替身验证——
        Google 失败要落到 MyMemory、全失败要给可操作提示、
        缓存命中不再请求、超长文本明确拒绝。
        用替身而不是靠网络，才能确定性地覆盖失败分支。

    联网层（无网 SKIP）：真的能翻译。

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

from chat import languages as lang  # noqa: E402
from chat import translation as tr  # noqa: E402


class TestLanguageRegistry(unittest.TestCase):
    """语言层：覆盖度与解析。"""

    def test_registry_is_meaningfully_large(self):
        count = lang.language_count()
        self.assertGreaterEqual(count, 80,
                                f"语言覆盖太少（{count} 种），做不到'不局限某几个语言'")

    def test_resolve_by_code(self):
        for code in ("ja", "en", "fr", "ar", "sw", "yue", "zh-TW"):
            self.assertEqual(lang.resolve(code), code, f"代码 {code} 无法解析")

    def test_resolve_by_chinese_name(self):
        cases = {"日语": "ja", "英语": "en", "法语": "fr", "斯瓦希里语": "sw",
                 "冰岛语": "is", "僧伽罗语": "si", "毛利语": "mi", "海地克里奥尔语": "ht"}
        for name, expected in cases.items():
            self.assertEqual(lang.resolve(name), expected, f"中文名 {name} 未识别")

    def test_resolve_by_english_name(self):
        cases = {"Japanese": "ja", "Kiswahili": None, "Icelandic": "is",
                 "Sinhala": "si", "Haitian Creole": "ht", "Maori": "mi",
                 "Nederlands": "nl", "Esperanto": "eo"}
        for name, expected in cases.items():
            if expected is None:
                continue
            self.assertEqual(lang.resolve(name), expected, f"英文名 {name} 未识别")

    def test_resolve_by_native_spelling(self):
        cases = {"日本語": "ja", "한국어": "ko", "Français": "fr",
                 "Deutsch": "de", "فارسی": "fa", "עברית": "he",
                 "ไทย": "th", "Tiếng Việt": "vi", "Ελληνικά": "el",
                 "Українська": "uk", "粵語": "yue"}
        for name, expected in cases.items():
            self.assertEqual(lang.resolve(name), expected, f"当地写法 {name} 未识别")

    def test_aliases_and_case_insensitivity(self):
        for name, expected in (("JP", "ja"), ("jpn", "ja"), ("Mandarin", "zh"),
                               ("广东话", "yue"), ("美式英语", "en"),
                               ("  Japanese  ", "ja"), ("CHINESE", "zh"),
                               ("bahasa", "id")):
            self.assertEqual(lang.resolve(name), expected, f"别称 {name} 未识别")

    def test_unknown_language_name_returns_none_not_a_guess(self):
        """语言名不认识就返回 None；代码形态的输入交给引擎（A 策略）。"""
        for name in ("克林贡语", "notalanguage", "", None, 12345):
            self.assertIsNone(lang.resolve(name), f"{name!r} 不该被猜出语言")

    def test_name_of(self):
        self.assertEqual(lang.name_of("ja"), "日语")
        self.assertEqual(lang.name_of("ja", style="en"), "Japanese")
        self.assertEqual(lang.name_of("zzz"), "zzz")

    def test_supported_names_is_listable(self):
        names = lang.supported_names()
        self.assertGreaterEqual(len(names), 80)
        self.assertIn("日语", names)


class TestFallbackPolicy(unittest.TestCase):
    """A+B：注册表不再是闸门；不支持时给“带标注的最佳努力”。"""

    def test_unknown_iso_code_passes_through(self):
        self.assertEqual(lang.resolve("mai"), "mai")
        self.assertEqual(lang.resolve("bho"), "bho")
        self.assertEqual(lang.resolve("zh-tw"), "zh-TW")

    def test_unknown_language_name_is_still_unknown(self):
        self.assertIsNone(lang.resolve("迈蒂利语"))
        self.assertIsNone(lang.resolve("Klingon"))

    def test_unknown_name_reports_machine_readable_status(self):
        result = tr.translate("超标", "迈蒂利语")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unsupported-language")

    def test_known_code_reaches_the_engine(self):
        calls = []

        def fake(text, source, target):
            calls.append((text, source, target))
            return "", "http-429"

        original = tr._ENGINE_CALLS.get("fake")
        tr._ENGINE_CALLS["fake"] = fake
        try:
            result = tr.translate("超标", "mai", engines=("fake",))
        finally:
            if original is None:
                tr._ENGINE_CALLS.pop("fake", None)
            else:
                tr._ENGINE_CALLS["fake"] = original

        self.assertEqual(calls, [("超标", "zh", "mai")])
        self.assertEqual(result.status, "engine-error")

    def test_tool_turns_unsupported_into_annotated_fallback_instruction(self):
        from chat import tools

        note = tools.translate_text("超标", "迈蒂利语")
        self.assertIn("没有这门语言的可靠数据", note)
        self.assertIn("参考译法，未经母语者核对", note)
        self.assertIn("澄清问题", note)

    def test_tool_explains_engine_failure_without_pretending(self):
        from chat import tools

        fake = tr.TranslationResult(
            False, "这次没能翻译成功：mymemory:http-429", status="engine-error")
        original = tr.translate
        tr.translate = lambda text, target_lang=None, **kw: fake
        try:
            note = tools.translate_text("hello", "ja")
        finally:
            tr.translate = original

        self.assertIn("这次没查成", note)
        self.assertIn("未能查证", note)


class TestScriptDetection(unittest.TestCase):
    """书写系统识别：旧版把非中文的一切都当英语。"""

    def test_detects_major_scripts(self):
        cases = {
            "你好世界": "Han",
            "こんにちは": "Hiragana",
            "カタカナ": "Katakana",
            "안녕하세요": "Hangul",
            "مرحبا بالعالم": "Arabic",
            "שלום עולם": "Hebrew",
            "Привет мир": "Cyrillic",
            "Γεια σου": "Greek",
            "สวัสดี": "Thai",
            "नमस्ते": "Devanagari",
        }
        for text, expected in cases.items():
            self.assertEqual(lang.detect_script(text), expected,
                             f"{text!r} 书写系统识别错误")

    def test_guess_source_language(self):
        cases = {
            "你好": "zh", "こんにちは": "ja", "안녕": "ko",
            "مرحبا": "ar", "Привет": "ru", "สวัสดี": "th",
            "hello": "en",
        }
        for text, expected in cases.items():
            self.assertEqual(lang.guess_source_language(text), expected,
                             f"{text!r} 源语言判断错误")

    def test_japanese_with_kanji_is_still_japanese(self):
        """日语常以汉字为主、只夹少量假名 —— 不能被判成中文。

        实测踩过：按"主导书写系统"判断时，「私は学生です」里汉字 3 个、
        假名 3 个（ひらがな + です），结果被判成 zh。
        修法是改用**假名作为决定性证据**（假名是日语独有，中文不会出现）。
        """
        for text in ("私は学生です", "今日はいい天気ですね", "日本語を勉強しています"):
            self.assertEqual(lang.guess_source_language(text), "ja",
                             f"{text!r} 应判为日语")

    def test_empty_and_digit_input_uses_default(self):
        for text in ("", "   ", "12345", "!@#$%"):
            self.assertEqual(lang.guess_source_language(text, default="en"), "en")


class TestEngineChain(unittest.TestCase):
    """引擎层：降级链（用替身，不靠网络）。"""

    def setUp(self) -> None:
        tr.clear_cache()
        self._orig_calls = dict(tr._ENGINE_CALLS)
        self._orig_key = tr._google_key
        self.calls: list[str] = []

    def tearDown(self) -> None:
        tr._ENGINE_CALLS.clear()
        tr._ENGINE_CALLS.update(self._orig_calls)
        tr._google_key = self._orig_key
        tr.clear_cache()

    def _install(self, behaviours: dict[str, tuple[str, str]]) -> None:
        """给各引擎装替身：behaviours[engine] = (译文, 错误)。"""
        def make(engine_id: str):
            def caller(text, source, target):
                self.calls.append(engine_id)
                return behaviours.get(engine_id, ("", "unused"))
            return caller

        tr._ENGINE_CALLS.clear()
        for engine_id in behaviours:
            tr._ENGINE_CALLS[engine_id] = make(engine_id)

    def test_success_on_first_engine(self):
        self._install({"google": ("Hello", ""), "mymemory": ("WRONG", "")})
        result = tr.translate("你好", "en")
        self.assertTrue(result.ok)
        self.assertEqual(result.engine, "google")
        self.assertEqual(result.translated, "Hello")
        self.assertEqual(self.calls, ["google"], "第一个引擎成功就不该再调别的")

    def test_falls_back_to_second_engine(self):
        """★ 关键：Google 失败必须落到 MyMemory。"""
        self._install({"google": ("", "http-403"), "mymemory": ("Hello", "")})
        result = tr.translate("你好", "en")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.engine, "mymemory")
        self.assertEqual(self.calls, ["google", "mymemory"])

    def test_all_engines_failing_gives_actionable_message(self):
        self._install({"google": ("", "http-403"), "mymemory": ("", "http-429")})
        result = tr.translate("你好", "en")
        self.assertFalse(result.ok)
        self.assertEqual(result.translated, "")
        # 必须说清失败原因，并给出下一步能做什么
        self.assertIn("Key", result.text)          # 403 的可读解释
        self.assertIn("额度", result.text)          # 429 的可读解释
        self.assertIn(tr.GOOGLE_KEY_ENV, result.text)   # 给出升级路径
        # 原始错误码留在 error 字段里，便于排查
        self.assertIn("403", result.error)
        self.assertIn("429", result.error)

    def test_no_key_is_not_reported_as_a_failure(self):
        """没配 Key 是"预期状态"，不该被当成错误吓唬用户。"""
        tr._google_key = lambda: ""
        self._install({"google": ("", "no-key"), "mymemory": ("Hello", "")})
        result = tr.translate("你好", "en")
        self.assertTrue(result.ok)
        self.assertEqual(result.engine, "mymemory")
        self.assertNotIn("404", result.text)

    def test_cache_prevents_second_request(self):
        self._install({"google": ("Hello", ""), "mymemory": ("", "")})
        tr.translate("你好", "en")
        before = list(self.calls)
        again = tr.translate("你好", "en")
        self.assertTrue(again.cached)
        self.assertEqual(self.calls, before, "同样的句子不该再请求一次")

    def test_cache_key_includes_target_language(self):
        self._install({"google": ("A", ""), "mymemory": ("", "")})
        tr.translate("你好", "en")
        tr.translate("你好", "ja")
        self.assertEqual(self.calls, ["google", "google"],
                         "不同目标语言不该共用缓存")

    def test_clear_cache(self):
        self._install({"google": ("Hello", ""), "mymemory": ("", "")})
        tr.translate("你好", "en")
        self.assertEqual(tr.clear_cache(), 1)
        tr.translate("你好", "en")
        self.assertEqual(len(self.calls), 2)


class TestTranslationGuards(unittest.TestCase):
    """输入校验与语言解析（不联网）。"""

    def test_empty_text(self):
        for text in ("", "   ", None):
            result = tr.translate(text, "en")
            self.assertFalse(result.ok)
            self.assertIn("没有需要翻译", result.text)

    def test_too_long_text_is_rejected_not_truncated(self):
        """宁可明确拒绝，也不要偷偷截断 —— 截断会让人拿到半句译文。"""
        result = tr.translate("啊" * (tr.MAX_QUERY_CHARS + 10), "en")
        self.assertFalse(result.ok)
        self.assertIn("太长", result.text)
        self.assertIn(str(tr.MAX_QUERY_CHARS), result.text)

    def test_unknown_target_language_is_not_guessed(self):
        result = tr.translate("你好", "克林贡语")
        self.assertFalse(result.ok)
        self.assertIn("不认识", result.text)
        self.assertIn("克林贡语", result.text)
        self.assertIn(str(lang.language_count()), result.text)

    def test_same_language_short_circuits(self):
        result = tr.translate("你好", "中文")
        self.assertTrue(result.ok)
        self.assertIn("不需要翻译", result.text)
        self.assertEqual(result.engine, "none")

    def test_auto_direction_zh_to_en(self):
        self.assertEqual(tr.detect_language("你好"), "zh")

    def test_auto_direction_uses_script(self):
        """韩语源文本不该被当成英语去翻成中文。"""
        self.assertEqual(tr.detect_language("안녕하세요"), "ko")

    def test_never_raises_on_odd_input(self):
        for text in (123, ["列表"], {"a": 1}, "🙂"):
            result = tr.translate(text)  # type: ignore[arg-type]
            self.assertIsInstance(result.text, str)


class TestEngineInfo(unittest.TestCase):
    def test_available_engines_shape(self):
        engines = tr.available_engines()
        self.assertEqual(set(engines), {"google", "mymemory"})
        self.assertTrue(engines["mymemory"], "MyMemory 应始终可用（免注册）")

    def test_coverage_description_mentions_key_when_missing(self):
        original = tr._google_key
        tr._google_key = lambda: ""
        try:
            text = tr.describe_coverage()
            self.assertIn(tr.GOOGLE_KEY_ENV, text)
        finally:
            tr._google_key = original

    def test_coverage_description_when_key_present(self):
        original = tr._google_key
        tr._google_key = lambda: "fake-key"
        try:
            self.assertIn("Google", tr.describe_coverage())
        finally:
            tr._google_key = original

    def test_engine_order_prefers_high_coverage(self):
        self.assertEqual(tr.ENGINE_ORDER[0], "google")
        self.assertIn("mymemory", tr.ENGINE_ORDER)


class TestLiveTranslation(unittest.TestCase):
    """联网层：无网时 SKIP（不伪装成通过）。"""

    def _try_live(self, text: str, target: str):
        tr.clear_cache()
        result = tr.translate(text, target)
        if not result.ok and ("连不上" in result.text or "超时" in result.text
                              or "网络" in result.text):
            self.skipTest(f"翻译服务不可达，跳过联网用例（{result.text}）")
        return result

    def test_zh_to_en_live(self):
        result = self._try_live("你好", "en")
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.translated)

    def test_less_common_language_live(self):
        """验证覆盖面：不是只有中日英。"""
        result = self._try_live("谢谢", "sw")     # 斯瓦希里语
        self.assertTrue(result.ok, result.text)

    def test_engine_is_reported(self):
        # 用日文假名而不是汉字：汉字文本的源语言本来就该判成中文（zh→ja），
        # 这里只想确认"引擎名字被报出来了"。
        result = self._try_live("こんにちは", "en")
        self.assertIn(result.engine, ("google", "mymemory"))


if __name__ == "__main__":
    unittest.main()
