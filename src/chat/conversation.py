# -*- coding: utf-8 -*-
"""
src/chat/conversation.py —— 会话逻辑（纯函数为主，可离线单测）
================================================================

分工（这是本模块存在的唯一理由）：
    conversation.py  决策：这轮该不该调模型？该用什么话术？历史怎么拼？会话怎么隔离？
    chat_app.py      只负责渲染：把上面的决策画到 Streamlit 上

这样一来，"安全闸是否优先级最高""MEDIUM 是否一定带资源""多用户会话是否互相污染"
这些**真正要紧的判断**都能用普通 unittest 覆盖，不需要起 Streamlit、不需要 API Key。

架构约束（写在这里，避免以后被顺手改坏）
--------------------------------------------------------------------------
1. **安全闸先于模型**：`plan_reply()` 里 HIGH 直接返回 is_safe_reply=True，
   调用方**不允许**再调模型。危机回应不由模型生成。
2. **MEDIUM 允许模型参与，但必须带资源**：靠 `safety.contains_resource_reference()`
   做出口校验，缺了就追加——"自然"和"必须给资源"两者都要成立。
3. **会话按用户隔离**：隧道一开就是多人同时访问同一进程。`new_session()`
   每次调用生成独立的状态容器，绝不放进全局变量（那会导致 A 看到 B 的情绪状态，
   这是真实事故，不是理论风险）。
4. **不落盘**：本模块只操作内存对象，没有任何写文件路径。数据因会话结束而消失。
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from chat import persona
from chat import safety
from chat import tools as tool_layer
from chat.history import (
    DEFAULT_RETENTION_DAYS,
    HistoryStore,
    new_session_id,
    validate_session_id,
)
from chat.safety import SafetyHit, SafetyLevel, build_safe_reply, check_safety
from chat.state import DIMENSIONS, EmotionState, StateSnapshot

__all__ = [
    "Turn",
    "Session",
    "new_session",
    "EngineUnavailable",
    "available_engines",
    "make_model",
    "assemble_chat_messages",
    "plan_reply",
    "finalize_medium_reply",
    "gauge_rows",
    "trend_label",
    "ENGINE_TABLE",
    "DEFAULT_ENGINE",
    "THINKING_ENGINE",
    "DEFAULT_CHAT_ENGINE",
    "SEARCH_ENGINE",
    "mode_engine",
    "REASONING_ENGINES",
    "fast_engine_candidate",
    "primary_engines",
    "fallback_engines",
    "selectable_engines",
    "RESOURCE_FALLBACK",
    "ToolCall",
    "ToolPhase",
    "bind_tools",
    "run_tool_phase",
    "tool_disclosure",
    "tool_note_inline",
    # 从 history 复导出：调用方（chat_app）只需要 import conversation 一处
    "HistoryStore",
    "DEFAULT_RETENTION_DAYS",
    "new_session_id",
    "validate_session_id",
]

# ===========================================================================
# 1. 会话容器
# ===========================================================================
# 被中断的回复在**上下文里**的占位文本。
# 刻意中性、简短：不要写成"我上次被打断了"，那会诱发多余的道歉。
_INTERRUPTED_NOTE = "（上一条没有说完）"


@dataclass
class Turn:
    """一轮交互：用户说了什么、系统回了什么、当时的判定与状态。"""

    role: str                       # "user" 或 "assistant"
    content: str
    hint: str | None = None         # 附在该条用户消息后的内部状态提示
    safety_level: str = "none"
    rule_ids: tuple[str, ...] = ()
    fixed_reply: bool = False       # 是否走了确定性安全话术（未调用模型）
    resources_attached: bool = False
    persona_role: str = ""          # 这一轮用的姿态（roles.Role.role_id）
    elapsed_ms: int = 0             # 这一轮"想"了多久（整轮耗时）
    # 用户中途按了停止 → 这条助手回复**只说了一半**。
    #
    # 为什么要单独一个标记（不是把半句直接当完整回复存）：
    #   半句可能被截断在语义中间（例如只吐出"你可以试着……"）。如果当成
    #   完整回复存进记忆，模型以后会**以为自己说过那句**，甚至自己引用自己 ——
    #   那比丢掉更糟。所以：
    #     · 界面照常显示（用户确实看到了那半句）
    #     · **不进后续上下文**（见 to_message_for_context）
    interrupted: bool = False
    # 这一轮用了哪些工具（如 ("weather",) / ("translate",)）。
    #
    # 为什么要落盘：工具调用对用户是**重要信息** ——
    # "我说的话被送到哪里去了"应该是可审计的。
    # 而在 Streamlit 版里它是一句 st.caption，重跑就没了；这里存进数据，
    # 刷新后仍然能看到"这轮查了天气"。
    tools: tuple[str, ...] = ()
    # 这一轮模型的思考过程（DeepSeek reasoning_content 的累计文本）。
    #
    # 为什么要落盘：它原来只通过 SSE 实时送到前端，刷新或重开历史就没了。
    # 用户看到的"已思考（用时 14 秒）"只剩一行标题，点开却是空的 ——
    # 那不是"隐藏"，而是内容根本没存。它只用于展示，不进入模型上下文。
    thinking: str = ""

    def to_message(self) -> dict[str, Any]:
        """转成 persona.assemble_messages() 能吃的结构。"""
        return {"role": self.role, "content": self.content, "hint": self.hint}

    def to_message_for_context(self) -> dict[str, Any] | None:
        """用于**喂给模型的历史**。

        被中断的那条**不返回半句原文**，而是返回一句**中性占位**。
        为什么不是直接丢掉（这是第二版设计，第一版丢掉了）：
            丢掉的后果是上下文里会出现**两条连续的 user 消息**
            （用户被打断的那句 + 下一句），模型可能困惑或同时回应两句。
            用一句占位补上，对话结构始终是 user/assistant 交替，
            而且模型知道"上一条没说完"——这与事实一致，也不会让它
            以为自己说过那半句。
            占位刻意写得中性、简短：不要写成"我上次被打断了"，
            那会诱发多余的道歉。
        """
        if self.interrupted:
            return {"role": "assistant", "content": _INTERRUPTED_NOTE, "hint": None}
        return self.to_message()

    def to_storage(self) -> dict[str, Any]:
        """转成**可 JSON 序列化**的结构（供 history.HistoryStore 落盘）。

        与 to_message() 的区别：这里带上安全判定等元数据，且所有值都是
        基础类型（tuple 要转 list，否则 json.dumps 会报错）。
        """
        return {
            "role": self.role,
            "content": self.content,
            "hint": self.hint,
            "safety_level": self.safety_level,
            "rule_ids": list(self.rule_ids),
            "fixed_reply": bool(self.fixed_reply),
            "resources_attached": bool(self.resources_attached),
            "persona_role": self.persona_role,
            "elapsed_ms": int(self.elapsed_ms or 0),
            "interrupted": bool(self.interrupted),
            "tools": list(self.tools),
            "thinking": self.thinking,
        }

    @classmethod
    def from_storage(cls, payload: Mapping[str, Any]) -> "Turn":
        """从未知来源的 dict 恢复一轮（字段缺失/类型不符都容忍）。"""
        role = str(payload.get("role") or "")
        if role not in ("user", "assistant"):
            role = "assistant"
        try:
            elapsed = int(payload.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            elapsed = 0
        return cls(
            role=role,
            content=str(payload.get("content") or ""),
            hint=(str(payload["hint"]) if payload.get("hint") else None),
            safety_level=str(payload.get("safety_level") or "none"),
            rule_ids=tuple(str(x) for x in (payload.get("rule_ids") or ())),
            fixed_reply=bool(payload.get("fixed_reply")),
            resources_attached=bool(payload.get("resources_attached")),
            persona_role=str(payload.get("persona_role") or ""),
            elapsed_ms=max(0, elapsed),
            interrupted=bool(payload.get("interrupted")),
            tools=tuple(str(x) for x in (payload.get("tools") or ())),
            thinking=str(payload.get("thinking") or ""),
        )


@dataclass
class Session:
    """一次会话的全部内存态。

    刻意**没有** id/用户名字段：本项目不做身份识别，也不落盘，
    会话靠 Streamlit 的 session_state 天然隔离（每个浏览器标签一份）。
    """

    state: EmotionState = field(default_factory=EmotionState)
    turns: list[Turn] = field(default_factory=list)
    last_symbol: str | None = None

    # ---- 历史操作 ----
    def add_user(self, text: str, *, hint: str | None = None,
                 hit: SafetyHit | None = None) -> Turn:
        turn = Turn(
            role="user",
            content=str(text),
            hint=hint,
            safety_level=(hit.level.value if hit else "none"),
            rule_ids=(tuple(hit.rule_ids) if hit else ()),
        )
        self.turns.append(turn)
        return turn

    def add_assistant(self, text: str, *, fixed_reply: bool = False,
                      resources_attached: bool = False,
                      persona_role: str = "", elapsed_ms: int = 0,
                      interrupted: bool = False,
                      tools: Sequence[str] = (),
                      thinking: str = "") -> Turn:
        turn = Turn(
            role="assistant",
            content=str(text),
            fixed_reply=fixed_reply,
            resources_attached=resources_attached,
            persona_role=str(persona_role or ""),
            elapsed_ms=max(0, int(elapsed_ms or 0)),
            interrupted=bool(interrupted),
            tools=tuple(str(x) for x in tools),
            thinking=str(thinking or ""),
        )
        self.turns.append(turn)
        return turn

    def clear(self) -> None:
        """一键清空：把这条会话的一切（对话 + 情绪状态）抹掉。"""
        self.state = EmotionState()
        self.turns.clear()
        self.last_symbol = None

    # ---- 只读视图 ----
    @property
    def turn_count(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")

    @property
    def snapshot(self) -> StateSnapshot | None:
        if self.state.records:
            return self.state.records[-1].snapshot
        return None

    @property
    def last_safety_level(self) -> str:
        for turn in reversed(self.turns):
            if turn.role == "user":
                return turn.safety_level
        return "none"

    @property
    def last_persona_role(self) -> str | None:
        """最近一次**真正用了**的姿态（跳过安全话术那几轮）。

        为什么要跳过：高危/中危那几轮由固定话术接管，没有姿态可言；
        把它们算进来会让"上一轮角色"这个信号失真。
        """
        for turn in reversed(self.turns):
            if turn.role == "assistant" and turn.persona_role:
                return turn.persona_role
        return None

    def set_last_persona_role(self, role_id: str) -> None:
        """把姿态标到最近一条用户消息上（反馈归因用它）。"""
        for turn in reversed(self.turns):
            if turn.role == "user":
                turn.persona_role = role_id
                return

    @property
    def last_persona_role_for_feedback(self) -> str:
        for turn in reversed(self.turns):
            if turn.role == "user" and turn.persona_role:
                return turn.persona_role
        return ""

    def chat_messages(self) -> list[dict[str, Any]]:
        """喂给模型的历史。

        ★ 被中断的半截回复**不进这里**（`to_message_for_context` 返回 None）。
        为什么：半句可能截断在语义中间（例如"你可以试着……"）。如果当成
        完整回复喂给模型，它下一轮会**以为自己说过那句**，甚至自己引用
        自己 —— 那比丢掉更糟。界面上照常显示，只是不参与推理。
        """
        out: list[dict[str, Any]] = []
        for turn in self.turns:
            item = turn.to_message_for_context()
            if item is not None:
                out.append(item)
        return out
    # ---- 落盘 / 恢复（S3）----
    def to_storage(self) -> dict[str, Any]:
        """整条会话的可序列化结构：对话 + 情绪状态。"""
        return {
            "turns": [turn.to_storage() for turn in self.turns],
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_storage(cls, payload: Mapping[str, Any] | None) -> "Session":
        """从历史记录恢复出一条会话。

        容错原则：历史文件可能被手改、被截断、或来自旧版本，
        任何字段异常都退化为"这一轮跳过 / 状态重置"，**绝不抛异常**——
        读不出历史最多是丢上下文，抛异常会让用户连页面都打不开。
        """
        session = cls(state=EmotionState())
        if not isinstance(payload, Mapping):
            return session

        state_payload = payload.get("state")
        if isinstance(state_payload, Mapping):
            try:
                session.state = EmotionState.from_dict(state_payload)
            except Exception:  # noqa: BLE001 - 坏状态就重置，不影响对话
                session.state = EmotionState()

        turns_payload = payload.get("turns")
        if isinstance(turns_payload, list):
            restored: list[Turn] = []
            for item in turns_payload:
                if isinstance(item, Mapping):
                    restored.append(Turn.from_storage(item))
            session.turns = restored
        return session


def new_session(*, use_llm_state: bool = False) -> Session:
    """创建一条**独立**会话。

    每次调用都返回全新对象——多用户场景下这是隔离的根基，
    永远不要把 Session 放进模块级变量。
    """
    return Session(state=EmotionState(use_llm=use_llm_state))


# ===========================================================================
# 2. 模型接入（主引擎 + 本地兜底，与 rag_demo.py 同一套思路）
# ===========================================================================
class EngineUnavailable(RuntimeError):
    """引擎不可用（缺依赖 / 缺 Key）。调用方应转成友好提示，而不是抛栈。"""


ENGINE_TABLE: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------
    # tier 字段决定这个引擎**是否出现在用户的选择器里**：
    #   "primary"  —— 出现在选择器里，用户可自选
    #   "fallback" —— 不出现在选择器里；只有主用引擎全部不可用时才自动顶上
    #
    # 为什么要有这个区分（这不是我的偏好，是实测结论）：
    #   我用同一份提示词、10 条覆盖不同场景的输入做过对照，测"反问率"与
    #   "问句是否附上'不想说也没关系'"：
    #       DeepSeek Flash：反问率 30%，凡问必附许可（100%），兜底触发 0%
    #       智谱 GLM 免费版：反问率 80%，仅 25% 附许可，兜底触发 60%
    #   也就是说 GLM 会**几乎每轮都反问**（闲聊"外面下雨了"要问"是不是影响
    #   心情"、报喜"面试过了"也要反问），而且无视许可规则。
    #
    #   ⚠️ 但请注意区分：GLM 的问题在**质量**，不在**安全**。
    #   安全属性（危机识别、求助热线）是代码级硬规则 + 确定性话术，
    #   **不依赖模型**，所以弱模型破坏不了安全底线，最坏只是"聊得不好"。
    #   正因为如此，它的正确处置是**降级备用**，而不是删除 ——
    #   删掉会同时失去降级链（DeepSeek 宕机就没得用）。
    # ---------------------------------------------------------------
    "deepseek": {
        "label": "DeepSeek Flash（云端，便宜快）",
        "tier": "primary",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com",
        "model_env": "DEEPSEEK_CHAT_MODEL",
        "model_default": "deepseek-flash",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "deepseek-pro": {
        "label": "DeepSeek V4 Pro（云端，默认）",
        "tier": "primary",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com",
        "model_env": "DEEPSEEK_PRO_MODEL",
        "model_default": "deepseek-v4-pro",
        "key_env": "DEEPSEEK_API_KEY",
    },
    # ---- 以下为降级备用：不在选择器里出现，只在主用引擎不可用时自动顶上 ----
    "zhipu": {
        # ★ label 要**如实标注弱项**：备用质量确实不如主用，但它可能是
        #   唯一还能用的（实测主用 141~362 秒时它只要 1.1 秒）。
        #   把弱项写出来，用户才知道什么时候该切、切了要预期什么。
        "label": "智谱 GLM（免费备用，说话方式不如主用稳）",
        "tier": "fallback",
        "base_url_env": "ZHIPU_BASE_URL",
        "base_url_default": "https://open.bigmodel.cn/api/paas/v4",
        "model_env": "ZHIPU_CHAT_MODEL",
        "model_default": "glm-4-flash",
        "key_env": "ZHIPU_API_KEY",
    },
    "ollama": {
        "label": "本地 Ollama（离线备用）",
        "tier": "fallback",
        "base_url_env": "OLLAMA_BASE_URL",
        "base_url_default": "http://localhost:11434/v1",
        "model_env": "OLLAMA_CHAT_MODEL",
        "model_default": "qwen2.5:3b",
        "key_env": "",
    },
}


def available_engines() -> dict[str, bool]:
    """探测各引擎当前是否**具备**启动条件（只查依赖与 Key 是否存在，不发起请求）。

    注意：返回 True 只代表"有 Key/有依赖"，不代表网络一定通——
    真正的失败在调用时暴露，由上层转成友好提示。
    """
    result: dict[str, bool] = {}
    try:
        import langchain_openai  # noqa: F401
        has_sdk = True
    except Exception:
        has_sdk = False

    for name, config in ENGINE_TABLE.items():
        key_env = config["key_env"]
        key_ok = True if not key_env else bool(os.environ.get(key_env, "").strip())
        result[name] = bool(has_sdk and key_ok)
    return result


# 未显式指定时的默认引擎。
#
# 2026-10-20 的三题同提示词实测显示：Pro 在细腻度上仍略胜，但单轮
# 首字约 5~19 秒；Flash 质量接近，延迟低 3~5 倍。默认对话采用 Flash，
# 用户开启「对话思考」时才显式切到 Pro。这样默认值、页面路由和
# 直连接口说的是同一件事，不会出现"配置写 Pro、实际跑 Flash"的分叉。
DEFAULT_ENGINE = "deepseek"

# 引擎能力标签（给"分任务路由"用，见 src/server/api.py 的 _stream_events）。
#
# 为什么需要这两个常量（不是拍脑袋，是测量 + 文献双向论证）：
#   · REASONING_ENGINES：会先"想"再说的引擎。实测 V4 Pro 的思考
#     让情绪陪伴类回答更具体、更暖（同提示词对照，见 DEFAULT_ENGINE 注释），
#     但代价是首字前有一段 8~45 秒的静默。
#   · 查询/工具类任务（天气、翻译、查定义）**不需要**这段思考 ——
#     CHI 2026《The Impact of Response Latency and Task Type on
#     Human-LLM Interaction and Perception》的结论是：lookup 类任务
#     用户只认快；creation/情绪类任务则宁可看到"有在认真想"。
#     同日三题实测又补了一层：Flash 在默认对话里的质量已接近 Pro，
#     而延迟低一个量级，因此默认与联网走 Flash，只有用户显式开启
#     「对话思考」时才切到 Pro；这不是取消分层，而是把 Pro 用在
#     用户明确愿意等待的那一档。
REASONING_ENGINES: frozenset[str] = frozenset({"deepseek-pro"})

# 快速引擎的优先顺序（工具判定与查询类回答用它）。
FAST_ENGINE_IDS: tuple[str, ...] = ("deepseek",)

# 2026-10-20 三题同提示词实测：Pro 最细腻但 5~19 秒；
# Flash 质量接近且快 3~5 倍；智谱在长建议题上更模板化。
# 所以思考模式用 Pro，默认与联网回答用 Flash，智谱保留降级。
THINKING_ENGINE = "deepseek-pro"
DEFAULT_CHAT_ENGINE = "deepseek"
# 联网检索的查询生成由 Flash 负责；真正的网页读取仍由 web_search 层完成。
SEARCH_ENGINE = "deepseek"


def mode_engine(thinking: bool) -> str:
    """按界面模式选择最终回答模型，并在首选不可用时按顺序降级。

    思考开启：Pro → Flash → 智谱 → Ollama
    思考关闭：Flash → Pro → 智谱 → Ollama

    这里只检查 Key/依赖是否配好，不发起网络请求；真正连接失败仍由
    `make_model` 之后的调用路径处理。
    """
    ready = available_engines()
    preferred = THINKING_ENGINE if thinking else DEFAULT_CHAT_ENGINE
    if ready.get(preferred):
        return preferred
    order = (THINKING_ENGINE, DEFAULT_CHAT_ENGINE, SEARCH_ENGINE,
             "zhipu", "ollama")
    for name in order:
        if name != preferred and ready.get(name):
            return name
    return preferred


def fast_engine_candidate(requested: str) -> str | None:
    """挑一个**当前可用**的快速引擎（不和 requested 重复）。

    返回 None 表示没有可用的快速引擎 —— 调用方应当留在原引擎上，
    而不是报错（这属于"没得选"而不是"选错了"）。
    """
    ready = available_engines()
    for name in FAST_ENGINE_IDS:
        if name != requested and ready.get(name):
            return name
    return None


def primary_engines() -> list[str]:
    """**可以出现在用户选择器里**的引擎（tier == "primary"）。

    全部不可用时返回空列表 —— 调用方应当退到 `fallback_engines()`，
    而不是给用户一个空下拉框。
    """
    ready = available_engines()
    return [name for name, cfg in ENGINE_TABLE.items()
            if cfg.get("tier") == "primary" and ready.get(name)]


def fallback_engines() -> list[str]:
    """备用引擎：不出现在选择器里，只在前者全不可用时自动顶上。"""
    ready = available_engines()
    return [name for name, cfg in ENGINE_TABLE.items()
            if cfg.get("tier") == "fallback" and ready.get(name)]


def selectable_engines() -> tuple[list[str], bool]:
    """返回 (选择器里该显示的引擎, 是否**没有主用可用**)。

    ★ 2026-10-15 改动：以前只显示主用引擎，备用被藏起来。
      藏起来的理由本身成立 —— 实测备用（GLM）质量较差：反问率 80%、
      只有 25% 会给"不说也没关系"，而主用是 30% / 100%。

      **但判断"能不能选"用的是"有没有配 Key"，不是"现在能不能用"。**
      于是撞上了一次真实事故：
          DeepSeek 配了 Key（所以"可用"），实际响应 **141~362 秒**；
          而 GLM 只要 **1.1 秒**。
          系统却把 GLM 藏了起来 ——
          **用户被锁死在两个几乎不可用的引擎上，界面上没有任何出口。**
      写这些代码时我以为"藏起来"是在保护用户不受弱模型影响，
      实际是在**保护一个已经半死的主用**。

      所以改成：**所有配好的引擎都能选**。质量差异靠 label 如实标注，
      由用户自己决定 —— 这符合本项目一贯的做法：
      宁可如实说，也不替用户做他看不见的决定。

      `degraded` 的语义因此变成"**一个主用都没有**"（界面要如实提示），
      而不是"用户没得选"。
    """
    ready = available_engines()
    names = [name for name, cfg in ENGINE_TABLE.items() if ready.get(name)]
    has_primary = any(ENGINE_TABLE[n].get("tier") == "primary" for n in names)
    return names, (not has_primary)


def make_model(engine: str = DEFAULT_ENGINE, *, temperature: float = 0.7,
               streaming: bool = True,
               timeout_s: float | None = None) -> Any:
    """构造聊天模型。

    temperature 默认 0.7：与 agent_tool_demo.py 的 0.0 刻意不同——
    工具调用要的是确定性，陪伴对话要的是自然；0.0 会让人感觉在跟说明书说话。

    失败一律抛 EngineUnavailable（带可操作的中文提示），
    由 chat_app.py 转成页面提示，绝不让 traceback 打到用户脸上。
    """
    config = ENGINE_TABLE.get(engine)
    if config is None:
        raise EngineUnavailable(f"未知引擎：{engine}（可选：{'、'.join(ENGINE_TABLE)}）")

    # 先校验「缺哪个 Key」，再校验依赖。
    # 顺序很重要：缺 Key 是用户能立刻自己修的，报这个最有用；
    # 反过来的话，用户会看到"缺少依赖"而去装一个其实不缺的包。
    key_env = config["key_env"]
    api_key: str | None = None
    if key_env:
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise EngineUnavailable(
                f"未配置 {key_env}（{config['label']} 需要它）。\n"
                "本地可在仓库根 .env 填写，或设置系统环境变量。\n"
                "也可以把引擎切换为「本地 Ollama」在完全离线的情况下使用。"
            )

    try:
        from langchain_openai import ChatOpenAI
    except Exception as exc:  # pragma: no cover - 取决于环境
        raise EngineUnavailable(
            "缺少依赖 langchain-openai。请先安装：pip install langchain-openai"
        ) from exc

    base_url = os.environ.get(config["base_url_env"], "").strip() or config["base_url_default"]
    model_name = os.environ.get(config["model_env"], "").strip() or config["model_default"]

    kwargs: dict[str, Any] = {
        "model": model_name,
        "base_url": base_url.rstrip("/"),
        "temperature": float(temperature),
        "streaming": bool(streaming),
        # ★ 超时可配置，默认从 60 秒提到 120 秒。
        #   依据是一次真实测量：DeepSeek 过载期单次响应 **141~362 秒**，
        #   而超时设的是 60 秒 —— 于是**每次都可能被自己掐断**，
        #   用户看到的是"发出去没反应"。60 秒在那个时段是不够的。
        #
        #   为什么不是设成无限大：陪伴类对话里"等 6 分钟"本身就是伤害
        #   （用户盯着空白）。所以策略是"给足 + 明确告知 + 让用户能换引擎"，
        #   而不是让请求无限挂着。
        "timeout": float(
            timeout_s if timeout_s is not None
            else (os.environ.get("HEYTALK_LLM_TIMEOUT_S", "").strip() or 120)
        ),
        # 重试交给上层按"有没有吐过字"决定，这里**关掉**自动重试 ——
        # 否则流式出现半句后再自动重试，用户会看到话说两遍。
        "max_retries": 0,
    }
    if api_key:
        kwargs["api_key"] = api_key
    else:
        # 本地 Ollama 不校验 Key，但 OpenAI 兼容客户端要求非空字符串
        kwargs["api_key"] = "ollama"

    try:
        return ChatOpenAI(**kwargs)
    except Exception as exc:  # pragma: no cover
        raise EngineUnavailable(f"初始化模型失败：{type(exc).__name__}: {exc}") from exc


# ===========================================================================
# 3. Prompt 拼装
# ===========================================================================
def assemble_chat_messages(session: Session, *, max_turns: int = persona.HISTORY_TURNS,
                           include_hint: bool = True,
                           role_instruction: str | None = None,
                           profile_block: str | None = None,
                           knowledge_block: str | None = None,
                           scaffold_instruction: str | None = None) -> list[dict[str, str]]:
    """把会话拼成 messages。

    include_hint=False 用于「先不带状态提示重试」这类降级场景。
    role_instruction / profile_block / knowledge_block 由调用方传入
    （见 role_router.route_role、profile.build_profile_block、
      knowledge.build_method_block）。
    """
    history = session.chat_messages()
    if not include_hint:
        history = [dict(item, hint=None) for item in history]
    return persona.assemble_messages(history, max_turns=max_turns,
                                     role_instruction=role_instruction,
                                     profile_block=profile_block,
                                     knowledge_block=knowledge_block,
                                     scaffold_instruction=scaffold_instruction)


# ===========================================================================
# 3.5 工具调用（S2）
#
#     为什么把工具阶段和回答阶段**分开**：
#         - 工具调用需要把用户原话交给模型与第三方接口，所以它必须发生在
#           "已经确认不是高危"之后（由 plan_reply 决定，本函数不做判断）；
#         - 分两段之后，工具调用结果可以显示给用户看（透明、可审计），
#           而最终回答依然能走流式输出（保住打字机体验）。
# ===========================================================================
@dataclass
class ToolCall:
    """一次工具调用的完整记录（给界面展示，也给测试断言）。"""

    name: str
    arguments: dict[str, Any]
    result: str
    error: str = ""
    # 机器可读状态（目前翻译用：unsupported-language / engine-error / ok）。
    # 空串 = 这个工具没有状态语义，UI 不需要额外提示。
    status: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "result": self.result,
            "error": self.error,
            "status": self.status,
        }

    def label(self) -> str:
        """一行摘要，用于界面折叠区标题（不含用户原话）。"""
        args = "、".join(f"{k}={v}" for k, v in self.arguments.items())
        return f"{self.name}({args})" if args else self.name


@dataclass
class ToolPhase:
    """工具阶段的结果。"""

    calls: list[ToolCall] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def used_tools(self) -> bool:
        return bool(self.calls)

    @property
    def extra_messages(self) -> list[dict[str, Any]]:
        """需要追加到 messages 的部分（助手发起调用 + 工具返回），不含基座消息。"""
        if not self.messages:
            return []
        # messages 的第 0 条是传入的基座列表，其余是追加的
        return self.messages[1:]

    def to_dict(self) -> dict[str, Any]:
        return {"calls": [c.to_dict() for c in self.calls]}


def bind_tools(model: Any, tools: Sequence[Any] | None = None) -> Any | None:
    """把工具绑定到模型；不支持绑定时返回 None（调用方退化为纯对话）。

    为什么不用 create_agent：本项目需要**对高危路径的绝对控制权**
    （HIGH 时一个模型调用都不许发生），而预置 Agent 会把工具循环封装起来。
    自己编排虽然代码多一点，但"什么时候调模型"这件事必须是我们说了算。
    """
    items = list(tools if tools is not None else tool_layer.AGENT_TOOLS)
    if not items:
        return None
    try:
        return model.bind_tools(items)
    except Exception:  # noqa: BLE001 - 不支持就退化，不打断对话
        return None


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """从模型响应里取出工具调用（兼容 dict 与对象两种形态）。"""
    raw = getattr(response, "tool_calls", None)
    if raw is None and isinstance(response, Mapping):
        raw = response.get("tool_calls")
    if not raw:
        return []
    calls: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "")
            args = item.get("args") or item.get("arguments") or {}
            call_id = str(item.get("id") or "")
        else:
            name = str(getattr(item, "name", "") or "")
            args = getattr(item, "args", {}) or {}
            call_id = str(getattr(item, "id", "") or "")
        if not name:
            continue
        if not isinstance(args, Mapping):
            args = {"input": args}
        calls.append({"name": name, "args": dict(args), "id": call_id})
    return calls


_DSML_INVOKE_RE = re.compile(
    r'<\|\|DSML\|\|invoke\s+name="([^"]+)"\s*>(.*?)</\|\|DSML\|\|invoke>',
    re.S,
)
_DSML_PARAM_RE = re.compile(
    r'<\|\|DSML\|\|parameter\s+name="([^"]+)"[^>]*>(.*?)</\|\|DSML\|\|parameter>',
    re.S,
)


def _extract_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
    """兼容层：有些引擎不返回 tool_calls，而是把 DSML 当正文吐出来。

    实测 DeepSeek 会把工具调用写成：

        <||DSML||callss>
        <||DSML||invoke name="get_current_weather">
        <||DSML||parameter name="city" string="true">Tokyo</||DSML||parameter>
        </||DSML||invoke>
        </||DSML||calls>

    这段文本如果直接流给用户，就是界面上出现的那串"代码"。
    这里只负责**解析**，调用仍要走 `tool_by_name` 白名单，模型不能凭空
    调到不存在的工具。
    """
    raw = str(text or "")
    if "<||DSML||" not in raw:
        return []
    calls: list[dict[str, Any]] = []
    for index, match in enumerate(_DSML_INVOKE_RE.finditer(raw)):
        name = match.group(1).strip()
        if not name:
            continue
        args: dict[str, Any] = {}
        for param in _DSML_PARAM_RE.finditer(match.group(2)):
            value = param.group(2).strip()
            if len(value) >= 2 and value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            args[param.group(1).strip()] = html.unescape(value)
        calls.append({"name": name, "args": args, "id": f"dsml_{index}"})
    return calls


def run_tool_phase(model: Any, messages: list[dict[str, Any]], *,
                   tools: Sequence[Any] | None = None,
                   max_tools: int = 3) -> ToolPhase:
    """让模型决定是否调用工具，执行后把结果拼回 messages。

    **调用方必须先确认不是高危**（本函数不做安全判定，也无法做——
    它只看得到已经拼好的 messages）。

    流程：
        1. bind_tools 后 invoke 一次 → 看模型是否要调工具
        2. 执行工具（每个工具都保证返回可读文本，不抛异常）
        3. 把"助手发起调用 + 工具返回"追加进 messages，供最终回答使用

    任何一步失败都返回空 ToolPhase（= 当没工具，继续正常对话），
    绝不因为工具坏了就答不出话。
    """
    bound = bind_tools(model, tools)
    if bound is None:
        return ToolPhase()

    try:
        response = bound.invoke(messages)
    except Exception:  # noqa: BLE001 - 工具阶段失败 → 退化纯对话
        return ToolPhase()

    limit = max(0, int(max_tools))
    requested = _extract_tool_calls(response)[:limit]
    response_text = _as_text(response)
    used_dsml = False
    if not requested:
        # 兼容层：模型把工具调用写成 DSML 正文时，在这里接住并真正执行，
        # 而不是让它原样流到用户界面上。
        requested = [call for call in _extract_dsml_tool_calls(response_text)
                     if tool_layer.tool_by_name(call["name"])][:limit]
        used_dsml = bool(requested)
    if not requested:
        return ToolPhase()

    working = list(messages)
    working.append({"role": "assistant",
                    "content": "" if used_dsml else response_text,
                    "tool_calls": [
                        {"name": c["name"], "args": c["args"], "id": c["id"]}
                        for c in requested
                    ]})

    executed: list[ToolCall] = []
    for call in requested:
        name, args = call["name"], dict(call["args"])
        result, error, status = _invoke_tool(name, args)
        executed.append(ToolCall(name=name, arguments=args, result=result,
                                 error=error, status=status))
        working.append({
            "role": "tool",
            "content": result,
            "name": name,
            "tool_call_id": call["id"],
        })

    return ToolPhase(calls=executed, messages=working)


def _invoke_tool(name: str, args: Mapping[str, Any]) -> tuple[str, str, str]:
    """执行一个工具，返回 (结果文本, 错误说明, 状态)。永不抛异常。"""
    item = tool_layer.tool_by_name(name)
    if item is None:
        return (f"没有名为 {name} 的工具。", f"unknown-tool:{name}", "")
    try:
        outcome = tool_layer.tool_outcome(name, args)
        if outcome is not None:
            return outcome.text, "", outcome.status
        if hasattr(item, "invoke"):
            result = item.invoke(dict(args))
        else:  # pragma: no cover - 兜底
            result = item(**dict(args))
        return str(result), "", ""
    except Exception as exc:  # noqa: BLE001 - 工具出错也要让对话继续
        return (f"工具 {name} 执行失败：{type(exc).__name__}。",
                f"{type(exc).__name__}", "")


def _as_text(response: Any) -> str:
    """把模型响应里的文本取出来（工具阶段只需要它的文字部分）。"""
    content = getattr(response, "content", None)
    if content is None and isinstance(response, Mapping):
        content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def tool_disclosure(phase: ToolPhase) -> str | None:
    """把工具调用渲染成给用户看的说明（透明：让人知道数据去哪了）。"""
    if not phase.used_tools:
        return None
    lines = ["（为了让回答更有依据，我用了下面这些工具："]
    for call in phase.calls:
        if call.ok:
            lines.append(f"- {call.label()} → {call.result}")
        else:
            lines.append(f"- {call.label()} → 没能成功（{call.error}）")
    lines.append("）")
    return "\n".join(lines)


def tool_note_inline(phase: ToolPhase) -> str:
    """把工具痕迹压成**一句**自然的话，追加进回答正文。

    为什么不用 st.expander 展示（这是实跑才发现的产品级 bug）：
        handle_turn 结束后会 st.rerun() 重渲染整页，而 expander 是在
        handle_turn 执行过程中创建的 —— 重渲染后就没了。
        结果是：工具调用对用户**完全不可见**，透明度等于零。
        把它写进回答正文（会随对话一起持久化）才是真正可见的。

    措辞刻意低调（"查了一下天气"），避免把工具细节堆到用户脸上。
    """
    if not phase.used_tools:
        return ""
    parts: list[str] = []
    for call in phase.calls:
        if call.ok:
            parts.append(call.result.strip())
        else:
            parts.append(f"（{call.name} 这次没查成）")
    return "\n\n*（我查了一下：" + "；".join(parts) + "）*"


# ===========================================================================
# 4. 本轮该怎么回应（安全闸优先，纯函数）
# ===========================================================================
@dataclass(frozen=True)
class ReplyPlan:
    """本轮回应计划：**这是全项目最要紧的一个决策结果**。"""

    hit: SafetyHit
    use_safe_reply: bool            # True = 用固定话术，禁止调用模型
    safe_reply: str | None
    must_attach_resources: bool     # True = 模型回复后必须校验/补上求助通道
    reason: str

    @property
    def level(self) -> SafetyLevel:
        return self.hit.level


def plan_reply(user_text: str) -> ReplyPlan:
    """根据安全判定决定本轮回应方式。

    规则（顺序即优先级）：
        HIGH   → 固定话术，**禁止**模型参与
        MEDIUM → 允许模型参与，但回复必须包含求助通道
        LOW    → 正常对话
        NONE   → 正常对话
    """
    hit = check_safety(user_text)

    if hit.level is SafetyLevel.HIGH:
        return ReplyPlan(
            hit=hit,
            use_safe_reply=True,
            safe_reply=build_safe_reply(hit),
            must_attach_resources=False,   # 固定话术里已含资源
            reason="高危：使用确定性话术并阻断模型自由发挥",
        )
    if hit.level is SafetyLevel.MEDIUM:
        return ReplyPlan(
            hit=hit,
            use_safe_reply=False,
            safe_reply=None,
            must_attach_resources=True,
            reason="中危：允许模型参与，但出口必须校验求助通道",
        )
    return ReplyPlan(
        hit=hit,
        use_safe_reply=False,
        safe_reply=None,
        must_attach_resources=False,
        reason="未触发危机流程：正常对话",
    )


RESOURCE_FALLBACK = (
    "\n\n---\n"
    "如果这种状态持续下去，请考虑联系专业帮助（有人 24 小时接听）：\n"
    "{lines}\n\n"
    "如有立即的生命危险，请拨打 120（急救）或 110（报警）。\n"
    "我不是心理咨询师，无法做诊断，也不能替代专业帮助。"
)


def finalize_medium_reply(reply_text: str, *,
                          hotlines: Iterable[dict[str, str]] | None = None,
                          region: str = "cn") -> tuple[str, bool]:
    """MEDIUM 出口校验：模型没给求助通道就补上。

    返回 (最终文本, 是否追加过)。追加而不是替换——模型那部分通常更自然，
    用户需要的是"自然的陪伴 + 确定的资源"，两者都要。
    """
    text = str(reply_text or "")
    if safety.contains_resource_reference(text, hotlines):
        return text, False
    return text + RESOURCE_FALLBACK.format(lines=safety.format_hotlines(region, hotlines)), True


# ===========================================================================
# 5. 情绪气压计（给界面用；数值不算好看，但透明、可否认）
# ===========================================================================
_GAUGE_LABELS: tuple[tuple[str, str], ...] = (
    ("valence", "情绪"),
    ("arousal", "张力"),
    ("dominance", "掌控感"),
    ("energy", "精力"),
)

_SYMBOLS = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")


def trend_label(series: Sequence[float]) -> str:
    """把最近几条数值渲染成迷你趋势符号（无需图表库）。"""
    if not series:
        return ""
    out: list[str] = []
    for value in series[-12:]:
        index = int(min(0.999, max(0.0, float(value))) * len(_SYMBOLS))
        out.append(_SYMBOLS[index])
    return "".join(out)


def gauge_rows(snapshot: StateSnapshot | None) -> list[dict[str, Any]]:
    """把快照转成界面可直接渲染的行（含中性标签与百分比）。

    为什么用「中性描述 + 百分比」而不是临床词：
    这是给用户自己看的镜子，不是给医生看的报告。
    """
    if snapshot is None:
        return []
    description = persona.describe_state(snapshot.dims)
    parts = dict(
        item.split("=", 1) for item in description.split("；") if "=" in item
    )
    rows: list[dict[str, Any]] = []
    for name, label in _GAUGE_LABELS:
        value = float(snapshot.dims.get(name, 0.5))
        rows.append({
            "name": name,
            "label": label,
            "value": value,
            "percent": int(round(value * 100)),
            "wording": parts.get(name, ""),
        })
    return rows
