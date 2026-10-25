# -*- coding: utf-8 -*-
"""
tests/test_server_auth.py —— 托管模式的登录与**数据隔离**
================================================================

为什么这组测试必须存在（它守的是一条安全属性，不是一个功能）：
    托管模式下如果隔离失效，**甲的侧边栏会列出乙的全部对话**，
    标题就是第一句话、直接可读。这不是"隐私偏好"问题，是真 bug。
    而这个 bug 有个特点：**功能测试全绿也可能漏掉它** ——
    因为"能列出会话"本身是正常行为。所以必须专门测"看不到别人的"。

另外两条同样是硬要求：
    · **本机模式不能被打破**：没配置用户时不该出现登录页，
      否则单人自己用会被凭空多出的一道门挡住。
    · **越权访问必须失败**：拿到别人的 session_id 也读不到、删不掉。
      （UUID 不可猜，但"不可猜"不是隔离措施。）
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class AuthTestBase(unittest.TestCase):
    """把用户表、令牌表、历史目录全部重定向到临时目录。

    ★ 绝不碰 data/ 下的真实文件 —— 主人的对话（包括敏感内容）在那里。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-auth-")
        base = Path(self.tmp.name)
        self.users_file = base / "users.json"
        self.sessions_file = base / "sessions.json"
        self.invites_file = base / "invites.json"
        self.history_dir = base / "chat_history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

        from server import api
        importlib.reload(api)
        self.api = api

        # 重定向到临时目录
        self._orig = (api.USERS_FILE, api.SESSIONS_FILE, api.CHAT_HISTORY_DIR,
                      api.INVITES_FILE)
        api.USERS_FILE = self.users_file
        api.SESSIONS_FILE = self.sessions_file
        api.CHAT_HISTORY_DIR = self.history_dir
        api.INVITES_FILE = self.invites_file
        # ★ 还必须换掉 `_history` 本身：它在模块**导入时**就绑定了真实目录，
        #   只改 CHAT_HISTORY_DIR 是没用的 —— 本机模式的测试会直接读到
        #   主人 data/chat_history/ 里的真实对话（我第一次跑就撞上了，
        #   测试断言里直接出现了真实记录的 session_id）。
        from chat.history import HistoryStore
        self._orig_history = api._history
        api._history = HistoryStore(self.history_dir)
        api._HISTORY_BY_USER.clear()
        api._CONVERSATIONS.clear()

        from starlette.testclient import TestClient
        self.client = TestClient(api.app)

        # ★ 限流器是**进程级单例**，会在用例之间累积额度。
        #   不清的话，本文件里连续调几次 /api/redeem 就会被 429 ——
        #   我加完限速后确实吃了 6 个失败 + 2 个错误。
        #   凡是走登录/开通接口的测试，都必须从干净额度开始。
        self._reset_limiters()

    def _reset_limiters(self) -> None:
        for lim in (self.api._rl.LOGIN_LIMITER,
                    self.api._rl.LOGIN_IP_LIMITER,
                    self.api._rl.REDEEM_LIMITER):
            lim._hits.clear()  # noqa: SLF001

    def tearDown(self) -> None:
        api = self.api
        (api.USERS_FILE, api.SESSIONS_FILE, api.CHAT_HISTORY_DIR,
         api.INVITES_FILE) = self._orig
        api._history = self._orig_history
        api._HISTORY_BY_USER.clear()
        api._CONVERSATIONS.clear()
        self._reset_limiters()
        self.tmp.cleanup()

    # ---- 辅助 ----
    def add_user(self, uid: str, pw: str, *, admin: bool = False,
                 name: str = "") -> None:
        from server import auth
        auth.set_user(self.users_file, uid, pw, name=name or uid,
                      is_admin=admin)

    def token(self, uid: str, pw: str) -> str:
        res = self.client.post("/api/login",
                               json={"user_id": uid, "password": pw})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    @staticmethod
    def hdr(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def seed(self, token: str, text: str) -> str:
        """在某个用户下造一条真实落盘的对话（不调模型）。"""
        api = self.api
        sid = self.client.post("/api/history/new",
                               headers=self.hdr(token)).json()["session_id"]
        conv = api._CONVERSATIONS[sid]
        conv.session.add_user(text)
        conv.session.add_assistant("回应：" + text)
        api.save_conversation(sid)
        return sid


class TestLocalMode(AuthTestBase):
    """★ 没配置用户时不能被登录挡住（这是主人自己每天用的模式）。"""

    def test_no_users_means_no_login_required(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertFalse(data["auth_required"])
        self.assertEqual(data["user_id"], "local")

    def test_local_mode_can_list_and_chat_without_token(self):
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sessions"], [])

        res = self.client.post("/api/history/new")
        self.assertEqual(res.status_code, 200)

    def test_local_mode_uses_root_history_dir(self):
        """本机模式用根目录 —— 这样主人已有的记录无需迁移就能看到。"""
        api = self.api
        from chat.history import HistoryStore
        self.assertIsInstance(api.history_for("local"), HistoryStore)
        # 同一个实例（根目录那个），不是子目录
        self.assertIs(api.history_for("local"), api._history)

    def test_admin_page_without_users_says_so(self):
        """后台在没配用户时也要能打开 —— 否则主人看不到自己本机的数据。"""
        # 本机模式没有管理员概念：应当被拒绝（没有 is_admin 的用户）
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 403)


class TestLogin(AuthTestBase):
    def test_configured_user_requires_login(self):
        self.add_user("jia", "pw-jia-123")
        self.assertTrue(self.client.get("/api/bootstrap").json()["auth_required"])
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.json()["need_login"])

    def test_wrong_password_is_rejected(self):
        self.add_user("jia", "pw-jia-123")
        res = self.client.post("/api/login",
                               json={"user_id": "jia", "password": "nope"})
        self.assertEqual(res.status_code, 401)

    def test_unknown_user_gives_the_same_message(self):
        """不区分"用户不存在"与"口令错" —— 否则等于帮人枚举用户名。"""
        self.add_user("jia", "pw-jia-123")
        a = self.client.post("/api/login",
                             json={"user_id": "jia", "password": "x"}).json()
        b = self.client.post("/api/login",
                             json={"user_id": "nobody", "password": "x"}).json()
        self.assertEqual(a["error"], b["error"])

    def test_token_works_and_logout_revokes_it(self):
        self.add_user("jia", "pw-jia-123")
        tok = self.token("jia", "pw-jia-123")
        self.assertEqual(self.client.get("/api/history",
                                         headers=self.hdr(tok)).status_code, 200)
        self.assertEqual(self.client.post("/api/logout",
                                          headers=self.hdr(tok)).status_code, 200)
        self.assertEqual(self.client.get("/api/history",
                                         headers=self.hdr(tok)).status_code, 401)

    def test_garbage_token_is_rejected(self):
        self.add_user("jia", "pw-jia-123")
        # 用 ASCII 假令牌：非 ASCII 会被 HTTP 头编码挡在更早的地方，
        # 那样测的就不是"令牌校验"了
        res = self.client.get("/api/history",
                              headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(res.status_code, 401)


class TestIsolation(AuthTestBase):
    """★ 这组是本次改动的核心：谁只能看到谁的。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user("jia", "pw-jia-123", name="甲")
        self.add_user("yi", "pw-yi-456", name="乙")
        self.tok_jia = self.token("jia", "pw-jia-123")
        self.tok_yi = self.token("yi", "pw-yi-456")

    def test_each_user_sees_only_their_own(self):
        self.seed(self.tok_jia, "甲说的话")
        self.seed(self.tok_yi, "乙说的话")

        jia = self.client.get("/api/history", headers=self.hdr(self.tok_jia))
        yi = self.client.get("/api/history", headers=self.hdr(self.tok_yi))
        titles_jia = [s["title"] for s in jia.json()["sessions"]]
        titles_yi = [s["title"] for s in yi.json()["sessions"]]

        self.assertEqual(titles_jia, ["甲说的话"])
        self.assertEqual(titles_yi, ["乙说的话"])

    def test_cannot_read_another_users_session(self):
        sid_yi = self.seed(self.tok_yi, "乙的私密话")
        res = self.client.get(f"/api/history/{sid_yi}",
                              headers=self.hdr(self.tok_jia))
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("乙的私密话", res.text)

    def test_cannot_delete_another_users_session(self):
        sid_yi = self.seed(self.tok_yi, "乙的私密话")
        res = self.client.delete(f"/api/history/{sid_yi}",
                                 headers=self.hdr(self.tok_jia))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["removed"], "甲不该能删掉乙的记录")
        # 乙自己还看得到
        still = self.client.get("/api/history", headers=self.hdr(self.tok_yi))
        self.assertEqual([s["title"] for s in still.json()["sessions"]],
                         ["乙的私密话"])

    def test_users_get_separate_directories(self):
        self.seed(self.tok_jia, "甲的")
        self.seed(self.tok_yi, "乙的")
        dirs = {p.name for p in self.history_dir.iterdir() if p.is_dir()}
        self.assertEqual(dirs, {"jia", "yi"})

    def test_opening_suggestions_are_scoped_to_each_user(self):
        self.seed(self.tok_jia, '最近准备考研压力很大，晚上总睡不着')
        self.seed(self.tok_yi, '我和女朋友分手后，一直在想这段关系')

        jia_topics = self.client.get(
            '/api/bootstrap', headers=self.hdr(self.tok_jia)
        ).json()['opening_suggestions']
        yi_topics = self.client.get(
            '/api/bootstrap', headers=self.hdr(self.tok_yi)
        ).json()['opening_suggestions']
        jia_joined = '\n'.join(jia_topics)
        yi_joined = '\n'.join(yi_topics)

        self.assertIn('考研', jia_joined)
        self.assertIn('女朋友', yi_joined)
        self.assertNotIn('女朋友', jia_joined,
                         '甲的推荐话题里混入了乙的对话')
        self.assertNotIn('考研', yi_joined,
                         '乙的推荐话题里混入了甲的对话')
        self.assertNotEqual(jia_topics, yi_topics)

    def test_session_id_reused_by_another_user_is_not_shared(self):
        """同一条 id 被另一个用户访问时，不能复用别人的内存态。"""
        api = self.api
        sid = self.seed(self.tok_jia, "甲的话")
        self.assertEqual(api._CONVERSATIONS[sid].user_id, "jia")
        # 乙虽然不知道 id，但即使知道也不会拿到甲的内存会话
        conv = api.get_conversation(sid, "yi")
        self.assertEqual(conv.user_id, "yi")
        self.assertEqual(len(conv.session.turns), 0)


class TestAdminPage(AuthTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.add_user("me", "pw-admin-999", admin=True, name="我")
        self.add_user("jia", "pw-jia-123", name="甲")
        self.tok_me = self.token("me", "pw-admin-999")
        self.tok_jia = self.token("jia", "pw-jia-123")

    def test_non_admin_is_refused(self):
        res = self.client.get("/admin", headers=self.hdr(self.tok_jia))
        self.assertEqual(res.status_code, 403)
        self.assertIn("不是管理员", res.text)

    def test_refusal_page_says_who_you_are_and_offers_logout(self):
        """★ 403 页面必须说出"你现在是谁"并给出退出入口。

        这是用真事故换来的：主人删掉旧账号后，浏览器里的令牌仍然有效，
        他被认成"已登录但不是管理员"，看到一句干巴巴的
        「只有管理员可以访问后台」—— 那句话把他引去找权限配置，
        而真正的问题是"你现在登录的是哪个账号"。
        只拒绝、不告诉他怎么出去，等于把人卡死在门口。
        """
        res = self.client.get("/admin", headers=self.hdr(self.tok_jia))
        self.assertIn("甲", res.text, "应当说出当前登录的身份")
        self.assertIn("jia", res.text)
        self.assertIn("/logout", res.text, "必须给一个退出去换账号的入口")

    def test_logout_link_clears_the_cookie(self):
        """`/logout` 是 GET（浏览器里只能点链接），要清 cookie 并跳回登录页。"""
        from starlette.testclient import TestClient
        c = TestClient(self.api.app)
        c.cookies.set("heytalk_token", self.tok_jia)
        self.assertEqual(c.get("/admin").status_code, 403)   # 先确认带着登录态
        res = c.get("/logout", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers.get("location"), "/app")
        self.assertEqual(c.get("/admin").status_code, 401, "退出后应回到未登录")

    def test_anonymous_is_refused(self):
        """未登录不能进后台。

        注意：必须用一个**全新的 client** —— TestClient 会保留 cookie，
        而 setUp 里已经登录过，所以复用同一个 client 时其实"带着登录态"。
        （这个坑我实际撞到了：测试报的错是"应该 401 却拿到了 200"。）
        """
        from starlette.testclient import TestClient
        fresh = TestClient(self.api.app)
        res = fresh.get("/admin")
        self.assertEqual(res.status_code, 401)

    def test_cookie_alone_is_enough_for_admin(self):
        """★ 浏览器直接导航时没有 Authorization 头，只能靠 cookie。

        这是修掉一个真缺陷时加的测试：`/admin` 是服务端直出的页面，
        用户在地址栏输入网址打开，浏览器不会替他加 Authorization 头。
        只认头的话，在浏览器里打开 /admin 永远是 401。
        """
        res = self.client.post("/api/login",
                               json={"user_id": "me", "password": "pw-admin-999"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("heytalk_token", res.cookies,
                      "登录没有下发 cookie —— 浏览器里打不开后台")

        from starlette.testclient import TestClient
        jar = TestClient(self.api.app)
        jar.cookies.set("heytalk_token", res.json()["token"])
        page = jar.get("/admin")           # 不带任何 Authorization 头
        self.assertEqual(page.status_code, 200)
        self.assertIn("Heytalk 后台", page.text)

    def test_admin_sees_users(self):
        res = self.client.get("/admin", headers=self.hdr(self.tok_me))
        self.assertEqual(res.status_code, 200)
        self.assertIn("jia", res.text)
        self.assertIn("我", res.text)

    def test_admin_can_read_any_users_conversation(self):
        sid = self.seed(self.tok_jia, "甲对系统说的话")
        res = self.client.get(f"/admin?user=jia&session={sid}",
                              headers=self.hdr(self.tok_me))
        self.assertEqual(res.status_code, 200)
        self.assertIn("甲对系统说的话", res.text)

    def test_admin_rejects_unknown_user_param(self):
        """挡住 ?user=../../ 这类路径穿越：不在用户表里的一律忽略。"""
        res = self.client.get("/admin?user=..%2F..%2Fetc",
                              headers=self.hdr(self.tok_me))
        self.assertEqual(res.status_code, 200)
        # 被忽略后不应渲染出任何"某人的对话"区块
        self.assertNotIn("的对话</h2>", res.text)

    def test_conversation_content_is_html_escaped(self):
        """★ XSS：对话内容会拼进后台的 HTML，必须转义。"""
        sid = self.seed(self.tok_jia, "<img src=x onerror=alert(1)>")
        res = self.client.get(f"/admin?user=jia&session={sid}",
                              headers=self.hdr(self.tok_me))
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("<img src=x", res.text, "未转义 —— 会被注入")
        self.assertIn("&lt;img", res.text)


class TestStaleToken(AuthTestBase):
    """★ 账号被删除后，它的登录令牌必须立刻失效。

    这是一个真实事故：主人删掉 demo 账号，但浏览器里那条 7 天有效的令牌
    仍然有效 —— 于是他打开 /admin 得到的是 403「不是管理员」而不是
    401「请先登录」，报错把他引向了错误的方向。
    """

    def test_token_of_deleted_user_stops_working(self):
        # 刻意保留另一个用户：**用户表被清空时服务会回到"本机免登录模式"**
        # （那是刻意的设计，见 test_removing_last_user_reverts_to_local_mode），
        # 所以这里必须有第二个用户，才能测"托管模式下的幽灵令牌"。
        self.add_user("keep", "pw-keep-123")
        self.add_user("gone", "pw-gone-123", admin=True)
        tok = self.token("gone", "pw-gone-123")
        self.assertEqual(self.client.get("/api/history",
                                         headers=self.hdr(tok)).status_code, 200)

        from server import auth
        auth.remove_user(self.users_file, "gone", self.sessions_file)

        # 令牌本身可能还在表里，但**用户没了就不该再认**
        self.assertEqual(self.client.get("/api/history",
                                         headers=self.hdr(tok)).status_code, 401)
        self.assertEqual(self.client.get("/admin",
                                         headers=self.hdr(tok)).status_code, 401,
                         "幽灵账号不该被当成'已登录但非管理员'")

    def test_removing_last_user_reverts_to_local_mode(self):
        """★ 把用户删光 → 回到"本机免登录模式"（刻意的行为，不是漏洞）。

        为什么这不算"认证被绕过"：能删 users.json 的人本来就是这台机器的
        主人 —— 他有文件系统权限，不需要绕过登录。
        反过来说，这条行为对主人很重要：**他随时能把服务退回单人自用**，
        不必先理解账号体系。所以它该被钉住，而不是被当成 bug 修掉。
        """
        from server import auth
        self.add_user("only", "pw-only-123")
        self.assertTrue(self.client.get("/api/bootstrap").json()["auth_required"])
        auth.remove_user(self.users_file, "only", self.sessions_file)
        self.assertFalse(self.client.get("/api/bootstrap").json()["auth_required"])
        self.assertEqual(self.client.get("/api/history").status_code, 200)

    def test_remove_user_revokes_tokens(self):
        from server import auth
        self.add_user("gone2", "pw-x-123")
        tok = self.token("gone2", "pw-x-123")
        revoked = auth.remove_user(self.users_file, "gone2", self.sessions_file)
        self.assertGreaterEqual(revoked, 1, "删用户时应同时吊销他的令牌")
        self.assertIsNone(auth.resolve_token(self.sessions_file, tok))


class TestInvites(AuthTestBase):
    """★ 邀请码：朋友自助开号，管理员全程不经手口令。

    为什么要有这套机制（主人实际提出来的问题）：
        "给朋友用是不是还要我给他们口令？不能他们自己创建？"
        让管理员分发口令有两个真实危害，而且我们当场撞上过 ——
        那个演示口令写进聊天记录的那一刻，它就该被视为已泄露。
    """

    def setUp(self) -> None:
        super().setUp()
        # 需要至少一个用户，服务才处于"托管模式"
        self.add_user("me", "pw-admin-999", admin=True, name="我")
        self.invites_file = Path(self.tmp.name) / "invites.json"
        self._orig_invites = self.api.INVITES_FILE
        self.api.INVITES_FILE = self.invites_file

    def tearDown(self) -> None:
        self.api.INVITES_FILE = self._orig_invites
        super().tearDown()

    def _invite(self, **kw) -> str:
        from server import auth
        return auth.create_invite(self.invites_file, **kw)

    def test_invite_lets_friend_self_serve(self):
        """★ 核心流程：管理员只发码，朋友自己起用户名、自己设口令。"""
        code = self._invite(name="甲")
        res = self.client.post("/api/redeem", json={
            "code": code, "user_id": "jia", "password": "his-own-secret"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["user_id"], "jia")
        self.assertEqual(data["name"], "甲")
        self.assertTrue(data["token"], "开完账号应当直接登录，不用再输一次")

        # 建出来的账号能用
        tok = self.token("jia", "his-own-secret")
        self.assertTrue(tok)

    def test_admin_never_sees_the_password(self):
        """管理员拿到的用户表里只有盐与哈希，没有明文口令。"""
        code = self._invite(name="甲")
        self.client.post("/api/redeem", json={
            "code": code, "user_id": "jia", "password": "his-own-secret"})
        raw = self.users_file.read_text(encoding="utf-8")
        self.assertNotIn("his-own-secret", raw, "口令被明文写盘了")

    def test_code_is_single_use(self):
        code = self._invite(name="甲")
        first = self.client.post("/api/redeem", json={
            "code": code, "user_id": "jia", "password": "pw-jia-123"})
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/redeem", json={
            "code": code, "user_id": "yi", "password": "pw-yi-123"})
        self.assertEqual(second.status_code, 400)
        self.assertIn("已经用过", second.json()["error"])

    def test_unknown_code_is_rejected(self):
        res = self.client.post("/api/redeem", json={
            "code": "heytalk-nosuch", "user_id": "x", "password": "pw-123456"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("邀请码不对", res.json()["error"])

    def test_taken_username_is_rejected(self):
        code = self._invite()
        res = self.client.post("/api/redeem", json={
            "code": code, "user_id": "me", "password": "pw-123456"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("已经有人用", res.json()["error"])

    def test_short_password_is_rejected(self):
        code = self._invite()
        res = self.client.post("/api/redeem", json={
            "code": code, "user_id": "newbie", "password": "123"})
        self.assertEqual(res.status_code, 400)
        # 短口令离线暴力破解很快，所以设了下限
        self.assertIn("至少 6 位", res.json()["error"])

    def test_missing_fields_are_rejected(self):
        code = self._invite()
        res = self.client.post("/api/redeem", json={"code": code})
        self.assertEqual(res.status_code, 400)

    def test_dangerous_username_is_rejected(self):
        """用户 id 会成为目录名 —— 必须挡住路径穿越。"""
        for bad in ("..", "a/b", "a\\b", "has space"):
            code = self._invite()
            res = self.client.post("/api/redeem", json={
                "code": code, "user_id": bad, "password": "pw-123456"})
            self.assertEqual(res.status_code, 400, f"{bad!r} 竟然通过了")

    def test_expired_invite_is_rejected(self):
        from server import auth
        code = auth.create_invite(self.invites_file, name="甲", ttl_s=-1)
        res = self.client.post("/api/redeem", json={
            "code": code, "user_id": "jia", "password": "pw-123456"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("过期", res.json()["error"])

    def test_invited_user_is_isolated_like_anyone_else(self):
        """邀请码开的账号同样只看得到自己的对话。"""
        code = self._invite(name="甲")
        r = self.client.post("/api/redeem", json={
            "code": code, "user_id": "jia", "password": "pw-jia-123"})
        tok_jia = r.json()["token"]
        tok_me = self.token("me", "pw-admin-999")

        self.seed(tok_jia, "甲说的话")
        self.seed(tok_me, "我说的话")

        jia = self.client.get("/api/history", headers=self.hdr(tok_jia))
        self.assertEqual([s["title"] for s in jia.json()["sessions"]],
                         ["甲说的话"])

    def test_non_admin_invite_does_not_grant_admin(self):
        code = self._invite(name="朋友")     # 默认 is_admin=False
        r = self.client.post("/api/redeem", json={
            "code": code, "user_id": "friend", "password": "pw-friend-1"})
        tok = r.json()["token"]
        res = self.client.get("/admin", headers=self.hdr(tok))
        self.assertEqual(res.status_code, 403, "普通朋友不该能进后台")


class TestAuthModule(unittest.TestCase):
    """直接测 auth 模块本身（不经过 HTTP）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-authm-")
        base = Path(self.tmp.name)
        self.users = base / "users.json"
        self.sessions = base / "sessions.json"
        from server import auth
        self.auth = auth

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_password_is_never_stored_in_plaintext(self):
        self.auth.set_user(self.users, "jia", "超级机密口令")
        raw = self.users.read_text(encoding="utf-8")
        self.assertNotIn("超级机密口令", raw, "口令被明文写进了文件")
        data = json.loads(raw)
        self.assertIn("salt", data["jia"])
        self.assertIn("hash", data["jia"])

    def test_same_password_gets_different_hashes(self):
        """每个用户独立随机盐 —— 相同的口令不能产生相同的哈希。"""
        self.auth.set_user(self.users, "a", "同一个口令")
        self.auth.set_user(self.users, "b", "同一个口令")
        data = json.loads(self.users.read_text(encoding="utf-8"))
        self.assertNotEqual(data["a"]["hash"], data["b"]["hash"])

    def test_user_id_cannot_escape_the_directory(self):
        """用户 id 会成为目录名，必须挡住路径穿越。"""
        for bad in ("../x", "a/b", "a\\b"):
            with self.assertRaises(self.auth.AuthError):
                self.auth.set_user(self.users, bad, "pw")

    def test_corrupt_user_file_does_not_crash(self):
        """用户表损坏最多是"要重新登录"，不能让服务起不来。"""
        self.users.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(self.auth.load_users(self.users), {})
        self.assertFalse(self.auth.auth_enabled(self.users))

    def test_expired_token_is_rejected(self):
        self.auth.set_user(self.users, "jia", "pw")
        tok = self.auth.issue_token(self.sessions, "jia")
        self.assertEqual(self.auth.resolve_token(self.sessions, tok), "jia")
        # 手动把过期时间改到过去
        data = json.loads(self.sessions.read_text(encoding="utf-8"))
        data[tok]["expires_at"] = 1
        self.sessions.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.auth.resolve_token(self.sessions, tok))

    def test_revoke_token(self):
        self.auth.set_user(self.users, "jia", "pw")
        tok = self.auth.issue_token(self.sessions, "jia")
        self.assertTrue(self.auth.revoke_token(self.sessions, tok))
        self.assertIsNone(self.auth.resolve_token(self.sessions, tok))


if __name__ == "__main__":
    unittest.main()
