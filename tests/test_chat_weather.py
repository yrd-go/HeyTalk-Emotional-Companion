# -*- coding: utf-8 -*-
"""
tests/test_chat_weather.py —— 全球真实天气（S2 升级，纯标准库）
==================================================================

覆盖两层，**分开是刻意的**：

    离线层（永远可测）：降级链、标注"模拟数据"、异常永不外抛、
                       WMO 天气代码映射、方位换算、坐标解析容错。

    联网层（无网时 SKIP）：真的能查到全球任意城市的实时天气。
        需要网络的用例**必须能优雅跳过**，而不是伪装成通过——
        这是本项目一贯的纪律（见 tests/README.md 的"受限说明"）。

    python tests/run_all.py
"""
from __future__ import annotations

import json
import socket
import sys
import time
import unittest
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import weather as w  # noqa: E402

NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, OSError)


class TestCityNameFallback(unittest.TestCase):
    """地名解析的补救链：用户说「厦门」也必须查到。

    ★ 这是修复一个真实投诉：用户输入「厦门」→ Open-Meteo 返回 0 条，
    界面却让用户"试试更常见的写法/带上国家"——**把查询引擎的匹配规则
    丢给用户去学**，体验很差。根因是 GeoNames 里这条地名就叫「厦门市」。

    这里全部用替身，不碰网络（确定性）。
    """

    def setUp(self) -> None:
        self._orig = w.geocode_city
        w._GEOCODE_CACHE.clear()          # 缓存会跨用例污染，必须清

    def tearDown(self) -> None:
        w.geocode_city = self._orig
        w._GEOCODE_CACHE.clear()

    def test_district_name_falls_back_to_city(self):
        """★「广州天河」必须回退到「广州」。

        真实投诉：用户问"广州天河现在是什么天气"，查不到，界面还让用户
        "提供更详细的地点信息"。**天河是区，不是城市** —— GeoNames 里只有
        「广州」。这个回退是让"人怎么说就怎么查"的关键。
        """
        candidates = w._candidate_names("广州天河")
        self.assertIn("广州", candidates,
                      f"没有回退到城市：{candidates}")
        self.assertLess(candidates.index("广州"), len(candidates),
                        "回退项应出现在候选列表里")

    def test_other_district_forms_also_fall_back(self):
        for raw, city in (("北京朝阳", "北京"),
                          ("深圳南山", "深圳"),
                          ("上海浦东新区", "上海"),
                          ("杭州西湖区", "杭州")):
            self.assertIn(city, w._candidate_names(raw),
                          f"{raw} 没有回退到 {city}")

    def test_city_ending_with_zhou_is_not_mangled(self):
        """★「广州」不能被剥成「广」。

        踩过的坑：『州』既是行政区后缀，又常是**城市名的一部分**
        （广州/杭州/苏州）。第一版把它当后缀一路剥，候选里出现了荒谬的
        『广』，而且因为提前判定"已经是完整地名"，连『广州市』都不试了。
        规则：剥离后至少留 2 个字；并区分"终止型后缀"与"可剥离后缀"。
        """
        candidates = w._candidate_names("广州")
        self.assertNotIn("广", candidates, f"过度剥离：{candidates}")
        self.assertIn("广州市", candidates,
                      f"「广州」应当也试「广州市」：{candidates}")

    def test_smart_geocode_uses_city_when_district_fails(self):
        """端到端：区名查不到时，最终命中的应该是城市。"""
        tried: list[str] = []

        def fake(name, *, language="zh"):
            tried.append(name)
            if name == "广州":
                return {"name": "广州", "country": "中国", "admin": "广东省",
                        "latitude": 23.11667, "longitude": 113.25}
            return None

        w.geocode_city = fake
        place = w.geocode_city_smart("广州天河")
        self.assertIsNotNone(place, "区名回退到城市后仍没命中")
        self.assertEqual(place["name"], "广州")
        self.assertIn("广州", tried)

    def test_network_error_is_not_reported_as_place_not_found(self):
        """★ 网络失败绝不能被说成"这个地方查不到"。

        修过的真 bug：`geocode_city_smart` 原本对每个候选写
        `except Exception: continue`，把网络超时也当成"这个候选不存在"吞掉。
        结果所有候选都因网络失败时返回 None，界面显示「没查到这个地方」——
        **把一次网络故障说成了"你写的城市不存在"**，用户会以为是自己写错了。

        正确行为：网络类异常必须**向上抛**，由 lookup_global_weather 统一
        说明"连不上天气服务（网络问题）"。
        """
        def broken(name, *, language="zh"):
            raise urllib.error.URLError("网络断了")

        w.geocode_city = broken
        with self.assertRaises(urllib.error.URLError):
            w.geocode_city_smart("東京")

        # 端到端：错误信息应当是"连不上"，而不是"没查到"
        result = w.lookup_global_weather("東京", allow_offline=False)
        self.assertFalse(result.ok)
        self.assertIn("连不上", result.text,
                      f"网络故障被误报成别的了：{result.text}")
        self.assertNotIn("没查到", result.text,
                         f"网络故障被说成了「这个地名不存在」：{result.text}")

    def test_candidates_try_raw_name_first(self):
        """先试用户原话 —— 多数地名（北京/上海）本来就能直接命中。"""
        candidates = w._candidate_names("北京")
        self.assertEqual(candidates[0], "北京")

    def test_candidates_add_admin_suffix_for_cjk(self):
        """中文地名要自动补「市」——这是修掉「厦门」查不到的关键一步。"""
        self.assertIn("厦门市", w._candidate_names("厦门"))

    def test_candidates_strip_suffix_and_use_aliases(self):
        """带后缀的要能去掉后缀；别名表里的要能补出来。"""
        self.assertIn("厦门", w._candidate_names("厦门市"))
        self.assertIn("Xiamen", w._candidate_names("厦门"))

    def test_english_name_is_not_given_chinese_suffix(self):
        candidates = w._candidate_names("Cambridge")
        self.assertNotIn("Cambridge市", candidates)

    def test_smart_geocode_retries_until_hit(self):
        """第一个写法查不到时，必须自动接着试下一个（而不是直接放弃）。"""
        tried: list[str] = []

        def fake(name, *, language="zh"):
            tried.append(name)
            if name == "厦门":
                return None                       # 模拟 GeoNames 查不到
            return {"name": "厦门市", "country": "中国", "admin": "福建省",
                    "latitude": 24.47979, "longitude": 118.08187}

        w.geocode_city = fake
        place = w.geocode_city_smart("厦门")
        self.assertIsNotNone(place, "补「市」之后仍然没命中")
        self.assertEqual(tried[:2], ["厦门", "厦门市"], f"尝试顺序不对：{tried}")
        self.assertEqual(place["matched_by"], "厦门市")

    def test_smart_geocode_caches_hit(self):
        """命中过的写法要缓存：同一个词第二次问不再发请求。"""
        calls: list[str] = []

        def fake(name, *, language="zh"):
            calls.append(name)
            return {"name": name, "country": "中国", "admin": "",
                    "latitude": 1.0, "longitude": 2.0}

        w.geocode_city = fake
        w.geocode_city_smart("上海")
        first_round = len(calls)
        w.geocode_city_smart("上海")
        self.assertEqual(len(calls), first_round, "缓存没生效，重复请求了")

    def test_smart_geocode_returns_none_when_all_fail(self):
        w.geocode_city = lambda name, *, language="zh": None
        self.assertIsNone(w.geocode_city_smart("不存在的地方xyz"))

    def test_not_found_message_does_not_lecture_about_format(self):
        """查不到时的措辞**不许**教用户改格式。

        这一条是防回归：改前的文案是"可以试试更常见的写法，或者带上
        国家/省份，例如「Cambridge, UK」"——把内部匹配规则推给用户。
        """
        result = w.lookup_global_weather("不存在的地方xyz", allow_offline=False)
        self.assertFalse(result.ok)
        for lecturing in ("Cambridge", "格式", "写法", "请用英文"):
            self.assertNotIn(lecturing, result.text,
                             f"查不到的提示里还在教用户改格式：{result.text}")


    def test_tokyo_prefers_japan_and_keeps_chinese_alternatives(self):
        """★ 用户说「东京」，必须查到日本东京，同时保留国内同名地点。

        真实投诉：Open-Meteo 对中文「东京」只返回江苏/浙江两个无人口小地点，
        模型据此回答"这是中国某个东京"，还把 DSML 工具调用源码吐给用户。
        位置身份必须由地理编码器的人口/行政级别证据决定。
        """
        def fake(name, *, language="zh"):
            if name == "东京":
                return {"name": "东京", "country": "中国", "admin": "江苏省",
                        "latitude": 32.2, "longitude": 119.2,
                        "population": 0, "feature_code": "PPL",
                        "alternatives": ["东京（中国，浙江省）"]}
            if name in ("東京", "Tokyo"):
                return {"name": "東京", "country": "日本", "admin": "东京都",
                        "latitude": 35.68, "longitude": 139.69,
                        "population": 9733276, "feature_code": "PPLC",
                        "alternatives": []}
            return None

        w.geocode_city = fake
        place = w.geocode_city_smart("东京")
        self.assertEqual(place["country"], "日本")
        self.assertEqual(place["matched_by"], "東京")
        self.assertTrue(any("中国" in alt for alt in place["alternatives"]),
                        f"中国同名地点被丢了：{place.get('alternatives')}")
        self.assertFalse(place["needs_disambiguation"],
                         "首都级结果不该再追问")

    def test_tokyo_reply_names_admin_and_discloses_namesakes(self):
        """回复必须带 admin1，并主动说明同名地点 —— 否则用户分不清查的是哪个。"""
        def fake(name, *, language="zh"):
            if name == "东京":
                return {"name": "东京", "country": "中国", "admin": "江苏省",
                        "latitude": 32.2, "longitude": 119.2,
                        "population": 0, "feature_code": "PPL",
                        "alternatives": ["东京（中国，浙江省）"]}
            return {"name": "東京", "country": "日本", "admin": "东京都",
                    "latitude": 35.68, "longitude": 139.69,
                    "population": 9733276, "feature_code": "PPLC",
                    "alternatives": []}

        original_fetch = w.fetch_current_weather
        w.geocode_city = fake
        w.fetch_current_weather = lambda lat, lon: {
            "code": 0, "temperature": 18, "temp_min": 12, "temp_max": 21,
        }
        try:
            result = w.lookup_global_weather("东京", allow_offline=False)
        finally:
            w.fetch_current_weather = original_fetch
        self.assertTrue(result.ok, result.text)
        self.assertIn("東京（日本，东京都）", result.text)
        self.assertIn("同名地点", result.text)
        self.assertIn("中国", result.text)

    def test_close_scores_ask_instead_of_guessing(self):
        """★ 分数接近时不许替用户猜：直接问"你要查哪一个"。"""
        place = {"name": "同名镇", "country": "中国", "admin": "甲省",
                 "latitude": 1.0, "longitude": 2.0, "population": 0,
                 "feature_code": "PPL",
                 "alternatives": ["同名镇（中国，乙省）"]}
        w.geocode_city = lambda name, *, language="zh": dict(place)
        result = w.lookup_global_weather("同名镇", allow_offline=False)
        self.assertFalse(result.ok)
        self.assertIn("多个同名地点", result.text)
        self.assertIn("你要查哪一个", result.text)


class TestOfflinePath(unittest.TestCase):
    """离线层：无网也必须可用。"""

    def test_offline_only_never_touches_network(self):
        """offline_only=True 时不该发任何请求。

        用"请求函数被调用就爆炸"的替身来证明——只断言返回结果证明不了这一点。
        """
        calls: list[str] = []
        original = w._get_json

        def exploding(url, params):  # pragma: no cover - 不该被调用
            calls.append(url)
            raise AssertionError("offline_only 不该联网")

        w._get_json = exploding
        try:
            result = w.lookup_global_weather("上海", offline_only=True)
        finally:
            w._get_json = original
        self.assertEqual(calls, [], "offline_only 竟然联网了")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, w.SOURCE_OFFLINE)

    def test_offline_result_is_labeled_mock(self):
        result = w.lookup_global_weather("北京", offline_only=True)
        self.assertIn("模拟", result.text)
        self.assertFalse(result.is_live)

    def test_empty_city_asks_for_one(self):
        result = w.lookup_global_weather("", offline_only=True)
        self.assertFalse(result.ok)
        self.assertIn("城市", result.text)

    def test_never_raises_on_odd_input(self):
        for value in (None, "", 123, "🙂", ["列表"], {"a": 1}, "a" * 400):
            result = w.lookup_global_weather(value, offline_only=True)  # type: ignore[arg-type]
            self.assertIsInstance(result.text, str)
            self.assertTrue(result.text.strip())

    def test_disabling_offline_gives_clear_message(self):
        """不允许降级且网络不可用时，必须明确说明，而不是沉默或编造。"""
        original = w._get_json

        def failing(url, params):
            raise urllib.error.URLError("network down")

        w._get_json = failing
        try:
            result = w.lookup_global_weather("上海", allow_offline=False)
        finally:
            w._get_json = original
        self.assertFalse(result.ok)
        self.assertEqual(result.source, w.SOURCE_NONE)
        self.assertIn("上海", result.text)


class TestWmoMapping(unittest.TestCase):
    """WMO 天气代码 → 中文。"""

    def test_common_codes_are_mapped(self):
        for code in (0, 1, 2, 3, 45, 61, 63, 65, 71, 80, 95, 99):
            self.assertIn(code, w.WMO_TEXT, f"缺少天气代码 {code}")

    def test_unknown_code_does_not_crash(self):
        text = w._describe("测试城", "测试国", {"code": 12345, "temperature": 20})
        self.assertIn("测试城", text)
        self.assertIn("20度", text)

    def test_description_includes_location_and_country(self):
        text = w._describe("Tokyo", "日本", {"code": 0, "temperature": 25,
                                            "wind_speed": 10, "wind_direction": 90,
                                            "humidity": 60})
        self.assertIn("Tokyo", text)
        self.assertIn("日本", text)
        self.assertIn("晴", text)
        self.assertIn("25度", text)
        self.assertIn("东风", text)

    def test_description_tolerates_missing_fields(self):
        text = w._describe("某地", "", {})
        self.assertIn("某地", text)


class TestResultShape(unittest.TestCase):
    def test_to_dict_is_serializable(self):
        result = w.lookup_global_weather("上海", offline_only=True)
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("source", payload)

    def test_sources_are_distinct(self):
        self.assertEqual(len({w.SOURCE_LIVE, w.SOURCE_OFFLINE, w.SOURCE_NONE}), 3)


class TestLiveNetwork(unittest.TestCase):
    """联网层：无网时 SKIP（不伪装成通过）。

    ★ 为什么这里要**重试**（这是修掉"随机红"的关键）：
      这些用例直连 Open-Meteo。而 `_require_network()` 只在**开头**探一次，
      之后的真实查询仍会被瞬时抖动打到 —— 于是一次网络抖动就报红。
      而"网抖了一下"和"接口真的挂了"是**两件不同的事**，都报红的结果是
      **噪声淹没信号**：主人看到红也不知道该不该管（他明确说过讨厌这种
      "随机红"）。

      所以判据改成：**连续 LIVE_ATTEMPTS 次都失败才算失败**。
      真的挂了仍然会红 —— 我们只是不再因为一次抖动就红。

    ⚠️ 为什么不干脆放宽断言或删掉它：
      查不到真实天气是**真的坏了** —— 主人的核心诉求就是
      "不管在美国、日本还是别的国家都要能查"。为了让测试变绿而放宽它，
      等于把真实故障藏起来。
    """

    LIVE_ATTEMPTS = 3

    def _require_network(self) -> None:
        try:
            if not w.weather_source_available():
                self.skipTest("天气服务当前不可达（无网或被拦截），跳过联网用例")
        except NETWORK_ERRORS as exc:
            self.skipTest(f"天气服务当前不可达（{type(exc).__name__}），跳过联网用例")

    def _live_lookup(self, city: str):
        """查一次真实天气；瞬时抖动会退避重试。返回最后一次的结果。

        调用方仍然要对返回值做断言 —— 重试只是消除抖动，
        **不是**把失败变成通过。
        """
        last = None
        for attempt in range(1, self.LIVE_ATTEMPTS + 1):
            try:
                result = w.lookup_global_weather(city, allow_offline=False)
            except NETWORK_ERRORS:
                result = None
            if result is not None and result.ok and result.source == w.SOURCE_LIVE:
                if attempt > 1:
                    print(f"  （{city} 第 {attempt} 次才成功 —— 说明刚才网络抖了一下）")
                return result
            last = result
            if attempt < self.LIVE_ATTEMPTS:
                time.sleep(1.5 * attempt)      # 退避：给网络一点恢复时间
        return last

    def test_china_city_live(self):
        self._require_network()
        result = self._live_lookup("上海")
        self.assertIsNotNone(result, "连续多次都没能发起查询（网络问题）")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.source, w.SOURCE_LIVE)
        self.assertIsNotNone(result.temperature)
        self.assertIn("上海", result.text)

    def test_global_cities_are_supported(self):
        """核心诉求：不管在美国、日本还是别的国家，都要能查。"""
        self._require_network()
        for city in ("New York", "東京", "London"):
            result = self._live_lookup(city)
            self.assertIsNotNone(result, f"{city}：连续多次都没能发起查询（网络问题）")
            self.assertTrue(result.ok, f"{city} 查询失败：{result.text}")
            self.assertEqual(result.source, w.SOURCE_LIVE,
                             f"{city} 没走真实数据源")

    def test_unknown_place_is_explained(self):
        self._require_network()
        result = w.lookup_global_weather("zzzznotaplace9999", allow_offline=False)
        self.assertFalse(result.ok)
        self.assertIn("没查到", result.text)


if __name__ == "__main__":
    unittest.main()
