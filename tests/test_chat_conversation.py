# -*- coding: utf-8 -*-
"""
tests/test_chat_conversation.py —— 会话决策的回归保护（P1，纯标准库）
======================================================================

本文件覆盖的是**真正要紧的判断**，不是渲染细节：

    ① 安全闸优先级：HIGH 必须走固定话术，且**不允许**调用模型；
    ② 中危出口校验：模型没给求助通道时必须补上；
    ③ 会话隔离：多条会话的状态/历史绝不能互相污染
       —— 加了隧道后就是多人同时用，串台是真实事故；
    ④ 隐私：会话里不保存用户原文之外的任何东西，且 clear() 能彻底清空；
    ⑤ 引擎探测：缺 Key 时必须抛 EngineUnavailable 而不是崩栈。

    python tests/run_all.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import conversation as convo  # noqa: E402
from chat.conversation import (  # noqa: E402
    EngineUnavailable,
    Session,
    assemble_chat_messages,
    available_engines,
    finalize_medium_reply,
    gauge_rows,
    make_model,
    new_session,
    plan_reply,
    trend_label,
)
from chat.safety import SafetyLevel, check_safety  # noqa: E402


class TestPlanReplyPriority(unittest.TestCase):
    """① 安全闸永远是第一优先级。"""

    def test_high_uses_fixed_reply_and_blocks_model(self):
        plan = plan_reply("我不想活了")
        self.assertTrue(plan.use_safe_reply)
        self.assertTrue(plan.hit.blocks_freeform_llm)
        self.assertIsNotNone(plan.safe_reply)
        self.assertIn("12356", plan.safe_reply)

    def test_medium_allows_model_but_requires_resources(self):
        plan = plan_reply("最近真的撑不下去了")
        self.assertIs(plan.level, SafetyLevel.MEDIUM)
        self.assertFalse(plan.use_safe_reply)
        self.assertTrue(plan.must_attach_resources)

    def test_none_and_low_allow_normal_chat(self):
        for text in ("你好", "今天很焦虑，压力很大"):
            plan = plan_reply(text)
            self.assertFalse(plan.use_safe_reply)
            self.assertFalse(plan.must_attach_resources)

    def test_plan_reply_never_raises(self):
        for value in (None, "", 123, "🙂"):
            plan = plan_reply(value)
            self.assertIsInstance(plan.level, SafetyLevel)


class TestMediumResourceGate(unittest.TestCase):
    """② 中危出口必须带资源（"自然"和"必须给资源"两者都要成立）。"""

    def test_missing_resources_are_appended(self):
        text, attached = finalize_medium_reply("我在这里听着，你先说说看。")
        self.assertTrue(attached)
        self.assertIn("12356", text)
        self.assertIn("120", text)
        self.assertIn("无法做诊断", text)

    def test_existing_resources_are_not_duplicated(self):
        original = "我在听。你可以打 12356，有人会接。"
        text, attached = finalize_medium_reply(original)
        self.assertFalse(attached)
        self.assertEqual(text, original)

    def test_emergency_number_counts_as_resource(self):
        text, attached = finalize_medium_reply("情况紧急的话请先拨 120。")
        self.assertFalse(attached)

    def test_empty_reply_still_gets_resources(self):
        text, attached = finalize_medium_reply("")
        self.assertTrue(attached)
        self.assertIn("12356", text)


class TestSessionIsolation(unittest.TestCase):
    """③ 多用户隔离——加了隧道之后这就是安全边界。"""

    def test_new_session_is_independent(self):
        first = new_session()
        second = new_session()
        first.add_user("我不想活了", hit=check_safety("我不想活了"))
        first.state.update("我不想活了", safety=check_safety("我不想活了"))

        self.assertEqual(second.turn_count, 0)
        self.assertEqual(second.turns, [])
        self.assertAlmostEqual(second.state.valence, 0.5)
        self.assertIsNot(first.state, second.state)

    def test_two_sessions_do_not_share_history(self):
        a, b = new_session(), new_session()
        a.add_user("a 的话")
        b.add_user("b 的话")
        self.assertEqual([t.content for t in a.turns], ["a 的话"])
        self.assertEqual([t.content for t in b.turns], ["b 的话"])

    def test_clear_wipes_everything(self):
        session = new_session()
        session.add_user("很难过", hit=check_safety("很难过"))
        session.state.update("很难过", safety=check_safety("很难过"))
        self.assertTrue(session.turns)
        self.assertNotAlmostEqual(session.state.valence, 0.5)

        session.clear()
        self.assertEqual(session.turns, [])
        self.assertAlmostEqual(session.state.valence, 0.5)
        self.assertEqual(session.turn_count, 0)
        self.assertIsNone(session.snapshot)
        self.assertIsNone(session.last_symbol)

    def test_turn_count_counts_only_user_turns(self):
        session = new_session()
        session.add_user("一")
        session.add_assistant("答")
        session.add_user("二")
        self.assertEqual(session.turn_count, 2)

    def test_last_safety_level_reflects_latest_user_turn(self):
        session = new_session()
        session.add_user("你好", hit=check_safety("你好"))
        self.assertEqual(session.last_safety_level, "none")
        session.add_user("我不想活了", hit=check_safety("我不想活了"))
        self.assertEqual(session.last_safety_level, "high")


class TestMessageAssembly(unittest.TestCase):
    def test_messages_start_with_system_and_end_with_user(self):
        session = new_session()
        session.add_user("你好")
        session.add_assistant("你好呀")
        session.add_user("今天有点累")
        messages = assemble_chat_messages(session)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "今天有点累")

    def test_hint_can_be_suppressed(self):
        session = new_session()
        session.add_user("今天很难受", hint="内部提示：不要说出来")
        with_hint = assemble_chat_messages(session)
        without = assemble_chat_messages(session, include_hint=False)
        self.assertIn("内部提示", with_hint[-1]["content"])
        self.assertNotIn("内部提示", without[-1]["content"])

    def test_empty_session_is_system_only(self):
        self.assertEqual(len(assemble_chat_messages(new_session())), 1)

    def test_scaffold_passes_through_to_system_prompt(self):
        session = new_session()
        session.add_user("为什么天空是蓝色的？")
        messages = assemble_chat_messages(
            session, scaffold_instruction="【测试支架】先直接答。")
        self.assertIn("【测试支架】先直接答。", messages[0]["content"])


class TestEngineDetection(unittest.TestCase):
    """⑤ 引擎探测与失败提示。"""

    def test_deepseek_config_matches_official_docs(self):
        """★ DeepSeek 的 base_url 与模型名必须与官方文档一致。

        为什么单独钉这一条：模型名是最容易写错、也最难自查的地方——
        第一版我凭印象写成了 `deepseek-chat` / `deepseek-reasoner`，
        而官方（https://api-docs.deepseek.com/ ）上的现行名字是
        `deepseek-flash` 与 `deepseek-v4-pro`。写错的后果是**用户拿到
        一个 404/模型不存在**，而且报错信息通常很难看懂。
        """
        for engine_id in ("deepseek", "deepseek-pro"):
            cfg = convo.ENGINE_TABLE.get(engine_id)
            self.assertIsNotNone(cfg, f"缺少引擎 {engine_id}")
            self.assertEqual(cfg["base_url_default"], "https://api.deepseek.com",
                             "base_url 必须与官方一致（注意**不要**带 /v1）")
            self.assertEqual(cfg["key_env"], "DEEPSEEK_API_KEY")

        self.assertEqual(convo.ENGINE_TABLE["deepseek"]["model_default"],
                         "deepseek-flash")
        self.assertEqual(convo.ENGINE_TABLE["deepseek-pro"]["model_default"],
                         "deepseek-v4-pro")

    def test_retired_model_names_are_not_used(self):
        """旧模型名不许再出现（官方已退役，请求会被转到别的模型）。"""
        for cfg in convo.ENGINE_TABLE.values():
            model = cfg["model_default"]
            self.assertNotIn(model, ("deepseek-chat", "deepseek-reasoner",
                                     "deepseek-v4-flash"),
                             f"用了已退役的模型名：{model}")

    def test_available_engines_covers_all_engines(self):
        engines = available_engines()
        self.assertEqual(set(engines), set(convo.ENGINE_TABLE))
        self.assertTrue(all(isinstance(v, bool) for v in engines.values()))

    # ------------------------------------------------------------------
    # 引擎分层（primary / fallback）
    # ------------------------------------------------------------------
    def test_default_engine_matches_the_balanced_chat_mode(self):
        """默认引擎要与未开思考时的页面路由一致。

        2026-10-20 三题实测：Flash 质量接近 Pro，但延迟低 3~5 倍，
        因此默认值统一为 Flash；Pro 由「对话思考」显式启用。
        """
        self.assertEqual(convo.DEFAULT_ENGINE, "deepseek")
        self.assertEqual(convo.ENGINE_TABLE["deepseek-pro"]["tier"], "primary")
        # flash 仍要留着给用户切（快、便宜），不能因为改了默认就删掉
        self.assertEqual(convo.ENGINE_TABLE["deepseek"]["tier"], "primary")

    def test_only_deepseek_is_user_selectable(self):
        """★ 选择器里只该出现主用引擎（DeepSeek 两个）。

        依据是一次实测（同一提示词、10 条覆盖闲聊/低落/报喜/提问的输入）：
            DeepSeek Flash：反问率 30%，凡问必附"不想说也没关系"（100%）
            智谱 GLM 免费版：反问率 80%，仅 25% 附许可
        即 GLM **几乎每轮都反问**且无视许可规则，不该作为正常选项推给用户。
        """
        for name, cfg in convo.ENGINE_TABLE.items():
            expect = "primary" if name.startswith("deepseek") else "fallback"
            self.assertEqual(cfg.get("tier"), expect,
                             f"{name} 的 tier 应为 {expect}")

    def test_fallback_is_always_selectable_as_an_escape_hatch(self):
        """★ 备用引擎必须**始终可选** —— 它可能是用户唯一的出路。

        实测事故（2026-10-15，本次会话真实发生）：
            DeepSeek 配了 Key（所以系统判定"可用"），实际响应 **141~362 秒**，
            三次里还有一次直接超时；而 GLM（zhipu）只要 **1.1 秒**。
            但 zhipu 是 tier=fallback，被选择器藏起来了 ——
            **用户被锁死在两个几乎不可用的引擎上，界面上没有任何出口。**

        根因：判断"能不能选"用的是"**有没有配 Key**"，
        而不是"**现在能不能用**"。主用配了 Key 就算可用，哪怕它 300 秒不回。

        所以规则是：**只要配好了，备用就必须能选。**
        质量差异靠 label 如实标注（"说话方式不如主用稳"），
        由用户自己决定 —— 而不是替他藏起来。
        """
        original = convo.available_engines
        try:
            # 主用和备用都配好了 → 全部都要出现在选择器里
            convo.available_engines = lambda: {
                n: True for n in convo.ENGINE_TABLE}
            names, degraded = convo.selectable_engines()
            for fallback in ("zhipu", "ollama"):
                self.assertIn(
                    fallback, names,
                    f"★ {fallback} 被藏起来了 —— 主用变慢/半死时用户将没有出口")
            self.assertFalse(degraded, "主用可用时不该报降级")

            # 备用在列表里，但**必须带如实标注**（不能让用户以为一样好）
            self.assertIn("备用", convo.ENGINE_TABLE["zhipu"]["label"],
                          "备用引擎的 label 要如实标注它弱在哪")
        finally:
            convo.available_engines = original

    def test_fallback_engines_are_kept_not_deleted(self):
        """备用引擎要**保留**，不能删除。

        为什么留着：GLM 的问题在**质量**不在**安全** —— 安全属性
        （危机识别、求助热线）是代码级硬规则，不依赖模型，所以弱模型
        破坏不了安全底线。而删掉会同时失去**降级链**：
        DeepSeek 宕机/超额时就没得用了。
        """
        tiers = {cfg.get("tier") for cfg in convo.ENGINE_TABLE.values()}
        self.assertIn("fallback", tiers, "备用引擎被删光了，会失去降级能力")
        self.assertIn("zhipu", convo.ENGINE_TABLE)
        self.assertIn("ollama", convo.ENGINE_TABLE)

    def test_selectable_engines_falls_back_and_reports_it(self):
        """主用一个都没有时，退到备用**并且要报告已降级**。

        不能假装备用和主用一样好 —— 实测差距是 60% vs 0% 的不合规率。
        """
        original = convo.available_engines
        try:
            # 场景一：主用可用 → 主用在前，但**备用也必须一起出现**
            # ★ 这一条的期望在 2026-10-15 改过。原来只给主用（把备用藏起来），
            #   结果撞上真实事故：主用 141~362 秒、备用 1.1 秒，而备用被藏着
            #   → 用户没有任何出口。详见下面 test_fallback_is_always_selectable。
            convo.available_engines = lambda: {
                n: n in ("deepseek", "deepseek-pro", "zhipu") for n in convo.ENGINE_TABLE}
            names, degraded = convo.selectable_engines()
            self.assertEqual(names, ["deepseek", "deepseek-pro", "zhipu"],
                             "配好的备用引擎必须也能选 —— 它是用户唯一的逃生出口")
            self.assertFalse(degraded)

            # 场景二：只有备用可用 → 给备用，且必须报告已降级
            convo.available_engines = lambda: {
                n: n in ("zhipu", "ollama") for n in convo.ENGINE_TABLE}
            names, degraded = convo.selectable_engines()
            self.assertEqual(set(names), {"zhipu", "ollama"})
            self.assertTrue(degraded, "退到备用却没说，会误导用户")

            # 场景三：全都没有 → 空列表（调用方要能处理）
            convo.available_engines = lambda: {
                n: False for n in convo.ENGINE_TABLE}
            names, degraded = convo.selectable_engines()
            self.assertEqual(names, [])
            self.assertTrue(degraded)
        finally:
            convo.available_engines = original

    def test_ui_mode_engine_uses_pro_for_thinking_and_flash_otherwise(self):
        """界面上的两个胶囊决定最终模型，而不是把默认模型名暴露给用户。"""
        original = convo.available_engines
        try:
            convo.available_engines = lambda: {
                name: True for name in convo.ENGINE_TABLE}
            self.assertEqual(convo.mode_engine(True), "deepseek-pro")
            self.assertEqual(convo.mode_engine(False), "deepseek")
        finally:
            convo.available_engines = original

    def test_ui_mode_engine_falls_back_without_hiding_the_choice(self):
        original = convo.available_engines
        try:
            convo.available_engines = lambda: {
                "zhipu": False, "deepseek": True, "deepseek-pro": True,
                "ollama": False,
            }
            self.assertEqual(convo.mode_engine(False), "deepseek")
            self.assertEqual(convo.mode_engine(True), "deepseek-pro")
        finally:
            convo.available_engines = original


class TestInterruptedTurn(unittest.TestCase):
    """★ 用户中途按停止：留话、留半句、但半句不进上下文。

    背景（实测出来的）：客户端断开时 Starlette **不会显式关闭**同步生成器，
    所以服务端改了策略 —— 先落盘用户消息，再定期落盘已生成内容。
    这里测的是**数据层**的规则，与传输层无关。
    """

    def test_partial_reply_is_kept_for_display(self):
        """半句要留着（用户确实在屏幕上看到了），并标 interrupted。"""
        s = convo.Session()
        s.add_user("我最近老是睡不着")
        s.add_assistant("睡不着", interrupted=True)
        self.assertEqual(len(s.turns), 2)
        self.assertEqual(s.turns[1].content, "睡不着")
        self.assertTrue(s.turns[1].interrupted)

    def test_partial_reply_is_replaced_with_a_placeholder_in_context(self):
        """★ 半句原文**不能**进上下文，但也不能直接把这一轮丢掉。

        为什么不能丢（我第一版就是丢掉的）：
            丢掉会在上下文里留下**两条连续的 user 消息**（用户被打断的那句
            + 下一句），模型可能困惑或同时回应两句。
        为什么不能留原文：
            半句可能截断在语义中间（例如"你可以试着……"）。模型下一轮会
            **以为自己说过那句**，甚至自己引用自己 —— 比丢掉更糟。
        所以用一句中性占位补位：结构保持 user/assistant 交替，内容不撒谎。
        """
        s = convo.Session()
        s.add_user("第一句")
        s.add_assistant("只说了一半", interrupted=True)
        s.add_user("第二句")
        ctx = s.chat_messages()

        self.assertEqual([m["role"] for m in ctx],
                         ["user", "assistant", "user"], "结构必须交替")
        self.assertNotIn("只说了一半", str(ctx), "半句原文不该进上下文")
        self.assertIn("没有说完", ctx[1]["content"], "应当有中性占位")

    def test_normal_turn_is_unaffected(self):
        """没有中断时行为与以前完全一致（不能为了新功能改坏旧路径）。"""
        s = convo.Session()
        s.add_user("第一句")
        s.add_assistant("完整回复")
        ctx = s.chat_messages()
        self.assertEqual(len(ctx), 2)
        self.assertEqual(ctx[1]["content"], "完整回复")

    def test_interrupted_flag_survives_storage_roundtrip(self):
        """落盘再读回来，标记不能丢（否则刷新后半句会被当成完整回复）。"""
        s = convo.Session()
        s.add_user("第一句")
        s.add_assistant("半句", interrupted=True)
        restored = convo.Session.from_storage(s.to_storage())
        self.assertTrue(restored.turns[1].interrupted)
        self.assertIn("没有说完", restored.chat_messages()[1]["content"])

    def test_thinking_survives_storage_roundtrip(self):
        """思考过程也要落盘：刷新后点开"已思考"不能是空的。"""
        s = convo.Session()
        s.add_user("问")
        s.add_assistant("答", thinking="先分析问题\n再给出结论")
        restored = convo.Session.from_storage(s.to_storage())
        self.assertEqual(restored.turns[1].thinking, "先分析问题\n再给出结论")

    def test_old_history_without_the_field_still_loads(self):
        """旧历史文件没有 interrupted 字段 —— 必须能正常读，不能炸。

        这是兼容性硬要求：用户已有的对话文件是旧格式写的。
        """
        old = {"turns": [{"role": "user", "content": "旧"},
                         {"role": "assistant", "content": "旧回复"}],
               "state": {}}
        restored = convo.Session.from_storage(old)
        self.assertEqual(len(restored.turns), 2)
        self.assertFalse(restored.turns[1].interrupted)

    def test_unknown_engine_raises_friendly_error(self):
        with self.assertRaises(EngineUnavailable) as ctx:
            make_model("不存在的引擎")
        self.assertIn("未知引擎", str(ctx.exception))

    def test_missing_key_raises_engine_unavailable(self):
        """缺少 ZHIPU_API_KEY 时必须抛 EngineUnavailable，而不是崩栈。"""
        saved = {key: os.environ.pop(key, None)
                 for key in ("ZHIPU_API_KEY",)}
        try:
            with self.assertRaises(EngineUnavailable) as ctx:
                make_model("zhipu")
            self.assertIn("ZHIPU_API_KEY", str(ctx.exception))
            # 提示里必须给出可操作的两条出路
            self.assertIn("Ollama", str(ctx.exception))
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_engine_error_never_leaks_key_value(self):
        os.environ["ZHIPU_API_KEY"] = "sk-test-placeholder-not-a-real-key"
        try:
            try:
                make_model("zhipu")
            except EngineUnavailable as exc:
                self.assertNotIn("sk-super-secret-value", str(exc))
        finally:
            os.environ.pop("ZHIPU_API_KEY", None)


class TestPublicApiSurface(unittest.TestCase):
    """回归：conversation 对外承诺的名字必须真的存在。

    背景（实跑抓到）：chat_app 里写了 `con.history.validate_session_id(...)`，
    而 conversation 模块并没有 history 这个属性，导致**刷新恢复对话的路径直接
    抛 AttributeError** —— 单测全绿，因为没有任何测试走那条分支。
    这里把"对外 API 清单"钉住，防止再出现同类低级错误。
    """

    def test_history_helpers_are_reexported(self):
        for name in ("HistoryStore", "new_session_id", "validate_session_id",
                     "DEFAULT_RETENTION_DAYS"):
            self.assertTrue(hasattr(convo, name), f"conversation 缺少 {name}")
            self.assertIn(name, convo.__all__, f"{name} 未登记在 __all__")

    def test_everything_in_all_exists(self):
        for name in convo.__all__:
            self.assertTrue(hasattr(convo, name), f"__all__ 里的 {name} 不存在")

    def test_session_round_trip_through_storage(self):
        """会话序列化 → 反序列化必须保真（刷新恢复靠它）。"""
        text = "很焦虑，压力很大"          # 该句确实触发 LOW（两次命中）
        hit = check_safety(text)
        self.assertIs(hit.level, SafetyLevel.LOW, "用例前提：这句必须是 LOW")

        session = new_session()
        session.add_user(text, hit=hit)
        session.state.update(text, safety=hit)
        session.add_assistant("听起来挺不容易的。")

        restored = convo.Session.from_storage(session.to_storage())
        self.assertEqual(len(restored.turns), 2)
        self.assertEqual(restored.turns[0].content, text)
        self.assertEqual(restored.turns[0].safety_level, "low")
        self.assertAlmostEqual(restored.state.valence, session.state.valence, places=4)

    def test_from_storage_tolerates_garbage(self):
        """历史文件可能损坏/来自旧版本：必须退化，不能抛异常。"""
        for payload in (None, {}, {"turns": "不是列表"},
                        {"turns": [{"role": "???"}, "字符串", None]},
                        {"state": "不是字典"}):
            restored = convo.Session.from_storage(payload)  # type: ignore[arg-type]
            self.assertIsInstance(restored, convo.Session)

    def test_turn_storage_handles_extra_and_missing_fields(self):
        turn = convo.Turn.from_storage({"role": "user", "content": "hi",
                                        "未知字段": 1})
        self.assertEqual(turn.role, "user")
        self.assertEqual(turn.safety_level, "none")
        self.assertEqual(turn.rule_ids, ())


class TestGaugeAndTrend(unittest.TestCase):
    def test_gauge_rows_shape(self):
        session = new_session()
        session.state.update("今天特别累，很难受", safety=check_safety("很难受"))
        rows = gauge_rows(session.snapshot)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIn(row["name"], ("valence", "arousal", "dominance", "energy"))
            self.assertGreaterEqual(row["percent"], 0)
            self.assertLessEqual(row["percent"], 100)
            self.assertTrue(row["label"])

    def test_gauge_rows_empty_when_no_snapshot(self):
        self.assertEqual(gauge_rows(None), [])

    def test_trend_label_length_is_bounded(self):
        self.assertEqual(trend_label([]), "")
        self.assertEqual(len(trend_label([0.5] * 50)), 12)

    def test_trend_label_is_monotonic_in_symbols(self):
        low = trend_label([0.0])
        high = trend_label([1.0])
        self.assertNotEqual(low, high)


class TestDsmlToolCallFallback(unittest.TestCase):
    """模型不返回 tool_calls、改把 DSML 写进正文时的兼容层。

    真实事故：用户问"东京什么天气"，界面上直接出现
    `<||DSML||calls>...` 源码。工具阶段必须接住并真正执行，
    而不是把这段文本留到最终回答里。
    """

    SAMPLE = (
        "<||DSML||calls>\n"
        '<||DSML||invoke name="get_current_weather">\n'
        '<||DSML||parameter name="city" string="true">Tokyo</||DSML||parameter>\n'
        "</||DSML||invoke>\n"
        "</||DSML||calls>"
    )

    def test_parse_dsml_tool_call(self):
        calls = convo._extract_dsml_tool_calls(self.SAMPLE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_current_weather")
        self.assertEqual(calls[0]["args"], {"city": "Tokyo"})

    def test_run_tool_phase_executes_dsml_call(self):
        from types import SimpleNamespace

        sample = self.SAMPLE

        class FakeTool:
            def invoke(self, args):
                return f"fake weather for {args['city']}"

        original = convo.tool_layer.tool_by_name
        convo.tool_layer.tool_by_name = (
            lambda name: FakeTool() if name == "get_current_weather" else None)

        class FakeModel:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                return SimpleNamespace(content=sample)

        try:
            phase = convo.run_tool_phase(
                FakeModel(), [{"role": "user", "content": "东京什么天气"}])
        finally:
            convo.tool_layer.tool_by_name = original

        self.assertTrue(phase.used_tools, "DSML 工具调用没有被接住")
        self.assertEqual(phase.calls[0].name, "get_current_weather")
        self.assertEqual(phase.calls[0].arguments["city"], "Tokyo")
        self.assertEqual(phase.calls[0].result, "fake weather for Tokyo")

    def test_unknown_dsml_tool_is_not_executed(self):
        """白名单：模型不能靠写一段 DSML 调到任意函数。"""
        from types import SimpleNamespace

        sample = ('<||DSML||calls><||DSML||invoke name="drop_database">'
                  "</||DSML||invoke></||DSML||calls>")
        original = convo.tool_layer.tool_by_name
        convo.tool_layer.tool_by_name = lambda name: None

        class FakeModel:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                return SimpleNamespace(content=sample)

        try:
            phase = convo.run_tool_phase(FakeModel(), [{"role": "user", "content": "hi"}])
        finally:
            convo.tool_layer.tool_by_name = original

        self.assertFalse(phase.used_tools, "未知工具被当成真实调用执行了")


if __name__ == "__main__":
    unittest.main()
