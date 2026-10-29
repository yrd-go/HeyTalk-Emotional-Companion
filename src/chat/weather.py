# -*- coding: utf-8 -*-
"""
src/chat/weather.py —— 全球实时天气（S2 升级：从模拟数据到真实数据）
======================================================================

数据源：Open-Meteo（https://open-meteo.com）
--------------------------------------------------------------------------
选它的理由（对照"不管在哪个国家都要能查实时天气"这个要求）：
    - **全球覆盖**：按经纬度查询，不是内置几个城市；自带 Geocoding API，
      所以"北京 / New York / 東京 / São Paulo"都能查到；
    - **实时 + 未来 16 天**，逐小时数据，来自各国气象局
      （含中国 CMA、美国 NOAA、日本 JMA 等 17 家模型）；
    - **不需要 API Key**，个人使用免费；
    - ⚠️ 许可是 "Free for non-commercial use"：毕业设计/自用没问题，
      如果以后商业化需要换付费版或别的数据源。

降级链（沿用项目一贯的模式）
--------------------------------------------------------------------------
    1. Open-Meteo（真实数据，需联网）
    2. 现有 MCP 模拟服务端（离线时用，会**明确标注**是模拟数据）
    3. 兜底：一句可读的说明，绝不抛异常

第 2 步复用 `src/mcp_demo/mcp_weather_server.py` —— 那个模块不删，
它现在是离线降级路径（延续"保留已存档模块"的要求）。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "WeatherResult",
    "GEOCODE_URL",
    "FORECAST_URL",
    "SOURCE_LIVE",
    "SOURCE_OFFLINE",
    "SOURCE_NONE",
    "geocode_city",
    "fetch_current_weather",
    "lookup_global_weather",
    "WMO_TEXT",
    "weather_source_available",
]

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# ---------------------------------------------------------------------------
# 地名解析的补救措施（因为 Open-Meteo/GeoNames 是**精确名称匹配**）
# ---------------------------------------------------------------------------
# 实跑踩到的：用户说「厦门」→ 0 条结果；「厦门市」→ 命中。
# 原因是 GeoNames 里这条地名的 name 字段就是"厦门市"。
# 于是"用户说了一个最自然的写法，却查不到"——这是**产品的错，不是用户的错**。
#
# 处理办法：按顺序自动多试几种写法。用户不需要知道这些。
_ZH_ADMIN_SUFFIXES = ("市", "区", "县", "州", "盟", "旗", "镇", "乡", "街道")

# 只靠这些后缀就能断定"这已经是个完整地名，不必再补市"。
# 为什么不直接用上面那整张表：「州」多数时候是**城市名的一部分**
# （广州、杭州、苏州…），拿它当终止符会让「广州」连「广州市」都不试，
# 而 GeoNames 里很可能就叫「广州市」。
# 所以：只有『市/区/县/镇/乡/街道』才终止，『州/盟/旗』只参与剥离。
_TERMINAL_SUFFIXES = ("市", "区", "县", "镇", "乡", "街道")

# 少数 GeoNames 里以"市"结尾、但去掉"市"查不到的常见城市。
# 这张表只是**兜底**：先试加"市"，命中率已经很高；表里放的是确认要额外记的。
_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "厦门": ("厦门市", "Xiamen"),
    "深圳": ("深圳市", "Shenzhen"),
    "苏州": ("苏州市", "Suzhou"),
    "青岛": ("青岛市", "Qingdao"),
    "大连": ("大连市", "Dalian"),
    "宁波": ("宁波市", "Ningbo"),
    "无锡": ("无锡市", "Wuxi"),
    "温州": ("温州市", "Wenzhou"),
    "佛山": ("佛山市", "Foshan"),
    "东莞": ("东莞市", "Dongguan"),
    "泉州": ("泉州市", "Quanzhou"),
    "珠海": ("珠海市", "Zhuhai"),
    "汕头": ("汕头市", "Shantou"),
    "洛阳": ("洛阳市", "Luoyang"),
    "唐山": ("唐山市", "Tangshan"),
    "徐州": ("徐州市", "Xuzhou"),
    "常州": ("常州市", "Changzhou"),
    "南通": ("南通市", "Nantong"),
    "烟台": ("烟台市", "Yantai"),
    "潍坊": ("潍坊市", "Weifang"),
    "绍兴": ("绍兴市", "Shaoxing"),
    "嘉兴": ("嘉兴市", "Jiaxing"),
    "台州": ("台州市", "Taizhou"),
    "中山": ("中山市", "Zhongshan"),
    "惠州": ("惠州市", "Huizhou"),
}

# 世界知名城市里，少数中文名会被国内同名小地点抢先命中。
# 这不是完整城市库，只补“原始写法明显不可信”的情况：
# 用户说「东京」时，GeoNames 的中文结果只有江苏/浙江两个无人口小地点，
# 而「東京/Tokyo」返回的是日本东京（人口 970 万、PPLC）。
_GLOBAL_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "东京": ("東京", "Tokyo"),
    "东京市": ("東京", "Tokyo"),
    "東京": ("Tokyo",),
    "纽约": ("New York City", "紐約市"),
    "纽约市": ("New York City", "紐約市"),
    "紐約": ("New York City", "紐約市"),
    "紐約市": ("New York City",),
    "New York": ("New York City",),
    "new york": ("New York City",),
}

# 用户在消歧时补充的国家/地区写法。仅在**城市名之外**出现时生效，
# 不会把 "US" 这种短代码当作任意单词的前缀。
_COUNTRY_ALIASES: dict[str, str] = {
    "美国": "US", "us": "US", "usa": "US", "united states": "US",
    "英国": "GB", "uk": "GB", "united kingdom": "GB",
    "日本": "JP", "japan": "JP",
    "法国": "FR", "france": "FR",
    "德国": "DE", "germany": "DE",
    "加拿大": "CA", "canada": "CA",
    "澳大利亚": "AU", "australia": "AU",
    "中国": "CN", "china": "CN",
}


def _country_code(value: str) -> str | None:
    return _COUNTRY_ALIASES.get(str(value or "").strip().strip(".").casefold())


def _split_place_qualifiers(name: str) -> tuple[str, str | None, str | None]:
    """把「美国纽约 / New York, USA」拆成城市与可验证的地区限定。

    为什么必须拆：Open-Meteo 的地名接口只匹配地名本身，不接受
    "城市, 国家" 这种人类常用写法。直接整串查询时，某些前缀会被
    退化成国家中心点（例如把「美国纽约」答成美国天气），这是错误答案，
    比查不到更糟。
    """
    raw = str(name or "").strip()
    if not raw:
        return "", None, None

    for sep in ("，", ","):
        if sep in raw:
            city, _, qualifier = raw.partition(sep)
            qualifier = qualifier.strip()
            code = _country_code(qualifier)
            return city.strip(), code, (None if code else qualifier)

    parts = raw.split()
    if len(parts) > 1:
        for index, part in enumerate(parts):
            code = _country_code(part)
            if code:
                city = " ".join(parts[:index] + parts[index + 1:]).strip()
                if city:
                    return city, code, None

    lowered = raw.casefold()
    for alias, code in sorted(_COUNTRY_ALIASES.items(),
                              key=lambda item: len(item[0]), reverse=True):
        # 两字母代码只用于空格/逗号分隔，避免把 "Cairo" 误拆成 CA + iro。
        if len(alias) < 3 and not any("\u4e00" <= ch <= "\u9fff" for ch in alias):
            continue
        if lowered.startswith(alias) and len(raw) > len(alias):
            city = raw[len(alias):].strip(" ,，")
            if len(city) >= 2:
                return city, code, None
        if lowered.endswith(alias) and len(raw) > len(alias):
            city = raw[: -len(alias)].strip(" ,，")
            if len(city) >= 2:
                return city, code, None
    return raw, None, None

# 进程内缓存：用户这次查过「厦门市」，下次问「厦门」直接命中，省一次往返
_GEOCODE_CACHE: dict[str, dict[str, Any]] = {}

# 城市下面的**下级地名后缀**。实跑踩到的真实案例：
#   用户说「广州天河」→ GeoNames 里没有"广州天河"这个地名 → 查不到。
#   因为"天河"是**区**，而地名库里只有"广州"。
# 人说话时经常带上区名（广州天河、北京朝阳、上海浦东），
# 所以要把这些后缀连同它前面的部分剥掉，退回到城市。
# 注意顺序：长的放前面（"新区"要在"区"之前匹配，否则"浦东新区"会剥成"浦东新"）。
_DISTRICT_SUFFIXES = (
    "经济技术开发区", "高新技术开发区", "自治县", "自治州", "特别行政区",
    "新区", "城区", "郊区", "矿区", "林区", "地区",
    "区", "县", "镇", "乡", "街道",
)


def _candidate_names(name: str) -> list[str]:
    """给一个地名，按"最可能先命中"的顺序列出要试的写法。

    顺序有讲究：**先试用户的原话**（很多地名本来就能直接命中，
    例如「北京」「上海」），再试加"市"，再去后缀/去区县名，最后查别名表。
    这样常见情况只需一次请求。

    两个真实投诉驱动的规则：
      1. 「厦门」查不到 → 自动补「市」（GeoNames 里叫「厦门市」）；
      2. 「广州天河」查不到 → 剥掉区名退回「广州」（天河是区，不是城市）。
    """
    raw = str(name or "").strip()
    if not raw:
        return []

    candidates: list[str] = [raw]
    # 先保留原话（用于收集国内同名地点），再补世界城市别名。
    candidates.extend(_GLOBAL_CITY_ALIASES.get(raw, ()))
    is_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in raw)

    if is_cjk and not raw.endswith(_TERMINAL_SUFFIXES):
        candidates.append(raw + "市")

    # 剥掉末尾的行政区划后缀（含区/县/镇…），得到"城市"这一层。
    # 要求剥完至少剩 2 个字：像「广州」这种以"州"结尾的城市名，
    # 剥成「广」是荒谬的（实跑在候选列表里看到过）。
    stripped = raw
    for suffix in _DISTRICT_SUFFIXES:
        if (stripped.endswith(suffix) and len(stripped) > len(suffix)
                and len(stripped) - len(suffix) >= 2):
            stripped = stripped[: -len(suffix)]
            break
    if stripped != raw:
        candidates.append(stripped)
        # 剥完之后再补一次"市"：广州天河 → 广州 → 广州市
        if is_cjk and not stripped.endswith(_TERMINAL_SUFFIXES):
            candidates.append(stripped + "市")

    # 城市级没命中时，还可能是"城市 + 下级地名"（广州天河 → 只留广州）
    # 做法：从右往左逐步截短，最少保留两个字。
    for cut in range(len(raw) - 1, 1, -1):
        head = raw[:cut]
        if not any("\u4e00" <= ch <= "\u9fff" for ch in head):
            break
        candidates.append(head)
        candidates.append(head + "市")

    for suffix in _ZH_ADMIN_SUFFIXES:
        # 同样要求剥完至少剩 2 个字：'广州' 以 '州' 结尾，
        # 但它不是"州 + 前缀"，剥成 '广' 纯属噪声（实跑抓到过）。
        if (raw.endswith(suffix) and len(raw) > len(suffix)
                and len(raw) - len(suffix) >= 2):
            candidates.append(raw[: -len(suffix)])
            break

    candidates.extend(_CITY_ALIASES.get(raw, ()))

    # 去重且保序
    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered

SOURCE_LIVE = "open-meteo"       # 真实数据
SOURCE_OFFLINE = "offline-mock"  # 降级：本地模拟
SOURCE_NONE = "unavailable"      # 完全拿不到

REQUEST_TIMEOUT = 8              # 秒。天气是"顺带问一句"，不能让人等太久

# WMO 天气代码 → 中文描述（Open-Meteo 用的是 WMO 4677 标准码）
WMO_TEXT: dict[int, str] = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "中等阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

# 风向（度）→ 中文方位（每 45 度一档，取 8 方位）
_COMPASS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


@dataclass(frozen=True)
class WeatherResult:
    """一次天气查询的结果。"""

    ok: bool
    text: str                       # 给用户/模型看的一句话
    source: str                     # open-meteo / offline-mock / unavailable
    city: str = ""
    country: str = ""
    temperature: float | None = None
    wind_speed: float | None = None
    weather_code: int | None = None

    @property
    def is_live(self) -> bool:
        return self.source == SOURCE_LIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "city": self.city,
            "country": self.country,
            "temperature": self.temperature,
            "wind_speed": self.wind_speed,
            "weather_code": self.weather_code,
        }


def _get_json(url: str, params: dict[str, Any]) -> Any:
    """发一次 GET 并解析 JSON。失败抛异常，由调用方决定降级。"""
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": "emotional-companion-agent/1.0"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def geocode_city(name: str, *, language: str = "zh",
                 country_code: str | None = None,
                 admin_hint: str | None = None) -> dict[str, Any] | None:
    """把城市名解析成坐标；多个同名结果时返回最可信的那个。

    不再盲取 API 的第一条：Open-Meteo 对「东京」的中文查询只会返回
    江苏/浙江两个同名小地点，而日本东京需要「東京/Tokyo」才会出现。
    这里按人口与行政级别排序，并把其余同名地点放进 alternatives，
    供上层明确告诉用户“我按哪个查的”。
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    payload = _get_json(GEOCODE_URL, {
        "name": cleaned, "count": 10, "language": language, "format": "json",
    })
    results = (payload or {}).get("results") or []
    places = []
    for item in results:
        feature = str(item.get("feature_code") or "")
        # 只接受居民点。否则查询「美国纽约」这类字符串退化成「美国」时，
        # GeoNames 会把国家/山脉/机场当成“城市”，返回一个国家中心点。
        if feature and not feature.startswith("PPL"):
            continue
        place = _place_from_result(item, cleaned)
        if place is None:
            continue
        if country_code and place.get("country_code") != country_code.upper():
            continue
        if admin_hint:
            admin = str(place.get("admin") or "").casefold()
            if admin_hint.casefold() not in admin:
                continue
        places.append(place)
    if not places:
        return None
    places.sort(key=_place_score, reverse=True)
    best = places[0]
    best["alternatives"] = _alternative_labels(best, places[1:])
    return best


def _place_from_result(item: Any, fallback_name: str) -> dict[str, Any] | None:
    """把 Open-Meteo 的一条结果转成稳定的内部结构。"""
    try:
        population = item.get("population")
        return {
            "name": str(item.get("name") or fallback_name),
            "country": str(item.get("country") or ""),
            "country_code": str(item.get("country_code") or ""),
            "admin": str(item.get("admin1") or ""),
            "latitude": float(item["latitude"]),
            "longitude": float(item["longitude"]),
            "population": int(population) if isinstance(population, (int, float)) else 0,
            "feature_code": str(item.get("feature_code") or ""),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _place_score(place: dict[str, Any]) -> float:
    """越大越可能是用户常说的那个地点（人口 + 行政级别）。"""
    population = place.get("population") or 0
    feature = str(place.get("feature_code") or "")
    score = float(population)
    if feature == "PPLC":
        score += 20_000_000
    elif feature.startswith("PPLA"):
        score += 2_000_000
    return score


def _is_confident(place: dict[str, Any]) -> bool:
    """足够可信时不再尝试别名，避免每次查询都多打几次网络请求。"""
    population = place.get("population") or 0
    feature = str(place.get("feature_code") or "")
    return (feature == "PPLC" or population >= 100_000
            or (feature.startswith("PPLA") and population >= 20_000))


def _format_place(place: dict[str, Any]) -> str:
    admin = str(place.get("admin") or "")
    suffix = f"，{admin}" if admin else ""
    return f"{place.get('name', '')}（{place.get('country', '')}{suffix}）"


def _alternative_labels(best: dict[str, Any], others: Iterable[dict[str, Any]]) -> list[str]:
    """把其他同名地点整理成简短、可读的候选，最多三条。"""
    labels: list[str] = []
    chosen = _format_place(best)
    for place in others:
        # 只把"居民点"算作同名地点：GeoNames 里还有山、河、站的同名条目，
        # 把它们列出来只会制造噪音（例如「Tōkyō Zan」是山名）。
        feature = str(place.get("feature_code") or "")
        if feature and not feature.startswith("PPL"):
            continue
        label = _format_place(place)
        if label and label != chosen and label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    return labels


def geocode_city_smart(name: str, *, language: str = "zh") -> dict[str, Any] | None:
    """按多种写法依次尝试解析地名（用户只负责说"厦门"）。

    这是给 `lookup_global_weather` 用的入口。相比单次 `geocode_city`：
      - 自动补「市」/去后缀/查别名表，解决「厦门」查不到的问题；
      - 命中后**缓存**这次用的写法，下次同一个词只发一次请求；
      - 每个候选只请求一次，最坏情况多花几百毫秒，换回来的是"能查到"。

    ★ 一个重要区分（这里修过一个会误导用户的 bug）：
        「**这个词查不到**」和「**请求失败了**」是两回事，绝不能混。
        第一版写成 `except Exception: continue`，把网络超时也当成
        "这个候选不存在"吞掉 —— 结果所有候选都因网络失败时返回 None，
        界面显示「没查到这个地方」，**把一次网络故障说成了"你写的城市不存在"**。
        正确做法：真正的网络/解析错误**向上抛**，由 lookup_global_weather
        的统一降级去说明"连不上天气服务"；只有"请求成功但没结果"才算查不到。

    返回与 `geocode_city` 相同的结构（外加它实际命中的写法 `matched_by`）。
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    key, country_code, admin_hint = _split_place_qualifiers(raw)
    if not key:
        return None
    cache_key = f"{key}|{country_code or ''}|{admin_hint or ''}"
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    last_error: BaseException | None = None
    best: dict[str, Any] | None = None
    best_candidate = ""
    original: dict[str, Any] | None = None
    for candidate in _candidate_names(key):
        try:
            geo_kwargs: dict[str, Any] = {}
            if country_code:
                geo_kwargs["country_code"] = country_code
            if admin_hint:
                geo_kwargs["admin_hint"] = admin_hint
            place = geocode_city(candidate, language=language, **geo_kwargs)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError, json.JSONDecodeError):
            # 网络/解析问题：不是"候选不对"，整条链都别指望了 —— 直接上报
            raise
        except Exception as exc:  # noqa: BLE001
            # 其它异常（例如某个候选引起的意外）：记下来，换下一个试试，
            # 但如果最后全军覆没，把**真实原因**抛出去而不是谎报"查不到"
            last_error = exc
            continue
        if not place:
            continue
        if candidate == key:
            original = place
        if best is None or _place_score(place) > _place_score(best):
            best = place
            best_candidate = candidate
        # 已经有可信结果时停止；「东京」的国内小地点没有人口，
        # 不会在这里提前截断，会继续试「東京/Tokyo」。
        if _is_confident(place):
            break
    if best is None:
        if last_error is not None:
            raise last_error
        return None
    if (original is not None and original is not best
            and original.get("country") != best.get("country")):
        alternatives = [_format_place(original)] + list(original.get("alternatives") or [])
        merged = list(best.get("alternatives") or [])
        for label in alternatives:
            if label not in merged and label != _format_place(best):
                merged.append(label)
        best["alternatives"] = merged[:3]
    # 分数接近（都没有人口/行政级别证据）时不替用户猜：
    # 交给上层直接问"你要查哪一个"，而不是把某个同名小地点当成答案。
    best["needs_disambiguation"] = (not country_code
                                    and not admin_hint
                                    and not _is_confident(best)
                                    and bool(best.get("alternatives")))
    best["matched_by"] = best_candidate
    _GEOCODE_CACHE[cache_key] = dict(best)
    return best


def fetch_current_weather(latitude: float, longitude: float, *,
                          timezone: str = "auto") -> dict[str, Any]:
    """按坐标取当前天气（实时值 + 今日最高最低）。"""
    payload = _get_json(FORECAST_URL, {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                   "weather_code,wind_speed_10m,wind_direction_10m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "timezone": timezone,
    })
    current = (payload or {}).get("current") or {}
    daily = (payload or {}).get("daily") or {}
    return {
        "temperature": current.get("temperature_2m"),
        "humidity": current.get("relative_humidity_2m"),
        "apparent": current.get("apparent_temperature"),
        "code": current.get("weather_code"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_direction": current.get("wind_direction_10m"),
        "temp_max": (daily.get("temperature_2m_max") or [None])[0],
        "temp_min": (daily.get("temperature_2m_min") or [None])[0],
    }


def _describe(city: str, country: str, data: dict[str, Any], *,
              admin: str = "",
              alternatives: Iterable[str] | None = None) -> str:
    """把原始数值拼成一句自然的中文。"""
    parts: list[str] = []
    code = data.get("code")
    if isinstance(code, (int, float)):
        parts.append(WMO_TEXT.get(int(code), f"天气代码 {int(code)}"))

    temp = data.get("temperature")
    if isinstance(temp, (int, float)):
        parts.append(f"{round(temp)}度")

    daily = []
    lo, hi = data.get("temp_min"), data.get("temp_max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        daily.append(f"{round(lo)}~{round(hi)}度")

    wind = data.get("wind_speed")
    if isinstance(wind, (int, float)):
        wd = data.get("wind_direction")
        direction = ""
        if isinstance(wd, (int, float)):
            direction = _COMPASS[int((float(wd) % 360) / 45 + 0.5) % 8]
        daily.append(f"{direction}风 {round(wind)} 公里/时")

    humidity = data.get("humidity")
    if isinstance(humidity, (int, float)):
        daily.append(f"湿度 {round(humidity)}%")

    # 必须带上 admin1（省/都道府县）：同名地点靠这一层才分得清。
    # 例如「東京（日本，东京都）」与「东京（中国，江苏省）」。
    suffix = f"，{admin}" if admin else ""
    location = f"{city}（{country}{suffix}）" if country else city
    detail = "，".join(parts)
    if daily:
        detail = f"{detail}（{'，'.join(daily)}）" if detail else "，".join(daily)
    text = f"{location}：{detail}"
    candidates = [str(item).strip() for item in (alternatives or []) if str(item).strip()]
    if candidates:
        text += (f"。数据源里还有同名地点：{'、'.join(candidates[:3])}。"
                 "我按上面这个地点查；如果你问的是另一个，告诉我国家或省份。")
    return text


def _offline_fallback(city: str) -> WeatherResult:
    """降级到本地 MCP 模拟服务端，并**明确标注**这不是真实数据。"""
    try:
        from mcp_demo.mcp_weather_server import get_current_weather
    except Exception as exc:  # noqa: BLE001
        return WeatherResult(
            ok=False,
            text=(f"现在查不了天气（联不上天气服务，本地降级也不可用："
                  f"{type(exc).__name__}）。"),
            source=SOURCE_NONE,
            city=city,
        )
    try:
        mock_text = str(get_current_weather(city))
    except Exception as exc:  # noqa: BLE001
        return WeatherResult(ok=False,
                             text=f"查询天气时出错：{type(exc).__name__}。",
                             source=SOURCE_NONE, city=city)
    return WeatherResult(
        ok=True,
        text=f"{mock_text}（注意：这是**本地模拟数据**，不是真实天气）",
        source=SOURCE_OFFLINE,
        city=city,
    )


def lookup_global_weather(city: Any, *, allow_offline: bool = True,
                          language: str = "zh",
                          offline_only: bool = False) -> WeatherResult:
    """查任意城市的真实天气；失败按降级链处理。**永不抛异常。**

    参数：
        city          城市名（中文/英文/当地语言都可以）
        allow_offline 是否允许降级到本地模拟数据（默认允许）
        offline_only  True 时**跳过联网**，直接用本地模拟数据。
                      这个开关是给测试与离线演示用的：让"离线路径"可以
                      在无网环境下被确定性地验证，而不是靠网络碰运气。
    """
    name = str(city or "").strip()
    if not name:
        return WeatherResult(ok=False,
                             text="未提供城市名：请告诉我要查哪个城市，例如「上海」。",
                             source=SOURCE_NONE)

    if offline_only:
        return _offline_fallback(name)

    try:
        place = geocode_city_smart(name, language=language)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        return _fallback_or_message(name, allow_offline, "连不上天气服务（网络问题）")
    except json.JSONDecodeError:
        return _fallback_or_message(name, allow_offline, "天气服务返回的内容无法解析")
    except Exception as exc:  # noqa: BLE001
        return _fallback_or_message(name, allow_offline, f"{type(exc).__name__}")

    if place is None:
        # 地名本身查不到：这是"输入问题"，不是"服务问题"，不必降级。
        # 措辞刻意**不提格式要求**：用户说"厦门"是最自然的写法，
        # 让用户去迁就查询引擎的匹配规则是产品设计失败（改前就是这么栽的）。
        return WeatherResult(
            ok=False,
            text=(f"没查到「{name}」这个地方。"
                  f"如果是小地方或重名，可以带上省份或国家再说一次。"),
            source=SOURCE_LIVE,
            city=name,
        )

    if place.get("needs_disambiguation"):
        labels = [_format_place(place)] + list(place.get("alternatives") or [])
        return WeatherResult(
            ok=False,
            text=(f"「{name}」有多个同名地点：{'、'.join(labels[:4])}。"
                  "你要查哪一个？告诉我国家或省份，我按你选的查。"),
            source=SOURCE_LIVE,
            city=name,
        )

    try:
        data = fetch_current_weather(place["latitude"], place["longitude"])
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        return _fallback_or_message(name, allow_offline, "连不上天气服务（网络问题）")
    except Exception as exc:  # noqa: BLE001
        return _fallback_or_message(name, allow_offline, f"{type(exc).__name__}")

    return WeatherResult(
        ok=True,
        text=_describe(place["name"], place["country"], data,
                       admin=str(place.get("admin") or ""),
                       alternatives=place.get("alternatives")),
        source=SOURCE_LIVE,
        city=place["name"],
        country=place["country"],
        temperature=(data.get("temperature")
                     if isinstance(data.get("temperature"), (int, float)) else None),
        wind_speed=(data.get("wind_speed")
                    if isinstance(data.get("wind_speed"), (int, float)) else None),
        weather_code=(int(data["code"])
                      if isinstance(data.get("code"), (int, float)) else None),
    )


def _fallback_or_message(city: str, allow_offline: bool, reason: str) -> WeatherResult:
    """服务不可用时的统一处理：能降级就降级，不能就明确说明。"""
    if allow_offline:
        result = _offline_fallback(city)
        if result.ok:
            return WeatherResult(
                ok=True,
                text=f"{result.text}（原因：{reason}）",
                source=result.source,
                city=result.city,
            )
    return WeatherResult(
        ok=False,
        text=f"暂时查不到「{city}」的天气（{reason}）。过一会儿再试，或者自己看一眼预报。",
        source=SOURCE_NONE,
        city=city,
    )


def weather_source_available() -> bool:
    """探测真实天气源是否可用（只做一次轻量请求）。

    仅用于界面提示与自检；**判断结果不缓存**——网络状况随时会变。
    """
    try:
        return geocode_city("Beijing") is not None
    except Exception:  # noqa: BLE001
        return False
