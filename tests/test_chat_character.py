# -*- coding: utf-8 -*-
"""
tests/test_chat_character.py —— 角落小动物的回归保护（纯标准库）
====================================================================

本文件最重要的一条**不是**"它好不好看"，而是那条设计红线：

    ★ 它绝不能表现出"因为你不理我而失落 / 在努力等你"。

那会传递"它有需求没被满足，而满足它是你的责任"。对正在低落、自责、
觉得自己是累赘的人，最可能的感觉是愧疚——对情绪陪伴是反效果。
所以这里用**禁用词扫描 + 状态白名单**双重钉住，任何"索取式"设计都会失败。

注意扫描对象是**渲染输出**（HTML），不是源码：源码注释里为了说明红线
必然会出现这些词，那不是问题；能被用户看到的文本才是。

其余覆盖：六个形象一一对应、SVG 可替换且完整、未知输入回落、
纯前端交互（不打断对话）、无障碍、渲染失败不影响对话。

    python tests/run_all.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import character as pet  # noqa: E402
from chat import roles  # noqa: E402

# ★ 禁用词：出现在**用户可见输出**里就说明它变成了"索取关注"的角色
FORBIDDEN_WORDS = (
    "失落", "委屈", "失望", "伤心", "流泪", "哭",
    "想你", "等你", "寂寞", "孤单", "冷落", "不要我",
    "努力", "加油", "可怜", "抱歉", "对不起",
)

# 允许的状态（白名单）：都必须是"不需要用户"的中性行为
ALLOWED_STATES = {"idle", "happy", "sleeping"}

ROLE_IDS = roles.role_ids()


def _all_outputs() -> list[tuple[str, str]]:
    """所有姿态 × 所有状态下的渲染输出（禁用词扫描的数据源）。"""
    outputs: list[tuple[str, str]] = []
    for role_id in ROLE_IDS:
        for state in sorted(ALLOWED_STATES):
            outputs.append((f"{role_id}/{state}",
                            pet.build_character_html(role_id, state=state)))
    return outputs


class TestNoGuiltDesign(unittest.TestCase):
    """★ 红线：不许有任何索取式/愧疚式设计。"""

    def test_no_forbidden_words_in_rendered_output(self):
        for label, html in _all_outputs():
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(
                    word, html,
                    f"{label} 的输出里出现禁用词「{word}」——"
                    f"用户会以为这个角色因为被冷落而难受")

    def test_only_neutral_states_exist(self):
        self.assertEqual(set(pet.IDLE_STATES), ALLOWED_STATES)

    def test_unknown_state_falls_back_to_idle(self):
        """即使有人传入"期待/失落"这类状态，也只能回落 idle。"""
        for bad in ("expecting", "sad", "waiting", "努力", "", None, 123):
            html = pet.build_character_html("friend", state=bad)  # type: ignore[arg-type]
            self.assertIn("pet-idle", html, f"非法状态 {bad!r} 必须回落 idle")
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, html)

    def test_sleeping_is_a_neutral_state(self):
        """睡觉是最中性的：它表达"我不需要你"，而不是"我在等你"。"""
        html = pet.build_character_html("friend", state="sleeping")
        self.assertIn("pet-sleeping", html)
        # 睡着时不响应点击：免得像"你一碰我就醒"，那也是一种隐性索取
        self.assertNotIn("dsh-pet-hit-", html)

    def test_interaction_is_pure_css_not_javascript(self):
        """★ 交互必须是纯 CSS（这是修复过一次的真实事故）。

        事故经过：最初"点一下 → 吃惊地跳一下"用 <script> + addEventListener
        实现，结果那段 JS **源代码被当纯文本打印在页面上** ——
        因为 st.markdown(unsafe_allow_html=True) 会执行 <style>、
        却不会执行 <script>。用户一眼看到一串脚本，非常难看。

        现在改成"隐藏 checkbox + label 热区"的纯 CSS 方案：
        零脚本、不触发 Streamlit 重跑。
        """
        html = pet.build_character_html("friend")
        self.assertIn("dsh-pet-tap-", html, "缺少点击用的 checkbox")
        self.assertIn("dsh-pet-hit-", html, "缺少点击热区 label")
        self.assertIn(":checked", html, "缺少 CSS 状态切换")

    def test_interaction_can_be_disabled(self):
        html = pet.build_character_html("friend", enable_interaction=False)
        self.assertNotIn("dsh-pet-hit-", html)
        self.assertNotIn("dsh-pet-tap-", html)

    def test_character_module_does_not_import_streamlit(self):
        """保持零依赖：这样它可被普通 unittest 覆盖，也不会牵动页面状态。"""
        source = (SRC / "chat" / "character.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import streamlit") or stripped.startswith("from streamlit"):
                self.fail(f"character.py 不该直接依赖 streamlit：{stripped}")


class TestCharacters(unittest.TestCase):
    """① 六个姿态都有形象，结构完整。"""

    def test_every_role_has_a_character(self):
        for role_id in ROLE_IDS:
            self.assertIn(role_id, pet.CHARACTERS, f"姿态 {role_id} 缺少对应形象")

    def test_no_orphan_characters(self):
        self.assertEqual(set(pet.CHARACTERS), set(ROLE_IDS))

    def test_svg_is_structurally_complete(self):
        for role_id, character in pet.CHARACTERS.items():
            svg = character.svg.strip()
            self.assertTrue(svg.startswith("<"), f"{role_id} 的 SVG 片段格式异常")
            self.assertNotIn("{accent}", svg, f"{role_id} 的 accent 占位符未替换")
            self.assertNotIn("{soft}", svg, f"{role_id} 的 soft 占位符未替换")
            self.assertIn(character.accent, svg, f"{role_id} 未用上主色")

    def test_each_character_has_distinct_identity(self):
        animals = [c.animal for c in pet.CHARACTERS.values()]
        accents = [c.accent for c in pet.CHARACTERS.values()]
        self.assertEqual(len(set(animals)), len(animals), "形象说明应各不相同")
        self.assertEqual(len(set(accents)), len(accents), "主色应各不相同")

    def test_html_contains_all_parts(self):
        html = pet.build_character_html("mentor")
        for part in ("<style>", "<svg", "viewBox", "position: fixed"):
            self.assertIn(part, html, f"HTML 缺少 {part}")
        self.assertIn(pet.CHARACTERS["mentor"].name, html)

    def test_unknown_role_falls_back(self):
        self.assertEqual(pet.get_character("不存在").role_id, "friend")
        self.assertEqual(pet.get_character(None).role_id, "friend")
        self.assertIn(pet.CHARACTERS["friend"].name,
                      pet.build_character_html("不存在"))


class TestNoScriptEverRendered(unittest.TestCase):
    """★ 全量扫描：渲染输出里**永远不许出现 <script>**。

    因为 Streamlit 不执行它，只会把源码当纯文本显示在页面上。
    这条断言是那次事故（页面上出现一大段 JS 代码）的防复发保护——
    只需扫一遍所有角色 × 所有状态的输出。
    """

    def test_no_script_tag_in_any_output(self):
        for label, html in _all_outputs():
            self.assertNotIn("<script", html.lower(),
                             f"{label} 的输出里出现了 <script>：Streamlit 会把它当"
                             f"纯文本打印在页面上（曾发生过一次）")

    def test_no_javascript_url_or_onclick(self):
        for label, html in _all_outputs():
            lowered = html.lower()
            for bad in ("javascript:", "onclick=", "addeventlistener"):
                self.assertNotIn(bad, lowered, f"{label} 含可疑脚本写法：{bad}")


class TestAccessibilityAndLayout(unittest.TestCase):
    """④ 无障碍与"不打扰"。"""

    def test_respects_reduced_motion(self):
        self.assertIn("prefers-reduced-motion",
                      pet.build_character_html("friend"))

    def test_is_small_and_translucent_by_default(self):
        """默认必须小且半透明，否则会和"第一屏不给压力"冲突。"""
        html = pet.build_character_html("friend")
        self.assertIn("opacity:", html)
        matches = re.findall(r"opacity:\s*([0-9.]+);", html)
        self.assertTrue(matches, "找不到默认不透明度")
        self.assertLess(float(matches[0]), 0.7,
                        f"默认不透明度太高（{matches[0]}），会抢注意力")

    def test_hover_makes_it_clearer(self):
        self.assertIn(":hover", pet.build_character_html("friend"))

    def test_marked_aria_hidden(self):
        """纯装饰：对读屏器隐藏（它不承载信息）。"""
        self.assertIn('aria-hidden="true"', pet.build_character_html("friend"))

    def test_positioned_in_corner(self):
        html = pet.build_character_html("friend")
        for part in ("position: fixed", "right:", "bottom:"):
            self.assertIn(part, html)

    def test_mobile_layout_is_adjusted(self):
        self.assertIn("@media (max-width: 640px)",
                      pet.build_character_html("friend"))

    def test_each_character_has_its_own_element_id(self):
        """每个姿态的容器 id 不同，避免切换时上一次的残留。"""
        ids = {role_id: f"dsh-pet-{role_id}" for role_id in ROLE_IDS}
        for role_id, element_id in ids.items():
            self.assertIn(element_id, pet.build_character_html(role_id))
        self.assertEqual(len(set(ids.values())), len(ids))


class TestRenderSafety(unittest.TestCase):
    """⑤ 渲染失败绝不能影响对话。"""

    def test_render_swallows_errors(self):
        class Broken:
            def markdown(self, *a, **k):
                raise RuntimeError("boom")

        pet.render(Broken(), "friend")        # 不应抛异常

    def test_render_calls_markdown_with_html(self):
        calls: list[tuple] = []

        class Fake:
            def markdown(self, *args, **kwargs):
                calls.append((args, kwargs))

        pet.render(Fake(), "witness")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].get("unsafe_allow_html"))

    def test_build_never_raises_on_odd_input(self):
        for role_id in (None, "", "不存在", 123, "friend"):
            html = pet.build_character_html(role_id)  # type: ignore[arg-type]
            self.assertIsInstance(html, str)
            self.assertTrue(html.strip())


if __name__ == "__main__":
    unittest.main()
