#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
rag_demo.py —— 企业级 RAG 演示：查询改写 + 混合检索 + 双引擎路由/自动降级

数据流（建库，未改动）：
    data/emotional_handbook.txt
      -> RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
      -> 智谱 embedding-3（默认，走 HTTP API；或本地 Ollama 向量化）
      -> Chroma 本地向量库（data/chroma_db，可复用，不重复向量化）

问答流程（默认开启 --rerank 进阶链路）：
    用户问题
      -> ① 查询改写 Query Rewrite（glm-4-flash，temperature=0.1）
             口语化提问 -> 检索词 + 关键词（严格 JSON 契约，解析失败自动降级）
      -> ② 混合检索 Hybrid Search
             向量召回：改写检索词 ∪ 原始问题（按片段内容去重合并，取较大相似度）
             关键词加分：IDF-lite 加权命中 -> 候选池内归一化到 0~1
             final = (1-α)·向量相似度 + α·关键词得分   （α = --keyword-weight，默认 0.3）
      -> ③ 打印检索明细（改写结果 / 排名表 / 关键词命中解释 / Context）
      -> ④ 智谱 glm-4-flash 生成回答；失败（超时/异常/Key 无效）打印告警并
           无缝降级到本地 Ollama qwen2.5:3b（生成阶段始终使用"原始问题"）

用法：
    python rag_demo.py "我的VPN坏了"                      # 默认：改写 + 混合检索
    python rag_demo.py "我的VPN坏了" --no-rerank          # 退回纯向量检索（旧行为，便于对比）
    python rag_demo.py "我的VPN坏了" --no-rewrite         # 不做 LLM 改写，仅本地分词加分
    python rag_demo.py "我的VPN坏了" --keyword-weight 0.4 # 调关键词权重 α
    python rag_demo.py "我的VPN坏了" --candidate-k 30     # 手动指定候选池大小
    python rag_demo.py "VPN 报 691" --k 6                 # 返回 6 个片段
    python rag_demo.py "如何重置密码？" --engine zhipu     # 只测主引擎
    python rag_demo.py "如何重置密码？" --engine ollama    # 只测备用引擎
    python rag_demo.py "打印机卡纸" --no-fallback          # 禁用生成降级
    python rag_demo.py --rebuild                          # 只重建向量库，不提问
    python rag_demo.py "Outlook 发不出邮件" --embedder ollama --rebuild   # 全离线链路

配置方式（优先级从高到低，脚本启动时自动加载项目根目录 .env）：
    1) 系统环境变量      $env:ZHIPU_API_KEY = "sk-..."     临时生效，仅当前终端
    2) 根目录 .env 文件  ZHIPU_API_KEY=sk-...              推荐；与 requirements.txt 同级
    3) 代码内默认值      仅非敏感项（base_url、模型名）
    云端（Streamlit Cloud）没有 .env，改为在 Secrets 里配置同名键，
    由 src/paths.py 的 load_env() 写入 os.environ 后交由本脚本读取。
    注意：.env 须保存为 UTF-8 无 BOM，键值两侧不加引号、不留空格。

环境变量：
    ZHIPU_API_KEY        必填（智谱 Embedding、GLM-4-Flash 生成与查询改写共用）
    ZHIPU_BASE_URL       可选，默认 https://open.bigmodel.cn/api/paas/v4
    ZHIPU_EMBED_MODEL    可选，默认 embedding-3
    ZHIPU_CHAT_MODEL     可选，默认 glm-4-flash（生成与查询改写共用）
    OLLAMA_BASE_URL      可选，默认 http://localhost:11434/v1
    OLLAMA_CHAT_MODEL    可选，默认 qwen2.5:3b（生成降级 + 无 Key 时的改写回退）
    OLLAMA_EMBED_MODEL   可选，默认 nomic-embed-text

依赖说明（重要）：
    本脚本只使用 langchain / langchain-community / langchain-openai / chromadb
    与 Python 标准库（json、re、math、hashlib），不额外安装任何包：
      - 不下载 HuggingFace 本地模型，不依赖 torch；
      - 查询改写与回答生成全部走智谱 OpenAI 兼容 HTTP API；
      - 中文关键词不依赖 jieba，用「CJK 连续串切 bigram + 英文/数字 token 正则」自实现。
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 路径引导与 .env / Secrets 加载
#   - 重构后本文件位于 <root>/src/rag_demo.py，且可由任何工作目录启动，
#     所以先动态把仓库根与 src/ 注入 sys.path，再 import paths；
#   - paths.load_env() 会加载「仓库根」的 .env（不再假设与本脚本同级），
#     云端则回退到 Streamlit Secrets，同样只写入 os.environ；
#   - 必须早于下方所有 os.getenv()（含 CONFIG 段），否则读不到配置；
#   - override=False：真实系统环境变量优先于 .env，
#     因此 $env:ZHIPU_API_KEY="..." 的老用法依然生效且优先级更高；
#   - python-dotenv 未安装时静默降级，不影响原有功能；
#   - 注意：from __future__ 必须是文档字符串之后的第一条语句，
#     故本段只能紧随其后，这是能满足"早于所有其他导入"的最靠前位置。
# --------------------------------------------------------------------------
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]      # 仓库根目录（本文件在 src/ 下）
SRC_DIR = Path(__file__).resolve().parent           # src/ 目录
for _path in (str(BASE_DIR), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import KB_FILE, CHROMA_DIR, ENV_FILE, load_env  # noqa: E402

_ENV_INFO = load_env()                # 只关心键数量/来源，绝不读取或打印键值
ENV_FILE_FOUND = bool(_ENV_INFO["env_file"] or _ENV_INFO["secrets_used"])

import argparse
import gc
import hashlib
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from os import fspath

# --------------------------------------------------------------------------
# Windows 控制台编码守卫：GBK 终端打印中文/特殊符号时不至于崩溃
# --------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 配置
#   KB_FILE / CHROMA_DIR / ENV_FILE 统一来自 src/paths.py（绝对路径，与
#   运行时的工作目录无关）：data/emotional_handbook.txt、data/chroma_db、<root>/.env
#   ★ 变更说明：KB_FILE 现在指向**情绪自助手册**（项目唯一的知识库）。
#     原先的 IT 手册与针对它的 40 题评测已按需求删除，不再有第二份语料。
# --------------------------------------------------------------------------
_KB_NAME = os.path.basename(fspath(KB_FILE))
COLLECTION = "emotional_handbook"

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
DEFAULT_K = 4

# ---- 混合检索 / 查询改写相关常量 ----
DEFAULT_KEYWORD_WEIGHT = 0.30   # α：final = (1-α)·向量 + α·关键词
CANDIDATE_K_FACTOR = 5          # 候选池 = max(k * 5, 20)
CANDIDATE_K_MIN = 20
WEIGHT_LATIN = 1.0              # 英文/数字/条目号（如 vpn、691、Q5-02）权重更高
WEIGHT_CJK = 0.6                # 中文 bigram 易切错，权重调低
MAX_TERMS = 24                  # 参与加分的词上限
MAX_LLM_KEYWORDS = 12           # 采纳大模型关键词的上限
MAX_TERM_LEN = 24               # 单个词的最大字符数
MAX_QUERY_LEN = 60              # 改写检索词的最大字符数，超长视为改写失败
REWRITE_TIMEOUT = 30            # 改写调用超时（秒）
REWRITE_TEMPERATURE = 0.1       # 改写要稳，不要发挥

# 口语噪声词：不参与关键词加分，避免"我的/坏了/怎么"污染打分
STOPWORDS = {
    "的", "了", "我", "你", "他", "她", "它", "们", "是", "在", "有", "和", "就", "都", "也", "还",
    "我的", "你的", "我们", "你们", "请问", "帮我", "帮忙", "一下", "怎么", "如何", "什么", "为什么",
    "哪里", "哪个", "可以", "能否", "是否", "需要", "想要", "坏了", "不好", "不了", "没用", "不行",
    "问题", "情况", "现在", "今天", "一直", "总是", "突然", "好像", "可能", "应该", "怎么办",
    "the", "and", "for", "with", "how", "what", "why", "not", "cannot", "please", "help",
    "is", "are", "was", "were", "does", "did", "can", "could", "should",
}

ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "").strip()
ZHIPU_EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")
ZHIPU_CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4-flash")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:3b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_API_KEY = "ollama"  # Ollama 不校验 Key，占位即可

LINE = "=" * 62
SUB = "-" * 62


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Hit:
    """一条检索结果（替代原来的 4 元组，避免解包顺序出错）。

    - vec_sim      向量相似度（1 - 余弦距离）
    - kw_score     关键词得分（候选池内归一化到 0~1）
    - final_score  融合得分 = (1-α)·vec_sim + α·kw_score
    - matched      命中的关键词（用于打印与人工核对）
    - contributions [(词, 权重, idf, 贡献分)]，用于打印命中明细
    - sources      该片段由哪几路召回命中（"改写检索词" / "原问题"）
    - key          片段内容 hash，用于双路召回去重合并
    """

    doc: object
    line_start: int = -1
    line_end: int = -1
    vec_sim: float = 0.0
    kw_score: float = 0.0
    final_score: float = 0.0
    matched: list = field(default_factory=list)
    contributions: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    key: str = ""


class RetrievalError(RuntimeError):
    """向量召回失败（超时 / 鉴权 / 限流 / 维度不一致）。

    为什么单独定义这个类型、为什么不在这里 die()：
        die() 抛的是 SystemExit，而 SystemExit 继承 BaseException、**不是 Exception**，
        上层 retrieve_with_fallback 的 except Exception 根本抓不到它 ——
        结果「向量召回失败 -> 关键词兜底」这条降级链路会被直接绕过（实测踩过：
        用一个无效 Key 触发 401，程序直接退出 1，兜底一次都没生效）。
        所以这里只负责「如实抛错」，把「要不要致命」的决定权交给调用方。
    """


@dataclass
class RewriteResult:
    """查询改写结果。ok=False 表示走了降级（沿用原始问题）。"""

    query: str
    keywords: list = field(default_factory=list)
    source: str = ""
    ok: bool = False
    note: str = ""


# --------------------------------------------------------------------------
# 依赖导入（延迟到此处，便于给出可操作的中文报错）
# --------------------------------------------------------------------------
IMPORT_ERROR = None
try:
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError:  # 老版本 langchain 的路径
        from langchain.text_splitter import RecursiveCharacterTextSplitter

    from langchain_community.vectorstores import Chroma
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
except Exception as _e:  # noqa: BLE001  ModuleNotFoundError 等
    IMPORT_ERROR = _e


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def info(msg: str) -> None:
    print(f"[信息] {msg}")


def warn(msg: str) -> None:
    print(f"[警告] {msg}")


def err(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    err(msg)
    raise SystemExit(code)


def describe_exc(exc: BaseException) -> str:
    """把异常压成一行可读文本，去掉超长的请求体。"""
    text = str(exc).replace("\n", " ").strip()
    if len(text) > 300:
        text = text[:300] + " ...(已截断)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# --------------------------------------------------------------------------
# 1) Embedding 引擎
# --------------------------------------------------------------------------
def build_embeddings(embedder: str):
    """embedder: 'zhipu' | 'ollama'。两者都走 OpenAI 兼容协议。"""
    if embedder == "zhipu":
        if not ZHIPU_API_KEY:
            die(
                "未检测到环境变量 ZHIPU_API_KEY。\n"
                '       请先执行：$env:ZHIPU_API_KEY = "你的智谱Key"\n'
                "       如只想离线跑通全链路，可改用：--embedder ollama"
            )
        kwargs = dict(
            base_url=ZHIPU_BASE_URL,
            api_key=ZHIPU_API_KEY,
            model=ZHIPU_EMBED_MODEL,
            # 关键：False 表示直接把字符串发给服务端。
            # 若为 True，langchain 会用 tiktoken 把文本切成 token 数组，
            # 智谱接口不接受这种入参，会直接报错。
            check_embedding_ctx_length=False,
            chunk_size=16,   # 智谱单请求批量上限保守取值
            max_retries=0,
        )
        try:
            return OpenAIEmbeddings(timeout=30, **kwargs)
        except TypeError:
            # 兼容不支持 timeout 别名的旧版本 langchain-openai
            return OpenAIEmbeddings(**kwargs)

    if embedder == "ollama":
        kwargs = dict(
            base_url=OLLAMA_BASE_URL,
            api_key=OLLAMA_API_KEY,
            model=OLLAMA_EMBED_MODEL,
            check_embedding_ctx_length=False,
            chunk_size=16,
            max_retries=0,
        )
        try:
            return OpenAIEmbeddings(timeout=120, **kwargs)
        except TypeError:
            return OpenAIEmbeddings(**kwargs)

    die(f"未知的 embedder：{embedder}（可选 zhipu / ollama）")


# --------------------------------------------------------------------------
# 2) 切分
# --------------------------------------------------------------------------
def split_knowledge_base():
    if not os.path.isfile(KB_FILE):
        die(f"找不到知识库文件：{KB_FILE}")

    try:
        with open(fspath(KB_FILE), "r", encoding="utf-8") as f:
            raw = f.read()
    except UnicodeDecodeError:
        with open(fspath(KB_FILE), "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        die(f"读取知识库失败：{describe_exc(e)}")

    if not raw.strip():
        die("知识库文件是空的，没有可切分的内容。")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        # 中文感知的分隔符优先级：先段落，再句子，避免把"步骤 3/步骤 4"劈开
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        add_start_index=True,
    )
    docs = splitter.create_documents(
        [raw], metadatas=[{"source": os.path.basename(KB_FILE)}]
    )

    # 用 start_index 反查每个片段在原文中的行号范围，便于人工核对 Context
    for d in docs:
        start = d.metadata.get("start_index")
        if isinstance(start, int):
            line_start = raw.count("\n", 0, start) + 1
        else:
            line_start = -1
        d.metadata["line_start"] = line_start
        d.metadata["line_end"] = (
            line_start + d.page_content.count("\n") if line_start > 0 else -1
        )
    return raw, docs


# --------------------------------------------------------------------------
# 3) 向量库
# --------------------------------------------------------------------------
def _index_info(db):
    """返回 (已存片段数, 建库时记录的 embedder 名称)"""
    try:
        col = getattr(db, "_collection", None)
        count = int(col.count()) if col is not None else 0
        meta = getattr(col, "metadata", None) or {}
        return count, meta.get("embedder")
    except Exception:
        return 0, None


def _purge_chroma_dir() -> None:
    """--rebuild 时彻底删除向量库目录。

    Chroma.from_documents 内部走 chromadb 的 get_or_create_collection()，
    集合同名时只会复用而不是重建，于是新 embedder 的向量会被追加到旧维度
    （如 768）的集合里，chromadb 直接抛出：
        InvalidArgumentError: Collection expecting embedding with
        dimension of 768, got 2048
    因此 --rebuild 必须把整个目录删干净，而不是往旧集合里追加。
    """
    if not os.path.isdir(CHROMA_DIR):
        info(f"未发现旧向量库目录，无需清理：{CHROMA_DIR}")
        return

    gc.collect()  # 先尽量释放可能残留的文件句柄（Windows 上 rmtree 常见占用）
    try:
        shutil.rmtree(fspath(CHROMA_DIR))
        info(f"已删除旧向量库目录：{CHROMA_DIR}")
        return
    except FileNotFoundError:
        return
    except Exception as e:
        # chroma.sqlite3 可能仍被占用（另一个终端在跑本脚本，或打开了该目录）
        warn(f"首次删除失败（{describe_exc(e)}），1 秒后重试 ...")
        time.sleep(1.0)
        gc.collect()
        try:
            shutil.rmtree(fspath(CHROMA_DIR))
            info(f"已删除旧向量库目录：{CHROMA_DIR}")
            return
        except Exception as e2:
            die(
                f"无法删除旧向量库目录：{CHROMA_DIR}\n"
                f"       原因：{describe_exc(e2)}\n"
                "       常见于目录被其他进程占用（另一个终端正在跑本脚本，"
                "或打开了该目录的窗口）。\n"
                "       请关闭后重试，或手动删除该目录。"
            )


def build_or_load_vectorstore(embedder: str, rebuild: bool):
    # 先构造 embedding（Key 缺失或参数错误会在此提前失败），
    # 保证不会在删掉旧库之后才发现新配置不可用
    emb = build_embeddings(embedder)
    has_dir = os.path.isdir(CHROMA_DIR) and bool(os.listdir(CHROMA_DIR))

    if rebuild:
        _purge_chroma_dir()
        has_dir = False

    if has_dir and not rebuild:
        try:
            db = Chroma(
                # 必须显式 str()：langchain 会把该值原样塞进 chromadb 的 Settings，
                # 而 chromadb/db/impl/sqlite.py 内部做的是
                #     persist_directory + "/chroma.sqlite3"
                # 传 Path 对象就会抛：
                #     TypeError: unsupported operand type(s) for +: 'WindowsPath' and 'str'
                # （chromadb 只在 direct PersistentClient(path=...) 时才自己 str()）
                persist_directory=fspath(CHROMA_DIR),
                collection_name=COLLECTION,
                embedding_function=emb,
            )
        except Exception as e:
            warn(f"读取已有向量库失败（{describe_exc(e)}），将重建。")
            db = None

        if db is not None:
            count, stored = _index_info(db)
            if count > 0:
                if stored and stored != embedder:
                    die(
                        f"向量库是用 `{stored}` 建的，当前却用 `{embedder}` 查询。\n"
                        "       两套向量空间不可混用，否则检索结果无意义。\n"
                        f"       请改为 --embedder {stored}，或执行 --embedder {embedder} --rebuild 重建。"
                    )
                if not stored:
                    warn(
                        "旧向量库未记录 embedder，无法校验向量维度。\n"
                        "       若检索时报 dimension 不匹配，请执行："
                        f"python rag_demo.py --embedder {embedder} --rebuild"
                    )
                info(f"复用已有向量库：{count} 个片段（embedder={stored or '未记录'}）")
                return db

    raw, docs = split_knowledge_base()
    info(
        f"切分完成：{len(docs)} 个片段"
        f"（chunk_size={CHUNK_SIZE}, chunk_overlap={CHUNK_OVERLAP}），"
        f"正在用 `{embedder}` 向量化 ..."
    )
    t0 = time.time()
    try:
        db = Chroma.from_documents(
            documents=docs,
            embedding=emb,
            # 同上：boundary 处必须转 str，否则 chromadb 内部 Path + str 报 TypeError
            persist_directory=fspath(CHROMA_DIR),
            collection_name=COLLECTION,
            collection_metadata={"hnsw:space": "cosine", "embedder": embedder},
        )
    except Exception as e:
        die(
            f"向量化/建库失败：{describe_exc(e)}\n"
            "       常见原因：ZHIPU_API_KEY 无效或额度不足、网络不通、"
            "向量库目录被占用。"
        )
    try:
        db.persist()  # 新版本 langchain 自动持久化，此行失败可忽略
    except Exception:
        pass

    count, _ = _index_info(db)
    info(f"向量库就绪：{count or len(docs)} 个片段，用时 {time.time() - t0:.1f}s，"
         f"位置 {CHROMA_DIR}")
    return db


# ==========================================================================
# 4) 关键词抽取（纯 Python，无 jieba / 无本地模型）
#    - 英文、数字、条目号：正则切分，权重 1.0
#    - 中文：取 CJK 连续串，切 bigram，权重 0.6
#    - 去停用词、去重、限长
# ==========================================================================
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{1,}")
_TERM_SPLIT_RE = re.compile(r"[\s、,，;；/|]+")


def _term_weight(term: str) -> float:
    """含英文/数字的词更可能是专有名词或错误码，权重高于中文 bigram。"""
    return WEIGHT_LATIN if re.search(r"[a-z0-9]", term.lower()) else WEIGHT_CJK


def _split_terms(text: str) -> list:
    """按常见中英文分隔符切词，用于拆开大模型返回的 "VPN、691 报错" 这类字符串。"""
    return [p.strip() for p in _TERM_SPLIT_RE.split(str(text)) if p.strip()]


def extract_keywords(text: str, limit: int = MAX_TERMS) -> list:
    """本地抽取关键词，返回 [(词, 权重)]，顺序即优先级。

    说明：不做真正的中文分词（避免引入 jieba），而是用 bigram 近似：
    "打印机卡纸" -> 打印 / 印机 / 机卡 / 卡纸。
    跨词 bigram（如"机卡"）属于噪声，但会被 IDF-lite 自动降权，
    且真正命中的 bigram 在候选池内归一化后仍能把正确片段顶上来。
    """
    if not text:
        return []

    terms: dict = {}
    lowered = text.lower()

    for m in _LATIN_TOKEN_RE.finditer(lowered):
        token = m.group(0).strip("._+-")
        if len(token) < 2 or token in STOPWORDS:
            continue
        terms.setdefault(token, WEIGHT_LATIN)

    for run in _CJK_RUN_RE.findall(text):
        if len(run) < 2:
            continue  # 单字噪声太大，直接丢弃
        pairs = [run] if len(run) == 2 else [run[i:i + 2] for i in range(len(run) - 1)]
        for pair in pairs:
            if pair in STOPWORDS:
                continue
            terms.setdefault(pair, WEIGHT_CJK)

    return list(terms.items())[:limit]


def build_terms(query: str, llm_keywords=None) -> list:
    """组装参与加分的词表：大模型关键词优先，本地分词补齐，去重限长。"""
    terms: list = []
    seen: set = set()

    def add(term: str) -> None:
        t = (term or "").strip()
        if not t or len(t) > MAX_TERM_LEN:
            return
        low = t.lower()
        if low in seen or low in STOPWORDS:
            return
        seen.add(low)
        terms.append((t, _term_weight(t)))

    for kw in llm_keywords or []:
        add(kw)
    for term, _w in extract_keywords(query or ""):
        add(term)
    return terms[:MAX_TERMS]


# ==========================================================================
# 5) 查询改写 Query Rewrite（glm-4-flash，严格 JSON 契约 + 四级容错解析）
# ==========================================================================
REWRITE_PROMPT = (
    "你是企业内部 IT 服务台的检索助手。请把员工口语化的提问改写成适合在"
    "《IT 运维手册》中做检索的查询词，并抽取关键词。\n"
    "【要求】\n"
    "1. 只做改写与抽词，不要回答问题，不要输出处理步骤。\n"
    "2. 不得编造员工没有给出的错误码、IP、账号、条目编号；可以补充领域同义词，"
    "例如“连不上”可补成“连接失败 报错 排查”。\n"
    "3. 检索词不超过 30 个字，多个词之间用空格分隔。\n"
    "4. 关键词 3~8 个，优先专有名词、产品名、错误码与条目编号（如 Q2-05）。\n"
    "5. 只输出一行 JSON，不要解释，不要 Markdown 代码块，格式严格为：\n"
    '{"query": "改写后的检索词", "keywords": ["关键词1", "关键词2"]}\n'
    "【员工提问】\n{question}\n"
)


def _strip_code_fence(text: str) -> str:
    """去掉 ``` / ```json 包裹，模型有时无视"不要代码块"的要求。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _json_to_payload(obj) -> tuple:
    """把解析出的 dict 规整成 (query, keywords)；容错中英文字段名。"""
    if not isinstance(obj, dict):
        return "", []
    query = (
        obj.get("query")
        or obj.get("检索词")
        or obj.get("查询词")
        or obj.get("rewritten_query")
        or ""
    )
    kws = obj.get("keywords") or obj.get("关键词") or obj.get("keyword") or []
    if isinstance(kws, str):
        kws = _split_terms(kws)
    if not isinstance(kws, list):
        kws = []
    cleaned = [str(x).strip() for x in kws if str(x).strip()]
    return str(query).strip(), cleaned


def parse_rewrite_payload(text: str) -> tuple:
    """四级容错解析：(1) 整串 JSON (2) 去代码围栏 (3) 抓第一个 {...} (4) 按行文本抽取。

    返回 (query, keywords)；全部失败时返回 ("", [])，由调用方决定降级。
    """
    if not text:
        return "", []
    raw = text.strip()

    for candidate in (raw, _strip_code_fence(raw)):
        try:
            query, kws = _json_to_payload(json.loads(candidate))
            if query:
                return query, kws
        except Exception:
            pass

    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            query, kws = _json_to_payload(json.loads(m.group(0)))
            if query:
                return query, kws
        except Exception:
            pass

    # 兜底：模型退化成了 "检索词：xxx / 关键词：a、b" 这种文本
    query = ""
    mq = re.search(r"(?:检索词|查询词|改写[^\n:：]*|query)\s*[:：]\s*(.+)", raw, re.I)
    if mq:
        query = mq.group(1).strip().strip('",')
    kws: list = []
    mk = re.search(r"(?:关键词|keywords?)\s*[:：]\s*(.+)", raw, re.I)
    if mk:
        kws = _split_terms(mk.group(1).strip().strip('",[]'))
    return query, kws


def _sanitize_rewrite(query, keywords, original: str) -> tuple:
    """清洗改写结果：检索词过长/为空/与原问题相同则退回原问题；关键词去噪限长。"""
    notes: list = []

    q = re.sub(r"\s+", " ", (query or "").strip().strip('"').strip())
    if not q:
        notes.append("未解析出检索词")
        q = original
    elif len(q) > MAX_QUERY_LEN:
        notes.append(f"检索词过长({len(q)}字)已丢弃，沿用原始问题")
        q = original
    if q == original:
        notes.append("改写结果与原始问题一致")

    kws: list = []
    seen: set = set()
    for raw_kw in keywords or []:
        for part in _split_terms(str(raw_kw)):
            low = part.lower()
            if len(part) > MAX_TERM_LEN or low in STOPWORDS or low in seen:
                continue
            seen.add(low)
            kws.append(part)
            if len(kws) >= MAX_LLM_KEYWORDS:
                break
        if len(kws) >= MAX_LLM_KEYWORDS:
            break

    return q, kws, "；".join(notes)


def _invoke_llm(prompt: str, engine: str, temperature: float, timeout: int):
    """统一的小调用入口：成功返回 (文本, None)，失败返回 (None, 异常)。"""
    try:
        if engine == "zhipu":
            if not ZHIPU_API_KEY:
                return None, RuntimeError("未检测到环境变量 ZHIPU_API_KEY（未设置或为空）")
            llm = _zhipu_llm(temperature=temperature, timeout=timeout)
        elif engine == "ollama":
            llm = _ollama_llm(temperature=temperature, timeout=timeout)
        else:
            return None, RuntimeError(f"未知的引擎：{engine}")
    except Exception as e:  # noqa: BLE001  构造期错误（参数不兼容等）
        return None, e

    try:
        resp = llm.invoke(prompt)
    except Exception as e:  # noqa: BLE001  超时 / 鉴权 / 限流 / 网络全部兜住
        return None, e
    text = (getattr(resp, "content", "") or "").strip()
    if not text:
        return None, RuntimeError("模型返回了空内容")
    return text, None


def rewrite_query(question: str, allow_ollama: bool = True) -> RewriteResult:
    """把口语化问题改写成检索词 + 关键词。

    降级策略（任何失败都不致命，绝不因此中断问答）：
      智谱（主）-> 本地 Ollama（可选，仅在未配置 Key 或 --engine 指定时兜底）
      都不可用 -> 沿用原始问题，关键词交给本地 extract_keywords()
    """
    original = question.strip()
    prompt = REWRITE_PROMPT.replace("{question}", original)
    problems: list = []

    if ZHIPU_API_KEY:
        text, e = _invoke_llm(prompt, "zhipu", REWRITE_TEMPERATURE, REWRITE_TIMEOUT)
        if e is None:
            query, kws = parse_rewrite_payload(text)
            query, kws, note = _sanitize_rewrite(query, kws, original)
            return RewriteResult(
                query=query,
                keywords=kws,
                source=f"智谱 {ZHIPU_CHAT_MODEL}",
                ok=True,
                note=note,
            )
        problems.append(f"智谱改写失败：{describe_exc(e)}")
    else:
        problems.append("未配置 ZHIPU_API_KEY，智谱改写不可用")

    if allow_ollama:
        text, e = _invoke_llm(prompt, "ollama", REWRITE_TEMPERATURE, 120)
        if e is None:
            query, kws = parse_rewrite_payload(text)
            query, kws, note = _sanitize_rewrite(query, kws, original)
            return RewriteResult(
                query=query,
                keywords=kws,
                source=f"本地 Ollama {OLLAMA_CHAT_MODEL}",
                ok=True,
                note=note,
            )
        problems.append(f"Ollama 改写失败：{describe_exc(e)}")

    return RewriteResult(
        query=original,
        keywords=[],
        source="跳过改写",
        ok=False,
        note="；".join(problems),
    )


def print_rewrite_trace(original: str, result: RewriteResult, terms: list) -> None:
    print(LINE)
    print("========== ① 查询改写（Query Rewrite） ==========")
    print(LINE)
    print(f"原始问题    ：{original}")
    if result.ok:
        print(f"改写检索词  ：{result.query}")
        print(f"改写提供关键词：{'、'.join(result.keywords) if result.keywords else '（无，全部改用本地分词）'}")
        print(f"改写引擎    ：{result.source}")
        if result.note:
            print(f"改写备注    ：{result.note}")
    else:
        print("改写检索词  ：（跳过，沿用原始问题）")
        print(f"跳过原因    ：{result.note or result.source}")
    joined = "、".join(t for t, _w in terms) if terms else "（无）"
    print(f"参与加分的词（{len(terms)} 个）：{joined}")
    print()


# ==========================================================================
# 6) 检索：纯向量召回 + 混合重排（向量 ∪ 双路召回，关键词 IDF-lite 加分）
# ==========================================================================
def _hit_key(doc) -> str:
    """片段内容指纹：双路召回时用于按内容去重合并。"""
    content = (getattr(doc, "page_content", "") or "").strip()
    return hashlib.md5(content.encode("utf-8", "ignore")).hexdigest()


def retrieve(db, question: str, k: int) -> list:
    """纯向量检索（保持旧语义），返回 list[Hit]。--no-rerank 与候选召回都用它。

    失败时抛 RetrievalError（**不再 die()**），让上层决定是否降级到本地关键词召回。
    """
    try:
        raw_hits = db.similarity_search_with_score(question, k=k)
    except Exception as e:
        _msg = describe_exc(e)
        if "dimension" in _msg.lower():
            raise RetrievalError(
                f"检索失败：{_msg}\n"
                "       这是新旧向量维度不一致：向量库是用别的 embedding 模型建的。\n"
                "       请执行：python rag_demo.py --embedder <你要用的> --rebuild"
            ) from e
        raise RetrievalError(
            f"检索失败：{_msg}\n"
            "       若报鉴权/连接类错误，说明 Embedding 接口不可用；"
            "可改用 --embedder ollama 走本地向量化。"
        ) from e

    hits: list = []
    for doc, score in raw_hits:
        # 建库时指定了 hnsw:space=cosine，故 score 为余弦距离，相似度 = 1 - 距离
        try:
            sim = 1.0 - float(score)
        except (TypeError, ValueError):
            sim = 0.0
        sim = max(0.0, min(1.0, sim))
        hits.append(
            Hit(
                doc=doc,
                line_start=doc.metadata.get("line_start", -1),
                line_end=doc.metadata.get("line_end", -1),
                vec_sim=sim,
                final_score=sim,  # 未重排时融合分=向量分，保证排序逻辑一致
                key=_hit_key(doc),
            )
        )
    return hits


def merge_pools(pools: list) -> list:
    """把多路召回结果按片段内容合并：向量相似度取较大者，召回来源合并。"""
    merged: dict = {}
    for pool in pools:
        for hit in pool:
            current = merged.get(hit.key)
            if current is None:
                merged[hit.key] = hit
                continue
            if hit.vec_sim > current.vec_sim:
                current.vec_sim = hit.vec_sim
            for src in hit.sources:
                if src not in current.sources:
                    current.sources.append(src)
    return list(merged.values())


def score_keywords(pool: list, terms: list) -> None:
    """关键词 IDF-lite 打分，就地写入 hit.kw_score / matched / contributions。

    打分公式（**混合检索与关键词兜底共用这一份**，两条路径口径必须一致）：
        idf(t)  = ln(1 + N / (1 + df(t)))       N=候选池片段数，df=含该词的片段数
        gain(t) = 权重(t) × idf(t) × (1 + ln tf)  tf = 该词在片段内出现次数
        raw(d)  = Σ gain(t)
        kw(d)   = raw(d) / (1 + ln(1 + len(d) / 平均长度))     ← 长度惩罚
        最后在候选池内归一化到 0~1。

    为什么不用最朴素的 `raw = Σ 权重 × idf` + 池内归一化（血泪教训）：
        候选池一大（降级时是全库 181 片段），大量片段都只命中同一个词一次，
        归一化后**全部撞成 1.0**，排序退化成按行号 —— 看起来有分数，其实没有区分度。
        补上 tf 与长度惩罚，分数才真正具备排序能力
        （回归测试：tests/test_retrieval_fallback.py::test_scores_have_discrimination_not_all_ones）。
    """
    if not pool:
        return

    n = len(pool)
    texts = [((hit.doc.page_content or "").lower()) for hit in pool]

    if not terms:
        for hit in pool:
            hit.kw_score = 0.0
        return

    df: dict = {}
    for term, _w in terms:
        needle = term.lower()
        df[term] = sum(1 for text in texts if needle in text)
    idf = {term: math.log(1.0 + n / (1.0 + df[term])) for term, _w in terms}

    avg_len = (sum(len(t) for t in texts) / n) if n else 1.0

    raws: list = []
    for hit, text in zip(pool, texts):
        total = 0.0
        matched: list = []
        contributions: list = []
        for term, weight in terms:
            needle = term.lower()
            if not needle:
                continue
            tf = text.count(needle)
            if tf <= 0:
                continue
            contribution = weight * idf[term] * (1.0 + math.log(tf))
            total += contribution
            matched.append(term)
            contributions.append((term, weight, idf[term], contribution))
        length_penalty = 1.0 + math.log(1.0 + len(text) / avg_len)
        hit.matched = matched
        hit.contributions = contributions
        raws.append(total / length_penalty)

    peak = max(raws) if raws else 0.0
    for hit, raw in zip(pool, raws):
        hit.kw_score = (raw / peak) if peak > 0 else 0.0


# ==========================================================================
# 6.5) 检索侧降级：向量召回不可用 -> 本地关键词召回（零外部依赖）
#
# 背景（真实缺口）：
#     生成阶段早就有主备降级（智谱 -> 本地 Ollama），但**检索阶段没有**。
#     向量召回依赖云端 Embedding 接口：它超时 / 限流 / Key 失效时，整个问答会
#     直接失败退出。这一节补上检索侧的兜底。
#
# 实现关键：走 chromadb 直接读片段，**不需要 embedding**（chromadb 本来就是本项目的
#     直接依赖），因此降级路径零外部依赖、离线可用，连 ZHIPU_API_KEY 都不需要。
# ==========================================================================
class RawChunk:
    """降级路径用的极简文档对象（只提供 page_content / metadata）。

    刻意不依赖 langchain 的 Document：降级发生的场景之一就是「依赖/接口不可用」，
    这种时候依赖越少越稳。
    """

    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str, metadata: dict | None = None) -> None:
        self.page_content = page_content
        self.metadata = metadata or {}


def _as_line_no(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chunks_from_chroma_payload(payload: dict) -> list:
    documents = payload.get("documents") or []
    metadatas = payload.get("metadatas") or [None] * len(documents)
    chunks = []
    for content, meta in zip(documents, metadatas):
        if content and str(content).strip():
            chunks.append(RawChunk(str(content), dict(meta or {})))
    return chunks


def load_all_chunks(db=None) -> list:
    """读出向量库里的全部片段（不需要 Embedding 接口）。

    优先复用已打开的 langchain Chroma 客户端，避免同一进程对同一持久化目录重复开
    客户端；拿不到时再用 chromadb 自己开一个（同样不需要 embedding）。
    """
    if db is not None:
        try:
            collection = getattr(db, "_collection", None)
            if collection is not None:
                chunks = _chunks_from_chroma_payload(
                    collection.get(include=["documents", "metadatas"])
                )
                if chunks:
                    return chunks
        except Exception:  # noqa: BLE001  复用失败就换下面的自建客户端
            pass

    try:
        import chromadb  # 本地向量库，requirements.txt 的直接依赖
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"缺少 chromadb，无法启用关键词兜底：{describe_exc(exc)}") from exc

    client = chromadb.PersistentClient(path=fspath(CHROMA_DIR))
    collection = client.get_collection(COLLECTION)
    return _chunks_from_chroma_payload(
        collection.get(include=["documents", "metadatas"])
    )


def rank_by_keywords(chunks: list, terms: list, k: int) -> tuple:
    """按本地关键词打分排序，返回 (top_k, 全量候选池)。

    打分复用 score_keywords 的**同一套公式**（tf + 长度惩罚 + 池内归一化）：
    线上混合检索与降级关键词召回这两条路径的口径必须一致，否则
    「为什么降级后的排序和线上不一样」会变成没人说得清的事。

    注意：降级路径没有向量参与，所以 vec_sim 恒为 0、final_score 直接等于关键词得分，
    **不伪造**一个假的「向量相似度」。
    """
    pool = []
    for chunk in chunks:
        meta = chunk.metadata
        pool.append(
            Hit(
                doc=chunk,
                line_start=_as_line_no(meta.get("line_start")),
                line_end=_as_line_no(meta.get("line_end")),
                vec_sim=0.0,
                key=_hit_key(chunk),
                sources=["关键词召回"],
            )
        )

    score_keywords(pool, terms)
    for hit in pool:
        hit.final_score = hit.kw_score
    pool.sort(key=lambda h: (-h.kw_score, h.line_start))
    return pool[:k], pool


def retrieve_keyword_only(question: str, rewritten_query: str, terms: list, k: int,
                          db=None) -> tuple:
    """零外部依赖的关键词召回（向量检索不可用 / --keyword-only 时使用）。"""
    chunks = load_all_chunks(db)
    if not chunks:
        return [], []
    if not terms:
        terms = build_terms(f"{rewritten_query or ''} {question or ''}".strip(), None)
    return rank_by_keywords(chunks, terms, k)


def retrieve_with_fallback(
    db,
    original_question: str,
    rewritten_query: str,
    terms: list,
    k: int,
    keyword_weight: float,
    candidate_k: int,
    allow_fallback: bool = True,
) -> tuple:
    """先走「向量 ∪ 关键词」混合检索；失败时（按配置）降级为本地关键词召回。

    返回 (hits, pool, mode, note)：mode ∈ {"hybrid", "keyword-only"}，
    note 是降级原因（未降级时为空串）。
    """
    try:
        hits, pool = retrieve_hybrid(
            db,
            original_question=original_question,
            rewritten_query=rewritten_query,
            terms=terms,
            k=k,
            keyword_weight=keyword_weight,
            candidate_k=candidate_k,
        )
        return hits, pool, "hybrid", ""
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        # 同时接 SystemExit：die() 抛的是 SystemExit（BaseException 子类），
        # 只写 except Exception 会漏掉它，降级就白写了（实测踩过这个坑）。
        note = describe_exc(exc)
        if not allow_fallback:
            raise
        warn(
            f"向量召回不可用（{note}），已降级为本地关键词召回（零外部依赖）。\n"
            "       本次只用本地分词 + IDF 打分排序，不使用向量相似度；\n"
            "       如需强制失败以便做对照实验，请加 --no-vec-fallback。"
        )
        hits, pool = retrieve_keyword_only(
            original_question, rewritten_query, terms, k, db=db
        )
        if not hits:
            die(
                "向量召回失败，关键词兜底也没有可用片段。\n"
                f"       向量失败原因：{note}\n"
                f"       兜底读不到片段：请确认 {CHROMA_DIR} 可读（或先 --rebuild 重建）。"
            )
        return hits, pool, "keyword-only", note


def print_fallback_banner(note: str, mode: str) -> None:
    """降级横幅：把「这次结果是怎么来的」讲清楚，别让人把关键词结果当成向量结果。"""
    if mode != "keyword-only":
        return
    print(LINE)
    print("========== 检索降级（Retrieval Fallback） ==========")
    print(LINE)
    print(f"[降级] 向量召回不可用：{note}")
    print("[降级] 已切换为本地关键词召回：chromadb 直读全量片段 + 本地 IDF 打分，零外部依赖。")
    print("[降级] 下方「向量」列恒为 0（本模式没有向量）、「融合」列等于「关键词」列，只看关键词列即可。")
    print("降级标记: retrieval=keyword-only")
    print()


def retrieve_hybrid(
    db,
    original_question: str,
    rewritten_query: str,
    terms: list,
    k: int,
    keyword_weight: float,
    candidate_k: int,
) -> tuple:
    """改写检索词 + 原始问题双路向量召回 -> 合并去重 -> 关键词加分 -> Top-K。

    返回 (top_k_hits, full_pool)：完整候选池用于打印打分明细。
    """
    queries: list = []
    rewritten = (rewritten_query or "").strip()
    original = (original_question or "").strip()
    # 只有"改写词确实不同于原问题"时才走双路召回、才贴「改写检索词」标签。
    # 否则（默认链路不做改写时）只发一路，且必须标成「原问题」——
    # 真实事故：这里曾无条件先贴「改写检索词」，而下面的规则是"没被原问题召回的片段
    # 就把 vec_sim 归零"，于是**所有**片段的向量分被清零，默认链路被悄悄退化成
    # 纯关键词排序（回归测试：test_no_rewrite_keeps_vector_scores_alive）。
    if rewritten and rewritten != original:
        queries.append((rewritten, "改写检索词"))
    if original:
        queries.append((original, "原问题"))
    if not queries:  # 理论上不会发生，防御性兜底
        queries.append((original or rewritten, "原问题"))

    pools: list = []
    for query, label in queries:
        hits = retrieve(db, query, candidate_k)
        for hit in hits:
            hit.sources = [label]
        pools.append(hits)

    pool = merge_pools(pools)

    # ★ 关键修复：只有「原始问题」那一路算出来的相似度，才与用户真正问的话可比。
    #   改写检索词只用于**扩展候选**（以及后面提供更好的关键词），
    #   它召回的片段如果没被原问题召回，就把 vec_sim 归零 —— 不参与向量打分。
    #
    #   依据（40 题评测，reports/eval_baseline.json）：合并时"两路取最大相似度"
    #   会把被改写词吸引、却与原问题无关的片段顶上来，默认链路 hit@1 只有 0.725，
    #   反而低于纯向量检索的 0.925 —— 也就是说这个"优化"当时是负优化。
    for hit in pool:
        if not any("原问题" in (src or "") for src in (hit.sources or [])):
            hit.vec_sim = 0.0
            hit.sources = list(hit.sources or []) + ["仅改写扩展(不计向量分)"]

    score_keywords(pool, terms)

    for hit in pool:
        hit.final_score = (1.0 - keyword_weight) * hit.vec_sim + keyword_weight * hit.kw_score

    pool.sort(key=lambda h: (-h.final_score, -h.vec_sim, h.line_start))
    return pool[:k], pool


def print_hybrid_trace(pool: list, k: int, keyword_weight: float, shown: int = 20) -> None:
    print(LINE)
    print(
        f"========== ② 混合检索（向量 {1.0 - keyword_weight:.2f} + 关键词 {keyword_weight:.2f}） =========="
    )
    print(LINE)
    if not pool:
        print("（候选池为空）")
        print()
        return
    print("带 * 的是最终送入模型的 Top-K 片段")
    for i, hit in enumerate(pool[:shown], 1):
        star = "*" if i <= k else " "
        span = f"{hit.line_start}-{hit.line_end}" if hit.line_start > 0 else "?"
        sources = "/".join(hit.sources) if hit.sources else "-"
        matched = "、".join(hit.matched) if hit.matched else "（无）"
        print(
            f"{star} {i:>3} | 行 {span:<10} | 向量 {hit.vec_sim:.4f} | "
            f"关键词 {hit.kw_score:.4f} | 融合 {hit.final_score:.4f} | "
            f"来源 {sources} | 命中 {matched}"
        )
    if len(pool) > shown:
        print(f"...（候选池共 {len(pool)} 条，此处只显示前 {shown} 条）")
    print()


def print_keyword_explanation(hits: list, mode: str = "hybrid") -> None:
    """关键词命中明细：把每个片段命中了哪些词、各自贡献多少分摊开，便于核对与调参。

    注意：`mode` 现在**不再影响公式** —— 混合检索与降级关键词召回共用同一套打分
    （tf + 长度惩罚）。保留这个参数只是兼容调用方；公式文本只有一份，
    避免再出现"输出里写的公式和实际算分对不上"的情况。
    """
    print(LINE)
    print("========== 关键词命中解释（IDF-lite 加权明细） ==========")
    print(LINE)
    print(
        "打分公式：贡献 = 词权重 × idf × (1 + ln(tf))，"
        "总分 = Σ贡献 / (1 + ln(1 + 片段长度/平均长度))；"
        "idf = ln(1 + 候选池片段数/(1 + 含该词片段数))，tf = 词在该片段内出现次数；"
        f"英文/数字权重 {WEIGHT_LATIN:.1f}，中文 bigram 权重 {WEIGHT_CJK:.1f}"
    )
    if not hits:
        print("（无）")
        print()
        return
    for i, hit in enumerate(hits, 1):
        span = f"{hit.line_start}-{hit.line_end}" if hit.line_start > 0 else "?"
        print(
            f"[片段 {i}] 第 {span} 行 | 关键词得分 {hit.kw_score:.4f} | "
            f"融合得分 {hit.final_score:.4f} | 向量 {hit.vec_sim:.4f}"
        )
        if not hit.contributions:
            print("         未命中任何加分词（完全依靠向量相似度入选）")
            continue
        parts = [
            f"{term}({weight:.1f}×{idf:.2f}={contribution:.2f})"
            for term, weight, idf, contribution in hit.contributions
        ]
        print("         命中：" + "  ".join(parts))
    print()


def print_context(hits: list, hybrid: bool = True) -> None:
    print(LINE)
    print("========== 检索到的原文片段（Context） ==========")
    print(LINE)
    if not hits:
        print("（未检索到任何片段）")
    for i, hit in enumerate(hits, 1):
        if hybrid:
            head = (
                f"[片段 {i}] 来源: {_KB_NAME} | 第 {hit.line_start}-{hit.line_end} 行 "
                f"| 向量 {hit.vec_sim:.4f} | 关键词 {hit.kw_score:.4f} | 融合 {hit.final_score:.4f}"
            )
        else:
            # --no-rerank：保持升级前的输出格式，便于新旧结果对比
            head = (
                f"[片段 {i}] 来源: {_KB_NAME} | 第 {hit.line_start}-{hit.line_end} 行 "
                f"| 相似度 {hit.vec_sim:.4f}"
            )
        print(head)
        if hybrid and hit.matched:
            print(f"         命中关键词：{'、'.join(hit.matched)}")
        print(SUB)
        print(hit.doc.page_content.strip())
        print(SUB)
    print()


_ENTRY_ID_RE = re.compile(r"【(Q\d-\d\d)】")


def entry_ids_of(text: str) -> list:
    """抽出片段正文里包含的条目编号（如 Q4-08），按出现顺序去重。

    为什么要它：片段是按字符数切分的，经常**从条目中间开始**，正文里未必带编号；
    反过来一个片段也可能横跨两条。把编号显式告诉模型，它才能把"步骤 7/8/9"
    落回到具体条目上，而不是因为不认识这段文字就拒答。
    """
    seen, out = set(), []
    for m in _ENTRY_ID_RE.finditer(text or ""):
        eid = m.group(1)
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


_PROMPT_RULES_LEGACY = """【回答规则】
1. 只使用《知识库片段》中的信息，不得依赖片段之外的常识补充具体步骤。
2. 若片段中没有答案，直接回答："知识库中未收录该信息，建议联系 IT 服务台（内线 8800）。"
3. 用简体中文回答，结构清晰；涉及操作时按 1. 2. 3. 编号列出步骤。
4. 引用到具体条目时，标注来源条目号，例如【Q5-02】。
5. 不要编造命令、IP、邮箱、电话或流程细节。"""

_PROMPT_RULES_CURRENT = """【回答规则】
1. 只使用《知识库片段》中的信息，不得依赖片段之外的常识补充具体步骤。
2. **片段里有多少就用多少**：只要片段包含与问题相关的条目、步骤或错误码说明，就先把这些
   可自助的部分整理出来并标明条目号（例如【Q4-08】）；**不要因为片段不完整就整段拒答**。
3. 只有在《知识库片段》与问题**完全无关**时，才回答：
   "知识库中未收录该信息，建议联系 IT 服务台（内线 8800）。"
4. 片段只覆盖流程的一部分时（例如只有错误码对照表、缺少前置条件），要明确指出
   "片段未覆盖的部分需要联系服务台"，并说明缺的是哪一步 —— 而不是含糊带过。
5. 用简体中文回答，结构清晰；涉及操作时按 1. 2. 3. 编号列出步骤。
6. 引用到具体条目时，标注来源条目号，例如【Q5-02】。
7. 不要编造命令、IP、邮箱、电话或流程细节。"""


def prompt_variant() -> str:
    """当前提示词变体：current（默认）或 legacy。

    为什么要这个开关：
        做提示词 A/B 必须能用**同一套评测集**跑出「改前 / 改后」。用环境变量切换
        （而不是改代码再改回来）才能保证两次运行只差"提示词"这一个变量，
        否则结论没法归因（这类混淆我在文档里批评过，自己不能再犯）。
    """
    value = os.environ.get("RAG_PROMPT_VARIANT", "").strip().lower()
    return "legacy" if value == "legacy" else "current"


def build_prompt(hits: list, question: str) -> str:
    blocks = []
    for i, hit in enumerate(hits, 1):
        content = (hit.doc.page_content or "").strip()
        ids = entry_ids_of(content)
        header = f"[片段 {i} | 第 {hit.line_start}-{hit.line_end} 行"
        if ids:
            header += " | 条目 " + "、".join(f"【{eid}】" for eid in ids)
        header += "]"
        blocks.append(f"{header}\n{content}")
    context = "\n\n".join(blocks) if blocks else "（无）"
    rules = _PROMPT_RULES_LEGACY if prompt_variant() == "legacy" else _PROMPT_RULES_CURRENT

    return f"""你是企业内部 IT 服务台的助理。请严格依据下面的《知识库片段》回答用户问题。

{rules}

【知识库片段】
{context}

【用户问题】
{question}
"""


# --------------------------------------------------------------------------
# 7) 双引擎生成 + 降级（逻辑未改动，仅让 LLM 构造支持自定义 temperature/timeout）
# --------------------------------------------------------------------------
def _zhipu_llm(temperature: float = 0.3, timeout: int = 30):
    return ChatOpenAI(
        base_url=ZHIPU_BASE_URL,
        api_key=ZHIPU_API_KEY,
        model=ZHIPU_CHAT_MODEL,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,  # 关掉内部重试，让降级立刻发生，而不是卡 90 秒
    )


def _ollama_llm(temperature: float = 0.3, timeout: int = 180):
    return ChatOpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        model=OLLAMA_CHAT_MODEL,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,
    )


def call_zhipu(prompt: str):
    """成功返回 (答案, None)；失败返回 (None, 异常)。"""
    if not ZHIPU_API_KEY:
        return None, RuntimeError(
            "未检测到环境变量 ZHIPU_API_KEY（未设置或为空）"
        )
    try:
        resp = _zhipu_llm().invoke(prompt)
        text = (getattr(resp, "content", "") or "").strip()
        if not text:
            return None, RuntimeError("智谱返回了空内容")
        return text, None
    except Exception as e:  # noqa: BLE001  超时 / 鉴权 / 限流 / 网络全部兜住
        return None, e


def call_ollama(prompt: str):
    """成功返回 (答案, None)；失败返回 (None, 异常)。"""
    try:
        resp = _ollama_llm().invoke(prompt)
        text = (getattr(resp, "content", "") or "").strip()
        if not text:
            return None, RuntimeError("Ollama 返回了空内容")
        return text, None
    except Exception as e:  # noqa: BLE001
        return None, e


def generate(prompt: str, engine: str, no_fallback: bool):
    """返回 (答案文本, 引擎标签)。任何无法继续的情况都以非零码退出。"""

    # ---- 只用备用引擎 ----
    if engine == "ollama":
        answer, e = call_ollama(prompt)
        if e is not None:
            die(
                f"本地 Ollama 不可用：{describe_exc(e)}\n"
                f"       请确认：1) 已执行 ollama pull {OLLAMA_CHAT_MODEL}；"
                f"2) ollama serve 正在运行；3) {OLLAMA_BASE_URL} 可访问。",
                code=3,
            )
        return answer, f"本地 Ollama {OLLAMA_CHAT_MODEL}"

    # ---- 只用主引擎 ----
    if engine == "zhipu":
        answer, e = call_zhipu(prompt)
        if e is not None:
            die(
                f"智谱主引擎不可用：{describe_exc(e)}\n"
                "       已指定 --engine zhipu，不执行降级。"
                "如需自动降级请去掉该参数。",
                code=2,
            )
        return answer, f"智谱 {ZHIPU_CHAT_MODEL}"

    # ---- auto：主引擎优先，失败降级 ----
    answer, e = call_zhipu(prompt)
    if e is None:
        return answer, f"智谱 {ZHIPU_CHAT_MODEL}"

    print("[警告] 智谱主引擎不可用，正在降级至本地 Ollama...")
    print(f"[原因] {describe_exc(e)}")

    if no_fallback:
        die(
            "已指定 --no-fallback，不执行降级，本次问答终止。\n"
            f"       智谱失败原因：{describe_exc(e)}",
            code=2,
        )

    answer, e2 = call_ollama(prompt)
    if e2 is not None:
        die(
            "主引擎与备用引擎均不可用。\n"
            f"       智谱：{describe_exc(e)}\n"
            f"       Ollama：{describe_exc(e2)}\n"
            f"       请检查：ZHIPU_API_KEY 是否有效；"
            f"ollama pull {OLLAMA_CHAT_MODEL} 是否已执行；"
            f"Ollama 服务是否已启动（{OLLAMA_BASE_URL}）。",
            code=3,
        )
    return answer, f"本地 Ollama {OLLAMA_CHAT_MODEL}（降级）"


# --------------------------------------------------------------------------
# 8) CLI
# --------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="rag_demo.py",
        description=(
            "企业级 RAG 演示：查询改写 + 向量/关键词混合检索 + 智谱与 Ollama 双引擎生成"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python rag_demo.py "我的VPN坏了"                       # 默认：改写 + 混合检索\n'
            '  python rag_demo.py "我的VPN坏了" --no-rerank           # 退回纯向量检索\n'
            '  python rag_demo.py "我的VPN坏了" --no-rewrite          # 只做关键词混合，不改写\n'
            '  python rag_demo.py "我的VPN坏了" --keyword-weight 0.4  # 调关键词权重\n'
            '  python rag_demo.py "如何重置密码？" --engine zhipu\n'
            '  python rag_demo.py "如何重置密码？" --engine ollama --embedder ollama\n'
            '  python rag_demo.py "我的VPN坏了" --keyword-only        # 零依赖：只用本地关键词召回\n'
            '  python rag_demo.py "我的VPN坏了" --no-vec-fallback     # 对照实验：禁用检索降级\n'
            "  python rag_demo.py --rebuild\n"
        ),
    )
    p.add_argument("question", nargs="?", default=None, help="要提问的问题")
    p.add_argument(
        "--engine",
        choices=["auto", "zhipu", "ollama"],
        default="auto",
        help="生成引擎：auto=智谱优先并自动降级（默认）；zhipu=只用智谱；ollama=只用本地 Ollama",
    )
    p.add_argument(
        "--embedder",
        choices=["zhipu", "ollama"],
        default="zhipu",
        help="向量化引擎：zhipu=智谱 embedding-3（默认）；ollama=本地向量化（用于全离线演示）",
    )
    p.add_argument("--k", type=int, default=DEFAULT_K, help=f"返回片段数，默认 {DEFAULT_K}")

    # --rerank / --no-rerank：默认开启，故用两个开关共同控制同一个 dest
    p.add_argument(
        "--rerank",
        dest="rerank",
        action="store_true",
        help="开启【查询改写 -> 向量+关键词混合检索 -> 打印检索结果】全链路（默认开启）",
    )
    p.add_argument(
        "--no-rerank",
        dest="rerank",
        action="store_false",
        help="关闭改写与混合重排，退回升级前的纯向量检索（便于新旧对比）",
    )
    p.set_defaults(rerank=True)

    p.add_argument(
        "--rewrite",
        dest="rewrite",
        action="store_true",
        help="调用大模型做查询改写（**默认关闭**：40 题评测显示它是负优化且慢 14 倍，"
             "需要时显式打开；仅 --rerank 生效）",
    )
    p.add_argument(
        "--no-rewrite",
        dest="rewrite",
        action="store_false",
        help="跳过 LLM 改写（默认行为；显式写出来让命令自解释、也便于脚本对照）",
    )
    p.set_defaults(rewrite=False)

    p.add_argument(
        "--keyword-weight",
        type=float,
        default=DEFAULT_KEYWORD_WEIGHT,
        help=f"关键词得分融合权重 α（0~1），final=(1-α)·向量+α·关键词，默认 {DEFAULT_KEYWORD_WEIGHT}",
    )
    p.add_argument(
        "--candidate-k",
        type=int,
        default=0,
        help=f"混合重排的候选池大小，0=自动（max(k*{CANDIDATE_K_FACTOR}, {CANDIDATE_K_MIN})）",
    )
    p.add_argument("--rebuild", action="store_true", help="强制重建向量库")
    p.add_argument("--no-fallback", action="store_true", help="禁用降级：主引擎失败即退出")
    p.add_argument(
        "--keyword-only",
        action="store_true",
        help="只用本地关键词召回：完全跳过向量库与 Embedding，零外部依赖、不需要任何 API Key",
    )
    p.add_argument(
        "--no-vec-fallback",
        action="store_true",
        help="禁用「向量召回失败 -> 本地关键词召回」的自动降级（失败即退出，用于对照实验）",
    )
    p.add_argument("--show-prompt", action="store_true", help="打印最终发给模型的完整 Prompt")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if IMPORT_ERROR is not None:
        die(
            f"缺少依赖：{describe_exc(IMPORT_ERROR)}\n"
            "       请先执行：\n"
            "         pip install -r requirements.txt\n"
            "       或：\n"
            "         pip install langchain langchain-community langchain-openai chromadb"
        )

    if args.k <= 0:
        die("--k 必须为正整数。")
    if not 0.0 <= args.keyword_weight <= 1.0:
        die("--keyword-weight 必须在 0.0 ~ 1.0 之间（例如 0.3）。")
    if args.candidate_k < 0:
        die("--candidate-k 不能为负数（0 表示自动）。")

    # 启动时打印一行配置来源，避免"配了却没生效"的困惑（只报来源与键数量，不含任何键值）
    if _ENV_INFO["source"] == "系统环境变量":
        _env_src = f"配置来源: 系统环境变量（未发现 {ENV_FILE}，也无 Secrets）"
    else:
        _env_src = (
            f"配置来源: {_ENV_INFO['source']}"
            f"（已识别 {_ENV_INFO['keys']} 个环境变量，键值不打印）"
        )
    _key_state = (
        f"ZHIPU_API_KEY: 已配置（长度 {len(ZHIPU_API_KEY)}）"
        if ZHIPU_API_KEY
        else "ZHIPU_API_KEY: 未配置"
    )
    _retrieval_mode = (
        "本地关键词（--keyword-only）" if args.keyword_only
        else (
            "纯向量（--no-rerank）" if not args.rerank
            else ("改写 + 混合重排（--rewrite）" if args.rewrite else "向量+关键词混合（默认，不做 LLM 改写）")
        )
    )
    _fallback_state = "关闭（--no-vec-fallback）" if args.no_vec_fallback else "开启"
    print(
        f"[配置] {_env_src} | {_key_state} | "
        f"生成引擎: {args.engine} | 向量化: {args.embedder} | "
        f"检索模式: {_retrieval_mode} | 检索降级: {_fallback_state}"
    )

    # 提前做一次关键配置体检，给出比 traceback 更友好的提示
    if args.embedder == "zhipu" and not ZHIPU_API_KEY and not args.rebuild:
        warn(
            "未设置 ZHIPU_API_KEY：向量检索这一步会失败。\n"
            f"       本地：在 {ENV_FILE} 中写入一行 ZHIPU_API_KEY=sk-你的Key\n"
            "       云端：在 Streamlit Cloud → Settings → Secrets 里同名键配置\n"
            '       或临时执行：$env:ZHIPU_API_KEY = "sk-你的Key" 后重试。'
        )

    if args.keyword_only:
        # 零依赖模式：不加载向量库、不构造 Embedding，连 Key 都不需要
        info("--keyword-only：跳过向量库与 Embedding，仅使用本地关键词召回（零外部依赖）")
        db = None
    else:
        try:
            db = build_or_load_vectorstore(args.embedder, args.rebuild)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            die(f"初始化向量库时发生未预期错误：{describe_exc(e)}")

    # 只重建索引、不提问（--keyword-only 下没有向量库可重建）
    if not args.question:
        if db is None:
            die("--keyword-only 模式下没有向量库可重建；请去掉该参数后再执行 --rebuild。")
        count, stored = _index_info(db)
        info(f"已就绪：{count} 个片段（embedder={stored or args.embedder}）。"
             "现在可以用 python rag_demo.py \"你的问题\" 提问。")
        return 0

    question = args.question.strip()
    candidate_k = (
        args.candidate_k if args.candidate_k > 0
        else max(args.k * CANDIDATE_K_FACTOR, CANDIDATE_K_MIN)
    )

    if args.rerank:
        # ---- ① 查询改写 ----
        if args.rewrite and args.keyword_only:
            rr = RewriteResult(
                query=question,
                keywords=[],
                source="--keyword-only",
                ok=False,
                note="--keyword-only 是零依赖模式，本次跳过 LLM 改写，改用本地分词",
            )
        elif args.rewrite:
            allow_ollama = args.engine in ("auto", "ollama")
            rr = rewrite_query(question, allow_ollama=allow_ollama)
            if not rr.ok:
                warn(
                    "查询改写不可用，本次沿用原始问题检索（混合检索仍照常执行）。\n"
                    f"       原因：{rr.note}"
                )
        else:
            rr = RewriteResult(
                query=question,
                keywords=[],
                source="默认（未开启 --rewrite）",
                ok=False,
                note="本次不做 LLM 改写（默认行为；需要改写请加 --rewrite），改用本地分词",
            )

        terms = build_terms(rr.query or question, rr.keywords)
        print_rewrite_trace(question, rr, terms)

        # ---- ② 向量 + 关键词 混合检索（含检索侧降级）----
        t0 = time.time()
        if db is None:                      # --keyword-only：不碰向量库、不需要任何 Key
            hits, pool = retrieve_keyword_only(
                question, rr.query or question, terms, args.k
            )
            mode, note = "keyword-only", "--keyword-only：按你的要求跳过向量检索"
            if not hits:
                die(
                    "关键词召回没有任何片段：请确认 data/chroma_db 可读"
                    "（或去掉 --keyword-only 后执行 --rebuild 重建向量库）。"
                )
        else:
            hits, pool, mode, note = retrieve_with_fallback(
                db,
                original_question=question,
                rewritten_query=rr.query or question,
                terms=terms,
                k=args.k,
                keyword_weight=args.keyword_weight,
                candidate_k=candidate_k,
                allow_fallback=not args.no_vec_fallback,
            )
        print(
            f"[信息] 候选池 {len(pool)} 个片段（每路召回 top-{candidate_k}，已按内容去重合并），"
            f"重排后取 {len(hits)} 个，用时 {time.time() - t0:.2f}s\n"
        )

        # ---- ③ 打印检索结果（降级时先打横幅，避免把关键词结果误读成向量结果）----
        print_fallback_banner(note, mode)
        print_hybrid_trace(pool, args.k, args.keyword_weight)
        print_keyword_explanation(hits, mode=mode)
        print_context(hits, hybrid=True)
    else:
        # 升级前的行为：纯向量检索 + 旧输出格式（同样带检索侧降级）
        t0 = time.time()
        note, mode = "", "vector"
        if db is None:                      # --keyword-only
            hits, _pool = retrieve_keyword_only(
                question, question, build_terms(question, None), args.k
            )
            note = "--keyword-only：按你的要求跳过向量检索"
            mode = "keyword-only"
        else:
            try:
                hits = retrieve(db, question, args.k)
            except (SystemExit, Exception) as exc:  # noqa: BLE001  含 SystemExit 兜底
                if args.no_vec_fallback:
                    raise
                note = describe_exc(exc)
                warn(f"向量召回不可用（{note}），已降级为本地关键词召回（零外部依赖）。")
                hits, _pool = retrieve_keyword_only(
                    question, question, build_terms(question, None), args.k, db=db
                )
                mode = "keyword-only"
        if not hits:
            die(
                "检索没有拿到任何片段：请确认 data/chroma_db 可读"
                "（或去掉 --keyword-only 后执行 --rebuild 重建向量库）。"
            )
        print(f"[信息] 检索到 {len(hits)} 个片段，用时 {time.time() - t0:.2f}s\n")
        print_fallback_banner(note, mode)
        print_context(hits, hybrid=False)

    # ---- ④ 生成（始终使用原始问题，避免改写后的检索词影响回答口径）----
    prompt = build_prompt(hits, question)
    if args.show_prompt:
        print(LINE)
        print("========== 发送给模型的完整 Prompt ==========")
        print(LINE)
        print(prompt)
        print()

    t0 = time.time()
    answer, engine_label = generate(prompt, args.engine, args.no_fallback)

    print(LINE)
    print(f"[引擎] 本次回答由：{engine_label} 生成（{time.time() - t0:.1f}s）")
    print("========== 最终回答（Answer） ==========")
    print(LINE)
    print(answer)
    print(LINE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户取消。", file=sys.stderr)
        raise SystemExit(130)
