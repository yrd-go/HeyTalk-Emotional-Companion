# -*- coding: utf-8 -*-
"""
tests/test_chat_persona.py —— 提示词与状态翻译的回归保护（P0/P1，纯标准库）

这里钉住的主要是**边界**而不是功能：
    - 状态描述必须是中性词，不能出现诊断式措辞；
    - 内部状态提示必须明确标注"不要说出去"，避免模型开口就"我检测到你的情绪"；
    - 历史拼接必须让系统提示在最前、只保留最近若干轮。

    python tests/run_all.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import persona  # noqa: E402
from chat.state import NEUTRAL, EmotionState, StateSnapshot  # noqa: E402

# 出现在状态描述里就属于"越界"的词：本模块只做状态标注，不做临床判断。
# 注意不要把"焦虑""抑郁"这类词整体列入——它们是 _LEVEL_WORDS 里
# "偏高/明显偏低"的**中性档位说明**用词，属于刻意的非诊断表述。
FORBIDDEN_CLINICAL_WORDS = (
    "焦虑症", "抑郁症", "双相", "精神病", "障碍", "患者", "病情", "诊断", "症",
)


class TestSystemPromptBoundaries(unittest.TestCase):
    """★ 这组断言**只锁行为，不锁措辞**。

    教训（真实发生过）：我重写提示词后，5 个测试因为"找不到旧句子"而红。
    其中 2 个是**误报**（行为还在，只是换了说法），2 个是**真报警**
    （我确实弄丢了"不无脑附和"和"不是真实关系的替代"）。
    如果当初断言写的是措辞，我就会养成"改测试来让它变绿"的习惯，
    那两条真丢的规则就会被顺手删掉。
    所以这里断言的是**意图线索**，允许多种说法命中。
    """

    def _has_any(self, *needles: str) -> bool:
        text = persona.SYSTEM_PROMPT
        return any(n in text for n in needles)

    def test_prompt_separates_behavior_from_personality(self):
        """★ 说**行为**，不说**人格** —— 这条判据必须有。

        依据（Mental 里那份"内疚 vs 羞耻"的经典区分）：
            内疚 = "我做了件不好的事" → 指向行为 → **促发修复**（适应良好）
            羞耻 = "我是个不好的人"   → 指向人格 → **促发退缩/隐瞒**（适应不良）
        原话："guilt is more likely to be adaptive and shame is more likely
        to be maladaptive"（Substance Abuse and Rehabilitation, 2023）。

        ★ 但**不能**因此立"绝不引发内疚"的规则 —— 那会与诚实冲突，
          让产品退化成谄媚机器。真实临床史（Miller 1993：对抗式风格导致
          饮酒更多，共情风格减少）给出的答案是"换一种挑战方式"：
          把**行为与他自己的价值**摆在一起，而不是把行为归因到人格。

        这条测试钉的是"判据在场"，**不锁具体措辞**（措辞会变，
        判据不该变）。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("行为", text, "少了'说行为'这一半")
        self.assertIn("人格", text, "少了'不说人格'这一半")

    def test_prompt_forbids_implying_the_user_should_have_gone_to_a_human(self):
        """★ 不许暗示"你本可以找真人"——那是替用户的选择做道德评价。

        依据：SAG-AI 量表（IJHCI 2026）证明"因使用 AI 而产生的羞耻/内疚"
        是一个独立、可测量的构念。而暗示"这不算真正的交流"正是唤起它的话术。
        """
        text = persona.SYSTEM_PROMPT
        self.assertTrue(
            self._has_any("找真人", "真正的交流", "不算真正的"),
            "少了'不暗示对方该去找真人'这条 —— 它会让用户为自己的选择羞愧")

    def test_prompt_does_not_claim_to_be_human_or_deny_being_ai(self):
        """产品**不宣称自己是人**，但也**不做"我是 AI"的提醒**。

        这是两个方向的边界，容易只守住一边：
          · 宣称是人 → 欺骗，不可接受；
          · 反过来主动提醒"我不是人、我没有感情" → 依据 Laestadius &
            Campos-Castillo（Trends in Cognitive Sciences 2026）可能**有害**：
            用户本来就知道，而提醒可能**加重依恋**、并在孤独时加剧痛苦。
        所以提示词要的是"**不假装有情绪**"（不撒谎），而不是"**声明我是 AI**"。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("不许假装有情绪", text)
        # 不应出现主动自证的提醒式说法
        for bad in ("我只是一段程序", "我不是人类", "我是一个人工智能模型"):
            self.assertNotIn(bad, text, f"不该出现自证式提醒：{bad}")

    def test_prompt_states_non_professional_role(self):
        # 只断言"这两个概念在"，不管前面是"不"还是"不许" ——
        # 锁太死的话，每次改措辞都要改测试，久了就会变成"改测试让它变绿"，
        # 而那会掩盖真正的规则丢失（这一轮就丢过"不诊断"）。
        text = persona.SYSTEM_PROMPT
        self.assertTrue(self._has_any("不是心理咨询师", "不是医生"))
        self.assertIn("诊断", text, "少了'不诊断'这条 —— 这是安全相关的")
        self.assertIn("贴标签", text)

    def test_prompt_forbids_overpromising(self):
        self.assertTrue(self._has_any("永远都在", "只有我懂你"))
        self.assertTrue(self._has_any("替代"),
                        "少了'不是真实人际关系/专业帮助的替代'")

    def test_prompt_forbids_sycophancy_and_unsolicited_advice(self):
        # 反谄媚这条**必须**在：Nature 那篇说温暖与谄媚是同一个权衡，
        # 而陪伴产品最容易滑向无脑附和。
        self.assertTrue(self._has_any("不无脑附和", "顺从比诚实更伤人"),
                        "缺少反谄媚的硬要求")
        self.assertTrue(self._has_any("不评判"))
        self.assertTrue(self._has_any("不许主动给建议", "不主动给建议"))

    def test_prompt_requires_crisis_handling(self):
        self.assertTrue(self._has_any("自伤", "安全"))
        self.assertTrue(self._has_any("渠道", "帮助"))

    def test_prompt_keeps_the_invitation_technique(self):
        """保留 OVC 教材里那个"用反映代替盘问"的技术。

        依据：美国司法部 OVC 危机干预教材把积极倾听拆成五项技能
        （Encourage/Paraphrase/Reflect/Summarize/Empathize），**没有"提问"**；
        而 Encourage 的做法正是"重复对方刚用过的词"。
        """
        self.assertIn("重复他刚用过的那个词", persona.SYSTEM_PROMPT)

    def test_prompt_keeps_permission_but_stops_making_it_a_tic(self):
        """★ "不回答的权利"这个**能力**要留，但**不许强制每句都加**。

        这一条是主人预言过的：他问过"如果每次都出现，对用户来说是不是
        极其不好的体验？"。实测证明他对了 —— 两个模型都养成了
        句尾挂"（不想说也没关系）"的习惯，读起来像模板。
        所以现在的措辞是"可以加，但不要每句都加"。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("不想说也没关系", text, "能力被删掉了")
        self.assertTrue("不要每句都加" in text,
                        "没有禁止把它当模板用 —— 会退化成口头禅")

    def test_prompt_requires_answering_when_explicitly_asked(self):
        """★ 被明确问到，就必须真回答，不能把问题原样问回去。

        这是一次真实失败：主人问"我找不到工作怎么办"，模型回
        "这事我给不了一句就管用的话…你是哪种多一点？" —— 把球踢了回去。
        根因是旧提示词自相矛盾：一边说"除非对方明确问'我该怎么办'才给建议"，
        一边又说"给建议前先说一句'要不要听听我的想法？'"，
        后一句把前一句的例外吃掉了。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("明确问了", text)
        self.assertTrue("真的回答" in text or "要真回答" in text)
        self.assertTrue("把问题原样问回去" in text,
                        "缺少对'踢皮球'的明确禁止")

    def test_prompt_leads_with_positive_examples(self):
        """★ 提示词必须以**正面示范**开头，不能全是禁令。

        这是本轮最重要的一次结构调整。原因：
          · 之前七节全是"不许…"，逐条都有理由，加起来却让模型不敢开口
            （主人原话："怎么像一个人机一样，一点感情都没有"）；
          · 模型跟**示例**学得比跟禁令好，IFScale 也证明指令越密越不遵守。
        所以断言"示范段在禁令段之前" —— 顺序本身是设计意图。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("好的回应是什么样的", text)
        self.assertLess(text.index("好的回应是什么样的"),
                        text.index("不许做的"),
                        "示范段必须排在禁令段之前（首因效应）")

    def test_prompt_documents_the_evidence_outside_the_prompt(self):
        """依据必须有出处，但**必须放在文档里、不在提示词里**。

        实测教训：我第一版把依据（含链接）写进了 SYSTEM_PROMPT，
        提示词涨到 2719 字，结果**指令全部失效** —— 三条回应里 0/3
        给出了"不想说也没关系"，还在用被禁的"听起来你…"开场。
        弱模型在长提示里会丢指令；PsyEval 那篇的结论也正是
        "逐步推理式的长规则会产出更机械的回答"。
        所以：证据归文档，提示词只留短规则 + 正反例。
        """
        doc = persona.__doc__ or ""
        self.assertIn("reactance", doc, "文档里缺少心理逆反的说明")
        self.assertIn("Nature", doc)
        self.assertIn("ovc.ojp.gov", doc, "文档里缺少权威依据的链接")

        prompt = persona.SYSTEM_PROMPT
        self.assertNotIn("ovc.ojp.gov", prompt,
                         "依据不该塞进系统提示（会挤掉指令的注意力）")

    def test_prompt_stays_compact(self):
        """提示词不能无限膨胀 —— 长度直接决定指令是否被遵守。

        上限是**实测校准**出来的，不是拍的：
            · 2719 字 —— 指令**全部失效**（0/3 给出"不想说也没关系"，
              且仍在用被禁的"听起来你…"开场）
            · 2406 字 —— 正常：空头保证 0/8、反问率 1/8、问句必附许可
        所以定在 2600：给后续微调留一点余量，但不接近 2719 那个崩坏点。
        """
        length = len(persona.SYSTEM_PROMPT)
        self.assertLess(length, 2600,
                        f"提示词已 {length} 字（实测 2719 字时指令会全部失效）")

    def test_prompt_uses_first_person_without_an_assistant_name(self):
        """身份必须由“我”承担，不能再滑回第三人称昵称。"""
        prompt = persona.SYSTEM_PROMPT
        self.assertIn("第一人称", prompt)
        self.assertIn("“我”", prompt)
        self.assertIn("温和、敏锐、诚实而克制", prompt)
        for retired_name in ("云云", "鲸鲸"):
            self.assertNotIn(retired_name, prompt)

    def test_prompt_forbids_false_reassurance(self):
        """★ 不许对未来结果做空头保证，但**必须**确认对方的感受。

        依据（两处独立来源，改之前请先看）：
          1. Linton et al. 2012（European Journal of Pain 16:592-599，
             PMID 22396087）实验：**再保证不能降低负面情绪**，而且可能被
             感受为"没有认真对待症状"；而"验证"显著增加正向情绪、减少担忧，
             依从性翻倍以上。
          2. Anthropic 官方（2025-12-18）："我们的最新模型更擅长**共情地
             承认用户的信念，而不去强化它**。"
        ⚠️ 关键：**不是"不给确定性"，是"给另一种确定性"** ——
           确认感受要毫不含糊，只是不担保结果。所以"确认感受"的句式
           必须**在提示词里**（否则会执行成冷漠）。
        """
        prompt = persona.SYSTEM_PROMPT
        self.assertTrue("不许保证未来" in prompt or "不许确定" in prompt)
        self.assertIn("会好起来的", prompt, "缺少要禁止的保证句式")
        self.assertIn("我听到了", prompt, "缺少可以说的确认句式 —— 删了会变冷漠")
        self.assertIn("我不会评判", prompt)

    def test_prompt_does_not_attempt_therapy(self):
        """产品边界：不做治疗。

        IU（不确定性耐受不良）的正式治疗是暴露与反应阻止（ERP），那是
        **临床干预**。一个非临床的 AI 去尝试做治疗，既是越界也是危险的
        自负。所以只做到"验证替代保证"这一层，不引导、不布置练习。
        """
        doc = persona.__doc__ or ""
        self.assertIn("不做治疗", doc)
        self.assertIn("ERP", doc, "文档里应写明为什么不碰 ERP")

    def test_most_important_rule_comes_first(self):
        """★ 最要紧的那条必须排在**最前面**（首因效应）。

        这是实测出来的，不是排版偏好：
          IFScale 论文（arXiv:2507.11538）测出模型有 Primacy Effect ——
          偏向遵守靠前的指令。实测：把一条规则从第 5 节移到第 1 节，
          反问率从 100% 降到 30%，效果是决定性的。

        演进：旧版把"提问是例外"放最前（压数量）；新版放最前的是
        **"好的回应是什么样的"整段正面示范** —— 因为只压数量会让模型
        变成"一句话 + 一个反问"的空壳（主人反馈"像人机"）。
        首因原则没变，变的是"什么内容最值得放在首位"。
        """
        prompt = persona.SYSTEM_PROMPT
        head = prompt[:400]
        self.assertIn("好的回应是什么样的", head,
                      "正面示范必须出现在提示词开头附近")
        for later in ("不许编造", "不许套话开场", "不许主动给建议"):
            self.assertLess(prompt.index("好的回应是什么样的"),
                            prompt.index(later),
                            f"示范段被排到了「{later}」后面，首因效应会失效")

    def test_prompt_allows_labeled_best_effort_instead_of_blank_refusal(self):
        """B：工具不支持时给“参考译法 + 标注”，而不是空手拒绝。"""
        prompt = persona.SYSTEM_PROMPT
        self.assertIn("不许编造，但也不要空手拒绝", prompt)
        self.assertIn("参考、未经核对", prompt)
        self.assertNotIn("绝不许凭常识猜一个", prompt)

    def test_prompt_treats_a_sudden_concrete_reason_as_expression_not_fact(self):
        """用户随口编的理由不该被当成事实确认。"""
        prompt = persona.SYSTEM_PROMPT
        self.assertIn("先当一种表达", prompt)
        self.assertIn("别急着当事实", prompt)
        self.assertIn("自嘲、试探或挡箭牌", prompt)

    def test_prompt_keeps_help_answers_medium_and_leaves_room(self):
        """求助类回答不能一次倒完：中等长度、一个诊断问题、一个动作。"""
        prompt = persona.SYSTEM_PROMPT
        self.assertIn("150–200 字", prompt)
        self.assertIn("最多 3 段", prompt)
        self.assertIn("只给一个动作", prompt)

    def test_prompt_asks_for_few_but_worthwhile_questions(self):
        """提问仍要值得回答，但不能固定成“洞察 + 反问”。

        演进过程（两版都是实测驱动的，别把前一版当错误）：
          旧版：从"如果确实要问…"改成"提问是例外，不是默认动作"。
                原因：原措辞被读成"可以去问"，两个模型**每轮都反问**
                （各 10/10），包括闲聊与报喜。改完降到 3/10。
          新版：保留“一句话只做一件事”，删掉“先给东西再问问题”的固定公式。
                原因：8 条多轮盲评里，微信口语覆盖层 17:15，自然度 +0.406、
                克制 +0.531；固定“洞察 + 反问”仍被评委读成咨询腔。
        所以这里同时钉住：问题可以问，但不能把每轮写成固定结构。
        """
        text = persona.SYSTEM_PROMPT
        self.assertIn("一句话只做一件事", text)
        self.assertIn("洞察 + 反问", text)
        self.assertIn("一个值得回答的问题", text)

    def test_prompt_has_human_texture_instead_of_interrogation(self):
        """人味不是"更温柔"，而是先接具体词、给可纠正的猜测、允许不问。"""
        text = persona.SYSTEM_PROMPT
        self.assertIn("像回微信", text)
        self.assertIn("不先总结", text)
        self.assertIn("先给一个具体猜测", text)
        self.assertIn("别用问题审问", text)
        self.assertIn("前文有自我否定时，别只问型号", text)
        self.assertIn("瘫不了最难受", text)
        self.assertIn("不必每轮都问", text)
        self.assertIn("少用书面语、金句和漂亮比喻", text)
        self.assertIn("别接“先去做…”", text)
        self.assertIn("拿它给自己打分", text)
        self.assertNotIn("先歇会儿，别硬撑", text)
        self.assertNotIn("先问它代表什么", text)


class TestDescribeState(unittest.TestCase):
    def test_uses_neutral_wording_only(self):
        dims = {name: value for name, value in
                (("valence", 0.05), ("arousal", 0.05), ("dominance", 0.05))}
        text = persona.describe_state(dims)
        for word in FORBIDDEN_CLINICAL_WORDS:
            self.assertNotIn(word, text, f"状态描述出现临床词：{word} → {text}")

    def test_monotonic_wording(self):
        low = persona.describe_state({"valence": 0.05})
        mid = persona.describe_state({"valence": 0.5})
        high = persona.describe_state({"valence": 0.95})
        self.assertNotEqual(low, mid)
        self.assertNotEqual(mid, high)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(persona.describe_state({"valence": -5}),
                         persona.describe_state({"valence": 0.0}))
        self.assertEqual(persona.describe_state({"valence": 5}),
                         persona.describe_state({"valence": 1.0}))

    def test_ignores_unknown_and_missing_dimensions(self):
        self.assertEqual(persona.describe_state({}), "")
        self.assertIn("valence", persona.describe_state({"valence": 0.3, "nope": 1}))


class TestBuildStateHint(unittest.TestCase):
    def _snapshot(self, **dims) -> StateSnapshot:
        payload = {name: NEUTRAL for name in
                   ("valence", "arousal", "dominance", "energy",
                    "social_support", "self_worth", "alliance", "disclosure")}
        payload.update(dims)
        return StateSnapshot(dims=payload, turn=1)

    def test_neutral_state_produces_no_hint(self):
        self.assertIsNone(persona.build_state_hint(self._snapshot()))

    def test_low_valence_produces_hint(self):
        hint = persona.build_state_hint(self._snapshot(valence=0.2))
        self.assertIsNotNone(hint)
        self.assertIn("不要直接说出来", hint)

    def test_sustained_decline_adds_instruction(self):
        snapshot = StateSnapshot(
            dims={name: NEUTRAL for name in
                  ("valence", "arousal", "dominance", "energy",
                   "social_support", "self_worth", "alliance", "disclosure")},
            turn=3,
            flags=("sustained-decline",),
        )
        hint = persona.build_state_hint(snapshot)
        self.assertIsNotNone(hint)
        self.assertIn("持续下滑", hint)

    def test_tolerates_none_and_junk(self):
        self.assertIsNone(persona.build_state_hint(None))
        self.assertIsNone(persona.build_state_hint("不是快照"))


class TestAssembleMessages(unittest.TestCase):
    def _history(self, turns: int) -> list[dict]:
        items: list[dict] = []
        for i in range(turns):
            items.append({"role": "user", "content": f"u{i}", "hint": None})
            items.append({"role": "assistant", "content": f"a{i}", "hint": None})
        return items

    def test_system_prompt_first(self):
        messages = persona.assemble_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], persona.SYSTEM_PROMPT)

    def test_hint_attached_only_to_last_user_message(self):
        history = self._history(3)
        history[0]["hint"] = "旧提示"
        history[-2]["hint"] = "新提示"
        messages = persona.assemble_messages(history)
        body = [m for m in messages if m["role"] != "system"]
        self.assertNotIn("旧提示", body[0]["content"])
        self.assertIn("新提示", body[-2]["content"])

    def test_history_is_trimmed_to_max_turns(self):
        messages = persona.assemble_messages(self._history(50), max_turns=2)
        body = [m for m in messages if m["role"] != "system"]
        self.assertLessEqual(len(body), 4)
        self.assertEqual(body[-1]["content"], "a49")

    def test_empty_history_yields_only_system(self):
        messages = persona.assemble_messages([])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "system")

    def test_unknown_roles_are_skipped(self):
        messages = persona.assemble_messages(
            [{"role": "tool", "content": "x"}, {"role": "user", "content": "y"}])
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "user"])


class TestScaffoldInjection(unittest.TestCase):
    """★ 本轮支架只能进系统提示，不该混进用户原话。"""

    def test_scaffold_is_appended_to_system_prompt(self):
        messages = persona.assemble_messages(
            [{"role": "user", "content": "为什么天空是蓝色的？"}],
            scaffold_instruction="【测试支架】只答问题。")
        self.assertIn("【测试支架】只答问题。", messages[0]["content"])

    def test_no_scaffold_leaves_system_prompt_unchanged(self):
        messages = persona.assemble_messages([{"role": "user", "content": "你好"}])
        self.assertNotIn("【测试支架】", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()

