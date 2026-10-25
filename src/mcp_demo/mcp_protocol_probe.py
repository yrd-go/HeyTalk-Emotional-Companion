"""
mcp_protocol_probe.py
MCP 旁路探针：不经 LangChain、不涉及大模型，直接用官方 mcp SDK 和
mcp_weather_server.py 对话，把 MCP 的握手与工具调用原样打印出来。

「旁路」指的是绕开 LangChain 这一层，**不是**绕开 SDK 手写 JSON-RPC 帧：
帧的编解码由官方 SDK 负责。本探针的价值在于两件事：
    ① 把 initialize / tools/list / tools/call 三步的「客户端可见事实」摊开；
    ② 提供 --break-stdout 负向模式，把「stdout 污染协议线」这个故障**稳定复现**出来，
       并给出结构化诊断（不是抛一段 traceback 让人自己猜）。

它就是 MCP 客户端最基本的三步（看懂这三步，MCP 就懂了一大半）：
    1) initialize  建连接 + 协商协议版本；拿到 server_info / capabilities / protocol_version
    2) tools/list  拿到服务端暴露的工具清单，含 input_schema —— 给模型看的"函数签名"
    3) tools/call  真正调用工具，读回 content（给模型看的文本块）
                   与 structured_content（给程序用的 JSON）、is_error（工具是否失败）

对照关系：
    LangChain 侧看到的 Tool(name/description/args) ← 就是 tools/list 的产物
    模型决定调用工具后发出的 tool_calls             ← 就是 tools/call 的请求

用法：
    pip install "mcp>=2"
    python mcp_protocol_probe.py                  # 默认查"上海"（正常路径）
    python mcp_protocol_probe.py 北京             # 指定城市
    python mcp_protocol_probe.py --break-stdout   # ★ 负向模式：故意让服务端污染 stdout，
                                                  #   复现通信中断并打印定位结论
    python mcp_protocol_probe.py --timeout 20     # 覆盖总超时（默认 45s）
不需要 ZHIPU_API_KEY。

退出码（便于脚本 / CI 断言）：
    0 = 三步全部成功，协议层正常
    5 = 负向模式未能复现（当前 SDK 已自带防护：fd 1 被指向 stderr，见 mcp/server/stdio.py）
    1 = 参数或路径错误
    2 = 缺少 mcp SDK
    3 = 环境限制（例如受限沙箱禁止创建子进程管道）
    4 = 协议层失败（这一步就是用来复现/定位它的）
    130 = 用户 Ctrl+C

注意（官方文档明确写了）：stdio 传输下客户端拉起的子进程**不会继承你的全部环境变量**，
它只拿到一份白名单（HOME/PATH/SHELL/TERM 等）。所以本探针默认不传 env=；
只有 --break-stdout 需要把 MCP_POLLUTE_STDOUT 交给子进程时，才显式传一份最小白名单。
服务端真需要 API Key 时也必须这样显式传入——这是 MCP stdio 的第二个坑
（第一个是 stdout 污染，见 mcp_weather_server.py 顶部规则 1）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 路径引导：本文件位于 <root>/src/mcp_demo/，先把仓库根与 src/ 注入 sys.path，
# 再从 src/paths.py 取「服务端脚本」的绝对路径。
#   - 不再用「与本脚本同级」推断，重构或再挪动目录都不会失效；
#   - 子进程用绝对路径启动，因此与运行时的工作目录无关（云端 Linux 同样成立）。
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[2]      # 仓库根目录
SRC_DIR = Path(__file__).resolve().parents[1]       # src/ 目录
for _path in (str(BASE_DIR), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import SERVER_SCRIPT, load_env  # noqa: E402

# 加载根目录 .env / Streamlit Secrets，供**本探针自己**使用（例如 MCP_PYTHON）。
# 注意：它不会自动传到被拉起的 MCP Server 子进程 —— stdio 子进程只拿到一份最小
# 环境白名单，服务端真需要 Key 时必须用 StdioServerParameters(env={...}) 显式传入。
load_env()

# 输出中文时避免在 GBK 终端下崩掉（这里只影响本探针自己的 stdout）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PYTHON_BIN = os.environ.get("MCP_PYTHON", sys.executable)

TOOL_NAME = "get_current_weather"

LINE = "=" * 72

DEFAULT_TIMEOUT = 45.0

# 只在 --break-stdout 时使用：显式交给子进程的最小环境白名单
_ENV_WHITELIST = ("PATH", "SystemRoot", "SystemDrive", "TEMP", "TMP", "HOME",
                  "USERPROFILE", "LANG", "LC_ALL", "PYTHONPATH")

# 「环境限制」而非「协议故障」的特征串：受限沙箱禁止创建子进程管道时会命中。
# 必须把这两类分开，否则自检会把「沙箱不让开管道」误报成「协议层坏了」。
_ENV_LIMIT_MARKERS = ("WinError 5", "PermissionError", "拒绝访问", "Access is denied",
                      "EPERM", "operation not permitted")


class ProbeStepError(RuntimeError):
    """三步中的某一步失败；携带步骤名与原始异常，便于给出定位结论。"""

    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(f"{step} 失败：{type(cause).__name__}: {cause}")
        self.step = step
        self.cause = cause


def section(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def _schema_of(tool) -> dict:
    """v2 是 input_schema，v1 是 inputSchema，两个都兼容。"""
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}


def _is_error(result):
    value = getattr(result, "is_error", None)
    if value is None:
        value = getattr(result, "isError", None)
    return value


def _structured(result):
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    return value


def print_tool(tool) -> None:
    title = getattr(tool, "title", None)
    print(f"  名称        : {getattr(tool, 'name', '?')}")
    print(f"  标题        : {title}")
    print(f"  描述        : {(getattr(tool, 'description', '') or '').strip()}")
    print("  input_schema: " + json.dumps(_schema_of(tool), ensure_ascii=False, indent=4))


def print_result(result) -> None:
    """打印 tools/call 的返回，并给出明确判定（而不是只把字段印出来）。"""
    is_error = _is_error(result)
    print(f"  is_error          : {is_error}")
    print("  content（给模型看的文本块）:")
    for block in getattr(result, "content", []) or []:
        kind = getattr(block, "type", type(block).__name__)
        print(f"      [{kind}] {getattr(block, 'text', block)}")
    structured = _structured(result)
    print("  structured_content（给程序用的 JSON）: "
          + (json.dumps(structured, ensure_ascii=False) if structured is not None else "None"))
    # 判定：is_error=False 表示协议层与业务层都正常；is_error=True 表示协议层正常、
    # 业务失败（例如参数不合法），这属于「调用成功但工具报错」，两者不能混为一谈。
    print("  判定              : " + (
        "工具执行成功（is_error 为假）"
        if not is_error else
        "工具业务失败（is_error 为真）—— 协议层通信正常，属业务层返回"
    ))


# ---------------------------------------------------------------------------
# mcp >= 2：from mcp import Client
# ---------------------------------------------------------------------------
async def probe_v2(params, city: str) -> None:
    from mcp import Client

    try:
        # Client(StdioServerParameters) 会被解析成 stdio 传输：进入 async with 时拉起子进程，
        # 退出时自动关掉（关 stdin → 等待 → 需要时 kill），不用你手动清理。
        async with Client(params) as client:
            section("[1/3] initialize 之后，客户端看到的连接事实")
            print(f"  server_info        : {client.server_info}")
            print(f"  protocol_version   : {client.protocol_version}")
            print(f"  server_capabilities: {client.server_capabilities}")

            section("[2/3] tools/list —— 服务端暴露的工具清单")
            try:
                listed = await client.list_tools()
            except Exception as exc:  # noqa: BLE001
                raise ProbeStepError("tools/list", exc) from exc
            for tool in listed.tools:
                print_tool(tool)
                print("  " + "-" * 68)

            section(f"[3/3] tools/call —— 调用 {TOOL_NAME}(city={city!r})")
            try:
                result = await client.call_tool(TOOL_NAME, {"city": city})
            except Exception as exc:  # noqa: BLE001
                raise ProbeStepError("tools/call", exc) from exc
            print_result(result)
    except ProbeStepError:
        raise
    except Exception as exc:  # noqa: BLE001  进入/退出上下文失败 = 握手层失败
        raise ProbeStepError("initialize（握手/建连）", exc) from exc


# ---------------------------------------------------------------------------
# mcp 1.x：ClientSession + stdio_client（旧写法，属性名是 camelCase）
# ---------------------------------------------------------------------------
async def probe_v1(params, city: str) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                section("[1/3] initialize 之后，客户端看到的连接事实")
                try:
                    init = await session.initialize()
                except Exception as exc:  # noqa: BLE001
                    raise ProbeStepError("initialize（握手/建连）", exc) from exc
                print(f"  serverInfo       : {init.serverInfo}")
                print(f"  protocolVersion  : {init.protocolVersion}")
                print(f"  capabilities     : {init.capabilities}")

                section("[2/3] tools/list —— 服务端暴露的工具清单")
                try:
                    listed = await session.list_tools()
                except Exception as exc:  # noqa: BLE001
                    raise ProbeStepError("tools/list", exc) from exc
                for tool in listed.tools:
                    print_tool(tool)
                    print("  " + "-" * 68)

                section(f"[3/3] tools/call —— 调用 {TOOL_NAME}(city={city!r})")
                try:
                    result = await session.call_tool(TOOL_NAME, {"city": city})
                except Exception as exc:  # noqa: BLE001
                    raise ProbeStepError("tools/call", exc) from exc
                print_result(result)
    except ProbeStepError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProbeStepError("initialize（握手/建连）", exc) from exc


async def run_probe(probe, params, city: str) -> None:
    await probe(params, city)


def _looks_like_environment_limit(exc: BaseException) -> bool:
    """判断失败是不是「环境不允许创建子进程管道」这类外部限制。

    这类失败与代码无关（受限沙箱常见），必须与真正的协议层故障区分开：
    前者退出码 3（自检记 SKIP），后者退出码 4（自检记 FAIL）。
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    cause = getattr(exc, "cause", None)
    if cause is not None:
        parts.append(f"{type(cause).__name__}: {cause}")
    text = " | ".join(parts)
    return any(marker in text for marker in _ENV_LIMIT_MARKERS)


def print_diagnosis(step: str, exc: BaseException, break_stdout: bool) -> None:
    """把失败翻译成结论 + 下一步动作，这是探针存在的意义。"""
    section("诊断结论（不是 traceback，是结论）")
    print(f"  失败步骤    : {step}")
    print(f"  原始异常    : {type(exc).__name__}: {exc}")
    if break_stdout:
        print("  判定        : 负向模式命中 —— 服务端被要求在 tools/call 里往 stdout 写文本，")
        print("                而 stdio 传输下 stdout 就是 JSON-RPC 协议线；报文被污染后，")
        print("                客户端要么解析失败，要么一直等不到合法响应（表现为卡住/超时）。")
        print("                也就是说：本次故障是**按预期复现**的，说明排障路径有效。")
        print("  修复动作    : 服务端所有日志改走 logging -> stderr，禁用 print()。")
        print(f"  对照文件    : {SERVER_SCRIPT}")
        print("  提示        : MCP_POLLUTE_STDOUT 只为复现实验存在，生产代码里不会打开。")
    else:
        print("  可能原因（按概率排序）：")
        print("    1. 服务端往 stdout 写了非协议内容（print / 日志配置错误）")
        print("       -> 直接跑一次 --break-stdout，如果故障稳定复现，就是这一类")
        print("    2. 服务端在 import 期就崩了（缺依赖 / 语法错误）")
        print("       -> 单独执行服务端脚本，看它的 stderr")
        print(f"    3. 解释器或脚本路径不对（启动命令：{PYTHON_BIN} {SERVER_SCRIPT}）")
        print("    4. SDK 版本与服务端不匹配（mcp 1.x / 2.x）-> 见上方 SDK 分支")
    print(f"  复现命令    : python {Path(__file__).name}"
          + ("" if break_stdout else " --break-stdout"))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp_protocol_probe.py",
        description="MCP 旁路探针：打印 initialize / tools/list / tools/call 三步的实际结果",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python mcp_protocol_probe.py                  # 正常路径，查"上海"\n'
            "  python mcp_protocol_probe.py 北京             # 指定城市\n"
            "  python mcp_protocol_probe.py --break-stdout   # 负向模式：复现 stdout 污染\n"
        ),
    )
    parser.add_argument("city", nargs="?", default="上海", help="要查询的城市（默认 上海）")
    parser.add_argument(
        "--break-stdout",
        action="store_true",
        help="负向模式：让服务端故意污染 stdout，复现 stdio 协议线被破坏的故障并打印定位结论",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"探针总超时秒数（默认 {DEFAULT_TIMEOUT:.0f}）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not SERVER_SCRIPT.is_file():
        print(f"[错误] 找不到服务端脚本：{SERVER_SCRIPT}", file=sys.stderr)
        return 1

    try:
        from mcp import StdioServerParameters  # v1 / v2 都能从这里导入
    except ImportError as exc:
        print(f'[错误] 未安装 mcp SDK：{exc}\n       请执行：pip install "mcp>=2"', file=sys.stderr)
        return 2

    params_kwargs: dict = {}
    if args.break_stdout:
        # 只有负向模式才需要给子进程传环境变量（见文件顶部关于白名单的说明）
        child_env = {k: os.environ[k] for k in _ENV_WHITELIST if k in os.environ}
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        child_env["MCP_POLLUTE_STDOUT"] = "1"
        params_kwargs["env"] = child_env

    params = StdioServerParameters(
        command=PYTHON_BIN,
        args=[str(SERVER_SCRIPT)],
        **params_kwargs,
    )

    try:
        from mcp import Client  # noqa: F401  mcp>=2 才有这个高层 Client

        sdk_flavor = "mcp>=2（mcp.Client）"
        probe = probe_v2
    except ImportError:
        sdk_flavor = "mcp 1.x（ClientSession + stdio_client）"
        probe = probe_v1

    print(f"启动命令   : {PYTHON_BIN} {SERVER_SCRIPT}")
    print("传输方式   : stdio（子进程的 stdin/stdout 就是协议线）")
    print(f"查询城市   : {args.city}")
    print(f"SDK 分支   : {sdk_flavor}")
    print("模式       : " + ("--break-stdout 负向模式（故意污染 stdout）"
                             if args.break_stdout else "正常模式"))
    print(f"总超时     : {args.timeout:.0f}s")

    try:
        asyncio.run(asyncio.wait_for(run_probe(probe, params, args.city), timeout=args.timeout))
    except asyncio.TimeoutError:
        print_diagnosis(
            f"超时（{args.timeout:.0f}s 内没等到合法响应）",
            TimeoutError("三步未在超时时间内完成"),
            args.break_stdout,
        )
        return 4
    except ProbeStepError as exc:
        if _looks_like_environment_limit(exc):
            print("[跳过] 环境限制，无法创建子进程管道（非协议问题）："
                  f"{type(exc.cause).__name__}: {exc.cause}", file=sys.stderr)
            return 3
        print_diagnosis(exc.step, exc.cause, args.break_stdout)
        return 4
    except OSError as exc:
        # 受限沙箱禁止创建子进程管道时走这里：属环境限制，不是代码问题
        print(f"[跳过] 环境限制，无法拉起子进程或创建管道：{exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        if _looks_like_environment_limit(exc):
            print(f"[跳过] 环境限制，无法创建子进程管道（非协议问题）：{exc}", file=sys.stderr)
            return 3
        print_diagnosis("未知阶段", exc, args.break_stdout)
        return 4

    print(f"\n{LINE}")
    if args.break_stdout:
        # 负向模式本该在协议层炸掉；能走到这里说明**污染没能破坏协议线**。
        # 不能谎报成功(0)，也不能谎报协议失败(4) —— 如实给出结论与退出码 5。
        print("========== 负向模式结论 ==========")
        print("[未复现] 本次污染（直接写 fd 1）没有破坏协议线：三步握手全部成功。")
        print("         最可能的原因：当前 MCP SDK（>= 2.2）启动时会抢占 fd 1，把协议线换成")
        print("         私有副本、并把 fd 1 指向 stderr（mcp/server/stdio.py，上游 issue #1933）")
        print("         —— 从工具内部已无法再污染协议线，这个经典故障被上游从结构上防住了。")
        print("         若环境里仍是旧版 SDK（< 2.2），同一条命令会以退出码 4 复现协议层失败。")
        print("退出码约定: 5 = 负向模式未能复现（环境已自带防护，不代表代码有问题）")
        print(LINE)
        return 5
    print("完成：initialize / tools/list / tools/call 三步全部成功，协议层正常。")
    print("覆盖边界：本探针断言的是客户端三步主链路；官方文档里的 initialized 通知、")
    print("          ping、progress、cancellation 等控制帧没有逐一断言。")
    print(LINE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户取消。", file=sys.stderr)
        raise SystemExit(130)
