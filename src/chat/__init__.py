# -*- coding: utf-8 -*-
"""
src/chat/__init__.py —— 情绪陪伴 Agent（P0：状态模型 + 安全闸）

本包是「关注人的情绪与精神状态的聊天 Agent」的核心，刻意与现有五个模块
解耦：它不修改 router / rag_demo / multi_agent_demo / mcp_demo 的任何行为，
而是复用同一套项目纪律（零依赖兜底、严格契约、降级链、隐私最小化）。

P0 交付物：
    chat.safety  硬规则危机分级（跑在任何 LLM 调用之前，纯确定性）
    chat.state   双轨情绪状态模型（规则轨永远可用；模型轨默认关闭）

用法：
    from chat.safety import check_safety, build_safe_reply
    from chat.state import EmotionState

    text = "最近一直失眠，觉得自己很没用"
    hit = check_safety(text)                 # 先判安全
    if hit.blocks_freeform_llm:
        reply = build_safe_reply(hit)        # 高危：固定话术，不交给模型
    else:
        record = EmotionState().update(text, safety=hit)
"""

from __future__ import annotations

__all__ = ["safety", "state", "persona", "conversation", "history",
           "feedback", "tools", "roles", "role_router", "profile",
           "knowledge", "weather", "translation", "languages",
           "dialogue_planner"]
