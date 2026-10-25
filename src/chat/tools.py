# -*- coding: utf-8 -*-
"""
src/chat/tools.py —— Agent 的工具层（S2）
================================================================

设计立场：把原有模块从"给人看的演示"变成"给模型用的手"
--------------------------------------------------------------------------
    天气   → 复用 src/mcp_demo/mcp_weather_server.py 的 get_current_weather
             （直接 import 并调用，不重写、不复制）
    翻译   → 复用 src/core/router.py 用过的 MyMemory 免费接口与语言映射
             （抽成可复用的 translate_text，路由脚本仍走它自己的 CLI 路径）

两条硬约束
--------------------------------------------------------------------------
1. **高危路径永不经过工具调用**（见 conversation.plan_reply）：
   工具调用会把用户原话塞进模型 Prompt 与第三方接口，而自伤表述
   绝不能被二次送进模型或外部 API。所以工具只在非高危路径上可用。
2. **工具永不抛异常**：查不到、连不上、参数缺失都返回可读的中文文本，
   让模型据此决定怎么回答（这是 agent_tool_demo.py 已经确立的纪律）。

关于 langchain 依赖：本模块的**纯逻辑**（normalize_city / lookup_weather_text /
translate_text / resolve_language_code）只用标准库；
只有 `AGENT_TOOLS` 需要 langchain。缺少 langchain 时纯逻辑依然可用、可测。
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from chat import languages

__all__ = [
    "WEATHER_TOOL_NAME",
    "TRANSLATE_TOOL_NAME",
    "AGENT_TOOL_NAMES",
    "AGENT_TOOLS",
    "langchain_available",
    "lookup_weather_text",
    "translate_text",
    "translate_outcome",
    "tool_outcome",
    "resolve_language_code",
    "normalize_city",
    "detect_source_lang",
    "LANG_NAME_TO_CODE",
    "TRANSLATE_TIMEOUT",
    "tool_by_name",
]

WEATHER_TOOL_NAME = "get_current_weather"
TRANSLATE_TOOL_NAME = "translate_text"

TRANSLATE_URL = "https://api.mymemory.translated.net/get"
TRANSLATE_TIMEOUT = 15          # 秒：与 router.py 保持一致，避免请求挂死

# 语言名 -> 语言代码。
# 说明：S2 升级后真正的语言注册表在 src/chat/languages.py（收录约 90 种语言，
# 支持代码/中文名/英文名/当地写法四种写法）。这里保留这个映射是**兼容用途**：
# 老代码与老测试 import 它，不该因为这个升级而失效。
# 内容由语言注册表生成，不再手工维护（避免两处漂移）。
def _build_lang_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for code, (cn, en, alias) in languages.LANGUAGES.items():
        for key in (code, cn, en):
            mapping[str(key).strip()] = code
        for piece in str(alias).split("/"):
            piece = piece.strip()
            if piece:
                mapping[piece] = code
    for alias, code in languages.COMMON_ALIASES.items():
        mapping[alias] = code
    return mapping


LANG_NAME_TO_CODE: dict[str, str] = _build_lang_map()

_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")


# ===========================================================================
# 1. langchain 可用性与工具包装（优雅降级）
# ===========================================================================
def langchain_available() -> bool:
    """检查 langchain-core 是否可用（只探测，不导入副作用）。"""
    try:
        import langchain_core.tools  # noqa: F401
    except Exception:
        return False
    return True


def _as_tool(name: str, description: str, func: Callable[..., str]) -> Any:
    """把纯函数包装成可绑定到模型的工具。

    有 langchain 就用官方的 @tool（模型能看到规范 schema）；
    没有就退化成一个带 `name` / `description` / `invoke` 的轻量对象，
    让**纯逻辑照样可用可测**——测试环境不该被迫装一整条模型链。
    """
    try:
        from langchain_core.tools import tool

        return tool(name, description=description)(func)
    except Exception:
        class _FallbackTool:
            def __init__(self) -> None:
                self.name = name
                self.description = description
                self.func = func

            def __call__(self, *args: Any, **kwargs: Any) -> str:
                return func(*args, **kwargs)

            def invoke(self, payload: Any, *args: Any, **kwargs: Any) -> str:
                if isinstance(payload, dict):
                    return func(**payload)
                return func(payload)

            def __repr__(self) -> str:  # pragma: no cover - 调试用
                return f"<FallbackTool {name}>"

        return _FallbackTool()


# ===========================================================================
# 2. 天气：直接复用 MCP 服务端里的纯函数
# ===========================================================================
def normalize_city(city: Any) -> str:
    """城市名清洗（含别名归一交给上游实现，这里只做去空白与类型兜底）。"""
    if city is None:
        return ""
    return str(city).strip()


def lookup_weather_text(city: Any, *, offline_only: bool = False) -> str:
    """查天气，返回一句中文描述。任何失败都返回可读文本，不抛异常。

    S2 升级：改成走 **Open-Meteo 全球真实数据**（任意国家/城市，不需要 Key）。
    拿不到时按降级链处理：
        1. Open-Meteo 真实数据
        2. 本地 MCP 模拟服务端（**明确标注**是模拟数据）
        3. 一句可读说明

    offline_only=True 跳过联网直接用模拟数据 —— 给测试与离线演示用，
    让"离线路径"能在无网环境下被确定性验证。
    """
    name = normalize_city(city)
    if not name:
        return "未提供城市名：请告诉我要查哪个城市，例如「上海」。"

    try:
        from chat.weather import lookup_global_weather
    except Exception as exc:  # noqa: BLE001 - 天气模块不可用也不该炸链路
        return (f"暂时查不了天气（天气模块不可用：{type(exc).__name__}）。"
                "你可以稍后再试，或者自己看一下天气预报。")

    try:
        result = lookup_global_weather(name, offline_only=offline_only)
        return str(result.text)
    except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
        return f"查询「{name}」的天气时出错：{type(exc).__name__}。"


# ===========================================================================
# 3. 翻译：复用 MyMemory 免费接口
# ===========================================================================
def resolve_language_code(name: Any) -> str | None:
    """把"日语 / Japanese / 日本語 / ja"统一成语言代码；不认识返回 None（不猜）。"""
    return languages.resolve(name)


def detect_source_lang(text: str) -> str:
    """判断源语言。

    升级说明：原来是"含中文字符 -> zh，否则 -> en"，会把日语、韩语、
    阿拉伯语、泰语**统统当成英语**。现在按书写系统判断，准确得多。
    """
    return languages.guess_source_language(text)


_UNSUPPORTED_LANGUAGE_NOTE = (
    "【翻译工具没有这门语言的可靠数据】\n"
    "目标语言：{target}\n"
    "请直接回答，不要只说“查不到”。要求：\n"
    "1. 若有把握，给出 1–3 个候选译法；\n"
    "2. 明确标注“参考译法，未经母语者核对”；\n"
    "3. 补充可核对的客观信息（书写系统、主要使用地区）；\n"
    "4. 不得编造词典、论文或出处；\n"
    "5. 最后问一个澄清问题（这个词用在什么场合）。"
)

_ENGINE_ERROR_NOTE = (
    "【翻译工具这次没查成】\n"
    "原因：{detail}\n"
    "不要假装查过。可以给参考译法，但必须标注“参考译法，未能查证”，"
    "说明这次没查成；如果用户需要确定结果，建议稍后重试。"
)


@dataclass(frozen=True)
class ToolOutcome:
    """工具的结构化结果：给模型看的文本 + 给上层路由/UI 看的状态。"""

    text: str
    status: str = ""


def translate_outcome(text: Any, target_lang: Any = None) -> ToolOutcome:
    """翻译并保留状态（ok / empty / too-long / unsupported-language / engine-error）。"""
    try:
        from chat.translation import translate as _translate
    except Exception as exc:  # noqa: BLE001 - 引擎模块不可用也不该炸链路
        return ToolOutcome(
            f"翻译功能暂时不可用（{type(exc).__name__}）。",
            "engine-error",
        )

    try:
        result = _translate(text, target_lang)
        status = str(getattr(result, "status", "") or "")
        if status == "unsupported-language":
            return ToolOutcome(
                _UNSUPPORTED_LANGUAGE_NOTE.format(target=str(target_lang or "该语言")),
                status,
            )
        if status == "engine-error":
            return ToolOutcome(
                _ENGINE_ERROR_NOTE.format(
                    detail=str(getattr(result, "text", "") or "翻译服务不可用")),
                status,
            )
        return ToolOutcome(str(result.text), status)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证工具永远有输出
        return ToolOutcome(f"翻译时出错：{type(exc).__name__}。", "engine-error")


def translate_text(text: Any, target_lang: Any = None) -> str:
    """把 text 翻译成 target_lang；未指定时自适应（中→英 / 英→中）。

    返回**可读中文文本**（成功是译文，失败是原因说明），永不抛异常——
    与 agent_tool_demo.py 的"工具永不抛异常"纪律一致。

    升级说明：真正干活的是 src/chat/translation.py 的**多引擎链**
    （Google 优先、MyMemory 兜底），覆盖从十几个语种扩到约 90 种语言。
    """
    return translate_outcome(text, target_lang).text


def tool_outcome(name: str, args: Any) -> ToolOutcome | None:
    """返回带状态的结果；只有需要状态透出的工具才返回它。"""
    if name == TRANSLATE_TOOL_NAME:
        payload = args if isinstance(args, dict) else {}
        return translate_outcome(payload.get("text"), payload.get("target_lang"))
    return None


# ===========================================================================
# 4. 交给模型的工具
# ===========================================================================
_WEATHER_DESC = (
    "查询某个城市当前的天气。当用户问天气、要不要带伞、适不适合出门时使用。"
    "参数 city 是城市名，例如「上海」。返回一句中文天气描述。"
)

_TRANSLATE_DESC = (
    "把一段文本翻译成另一种语言。当用户要求翻译、或需要把某个词句换成别的语言时使用。"
    "参数 text 是要翻译的内容；target_lang 是目标语言（如「日语」「英文」或 ja/en），"
    "留空则中英自适应。返回译文。"
)

_wrapped_weather = _as_tool(
    WEATHER_TOOL_NAME, _WEATHER_DESC,
    lambda city: lookup_weather_text(city),
)
_wrapped_translate = _as_tool(
    TRANSLATE_TOOL_NAME, _TRANSLATE_DESC,
    lambda text, target_lang=None: translate_text(text, target_lang),
)

AGENT_TOOLS: tuple[Any, ...] = (_wrapped_weather, _wrapped_translate)
AGENT_TOOL_NAMES: tuple[str, ...] = tuple(
    getattr(item, "name", "") or getattr(item, "__name__", "") for item in AGENT_TOOLS
)


def tool_by_name(name: str) -> Any | None:
    """按名字取工具（用于测试与诊断）。"""
    for item in AGENT_TOOLS:
        if (getattr(item, "name", "") or getattr(item, "__name__", "")) == name:
            return item
    return None
