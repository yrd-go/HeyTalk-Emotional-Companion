"""
mcp_weather_client.py
用 LangChain 连接 MCP Server，并让模型自动调用 MCP 工具。

链路：
    LangChain Agent（智谱 glm-4-flash）
      -> MCP 适配层：把 MCP 的 tools/list 结果转成 LangChain Tool
      -> stdio 子进程：python mcp_weather_server.py
      -> MCP tools/call: get_current_weather(city="上海")
      -> 工具结果回灌模型 -> 最终中文回答

和 agent_tool_demo.py 的区别只有一个：那里的工具是本进程里的 @tool 函数，
这里的工具**在另一个进程里**，靠 MCP 协议跨进程调用 —— 这正是 MCP 的价值：
同一个 Server 可以同时被 LangChain、Claude Desktop、IDE 等任何 MCP Host 复用。

配置方式（优先级从高到低，脚本启动时自动加载同目录 .env，与 rag_demo.py 统一）：
    1) 系统环境变量      $env:ZHIPU_API_KEY = "sk-..."     临时生效，仅当前终端
    2) 同目录 .env 文件  ZHIPU_API_KEY=sk-...              推荐
    3) 代码内默认值      仅非敏感项（base_url、模型名）

环境变量（Key 只从环境读取，绝不写死在代码里）：
    ZHIPU_API_KEY        必填，智谱开放平台的 API Key（--direct 模式下不需要）
    ZHIPU_BASE_URL       选填，默认 https://open.bigmodel.cn/api/paas/v4
    ZHIPU_CHAT_MODEL     选填，默认 glm-4-flash（与 rag_demo.py 同名）
    ZHIPU_MODEL          选填，ZHIPU_CHAT_MODEL 的兼容别名；两者都设时前者优先
    MCP_PYTHON           选填，默认当前解释器 sys.executable（用于拉起 MCP Server 子进程）

依赖：
    pip install "langchain[mcp]>=1.4.0" langchain-openai python-dotenv
    pip install "mcp>=2"     # MCP Server 用（langchain[mcp] 也会带上 FastMCP）

用法：
    python mcp_weather_client.py                     # 默认问题：查上海天气
    python mcp_weather_client.py "北京今天天气怎么样？"
    python mcp_weather_client.py --direct            # 跳过模型，直接 tools/call（最裸对照，不需要 Key）

关于适配层：MCP 能力已从 langchain-mcp-adapters 迁入 langchain.mcp（beta，导入会有
LangChainBetaWarning）。本脚本优先用 langchain.mcp.MCPAdapter，装不上时自动回退到
旧的 langchain_mcp_adapters.MultiServerMCPClient。
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 路径引导与 .env / Secrets 加载（与 rag_demo.py / agent_tool_demo.py 保持一致）
#   - 重构后本文件位于 <root>/src/mcp_demo/，且可由任何工作目录启动，
#     所以先动态把仓库根与 src/ 注入 sys.path，再 import paths；
#   - paths.load_env() 加载「仓库根」的 .env（不再假设与本脚本同级），
#     云端没有 .env 时自动回退到 Streamlit Secrets，同样只写入 os.environ；
#   - 必须早于下方所有 os.environ / os.getenv()，否则读不到配置；
#   - override=False：真实系统环境变量优先于 .env。
# --------------------------------------------------------------------------
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]      # 仓库根目录
SRC_DIR = Path(__file__).resolve().parents[1]       # src/ 目录
for _path in (str(BASE_DIR), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import ENV_FILE, SERVER_SCRIPT, load_env  # noqa: E402

_ENV_INFO = load_env()                # 只关心来源与键数量，绝不读取或打印键值
ENV_FILE_FOUND = bool(_ENV_INFO["env_file"] or _ENV_INFO["secrets_used"])

import asyncio
import json

from langchain_openai import ChatOpenAI

# 控制台按 UTF-8 输出，避免 Windows 下中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# SERVER_SCRIPT 来自 src/paths.py：<root>/src/mcp_demo/mcp_weather_server.py
# 用 sys.executable 而不是字符串 "python"：保证子进程就是"装了 mcp 的那个解释器"
PYTHON_BIN = os.environ.get("MCP_PYTHON", sys.executable)

ZHIPU_BASE_URL = os.environ.get(
    "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
).rstrip("/")
ZHIPU_CHAT_MODEL = (
    os.environ.get("ZHIPU_CHAT_MODEL")
    or os.environ.get("ZHIPU_MODEL")
    or "glm-4-flash"
)

SERVER_NAME = "weather"
TOOL_NAME = "get_current_weather"
DEFAULT_QUESTION = "帮我查一下上海现在的天气？"
DEFAULT_CITY = "上海"

SYSTEM_PROMPT = (
    "你是一个天气助手。用户询问天气时，必须调用 get_current_weather 工具去取真实数据，"
    "不要凭猜测回答。拿到工具返回的结果后，用简洁的中文回答用户。"
)


# ---------------------------------------------------------------------------
# 1. MCP Server 的连接配置
# ---------------------------------------------------------------------------
def mcp_config() -> dict:
    """新版 langchain.mcp 用的标准 MCPConfig 形状（mcpServers）。

    注意：条目里**不写** transport —— MCPAdapter 会根据 target 推断，
    有 command/args 就是 stdio 子进程，有 url 就是 Streamable HTTP。
    """
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": PYTHON_BIN,
                "args": [str(SERVER_SCRIPT)],
            }
        }
    }


def legacy_connections() -> dict:
    """旧版 langchain-mcp-adapters 用的形状：连接里必须显式写 transport。"""
    return {
        SERVER_NAME: {
            "command": PYTHON_BIN,
            "args": [str(SERVER_SCRIPT)],
            "transport": "stdio",
        }
    }


# ---------------------------------------------------------------------------
# 2. 模型与 Agent（与 agent_tool_demo.py 相同的取用方式）
# ---------------------------------------------------------------------------
def build_model() -> ChatOpenAI:
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise SystemExit(
            "缺少环境变量 ZHIPU_API_KEY。\n"
            "PowerShell:  $env:ZHIPU_API_KEY=\"你的Key\"\n"
            "bash:        export ZHIPU_API_KEY=你的Key\n"
            "只想验证 MCP 链路可以加 --direct（不调用大模型）。"
        )
    return ChatOpenAI(
        model=ZHIPU_CHAT_MODEL,
        api_key=api_key,
        base_url=ZHIPU_BASE_URL,
        temperature=0.0,
    )


def build_agent(model, tools):
    """返回 (agent, 实现名称)。v1 优先，v0.x 回退。"""
    try:
        from langchain.agents import create_agent  # LangChain v1

        return (
            create_agent(model, tools, system_prompt=SYSTEM_PROMPT),
            "langchain.agents.create_agent",
        )
    except ImportError:
        try:
            from langgraph.prebuilt import create_react_agent  # v0.x

            return (
                create_react_agent(model, tools, prompt=SYSTEM_PROMPT),
                "langgraph.prebuilt.create_react_agent",
            )
        except ImportError as e:
            raise SystemExit(
                "未找到 Agent 构造函数，请先安装：\n"
                "    pip install -U langchain langgraph\n"
                f"（原始错误：{e}）"
            ) from e


# ---------------------------------------------------------------------------
# 3. 日志：tools/list 清单 + Human -> AI(tool_calls) -> Tool -> AI 流转
# ---------------------------------------------------------------------------
def _text(content) -> str:
    """把消息 content 统一成字符串（兼容 content blocks 列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or str(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    return "" if content is None else str(content)


def print_tools(tools) -> None:
    """打印 MCP tools/list 转成的 LangChain Tool —— 这一份就是模型看到的工具契约。"""
    print(f"\n[MCP] tools/list 返回 {len(tools)} 个工具：")
    for tool in tools:
        print(f"  - 名称: {tool.name}")
        description = (getattr(tool, "description", "") or "").strip().replace("\n", " ")
        if description:
            print(f"    描述: {description}")
        args = getattr(tool, "args", None)
        if args:
            print(f"    参数: {json.dumps(args, ensure_ascii=False)}")
        metadata = getattr(tool, "metadata", None) or {}
        if "mcp" in metadata:
            # MCP 来源信息（服务端身份、注解等）会被挂在 metadata["mcp"] 下
            print(f"    MCP 元数据: {json.dumps(metadata['mcp'], ensure_ascii=False, default=str)}")
    print("    提示: 工具名可能带 server 前缀（如 weather_get_current_weather），"
          "以适应层实际返回为准。")


def print_flow(messages) -> bool:
    """逐条打印消息流转。返回是否真的发生了工具调用。"""
    tool_called = False
    print("=" * 72)
    print(f"流转日志（共 {len(messages)} 条消息）")
    print("=" * 72)

    for step, msg in enumerate(messages, 1):
        kind = type(msg).__name__

        if kind == "HumanMessage":
            print(f"\n[步骤 {step}] 用户输入 (HumanMessage)")
            print(f"    {_text(msg.content)}")

        elif kind == "AIMessage":
            tool_calls = getattr(msg, "tool_calls", None) or []
            content = _text(msg.content).strip()
            if tool_calls:
                tool_called = True
                print(f"\n[步骤 {step}] 模型决策 (AIMessage)：决定调用工具")
                if content:
                    print(f"    思考文本：{content}")
                for call in tool_calls:
                    print(f"    -> 工具名：{call.get('name')}")
                    print(f"       参数  ：{json.dumps(call.get('args'), ensure_ascii=False)}")
                    print(f"       调用ID：{call.get('id')}")
            else:
                print(f"\n[步骤 {step}] 模型最终回答 (AIMessage)")
                print(f"    {content or '(空)'}")

        elif kind == "ToolMessage":
            tool_called = True
            print(f"\n[步骤 {step}] 工具执行结果 (ToolMessage，来自 MCP Server)")
            print(f"    工具名：{getattr(msg, 'name', None)}")
            print(f"    调用ID：{getattr(msg, 'tool_call_id', None)}")
            print(f"    返回  ：{_text(msg.content)}")

        else:
            print(f"\n[步骤 {step}] 其他消息 ({kind})")
            print(f"    {_text(getattr(msg, 'content', ''))}")

    print("\n" + "=" * 72)
    return tool_called


def _final_answer(messages) -> str:
    for msg in reversed(messages):
        if type(msg).__name__ == "AIMessage" and not (getattr(msg, "tool_calls", None) or []):
            return _text(msg.content).strip()
    return ""


def pick_tool(tools, wanted: str):
    """按名字找工具，并容忍适配层自动加上的 server 前缀。"""
    for tool in tools:
        if tool.name == wanted:
            return tool
    for tool in tools:
        if tool.name.endswith(wanted):
            return tool
    raise SystemExit(
        f"未找到工具 {wanted}，MCP 实际返回的是：{[t.name for t in tools]}"
    )


# ---------------------------------------------------------------------------
# 4. 两条执行路径
# ---------------------------------------------------------------------------
async def run_direct(tools, city: str) -> int:
    """跳过模型，直接触发一次 MCP tools/call —— 不需要 ZHIPU_API_KEY。"""
    tool = pick_tool(tools, TOOL_NAME)
    print(f"\n[直接调用] {tool.name}(city={city!r}) —— 这一步等价于 MCP 的 tools/call")
    result = await tool.ainvoke({"city": city})
    print(f"[直接调用] 返回：{_text(result)}")
    return 0


async def run_agent(model, tools, question: str, adapter_label: str) -> int:
    print_tools(tools)
    agent, agent_impl = build_agent(model, tools)
    print(f"\n[LangChain] MCP 适配层：{adapter_label}")
    print(f"[LangChain] Agent 实现：{agent_impl}")

    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    messages = result["messages"]

    tool_called = print_flow(messages)
    answer = _final_answer(messages)

    print(f"是否发生工具调用：{'是' if tool_called else '否'}")
    print(f"最终回答：{answer or '(空)'}")
    return 0 if tool_called else 1


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def describe_env_source() -> str:
    """只报配置来源与键数量，绝不回显任何键值。"""
    if _ENV_INFO["source"] == "系统环境变量":
        return f"配置来源: 系统环境变量（未发现 {ENV_FILE}，也无 Secrets）"
    return (
        f"配置来源: {_ENV_INFO['source']}"
        f"（已识别 {_ENV_INFO['keys']} 个环境变量，键值不打印）"
    )


async def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    question = argv[0] if argv else DEFAULT_QUESTION
    direct = "--direct" in sys.argv
    city = DEFAULT_CITY

    _api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    _key_state = (
        f"ZHIPU_API_KEY: 已配置（长度 {len(_api_key)}）" if _api_key else "ZHIPU_API_KEY: 未配置"
    )
    print(f"[配置] {describe_env_source()} | {_key_state}")
    print(f"服务端脚本  : {SERVER_SCRIPT}")
    print(f"子进程解释器: {PYTHON_BIN}")
    print(f"模型        : {'（--direct 模式，不调用模型）' if direct else ZHIPU_CHAT_MODEL}")

    if not SERVER_SCRIPT.is_file():
        raise SystemExit(f"找不到 MCP Server 脚本：{SERVER_SCRIPT}")

    model = None if direct else build_model()

    # ---- 主路径：langchain.mcp.MCPAdapter（beta）----
    try:
        from langchain.mcp import MCPAdapter  # 需要 langchain[mcp]>=1.4.0
    except ImportError:
        MCPAdapter = None

    if MCPAdapter is not None:
        # MCPAdapter 是异步上下文管理器：进入时连 Server，退出时断开。
        # 保持整个 Agent 运行都在 with 内部，会话生命周期最清晰。
        async with MCPAdapter(mcp_config()) as adapter:
            tools = await adapter.list_tools()
            if direct:
                return await run_direct(tools, city)
            return await run_agent(model, tools, question, "langchain.mcp.MCPAdapter")

    # ---- 回退路径：旧包 langchain-mcp-adapters（已停止积极维护）----
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as e:
        raise SystemExit(
            "没有可用的 LangChain MCP 适配层，请安装其中一个：\n"
            '    pip install "langchain[mcp]>=1.4.0"     # 新版（推荐）\n'
            "    pip install langchain-mcp-adapters      # 旧版（回退）\n"
            f"（原始错误：{e}）"
        ) from e

    client = MultiServerMCPClient(legacy_connections())
    tools = await client.get_tools()
    if direct:
        return await run_direct(tools, city)
    return await run_agent(
        model, tools, question, "langchain_mcp_adapters.MultiServerMCPClient"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[中断] 用户取消。", file=sys.stderr)
        raise SystemExit(130)
