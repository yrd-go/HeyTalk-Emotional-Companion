# -*- coding: utf-8 -*-
"""
tests/test_retrieval_fallback.py —— 检索侧降级（向量召回失败 -> 本地关键词召回）

为什么有这条测试：
    修复前只有**生成**阶段有降级，**检索**阶段没有：向量召回依赖云端 Embedding
    接口，它超时/限流/Key 失效时整个问答直接失败退出。这条测试把新的降级路径
    钉住：向量检索一旦抛异常，必须自动改用「chromadb 直读 + 本地 IDF 打分」，
    并在输出里明确标注降级（而不是偷偷返回一个看起来像向量结果的东西）。

全部用 mock，不联网、不读真实向量库，因此 CI 里也能跑。

    python -m unittest discover -s tests -v
    python tests/run_all.py
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import rag_demo  # noqa: E402


def chunk(text: str, line_start: int = 1, line_end: int = 2):
    return rag_demo.RawChunk(text, {"line_start": line_start, "line_end": line_end})


CHUNKS = [
    chunk("VPN 连接报错 691 的排查步骤：先重置密码，再检查账号是否被锁定。", 10, 12),
    chunk("团队周报模板：本周进展、下周计划、风险与求助。", 40, 42),
    chunk("打印机卡纸时先断电，再打开后盖取出纸张。", 70, 71),
]


class TestRankByKeywords(unittest.TestCase):
    def test_orders_by_keyword_score_and_does_not_fake_vector_similarity(self):
        top, pool = rag_demo.rank_by_keywords(
            CHUNKS, [("vpn", rag_demo.WEIGHT_LATIN), ("691", rag_demo.WEIGHT_LATIN)], k=2
        )
        self.assertEqual(len(pool), 3)
        self.assertEqual(len(top), 2)
        self.assertIn("VPN", top[0].doc.page_content)
        # 降级模式不伪造向量相似度：vec_sim 恒为 0，final_score 就是关键词分
        for hit in pool:
            self.assertEqual(hit.vec_sim, 0.0)
            self.assertAlmostEqual(hit.final_score, hit.kw_score)
        # 完全没命中的片段关键词分为 0，排在最后
        self.assertEqual(pool[-1].kw_score, 0.0)
        # 召回来源要标出来（打印 trace 时会显示「来源 关键词召回」）
        self.assertEqual(pool[0].sources, ["关键词召回"])

    def test_scores_have_discrimination_not_all_ones(self):
        """回归保护：早期版本所有命中片段都归一化成 1.0，排序退化成按行号。

        这里构造两个都命中同一个词、但命中次数/长度不同的片段，断言分数被区分开。
        """
        chunks = [
            rag_demo.RawChunk("vpn vpn vpn 报错排查", {"line_start": 1}),
            rag_demo.RawChunk("vpn 一句话", {"line_start": 2}),
            rag_demo.RawChunk("无关内容" * 50, {"line_start": 3}),
        ]
        top, pool = rag_demo.rank_by_keywords(chunks, [("vpn", 1.0)], k=2)
        scores = [hit.kw_score for hit in pool]
        self.assertEqual(len(set(round(s, 6) for s in scores[:2])), 2,
                         f"两个命中片段的关键词分不应相同：{scores}")
        self.assertEqual(pool[-1].kw_score, 0.0)

    def test_line_numbers_come_from_metadata(self):
        top, _pool = rag_demo.rank_by_keywords(CHUNKS, [("vpn", 1.0)], k=1)
        self.assertEqual(top[0].line_start, 10)
        self.assertEqual(top[0].line_end, 12)


class TestRetrieveWithFallback(unittest.TestCase):
    def test_uses_keyword_fallback_when_vector_search_raises(self):
        with mock.patch.object(rag_demo, "retrieve_hybrid",
                               side_effect=TimeoutError("embedding request timeout")), \
                mock.patch.object(rag_demo, "load_all_chunks", return_value=CHUNKS):
            hits, pool, mode, note = rag_demo.retrieve_with_fallback(
                db=object(),
                original_question="我的VPN坏了",
                rewritten_query="VPN 连接故障",
                terms=[("vpn", rag_demo.WEIGHT_LATIN)],
                k=2,
                keyword_weight=0.3,
                candidate_k=5,
            )
        self.assertEqual(mode, "keyword-only")
        self.assertIn("timeout", note)
        self.assertTrue(hits)
        self.assertIn("VPN", hits[0].doc.page_content)
        self.assertEqual(len(pool), 3)

    def test_fallback_can_be_disabled_for_ab_comparison(self):
        with mock.patch.object(rag_demo, "retrieve_hybrid",
                               side_effect=TimeoutError("embedding request timeout")):
            with self.assertRaises(TimeoutError):
                rag_demo.retrieve_with_fallback(
                    db=object(),
                    original_question="q",
                    rewritten_query="q",
                    terms=[("vpn", 1.0)],
                    k=1,
                    keyword_weight=0.3,
                    candidate_k=5,
                    allow_fallback=False,
                )

    def test_normal_path_reports_hybrid_mode(self):
        fake_hits = [rag_demo.Hit(doc=chunk("x"), vec_sim=0.9, final_score=0.9, key="k")]
        with mock.patch.object(rag_demo, "retrieve_hybrid",
                               return_value=(fake_hits, fake_hits)):
            hits, _pool, mode, note = rag_demo.retrieve_with_fallback(
                db=object(),
                original_question="q",
                rewritten_query="q",
                terms=[],
                k=1,
                keyword_weight=0.3,
                candidate_k=5,
            )
        self.assertEqual(mode, "hybrid")
        self.assertEqual(note, "")
        self.assertEqual(len(hits), 1)


class TestKeywordOnlyRetrieval(unittest.TestCase):
    def test_works_without_terms(self):
        """不给关键词时应当自己从问题里抽词（零依赖路径不能要求调用方先算好）。"""
        with mock.patch.object(rag_demo, "load_all_chunks", return_value=CHUNKS):
            hits, pool = rag_demo.retrieve_keyword_only("打印机卡纸怎么办", "", [], k=1)
        self.assertTrue(hits)
        self.assertIn("打印机", hits[0].doc.page_content)
        self.assertEqual(len(pool), 3)

    def test_empty_library_returns_empty(self):
        with mock.patch.object(rag_demo, "load_all_chunks", return_value=[]):
            hits, pool = rag_demo.retrieve_keyword_only("q", "q", [("vpn", 1.0)], k=1)
        self.assertEqual(hits, [])
        self.assertEqual(pool, [])


class TestLoadAllChunks(unittest.TestCase):
    def test_prefers_existing_langchain_client(self):
        """优先复用已打开的 Chroma 客户端，避免同进程重复打开持久化目录。"""

        class FakeCollection:
            @staticmethod
            def get(include=None):
                return {
                    "documents": ["片段A", "片段B", ""],
                    "metadatas": [{"line_start": 1}, {"line_start": 5}, {"line_start": 9}],
                }

        class FakeDB:
            _collection = FakeCollection()

        chunks = rag_demo.load_all_chunks(FakeDB())
        self.assertEqual(len(chunks), 2)          # 空片段被丢掉
        self.assertEqual(chunks[1].page_content, "片段B")
        self.assertEqual(chunks[1].metadata["line_start"], 5)


class TestFallbackBanner(unittest.TestCase):
    def test_banner_marks_keyword_only_mode(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rag_demo.print_fallback_banner("embedding timeout", "keyword-only")
        out = buf.getvalue()
        self.assertIn("检索降级", out)
        self.assertIn("retrieval=keyword-only", out)      # 稳定的机器可读标记
        self.assertIn("embedding timeout", out)

    def test_no_banner_in_normal_mode(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rag_demo.print_fallback_banner("", "hybrid")
        self.assertEqual(buf.getvalue(), "")


class TestRetrievalErrorContract(unittest.TestCase):
    """回归保护：向量召回失败必须「抛异常」而不是「die()」。

    真实事故：retrieve() 原来在检索失败时调用 die()，而 die() 抛的是 SystemExit
    （BaseException，不是 Exception），于是 retrieve_with_fallback 的
    except Exception 抓不到它 —— 用一个无效 Key 触发 401 时程序直接退出 1，
    关键词兜底一次都没生效。这条测试把这个契约钉住。
    """

    def test_retrieve_raises_retrieval_error_instead_of_exiting(self):
        class BoomDB:
            @staticmethod
            def similarity_search_with_score(query, k):
                raise RuntimeError("Error code: 401 - 令牌已过期或验证不正确")

        with self.assertRaises(rag_demo.RetrievalError):
            rag_demo.retrieve(BoomDB(), "q", 1)

    def test_dimension_mismatch_also_raises(self):
        class BoomDB:
            @staticmethod
            def similarity_search_with_score(query, k):
                raise ValueError("Collection expecting embedding with dimension of 768, got 2048")

        with self.assertRaises(rag_demo.RetrievalError) as ctx:
            rag_demo.retrieve(BoomDB(), "q", 1)
        self.assertIn("--rebuild", str(ctx.exception))

    def test_fallback_survives_systemexit_from_lower_layer(self):
        """安全网：万一底层某处仍然 die()（SystemExit），降级也必须生效。"""
        with mock.patch.object(rag_demo, "retrieve_hybrid",
                               side_effect=SystemExit("检索失败：401")), \
                mock.patch.object(rag_demo, "load_all_chunks", return_value=CHUNKS):
            hits, _pool, mode, note = rag_demo.retrieve_with_fallback(
                db=object(),
                original_question="VPN 坏了",
                rewritten_query="VPN 连接故障",
                terms=[("vpn", 1.0)],
                k=1,
                keyword_weight=0.3,
                candidate_k=5,
            )
        self.assertEqual(mode, "keyword-only")
        self.assertIn("检索失败", note)
        self.assertTrue(hits)


class TestNoRewriteKeepsVectorScores(unittest.TestCase):
    """回归保护：不做 LLM 改写时，向量分**绝不允许**被全部归零。

    真实事故：`retrieve_hybrid` 曾无条件把第一路标成「改写检索词」，而打分规则是
    "没被「原问题」这路召回的片段就把 vec_sim 归零" —— 于是默认链路（不做改写）
    里所有片段的向量分被清零，**悄悄退化成纯关键词排序**；
    当时 95 个测试没有一个报警，是实跑命令时看着 trace 里"向量 0.0000"才发现的。
    """

    def _fake_hits(self):
        return [
            rag_demo.Hit(
                doc=chunk("打印机卡纸时先断电，再打开后盖取出纸张。", 70, 71),
                line_start=70,
                line_end=71,
                vec_sim=0.87,
                key="k1",
                sources=["（由 retrieve_hybrid 覆盖）"],
            )
        ]

    def test_single_route_is_labeled_as_original_question(self):
        question = "打印机卡纸了怎么办"
        terms = rag_demo.build_terms(question, None)
        with mock.patch.object(rag_demo, "retrieve", return_value=self._fake_hits()):
            _top, pool = rag_demo.retrieve_hybrid(
                mock.MagicMock(),
                original_question=question,
                rewritten_query=question,      # 不做改写时，改写词就是原问题
                terms=terms,
                k=1,
                keyword_weight=0.3,
                candidate_k=20,
            )
        self.assertTrue(pool, "候选池不应为空")
        for hit in pool:
            self.assertIn("原问题", hit.sources)
            self.assertGreater(hit.vec_sim, 0.0,
                               "向量分被归零 = 默认链路退化成关键词排序")


class TestKeywordExplanationFormula(unittest.TestCase):
    """输出里写的公式必须与实际算分一致。

    历史：这里曾有两套公式（混合检索 `Σ权重×idf`、降级路径 `Σ权重×idf×(1+ln tf)` + 长度惩罚）；
    现已统一为一套，因此两种模式都应打印同一条公式。
    """

    def _hits(self):
        return [rag_demo.Hit(
            doc=chunk("vpn 报错"),
            matched=["vpn"],
            contributions=[("vpn", 1.0, 1.38, 1.38)],
        )]

    def test_both_modes_print_the_unified_formula(self):
        for mode in ("hybrid", "keyword-only"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rag_demo.print_keyword_explanation(self._hits(), mode=mode)
            out = buf.getvalue()
            self.assertIn("(1 + ln(tf))", out, mode)
            self.assertIn("长度", out, mode)


class TestCliFlags(unittest.TestCase):
    def test_new_flags_are_wired(self):
        args = rag_demo.parse_args(["问题", "--keyword-only", "--no-vec-fallback"])
        self.assertTrue(args.keyword_only)
        self.assertTrue(args.no_vec_fallback)

    def test_defaults_keep_fallback_on(self):
        args = rag_demo.parse_args(["问题"])
        self.assertFalse(args.keyword_only)
        self.assertFalse(args.no_vec_fallback)      # 默认开启降级

    def test_default_chain_does_not_call_llm_rewrite(self):
        """默认链路不做 LLM 改写 —— 依据 40 题评测（改写是负优化，且慢 14 倍）。

        回归保护：这个默认值一度是 True。如果有人改回去，"默认链路"就会重新变成
        hit@1 0.725 / 4.4s 的那一版，而且没有任何测试会报警 —— 所以必须钉住。
        """
        args = rag_demo.parse_args(["问题"])
        self.assertTrue(args.rerank)      # 混合检索仍然默认开启
        self.assertFalse(args.rewrite)    # 但不调用 LLM 改写


if __name__ == "__main__":
    unittest.main()
