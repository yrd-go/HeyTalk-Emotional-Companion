# -*- coding: utf-8 -*-
"""
tests/test_rag_scoring.py —— RAG 关键词抽取 / 双路召回合并 / 融合打分的回归测试

思路：用假的向量库（_StubDB）替代 Chroma，把「检索编排逻辑」单独测出来。
这样即使没有 API Key、没有向量库、没有网络，也能验证：
    - 关键词抽取的权重与停用词规则
    - 双路召回按内容去重时「取较大向量相似度、合并召回来源」
    - final = (1-α)·向量 + α·关键词 的融合与排序

rag_demo.py 的第三方依赖（langchain / chromadb）是延迟导入且容错的，
缺失时只设置 IMPORT_ERROR，不影响本文件的纯函数测试。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import rag_demo  # noqa: E402


def make_hit(content: str, vec_sim: float, key: str, sources=()):
    """构造一个 Hit（doc 只要满足 .page_content 即可）。"""
    doc = types.SimpleNamespace(page_content=content, metadata={})
    return rag_demo.Hit(
        doc=doc, vec_sim=vec_sim, final_score=vec_sim, key=key, sources=list(sources)
    )


class StubDB:
    """最小向量库替身：score 是「余弦距离」，与 Chroma 的语义保持一致。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[tuple[str, int]] = []

    def similarity_search_with_score(self, query, k):
        self.calls.append((query, k))
        out = []
        for content, distance in self.mapping.get(query, []):
            doc = types.SimpleNamespace(
                page_content=content, metadata={"line_start": 1, "line_end": 2}
            )
            out.append((doc, distance))
        return out[:k]


class TestKeywordExtraction(unittest.TestCase):
    def test_latin_token_weight_is_higher_than_cjk(self):
        terms = dict(rag_demo.extract_keywords("VPN 报错 691"))
        self.assertIn("vpn", terms)
        self.assertEqual(terms["vpn"], rag_demo.WEIGHT_LATIN)
        self.assertGreater(rag_demo.WEIGHT_LATIN, rag_demo.WEIGHT_CJK)

    def test_cjk_is_split_into_bigrams(self):
        terms = [t for t, _w in rag_demo.extract_keywords("打印机卡纸")]
        self.assertIn("打印", terms)
        self.assertIn("卡纸", terms)

    def test_stopwords_are_removed(self):
        terms = [t for t, _w in rag_demo.extract_keywords("请问怎么重置密码")]
        self.assertNotIn("请问", terms)
        self.assertNotIn("怎么", terms)

    def test_empty_input(self):
        self.assertEqual(rag_demo.extract_keywords(""), [])

    def test_term_count_is_capped(self):
        text = " ".join(f"tok{i}" for i in range(80))
        self.assertLessEqual(len(rag_demo.extract_keywords(text)), rag_demo.MAX_TERMS)


class TestMergePools(unittest.TestCase):
    def test_same_content_is_deduped_and_best_similarity_kept(self):
        pools = [
            [make_hit("alpha", 0.50, "k1", ["改写检索词"])],
            [make_hit("alpha", 0.80, "k1", ["原问题"])],
        ]
        merged = rag_demo.merge_pools(pools)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].vec_sim, 0.80)
        self.assertEqual(sorted(merged[0].sources), ["原问题", "改写检索词"])

    def test_distinct_contents_are_kept(self):
        merged = rag_demo.merge_pools([[make_hit("a", 0.1, "k1")], [make_hit("b", 0.2, "k2")]])
        self.assertEqual(len(merged), 2)


class TestScoreKeywords(unittest.TestCase):
    def test_scores_are_normalized_to_zero_one(self):
        pool = [
            make_hit("VPN 691 报错排查", 0.5, "k1"),
            make_hit("团队周报模板", 0.4, "k2"),
        ]
        rag_demo.score_keywords(pool, [("vpn", 1.0), ("691", 1.0)])
        self.assertAlmostEqual(max(h.kw_score for h in pool), 1.0)
        self.assertAlmostEqual(pool[1].kw_score, 0.0)
        self.assertIn("vpn", pool[0].matched)
        self.assertTrue(pool[0].contributions)

    def test_empty_terms_zeroes_everything(self):
        pool = [make_hit("anything", 0.3, "k1")]
        rag_demo.score_keywords(pool, [])
        self.assertEqual(pool[0].kw_score, 0.0)

    def test_idf_suppresses_terms_present_in_every_chunk(self):
        """到处都是的词应当比稀有词贡献小（IDF-lite 的核心作用）。"""
        pool = [
            make_hit("排查 步骤 vpn", 0.5, "k1"),
            make_hit("排查 步骤 打印机", 0.4, "k2"),
        ]
        rag_demo.score_keywords(pool, [("排查", 1.0), ("vpn", 1.0)])
        common = [c for c in pool[0].contributions if c[0] == "排查"][0]
        rare = [c for c in pool[0].contributions if c[0] == "vpn"][0]
        self.assertLess(common[3], rare[3])


class TestRetrieveHybrid(unittest.TestCase):
    def test_double_recall_merges_dedupes_and_fuses(self):
        db = StubDB({
            "VPN 连接故障": [("VPN 691 报错排查", 0.2)],          # 距离 0.2 -> 相似度 0.8
            "我的VPN坏了": [("VPN 691 报错排查", 0.3), ("打印机卡纸", 0.1)],
        })
        top, pool = rag_demo.retrieve_hybrid(
            db,
            original_question="我的VPN坏了",
            rewritten_query="VPN 连接故障",
            terms=[("vpn", rag_demo.WEIGHT_LATIN)],
            k=2,
            keyword_weight=0.3,
            candidate_k=5,
        )
        # 双路召回按内容去重：2 条不同内容
        self.assertEqual(len(pool), 2)
        # 两路都召回到的片段取较大相似度、并合并来源
        vpn = [h for h in pool if "VPN" in h.doc.page_content][0]
        self.assertAlmostEqual(vpn.vec_sim, 0.8)
        self.assertEqual(sorted(vpn.sources), ["原问题", "改写检索词"])
        # 融合分：0.7*0.8 + 0.3*1.0 = 0.86，应当排第一
        self.assertAlmostEqual(vpn.final_score, 0.7 * 0.8 + 0.3 * 1.0, places=6)
        self.assertEqual(top[0].key, vpn.key)

    def test_same_query_is_not_recalled_twice(self):
        db = StubDB({"同样的问题": [("片段A", 0.2)]})
        _top, pool = rag_demo.retrieve_hybrid(
            db, "同样的问题", "同样的问题", [], k=1, keyword_weight=0.3, candidate_k=5
        )
        self.assertEqual(len(db.calls), 1)          # 改写词 == 原问题时不重复召回
        self.assertEqual(len(pool), 1)

    def test_empty_query_pair_still_recalls_once(self):
        db = StubDB({"": [("兜底片段", 0.5)]})
        _top, pool = rag_demo.retrieve_hybrid(
            db, "", "", [], k=1, keyword_weight=0.3, candidate_k=5
        )
        self.assertEqual(len(db.calls), 1)          # 防御性兜底：至少召回一次
        self.assertEqual(len(pool), 1)


if __name__ == "__main__":
    unittest.main()
