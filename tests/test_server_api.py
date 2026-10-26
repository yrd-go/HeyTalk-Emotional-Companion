# -*- coding: utf-8 -*-
"""
tests/test_server_api.py —— HTTP 后端（换前端的第一步）
================================================================

这一层是新加的，所以必须把它自己的红线也钉住。**最关键的三条**：

    ① 高危 → 固定话术，**零模型调用**（自伤表述绝不外发）；
    ② 中危 → 出口必须带求助通道；
    ③ 接口契约（bootstrap / reset / icon / 首页）不能被改坏。

用 FastAPI 的 TestClient（底层是 starlette 的测试客户端，不需要起服务器），
所以这些用例在受限环境里也能跑。

    python tests/run_all.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from starlette.testclient import TestClient  # type: ignore
    HAS_TESTCLIENT = True
    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - 取决于环境
    TestClient = None  # type: ignore[assignment]
    HAS_TESTCLIENT = False
    IMPORT_ERROR = exc

from server import auth  # noqa: E402

SKIP_REASON = f"缺少 starlette 测试客户端，跳过 HTTP 层用例（{IMPORT_ERROR}）"


def _uuid() -> str:
    """一个合法的会话 id。

    服务端对 id 是严格白名单（它会被用于拼文件路径，防 `../` 穿越），
    所以测试里不能随手写 "s-ok" 这种字符串 —— 那样落盘会失败，
    虽然不影响断言，但会在 stderr 刷一堆"落盘失败"，掩盖真正的问题。
    """
    from chat.history import new_session_id

    return new_session_id()


def _sse_frames(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体拆成 [(事件名, payload), ...]。"""
    frames: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            try:
                frames.append((event, json.loads(data)))
            except json.JSONDecodeError:
                continue
    return frames


@unittest.skipUnless(HAS_TESTCLIENT, SKIP_REASON)
class ServerTestBase(unittest.TestCase):
    """每个用例一个干净的 app 实例，避免内存会话互相污染。"""

    def setUp(self) -> None:
        import importlib
        import tempfile

        import server.api as api_module  # type: ignore

        importlib.reload(api_module)          # 清掉 _CONVERSATIONS
        self.api = api_module

        # ★ 把历史重定向到临时目录：绝不能拿用户真实的 data/chat_history 做测试
        #   （那里面是真实的心理敏感对话，测试往里写东西是不可接受的）
        self._tmp = tempfile.TemporaryDirectory(prefix="heytalk-api-")
        self.api._history = api_module.HistoryStore(self._tmp.name)

        # ★★ 用户表与令牌表也必须重定向到临时目录。
        #    为什么这一条是硬要求（不是可选的洁癖）：
        #      服务端有个刻意的设计 —— **没配置用户时不需要登录**。
        #      所以整套 HTTP 测试原本依赖"data/users.json 不存在"这个前提。
        #      而主人在真实使用中**一定会有**这个文件（多用户模式要它），
        #      一旦有了，这里的每个用例都会突然变成 401 —— 我实际撞到过，
        #      一次 16 个失败，看起来像"代码坏了"，其实是测试没有隔离配置。
        #    重定向之后，无论主人是否配了账号，测试结果都一样。
        self._orig_auth = (api_module.USERS_FILE, api_module.SESSIONS_FILE)
        api_module.USERS_FILE = Path(self._tmp.name) / "users.json"
        api_module.SESSIONS_FILE = Path(self._tmp.name) / "sessions.json"
        api_module._HISTORY_BY_USER.clear()
        api_module._CONVERSATIONS.clear()

        self.client = TestClient(api_module.app)

    def tearDown(self) -> None:
        self.api.USERS_FILE, self.api.SESSIONS_FILE = self._orig_auth
        self.api._HISTORY_BY_USER.clear()
        self._tmp.cleanup()

    def _sid(self) -> str:
        """生成一个**合法**的会话 id。

        服务端对 id 是严格白名单（因为它会用于拼文件路径，防 `../` 穿越），
        所以测试里不能随手写 "persist-1" 这种字符串 —— 那样根本存不进去，
        会得到一堆假失败。
        """
        return self.api.new_session_id()

    def _install_canary(self) -> dict:
        """把 make_model 换成"被调用就记一笔"的替身。"""
        calls: list[str] = []

        class Canary:
            def bind_tools(self, tools):
                calls.append("bind_tools")
                return self

            def invoke(self, messages):
                calls.append("invoke")
                return {"content": "", "tool_calls": []}

            def stream(self, messages):
                calls.append("stream")
                yield type("C", (), {"content": "我听着呢。"})()

        self.api.con.make_model = lambda *a, **k: Canary()
        return {"calls": calls}


class TestSafetyInvariants(ServerTestBase):
    """★ 红线：安全判定永远先于模型，高危零调用。"""

    def test_high_risk_never_calls_model(self):
        spy = self._install_canary()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "我不想活了",
        })
        self.assertEqual(res.status_code, 200)
        frames = _sse_frames(res.text)

        self.assertEqual(spy["calls"], [],
                         f"高危路径竟然调用了模型：{spy['calls']}")

        events = [name for name, _ in frames]
        self.assertIn("done", events)
        joined = "".join(p.get("text", "") for _, p in frames if "text" in p)
        self.assertIn("12356", joined, "高危回应必须带真实求助热线")

        metas = [p for name, p in frames if name == "meta"]
        self.assertTrue(metas and metas[0].get("safety") == "high",
                        f"meta 里应标注高危：{metas}")
        self.assertTrue(metas[0].get("fixed_reply"), "高危必须是固定话术")

    def test_normal_turn_streams_and_reports_elapsed(self):
        self._install_canary()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "今天有点累",
        })
        frames = _sse_frames(res.text)
        joined = "".join(p.get("text", "") for _, p in frames if "text" in p)
        self.assertIn("我听着呢", joined)

        done = [p for name, p in frames if name == "done"]
        self.assertTrue(done and done[0].get("ok"), f"没有正常收尾：{frames[-3:]}")
        self.assertIn("elapsed_ms", done[0])

    def test_medium_risk_reply_must_keep_resources(self):
        """中危：模型没给求助通道时，出口校验必须补上。"""
        class NoResourceModel:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                return {"content": "", "tool_calls": []}

            def stream(self, messages):
                yield type("C", (), {"content": "我明白，这种感觉很难熬。"})()

        self.api.con.make_model = lambda *a, **k: NoResourceModel()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "活着没什么意思，感觉自己是负担",
        })
        joined = "".join(p.get("text", "") for _, p in _sse_frames(res.text)
                         if "text" in p)
        self.assertIn("12356", joined,
                      f"中危回应的出口没补上求助通道：{joined[:120]}")


class TestScaffoldWiring(ServerTestBase):
    """★ 支架默认关闭，且只在普通对话分支注入。"""

    def _capture(self, message: str, *, enabled: bool = True):
        self._install_canary()
        captured: dict = {}
        scaffold_calls: list[str] = []
        original_assemble = self.api.con.assemble_chat_messages
        original_scaffold = self.api._scaffold_block

        def scaffold_spy(text):
            scaffold_calls.append(text)
            return original_scaffold(text)

        def assemble_spy(session, **kwargs):
            captured.update(kwargs)
            return original_assemble(session, **kwargs)

        self.api._scaffold_block = scaffold_spy
        self.api.con.assemble_chat_messages = assemble_spy
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                if enabled:
                    os.environ.pop("HEYTALK_INTENT_SCAFFOLD", None)
                else:
                    os.environ["HEYTALK_INTENT_SCAFFOLD"] = "0"
                res = self.client.post("/api/chat", json={
                    "session_id": self._sid(), "message": message,
                })
        finally:
            self.api._scaffold_block = original_scaffold
            self.api.con.assemble_chat_messages = original_assemble
        self.assertEqual(res.status_code, 200)
        return captured, scaffold_calls

    def test_translation_turn_gets_its_own_scaffold(self):
        captured, calls = self._capture("翻译成拉丁文：愿我们都能平安回家")
        self.assertEqual(calls, ["翻译成拉丁文：愿我们都能平安回家"])
        block = captured.get("scaffold_instruction")
        self.assertIsNotNone(block)
        self.assertIn("准确译文", block)

    def test_knowledge_turn_gets_no_scaffold(self):
        captured, calls = self._capture("为什么天空是蓝色的？")
        self.assertEqual(calls, ["为什么天空是蓝色的？"])
        self.assertIsNone(captured.get("scaffold_instruction"))

    def test_emotional_turn_gets_no_scaffold(self):
        captured, calls = self._capture("周末一个人在家，突然觉得有点空")
        self.assertEqual(calls, ["周末一个人在家，突然觉得有点空"])
        self.assertIsNone(captured.get("scaffold_instruction"))

    def test_scaffold_can_be_switched_off(self):
        captured, calls = self._capture(
            "翻译成拉丁文：愿我们都能平安回家", enabled=False)
        self.assertEqual(calls, ["翻译成拉丁文：愿我们都能平安回家"])
        self.assertIsNone(captured.get("scaffold_instruction"))

    def test_high_risk_turn_never_reaches_scaffold_or_prompt(self):
        captured, calls = self._capture("我不想活了")
        self.assertEqual(calls, [])
        self.assertEqual(captured, {})

    def test_clear_memory_turn_never_reaches_scaffold_or_prompt(self):
        captured, calls = self._capture("把我之前说过的所有事都忘掉")
        self.assertEqual(calls, [])
        self.assertEqual(captured, {})


class TestDialoguePlannerWiring(ServerTestBase):
    def test_self_worth_followup_gets_continuity_instruction(self):
        history = [
            {"role": "user", "content": "感觉自己没什么用"},
            {"role": "assistant", "content": "是今天有什么事勾出来的吗？"},
        ]
        with mock.patch.dict(os.environ, {"HEYTALK_DIALOGUE_PLANNER": "1"}, clear=False):
            block = self.api._instruction_block("因为我没有法拉利", history)
        self.assertIsNotNone(block)
        self.assertIn("对话背景", block)
        self.assertIn("感觉自己没什么用", block)

    def test_dialogue_planner_is_off_by_default(self):
        history = [
            {"role": "user", "content": "感觉自己没什么用"},
            {"role": "assistant", "content": "是今天有什么事勾出来的吗？"},
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEYTALK_DIALOGUE_PLANNER", None)
            block = self.api._instruction_block("因为我没有法拉利", history)
        self.assertIsNone(block)

    def test_translation_after_self_worth_does_not_force_old_thread(self):
        history = [{"role": "user", "content": "感觉自己没什么用"}]
        block = self.api._instruction_block("翻译成拉丁文：平安", history)
        self.assertIsNotNone(block)
        self.assertNotIn("对话背景", block)
        self.assertIn("准确译文", block)


class TestContracts(ServerTestBase):
    """接口契约。"""

    def test_bootstrap_lists_engines_and_safety_info(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertIn("engines", data)
        self.assertIn("opening_suggestions", data)
        self.assertTrue(data["opening_suggestions"], "开场建议不能为空")
        self.assertIn("12356", data["hotlines_markdown"],
                      "bootstrap 必须带上求助热线（前端侧边栏要显示）")
        for engine in data["engines"]:
            self.assertIn("id", engine)
            self.assertIn("label", engine)

    def test_bootstrap_suggestions_follow_recent_user_topic(self):
        sid = self._sid()
        self.api._history.save(sid, turns=[
            {"role": "user", "content": "最近准备考研压力很大，晚上总睡不着"},
            {"role": "assistant", "content": "听起来这阵子挺难的。"},
        ], state={})

        data = self.client.get("/api/bootstrap").json()
        self.assertIn("考研", "\n".join(data["opening_suggestions"]))

    def test_index_serves_html(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertIn("Heytalk", res.text)

    def test_index_redirects_to_app_when_auth_on_and_anonymous(self):
        auth.set_user(self.api.USERS_FILE, "me", "secret123",
                      name="管理员", is_admin=True)
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/app")

    def test_index_always_redirects_to_react_app(self):
        """Native page retired: even a logged-in user lands on /app."""
        auth.set_user(self.api.USERS_FILE, "me", "secret123",
                      name="admin", is_admin=True)
        login = self.client.post(
            "/api/login", json={"user_id": "me", "password": "secret123"})
        token = login.json()["token"]
        res = self.client.get(
            "/", headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/app")

    def test_icon_is_served_as_png(self):
        res = self.client.get("/api/icon")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content[:8], b"\x89PNG\r\n\x1a\n", "图标不是合法 PNG")

    def test_wordmark_is_text_only(self):
        """词标要能取到，而且**只含文字**（不含图标）。

        为什么强调"不含图标"：侧边栏里图标是单独放的 `<img>`，
        如果词标图里也画了图标，页面上会出现两个心（实跑踩过）。
        校验方式：文字版应当比"图标 + 文字"的完整版**窄**。
        """
        import io as _io

        res = self.client.get("/api/wordmark")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content[:8], b"\x89PNG\r\n\x1a\n")

        from PIL import Image  # Pillow 是 Streamlit 的依赖，环境里一定有

        with Image.open(_io.BytesIO(res.content)) as plain:
            plain_w = plain.width
        full_path = PROJECT_ROOT / "assets" / "icon" / "wordmark.png"
        self.assertTrue(full_path.is_file(), "缺少含图标的完整词标")
        with Image.open(full_path) as full:
            self.assertLess(plain_w, full.width,
                            "文字版词标不该和含图标版一样宽（说明图标没去掉）")

    def test_unreachable_ollama_is_not_offered(self):
        """★ 连不上的模型不该出现在列表里。

        修过的真 bug：`available_engines()` 对 Ollama 永远返回 True
        （因为它**不需要 Key**，而那个函数只查 Key 与依赖）。于是选择器里
        出现一个根本连不上的选项，用户一切过去就吃 OpenAIConnectionError，
        看起来像程序坏了。现在只列"真的在监听"的 Ollama。
        """
        data = self.client.get("/api/bootstrap").json()
        ids = [e["id"] for e in data["engines"]]
        # 测试环境里 Ollama 通常没跑；如果恰好跑着，那它出现是合理的
        if not self.api._ollama_reachable():
            self.assertNotIn("ollama", ids,
                             f"Ollama 没在跑却出现在列表里：{ids}")
        self.assertNotIn(None, ids, "引擎列表里不该出现 None")


class TestHistory(ServerTestBase):
    """★ 对话记录：主人最初最在意的一条（"一刷新就没有记录"）。"""

    def _chat(self, sid: str, message: str) -> None:
        self._install_canary()
        res = self.client.post("/api/chat", json={
            "session_id": sid, "message": message,
        })
        self.assertEqual(res.status_code, 200)

    def test_new_session_returns_a_valid_id(self):
        res = self.client.post("/api/history/new")
        self.assertEqual(res.status_code, 200)
        sid = res.json()["session_id"]
        self.assertTrue(sid, "新会话必须返回 id")
        self.assertLessEqual(len(sid), self.api.MAX_SESSION_ID_CHARS)
        # 这个 id 必须是历史层认可的形式，否则后面存不进去
        from chat.history import validate_session_id

        self.assertEqual(validate_session_id(sid), sid)

    def test_turn_is_persisted_and_reloadable(self):
        """一轮对话之后，记录必须落盘并且能读回来。"""
        sid = self._sid()
        self._chat(sid, "今天有点累")
        listing = self.client.get("/api/history").json()["sessions"]
        ids = [s["session_id"] for s in listing]
        self.assertIn(sid, ids, f"这轮没有落盘：{ids}")

        detail = self.client.get(f"/api/history/{sid}").json()
        roles = [t["role"] for t in detail["turns"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        contents = " ".join(t["content"] for t in detail["turns"])
        self.assertIn("今天有点累", contents)

    def test_high_risk_turn_is_also_persisted(self):
        """高危轮次也要落盘（否则刷新后用户看不到那句安全回应）。"""
        sid = self._sid()
        self._chat(sid, "我不想活了")
        detail = self.client.get(f"/api/history/{sid}").json()
        joined = " ".join(t["content"] for t in detail["turns"])
        self.assertIn("12356", joined)
        self.assertTrue(any(t["fixed_reply"] for t in detail["turns"]),
                        "固定话术的标记没有存下来")

    def test_delete_removes_the_record(self):
        sid = self._sid()
        self._chat(sid, "随便说一句")
        self.assertIn(sid, [s["session_id"] for s in
                            self.client.get("/api/history").json()["sessions"]])

        res = self.client.request("DELETE", f"/api/history/{sid}")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(sid, [s["session_id"] for s in
                               self.client.get("/api/history").json()["sessions"]])
        self.assertEqual(self.client.get(f"/api/history/{sid}").status_code, 404)

    def test_unknown_session_returns_404(self):
        self.assertEqual(
            self.client.get(f"/api/history/{self._sid()}").status_code, 404)

    def test_illegal_session_id_is_rejected(self):
        """★ 会话 id 是安全边界：形如 `../` 的 id 必须被拒绝。

        它会被用于拼文件路径，所以服务端是严格白名单。
        """
        for bad in ("../etc/passwd", "a", "x" * 100):
            detail = self.client.get(f"/api/history/{bad}")
            self.assertIn(detail.status_code, (404, 400),
                          f"非法 id {bad!r} 没有被拒绝")

    def test_history_records_written_to_the_temp_dir_not_real_data(self):
        """确认历史写到了临时目录（绝不动用户真实数据）。"""
        self._chat(self._sid(), "测试写入")
        files = list(Path(self._tmp.name).glob("*.json"))
        self.assertTrue(files, "没有写到临时目录")
        for f in files:
            self.assertIn(self._tmp.name, str(f))


class TestRegenerateAnyTurn(ServerTestBase):
    """★ 任何一条历史回复都能重载，而不只是最后一条。"""

    def _chat(self, sid: str, message: str) -> None:
        self._install_canary()
        res = self.client.post("/api/chat", json={
            "session_id": sid,
            "message": message,
            "use_roles": False,
            "use_knowledge": False,
            "use_tools": False,
        })
        self.assertEqual(res.status_code, 200)

    def _seed_two_turns(self, sid: str) -> None:
        self._chat(sid, "第一句")
        self._chat(sid, "第二句")
        conv = self.api._CONVERSATIONS[sid]
        self.assertEqual([t.role for t in conv.session.turns],
                         ["user", "assistant", "user", "assistant"])

    def test_regenerate_earlier_reply_replaces_in_place_and_keeps_later_turns(self):
        sid = self._sid()
        self._seed_two_turns(sid)

        res = self.client.post("/api/chat", json={
            "session_id": sid,
            "message": "",
            "regenerate": True,
            "regenerate_index": 1,
            "use_roles": False,
            "use_knowledge": False,
            "use_tools": False,
        })
        self.assertEqual(res.status_code, 200)

        turns = self.api._CONVERSATIONS[sid].session.turns
        self.assertEqual([t.role for t in turns],
                         ["user", "assistant", "user", "assistant"],
                         "重载历史回复时不能删除或追加后面的对话")
        self.assertEqual(turns[0].content, "第一句",
                         "重载历史回复时必须使用它对应的那条用户消息")
        self.assertEqual(turns[2].content, "第二句",
                         "后面的用户消息被改动了")
        self.assertEqual(turns[3].content, "我听着呢。",
                         "后面的助手回复被改动了")
        done = [p for event, p in _sse_frames(res.text) if event == "done"]
        self.assertTrue(done and done[-1].get("ok"),
                        f"重载没有正常完成：{done}")

    def test_regenerate_without_index_still_uses_last_reply(self):
        sid = self._sid()
        self._seed_two_turns(sid)

        res = self.client.post("/api/chat", json={
            "session_id": sid,
            "message": "",
            "regenerate": True,
            "use_roles": False,
            "use_knowledge": False,
            "use_tools": False,
        })
        self.assertEqual(res.status_code, 200)
        turns = self.api._CONVERSATIONS[sid].session.turns
        self.assertEqual([t.content for t in turns[:2]], ["第一句", "我听着呢。"])
        self.assertEqual([t.role for t in turns], ["user", "assistant", "user", "assistant"])

    def test_invalid_regenerate_index_does_not_mutate_conversation(self):
        sid = self._sid()
        self._seed_two_turns(sid)
        before = [(t.role, t.content) for t in self.api._CONVERSATIONS[sid].session.turns]

        for bad_index in (0, -1, 4):
            res = self.client.post("/api/chat", json={
                "session_id": sid,
                "message": "",
                "regenerate": True,
                "regenerate_index": bad_index,
            })
            self.assertEqual(res.status_code, 400)
            after = [(t.role, t.content)
                     for t in self.api._CONVERSATIONS[sid].session.turns]
            self.assertEqual(after, before, f"非法序号 {bad_index} 改动了对话")


class TestRequestValidation(ServerTestBase):
    """参数校验与清空接口。"""

    def test_empty_message_is_rejected(self):
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "   ",
        })
        self.assertEqual(res.status_code, 400)

    def test_too_long_message_is_rejected(self):
        """超长消息要被校验拦下（防止有人拿它当免费大模型用）。"""
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "啊" * (self.api.MAX_MESSAGE_CHARS + 10),
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("太长", res.json()["error"])

    def test_missing_session_id_is_rejected(self):
        res = self.client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("session_id", res.json()["error"])

    def test_reset_does_not_require_a_message(self):
        """清空对话的接口不该被要求带一条假消息。"""
        res = self.client.post("/api/reset", json={"session_id": self._sid()})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    def test_sessions_are_isolated(self):
        """两个 session_id 的历史不能互相看到。"""
        self._install_canary()
        sid_a, sid_b = self._sid(), self._sid()
        self.client.post("/api/chat", json={
            "session_id": sid_a, "message": "我只跟 A 说过这件事",
        })
        conv_a = self.api.get_conversation(sid_a)
        conv_b = self.api.get_conversation(sid_b)
        self.assertEqual(len(conv_b.session.turns), 0,
                         "B 会话不该拿到 A 的内容")
        self.assertGreater(len(conv_a.session.turns), 0)


class TestFrontendWiring(ServerTestBase):
    """★ 前端 HTML 与 JS 的接线自检（纯静态，不需要浏览器）。

    加这一组的原因是一个真实事故：
        我误删了侧边栏折叠按钮（主人说"去掉重复的那个"，我删错了），
        后来把按钮**加回来却忘了把点击监听加回来** —— 于是按钮点了没反应。
        更糟的是**我的截图验证看不见它**：截图用的是 `?collapsed=1`，
        那条路直接调函数、绕过了点击事件。
        主人点了一下才发现。

    这类"控件存在但没接线"的 bug，用两个静态检查就能挡住：
        ① 每个 `<button id="X">` 都必须在 JS 里被引用过；
        ② JS 里 `$("X")` 引用的每个 id 都必须在 HTML 里存在。
    """

    HTML_PATH = PROJECT_ROOT / "src" / "server" / "static" / "index.html"

    def setUp(self) -> None:
        super().setUp()
        self.html = self.HTML_PATH.read_text(encoding="utf-8")

    def test_every_button_id_is_wired_in_js(self):
        """每个按钮的 id 都必须在 JS 里出现 —— 否则它点了不会有任何反应。"""
        import re

        button_ids = set(re.findall(r'<button[^>]*\bid="([^"]+)"', self.html))
        self.assertTrue(button_ids, "前端竟然没有按钮？")
        for bid in sorted(button_ids):
            self.assertRegex(
                self.html, rf'\$\("{re.escape(bid)}"\)',
                f"按钮 #{bid} 没有在 JS 里被引用 —— 点了不会有反应")

    def test_every_referenced_id_exists_in_html(self):
        """JS 引用的 id 必须真实存在（否则是拼错的死引用）。"""
        import re

        defined = set(re.findall(r'\bid="([^"]+)"', self.html))
        # JS 里动态创建的 id 也算
        script = self.html.split("<script>")[-1]
        defined |= set(re.findall(r'id="([^"]+)"', script))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', self.html))
        missing = sorted(referenced - defined)
        self.assertEqual(missing, [],
                         f"JS 引用了不存在的 id：{missing}")

    def test_dev_screenshot_hooks_click_real_buttons(self):
        """截图钩子必须**点真实按钮**，不能直接调函数。

        这是安全网的一部分：直接调函数会绕过监听器，
        于是"按钮没接线"这类 bug 在截图里看不出来（我踩过）。
        """
        script = self.html.split("<script>")[-1]
        self.assertIn("devClick", script,
                      "截图钩子应当通过 devClick 点真实元素")
        # 钩子里不该再出现直接调用 toggleSidebar 的形式
        self.assertNotRegex(
            script, r'devParams\.get\("collapsed"\)[^\n]*toggleSidebar\(',
            "截图钩子又变回直接调函数了 —— 那样会绕过点击事件")


class TestTimeoutResilience(ServerTestBase):
    """★ 超时/断流时的重试判据：**只在"一个字都没吐过"时才重试**。

    依据（2026-10-15 的实测）：DeepSeek 过载期单次响应 **141~362 秒**，
    三次里还有一次直接超时。所以失败是常态，**需要重试**。

    但这是**流式**接口 —— 如果用户已经看到半句话，重试会把话说两遍，
    **那比慢更糟**（用户会以为它错乱了）。所以判据是"有没有吐过字"，
    而不是"有没有出错"。

    这两条测试守的就是这个分界：一个允许重试，一个**禁止**重试。
    """

    def _install_flaky(self, *, fail_times: int, emit_then_fail: bool) -> dict:
        stats = {"stream_calls": 0}

        class Flaky:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                return {"content": "", "tool_calls": []}

            def stream(self, messages):
                stats["stream_calls"] += 1
                n = stats["stream_calls"]
                if emit_then_fail:
                    # 先说半句再断 —— 模拟"流到一半网络掉了"
                    yield type("C", (), {"content": "话说了一半"})()
                    raise RuntimeError("stream broke mid-way")
                if n <= fail_times:
                    raise TimeoutError("simulated timeout")
                yield type("C", (), {"content": "这次说出来了。"})()

        self.api.con.make_model = lambda *a, **k: Flaky()
        return stats

    def test_retries_when_nothing_was_emitted(self):
        """一个字都没收到 → 应当重试，并且真的拿到第二次的回复。"""
        stats = self._install_flaky(fail_times=1, emit_then_fail=False)
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "你好",
        })
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(stats["stream_calls"], 2,
                                "零输出时应当重试 —— 否则过载期每次都会失败")
        frames = _sse_frames(res.text)
        joined = "".join(p.get("text", "") for _, p in frames if "text" in p)
        self.assertIn("这次说出来了", joined, "重试后应当拿到正常回复")
        metas = [p for name, p in frames if name == "meta"]
        self.assertTrue(any(m.get("retrying") for m in metas),
                        f"应当告诉前端正在重试：{metas}")

    def test_does_not_retry_after_partial_output(self):
        """★ 已经吐过字 → **绝不能**重试。

        否则用户会看到同一句话说两遍 —— 比慢更糟。
        """
        stats = self._install_flaky(fail_times=0, emit_then_fail=True)
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "你好",
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(stats["stream_calls"], 1,
                         "★ 半句之后重试了 —— 用户会看到话说两遍")
        frames = _sse_frames(res.text)
        joined = "".join(p.get("text", "") for _, p in frames if "text" in p)
        self.assertIn("话说了一半", joined, "已经说出的半句必须保留")

    def test_timeout_message_is_actionable_not_a_raw_exception_name(self):
        """超时话术必须**能行动**，不能只把异常名丢给用户。

        改动前用户看到的是 `OpenAITimeoutError` —— 那是给我们看的。
        而用户其实有办法：换引擎（实测 GLM 比过载期的 DeepSeek 快 100 倍以上）。
        """
        self._install_flaky(fail_times=99, emit_then_fail=False)
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "你好",
        })
        frames = _sse_frames(res.text)
        joined = "".join(p.get("text", "") for _, p in frames if "text" in p)
        self.assertNotIn("OpenAITimeoutError", joined,
                         "不该把原始异常名显示给用户")
        self.assertIn("GLM", joined, "错误信息里要给出「换个模型」这条出路")


class TestToolWiring(ServerTestBase):
    """★ 服务端必须真的**去调**工具层。

    加这组的原因是一个真实事故，而且是第三次同类事故：
        换前端时我把工具阶段整个漏掉了（`run_tool_phase` 只在 Streamlit 版里
        被调用）。于是用户问"深圳天气"，模型回答"我没有实时天气的工具" ——
        **那是真话，它确实没有**。
        最要命的是：**单元测试测工具函数会全绿**，因为函数一直好好的，
        丢的是"服务端有没有去调它"这一环。所以必须从 HTTP 层面测。
    """

    def _spy_tool_phase(self) -> dict:
        """把 run_tool_phase 换成间谍：被调用就记一笔，并返回空结果。"""
        calls: dict = {"n": 0, "messages": None}
        convo = self.api.con
        original = convo.run_tool_phase

        def fake(model, messages, **kw):
            calls["n"] += 1
            calls["messages"] = messages
            return convo.ToolPhase()

        convo.run_tool_phase = fake

        def restore() -> None:
            # ★ 只还原这一个属性，**不要 reload 整个模块**。
            #   我第一版用 importlib.reload，单独跑没问题、全量跑就报错：
            #   reload 会重新执行模块顶层代码，把别的测试已经替换掉的
            #   东西（以及类身份）一起冲掉，属于"为了收拾自己而踩别人"。
            convo.run_tool_phase = original

        self.addCleanup(restore)
        return calls

    def _chat(self, message: str, **extra) -> str:
        sid = self._sid()
        self.client.post("/api/history/new")   # 本机模式不需要令牌
        body = {"session_id": sid, "message": message,
                "engine": "deepseek", **extra}
        res = self.client.post("/api/chat", json=body)
        self.assertEqual(res.status_code, 200, res.text)
        return res.text

    def test_server_runs_the_tool_phase(self):
        """默认应当调用工具阶段（否则天气/翻译就是坏的）。"""
        calls = self._spy_tool_phase()
        self._chat("深圳现在是什么天气？")
        self.assertGreater(calls["n"], 0,
                           "服务端没有调用 run_tool_phase —— 天气/翻译会失效")

    def test_tools_can_be_switched_off(self):
        """关掉 tools 就不该调 —— 这是省一次模型调用的开关。"""
        calls = self._spy_tool_phase()
        self._chat("深圳现在是什么天气？", use_tools=False)
        self.assertEqual(calls["n"], 0)

    def test_emotional_message_skips_the_tool_phase(self):
        """情绪倾诉没有查询意图：不要先付一次必然为空的工具判定。"""
        calls = self._spy_tool_phase()
        self._chat("工作快撑不住了")
        self.assertEqual(calls["n"], 0,
                         "情绪消息不该进入工具判定，这会白等约 2.5 秒")

    def test_lookup_with_emotional_words_keeps_the_tool_phase(self):
        """同一句里既有情绪又有天气/翻译需求时，工具阶段不能被跳掉。"""
        calls = self._spy_tool_phase()
        self._chat("我今天有点累，北京现在天气怎么样")
        self.assertGreater(calls["n"], 0,
                           "带查询意图的消息仍必须进入工具阶段")

    def test_high_risk_never_touches_tools(self):
        """★ 高危时连工具阶段都不能发生。

        安全红线是"HIGH ⇒ 零模型调用"。工具阶段内部要 invoke 一次模型，
        所以它必须排在高危判定**之后** —— 顺序错了就是红线破了。
        """
        calls = self._spy_tool_phase()
        body = self._chat("我想死了")
        self.assertEqual(calls["n"], 0,
                         "高危时不该进入工具阶段（那会发起模型调用）")
        self.assertIn("12356", body, "高危话术必须带求助热线")

    def test_tool_status_is_forwarded_to_sse(self):
        """C：工具状态要透出到 HTTP 层，前端才能显示降级提示。"""
        convo = self.api.con
        original = convo.run_tool_phase

        def fake(_model, _messages, **_kw):
            return convo.ToolPhase(calls=[convo.ToolCall(
                name="translate_text",
                arguments={"text": "超标", "target_lang": "迈蒂利语"},
                result="参考译法，未经母语者核对",
                status="unsupported-language",
            )])

        convo.run_tool_phase = fake
        self.addCleanup(lambda: setattr(convo, "run_tool_phase", original))
        body = self._chat("把超标翻译成迈蒂利语")
        frames = _sse_frames(body)
        tool_payloads = [payload for event, payload in frames if event == "tools"]
        self.assertTrue(tool_payloads, "没有发出 tools 事件")
        self.assertEqual(tool_payloads[0]["statuses"][0]["status"],
                         "unsupported-language")


@unittest.skipUnless(HAS_TESTCLIENT, SKIP_REASON)
class TestLatencyRouting(ServerTestBase):
    """2026-10-16 提速改动：思考可见 + 查询类快速通道。

    这两条是"不损伤能力地把响应变快"的骨架，必须有独立断言钉住：
      ① DeepSeek 的 reasoning_content 要以 thinking 事件流到前端；
      ② 查询/工具类问题走快速引擎，情感陪伴类留在推理引擎；
      ③ 路由可以被 HEYTALK_AUTO_ROUTING 关掉（如实可审计）。
    """

    def setUp(self) -> None:
        super().setUp()
        self._saved_env = {
            var: os.environ.get(var)
            for var in ("DEEPSEEK_API_KEY", "HEYTALK_AUTO_ROUTING")
        }
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        os.environ["HEYTALK_AUTO_ROUTING"] = "1"

    def tearDown(self) -> None:
        for var, value in self._saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        super().tearDown()

    class _ThinkingModel:
        """带 reasoning_content 的原始 SDK 假流（langchain-openai 1.6.6
        会丢 reasoning_content，所以正确实现是走 model.client 直读）。"""

        def __init__(self) -> None:
            self.client = SimpleNamespace(create=self._create)

        def _get_request_payload(self, messages):  # noqa: ANN001
            return {"model": "x", "messages": messages}

        def _create(self, **kwargs):  # noqa: ANN001, ANN003
            assert kwargs.get("stream") is True

            def chunk(reasoning="", content=""):
                delta = SimpleNamespace(reasoning_content=reasoning,
                                        content=content)
                return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

            yield chunk(reasoning="先想想")
            yield chunk(content="我听着呢。")

        def stream(self, messages):  # noqa: ANN001
            yield SimpleNamespace(content="fallback")

    class _FastModel:
        def stream(self, messages):  # noqa: ANN001
            yield SimpleNamespace(content="查到了。")

    def _empty_tools(self):
        # ★ 必须还原：这是模块级状态，漏还原会污染后续所有用例。
        #   （新加的 DSML 工具调用测试就是在全量跑时被这里坑到的。）
        original = self.api.con.run_tool_phase
        self.api.con.run_tool_phase = lambda *a, **k: self.api.con.ToolPhase()
        self.addCleanup(
            lambda: setattr(self.api.con, "run_tool_phase", original))

    def test_reasoning_streams_as_thinking_events(self):
        """思考过程必须流到前端，而不是被丢掉。"""
        self.api.con.make_model = lambda *a, **k: self._ThinkingModel()
        self._empty_tools()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "今天好累"})
        self.assertEqual(res.status_code, 200, res.text)
        frames = _sse_frames(res.text)
        think = [p.get("text") for n, p in frames if n == "thinking"]
        self.assertEqual(think, ["先想想"],
                         "reasoning_content 应作为 thinking 事件透传")
        deltas = "".join(p.get("text", "") for n, p in frames if n == "delta")
        self.assertEqual(deltas, "我听着呢。")
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("first_token_ms") is not None,
                        "思考先到 → first_token_ms 应记下首字时刻")

    def test_lookup_question_routes_to_fast_engine(self):
        """查询类问题默认换快引擎，并如实发 routed 标记。"""
        calls: list[str] = []
        self.api.con.make_model = lambda engine, **kw: (calls.append(engine)
                                                        or self._FastModel())
        self._empty_tools()
        # Routing only happens when the fast engine is available. Simulate that
        # precondition instead of turning a missing local key into a false failure.
        ready = {"deepseek": True, "deepseek-pro": True,
                 "zhipu": True, "ollama": False}
        with mock.patch.object(self.api.con, "available_engines",
                               return_value=ready):
            res = self.client.post("/api/chat", json={
                "session_id": self._sid(), "message": "什么是情绪价值",
                "engine": "deepseek-pro"})
        frames = _sse_frames(res.text)
        self.assertIn("deepseek", calls,
                      f"查询类问题应调用快速引擎：{calls}")
        metas = [p for n, p in frames if n == "meta"]
        self.assertTrue(any(m.get("phase") == "routed" for m in metas),
                        f"应当如实标注快速通道：{metas}")
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("engine") == "deepseek")

    def test_emotional_question_keeps_reasoning_engine(self):
        """陪伴类问题留在推理引擎 —— 快了反而像敷衍（CHI 2026）。"""
        self.api.con.make_model = lambda *a, **k: self._FastModel()
        self._empty_tools()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "今天好累啊",
            "engine": "deepseek-pro"})
        frames = _sse_frames(res.text)
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("engine") == "deepseek-pro",
                        "情感陪伴不该被路由到快引擎")

    def test_routing_can_be_disabled(self):
        """路由开关必须真的有效（用户可关掉自动化，保持透明）。"""
        os.environ["HEYTALK_AUTO_ROUTING"] = "0"
        self.api.con.make_model = lambda *a, **k: self._FastModel()
        self._empty_tools()
        res = self.client.post("/api/chat", json={
            "session_id": self._sid(), "message": "什么是情绪价值",
            "engine": "deepseek-pro"})
        frames = _sse_frames(res.text)
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("engine") == "deepseek-pro",
                        "关掉路由后应留在原引擎")
        metas = [p for n, p in frames if n == "meta"]
        self.assertFalse(any(m.get("phase") == "routed" for m in metas))

    def test_thinking_pill_selects_pro_and_skips_lookup_routing(self):
        """思考开启时必须留在 Pro，查询类问题也不能偷偷换成 Flash。"""
        calls: list[str] = []
        self.api.con.make_model = lambda engine, **kw: (calls.append(engine)
                                                        or self._FastModel())
        self._empty_tools()
        ready = {"deepseek": True, "deepseek-pro": True,
                 "zhipu": True, "ollama": False}
        with mock.patch.object(self.api.con, "available_engines",
                               return_value=ready):
            res = self.client.post("/api/chat", json={
                "session_id": self._sid(), "message": "什么是情绪价值",
                "thinking": True, "use_tools": False,
            })
        self.assertEqual(res.status_code, 200, res.text)
        frames = _sse_frames(res.text)
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("engine") == "deepseek-pro")
        self.assertNotIn("deepseek", calls)

    def test_thinking_off_selects_flash_by_default(self):
        """未点思考时最终回答走 Flash；Pro 只负责开启思考后的深答。"""
        self.api.con.make_model = lambda *a, **k: self._FastModel()
        self._empty_tools()
        ready = {"deepseek": True, "deepseek-pro": True,
                 "zhipu": True, "ollama": False}
        with mock.patch.object(self.api.con, "available_engines",
                               return_value=ready):
            res = self.client.post("/api/chat", json={
                "session_id": self._sid(), "message": "今天有点累",
                "thinking": False,
            })
        self.assertEqual(res.status_code, 200, res.text)
        frames = _sse_frames(res.text)
        done = [p for n, p in frames if n == "done"]
        self.assertTrue(done and done[0].get("engine") == "deepseek",
                        f"未启用思考时应走 Flash：{done}")


class TestDsmlGuard(unittest.TestCase):
    """★ 模型把工具调用写成 DSML 正文时，源码一个字都不许流给用户。

    这是真实事故：用户问"东京什么天气"，界面上出现整段
    `<||DSML||calls> ... </||DSML||calls>` 源码。
    纯函数级测试，不依赖模型/网络。
    """

    SAMPLE = (
        "先说一句。\n"
        "<||DSML||calls>\n"
        '<||DSML||invoke name="get_current_weather">\n'
        '<||DSML||parameter name="city" string="true">Tokyo</||DSML||parameter>\n'
        "</||DSML||invoke>\n"
        "</||DSML||calls>\n"
        "再说一句。"
    )

    def setUp(self) -> None:
        import server.api as api_module
        self.api = api_module

    def test_stream_filter_handles_one_char_chunks(self):
        """标记被拆成单字符也得挡住 —— 逐块正则一定会漏。"""
        flt = self.api._DsmlStreamFilter()
        out = "".join(flt.feed(ch) for ch in self.SAMPLE) + flt.flush()
        self.assertNotIn("DSML", out)
        self.assertEqual(out.strip(), "先说一句。\n\n再说一句。")

    def test_stream_filter_handles_many_chunk_sizes(self):
        for size in (2, 3, 5, 7, 13, 64):
            flt = self.api._DsmlStreamFilter()
            out = "".join(flt.feed(self.SAMPLE[i:i + size])
                          for i in range(0, len(self.SAMPLE), size))
            out += flt.flush()
            self.assertNotIn("DSML", out, f"chunk={size} 漏出了 DSML")
            self.assertEqual(out.strip(), "先说一句。\n\n再说一句。")

    def test_stateless_sanitizer_drops_nested_block(self):
        self.assertEqual(self.api.sanitize_dsml(self.SAMPLE).strip(),
                         "先说一句。\n\n再说一句。")

    def test_unterminated_block_is_dropped(self):
        self.assertEqual(self.api.sanitize_dsml("<||DSML||calls>没有结尾"), "")

    def test_tool_meta_english_plan_is_removed(self):
        """工具轮里的英文计划不能混进用户可见回复。"""
        raw = ('The tool returned "salvete". I should give that. '
               '**salvete**。\n\n补充一句：这是对多个人打招呼用的。')
        out = self.api._strip_tool_meta(raw)
        self.assertNotIn("The tool", out)
        self.assertNotIn("I should", out)
        self.assertIn("salvete", out)
        self.assertIn("补充一句", out)

    def test_tool_meta_chinese_plan_is_removed(self):
        raw = "用户问东京天气。工具返回：晴。简洁回答。东京现在是晴，23 度。"
        self.assertEqual(self.api._strip_tool_meta(raw), "东京现在是晴，23 度。")

    def test_tool_meta_leaves_normal_reply_untouched(self):
        raw = "东京现在是晴，23 度。出门带件外套就够了。"
        self.assertEqual(self.api._strip_tool_meta(raw), raw)
    def test_admin_timestamp_is_shanghai_time(self):
        """后台存 UTC，显示必须是北京时间（固定 +8，不靠 tzdata）。"""
        self.assertEqual(self.api._display_time("2026-10-17T10:20:46+00:00"),
                         "2026-10-17 18:20:46（北京时间）")
        self.assertEqual(self.api._display_time("2026-10-17T10:20:46Z"),
                         "2026-10-17 18:20:46（北京时间）")
        self.assertEqual(self.api._display_time("bad-value"), "bad-value")


if __name__ == "__main__":
    unittest.main()
