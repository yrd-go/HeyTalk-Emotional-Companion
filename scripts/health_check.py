# -*- coding: utf-8 -*-
"""
scripts/health_check.py —— 重构后的全量健康自检（本地 Windows / 云端 Linux 通用）
=================================================================================

一次运行完成 6 类检查，任何一类失败都会明确告诉你「哪个文件、哪一行、怎么修」：

    1.   目录结构   : 期望的目录/文件是否都在位（含根目录必须保留的 README/png）
    2.   硬编码路径 : 扫描 .py 里遗留的 Windows 绝对路径（C:\\Users\\...、反斜杠等）
    3.   静态导入   : 用 ast 解析每个 .py 的顶层 import，逐一确认模块可被解析
    3.5  Path 边界  : 找出「Path 对象被直接交给第三方 API」的写法
                     （chromadb 内部 persist_directory + "/chroma.sqlite3" 就会因此崩）
    4.   路径常量   : 从 src/paths.py 加载常量，确认指向的文件/目录真实存在
    5.   启动烟雾   : 用子进程真实拉起每个入口脚本（带超时），捕获异常与 traceback

用法：
    python scripts/health_check.py            # 全部检查
    python scripts/health_check.py --quick    # 跳过第 5 项（不启动子进程，最快）

退出码：0 = 全部通过（允许存在 WARN）；1 = 至少一项 FAIL。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 定位仓库根：原则与 src/paths.py 一致 —— 向上找 requirements.txt / .env
# ---------------------------------------------------------------------------
def find_project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "requirements.txt").is_file() or (candidate / ".env").is_file():
            return candidate
    return current


PROJECT_ROOT = find_project_root(Path(__file__).resolve().parent)
SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
LOGS_DIR = PROJECT_ROOT / "logs"

# 顶层 import 到「第三方包名」的映射：import 名 -> 安装名提示
OPTIONAL_PACKAGES = {
    "pymongo": "pymongo",
    "dotenv": "python-dotenv",
    "langchain": "langchain",
    "langchain_core": "langchain-core",
    "langchain_openai": "langchain-openai",
    "langchain_community": "langchain-community",
    "langgraph": "langgraph",
    "langchain_mcp_adapters": "langchain-mcp-adapters",
    "chromadb": "chromadb",
    "mcp": "mcp",
}

# 期望存在的目录/文件（相对仓库根）
EXPECTED_DIRS = ("src", "src/core", "src/mcp_demo", "src/server", "data", "scripts",
                 "scripts/js", "scripts/legacy", "logs", "assets/samples", "web",
                 "docs", "docs/images", "tests", ".github/workflows")
EXPECTED_FILES = (
    "requirements.txt",
    "DEPLOY.md",
    "DEPLOY_STRUCTURE.md",
    ".dockerignore",
    "src/__init__.py",
    "src/paths.py",
    "src/rag_demo.py",
    "src/multi_agent_demo.py",
    "src/core/__init__.py",
    "src/core/router.py",
    "src/core/query_student.py",
    "src/core/init_db.py",
    "src/core/agent_tool_demo.py",
    "src/core/employee_api.py",
    "src/mcp_demo/__init__.py",
    "src/mcp_demo/mcp_weather_server.py",
    "src/mcp_demo/mcp_weather_client.py",
    "src/mcp_demo/mcp_protocol_probe.py",
    "src/server/__init__.py",
    "src/server/api.py",
    "src/server/auth.py",
    "src/server/ratelimit.py",
    "src/server/transcribe.py",
    "web/package.json",
    "web/index.html",
    "data/emotional_handbook.txt",
    "data/eval/qa.jsonl",
    "data/test.db",
    "scripts/health_check.py",
    "scripts/import_check.py",
    "scripts/legacy/Dockerfile.legacy",
    "README.md",
    "README_EN.md",
    "LICENSE",
    "docs/PITFALLS.md",
    ".github/workflows/ci.yml",
)
# 允许缺失（存在则报告为 OK，不存在只提示）
TOLERATED_FILES = (".env", "router_output.png", "assets/samples/hello.txt")

# 只在「远程仓库 / clone 出来的工作副本」里才要求存在、本地源码目录允许缺失的文件。
# 现状：README.md 与 README_EN.md 都已改为「本地即改动源」（推送由
# scripts/publish_docs_to_github.ps1 完成），因此这里已没有 clone-only 文件；
# 保留该机制是为了以后真有「只在远端维护」的文件时可以直接用。
CLONE_ONLY_FILES: tuple[str, ...] = ()

# 硬编码路径检测规则：(正则/子串, 说明)
HARDCODE_PATTERNS = (
    (r"C:\\+Users", "Windows 用户目录绝对路径"),
    (r"C:/+Users", "Windows 用户目录绝对路径（正斜杠写法）"),
    (r"[A-Za-z]:\\\\", "盘符绝对路径"),
    (r"Administrator", "含主机用户名的路径"),
)

# 「Path 对象进入第三方 API」检测规则
#   —— 背景：langchain 会把 persist_directory 原样塞给 chromadb，而
#      chromadb/db/impl/sqlite.py 内部执行 `persist_directory + "/chroma.sqlite3"`，
#      传 Path 对象就抛 TypeError: unsupported operand type(s) for +: 'WindowsPath' and 'str'。
#      凡是把 Path 交给第三方库的地方，一律显式 str() / os.fspath() 才是稳的。
PATH_ARG_CALLS = (
    "persist_directory", "Chroma", "from_documents", "shutil.rmtree", "subprocess.run", "open",
)
# 注意：刻意不检查 os.path.* / os.makedirs 等：它们内部走 __fspath__，传 Path 是安全的
PATH_ARG_RE = re.compile(
    r"\b(?P<call>" + "|".join(PATH_ARG_CALLS) + r")\w*\s*\([^)]*?"
    r"(?P<arg>[A-Z_][A-Z0-9_]{2,})\b"
)
PATH_ARG_SAFE_WORDS = ("str(", "fspath(", "os.fspath(", "sys.executable", '"/', "'/", "request")

SKIP_DIR_NAMES = {".venv", ".venv-rag", "venv", "env", "__pycache__", ".git", "node_modules"}

RESULTS: list[tuple[str, str, str]] = []   # (级别 OK/WARN/FAIL/SKIP, 标题, 细节)

# 控制台统一按 UTF-8 输出：避免 Windows GBK 终端打印中文/符号时直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ICONS = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}
# 沙箱禁止子进程创建管道时的特征串：这类失败是环境限制，不是代码问题
SANDBOX_MARKERS = ("WinError 5", "PermissionError", "拒绝访问", "EPERM",
                   "operation not permitted")


def record(level: str, title: str, detail: str = "") -> None:
    RESULTS.append((level, title, detail))
    print(f"{ICONS[level]} {title}" + (f"\n       {detail}" if detail else ""))


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


# ---------------------------------------------------------------------------
# 1) 目录结构
# ---------------------------------------------------------------------------
def check_layout() -> None:
    print("\n【1/5】目录结构检查")
    missing_dirs = [d for d in EXPECTED_DIRS if not (PROJECT_ROOT / d).is_dir()]
    missing_files = [f for f in EXPECTED_FILES if not (PROJECT_ROOT / f).is_file()]
    if missing_dirs or missing_files:
        record("FAIL", "目录结构不完整",
               "缺少目录: " + (", ".join(missing_dirs) or "无") +
               " | 缺少文件: " + (", ".join(missing_files) or "无"))
    else:
        record("OK", f"目录结构完整（{len(EXPECTED_DIRS)} 个目录 / {len(EXPECTED_FILES)} 个关键文件）")

    # 可选资产（截图已归入 docs/images/，根目录只留 router_output.png 这张历史产物）
    kept = [f for f in TOLERATED_FILES if (PROJECT_ROOT / f).is_file()]
    record("OK" if "router_output.png" in kept else "WARN",
           "可选资产（缺失不致命：.env / 历史截图 / 示例文本）",
           "存在: " + (", ".join(kept) or "无"))

    # clone-only 文件：本地源码目录允许缺失，clone 出来的工作副本里应当存在
    if not CLONE_ONLY_FILES:
        record("OK", "无 clone-only 文件", "README 中英文都在本地维护，本地即改动源")
    else:
        clone_only = [f for f in CLONE_ONLY_FILES if not (PROJECT_ROOT / f).is_file()]
        if clone_only:
            record("OK", f"{', '.join(clone_only)} 未在本地源码目录（预期行为）",
                   "这类文件只在 clone 出来的工作副本里维护；若当前是 clone 目录，请检查是否被误删。")
        else:
            record("OK", f"{', '.join(CLONE_ONLY_FILES)} 存在（当前看起来是 clone 出来的工作副本）")

    # 旧路径残留检查
    # 说明：chroma_db 与 knowledge_base.txt 已不再是"项目文件"——
    # chroma_db 是按需重建的构建产物，IT 手册已按需求删除，
    # 所以它们**不再算残留**（继续当残留会让检查永远 FAIL 或误导）。
    leftovers = [p for p in ("web_demo", "test.db")
                 if (PROJECT_ROOT / p).exists()]
    record("FAIL" if leftovers else "OK",
           "旧位置残留检查",
           ("仍存在旧路径: " + ", ".join(leftovers)) if leftovers else "无残留")

    # 敏感文件保护检查：这两个文件必须存在但必须被 gitignore 覆盖
    for secret in (".env",):
        path = PROJECT_ROOT / secret
        if not path.is_file():
            record("OK", f"{secret} 未落盘（不存在即不会误提交）")
            continue
        gitignore_text = ""
        try:
            gitignore_text = (PROJECT_ROOT / ".gitignore").read_text(
                encoding="utf-8-sig", errors="replace")
        except OSError:
            pass
        covered = Path(secret).name in gitignore_text or secret in gitignore_text
        record("OK" if covered else "FAIL",
               f"{secret} 的提交保护",
               "已被 .gitignore 覆盖" if covered else "⚠️ 未被 .gitignore 覆盖，存在误提交风险")


# ---------------------------------------------------------------------------
# 2) 硬编码路径扫描
# ---------------------------------------------------------------------------
def check_hardcoded_paths() -> None:
    print("\n【2/5】硬编码路径扫描")

    # 本文件自身存放检测规则（含 "C:\\Users"、"Administrator" 这类字面量），必须排除
    self_path = Path(__file__).resolve()

    hits: list[str] = []
    for path in iter_python_files():
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            record("WARN", f"无法读取 {path}", str(exc))
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # 注释与文档字符串里的说明文字不算硬编码路径
            if stripped.startswith("#"):
                continue
            for pattern, why in HARDCODE_PATTERNS:
                if re.search(pattern, line):
                    hits.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno} [{why}] {stripped[:90]}")
    if hits:
        record("FAIL", f"发现 {len(hits)} 处可疑硬编码路径", "\n     ".join(hits[:12]))
    else:
        record("OK", "未发现硬编码绝对路径"
                     f"（已扫描 {len(iter_python_files()) - 1} 个 .py 文件，"
                     "scripts/health_check.py 因存放检测规则而排除）")


# ---------------------------------------------------------------------------
# 3) 静态导入检查（ast，不执行任何脚本）
# ---------------------------------------------------------------------------
def top_level_imports(path: Path) -> list[tuple[str, int]]:
    """返回 [(模块名, 行号)]，只取顶层 import / from ... import。"""
    found: list[tuple[str, int]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"), filename=str(path))
    except SyntaxError as exc:
        record("FAIL", f"语法错误：{path.relative_to(PROJECT_ROOT)}", f"第 {exc.lineno} 行：{exc.msg}")
        return found
    for node in tree.body:      # 只看顶层，函数内的延迟 import 由运行期负责
        if isinstance(node, ast.Import):
            found.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 相对导入跳过
                continue
            if node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def check_imports() -> None:
    print("\n【3/5】静态导入检查")
    # 项目自有的顶层模块 = 源码文件的 stem（rag_demo / paths ...）
    #                    + src/ 下的子包（core / ui / mcp_demo）
    #                    + 仓库根的目录（tests / scripts / docs ...）
    # 不加这几类会把 `import core.router` 误判成「缺第三方包 core」并提示 pip install core，
    # 属于自检工具自身的假阳性（tests/ 曾触发过），因此这里显式白名单化。
    local_modules = {p.stem for p in SRC_DIR.rglob("*.py") if p.name != "__init__.py"}
    local_modules |= {p.name for p in SRC_DIR.iterdir() if p.is_dir()}
    local_modules |= {p.name for p in PROJECT_ROOT.iterdir() if p.is_dir()}
    missing: dict[str, set[str]] = {}
    checked = 0

    for path in iter_python_files():
        for module, lineno in top_level_imports(path):
            checked += 1
            if module in local_modules:
                continue
            if importlib.util.find_spec(module) is None:
                missing.setdefault(module, set()).add(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")

    if missing:
        lines = []
        for module, where in sorted(missing.items()):
            hint = OPTIONAL_PACKAGES.get(module, module)
            lines.append(f"{module}（pip install {hint}）<- {', '.join(sorted(where))}")
        record("WARN", f"{len(missing)} 个模块当前环境未安装（云端由 requirements.txt 安装）",
               "\n     ".join(lines))
    else:
        record("OK", f"所有顶层导入均可解析（共检查 {checked} 条 import）")


# ---------------------------------------------------------------------------
# 4) 路径常量检查
# ---------------------------------------------------------------------------
def check_path_boundaries() -> None:
    """3.5) 扫描「Path 对象被直接交给第三方 API」的写法。

    这类写法不会立刻报错，但一旦对方库内部做字符串拼接（如 chromadb 的
    sqlite 后端）就会抛 TypeError，而且报错位置在第三方库里，极难定位。
    因此这里主动预防：命中即 FAIL（路径/名称类白名单除外）。
    """
    print("\n【3.5/5】Path 边界检查（Path 对象进第三方 API）")
    self_path = Path(__file__).resolve()
    hits: list[str] = []

    for path in iter_python_files():
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if any(word in line for word in PATH_ARG_SAFE_WORDS):
                continue
            match = PATH_ARG_RE.search(line)
            if match:
                hits.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{lineno} "
                    f"[{match.group('call')} 收到 {match.group('arg')}] {stripped[:80]}"
                )

    if hits:
        record("FAIL", f"发现 {len(hits)} 处可能把 Path 对象直接交给第三方 API",
               "\n     ".join(hits[:10]) + "\n     修法：改成 str(路径对象) 或 os.fspath(路径对象)")
    else:
        record("OK", "Path 边界干净：所有进入第三方 API 的路径均已显式 str() / os.fspath()")


def check_paths_module() -> None:
    print("\n【4/5】路径常量检查（src/paths.py）")
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        import paths  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "无法导入 src/paths.py", f"{type(exc).__name__}: {exc}")
        return

    if paths.PROJECT_ROOT.resolve() != PROJECT_ROOT.resolve():
        record("FAIL", "PROJECT_ROOT 定位错误",
               f"paths.py 算出 {paths.PROJECT_ROOT}，实际应为 {PROJECT_ROOT}")
    else:
        record("OK", f"PROJECT_ROOT 定位正确：{paths.PROJECT_ROOT}")

    must_exist = {
        # KB_FILE 现在指向**情绪手册**（项目唯一的知识库）
        "KB_FILE（情绪手册）": paths.KB_FILE,
        "SERVER_SCRIPT（MCP 服务端）": paths.SERVER_SCRIPT,
        "TEST_DB（示例 SQLite）": paths.TEST_DB,
        "requirements.txt": PROJECT_ROOT / "requirements.txt",
    }
    # 注意：CHROMA_DIR（向量库）**故意不在这里** —— 它是按需生成的构建产物。
    # 项目改造后已删除旧的 IT 向量库，情绪手册的向量库要跑一次
    # `python src/rag_demo.py --rebuild` 才会出现；不存在是正常状态。
    bad = [f"{name} -> {p}" for name, p in must_exist.items() if not p.exists()]
    record("FAIL" if bad else "OK",
           "关键路径可达性",
           ("不可达: " + " | ".join(bad)) if bad else "全部可达（绝对路径，与工作目录无关）")

    missing_scripts = [
        str(script) for script in
        (paths.RAG_SCRIPT, paths.MULTI_AGENT_SCRIPT, paths.ROUTER_SCRIPT,
         paths.QUERY_STUDENT_SCRIPT, paths.AGENT_TOOL_SCRIPT, paths.PROBE_SCRIPT)
        if not script.is_file()
    ]
    record("FAIL" if missing_scripts else "OK",
           "被调脚本路径可达性",
           ("缺失: " + ", ".join(missing_scripts)) if missing_scripts else "6 个被调脚本路径均存在")

    info = paths.load_env()
    record("OK", "配置加载（只报来源与键数量，不读值）",
           f"来源={info['source']} | 已识别键数={info['keys']} | .env={info['env_file'] or '未使用'}")
    record("OK", "子进程环境变量", f"PYTHONPATH={paths.subprocess_env().get('PYTHONPATH')}")


# ---------------------------------------------------------------------------
# 5) 启动烟雾测试（真实子进程）
# ---------------------------------------------------------------------------
SMOKE_CASES: tuple[tuple[str, str, tuple[str, ...], int, tuple[int, ...]], ...] = (
    # (展示名, 脚本相对路径, 附加参数, 超时秒, 属于「业务降级/未走到核心路径」的退出码)
    #
    # ★ 第 5 列是这次修复假绿的关键：非零退出码必须在这里声明过业务语义才会记为 WARN，
    #   没声明的一律 FAIL。旧版本写的是「returncode not in (0, 1) 才算失败」，等于把
    #   「用了 mock 数据 / 没调用工具 / 没配 Key」全部当成健康，自检因此会假绿。
    #
    # 各用例退出码语义（与各脚本 docstring 保持一致）：
    #   query_student   1=mock 数据  2=参数错误  3=严格模式下连接/查询失败
    #   router          --help 未命中任何路由 -> 打印用法并返回 1（既定行为）
    #   agent_tool_demo 1=未发生工具调用（缺 ZHIPU_API_KEY 时也走这里）
    #   MCP 客户端/探针 1/2/3=缺 Key 或 SDK、4=协议层真的失败了
    ("src/rag_demo.py --help", "src/rag_demo.py", ("--help",), 60, ()),
    # multi_agent_demo 缺 langgraph 时会打印 [FATAL] 并以 1 退出 —— 属缺依赖，
    # 记 WARN 而不是 FAIL（自检不把「本机缺件」当成代码坏了）。
    ("src/multi_agent_demo.py", "src/multi_agent_demo.py", (), 120, (1,)),
    ("src/core/router.py --help", "src/core/router.py", ("--help",), 60, (1,)),
    ("src/core/query_student.py 2", "src/core/query_student.py", ("2",), 60, (1, 2, 3)),
    ("src/core/agent_tool_demo.py --help", "src/core/agent_tool_demo.py", ("--help",), 60, (1, 2)),
    ("src/mcp_demo/mcp_weather_client.py --help",
     "src/mcp_demo/mcp_weather_client.py", ("--help",), 60, (1, 2, 3)),
    # 服务端是 stdio 长驻进程，直接跑会一直等 stdin（旧写法必然超时被记 FAIL）；
    # 这里改成「只导入、不 run()」：__main__ 守卫保证不会起服务，仍能验证 SDK 与导入链。
    ("src/mcp_demo/mcp_weather_server.py（仅导入）", "-c",
     ("import sys;sys.path.insert(0,'src');import mcp_demo.mcp_weather_server as m;"
      "print('server-sdk:', m.SDK_FLAVOR)",), 60, (1,)),
    ("src/mcp_demo/mcp_protocol_probe.py", "src/mcp_demo/mcp_protocol_probe.py", (), 90, (2, 3)),
    # 用独立脚本做导入检查（避免把多语句 Python 代码塞进 -c，受 shell 引号转义影响）
    ("scripts/import_check.py（跨目录 import 检查）", "scripts/import_check.py", (), 90, ()),
)

# 这些用例会创建子进程管道（MCP 客户端/探针要跟子进程说 stdio 协议）。
# 受限沙箱禁止创建管道时会 WinError 5；此时只报 SKIP（环境限制），不算代码失败。
PIPE_DEPENDENT_CASES = frozenset({
    "src/mcp_demo/mcp_weather_client.py --help",
    "src/mcp_demo/mcp_protocol_probe.py",
    "src/server/__init__.py",
    "src/server/api.py",
    "src/server/auth.py",
    "src/server/ratelimit.py",
    "src/server/transcribe.py",
    "web/package.json",
    "web/index.html",
})


def run_smoke(python_bin: str, script: str, args: tuple[str, ...],
              timeout: int, business_codes: tuple[int, ...] = ()) -> tuple[str, str]:
    """用子进程真实启动脚本，回传 ("OK" | "WARN" | "FAIL", 详情)。

    business_codes 是「有明确业务语义的非零退出码」（例如 query_student 的 1 = 用了
    mock 数据）。命中它只记为 WARN —— 自检不会再把握手降级误报成健康；未声明的
    非零退出码一律 FAIL。

    script 传相对仓库根的脚本路径（用正斜杠，Windows/Linux 都成立）；
    传 "-c" 时表示直接执行 args 里的内联代码。

    注意：这里刻意「用临时文件重定向 stdout/stderr」而不是 capture_output=True。
    原因：某些受限沙箱（例如 DSH 的 workspace-write 模式）禁止创建匿名管道，
    用管道捕获输出会直接抛 WinError 5 / EPERM；而普通文件重定向不受限制，
    因此本自检在受限沙箱里也能跑通。
    """
    # 解释器路径必须是绝对路径：子进程的 cwd 固定为仓库根，相对路径会解析失败
    python_bin = str(Path(python_bin).expanduser().resolve())
    if python_bin.startswith("-") or script == "-c":
        cmd = [python_bin, "-c", *args]
    else:
        cmd = [python_bin, script, *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    src_text = str(SRC_DIR)
    if src_text not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = src_text + os.pathsep + env.get("PYTHONPATH", "")

    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace",
                                    dir=str(logs_dir), prefix="smoke_", suffix=".log") as sink:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                )
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                return "FAIL", f"超时（{timeout}s）—— 可能正在等网络或交互输入"
            except OSError as exc:
                return "FAIL", f"无法启动子进程：{exc}"
            sink.seek(0)
            combined = sink.read()
    except OSError as exc:
        return "FAIL", f"无法创建日志临时文件：{exc}"

    if "Traceback (most recent call last)" in combined:
        tail = [ln for ln in combined.splitlines() if ln.strip()][-6:]
        return "FAIL", "出现 Traceback：\n     " + "\n     ".join(tail)
    # 解释器参数错误 / 找不到脚本：这类错误没有 traceback，必须显式识别，
    # 否则会把「根本没跑起来」误判成通过。
    # 注意：只在「输出开头」判断启动期错误，避免脚本业务输出里出现同名文字造成误报。
    first_lines = "\n".join([ln for ln in combined.splitlines() if ln.strip()][:3])
    fatal_markers = ("can't open file", "No such file or directory",
                     "No module named", "SyntaxError")
    if any(marker in first_lines for marker in fatal_markers):
        return "FAIL", "启动即失败：\n     " + first_lines.replace("\n", "\n     ")
    tail = [ln for ln in combined.splitlines() if ln.strip()][-4:]
    detail = "\n     ".join(tail)
    if returncode == 0:
        head = [ln for ln in combined.splitlines() if ln.strip()][:1]
        return "OK", "退出码 0，无 traceback" + (f"｜首行输出：{head[0][:70]}" if head else "")
    if returncode in business_codes:      # 已声明业务语义的非零码：降级运行，不算健康
        # 受限沙箱下的「管道不可用」常表现为某个脚本以约定退出码结束，
        # 而它的 stderr 因为块缓冲不一定落在输出尾部；这里在全量输出里再匹配一次，
        # 把「环境限制」与「业务降级」区分开（前者由 check_smoke 记为 SKIP）。
        env_hint = ""
        if any(marker in combined for marker in SANDBOX_MARKERS):
            env_hint = ("\n     [环境限制特征] 命中沙箱标志（WinError 5 / 拒绝访问 / EPERM）："
                        "本条属环境限制，不是代码问题")
        return "WARN", (
            f"退出码 {returncode}（业务降级/未走到核心路径；已声明的语义码 {business_codes}）"
            f"，不能判定为健康：\n     {detail}{env_hint}"
        )
    return "FAIL", f"退出码 {returncode}（未声明的失败码）：\n     {detail}"


def check_smoke(python_bin: str) -> None:
    print("\n【5/5】启动烟雾测试")
    for label, script, args, timeout, business_codes in SMOKE_CASES:
        start = time.perf_counter()
        status, detail = run_smoke(python_bin, script, args, timeout, business_codes)
        cost = time.perf_counter() - start
        if status == "OK":
            record("OK", f"{label}（{cost:.1f}s）", detail)
        elif label in PIPE_DEPENDENT_CASES and any(m in detail for m in SANDBOX_MARKERS):
            # 顺序很重要：环境限制要先于 WARN 判断，否则「沙箱不让开管道」会被记成
            # 「业务降级」，看起来像代码问题。
            record("SKIP", f"{label}（{cost:.1f}s）",
                   "受限沙箱禁止创建子进程管道（WinError 5），无法在此环境验证；"
                   "该用例与代码路径无关，请在普通终端重跑：\n       "
                   f"python {script}")
        elif status == "WARN":
            record("WARN", f"{label}（{cost:.1f}s）", detail)
        else:
            record("FAIL", f"{label}（{cost:.1f}s）", detail)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="my-agent 重构后健康自检")
    parser.add_argument("--quick", action="store_true", help="跳过启动烟雾测试")
    parser.add_argument("--python", default=sys.executable, help="用于烟雾测试的解释器")
    args = parser.parse_args()

    print("=" * 78)
    print("my-agent 健康自检")
    print("=" * 78)
    print(f"仓库根      : {PROJECT_ROOT}")
    print(f"当前解释器  : {sys.executable}")
    print(f"烟雾解释器  : {args.python}")
    print(f"Python 版本 : {sys.version.split()[0]}")

    check_layout()
    check_hardcoded_paths()
    check_imports()
    check_path_boundaries()
    check_paths_module()
    if args.quick:
        print("\n【5/5】启动烟雾测试：已按 --quick 跳过")
    else:
        check_smoke(args.python)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    skips = [r for r in RESULTS if r[0] == "SKIP"]
    passed = [r for r in RESULTS if r[0] == "OK"]
    print("\n" + "=" * 78)
    print(f"汇总：{len(RESULTS)} 项检查 -> 通过 {len(passed)}，"
          f"未判定为健康（WARN）{len(warns)}，跳过 {len(skips)}，失败 {len(fails)}")
    if fails:
        print("\n失败项：")
        for _level, title, detail in fails:
            print(f"  [FAIL] {title}" + (f"\n       {detail}" if detail else ""))
    if skips:
        print("\n跳过项（受运行环境限制，非代码问题）：")
        for _level, title, _detail in skips:
            print(f"  [SKIP] {title}")
    if warns:
        print("\n警告项（**未判定为健康**：缺可选依赖，或业务降级运行，例如用了 mock 数据）：")
        for _level, title, _detail in warns:
            print(f"  [WARN] {title}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
