# -*- coding: utf-8 -*-
"""轻量联网检索：只负责取回可引用的网页摘要。

设计边界
--------
* 这是**检索**，不是让模型自行决定要不要上网。用户显式打开“联网搜索”时才调用，
  这样行为可预期，也不会在每轮闲聊里偷偷多一次网络请求。
* 所有网络/解析异常都降级为空结果；调用方负责如实告诉用户“这次没查到”。
* 只取标题、链接与短摘要，并把长度封顶；不把整页 HTML 塞进上下文。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


_SPACE_RE = re.compile(r"\s+")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)


def _clean(value: str, limit: int) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[:limit]


def search_web(
    query: str,
    *,
    max_results: int = 5,
    timeout: float = 7.0,
) -> list[SearchHit]:
    """从 Bing 的公开结果页取前几条结果。

    `timeout` 同时限制连接与读取，避免把一次对话卡在网络请求上。
    失败时返回空列表，而不是抛异常。
    """
    query = _clean(query, 220)
    if not query:
        return []

    try:
        response = requests.get(
            "https://cn.bing.com/search",
            params={"q": query, "setlang": "zh-hans"},
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
            timeout=(min(3.5, timeout), timeout),
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception:
        return []

    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a[href]")
        if link is None:
            continue
        title = _clean(link.get_text(" ", strip=True), 180)
        url = urljoin("https://cn.bing.com", str(link.get("href") or ""))
        if not title or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        caption = item.select_one(".b_caption p, .b_snippet")
        snippet = _clean(caption.get_text(" ", strip=True) if caption else "", 420)
        seen.add(url)
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max(1, min(8, int(max_results))):
            break
    return hits


def format_hits(hits: list[SearchHit]) -> str:
    """把检索结果整理成给模型看的简短事实清单。"""
    rows: list[str] = []
    for index, hit in enumerate(hits, start=1):
        rows.append(
            f"[{index}] {hit.title}\n"
            f"URL: {hit.url}\n"
            f"摘要: {hit.snippet or '（结果页没有提供摘要）'}"
        )
    return "\n\n".join(rows)


def build_search_query(model: Any, text: str, *, max_chars: int = 160) -> str:
    """用 Flash 把用户原话压成一条可检索的查询。

    Flash 只负责「怎么查」，不负责「查到了什么」：网页结果仍由
    `search_web` 直接拉取。这样即使模型生成失败，也能退回原话继续检索，
    不会把模型记忆伪装成网页事实。
    """
    raw = _clean(text, 1000)
    if not raw:
        return ""
    try:
        response = model.invoke([
            {
                "role": "system",
                "content": (
                    "你只负责生成一条简短的中文网页搜索查询。"
                    "保留用户问题里的实体、地点、时间和限定词；"
                    "不要回答问题，不要编造事实，不要输出引号、列表或解释。"
                    f"只输出一行，最多 {max_chars} 个字符。"
                ),
            },
            {"role": "user", "content": raw},
        ])
        content = str(getattr(response, "content", response) or "").strip()
        line = next((part.strip() for part in content.splitlines()
                     if part.strip()), "")
        line = re.sub(r"^(搜索词|查询|关键词)\s*[:：]\s*", "", line)
        line = line.strip("\"'“”‘’ ")
        if line and len(line) <= max_chars:
            return line
    except Exception:
        pass
    return raw[:max_chars]


__all__ = ["SearchHit", "build_search_query", "format_hits", "search_web"]
