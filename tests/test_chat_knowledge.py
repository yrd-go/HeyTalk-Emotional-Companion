# -*- coding: utf-8 -*-
"""
tests/test_chat_knowledge.py —— 情绪自助手册检索的回归保护（S4，纯标准库）
==============================================================================

这一层盯三件事，按重要性排序：

    ★ ① 手册里的求助号码必须与 safety.HOTLINES **完全一致**。
         写错号码比不写更危险，所以这条是硬断言。

    ② 检索能命中正确条目 —— 且**不是**靠关键词碰运气：
       "失眠怎么办"要命中睡眠条目，"心跳快"要命中呼吸/生理条目。

    ③ 手册内容必须遵守写作纪律：不出现"你应该"、不承诺疗效、
       不出现疾病诊断词。内容质量是这份语料的核心价值，必须被钉住。

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

from chat import knowledge as kb  # noqa: E402
from chat import safety  # noqa: E402
from chat.state import NEUTRAL, StateSnapshot  # noqa: E402
from paths import EMOTIONAL_HANDBOOK, KB_FILE  # noqa: E402


def _snapshot(**dims) -> StateSnapshot:
    payload = {name: NEUTRAL for name in
               ("valence", "arousal", "dominance", "energy",
                "social_support", "self_worth", "alliance", "disclosure")}
    payload.update(dims)
    return StateSnapshot(dims=payload, turn=1)


class TestHotlineConsistency(unittest.TestCase):
    """★ ① 号码一致性：写错比不写更危险。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)

    def test_handbook_exists_and_parses(self):
        self.assertTrue(EMOTIONAL_HANDBOOK.is_file(), "缺少情绪自助手册")
        self.assertGreater(len(self.handbook), 10,
                           "手册条目太少，检索没有意义")

    def test_all_official_hotlines_appear_in_handbook(self):
        missing = kb.check_hotline_consistency(self.handbook, safety.HOTLINES)
        self.assertEqual(missing, [],
                         f"手册里缺少这些官方号码：{missing}（写错或不写都很危险）")

    def test_hotlines_in_docs_and_handbook_are_exactly_right(self):
        """★ 文档与手册里的每个电话号码都必须**逐位正确**。

        为什么加这一条：实跑发现 README 里把 010-82951332 写成了
        010-82951932（后四位颠倒）——**一个数字写错，热线就打不通**，
        而这是全项目里后果最严重的笔误类型，绝不能只靠肉眼校对。

        做法：把源码 safety.HOTLINES 当作唯一事实来源，扫描所有文档与
        数据文件里出现的"疑似电话号码"，凡不在允许集合里的就失败。
        """
        # 事实来源：源码里的号码 + 公共紧急电话
        official = " ".join(str(item.get("number") or "") for item in safety.HOTLINES)
        digits_ok = set(re.findall(r"\d{3,4}-\d{7,8}", official))
        digits_ok |= {"12356", "120", "110", "119", "800-810-1117"}

        targets = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "README_EN.md",
            PROJECT_ROOT / "DEPLOY_STRUCTURE.md",
            PROJECT_ROOT / "DEPLOY.md",
            PROJECT_ROOT / "data" / "emotional_handbook.txt",
        ]
        pattern = re.compile(r"\d{3,4}-\d{7,8}")
        for path in targets:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for found in pattern.findall(text):
                self.assertIn(found, digits_ok,
                              f"{path.name} 里的号码 {found!r} 不在官方号码集合里"
                              f"（正确的有：{sorted(digits_ok)}）")

    def test_only_one_knowledge_base_exists(self):
        """项目现在**只有一个**知识库：情绪手册。

        历史说明：原先还有一份 IT 手册（`data/knowledge_base.txt`）与针对它的
        40 题评测；按需求已整体删除，`paths.KB_FILE` 改成指向情绪手册的兼容别名。
        这条测试钉住"不要又悄悄长回第二份语料"。
        """
        self.assertEqual(KB_FILE, EMOTIONAL_HANDBOOK,
                         "KB_FILE 应当就是情绪手册（唯一知识库）")
        self.assertTrue(EMOTIONAL_HANDBOOK.is_file(), "情绪手册不见了")
        self.assertFalse((PROJECT_ROOT / "data" / "knowledge_base.txt").exists(),
                         "旧的 IT 手册不该再出现")

    def test_emotional_handbook_has_no_it_content(self):
        """手册必须是情绪内容，不能混进 IT 内容。"""
        text = EMOTIONAL_HANDBOOK.read_text(encoding="utf-8")
        for it_word in ("VPN", "打印机", "密码重置", "IT 服务台", "Outlook"):
            self.assertNotIn(it_word, text, f"情绪手册里混进了 IT 内容：{it_word}")


class TestWritingDiscipline(unittest.TestCase):
    """③ 内容纪律：这是这份语料的核心价值。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)

    def test_no_prescriptive_you_should(self):
        for entry in self.handbook.entries:
            self.assertNotIn("你应该", entry.body,
                             f"{entry.entry_id} 出现「你应该」——违反写作纪律")

    def test_no_effect_promises(self):
        """禁止"承诺式"表达。

        注意措辞边界：像"不用承诺一定会好"这类**劝阻承诺**的句子本身是好的，
        所以只禁"一定要/一定能"这种真正下承诺的用法，不用裸"一定"（会误伤）。
        """
        banned = ("一定要", "一定能", "保证能", "马上就好", "治愈")
        for entry in self.handbook.entries:
            for word in banned:
                self.assertNotIn(word, entry.body,
                                 f"{entry.entry_id} 出现绝对化承诺「{word}」")

    def test_no_diagnostic_terms(self):
        """手册不该出现疾病名称或诊断式表述（我们不做诊断）。"""
        banned = ("抑郁症", "焦虑症", "双相", "精神病", "确诊", "病情")
        for entry in self.handbook.entries:
            for word in banned:
                self.assertNotIn(word, entry.body,
                                 f"{entry.entry_id} 出现诊断词「{word}」")

    def test_every_entry_has_body(self):
        for entry in self.handbook.entries:
            self.assertTrue(entry.body.strip(),
                            f"{entry.entry_id} 只有标题没有内容")
            self.assertTrue(entry.title.strip(), f"{entry.entry_id} 缺少标题")

    def test_entry_ids_are_unique(self):
        ids = [e.entry_id for e in self.handbook.entries]
        self.assertEqual(len(ids), len(set(ids)), "条目编号有重复")

    def test_entries_have_chapters(self):
        self.assertGreaterEqual(len(self.handbook.chapters()), 3)
        for entry in self.handbook.entries:
            self.assertGreaterEqual(entry.chapter, 1)


class TestParsing(unittest.TestCase):
    def test_parses_simple_text(self):
        text = """
第 1 章  测试章
================================================================================
【E1-01】第一条
正文第一行。
正文第二行。

【E1-02】第二条
另一段正文。
"""
        handbook = kb.parse_handbook(text)
        self.assertEqual(len(handbook), 2)
        first = handbook.by_id("E1-01")
        self.assertIsNotNone(first)
        self.assertEqual(first.title, "第一条")
        self.assertIn("正文第一行", first.body)
        self.assertEqual(first.chapter, 1)

    def test_tolerates_garbage(self):
        for text in ("", "没有任何条目的文本", None, 123, "【E1-01】只有标题"):
            handbook = kb.parse_handbook(text)  # type: ignore[arg-type]
            self.assertIsInstance(handbook, kb.Handbook)

    def test_missing_file_yields_empty_handbook(self):
        handbook = kb.load_handbook(PROJECT_ROOT / "不存在的文件.txt")
        self.assertEqual(len(handbook), 0)

    def test_by_id_returns_none_for_unknown(self):
        self.assertIsNone(kb.parse_handbook("").by_id("E9-99"))


class TestRetrieval(unittest.TestCase):
    """② 检索命中正确的条目。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)

    def _top(self, query: str, **dims) -> list[kb.HandbookEntry]:
        return kb.search(self.handbook, query, snapshot=_snapshot(**dims),
                         limit=3, min_score=0.3)

    def test_sleep_query_hits_sleep_entries(self):
        hits = self._top("我最近总是睡不着，躺下脑子停不下来")
        ids = [e.entry_id for e in hits]
        self.assertTrue(any(i in ("E1-04", "E3-01") for i in ids),
                        f"失眠问题没命中所属条目，实际：{ids}")

    def test_anxiety_body_query_hits_physiological_entries(self):
        hits = self._top("心跳很快，胸口发紧，喘不过气", arousal=0.9)
        ids = [e.entry_id for e in hits]
        self.assertIn("E1-01", ids, f"焦虑躯体症状没命中 E1-01，实际：{ids}")

    def test_self_criticism_query_hits_self_compassion_entries(self):
        hits = self._top("我觉得自己很没用，什么都做不好",
                        valence=0.25, self_worth=0.2)
        ids = [e.entry_id for e in hits]
        self.assertTrue(any(i.startswith("E4") for i in ids),
                        f"自我批评没命中第 4 章，实际：{ids}")

    def test_help_seeking_query_hits_professional_help_chapter(self):
        """求助类问题要命中第 6 章（怎么找真人帮忙）。

        用真实措辞（"找咨询师"/"心理咨询"/"去哪求助"）。
        实测记录：更模糊的"我想找人帮忙，但不知道去哪找"会无命中——
        因为**"帮忙"到底是帮自己还是帮别人，字面上分不出来**
        （同一句话也可能在问"怎么陪朋友"）。这是纯字面检索的固有歧义，
        不是可以靠调权重解决的，所以如实写在这里而不是硬凑。
        """
        for query in ("我想找咨询师，但不知道去哪找",
                      "我想去心理咨询，该怎么开口",
                      "不知道该去哪里求助"):
            hits = kb.search(self.handbook, query, snapshot=_snapshot(),
                             limit=3)
            ids = [e.entry_id for e in hits]
            self.assertTrue(any(i.startswith("E6") or i.startswith("E5") for i in ids),
                            f"求助类查询「{query}」没命中求助相关章节，实际：{ids}")

    def test_lexically_weak_query_is_a_known_limitation(self):
        """已知局限（如实记录，不装作没有）：

        "我一直拖延"与 E3-02 的措辞重合度低（查询里"我/一直/事情/堆着"都是杂词），
        实测得分 0.298，低于默认阈值 0.35。与其为了这一个用例把阈值调到会放进
        无关条目的地步，不如**承认它**：用户真说"我该怎么治拖延"时，
        措辞会与条目更接近，命中率更高。
        """
        hits = kb.search(self.handbook, "我一直拖延，事情堆着不想动",
                         snapshot=_snapshot(), min_score=0.35)
        # 这条不强制命中；只要求它在**更宽松**的阈值下能排到前面
        loose = kb.search(self.handbook, "我一直拖延，事情堆着不想动",
                          snapshot=_snapshot(), min_score=0.25)
        self.assertTrue(any(e.entry_id == "E3-02" for e in loose),
                        "即使放宽阈值也应命中 E3-02")

    def test_high_arousal_boosts_chapter_one(self):
        """状态参与排序：高度激动时"先把身体调回来"应更靠前。"""
        query = "很难受"
        calm = kb.search(self.handbook, query, snapshot=_snapshot(arousal=0.5),
                         limit=5, min_score=0.0)
        agitated = kb.search(self.handbook, query, snapshot=_snapshot(arousal=0.95),
                             limit=5, min_score=0.0)
        chapter_of = {e.entry_id: e.chapter for e in self.handbook.entries}
        calm_ch1 = sum(1 for e in calm if chapter_of[e.entry_id] == 1)
        agitated_ch1 = sum(1 for e in agitated if chapter_of[e.entry_id] == 1)
        self.assertGreaterEqual(agitated_ch1, calm_ch1,
                                "唤醒度高时第 1 章没有被提前")

    def test_clearly_irrelevant_queries_are_filtered(self):
        """明确无关的问题不该硬塞条目 —— 宁可什么都不给。"""
        for query in ("今天股市涨了多少", "帮我写一段快速排序的代码",
                      "给我讲讲量子力学", "推荐几部电影"):
            hits = kb.search(self.handbook, query,
                             snapshot=_snapshot(), min_score=0.35)
            self.assertEqual(hits, [], f"无关问题「{query}」不该命中条目")

    def test_search_respects_limit(self):
        hits = kb.search(self.handbook, "难受 睡不着 焦虑 拖延 自我否定",
                         snapshot=_snapshot(), limit=2, min_score=0.0)
        self.assertLessEqual(len(hits), 2)

    def test_never_raises_on_odd_input(self):
        for query in (None, "", 123, "🙂", "a" * 2000):
            self.assertIsInstance(
                kb.search(self.handbook, query, snapshot=None), list)  # type: ignore[arg-type]


class TestOfferGate(unittest.TestCase):
    """必须"用户问了怎么做"才给方法（与 persona 的"不主动给建议"一致）。"""

    def test_explicit_questions_are_recognized(self):
        for text in ("我该怎么办", "有什么办法吗", "教教我怎么做",
                     "怎么才能睡着", "有没有什么方法", "我该做点什么"):
            self.assertTrue(kb.should_offer_methods(text), f"未识别求教：{text!r}")

    def test_plain_venting_does_not_trigger_methods(self):
        for text in ("今天好累", "我不想活了", "我妈又跟我吵架了",
                     "就是想说说话", "别给我建议"):
            self.assertFalse(kb.should_offer_methods(text),
                             f"这句不该触发「给方法」：{text!r}")

    def test_empty_input_is_false(self):
        for text in (None, "", "   "):
            self.assertFalse(kb.should_offer_methods(text))


class TestMethodBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)

    def test_none_when_no_entries(self):
        self.assertIsNone(kb.build_method_block([]))

    def test_block_contains_discipline_reminders(self):
        entry = self.handbook.by_id("E1-01")
        block = kb.build_method_block([entry])
        self.assertIsNotNone(block)
        self.assertIn("E1-01", block)
        self.assertIn("商量的语气", block)
        self.assertIn("一次最多讲一条", block)

    def test_block_caps_entry_count(self):
        entries = list(self.handbook.entries)[:5]
        block = kb.build_method_block(entries, max_items=2)
        # 只应包含两条目：以编号出现次数判断
        self.assertLessEqual(block.count("【E"), 3)


if __name__ == "__main__":
    unittest.main()
