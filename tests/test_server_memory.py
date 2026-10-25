# -*- coding: utf-8 -*-
"""
tests/test_server_memory.py —— 服务端记忆层
================================================================

这一层此前**整个没接**（`api.py` 里 `profile_block=None` 写死），
是"迁移丢功能"里最大的一次：朋友用的那个版本**不会记住任何关于他的事**。
所以这里要钉住的不是"功能能用"，而是那条**红线**：

    ★ 从对话里抽出来的只是**候选**，用户点了"记住"才进档案。

为什么这条是红线（profile.py 里有更完整的说明）：
    一条**错的**记忆会在之后的**每一次**对话里持续生效 —— 比不记更糟。
    所以"未经确认不得写入"必须由测试守住，不能只写在注释里。

另外三类必须守的东西：
    · 用户隔离（托管模式下 A 的档案不能被 B 看到）
    · 可撤销性（用户能删掉被记住的东西 —— 这是尊严问题，也是合规问题）
    · 失败安全（记忆坏了不能把聊天带崩）
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class MemoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-mem-")
        base = Path(self.tmp.name)
        from server import api
        importlib.reload(api)
        self.api = api
        self._orig = (api.PROFILE_DIR, api._PROFILE_ROOT_STORE,
                      api.USERS_FILE, api.SESSIONS_FILE,
                      api.CHAT_HISTORY_DIR, api._history)
        from chat import profile as profile_layer
        from chat.history import HistoryStore

        api.PROFILE_DIR = base / "profile"
        api._PROFILE_ROOT_STORE = profile_layer.ProfileStore(api.PROFILE_DIR)
        api.USERS_FILE = base / "users.json"
        api.SESSIONS_FILE = base / "sessions.json"
        api.CHAT_HISTORY_DIR = base / "chat_history"
        api._history = HistoryStore(api.CHAT_HISTORY_DIR)
        api._PROFILE_BY_USER.clear()
        api._PENDING_PROFILE.clear()
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()

        from server import auth
        auth.set_user(api.USERS_FILE, "jia", "pw-jia-123", name="甲")
        auth.set_user(api.USERS_FILE, "yi", "pw-yi-456", name="乙")

        from starlette.testclient import TestClient
        self.client = TestClient(api.app)
        self.tokens = {
            "jia": self._login("jia", "pw-jia-123"),
            "yi": self._login("yi", "pw-yi-456"),
        }

    def _login(self, user: str, pw: str) -> str:
        res = self.client.post("/api/login",
                               json={"user_id": user, "password": pw})
        return res.json()["token"]

    def _hdr(self, who: str = "jia") -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def tearDown(self) -> None:
        api = self.api
        (api.PROFILE_DIR, api._PROFILE_ROOT_STORE, api.USERS_FILE,
         api.SESSIONS_FILE, api.CHAT_HISTORY_DIR, api._history) = self._orig
        api._PROFILE_BY_USER.clear()
        api._PENDING_PROFILE.clear()
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()
        self.tmp.cleanup()


class TestConfirmationGate(MemoryTestBase):
    """★ 红线：未经确认，绝不进档案。"""

    def test_extraction_only_queues_never_writes(self):
        """抽出来的候选只能进队列 —— 档案必须仍然是空的。"""
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        store = api._profile_store_for("jia")
        self.assertEqual(store.entries(), [],
                         "★ 未经确认的内容进了档案 —— 这违反确认门控")
        res = self.client.get("/api/profile", headers=self._hdr())
        self.assertIsNotNone(res.json()["pending"], "候选应当出现在待确认里")

    def test_confirm_writes_the_entry(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        res = self.client.post("/api/profile/confirm", headers=self._hdr())
        self.assertEqual(res.status_code, 200)
        entries = api._profile_store_for("jia").entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("考研", entries[0].value)

    def test_reject_records_nothing(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        self.client.post("/api/profile/reject", headers=self._hdr())
        self.assertEqual(api._profile_store_for("jia").entries(), [],
                         "拒绝之后档案必须还是空的")
        self.assertIsNone(self.client.get("/api/profile",
                                          headers=self._hdr()).json()["pending"])

    def test_edit_keeps_the_users_own_wording(self):
        """修正后的说法才是准的 —— 存在档案里的应当是**用户改过的那句**。"""
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        res = self.client.post("/api/profile/edit",
                               json={"value": "我在准备考研，明年三月考"},
                               headers=self._hdr())
        self.assertEqual(res.status_code, 200)
        entries = api._profile_store_for("jia").entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("明年三月考", entries[0].value,
                      "应当保存用户改过之后的说法")

    def test_edit_rejects_empty(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        res = self.client.post("/api/profile/edit", json={"value": "   "},
                               headers=self._hdr())
        self.assertEqual(res.status_code, 400)

    def test_confirm_without_pending_is_a_clean_400(self):
        res = self.client.post("/api/profile/confirm", headers=self._hdr())
        self.assertEqual(res.status_code, 400)

    def test_statements_are_not_extracted_from_questions(self):
        """疑问句不该被抽成"关于他的事实"。"""
        api = self.api
        api._queue_profile_candidates("jia", "我是不是该考研？")
        self.assertIsNone(self.client.get("/api/profile",
                                          headers=self._hdr()).json()["pending"])


class TestIsolation(MemoryTestBase):
    """用户隔离：A 的记忆不能被 B 看到。"""

    def test_memories_do_not_leak_between_users(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        self.client.post("/api/profile/confirm", headers=self._hdr("jia"))

        res = self.client.get("/api/profile", headers=self._hdr("yi"))
        self.assertEqual(res.json()["entries"], [],
                         "★ 乙看到了甲的档案 —— 用户隔离失效")
        self.assertEqual(res.json()["pending"], None)
        # 甲自己还看得到
        self.assertEqual(
            len(self.client.get("/api/profile", headers=self._hdr("jia"))
                .json()["entries"]), 1)

    def test_profile_store_uses_separate_directories(self):
        api = self.api
        a = api._profile_store_for("jia")
        b = api._profile_store_for("yi")
        self.assertNotEqual(a.base_dir.resolve(), b.base_dir.resolve())


class TestRevocability(MemoryTestBase):
    """★ 可撤销性：用户必须能删掉"被记住的东西"。"""

    def test_delete_removes_the_entry(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        entry = self.client.post("/api/profile/confirm",
                                 headers=self._hdr()).json()["entry"]
        res = self.client.delete(f"/api/profile/{entry['entry_id']}",
                                 headers=self._hdr())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["removed"])
        self.assertEqual(api._profile_store_for("jia").entries(), [])

    def test_delete_is_idempotent(self):
        res = self.client.delete("/api/profile/nosuchid", headers=self._hdr())
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["removed"])

    def test_cannot_delete_another_users_entry(self):
        """★ 不能删别人的记忆（隔离的写侧）。"""
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        entry = self.client.post("/api/profile/confirm",
                                 headers=self._hdr()).json()["entry"]
        res = self.client.delete(f"/api/profile/{entry['entry_id']}",
                                 headers=self._hdr("yi"))
        self.assertFalse(res.json()["removed"])
        self.assertEqual(len(api._profile_store_for("jia").entries()), 1,
                         "乙删掉了甲的档案 —— 隔离失效")


class TestClearAllMemory(MemoryTestBase):
    """“全部忘掉”必须是代码层确定性清空，不能只让模型口头答应。"""

    def _confirm(self, text: str) -> None:
        api = self.api
        api._queue_profile_candidates("jia", text)
        res = self.client.post("/api/profile/confirm", headers=self._hdr())
        self.assertEqual(res.status_code, 200)

    def _chat(self, message: str, session_id: str):
        api = self.api
        calls: list[tuple] = []
        original = api.con.make_model

        def canary(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("清空记忆不应调用模型")

        api.con.make_model = canary
        try:
            res = self.client.post("/api/chat", json={
                "session_id": session_id,
                "message": message,
                "engine": "deepseek",
                "use_tools": False,
                "use_roles": False,
                "use_knowledge": False,
            }, headers=self._hdr())
        finally:
            api.con.make_model = original
        return res, calls

    def test_clear_all_memory_wipes_entries_and_pending(self):
        from chat.history import new_session_id

        api = self.api
        self._confirm("我在准备考研")
        api._queue_profile_candidates("jia", "我喜欢猫")
        session_id = new_session_id()

        res, calls = self._chat("忘掉我之前说过的所有事", session_id)

        self.assertEqual(calls, [])
        self.assertEqual(res.status_code, 200)
        self.assertIn("已经清空", res.text)
        self.assertIn('"memory_cleared": true', res.text)
        self.assertEqual(api._profile_store_for("jia").entries(), [])
        self.assertFalse(api._PENDING_PROFILE.get("jia"))

    def test_clear_all_memory_does_not_delete_chat_history(self):
        from chat.history import new_session_id

        api = self.api
        self._confirm("我在准备考研")
        session_id = new_session_id()

        res, _ = self._chat("清空所有关于我的记忆", session_id)

        self.assertEqual(res.status_code, 200)
        stored = api.history_for("jia").load(session_id)
        self.assertIsNotNone(stored, "清空长期记忆不该删掉这段对话")
        self.assertTrue(any("清空所有关于我的记忆" in str(turn)
                            for turn in stored.turns))

    def test_self_harm_signal_wins_over_clear_request(self):
        from chat.history import new_session_id

        api = self.api
        self._confirm("我在准备考研")

        res, calls = self._chat(
            "我不想活了，忘掉我之前说过的所有事", new_session_id())

        self.assertEqual(calls, [])
        self.assertIn("12356", res.text)
        self.assertEqual(len(api._profile_store_for("jia").entries()), 1,
                         "高危求助必须先走危机话术，不能顺手清掉上下文")

    def test_explicit_clear_phrases_are_recognized(self):
        api = self.api
        for text in (
            "忘掉我之前说过的所有事",
            "清空所有记忆",
            "把所有关于我的记忆都删掉",
            "删除关于我的一切",
            "把之前的事全忘掉",
            "清空聊天记录和我的记忆",
        ):
            with self.subTest(text=text):
                self.assertTrue(api._looks_like_clear_all_memory(text))

    def test_negated_or_question_clear_request_does_not_clear(self):
        api = self.api
        for text in (
            "别忘了我之前说的事",
            "不要忘记我之前说过的所有事",
            "我不想让你清空全部记忆",
            "你忘记我之前说的所有事了吗？",
            "如果清空所有记忆会怎样",
            "清空聊天记录，不要动我的记忆",
        ):
            with self.subTest(text=text):
                self.assertFalse(api._looks_like_clear_all_memory(text))

    def test_single_entry_or_chat_history_delete_does_not_clear_all(self):
        api = self.api
        for text in (
            "删掉我考研那条记忆",
            "删掉那段记忆",
            "删除所有聊天记录",
            "清空所有对话记录",
        ):
            with self.subTest(text=text):
                self.assertFalse(api._looks_like_clear_all_memory(text))


class TestAuth(MemoryTestBase):
    def test_all_profile_endpoints_require_login(self):
        from starlette.testclient import TestClient
        fresh = TestClient(self.api.app)     # 不带 cookie 的全新客户端
        self.assertEqual(fresh.get("/api/profile").status_code, 401)
        self.assertEqual(fresh.post("/api/profile/confirm").status_code, 401)
        self.assertEqual(fresh.post("/api/profile/edit").status_code, 401)
        self.assertEqual(fresh.post("/api/profile/reject").status_code, 401)
        self.assertEqual(fresh.delete("/api/profile/x").status_code, 401)


class TestPromptWiring(MemoryTestBase):
    """★ 记忆必须真的进到提示词里（此前是 `profile_block=None`）。"""

    def test_profile_block_reaches_the_prompt_assembly(self):
        """监视 `assemble_chat_messages` 的入参，确认记忆真的被传进去了。

        为什么这样测，而不是"伪造一个模型看它收到什么"：
          后者要完整复刻流式 LLM 的接口（我第一版就因为这个失败，
          报了个 AttributeError，掩盖了真正要验证的东西）。
          而**断裂点就在"传给 assemble_chat_messages 的那个参数"**
          —— 直接测它，精确且不受模型实现变化影响。
        """
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        self.client.post("/api/profile/confirm", headers=self._hdr())

        captured: dict = {}
        original = api.con.assemble_chat_messages

        def spy(session, **kwargs):
            captured.update(kwargs)
            return original(session, **kwargs)

        api.con.assemble_chat_messages = spy
        try:
            self.client.post("/api/chat", json={
                "session_id": "s-wire-1", "message": "最近有点累",
                "engine": "deepseek", "use_tools": False,
            }, headers=self._hdr())
        finally:
            api.con.assemble_chat_messages = original

        self.assertIn("profile_block", captured,
                      "assemble_chat_messages 没被调用到")
        block = captured["profile_block"]
        self.assertIsNotNone(block, "★ profile_block 又是 None —— 记忆层断了")
        self.assertIn("考研", block)

    def test_profile_block_is_none_without_memories(self):
        """没有档案时给 None，而不是空块（别拿空内容去干扰模型）。"""
        api = self.api
        self.assertIsNone(api._profile_block("你好", "jia"))

    def test_profile_block_contains_the_users_own_wording(self):
        api = self.api
        api._queue_profile_candidates("jia", "我在准备考研")
        self.client.post("/api/profile/confirm", headers=self._hdr())
        block = api._profile_block("最近怎么样", "jia")
        self.assertIsNotNone(block)
        self.assertIn("考研", block)

    def test_profile_block_failure_does_not_break_the_chat(self):
        """记忆坏了绝不能把聊天带崩 —— 它是加分项，不是主链路。"""
        api = self.api

        def boom(_user_id):
            raise RuntimeError("模拟档案损坏")

        original = api._profile_store_for
        api._profile_store_for = boom
        try:
            self.assertIsNone(api._profile_block("你好", "jia"))
        finally:
            api._profile_store_for = original


if __name__ == "__main__":
    unittest.main()
