# -*- coding: utf-8 -*-
"""
src/server/ratelimit.py —— 登录/开通接口的速率限制
================================================================

为什么必须要有它（而不是"反正是给两个朋友用的"）：
    一旦走公网隧道把服务暴露出去，**登录接口就是对全网开放的**。
    我们的口令用 PBKDF2（20 万轮）哈希，暴力破解单次很慢，但
    **可以无限次尝试** —— 没有次数限制，慢也终究会被磨出来。
    邀请码同理（虽然 31^8 的空间基本猜不到，但不该把安全性
    完全押在"猜不到"上）。

★ 一个容易做错的关键点：**隧道后面所有请求的来源 IP 都一样**
    （Cloudflare/ngrok 转发时来源是 127.0.0.1）。
    如果只按 IP 限速，一个攻击者刷满额度就会**把所有人一起锁在门外** ——
    那等于给攻击者提供了一个"一键拒绝服务"的按钮。
    所以这里按**两个维度分别限**：
      · 按用户 id（被打的那个账号）
      · 按有效来源 IP（换账号刷也要受限）
    任一维度超限即拒绝。

关于 X-Forwarded-For：
    隧道会在该头里放真实客户端 IP。**如果服务直接暴露在公网**
    （HEYTALK_HOST=0.0.0.0），该头是可伪造的 —— 所以只在监听本机
    （即"肯定在代理后面"）时才采信它；否则退回 socket 的 peer 地址。
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Deque

__all__ = ["SlidingWindowLimiter", "effective_client_ip", "LOGIN_LIMITER",
           "LOGIN_IP_LIMITER", "REDEEM_LIMITER", "TRANSCRIBE_LIMITER"]

# 登录：15 分钟内同一账号最多失败 10 次；同一 IP 最多 30 次
_LOGIN_PER_USER = 10
_LOGIN_PER_IP = 30
_WINDOW_S = 15 * 60


class SlidingWindowLimiter:
    """滑动窗口限流器（纯内存，进程重启即清零）。

    为什么用内存而不是落盘：
      · 限流是**短时**保护，重启清零可以接受；
      · 落盘会引入"清理与并发写"的复杂度，而收益很小。
    为什么用滑动窗口而不是固定窗口：
      固定窗口在边界上可以被绕过（窗口末尾刷满 + 下一窗口开头再刷满，
      等于瞬间两倍额度）。滑动窗口没有这个缝。
    """

    def __init__(self, limit: int, window_s: float = _WINDOW_S) -> None:
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._hits: dict[str, Deque[float]] = {}

    def _prune(self, key: str, now: float) -> Deque[float]:
        q = self._hits.get(key)
        if q is None:
            q = deque()
            self._hits[key] = q
        cutoff = now - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def allow(self, key: str) -> bool:
        """记一次尝试；返回是否**允许**（超限返回 False）。"""
        now = time.monotonic()
        q = self._prune(key, now)
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def blocked(self, key: str) -> bool:
        """只看是否已超限，**不记这一次**。"""
        now = time.monotonic()
        return len(self._prune(key, now)) >= self.limit

    def retry_after(self, key: str) -> int:
        """还要等多少秒才能再试（0 表示现在可以）。"""
        now = time.monotonic()
        q = self._prune(key, now)
        if len(q) < self.limit:
            return 0
        return max(1, int(q[0] + self.window_s - now) + 1)

    def clear(self, key: str) -> None:
        """清掉某个键的记录（登录成功时用 —— 成功不该继续累积失败）。"""
        self._hits.pop(key, None)

    def sweep(self) -> None:
        """清掉所有已空的键，避免字典无限增长。"""
        now = time.monotonic()
        for key in [k for k in self._hits if not self._prune(k, now)]:
            self._hits.pop(key, None)


def effective_client_ip(request, *, trust_forwarded: bool | None = None) -> str:
    """取"有效客户端 IP"。

    参数 trust_forwarded：
        True  → 采信 X-Forwarded-For / CF-Connecting-IP 的第一段
        False → 只用 socket 的 peer 地址
        None  → 自动判断：**监听在本机时**才采信（那种情况下服务
                只可能通过代理被访问，头部由代理写入、客户端伪造不了）
    """
    if trust_forwarded is None:
        host = (os.environ.get("HEYTALK_HOST", "") or "").strip()
        # 默认监听 127.0.0.1 ⇒ 外部只能经由代理进来 ⇒ 头部可信
        trust_forwarded = host in ("", "127.0.0.1", "localhost")

    if trust_forwarded:
        for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
            raw = request.headers.get(header)
            if raw:
                first = raw.split(",")[0].strip()
                if first:
                    return first

    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


# 进程级单例（两个接口各一份额度）
LOGIN_LIMITER = SlidingWindowLimiter(_LOGIN_PER_USER, _WINDOW_S)
LOGIN_IP_LIMITER = SlidingWindowLimiter(_LOGIN_PER_IP, _WINDOW_S)
REDEEM_LIMITER = SlidingWindowLimiter(5, _WINDOW_S)

# 语音识别：**很吃 CPU**（一段 30 秒的语音在纯 CPU 上要好几秒），
# 所以额度比登录更紧。按用户限，不按 IP —— 这是一个明确的用户行为，
# 不是攻击面，用 IP 限会误伤同一个 WiFi 下的其他人。
TRANSCRIBE_LIMITER = SlidingWindowLimiter(30, 10 * 60)
