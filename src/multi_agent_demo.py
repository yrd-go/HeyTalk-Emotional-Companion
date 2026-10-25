# -*- coding: utf-8 -*-
r"""
multi_agent_demo.py —— Agent 核心循环（Agent Loop）最小可运行演示
===================================================================

目标：用 LangGraph 的 State Graph（状态图）演示「两个 Agent 互相协作 + 条件边打回重试」。

图结构（这就是 Agent Loop 的本质：图上的一个"环" + 条件边决定继续还是退出）：

                      ┌───────────────────────────────┐
                      │                               │ (条件边: retry，最多 2 次)
                      ▼                               │
    START ──▶ [Retriever 检索 Agent] ──▶ [Reviewer 审查 Agent] ──▶ route_after_review
                     ▲                        │                    │
                     │                        │                    ├─ approved  ──▶ END
                     └────────────────────────┘                    └─ give_up   ──▶ END
                          （把 review_feedback 回传给 Retriever，
                            下一轮据此"改写检索词"，相当于 Agent 的自我修正）

关键学习点：
  1. State（TypedDict）是整个循环共享的"黑板"，节点只返回自己改动的字段（增量更新）。
  2. 节点是普通函数：state -> partial_state。
  3. 循环靠"环 + 条件边"实现，不是靠 while：退出条件写在 route 函数里。
  4. 必须有计数器（retry_count）兜底，否则 Agent Loop 会无限打转（这正是生产环境的常见坑）。
  5. Agent 之间的"信息回传"（review_feedback）是让第二次检索比第一次更好的原因。

依赖：langgraph（本机 .venv-rag 已安装）。不需要任何 LLM API Key，全部是本地 mock。

运行：
    .\.venv-rag\Scripts\python.exe multi_agent_demo.py
"""

from __future__ import annotations

import json
import operator
import sys
from typing import Annotated, Any, TypedDict

# Windows 控制台默认可能是 GBK，强制 UTF-8 输出，避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

LANGGRAPH_ERROR: Exception | None = None
try:
    from langgraph.graph import END, START, StateGraph
except ImportError as _exc:  # pragma: no cover - 只有没装 langgraph 时走到
    # 刻意不在这里调用 sys.exit()：
    #   模块级 sys.exit 会让「导入本模块」这件事本身失败，而 SystemExit 继承
    #   BaseException —— 普通的 except Exception 抓不到它，于是任何做导入自检的
    #   工具（scripts/import_check.py）都会被带崩并返回非零退出码（CI 就栽在这里）。
    #   改成「记录错误、延迟到真正要建图时再报」：对直接运行脚本的人行为完全一致。
    END = START = StateGraph = None  # type: ignore[assignment]
    LANGGRAPH_ERROR = _exc


# ============================================================================
# 0. 常量配置
# ============================================================================

MAX_RETRIES = 2          # 最多被打回（重试）2 次
MIN_CONTEXT_CHARS = 200  # Reviewer 的长度门槛
MIN_SIGNAL_HITS = 2      # Reviewer 要求至少命中的"信息量关键词"个数
TOP_K = 3                # Retriever 每次最多取几篇资料

# 这些词代表"资料是否讲得够深"。Reviewer 用它们来判断详细程度。
SIGNAL_WORDS = [
    "超时", "重试", "指数退避", "熔断", "降级", "幂等",
    "向量检索", "重排", "分块", "召回", "幻觉", "评估", "Prompt", "缓存",
]

# 极简"分词"词表：避免为了 demo 引入 jieba。真实项目请用真正的分词/embedding。
VOCAB = SIGNAL_WORDS + ["RAG", "Agent", "工具", "调用", "检索", "知识库", "问答"]


# ============================================================================
# 1. 模拟的 RAG 知识库
#    tags  = 可被检索命中的标签
#    related = 同主题的"邻近文档"，Retriever 依据 Reviewer 的 feedback 向这些邻居深挖
# ============================================================================

MOCK_KB: list[dict[str, Any]] = [
    {
        "id": "kb-001",
        "title": "RAG 召回质量优化",
        "tags": ["RAG", "向量检索", "重排", "分块"],
        "related": [],
        "content": (
            "RAG 的召回质量由三个环节共同决定。第一是分块（chunking）：建议按语义边界切分，"
            "块长 300~800 token，相邻块保留 10%~15% 重叠，避免答案刚好被切断。"
            "第二是向量检索：用 embedding 模型把 chunk 编码后写入向量库，检索时取 top-k"
            "（k 常取 20~50），再用 MMR 或相似度阈值过滤掉明显不相关的块。"
            "第三是重排（rerank）：把召回的候选块交给 cross-encoder 重排模型打分，"
            "取 top-n（n 常取 3~5）拼进 Prompt，通常能明显提升最终答案的准确率。"
            "此外一定要做召回评估：准备 50~100 条带标准答案的测试集，用 hit-rate 和 MRR 衡量，"
            "任何改动都跑回归。如果答案仍不理想，优先加大 top-k 并上重排，而不是直接换更大的生成模型。"
        ),
    },
    {
        "id": "kb-002",
        "title": "工具调用超时设置",
        "tags": ["超时", "工具", "调用"],
        "related": ["kb-003"],
        "content": (
            "工具调用要显式设置超时：HTTP 请求用 timeout=(3, 30)，数据库查询把 "
            "statement_timeout 设为 5s；超时后先记录日志，再返回可读的错误信息，"
            "不要把原始异常直接抛给用户。"
        ),
    },
    {
        "id": "kb-003",
        "title": "容错链路：重试 / 退避 / 熔断 / 降级 / 幂等",
        "tags": ["重试", "指数退避", "熔断", "降级", "幂等"],
        "related": [],
        "content": (
            "工具调用失败之后要有完整的容错链路。重试：只对幂等且可恢复的错误"
            "（网络抖动、429、503）重试，参数错误不要重试。指数退避：第 n 次重试等待 "
            "base * 2^n，再叠加随机抖动（jitter），例如 base=0.5s 时得到 0.5s、1s、2s，"
            "最多 3~5 次，并设置总时间预算上限。熔断：用滑动窗口统计失败率，"
            "失败率超过 50% 且请求数达到阈值就打开断路器，冷却期结束后进入半开状态，"
            "只放行少量探测请求。降级：熔断打开时走本地缓存、默认值或更便宜的小模型，"
            "保证主流程可用。幂等：给每次工具调用带上 request_id 并在服务端去重，"
            "这样重试不会产生重复副作用。最后是可观测性：把重试次数、熔断状态、超时率"
            "写入指标并配告警，否则线上出问题只能靠猜。"
        ),
    },
    {
        "id": "kb-004",
        "title": "团队周报模板",
        "tags": ["周报", "模板"],
        "related": [],
        "content": "本周进展：…… 下周计划：…… 风险与求助：……（与检索主题无关的文档）",
    },
]

NO_MATCH_PLACEHOLDER = "（知识库中没有匹配到相关资料）"


# ============================================================================
# 2. State：整个 Agent Loop 共享的状态（黑板）
#    每个节点只返回自己修改的字段，LangGraph 负责合并
# ============================================================================

class AgentState(TypedDict):
    question: str        # 用户问题（输入，全程不变）
    context: str         # Retriever 检索到的资料（每轮覆盖写）
    is_approved: bool    # Reviewer 的审查结论
    review_feedback: str # 审查意见；被打回时它是 Retriever 下一轮的"改进指令"
    retry_count: int     # 已被打回次数（= 已重试次数），用于防死循环
    # Annotated + operator.add => 这个字段是"追加"语义而不是覆盖，用来记录完整流转轨迹
    trace: Annotated[list[str], operator.add]


# ============================================================================
# 3. Retriever Agent（节点 1）：模拟去 RAG 知识库查资料
# ============================================================================

def extract_terms(text: str) -> list[str]:
    """极简分词：从词表里挑出出现在 text 中的词（大小写不敏感），去重保序。"""
    low = text.lower()
    seen: list[str] = []
    for word in VOCAB:
        if word.lower() in low and word not in seen:
            seen.append(word)
    return seen


def retrieve(question_terms: list[str], feedback_terms: list[str],
             allow_expansion: bool) -> list[dict[str, Any]]:
    """
    两阶段检索：
      阶段 1（基础检索）：按 question 词与文档 tags 的重合度打分，取 top-k。
      阶段 2（反馈扩展）：只有当 review_feedback 存在时，才顺着已命中文档的 related
                          邻居继续深挖（模拟 Agent 根据审查意见"改写检索词"）。
    注意：没有基础命中时不会做任何扩展 —— 所以知识库里真的没有的资料，重试再多也没用，
          这正好演示了"重试不能解决本质缺失，必须靠计数器兜底退出"。
    """
    scored: list[tuple[int, dict[str, Any]]] = []
    for doc in MOCK_KB:
        tags_low = [t.lower() for t in doc["tags"]]
        score = sum(1 for t in question_terms if t.lower() in tags_low)
        scored.append((score, doc))

    base = [(s, d) for s, d in scored if s > 0]
    # 分数高的优先；同分时资料更长的优先（更可能"够详细"）
    base.sort(key=lambda item: (-item[0], -len(item[1]["content"])))
    selected: list[dict[str, Any]] = [d for _, d in base[:TOP_K]]

    if allow_expansion and selected and feedback_terms:
        neighbor_ids: set[str] = set()
        for doc in selected:
            neighbor_ids.update(doc.get("related", []))
        selected_ids = {d["id"] for d in selected}
        for doc in MOCK_KB:
            if doc["id"] in neighbor_ids and doc["id"] not in selected_ids:
                tags_low = [t.lower() for t in doc["tags"]]
                if any(t.lower() in tags_low for t in feedback_terms):
                    selected.append(doc)

    return selected


def retriever_node(state: AgentState) -> dict[str, Any]:
    """Retriever Agent：查资料。第 2、3 轮会带上 Reviewer 的反馈去深挖。"""
    attempt = state.get("retry_count", 0) + 1          # 第几次检索
    feedback = state.get("review_feedback", "")
    question_terms = extract_terms(state["question"])
    feedback_terms = extract_terms(feedback)

    print(f"\n  ┌─ [Retriever Agent] 第 {attempt} 次检索"
          f"{'（依据审查反馈深挖）' if feedback_terms else ''}")
    print(f"  │  检索词: {question_terms or '(无有效关键词)'}")
    if feedback_terms:
        print(f"  │  反馈补充词: {feedback_terms}")

    docs = retrieve(question_terms, feedback_terms, allow_expansion=bool(feedback_terms))

    if not docs:
        context = NO_MATCH_PLACEHOLDER
        print(f"  │  命中: 0 篇  →  context = {context}")
    else:
        context = "\n\n".join(f"【{d['id']} {d['title']}】\n{d['content']}" for d in docs)
        print(f"  │  命中: {len(docs)} 篇 {[d['id'] for d in docs]}  →  context = {len(context)} 字")
    print("  └─ 状态更新: context, is_approved=False")

    # 关键：不能把 feedback 清空，Reviewer 下一轮会覆盖它；这里保留即可
    return {
        "context": context,
        "is_approved": False,          # 新一轮检索后，审查结论必须重置
        "trace": [f"retriever(第{attempt}次检索, 命中{len(docs)}篇)"],
    }


# ============================================================================
# 4. Reviewer Agent（节点 2）：审查资料够不够详细
# ============================================================================

def reviewer_node(state: AgentState) -> dict[str, Any]:
    """Reviewer Agent：用「长度 + 关键词命中数」两个客观指标模拟审查。"""
    context = state.get("context", "")
    hits = [w for w in SIGNAL_WORDS if w.lower() in context.lower()]
    length_ok = len(context) >= MIN_CONTEXT_CHARS
    keyword_ok = len(hits) >= MIN_SIGNAL_HITS
    approved = length_ok and keyword_ok

    print("\n  ┌─ [Reviewer Agent] 开始审查")
    print(f"  │  长度检查: {len(context)} 字 (要求 ≥ {MIN_CONTEXT_CHARS}) -> "
          f"{'PASS' if length_ok else 'FAIL'}")
    print(f"  │  关键词检查: 命中 {len(hits)} 个 {hits} (要求 ≥ {MIN_SIGNAL_HITS}) -> "
          f"{'PASS' if keyword_ok else 'FAIL'}")

    if approved:
        feedback = f"审查通过：资料 {len(context)} 字，命中关键词 {hits}。"
        print("  │  结论: ✅ 通过")
        print("  └─ 状态更新: is_approved=True")
        return {
            "is_approved": True,
            "review_feedback": feedback,
            "retry_count": state.get("retry_count", 0),   # 通过则不再累加
            "trace": ["reviewer(通过)"],
        }

    # ---- 不通过：给出可执行的改进指令，回传给 Retriever ----
    missing = [w for w in SIGNAL_WORDS if w not in hits][:4]
    if context == NO_MATCH_PLACEHOLDER or not context.strip():
        feedback = ("资料不足：知识库中没有任何匹配该问题的文档。"
                    f"建议补充/改写检索：{'、'.join(missing)}。")
    else:
        feedback = (f"资料不足：当前 {len(context)} 字（要求≥{MIN_CONTEXT_CHARS}），"
                    f"命中关键词 {len(hits)} 个（要求≥{MIN_SIGNAL_HITS}）。"
                    f"请围绕已命中主题继续深挖：{'、'.join(missing)}。")

    new_retry = state.get("retry_count", 0) + 1
    print(f"  │  结论: ❌ 打回  |  意见: {feedback}")
    print(f"  └─ 状态更新: is_approved=False, retry_count={new_retry}")

    return {
        "is_approved": False,
        "review_feedback": feedback,
        "retry_count": new_retry,
        "trace": [f"reviewer(打回, retry_count={new_retry})"],
    }


# ============================================================================
# 5. 条件边：Agent Loop 的"继续 / 退出"决策（核心！）
#    返回值是标签，由 add_conditional_edges 的映射表决定下一个节点
# ============================================================================

def route_after_review(state: AgentState) -> str:
    """纯函数：只读 state，决定下一步走向。这就是 Agent Loop 的退出条件。"""
    if state.get("is_approved"):
        decision, reason = "approved", "审查通过"
    elif state.get("retry_count", 0) < MAX_RETRIES:
        decision, reason = "retry", (
            f"未通过且重试次数 {state.get('retry_count', 0)}/{MAX_RETRIES}，打回 Retriever"
        )
    else:
        decision, reason = "give_up", (
            f"未通过且已达最大重试次数 {MAX_RETRIES}，强制结束（防止无限循环）"
        )
    print(f"\n  ⤷ [条件边 route_after_review] -> {decision}  ({reason})")
    return decision


# ============================================================================
# 6. 组装 State Graph
# ============================================================================

def build_graph():
    # 依赖缺失在这里才报错（而不是在模块顶层 sys.exit），保证本模块始终可被导入：
    # 直接运行脚本时，用户看到的提示与退出码与以前一致；而 import 自检不再被带崩。
    if LANGGRAPH_ERROR is not None:
        raise SystemExit("[FATAL] 没有找到 langgraph，请先安装：pip install langgraph")
    builder = StateGraph(AgentState)

    builder.add_node("retriever", retriever_node)   # Agent 1
    builder.add_node("reviewer", reviewer_node)     # Agent 2

    builder.add_edge(START, "retriever")            # 入口
    builder.add_edge("retriever", "reviewer")       # 检索完必然进入审查

    # ★ 条件边：reviewer 之后根据 route_after_review 的标签跳转
    #   retry -> 回到 retriever（形成环）；approved / give_up -> 结束
    builder.add_conditional_edges(
        "reviewer",
        route_after_review,
        {
            "retry": "retriever",
            "approved": END,
            "give_up": END,
        },
    )

    return builder.compile()


# ============================================================================
# 7. 本地验证：3 个用例覆盖"一次通过 / 打回后通过 / 重试耗尽"
# ============================================================================

CASES: list[dict[str, str]] = [
    {
        "name": "用例 1：一次通过（知识库里有详细资料）",
        "question": "RAG 里怎么用向量检索和重排提升召回质量？",
        "expect": "1 轮结束，is_approved=True，retry_count=0",
    },
    {
        "name": "用例 2：打回 1 次后通过（feedback 让第 2 轮检索更全）",
        "question": "Agent 调用工具超时了怎么处理？",
        "expect": "2 轮检索，is_approved=True，retry_count=1",
    },
    {
        "name": "用例 3：连续打回 2 次仍未通过（触发上限，安全退出）",
        "question": "怎么用 Qwen 做视频生成？",
        "expect": "3 轮检索，is_approved=False，retry_count=2 后 give_up，不无限循环",
    },
]


def run_case(graph, index: int, case: dict[str, str]) -> dict[str, Any]:
    print("\n" + "=" * 78)
    print(f"{case['name']}")
    print(f"question = {case['question']}")
    print(f"预期: {case['expect']}")
    print("-" * 78)

    init_state: AgentState = {
        "question": case["question"],
        "context": "",
        "is_approved": False,
        "review_feedback": "",
        "retry_count": 0,
        "trace": [],
    }

    # recursion_limit 是 LangGraph 的硬保护：即使逻辑写错成死循环，也会抛错而不是挂死
    final_state = graph.invoke(init_state, config={"recursion_limit": 25})

    print("\n  ---------- 最终 State ----------")
    printable = {k: v for k, v in final_state.items() if k != "context"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    print(f"  context 长度 = {len(final_state['context'])} 字")

    verdict = "✅ 通过" if final_state["is_approved"] else "❌ 未通过（已达重试上限，安全退出）"
    print(f"  最终结论: {verdict}")
    return final_state


def main() -> int:
    print(__doc__.split("依赖：")[0])
    graph = build_graph()

    try:  # 打印 LangGraph 自己渲染的图结构（部分环境缺依赖，失败不影响主流程）
        print("【LangGraph 渲染的图结构】")
        print(graph.get_graph().draw_ascii())
    except Exception as exc:  # pragma: no cover
        print(f"（draw_ascii 不可用: {exc}）")

    results = []
    for i, case in enumerate(CASES, start=1):
        results.append(run_case(graph, i, case))

    print("\n" + "=" * 78)
    print("汇总：状态在两个节点之间的流转是否如预期")
    print("=" * 78)
    for i, (case, st) in enumerate(zip(CASES, results), start=1):
        print(f"[{i}] is_approved={st['is_approved']}  retry_count={st['retry_count']}"
              f"  检索次数={sum(1 for t in st['trace'] if t.startswith('retriever'))}")
        print(f"    流转: {' -> '.join(st['trace'])}")

    ok = (
        results[0]["is_approved"] and results[0]["retry_count"] == 0
        and results[1]["is_approved"] and results[1]["retry_count"] == 1
        and not results[2]["is_approved"] and results[2]["retry_count"] == MAX_RETRIES
    )
    print("\n断言结果:", "全部符合预期 ✅" if ok else "存在不符合预期的用例 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
