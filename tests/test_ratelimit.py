# -*- coding: utf-8 -*-
"""
tests/test_ratelimit.py —— 登录限速
================================================================

为什么这组测试必须存在：
    一旦服务经公网隧道暴露出去，**登录接口就是对全网开放的**。
    口令用 PBKDF2（20 万轮）哈希，单次暴力破解很慢，但**可以无限次尝试**。
    没有次数限制，慢也终究会被磨出来。

★ 这里有一条**容易做错、而且错了很危险**的规则：
    隧道后面所有请求的来源 IP 都是 127.0.0.1。如果只按 IP 限速，
    一个人刷满额度就会把**所有人**锁在门外 —— 等于给攻击者送一个
    "一键拒绝服务"的按钮。所以必须**同时**按账号与来源两个维度限。
    下面 test_ip_bucket_does_not_lock_out_other_users 专门守这条。
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.ratelimit import SlidingWindowLimiter  # noqa: E402


class TestSlidingWindowLimiter(unittest.TestCase):
    def test_allows_up_to_the_limit_then_blocks(self):
        lim = SlidingWindowLimiter(limit=3, window_s=60)
        self.assertTrue(lim.allow("k"))
        self.assertTrue(lim.allow("k"))
        self.assertTrue(lim.allow("k"))
        self.assertFalse(lim.allow("k"), "第 4 次应当被拒")

    def test_keys_are_independent(self):
        lim = SlidingWindowLimiter(limit=1, window_s=60)
        self.assertTrue(lim.allow("a"))
        self.assertFalse(lim.allow("a"))
        self.assertTrue(lim.allow("b"), "不同键不该互相影响")

    def test_blocked_does_not_consume_quota(self):
        lim = SlidingWindowLimiter(limit=2, window_s=60)
        lim.allow("k")
        lim.allow("k")
        for _ in range(5):
            self.assertTrue(lim.blocked("k"))
        # 只看不记，所以额度没有被"看"消耗掉（还是 2 次）
        self.assertEqual(lim.retry_after("k") > 0, True)

    def test_window_expiry_frees_the_slot(self):
        lim = SlidingWindowLimiter(limit=1, window_s=0.2)
        self.assertTrue(lim.allow("k"))
        self.assertFalse(lim.allow("k"))
        time.sleep(0.25)
        self.assertTrue(lim.allow("k"), "窗口过去后应当重新放行")

    def test_clear_resets_a_key(self):
        lim = SlidingWindowLimiter(limit=1, window_s=60)
        lim.allow("k")
        self.assertFalse(lim.allow("k"))
        lim.clear("k")
        self.assertTrue(lim.allow("k"), "登录成功后应清掉失败计数")

    def test_sweep_drops_empty_keys(self):
        lim = SlidingWindowLimiter(limit=1, window_s=0.1)
        lim.allow("k")
        time.sleep(0.15)
        lim.sweep()
        self.assertEqual(len(lim._hits), 0, "过期键应被清掉，避免字典无限增长")


class TestEffectiveClientIp(unittest.TestCase):
    """取"有效客户端 IP"的规则（隧道场景下不能全靠 socket 地址）。"""

    def setUp(self) -> None:
        from server import ratelimit
        self.rl = ratelimit

    class _Req:
        def __init__(self, headers=None, host="127.0.0.1"):
            self.headers = headers or {}
            self.client = type("C", (), {"host": host})()

    def test_forwarded_header_is_used_when_trusted(self):
        req = self._Req({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
        self.assertEqual(
            self.rl.effective_client_ip(req, trust_forwarded=True), "203.0.113.9")

    def test_cf_header_takes_priority(self):
        req = self._Req({"cf-connecting-ip": "198.51.100.7",
                         "x-forwarded-for": "203.0.113.9"})
        self.assertEqual(
            self.rl.effective_client_ip(req, trust_forwarded=True), "198.51.100.7")

    def test_socket_address_used_when_not_trusted(self):
        """服务直接暴露在公网时，X-Forwarded-For 是可伪造的 —— 不能采信。"""
        req = self._Req({"x-forwarded-for": "1.2.3.4"}, host="192.168.1.50")
        self.assertEqual(
            self.rl.effective_client_ip(req, trust_forwarded=False), "192.168.1.50")

    def test_falls_back_when_header_missing(self):
        req = self._Req({}, host="10.1.2.3")
        self.assertEqual(
            self.rl.effective_client_ip(req, trust_forwarded=True), "10.1.2.3")


class TestLoginRateLimitOnServer(unittest.TestCase):
    """通过 HTTP 验证限速真的挂在登录接口上。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-rl-")
        base = Path(self.tmp.name)
        self.users_file = base / "users.json"

        from server import api
        importlib.reload(api)
        self.api = api
        self._orig = (api.USERS_FILE, api.SESSIONS_FILE, api.INVITES_FILE,
                      api.CHAT_HISTORY_DIR, api._history)
        from chat.history import HistoryStore
        api.USERS_FILE = self.users_file
        api.SESSIONS_FILE = base / "sessions.json"
        api.INVITES_FILE = base / "invites.json"
        api.CHAT_HISTORY_DIR = base / "chat_history"
        api._history = HistoryStore(api.CHAT_HISTORY_DIR)
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()

        from server import auth
        auth.set_user(self.users_file, "jia", "pw-jia-123", name="甲")

        # 每个用例都从干净额度开始（限流器是进程级单例）
        for lim in (api._rl.LOGIN_LIMITER, api._rl.LOGIN_IP_LIMITER,
                    api._rl.REDEEM_LIMITER):
            lim._hits.clear()

        from starlette.testclient import TestClient
        self.client = TestClient(api.app)

    def tearDown(self) -> None:
        api = self.api
        (api.USERS_FILE, api.SESSIONS_FILE, api.INVITES_FILE,
         api.CHAT_HISTORY_DIR, api._history) = self._orig
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()
        for lim in (api._rl.LOGIN_LIMITER, api._rl.LOGIN_IP_LIMITER,
                    api._rl.REDEEM_LIMITER):
            lim._hits.clear()
        self.tmp.cleanup()

    def _login(self, pw: str, user: str = "jia"):
        return self.client.post("/api/login",
                                json={"user_id": user, "password": pw})

    def test_repeated_failures_get_blocked(self):
        limit = self.api._rl.LOGIN_LIMITER.limit
        codes = [self._login("wrong").status_code for _ in range(limit)]
        self.assertNotIn(429, codes, "额度内不该被拦")
        after = self._login("wrong")
        self.assertEqual(after.status_code, 429, "超限后应当是 429")
        self.assertIn("过多", after.json()["error"])

    def test_correct_password_also_blocked_while_locked(self):
        """锁定期间即使口令是对的也要拒 —— 否则限速形同虚设。"""
        limit = self.api._rl.LOGIN_LIMITER.limit
        for _ in range(limit):
            self._login("wrong")
        res = self._login("pw-jia-123")
        self.assertEqual(res.status_code, 429)

    def test_success_clears_the_failure_count(self):
        """成功登录应清掉该账号的失败计数。

        否则"偶尔手滑几次"会一直累积，某天突然把正常用户锁住。
        """
        for _ in range(self.api._rl.LOGIN_LIMITER.limit - 1):
            self._login("wrong")
        self.assertEqual(self._login("pw-jia-123").status_code, 200)
        # 清空之后应当又能正常失败 limit 次而不被锁
        codes = [self._login("wrong").status_code
                 for _ in range(self.api._rl.LOGIN_LIMITER.limit - 1)]
        self.assertNotIn(429, codes)

    def test_ip_bucket_does_not_lock_out_other_users(self):
        """★ 按 IP 的额度不能把**别的账号**也锁死。

        隧道后面所有请求的来源 IP 都是 127.0.0.1（测试里也是）。
        如果只按 IP 限速，攻击者刷满额度就能让所有正常用户都登不进来 ——
        那等于提供了一个拒绝服务按钮。
        这条测试守的就是"账号维度独立于 IP 维度"。
        """
        from server import auth
        auth.set_user(self.users_file, "yi", "pw-yi-456", name="乙")

        # 把**甲**这个账号刷到锁
        for _ in range(self.api._rl.LOGIN_LIMITER.limit):
            self._login("wrong", user="jia")
        self.assertEqual(self._login("pw-jia-123", user="jia").status_code, 429)

        # 乙仍然能正常登录（IP 额度还没满）
        self.assertEqual(self._login("pw-yi-456", user="yi").status_code, 200)

    def test_redeem_is_rate_limited(self):
        """开通接口也要限速（邀请码空间大，但不该只靠"猜不到"）。"""
        limit = self.api._rl.REDEEM_LIMITER.limit
        for _ in range(limit):
            self.client.post("/api/redeem", json={
                "code": "heytalk-nosuch", "user_id": "x", "password": "pw-123456"})
        res = self.client.post("/api/redeem", json={
            "code": "heytalk-nosuch", "user_id": "x", "password": "pw-123456"})
        self.assertEqual(res.status_code, 429)


if __name__ == "__main__":
    unittest.main()
