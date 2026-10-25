# -*- coding: utf-8 -*-
"""
tests/test_router.py —— src/core/router.py 的纯函数回归测试

router.py 的关键词路由与翻译指令解析是「一改就错、错了只在真实调用时才暴露」的
逻辑，因此优先给它们建测试：只依赖标准库，不联网、不调大模型。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.router as router  # noqa: E402


class TestExtractStudentId(unittest.TestCase):
    def test_extracts_first_number(self):
        self.assertEqual(router.extract_student_id("帮我查学生3"), 3)

    def test_multiple_numbers_takes_first(self):
        self.assertEqual(router.extract_student_id("帮我查学生12号"), 12)

    def test_defaults_when_no_digit(self):
        self.assertEqual(
            router.extract_student_id("帮我查学生"), router.DEFAULT_STUDENT_ID
        )


class TestParseTranslateInstruction(unittest.TestCase):
    def test_strips_prefix_only(self):
        query, target = router.parse_translate_instruction("帮我翻译 你好")
        self.assertEqual(query, "你好")
        self.assertIsNone(target)

    def test_chinese_target_suffix(self):
        query, target = router.parse_translate_instruction("帮我翻译 你好 到日语")
        self.assertEqual(target, "ja")
        self.assertEqual(query, "你好")

    def test_english_target_suffix(self):
        query, target = router.parse_translate_instruction("帮我翻译 hello to Russian")
        self.assertEqual(target, "ru")
        self.assertEqual(query, "hello")

    def test_trailing_punctuation_is_stripped(self):
        query, _target = router.parse_translate_instruction("帮我翻译 你好，")
        self.assertEqual(query, "你好")

    def test_empty_body(self):
        query, target = router.parse_translate_instruction("帮我翻译")
        self.assertEqual(query, "")
        self.assertIsNone(target)

    def test_unknown_language_name_is_kept_as_text(self):
        """不认识的「到XX」不应被当成目标语言，否则会把正文吃掉。"""
        query, target = router.parse_translate_instruction("帮我翻译 你好 到火星")
        self.assertIsNone(target)
        self.assertIn("你好", query)


class TestLangpair(unittest.TestCase):
    def test_detect_source_lang(self):
        self.assertEqual(router.detect_source_lang("你好"), "zh")
        self.assertEqual(router.detect_source_lang("hello"), "en")

    def test_auto_direction(self):
        self.assertEqual(router.resolve_langpair("你好", None), ("zh", "en"))
        self.assertEqual(router.resolve_langpair("hello", None), ("en", "zh"))

    def test_explicit_target_is_respected(self):
        self.assertEqual(router.resolve_langpair("你好", "ja"), ("zh", "ja"))

    def test_same_language_pair_is_detectable(self):
        """同语言保护依赖这个判断（源==目标时不应发无效请求）。"""
        source, target = router.resolve_langpair("你好", "zh")
        self.assertEqual(source, target)


class TestRoutesTable(unittest.TestCase):
    def test_route_order_is_priority(self):
        self.assertEqual(
            [name for name, _kw, _fn in router.ROUTES],
            ["学生查询", "翻译", "代码生成"],
        )

    def test_every_route_has_keywords_and_handler(self):
        for name, keywords, handler in router.ROUTES:
            self.assertTrue(keywords, name)
            self.assertTrue(callable(handler), name)

    def test_student_route_requires_both_keywords(self):
        keywords = dict((n, k) for n, k, _f in router.ROUTES)["学生查询"]
        self.assertIn("查", keywords)
        self.assertIn("学生", keywords)


if __name__ == "__main__":
    unittest.main()
