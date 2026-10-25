"""
agent_tool_demo.py
LangChain Function Calling（工具调用 / ReAct Agent）入门示例。

流程：
    用户提问 -> 模型决定调用工具 -> 工具查 MongoDB -> 结果回灌模型 -> 模型给出最终回答

数据来源直接复用 query_student.py 里的连接配置与查询语义，
避免把 MONGO_URI / 库名 / 集合名在多个文件里重复硬编码。

配置方式（优先级从高到低，脚本启动时自动加载同目录 .env，与 rag_demo.py 统一）：
    1) 系统环境变量      $env:ZHIPU_API_KEY = "sk-..."     临时生效，仅当前终端
    2) 同目录 .env 文件  ZHIPU_API_KEY=sk-...              推荐；与 agent_tool_demo.py 同级
    3) 代码内默认值      仅非敏感项（base_url、模型名）
    注意：.env 须保存为 UTF-8 无 BOM，键值两侧不加引号、不留空格。

环境变量（Key 只从环境读取，绝不写死在代码里）：
    ZHIPU_API_KEY        必填，智谱开放平台的 API Key
    ZHIPU_BASE_URL       选填，默认 https://open.bigmodel.cn/api/paas/v4
    ZHIPU_CHAT_MODEL     选填，默认 glm-4-flash（与 rag_demo.py 同名）
    ZHIPU_MODEL          选填，ZHIPU_CHAT_MODEL 的兼容别名；两者都设时前者优先
    MONGO_URI            选填，默认 mongodb://localhost:27017/
    MONGO_DB             选填，默认 agent_db
    MONGO_COLLECTION     选填，默认 students

依赖：
    pip install langchain langchain-openai langgraph pymongo

用法：
    python agent_tool_demo.py
    python agent_tool_demo.py "帮我查一下学号是 1 的学生叫什么名字？"
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 路径引导与 .env / Secrets 加载（与 rag_demo.py 保持一致）
#   - 本文件位于 <root>/src/core/，先动态把仓库根与 src/ 注入 sys.path，
#     这样既能在任何工作目录下运行，也能 import 同目录的 query_student；
#   - paths.load_env() 加载「仓库根」的 .env（不再假设与本脚本同级），
#     云端没有 .env 时回退到 Streamlit Secrets，同样只写入 os.environ；
#   - 必须早于下方所有 os.environ / os.getenv()，否则读不到配置；
#   - override=False：真实系统环境变量优先于 .env，
#     因此 $env:ZHIPU_API_KEY="..." 的老用法依然生效且优先级更高。
# --------------------------------------------------------------------------
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 仓库根目录
SRC_DIR = Path(__file__).resolve().parents[1]        # src/ 目录
for _path in (str(PROJECT_ROOT), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import ENV_FILE, load_env  # noqa: E402

_ENV_INFO = load_env()                # 只关心来源与键数量，绝不读取或打印键值
ENV_FILE_FOUND = bool(_ENV_INFO["env_file"] or _ENV_INFO["secrets_used"])

import json
import re
import sys

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

# 复用 query_student.py 的连接配置（MONGO_URI / DB_NAME / COLLECTION_NAME）
# 两种导入方式都要兼容：
#   1) 直接运行本文件（python src/core/agent_tool_demo.py）时，Python 会把脚本所在
#      目录 src/core/ 放进 sys.path，因此平铺名 query_student 可用；
#   2) 被当作模块导入（Streamlit 页面 / scripts/import_check.py）时，只有 src/ 在
#      sys.path 上，必须走包路径 core.query_student。
try:
    import core.query_student as student_query
except ImportError:  # pragma: no cover - 仅在直接运行脚本时走到
    import query_student as student_query

# ---------------------------------------------------------------------------
# 0. 控制台按 UTF-8 输出，避免 Windows 下中文乱码
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 智谱的 OpenAI 兼容端点
ZHIPU_BASE_URL = os.environ.get(
    "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
).rstrip("/")
# 模型名与 rag_demo.py 对齐：ZHIPU_CHAT_MODEL 优先，ZHIPU_MODEL 作为兼容别名
ZHIPU_CHAT_MODEL = (
    os.environ.get("ZHIPU_CHAT_MODEL")
    or os.environ.get("ZHIPU_MODEL")
    or "glm-4-flash"
)

DEFAULT_QUESTION = "帮我查一下学号是 2 的学生叫什么名字？"

SYSTEM_PROMPT = (
    "你是一个学生信息助手。"
    "当用户询问某个学号对应的学生信息时，必须调用 query_student_info 工具去查真实数据，"
    "不要凭猜测回答。拿到工具返回的结果后，用简洁的中文回答用户。"
    "特别注意：如果工具返回的 JSON 里 status 是 mock，说明数据库不可用、这是演示用的模拟数据，"
    "请明确告诉用户「这是模拟数据」并说明需要配置 MONGO_URI 才能查到真实数据。"
)


# ---------------------------------------------------------------------------
# 1. 把本地数据库查询包装成一个工具
# ---------------------------------------------------------------------------
def _to_json(payload: dict) -> str:
    """统一用 JSON 字符串作为工具返回值，便于模型解析。"""
    return json.dumps(payload, ensure_ascii=False)


# 数据库不可用时的模拟数据表（与 query_student.py 的 MOCK_STUDENTS 保持一致）
_MOCK_STUDENTS = {1: "张三", 2: "李四", 3: "王五"}
_MOCK_NOTE = (
    "MongoDB 不可用，已返回模拟数据（status=mock）。"
    "请如实告知用户这是演示数据；真实数据请配置 MONGO_URI。"
    "如需严格模式（连不上直接失败）：设置 STUDENT_ALLOW_MOCK=false。"
)


def _compact_reason(reason: str) -> str:
    """把 PyMongo 冗长的拓扑报错压成一小段，避免淹没模型的观察结果。

    示例：'MongoDB 操作出错：localhost:27017: [WinError 10061] 由于目标计算机积极拒绝，
    无法连接。 (configured timeouts: ...)' -> 'MongoDB 操作出错：localhost:27017:
    [WinError 10061] 由于目标计算机积极拒绝，无法连接。'
    """
    text = " ".join(reason.split())                       # 合并换行与多余空格
    text = re.split(r"\s*\(configured timeouts", text)[0]  # 砍掉驱动内部细节
    text = re.split(r", Timeout: \d", text)[0]
    return text if len(text) <= 160 else text[:160] + " ..."


def _fallback_result(student_id: int, reason: str) -> str:
    """数据库不可用时的统一兜底返回。

    默认（STUDENT_ALLOW_MOCK 非 false）返回 status=mock 的模拟数据，
    使模型仍能完成 ReAct 闭环并输出最终回答；严格模式下返回 status=error，
    由模型据此告知用户失败原因。
    """
    reason = _compact_reason(reason)
    if student_query.STUDENT_ALLOW_MOCK:
        return _to_json({
            "status": "mock",
            "student_id": student_id,
            "name": _MOCK_STUDENTS.get(student_id, f"模拟学生{student_id}"),
            "source": "mock",
            "error": reason,
            "note": _MOCK_NOTE,
        })
    return _to_json({
        "status": "error",
        "student_id": student_id,
        "name": None,
        "source": "mongodb",
        "error": reason,
        "note": "严格模式（STUDENT_ALLOW_MOCK=false）：未使用模拟数据。",
    })


@tool("query_student_info")
def query_student_info(student_id: int) -> str:
    """根据学号查询学生的姓名。

    参数：
        student_id: 学生学号（整数），例如 2。

    返回：
        一个 JSON 字符串，字段含义如下：
            status : ok（真实数据）/ not_found（确实没有该学生）/ mock（数据库不可用，返回模拟数据）
            name   : 学生姓名（mock 时为模拟姓名，务必按 status 区分）
            error  : 仅在失败或降级时出现的原因说明

    本地容错说明：
        本机与 Streamlit Cloud 通常都没有 localhost:27017 的 MongoDB。
        此时不再抛出异常中断 Agent，而是返回 status=mock 的模拟数据，
        让模型能继续完成 ReAct 闭环并给出最终回答。
        如需「连不上就报错」的严格模式，设置环境变量 STUDENT_ALLOW_MOCK=false。
    """
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        return _to_json({
            "status": "error",
            "error": "未安装 pymongo，请先执行：pip install pymongo",
        })

    client = None
    try:
        # serverSelectionTimeoutMS 让连不上时快速失败，而不是长时间挂起
        client = MongoClient(student_query.MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")

        collection = client[student_query.DB_NAME][student_query.COLLECTION_NAME]
        # 用字典条件查询，由驱动负责转义，避免注入
        doc = collection.find_one({"id": student_id}, {"_id": 0, "name": 1})

        if doc is None:
            return _to_json(
                {
                    "status": "not_found",
                    "student_id": student_id,
                    "name": None,
                    "message": f"未找到学号为 {student_id} 的学生。",
                }
            )
        return _to_json(
            {"status": "ok", "student_id": student_id, "name": doc.get("name")}
        )

    except PyMongoError as e:
        # 把「数据库不可用」当成可继续的观察结果：允许时返回模拟数据，让 ReAct 闭环走完
        return _fallback_result(student_id, f"MongoDB 操作出错：{e}")
    except Exception as e:  # noqa: BLE001 - 兜底，保证工具永远返回可读信息
        return _fallback_result(student_id, f"查询学生信息时发生未知错误：{e}")
    finally:
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------
# 2. 初始化 glm-4-flash（走 langchain_openai，指到智谱兼容端点）
# ---------------------------------------------------------------------------
def build_model() -> ChatOpenAI:
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise SystemExit(
            "缺少环境变量 ZHIPU_API_KEY。\n"
            "PowerShell:  $env:ZHIPU_API_KEY=\"你的Key\"\n"
            "bash:        export ZHIPU_API_KEY=你的Key"
        )
    # api_key 只从环境变量读取，绝不写死在代码里，也不打印
    return ChatOpenAI(
        model=ZHIPU_CHAT_MODEL,
        api_key=api_key,
        base_url=ZHIPU_BASE_URL,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# 3. 创建 ReAct Agent 并绑定工具（兼容 LangChain v1 与 v0.x）
# ---------------------------------------------------------------------------
def build_agent(model, tools):
    """返回 (agent, 实现名称)。

    LangChain v1：langchain.agents.create_agent（推荐，system_prompt=...）
    LangChain v0.x：langgraph.prebuilt.create_react_agent（旧写法，prompt=...）
    """
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
# 4. 打印完整流转日志：Human -> AI(思考/工具调用) -> Tool(观察) -> AI(最终回答)
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
            print(f"\n[步骤 {step}] 工具执行结果 (ToolMessage)")
            print(f"    工具名：{getattr(msg, 'name', None)}")
            print(f"    调用ID：{getattr(msg, 'tool_call_id', None)}")
            print(f"    返回  ：{_text(msg.content)}")

        else:
            print(f"\n[步骤 {step}] 其他消息 ({kind})")
            print(f"    {_text(getattr(msg, 'content', ''))}")

    print("\n" + "=" * 72)
    return tool_called


def _final_answer(messages) -> str:
    """取最后一条不带 tool_calls 的 AI 消息作为最终回答。"""
    for msg in reversed(messages):
        if type(msg).__name__ == "AIMessage" and not (getattr(msg, "tool_calls", None) or []):
            return _text(msg.content).strip()
    return ""


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION

    # 启动时打印一行配置来源，避免"配了却没生效"的困惑（只报来源与键数量，不含键值）
    if _ENV_INFO["source"] == "系统环境变量":
        _env_src = f"配置来源: 系统环境变量（未发现 {ENV_FILE}，也无 Secrets）"
    else:
        _env_src = (
            f"配置来源: {_ENV_INFO['source']}"
            f"（已识别 {_ENV_INFO['keys']} 个环境变量，键值不打印）"
        )

    _api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    _key_state = (
        f"ZHIPU_API_KEY: 已配置（长度 {len(_api_key)}）"
        if _api_key
        else "ZHIPU_API_KEY: 未配置"
    )
    print(f"[配置] {_env_src} | {_key_state}")

    print(f"模型        : {ZHIPU_CHAT_MODEL}")
    print(f"base_url    : {ZHIPU_BASE_URL}")
    print(f"MongoDB     : {student_query.DB_NAME}.{student_query.COLLECTION_NAME}")
    print(f"工具        : {query_student_info.name}")
    print(f"工具参数    : {json.dumps(query_student_info.args, ensure_ascii=False)}")
    print(f"用户提问    : {question}")

    model = build_model()
    agent, impl = build_agent(model, [query_student_info])
    print(f"Agent 实现  : {impl}")

    # 附：跳过 Agent，只看模型自己会不会调用工具（Function Calling 的最小形态）
    direct = model.bind_tools([query_student_info]).invoke(question)
    print("\n[附] 只 bind_tools、不挂 Agent 时，模型原始返回：")
    print(f"    content    : {_text(direct.content).strip() or '(空)'}")
    print(f"    tool_calls : {json.dumps(direct.tool_calls, ensure_ascii=False) or '(空)'}")

    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = result["messages"]

    tool_called = print_flow(messages)
    answer = _final_answer(messages)

    print(f"是否发生工具调用：{'是' if tool_called else '否'}")
    print(f"最终回答：{answer or '(空)'}")
    return 0 if tool_called else 1


if __name__ == "__main__":
    raise SystemExit(main())
