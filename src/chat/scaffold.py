# -*- coding: utf-8 -*-
"""按意图注入的短支架（目前只保留工具追问与翻译两类）。

为什么只剩两类（有否决证据，不是随手缩水）：
    50 例 × 2 评委 × 正反序的盲评里，支架版整体 73 胜 102 负；
    按类型拆开是 knowledge 12:35、memory 0:4、boundary 5:7，
    只有 tool 3:1 与 translation 4:0 没有输。知识类支架会把回答削短，
    评委在“自然度/理解”上扣分，所以整类撤掉。

为什么现在默认开启：
    这两类扩到 40 例（20+20）、双评委正反序盲评后是 94 胜 43 负 23 平，
    均分 4.206 对 3.900，两个评委方向一致；非这两类的输入一律不注入。
    如要临时关闭，设 HEYTALK_INTENT_SCAFFOLD=0。

支架只做“这一轮该把注意力放哪”的提示，不替代安全闸、工具结果或记忆门控。
"""
from __future__ import annotations

import os
import re
from typing import Any

__all__ = ["classify", "build_scaffold", "enabled", "ENV_FLAG"]

ENV_FLAG = "HEYTALK_INTENT_SCAFFOLD"
_FALSE = {"0", "false", "no", "off"}

_WS_RE = re.compile(r"\s+")

_BOUNDARY_RE = re.compile(
    r"(胸口|膝盖|头疼|头痛|肚子|胃|发烧|医院|医生|体检报告|检查结果|症状|心梗|阿司匹林|"
    r"吃药|用药|诊断|抑郁|焦虑症|押金|房东|合同|起诉|律师|报警|法律|违法|"
    r"换锁|仲裁|投资|股票|基金|贷款|急救|中毒)"
)
_TOOL_RE = re.compile(
    r"(?:(?:刚才|上次|之前|刚刚).{0,10}(?:查|看|问|说).{0,10}(?:天气|翻译|结果)"
    r"|刚才查的|上次查的|明天会下雨)"
)
_MEMORY_RE = re.compile(r"(我之前说过|我说过什么|你记得我|还记得我|我喜欢什么来着)")
_TRANSLATION_RE = re.compile(r"(翻译|译成|翻成|拉丁文|英文怎么说|用.{0,6}语怎么说)")
_CLEAR_MEMORY_RE = re.compile(
    r"(?:(?:忘掉|忘记|清除|清空|删除|删掉).{0,12}(?:所有|全部|一切)"
    r"|(?:所有|全部|一切).{0,12}(?:忘掉|忘记|清除|清空|删除|删掉))"
)

_TEXTS = {
    "tool": "【本轮支架】依赖上一轮工具结果：看不到就直说，并给一个可执行的替代判断；不要猜数据。",
    "translation": "【本轮支架】翻译题：先给准确译文，再补一句语气或用法；不要展开无关内容。",
}


def enabled() -> bool:
    """线上开关：默认开启（仅 tool/translation 两类）；设 0 可关闭。"""
    return os.environ.get(ENV_FLAG, "").strip().lower() not in _FALSE


def classify(text: Any) -> str:
    """返回本轮支架类型；不需要支架时返回空字符串。

    边界/记忆类一律返回空：它们曾经被注入过，但在盲评里输了，
    这里保留为**排除项**，避免它们被误判成工具或翻译题。
    """
    normalized = _WS_RE.sub("", str(text or ""))
    if not normalized:
        return ""
    if _BOUNDARY_RE.search(normalized):
        return ""
    if _CLEAR_MEMORY_RE.search(normalized):
        return ""
    if _MEMORY_RE.search(normalized):
        return ""
    if _TOOL_RE.search(normalized):
        return "tool"
    if _TRANSLATION_RE.search(normalized):
        return "translation"
    return ""


def build_scaffold(text: Any) -> str | None:
    """给这一轮返回一段短支架；不需要时返回 None（不打扰模型）。"""
    return _TEXTS.get(classify(text))

