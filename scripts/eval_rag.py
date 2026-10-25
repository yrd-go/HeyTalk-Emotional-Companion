#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/eval_rag.py —— 检索质量评测（针对情绪自助手册）
================================================================

这个脚本刻意做得**很小**，原因很实在
--------------------------------------------------------------------------
它的前任是一个 300+ 行的 4 配置对比评测（跑智谱 embedding、查重排、算拒答率），
配合 40 题 IT 手册评测集。那份语料与评测集已按需求删除。
重做时我选择只保留**最要紧的一件事**：

    给定"用户会怎么问"，关键词检索能不能把**正确的条目**捞进 top-k。

为什么只评检索、不评答案
--------------------------------------------------------------------------
- **检索是 Agent 的硬依赖**：手册条目捞不上来，后面说什么都白搭；
- **答案质量没法自动评**：它取决于提示词、姿态、模型，而且"有没有帮到人"
  最终只能靠人。用 LLM 当裁判只会制造一个看起来客观、实际不可信的数字
  （本项目 docs/PITFALLS.md 第 21 节就是被这种"单次实测"坑过的）。
- 关键词检索**零依赖、可离线、可复现** —— 所以它的指标是真的可比的。

指标口径（写死在这里，避免口径漂移）
--------------------------------------------------------------------------
    hit@1  第一名的片段是否命中期望条目
    hit@k  前 k 个片段里是否命中
    MRR    第一个命中位置的倒数（1.0 = 第一次就命中）

命中判定：片段正文含 `【Ex-xx】`（编号）**或**片段行区间与 `expect_lines` 重叠。
两者满足其一即可 —— 因为片段可能从条目中间开始、正文里没带编号。

用法：
    python scripts/eval_rag.py                 # 全量 18 题
    python scripts/eval_rag.py --limit 5       # 抽样快跑
    python scripts/eval_rag.py --k 5           # 改变 top-k
    python scripts/eval_rag.py --json out.json # 另存明细（含失败案例）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from paths import EMOTIONAL_HANDBOOK  # noqa: E402

QA_FILE = PROJECT_ROOT / "data" / "eval" / "qa.jsonl"

# 条目编号在手册里的形式：【E1-01】
_ENTRY_RE = re.compile(r"【(E\d{1,2}-\d{2})】")


# ===========================================================================
# 纯函数（可单测；不碰模型、不碰网络）
# ===========================================================================
def load_qa(path: Path = QA_FILE) -> list[dict]:
    """读评测集；坏行跳过（不让一条脏数据毁掉整轮评测）。"""
    if not path.is_file():
        return []
    items: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("question"):
            items.append(payload)
    return items


def chunk_matches(hit, expect_ids, expect_lines) -> bool:
    """一个检索片段是否命中期望条目。

    hit 需要提供 text 与 line_start/line_end（rag_demo 的 Hit 就是这么设计的）。
    """
    text = ""
    for attr in ("text", "content"):
        value = getattr(hit, attr, None)
        if isinstance(value, str) and value:
            text = value
            break
    if not text:
        doc = getattr(hit, "doc", None)
        text = getattr(doc, "page_content", "") or ""

    for entry_id in expect_ids or []:
        if f"【{entry_id}】" in text:
            return True

    start = getattr(hit, "line_start", None)
    end = getattr(hit, "line_end", None)
    if isinstance(start, int) and isinstance(end, int):
        for pair in expect_lines or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            lo, hi = int(pair[0]), int(pair[1])
            # 区间重叠即命中
            if not (end < lo or start > hi):
                return True
    return False


def first_hit_rank(hits, expect_ids, expect_lines) -> int | None:
    """返回第一个命中的名次（从 1 开始）；没有命中返回 None。"""
    for index, hit in enumerate(hits, 1):
        if chunk_matches(hit, expect_ids, expect_lines):
            return index
    return None


def summarize(ranks: list[int | None]) -> dict:
    """把各题名次汇成指标。**未命中不计入 hit@k，但计入总数**。"""
    total = len(ranks)
    if total == 0:
        return {"n": 0, "hit@1": None, "hit@3": None, "hit@5": None, "mrr": None}
    hit1 = sum(1 for r in ranks if r == 1)
    hit3 = sum(1 for r in ranks if r is not None and r <= 3)
    hit5 = sum(1 for r in ranks if r is not None and r <= 5)
    mrr = sum((1.0 / r) for r in ranks if r) / total
    return {
        "n": total,
        "hit@1": hit1 / total,
        "hit@3": hit3 / total,
        "hit@5": hit5 / total,
        "mrr": mrr,
        "missed": sum(1 for r in ranks if r is None),
    }


# ===========================================================================
# 真正跑检索
# ===========================================================================
def build_retriever():
    """构造"零依赖关键词检索器"。

    直接复用 Agent 自己的检索实现（src/chat/knowledge.py 的切词 + IDF + 同义词扩容），
    这样评测的就是**线上真正在跑的那套逻辑**，而不是另写一个好看的对照物。
    """
    from chat import knowledge as kb

    handbook = kb.load_handbook(EMOTIONAL_HANDBOOK)
    spans = entry_line_spans()

    class _Hit:
        """把检索结果包装成评测需要的形状（文本 + 行区间）。"""

        def __init__(self, entry) -> None:
            self.entry = entry
            self.text = entry.to_text()
            start_end = spans.get(entry.entry_id)
            self.line_start = start_end[0] if start_end else None
            self.line_end = start_end[1] if start_end else None

    def retrieve(question: str, k: int) -> list:
        # min_score=0：评测要看到完整名次，不能因为门槛把候选提前砍掉
        entries = kb.search(handbook, question, limit=k, min_score=0.0)
        return [_Hit(entry) for entry in entries]

    return handbook, retrieve


def entry_line_spans(path: Path = EMOTIONAL_HANDBOOK) -> dict[str, tuple[int, int]]:
    """算出手册里每条条目的行区间。

    **用代码算，不靠手数** —— 上一版评测最脆的地方就是手写行号，
    手册一改行号就失效（它的维护约定里专门写了这一条）。
    """
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    starts: list[tuple[str, int]] = []
    for index, line in enumerate(lines, 1):
        match = _ENTRY_RE.match(line.strip())
        if match:
            starts.append((match.group(1), index))

    spans: dict[str, tuple[int, int]] = {}
    for idx, (entry_id, start) in enumerate(starts):
        end = starts[idx + 1][1] - 1 if idx + 1 < len(starts) else len(lines)
        spans[entry_id] = (start, end)
    return spans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量评测（情绪自助手册）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全量）")
    parser.add_argument("--k", type=int, default=5, help="取前 k 个片段（默认 5）")
    parser.add_argument("--json", type=str, default="", help="把明细另存为 JSON")
    args = parser.parse_args(argv)

    items = load_qa()
    if not items:
        print(f"[错误] 读不到评测集：{QA_FILE}")
        return 2
    if args.limit > 0:
        items = items[: args.limit]

    try:
        handbook, retrieve = build_retriever()
    except Exception as exc:  # noqa: BLE001 - 给出可读原因而不是栈
        print(f"[错误] 无法构造检索器：{type(exc).__name__}: {exc}")
        return 2

    print("=" * 78)
    print(f"检索评测 · 语料 {EMOTIONAL_HANDBOOK.name}（{len(handbook)} 条）"
          f" · 题目 {len(items)} 道 · top-{args.k}")
    print("=" * 78)

    ranks: list[int | None] = []
    details: list[dict] = []
    for item in items:
        question = str(item.get("question") or "")
        expect_ids = item.get("expect_ids") or []
        expect_lines = item.get("expect_lines") or []
        hits = retrieve(question, args.k)
        rank = first_hit_rank(hits, expect_ids, expect_lines)
        ranks.append(rank)

        mark = "OK " if rank == 1 else ("~  " if rank else "MISS")
        print(f"  [{mark}] {question[:38]:<40} 期望 {','.join(expect_ids)}"
              f"  名次 {rank if rank else '-'}")
        details.append({
            "id": item.get("id"), "question": question,
            "expect": expect_ids, "rank": rank,
            "got": [getattr(h, "entry").entry_id for h in hits],
        })

    metrics = summarize(ranks)
    print("-" * 78)
    if metrics["n"]:
        print(f"  hit@1 = {metrics['hit@1']:.3f}   "
              f"hit@3 = {metrics['hit@3']:.3f}   "
              f"hit@5 = {metrics['hit@5']:.3f}   "
              f"MRR = {metrics['mrr']:.3f}")
        print(f"  未命中 {metrics['missed']} / {metrics['n']}")
    print("=" * 78)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"metrics": metrics, "details": details},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入：{out}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
