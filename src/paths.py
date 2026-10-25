# -*- coding: utf-8 -*-
"""
src/paths.py —— 全项目唯一的路径与环境变量中心（云端 Linux / 本地 Windows 通用）
=================================================================================

为什么要这个文件：
    重构后脚本分布在 src/、src/core/、src/mcp/、src/chat/、src/server/ 等不同深度。
    如果每个脚本各自写 ``Path(__file__).parent.parent``，一旦以后有人再挪动
    文件，路径就会静默指错目录（典型症状：云端报 FileNotFoundError）。
    所以这里集中做一次「动态向上寻找项目根目录」，所有脚本只 import 常量。

动态定位规则（find_project_root）：
    从本文件所在目录开始逐级向上，第一个包含 .env 或 requirements.txt 的目录
    就是项目根目录（PROJECT_ROOT）。找不到时回退到本文件的上两级目录。
    这意味着所有资源都用绝对路径拼装，与「运行时的工作目录」彻底解耦。

云端注意：
    云端环境不存在 .env（密钥走平台 Secrets），此时 markers 里的
    requirements.txt 就是根目录锚点，因此定位依然成立。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

__all__ = [
    "PROJECT_ROOT", "SRC_DIR", "ASSETS_DIR", "DATA_DIR", "LOGS_DIR", "SCRIPTS_DIR",
    "KB_FILE", "CHROMA_DIR", "TEST_DB", "ENV_FILE",
    "EMOTIONAL_HANDBOOK",
    "SERVER_SCRIPT", "PROBE_SCRIPT", "RAG_SCRIPT", "MULTI_AGENT_SCRIPT",
    "ROUTER_SCRIPT", "QUERY_STUDENT_SCRIPT", "AGENT_TOOL_SCRIPT",
    "CHAT_DIR", "CHAT_SAFETY_SCRIPT", "CHAT_STATE_SCRIPT",
    "CHAT_PERSONA_SCRIPT", "CHAT_CONVERSATION_SCRIPT", "CHAT_HISTORY_SCRIPT",
    "CHAT_FEEDBACK_SCRIPT", "CHAT_PROFILE_SCRIPT", "CHAT_KNOWLEDGE_SCRIPT",
    "CHAT_WEATHER_SCRIPT", "CHAT_TRANSLATION_SCRIPT", "CHAT_LANGUAGES_SCRIPT",
    "CHAT_HISTORY_DIR", "CHAT_FEEDBACK_DIR", "PROFILE_DIR",
    "USERS_FILE", "SESSIONS_FILE", "INVITES_FILE",
    "PERSONA_FILE", "EMOTION_EVAL_FILE",
    "INTERPRETER", "VENV_CANDIDATES",
    "find_project_root", "bootstrap", "load_env", "subprocess_env",
    "mask_uri", "mask_secrets",
]

# 根目录锚点文件：命中任意一个即认为是项目根目录
_ROOT_MARKERS = ("requirements.txt", ".env")

# 虚拟环境里的解释器（本地优先用装了 langchain/langgraph/chromadb 的那个环境）。
# 云端没有这些目录，list 为空，自动退化为当前解释器 INTERPRETER。
_VENV_RELATIVE_PYTHONS = (
    ".venv-rag/Scripts/python.exe",   # Windows + .venv-rag
    ".venv/Scripts/python.exe",       # Windows + .venv
    ".venv-rag/bin/python",           # Linux/macOS + .venv-rag
    ".venv/bin/python",               # Linux/macOS + .venv
)


def find_project_root(start: Path | None = None) -> Path:
    """从 start（默认本文件所在目录）向上逐级查找项目根目录。"""
    current = (start or Path(__file__).resolve().parent).resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).is_file() for marker in _ROOT_MARKERS):
            return candidate
    # 兜底：本文件位于 <root>/src/paths.py，所以上两级就是根目录
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT: Path = find_project_root()
SRC_DIR: Path = PROJECT_ROOT / "src"
ASSETS_DIR: Path = PROJECT_ROOT / "assets"
DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"

# ---- 数据资源（重构后统一收纳在 data/ 下）----
# 情绪自助手册：项目现在**唯一**的知识库。
# 它既是陪伴 Agent 的检索语料（src/chat/knowledge.py），
# 也是 RAG 演示（src/rag_demo.py）的语料 —— 一份数据两用，不再有第二份。
EMOTIONAL_HANDBOOK: Path = DATA_DIR / "emotional_handbook.txt"
# 兼容别名：老代码/老文档里的 KB_FILE 现在指向同一份情绪手册。
KB_FILE: Path = EMOTIONAL_HANDBOOK
CHROMA_DIR: Path = DATA_DIR / "chroma_db"
TEST_DB: Path = DATA_DIR / "test.db"

# ---- 脚本之间的互相调用（子进程用绝对路径启动，与工作目录无关）----
SERVER_SCRIPT: Path = SRC_DIR / "mcp_demo" / "mcp_weather_server.py"
PROBE_SCRIPT: Path = SRC_DIR / "mcp_demo" / "mcp_protocol_probe.py"
RAG_SCRIPT: Path = SRC_DIR / "rag_demo.py"
MULTI_AGENT_SCRIPT: Path = SRC_DIR / "multi_agent_demo.py"
ROUTER_SCRIPT: Path = SRC_DIR / "core" / "router.py"
QUERY_STUDENT_SCRIPT: Path = SRC_DIR / "core" / "query_student.py"
AGENT_TOOL_SCRIPT: Path = SRC_DIR / "core" / "agent_tool_demo.py"

# ---- 情绪陪伴 Agent（src/chat/）：P0 两个模块 + P1 界面/会话逻辑 ----
CHAT_DIR: Path = SRC_DIR / "chat"
CHAT_SAFETY_SCRIPT: Path = CHAT_DIR / "safety.py"
CHAT_STATE_SCRIPT: Path = CHAT_DIR / "state.py"
CHAT_PERSONA_SCRIPT: Path = CHAT_DIR / "persona.py"
CHAT_CONVERSATION_SCRIPT: Path = CHAT_DIR / "conversation.py"
CHAT_HISTORY_SCRIPT: Path = CHAT_DIR / "history.py"     # S3：对话历史持久化
CHAT_FEEDBACK_SCRIPT: Path = CHAT_DIR / "feedback.py"   # S5：对话内反馈埋点
CHAT_PROFILE_SCRIPT: Path = CHAT_DIR / "profile.py"     # S4：动态知识点档案
CHAT_KNOWLEDGE_SCRIPT: Path = CHAT_DIR / "knowledge.py"  # S4：情绪自助手册检索
CHAT_WEATHER_SCRIPT: Path = CHAT_DIR / "weather.py"      # S2：全球实时天气
CHAT_TRANSLATION_SCRIPT: Path = CHAT_DIR / "translation.py"  # S2：多引擎翻译
CHAT_LANGUAGES_SCRIPT: Path = CHAT_DIR / "languages.py"      # S2：语言注册表
# 对话历史落地目录：只写本机，永不上云；超过保留期由 history.prune() 删除。
# 注意：已在 .gitignore 里忽略，避免私聊记录被推到 GitHub。
CHAT_HISTORY_DIR: Path = DATA_DIR / "chat_history"
# 档案与反馈：同为本机数据，已在 .gitignore 忽略。
CHAT_FEEDBACK_DIR: Path = DATA_DIR / "feedback"
# S4：关于用户本人的长期信息（**只存用户确认过的**）
PROFILE_DIR: Path = DATA_DIR / "profile"
# 登录/会话/邀请：auth 层的小 JSON，已在 .gitignore 忽略。
USERS_FILE: Path = DATA_DIR / "users.json"
SESSIONS_FILE: Path = DATA_DIR / "sessions.json"
INVITES_FILE: Path = DATA_DIR / "invites.json"
PERSONA_FILE: Path = DATA_DIR / "persona.yaml"        # P3：人格参数化的 YAML 版本
EMOTION_EVAL_FILE: Path = DATA_DIR / "eval" / "emotion.jsonl"   # P4：情绪评测集

# ---- 配置 ----
ENV_FILE: Path = PROJECT_ROOT / ".env"

# ---- 解释器 ----
INTERPRETER: Path = Path(sys.executable)
VENV_CANDIDATES: tuple[Path, ...] = tuple(
    PROJECT_ROOT / rel for rel in _VENV_RELATIVE_PYTHONS
)


def bootstrap() -> Path:
    """把仓库根与 src/ 加入 sys.path，返回 PROJECT_ROOT。

    每个「可独立运行的脚本」在 import 本项目模块之前先调用一次即可：
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from paths import PROJECT_ROOT, bootstrap
        bootstrap()
    这样 ``import paths`` 与 ``import core.router`` 这类跨目录导入在任何
    工作目录、任何操作系统下都成立。
    """
    for path in (PROJECT_ROOT, SRC_DIR):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    return PROJECT_ROOT


def _iter_declared_keys() -> list[str]:
    """列出 .env / .env.example 里出现过的键名。

    只返回「键名」，绝不返回键值，避免密钥进入日志或页面。
    """
    keys: list[str] = []
    for file in (ENV_FILE, PROJECT_ROOT / ".env.example"):
        try:
            for raw in file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                if key and key not in keys:
                    keys.append(key)
        except OSError:
            continue
    return keys


def load_env() -> dict:
    """加载配置：读根目录 .env（只写 os.environ，绝不回显）。

    返回值只有「是否加载到 / 来源描述 / 键数量」，不含任何键值：
        {"env_file": str|None, "secrets_used": bool, "keys": int, "source": str}
    设计原则：只写入 os.environ（子进程可通过 os.environ 透传），
    绝不打印、绝不回显、绝不返回任何 Key 的内容。
    """
    loaded: list[str] = []
    env_file_used: str | None = None

    # ---- 1) 本地 .env（云端通常不存在，失败也不影响）----
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None and ENV_FILE.is_file():
        try:
            load_dotenv(str(ENV_FILE), override=False)
            env_file_used = str(ENV_FILE)
            loaded.append(".env")
        except Exception:
            env_file_used = None

    declared = _iter_declared_keys()
    configured = [k for k in declared if os.environ.get(k)]
    source = " + ".join(loaded) if loaded else "系统环境变量"
    return {
        "env_file": env_file_used,
        "secrets_used": False,
        "secrets_seen": 0,
        "keys": len(configured),
        "source": source,
    }


def subprocess_env() -> dict:
    """构造子进程环境变量：透传配置 + 强制 UTF-8 + 允许子进程 import 本项目模块。

    云端 Linux 默认就是 UTF-8，显式设置无害；它主要解决 Windows 下
    管道输出按 GBK 编码导致的乱码问题。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    src_text = str(SRC_DIR)
    existing = env.get("PYTHONPATH", "")
    if src_text not in existing.split(os.pathsep):
        env["PYTHONPATH"] = f"{src_text}{os.pathsep}{existing}" if existing else src_text
    return env


# ---------------------------------------------------------------------------
# 密钥脱敏（只打码，永远不还原、不落盘、不回显）
#
# 为什么放在 paths.py：
#     它是全项目唯一的公共模块，脚本与服务端都会 import 它，因此脱敏
#     规则只有一处定义，不会各写一份、各自漂移。
#
# 分层防御：
#     第 1 层（源头）：代码本身不打印密钥值，只打印「键名/来源/数量」；
#     第 2 层（出口）：真要打印连接串时走 mask_uri()，只留主机与库名；
#     第 3 层（兜底）：子脚本 + 第三方库的 stdout/stderr 在渲染进网页前
#                     统一走 mask_secrets()，避免任何意外把凭据送到浏览器。
# ---------------------------------------------------------------------------
# mongodb://user:pass@host  ->  mongodb://***:***@host
_URI_USERINFO_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@"
)

# 其余常见密钥形态：sk-xxx / Bearer xxx / key=value 形式的凭据
_SECRET_RULES = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token"
            r"|password|passwd|pwd|secret)\b(\s*[=:]\s*)([^\s,;'\"]+)"
        ),
        r"\1\2***",
    ),
)


def mask_uri(uri: object) -> str:
    """把连接串里的账号密码打码，保留 scheme / 主机 / 端口 / 库名，便于排错对照。

    mongodb+srv://yrd:secret@cluster0.x.mongodb.net/?retryWrites=true
        -> mongodb+srv://***:***@cluster0.x.mongodb.net/?retryWrites=true

    无凭据的连接串原样返回（本地 localhost 场景不产生噪音）。
    """
    if not uri:
        return ""
    return _URI_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}***:***@", str(uri))


def mask_secrets(text: object) -> str:
    """通用脱敏兜底：任何要写日志或渲染到页面的文本，先过一遍这里。

    覆盖：连接串凭据、sk- 开头的 Key、Bearer 令牌、key=value / key: value 形式的密钥。
    注意：这是「最后一道网」，不是唯一一道 —— 代码本来也不该打印密钥值。
    """
    if not text:
        return ""
    out = _URI_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}***:***@", str(text))
    for pattern, repl in _SECRET_RULES:
        out = pattern.sub(repl, out)
    return out
