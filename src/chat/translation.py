# -*- coding: utf-8 -*-
"""
src/chat/translation.py —— 多引擎翻译（S2 升级：从单一免费接口到多引擎）
================================================================

为什么是"多引擎 + 降级"而不是选一个最好的
--------------------------------------------------------------------------
因为"覆盖全世界语言"和"不花钱、不注册"是两个互相冲突的目标：
    - 覆盖 130+ 语言：只有 Google Cloud Translation 这类商业云能做，需 Key；
    - 不注册、零成本：MyMemory 免费，但语言覆盖面窄、质量不稳。

所以这里沿用项目一贯的模式（智谱↔Ollama、向量↔关键词），做成一条降级链：

    Google Cloud（若配了 GOOGLE_TRANSLATE_API_KEY）
        └─ 不可用 → MyMemory（免费、免注册）
              └─ 不可用 → 明确说"这个语种暂时翻不了"，绝不编答案

好处：**没有 Key 也完整可用**；哪天配上 Key，填一行 .env 就自动升级，
代码一行不用改。

与安全红线的接线
--------------------------------------------------------------------------
翻译会把用户原话发到**第三方服务器**。所以：
    1. 只在用户**明确要求**翻译时才调用（绝不后台自动翻译对话）；
    2. 高危内容（HIGH）永远不经过翻译 —— 自伤表述不发往任何外部服务；
    3. 被翻译的原文只进缓存用于省钱，**不落盘**、不写日志。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from chat import languages

__all__ = [
    "ENGINES",
    "ENGINE_ORDER",
    "TranslationResult",
    "available_engines",
    "translate",
    "detect_language",
    "resolve_language",
    "describe_coverage",
    "clear_cache",
    "GOOGLE_KEY_ENV",
    "MYMEMORY_URL",
    "GOOGLE_URL",
]

GOOGLE_KEY_ENV = "GOOGLE_TRANSLATE_API_KEY"
# 用 v2（Basic）而不是 v3（Advanced）：v2 支持 API Key 鉴权，
# 不需要 service account 与 IAM，对"本地自用"这个场景成本最低。
# 代价：v3 才有词表/批量/文档翻译，我们都不需要。
GOOGLE_URL = "https://translation.googleapis.com/language/translate/v2"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"

REQUEST_TIMEOUT = 12
MAX_QUERY_CHARS = 4000          # 单次翻译上限（超出明确拒绝，不截断）

# 缓存：同样的句子反复翻是浪费（Google 按字符计费）也是最容易被投诉的点
_CACHE_LIMIT = 512
_CACHE: "OrderedDict[tuple[str, str, str], str]" = OrderedDict()


@dataclass(frozen=True)
class TranslationResult:
    """一次翻译的结果。"""

    ok: bool
    text: str                    # 给用户看的一句话（含译文或明确原因）
    translated: str = ""         # 纯译文（成功时）
    source_lang: str = ""
    target_lang: str = ""
    engine: str = ""
    cached: bool = False
    error: str = ""
    # 机器可读的结果类型：ok / empty / too-long / unsupported-language / engine-error
    # 为什么要有它：把“不支持这门语言”和“网络/额度失败”分开，
    # 上层才能选择不同的降级话术（而不是一律拒答）。
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "cached": self.cached,
            "error": self.error,
            "status": self.status,
        }


# ===========================================================================
# 引擎可用性
# ===========================================================================
@dataclass(frozen=True)
class EngineInfo:
    engine_id: str
    label: str
    requires_key: bool
    coverage: str
    note: str = ""


ENGINES: dict[str, EngineInfo] = {
    "google": EngineInfo(
        engine_id="google",
        label="Google 翻译（覆盖 130+ 语言）",
        requires_key=True,
        coverage=f"{languages.language_count()}+",
        note=("需要 GOOGLE_TRANSLATE_API_KEY；免费额度为每月前 50 万字符，"
              "超额约 $20/百万字符"),
    ),
    "mymemory": EngineInfo(
        engine_id="mymemory",
        label="MyMemory（免费、免注册）",
        requires_key=False,
        coverage="常用语言",
        note=("匿名每天 5000 字符，带邮箱 50000 字符；"
              "质量与覆盖面不如商业云翻译"),
    ),
}

# 降级顺序：高覆盖优先，免费兜底
ENGINE_ORDER: tuple[str, ...] = ("google", "mymemory")


def _google_key() -> str:
    return os.environ.get(GOOGLE_KEY_ENV, "").strip()


def available_engines() -> dict[str, bool]:
    """探测各引擎是否具备使用条件（Google 只看有没有 Key，不发请求）。"""
    return {
        "google": bool(_google_key()),
        "mymemory": True,
    }


def describe_coverage() -> str:
    """一句话说明当前实际能覆盖多少语言（给界面用）。"""
    if _google_key():
        return (f"当前使用 Google 翻译，可翻译 {languages.language_count()}+ 种语言。")
    return (f"当前使用 MyMemory（免费）。注册表收录 {languages.language_count()} 种语言，"
            f"但免费引擎只覆盖其中常用的一部分；"
            f"配置 {GOOGLE_KEY_ENV} 可获得完整覆盖。")


# ===========================================================================
# 语言解析
# ===========================================================================
def resolve_language(name: Any) -> str | None:
    """把任意语言写法解析成代码；不认识返回 None（不猜）。"""
    return languages.resolve(name)


def detect_language(text: str) -> str:
    """判断源语言（按书写系统，比原来"含中文就 zh 否则 en"准确得多）。"""
    return languages.guess_source_language(text)


# ===========================================================================
# 缓存
# ===========================================================================
def clear_cache() -> int:
    count = len(_CACHE)
    _CACHE.clear()
    return count


def _cache_get(key: tuple[str, str, str]) -> str | None:
    value = _CACHE.get(key)
    if value is not None:
        _CACHE.move_to_end(key)
    return value


def _cache_put(key: tuple[str, str, str], value: str) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)


# ===========================================================================
# 各引擎实现
#
# 约定：引擎函数返回 (译文, 错误说明)。
#       译文非空即成功；否则用错误说明走降级。
#       缓存命中也算成功，并在 result 上标出来。
# ===========================================================================
def _call_google(text: str, source: str, target: str) -> tuple[str, str]:
    key = _google_key()
    if not key:
        return "", "no-key"

    body = urllib.parse.urlencode({
        "q": text, "target": target, "source": source,
        "format": "text", "key": key,
    }).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        data = (payload.get("data") or {}).get("translations") or []
        translated = (data[0] or {}).get("translatedText") if data else ""
        if translated:
            return str(translated), ""
        return "", "empty-response"
    except urllib.error.HTTPError as exc:
        # 401/403 通常是 Key 无效或额度用尽 —— 都该降级
        return "", f"http-{exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return "", f"network:{type(exc).__name__}"
    except json.JSONDecodeError:
        return "", "bad-json"
    except Exception as exc:  # noqa: BLE001
        return "", type(exc).__name__


def _call_mymemory(text: str, source: str, target: str) -> tuple[str, str]:
    params = {"q": text, "langpair": f"{source}|{target}"}
    # MyMemory 允许带邮箱把额度从 5000 提到 50000 字符/天（官方文档）
    contact = os.environ.get("MYMEMORY_EMAIL", "").strip()
    if contact:
        params["de"] = contact

    url = f"{MYMEMORY_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        translated = (payload.get("responseData") or {}).get("translatedText")
        if translated:
            return str(translated), ""
        return "", "empty-response"
    except urllib.error.HTTPError as exc:
        # 429 = 当天额度用完了（官方限制），这是最常见的情况
        return "", f"http-{exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return "", f"network:{type(exc).__name__}"
    except json.JSONDecodeError:
        return "", "bad-json"
    except Exception as exc:  # noqa: BLE001
        return "", type(exc).__name__


_ENGINE_CALLS: dict[str, Callable[[str, str, str], tuple[str, str]]] = {
    "google": _call_google,
    "mymemory": _call_mymemory,
}

# 引擎失败原因 -> 给人看的话（必须能指导用户下一步怎么做）
_FAILURE_HINTS: dict[str, str] = {
    "no-key": f"没有配置 {GOOGLE_KEY_ENV}",
    "http-401": "翻译服务的 Key 无效",
    "http-403": "翻译服务拒绝了这个请求（Key 权限或额度问题）",
    "http-429": "免费翻译额度用完了（每天 5 万字符），明天会恢复",
    "network:URLError": "连不上翻译服务（检查网络）",
    "network:timeout": "翻译请求超时",
    "empty-response": "翻译服务没有返回内容",
    "bad-json": "翻译服务返回的内容无法解析",
}


def _hint_for(error: str) -> str:
    if error in _FAILURE_HINTS:
        return _FAILURE_HINTS[error]
    if error.startswith("network:"):
        return "网络异常"
    return f"翻译引擎出错（{error}）" if error else "未知原因"


# ===========================================================================
# 对外主函数
# ===========================================================================
def translate(text: Any, target_lang: Any = None, *,
              source_lang: Any = None,
              engines: Sequence[str] | None = None) -> TranslationResult:
    """把 text 翻译成 target_lang。绝不抛异常。

    - 未指定目标语言时，自动反向（中文↔英文；识别不出时用英文）；
    - 未指定源语言时按书写系统自动判断；
    - 引擎顺序默认 ENGINE_ORDER，可传入自定义顺序（便于测试与"强制某引擎"）。
    """
    body = str(text or "").strip()
    if not body:
        return TranslationResult(
            False, "没有需要翻译的内容：请把要翻译的句子告诉我。", status="empty")
    if len(body) > MAX_QUERY_CHARS:
        return TranslationResult(
            False,
            f"这段太长了（{len(body)} 字，上限 {MAX_QUERY_CHARS} 字）。"
            f"可以分几次发给我。",
            status="too-long")

    # ---- 目标语言 ----
    target = resolve_language(target_lang) if target_lang else None
    if target_lang and target is None:
        return TranslationResult(
            False,
            f"我不认识目标语言「{target_lang}」。"
            f"可以直接写语言代码（如 ja / fr / ar / mai），"
            f"或常见语言名（日语 / 法语 / 阿拉伯语）。"
            f"注册表收录了 {languages.language_count()} 种语言。",
            status="unsupported-language")

    # ---- 源语言 ----
    source = resolve_language(source_lang) if source_lang else None
    if source is None:
        source = detect_language(body)
    if target is None:
        target = "en" if source == "zh" else "zh"

    if source == target:
        return TranslationResult(
            True, f"原文已经是{_lang_name(source)}了，不需要翻译：{body}",
            translated=body, source_lang=source, target_lang=target,
            engine="none", status="ok")

    cache_key = (body, source, target)
    cached = _cache_get(cache_key)
    if cached is not None:
        return TranslationResult(
            True, f"{body} → {cached}", translated=cached,
            source_lang=source, target_lang=target, engine="cache", cached=True,
            status="ok")

    order = tuple(engines) if engines else ENGINE_ORDER
    failures: list[str] = []
    for engine_id in order:
        caller = _ENGINE_CALLS.get(engine_id)
        if caller is None:
            continue
        translated, error = caller(body, source, target)
        if translated:
            _cache_put(cache_key, translated)
            return TranslationResult(
                True, f"{body} → {translated}", translated=translated,
                source_lang=source, target_lang=target, engine=engine_id,
                status="ok")
        if error and error != "no-key":
            # error 里同时带上**原始错误码**与**可读解释**：
            # 前者用于排查（日志/界面细节），后者用于告诉用户怎么办。
            failures.append(f"{engine_id}:{error}（{_hint_for(error)}）")

    # ---- 全都失败：明确说明，绝不编答案 ----
    detail = "；".join(failures) if failures else "没有可用的翻译引擎"
    extra = ("" if _google_key()
             else f"（配置 {GOOGLE_KEY_ENV} 可获得更完整的语言覆盖）")
    return TranslationResult(
        False,
        f"这次没能翻译成功：{detail}{extra}",
        source_lang=source, target_lang=target, error=detail,
        status="engine-error")


def _lang_name(code: str) -> str:
    return languages.name_of(code) or code
