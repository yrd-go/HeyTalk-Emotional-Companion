"""根据最近对话生成首屏推荐话题。

首屏建议不是装饰：它要能帮用户想起"上次说到哪里"。这里刻意不用模型生成，
而是从用户自己的短句里抽取，再用固定句式改写。理由是：

* 不增加一次模型调用的延迟和费用；
* 不会把用户没说过的事写成事实；
* 历史很短、文件损坏或没有权限时，总能退回默认四句话。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

DEFAULT_OPENING_SUGGESTIONS = (
    "今天有点累",
    "晚上总是睡不着",
    "脑子里一直停不下来，很烦躁",
    "没什么大事，就想说说话",
)

_MAX_SESSIONS = 8
_MAX_USER_TEXTS = 16
_EXCERPT_CHARS = 22

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]")
_TRAILING_PUNCT = "，,。.!！?？;；:：\"'“”‘’"
_LOW_VALUE_REPLIES = {
    "你好", "您好", "嗨", "hi", "hello", "在吗", "谢谢", "好的", "嗯", "测试",
}
_TOOL_PREFIXES = (
    "帮我查", "帮我搜", "帮我翻译", "查一下", "查询", "搜索", "联网",
    "翻译", "请翻译", "打开", "播报",
)

# 顺序即优先级；同分时取更靠前的一类。
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("睡不着", "失眠", "熬夜", "睡不好", "睡觉", "做梦"),
     "最近睡得不太好，想聊聊怎么调整"),
    (("累", "疲惫", "没力气", "撑不住", "心力交瘁"),
     "最近还是有点累，想慢慢缓一缓"),
    (("焦虑", "烦", "压力", "紧张", "停不下来", "内耗"),
     "脑子还是有点乱，想理一理"),
    (("工作", "上班", "加班", "老板", "同事", "考研", "考试", "学习",
      "面试", "论文", "作业"),
     "工作学习上的事还压在心里，想说说"),
    (("妈妈", "爸爸", "家人", "朋友", "对象", "恋爱", "分手", "吵架", "关系"),
     "最近和重要的人有些事，想聊聊"),
    (("孤独", "一个人", "没人", "寂寞"),
     "一个人的时候有点空，想找人说说话"),
    (("生病", "医院", "疼", "不舒服", "身体"),
     "最近身体不太舒服，想聊聊"),
    (("电影", "音乐", "游戏", "书", "运动", "旅行", "旅游"),
     "想聊聊最近喜欢的电影、音乐或游戏"),
)


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_usable(text: str) -> bool:
    if len(text) < 4 or text.casefold() in _LOW_VALUE_REPLIES:
        return False
    if any(text.startswith(prefix) for prefix in _TOOL_PREFIXES):
        return False
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
        return False
    return True


def _excerpt(text: Any) -> str:
    cleaned = _clean_text(text)
    if not _is_usable(cleaned):
        return ""
    first = _SENTENCE_SPLIT_RE.split(cleaned, maxsplit=1)[0].strip()
    if len(first) < 4:
        first = cleaned
    if len(first) > _EXCERPT_CHARS:
        first = first[:_EXCERPT_CHARS].rstrip("，,、 ")
    return first.strip(_TRAILING_PUNCT)


def _recent_user_texts(sessions: list[Any]) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for session in sessions[:_MAX_SESSIONS]:
        turns = getattr(session, "turns", None)
        if not isinstance(turns, list):
            continue
        for turn in reversed(turns):
            if not isinstance(turn, Mapping) or turn.get("role") != "user":
                continue
            excerpt = _excerpt(turn.get("content"))
            if not excerpt:
                continue
            key = excerpt.casefold()
            if key in seen:
                continue
            seen.add(key)
            texts.append(excerpt)
            if len(texts) >= _MAX_USER_TEXTS:
                return texts
    return texts


def _category_topics(texts: list[str], *, limit: int) -> list[str]:
    scores: list[tuple[int, int, str]] = []
    for index, (keywords, topic) in enumerate(_CATEGORY_RULES):
        score = 0
        for text_index, text in enumerate(texts):
            if any(keyword in text for keyword in keywords):
                score += max(1, 8 - text_index)
        if score:
            scores.append((score, index, topic))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [topic for _, _, topic in scores[:max(0, limit)]]


def _dedupe(candidates: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        key = re.sub(r"[\s，,。.!！?？:：\"'“”‘’]+", "", text).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def build_opening_suggestions(history: Any, *, limit: int = 4) -> list[str]:
    """返回至多 ``limit`` 条推荐话题，失败时永远有默认值。"""
    if limit <= 0:
        return []
    try:
        sessions = list(history.list_sessions())
    except Exception:  # noqa: BLE001 - 建议是加分项，不能拖垮启动
        sessions = []

    texts = _recent_user_texts(sessions)
    candidates: list[str] = []
    if texts:
        candidates.append(f"我想接着说说：{texts[0]}")
    if len(texts) > 1:
        candidates.append(f"关于{texts[1]}，我还想多聊一点")

    candidates.extend(_category_topics(texts, limit=min(3, limit)))
    if len(texts) > 2:
        candidates.append(f"最近我在想：{texts[2]}")

    candidates.extend(DEFAULT_OPENING_SUGGESTIONS)
    return _dedupe(candidates, limit)
