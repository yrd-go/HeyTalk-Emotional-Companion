# -*- coding: utf-8 -*-
"""
tests/test_chat_profile.py —— 动态档案的回归保护（S4，纯标准库）
====================================================================

本文件里最重要的一条**不是**"抽取准不准"，而是那条红线：

    ★ 候选**绝不会**自动写进档案。只有用户确认（或修正）之后才会落盘。

这条如果破了，后果是"系统在背后记下我没说过的话"，而且错误会
在之后的每次对话里持续生效——比不记更糟。所以这里用多种路径反复验证：
    直接调 store 不会写、reject 不会写、只有 accept/edit 才写。

其余覆盖：
    ① 抽取规则：抽长期陈述、不抽当下情绪/疑问/假设；
    ② 检索：相关的排前面，完全无关时仍有兜底；
    ③ 存储：同 key 覆盖、坏数据容忍、清空、上限保护；
    ④ 确认门控的统计口径（accept_rate / wrong_rate）——消融实验靠它。

    python tests/run_all.py
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat.profile import (  # noqa: E402
    MAX_ACTIVE_ENTRIES,
    MAX_PROMPT_ENTRIES,
    KINDS,
    ProfileEntry,
    ProfileExtractor,
    ProfileStore,
    build_profile_block,
    extract_candidates,
    extract_with_fallback,
    parse_extraction_payload,
    score_relevance,
    select_relevant,
)

TMP_ROOT = Path(__file__).resolve().parent / ".tmp_profile"


class ProfileTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = TMP_ROOT / self._testMethodName
        shutil.rmtree(self.base, ignore_errors=True)
        self.base.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def store(self) -> ProfileStore:
        return ProfileStore(self.base)

    def candidate(self, value: str = "我在准备考研", key: str = "考研",
                  kind: str = "goal") -> ProfileEntry:
        return ProfileEntry(kind=kind, key=key, value=value, source="candidate")


class TestConfirmationGateIsRedLine(ProfileTestBase):
    """★ 红线：没有用户确认，档案里不许有任何东西。"""

    def test_extraction_alone_writes_nothing(self):
        """抽取只是提议，绝不落盘。"""
        store = self.store()
        found = extract_candidates("我在准备考研，也在找实习")
        self.assertTrue(found, "用例前提：这句应当抽出候选")
        self.assertEqual(store.entries(), [], "抽取阶段不该写入任何条目")
        self.assertFalse(store.exists(), "抽取阶段不该创建档案文件")

    def test_rejected_candidate_never_enters_profile(self):
        store = self.store()
        candidate = self.candidate()
        store.reject_candidate(candidate)
        self.assertEqual(store.entries(), [], "被拒绝的候选绝不能进档案")
        # 但拒绝这件事要被记下来（用于统计系统猜错率）
        self.assertEqual(store.stats()["rejected"], 1)

    def test_only_accept_or_edit_writes(self):
        store = self.store()
        accepted = store.accept_candidate(self.candidate("我在准备考研", "考研"))
        self.assertEqual(len(store.entries()), 1)
        self.assertEqual(store.entries()[0].value, "我在准备考研")
        self.assertEqual(accepted.source, "confirmed")

        store.edit_candidate(self.candidate("我在广州上学", "学校"),
                             "我在广州读研")
        self.assertEqual(len(store.entries()), 2)
        edited = [e for e in store.entries() if e.key == "学校"][0]
        self.assertEqual(edited.value, "我在广州读研")
        self.assertEqual(edited.source, "edited")

    def test_edit_with_blank_text_is_treated_as_reject(self):
        store = self.store()
        result = store.edit_candidate(self.candidate(), "   ")
        self.assertIsNone(result)
        self.assertEqual(store.entries(), [])
        self.assertEqual(store.stats()["rejected"], 1)

    def test_edited_entry_is_more_trusted_than_accepted(self):
        """用户亲口纠正过的，可信度应高于系统抽对的。"""
        store = self.store()
        accepted = store.accept_candidate(self.candidate(value="我在准备考研",
                                                          key="考研"))
        edited = store.edit_candidate(self.candidate(value="我在考研", key="学校"),
                                      "我在准备考研调剂")
        self.assertGreater(edited.confidence, accepted.confidence)


class TestExtractionRules(unittest.TestCase):
    """① 抽取：长期陈述要抽，当下情绪/疑问/假设不抽。"""

    def test_extracts_long_term_statements(self):
        cases = {
            "我在准备考研": "考研",
            "我最近在考公务员": "考公",
            "我在广州上学": "城市",
            "我妈总是跟我吵架": "母亲",
            "我很喜欢看书": "喜欢",
        }
        for text, expected_key in cases.items():
            found = extract_candidates(text)
            keys = [c.key for c in found]
            self.assertIn(expected_key, keys, f"{text!r} 应抽出 key={expected_key}")

    def test_does_not_extract_transient_mood(self):
        """带'今天/刚才'的是当下情绪，属于 state.py 的领域，不该进长期档案。"""
        for text in ("我今天很累", "我刚才很难过", "我现在很焦虑"):
            self.assertEqual(extract_candidates(text), [], f"不该抽：{text!r}")

    def test_does_not_extract_questions_or_hypotheticals(self):
        for text in ("我在准备考研吗", "如果我在准备考研呢", "要是我不喜欢上班"):
            self.assertEqual(extract_candidates(text), [], f"不该抽：{text!r}")

    def test_caps_candidates_per_utterance(self):
        text = "我在准备考研，我在考公，我住在广州，我妈总是跟我吵架"
        self.assertLessEqual(len(extract_candidates(text, max_candidates=2)), 2)

    def test_never_raises_on_odd_input(self):
        for value in (None, "", 123, "🙂", "a" * 3000, ["列表"]):
            self.assertIsInstance(extract_candidates(value), list)

    def test_candidate_source_is_marked_candidate(self):
        for item in extract_candidates("我在准备考研"):
            self.assertEqual(item.source, "candidate",
                             "抽出来的只能是候选，不能直接是 confirmed")

    def test_extracts_recurring_emotional_context(self):
        """反复出现的情境可以提供记忆候选，但仍只是候选。"""
        cases = {
            "朋友突然不回消息，我总觉得自己哪里做错了": "朋友不回消息",
            "家里一直催婚，我一回去就被问": "催婚",
        }
        for text, expected_key in cases.items():
            found = extract_candidates(text)
            self.assertIn(expected_key, [item.key for item in found],
                          f"{text!r} 应提出 {expected_key!r} 候选")

    def test_does_not_promote_a_current_mood_to_memory(self):
        """当下的难受不等于长期事实；不能因为情绪强就自动记住。"""
        for text in ("工作快撑不住了", "我今天很累", "刚才有点想哭"):
            self.assertEqual(extract_candidates(text), [],
                             f"不该把当下情绪写进候选：{text!r}")


class TestExtractorFallback(unittest.TestCase):
    """轨道 B 失败必须回落到轨道 A。"""

    def test_model_extractor_returns_empty_without_model(self):
        self.assertEqual(ProfileExtractor().extract("我在准备考研"), [])

    def test_model_extractor_swallows_errors(self):
        class Broken:
            def invoke(self, *_a, **_k):
                raise RuntimeError("网络挂了")

        self.assertEqual(ProfileExtractor(Broken()).extract("我在准备考研"), [])

    def test_parses_valid_payload(self):
        payload = {"items": [{"kind": "goal", "key": "考研", "value": "在准备考研"}],
                   "confidence": 0.9}
        found = parse_extraction_payload(payload)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].key, "考研")

    def test_rejects_malformed_payloads(self):
        for payload in (None, "字符串", {}, {"items": "不是列表"},
                        {"items": [{"key": "", "value": ""}]}):
            self.assertEqual(parse_extraction_payload(payload), [])

    def test_fallback_uses_rules_when_model_yields_nothing(self):
        class Empty:
            def invoke(self, *_a, **_k):
                return type("R", (), {"content": '{"items": [], "confidence": 0}'})()

        found = extract_with_fallback("我在准备考研",
                                      extractor=ProfileExtractor(Empty()))
        self.assertTrue(found, "模型抽不到时应回落规则轨")
        self.assertEqual(found[0].key, "考研")


class TestRetrieval(unittest.TestCase):
    """② 检索：相关的排前面，无关时仍有兜底。"""

    def _entries(self) -> list[ProfileEntry]:
        return [
            ProfileEntry(kind="goal", key="考研", value="在准备考研",
                         updated_at="2026-01-01T00:00:00+00:00"),
            ProfileEntry(kind="fact", key="城市", value="在广州上学",
                         updated_at="2026-06-01T00:00:00+00:00"),
            ProfileEntry(kind="relationship", key="母亲", value="我妈总是跟我吵架",
                         updated_at="2026-03-01T00:00:00+00:00"),
        ]

    def test_relevant_entry_scores_higher(self):
        entries = self._entries()
        mother = [e for e in entries if e.key == "母亲"][0]
        city = [e for e in entries if e.key == "城市"][0]
        self.assertGreater(score_relevance(mother, "我妈今天又跟我吵架了"),
                           score_relevance(city, "我妈今天又跟我吵架了"))

    def test_selection_puts_relevant_first(self):
        chosen = select_relevant(self._entries(), "考研好难，复习不进去")
        self.assertEqual(chosen[0].key, "考研")

    def test_selection_still_returns_something_when_nothing_matches(self):
        """完全不相关时也要带一点，否则"记得你"显得很假。"""
        chosen = select_relevant(self._entries(), "今天天气不错")
        self.assertTrue(chosen, "无关时不该返回空")

    def test_selection_respects_cap(self):
        entries = [ProfileEntry(kind="fact", key=f"k{i}", value=f"关于第{i}件事的事")
                   for i in range(30)]
        self.assertLessEqual(len(select_relevant(entries, "随便说说")),
                             MAX_PROMPT_ENTRIES)

    def test_empty_entries_yield_empty(self):
        self.assertEqual(select_relevant([], "随便"), [])


class TestPromptBlock(unittest.TestCase):
    def test_none_when_no_entries(self):
        self.assertIsNone(build_profile_block([], "你好"))

    def test_block_warns_about_staleness(self):
        block = build_profile_block(
            [ProfileEntry(kind="goal", key="考研", value="在准备考研")], "考研")
        self.assertIsNotNone(block)
        self.assertIn("在准备考研", block)
        self.assertIn("可能已经过时", block)
        self.assertIn("不要生硬复述", block)

    def test_block_does_not_claim_to_be_a_record(self):
        """不许说"根据我的记录"——那读起来像监视。"""
        block = build_profile_block(
            [ProfileEntry(kind="goal", key="考研", value="在准备考研")], "考研")
        self.assertIn("不要", block)
        self.assertIn("记录", block)      # 以"不要说『根据我的记录』"的形式出现


class TestStoreBehaviour(ProfileTestBase):
    """③ 存储：覆盖、容忍坏数据、清空、上限。"""

    def test_same_key_is_replaced_not_duplicated(self):
        store = self.store()
        store.accept_candidate(self.candidate("我在准备考研", "考研"))
        store.accept_candidate(self.candidate("我考上研了", "考研"))
        entries = store.entries()
        self.assertEqual(len(entries), 1, "同一个 key 应覆盖而不是堆积")
        self.assertEqual(entries[0].value, "我考上研了")

    def test_times_used_survives_replacement(self):
        store = self.store()
        entry = store.accept_candidate(self.candidate())
        store.mark_used([entry.entry_id])
        store.accept_candidate(self.candidate("我在准备考研调剂", "考研"))
        self.assertEqual(store.entries()[0].times_used, 1)

    def test_mark_used_counts(self):
        store = self.store()
        entry = store.accept_candidate(self.candidate())
        store.mark_used([entry.entry_id, "不存在的 id"])
        self.assertEqual(store.entries()[0].times_used, 1)

    def test_delete_single_entry(self):
        store = self.store()
        entry = store.accept_candidate(self.candidate())
        self.assertTrue(store.delete(entry.entry_id))
        self.assertEqual(store.entries(), [])
        self.assertFalse(store.delete(entry.entry_id))

    def test_clear_removes_everything(self):
        store = self.store()
        store.accept_candidate(self.candidate("我在准备考研", "考研"))
        store.accept_candidate(self.candidate("我在广州上学", "城市"))
        self.assertEqual(store.clear(), 2)
        self.assertEqual(store.entries(), [])
        self.assertEqual(store.decisions(), [], "清空要连门控记录一起清掉")

    def test_find_by_key(self):
        store = self.store()
        store.accept_candidate(self.candidate())
        self.assertIsNotNone(store.find("考研"))
        self.assertIsNone(store.find("不存在"))

    def test_broken_json_is_tolerated(self):
        store = self.store()
        store.ensure_dir()
        store.file.write_text("{ 这不是合法 JSON", encoding="utf-8")
        self.assertEqual(store.entries(), [])
        self.assertEqual(store.stats()["entries"], 0)

    def test_wrong_schema_is_tolerated(self):
        store = self.store()
        store.ensure_dir()
        store.file.write_text(json.dumps({"schema": 999, "entries": [{}]}),
                              encoding="utf-8")
        self.assertEqual(store.entries(), [])

    def test_bad_entries_are_skipped_not_fatal(self):
        store = self.store()
        store.ensure_dir()
        store.file.write_text(json.dumps({
            "schema": 1,
            "entries": [{"key": "", "value": ""}, "字符串", None,
                        {"key": "考研", "value": "在准备考研"}],
            "decisions": [],
        }, ensure_ascii=False), encoding="utf-8")
        entries = store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].key, "考研")

    def test_entry_cap_is_enforced(self):
        store = self.store()
        for i in range(MAX_ACTIVE_ENTRIES + 15):
            store.accept_candidate(self.candidate(f"事实{i}", f"k{i}"))
        self.assertLessEqual(len(store.entries()), MAX_ACTIVE_ENTRIES)

    def test_unknown_kind_is_normalized(self):
        entry = ProfileEntry(kind="不存在的类别", key="k", value="v")
        self.assertIn(entry.kind, KINDS)

    def test_round_trip_through_file(self):
        store = self.store()
        original = store.accept_candidate(self.candidate())
        reloaded = ProfileStore(self.base).entries()[0]
        self.assertEqual(reloaded.entry_id, original.entry_id)
        self.assertEqual(reloaded.value, original.value)
        self.assertEqual(reloaded.kind, original.kind)


class TestGateStatistics(ProfileTestBase):
    """④ 门控统计：消融实验要看的两个数字。"""

    def test_rates_are_computed(self):
        store = self.store()
        store.accept_candidate(self.candidate("A", "a"))
        store.accept_candidate(self.candidate("B", "b"))
        store.edit_candidate(self.candidate("C", "c"), "C 改")
        store.reject_candidate(self.candidate("D", "d"))

        stats = store.stats()
        self.assertEqual(stats["proposed"], 4)
        self.assertEqual(stats["accepted"], 2)
        self.assertEqual(stats["edited"], 1)
        self.assertEqual(stats["rejected"], 1)
        # 认可率 = （确认 + 修正）/ 提议
        self.assertAlmostEqual(stats["accept_rate"], 0.75, places=4)
        self.assertAlmostEqual(stats["wrong_rate"], 0.25, places=4)

    def test_rates_are_none_without_proposals(self):
        stats = self.store().stats()
        self.assertIsNone(stats["accept_rate"])
        self.assertIsNone(stats["wrong_rate"])

    def test_stats_contains_no_entry_values(self):
        """统计里不该泄露档案内容（可能被渲染或写日志）。"""
        store = self.store()
        store.accept_candidate(self.candidate("我妈妈生病住院了", "母亲"))
        blob = json.dumps(store.stats(), ensure_ascii=False)
        self.assertNotIn("生病", blob)


if __name__ == "__main__":
    unittest.main()
