# -*- coding: utf-8 -*-
"""
src/server/auth.py —— 多用户登录（托管模式才需要）
================================================================

这个模块解决的是一个**识别**问题，不是**认证**问题
--------------------------------------------------------------------------
主人要给两个朋友用同一个服务，而数据必须按人分开
（否则甲的侧边栏会显示乙的对话 —— 那是真 bug，不是隐私偏好）。
朋友不是对手，没有秘密要防，所以这里刻意**不引入**：
    · 短信验证码（政策已不允许个人申请，见下）
    · 邮箱验证 / 第三方 OAuth（要注册应用、要联网）
    · 密码找回、二次验证、登录风控

**关于短信为什么没做**（免得后人以为是漏了）：
    腾讯云官方文档（实名资质审核标准）写明："由于当前运营商不支持对个人
    用户进行签名报备，自 2025 年 9 月 18 日起，控制台将不再支持创建和
    修改个人自用资质"；"国内短信仅支持中国大陆公司授权申请签名"。
    也就是说个人开发者要发国内短信，先得有一家公司并提交盖公章的证件。
    详见 https://cloud.tencent.cn/document/product/382/108274

设计原则
--------------------------------------------------------------------------
1. **本机模式零门槛**：users.json 不存在或为空 → 不需要登录，
   一切照旧（单人自己用不该被登录页挡住）。只有配置了用户才启用登录。
2. **不存明文口令**：用 PBKDF2-HMAC-SHA256 + 每个用户独立随机盐。
3. **令牌可人工吊销**：存在 data/sessions.json，删掉一行即失效。
4. **不依赖数据库**：全用本地 JSON，便于直接查看与修复。

⚠️ 这个模块**不是**为了抵御有决心的攻击者。它是给"两个熟人 + 一台
   内网/隧道机器"这个场景用的。如果将来要给陌生人用，需要重做：
   加限速、加令牌过期、加 HTTPS、加审计日志。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

__all__ = [
    "AuthError",
    "load_users",
    "auth_enabled",
    "set_user",
    "remove_user",
    "verify_login",
    "issue_token",
    "resolve_token",
    "revoke_token",
    "token_owner",
    "load_invites",
    "create_invite",
    "redeem_invite",
    "revoke_invite",
    "LOCAL_USER_ID",
]

# 本机模式下的默认用户 id。历史目录用它做子目录名。
LOCAL_USER_ID = "local"

# PBKDF2 迭代次数。20 万次在普通机器上约几十毫秒 ——
# 对登录足够，又不至于让登录明显卡顿。
_PBKDF2_ROUNDS = 200_000
_SALT_BYTES = 16

# 令牌有效期（秒）。7 天：朋友不用天天登录，出问题也不会长期有效。
_TOKEN_TTL_S = 7 * 24 * 3600


class AuthError(Exception):
    """登录相关错误（带给用户看的中文提示）。"""


# ---------------------------------------------------------------------------
# 文件读写（路径由调用方注入，便于测试用临时目录）
# ---------------------------------------------------------------------------
def _read_json(path: Path, default: Any) -> Any:
    """读 JSON；文件不存在或损坏都返回 default，**绝不抛异常**。

    为什么容忍损坏：这是登录用的文件，读不出来最多是"要重新登录"，
    抛异常会让整个服务起不来 —— 代价不对等。
    """
    try:
        if not path.is_file():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, payload: Any) -> None:
    """原子写：先写临时文件再替换，避免写一半被中断导致文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt_hex: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
        _PBKDF2_ROUNDS)
    return digest.hex()


def make_credential(password: str) -> dict[str, Any]:
    """生成一条可用于 users.json 的凭据（含随机盐与哈希）。"""
    if not password:
        raise AuthError("口令不能为空")
    salt = secrets.token_hex(_SALT_BYTES)
    return {
        "salt": salt,
        "hash": _hash_password(password, salt),
        "rounds": _PBKDF2_ROUNDS,
        "created_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# 用户表
# ---------------------------------------------------------------------------
def load_users(users_file: Path) -> dict[str, dict[str, Any]]:
    """读用户表。返回 {user_id: {...}}；文件不存在则返回空 dict。"""
    data = _read_json(users_file, {})
    if not isinstance(data, dict):
        return {}
    users: dict[str, dict[str, Any]] = {}
    for uid, info in data.items():
        if isinstance(info, dict) and info.get("hash") and info.get("salt"):
            users[str(uid)] = info
    return users


def auth_enabled(users_file: Path) -> bool:
    """是否启用登录。

    ★ 关键设计：**没有配置任何用户时，不需要登录**。
    这样单人自己用（现在就是）完全不受影响 —— 不会因为加了多用户
    支持，就凭空多出一个登录页把主人自己挡在外面。
    """
    return bool(load_users(users_file))


def set_user(users_file: Path, user_id: str, password: str, *,
             name: str = "", is_admin: bool = False) -> None:
    """新增或覆盖一个用户（覆盖即改口令）。"""
    uid = str(user_id).strip()
    if not uid:
        raise AuthError("用户 id 不能为空")
    # 目录名会用这个 id，所以要挡住路径穿越
    if any(c in uid for c in ("/", "\\", "..", "\x00")):
        raise AuthError(f"用户 id 不能包含 / \\ 或 .. ：{uid!r}")

    users = load_users(users_file)
    cred = make_credential(password)
    cred["name"] = str(name or uid)
    cred["is_admin"] = bool(is_admin)
    users[uid] = cred
    _write_json(users_file, users)


def remove_user(users_file: Path, user_id: str,
                sessions_file: Path | None = None) -> int:
    """删除用户。返回**被吊销的令牌数**。

    ★ 为什么删用户的同时必须吊销令牌（真实事故）：
      主人删掉 demo 账号后，浏览器里那条 7 天有效的令牌仍然有效，
      于是他打开 /admin 得到的是「只有管理员可以访问后台」（403），
      而不是「请先登录」（401）—— 一条把他引向错误方向的报错。
      账号都不存在了，它的会话就不该还活着。
    （即便漏了这一步，api._current_user 也会二次校验用户是否存在；
      这里是"把该清的清干净"，避免令牌表里堆一堆幽灵条目。）
    """
    users = load_users(users_file)
    existed = users.pop(str(user_id), None) is not None
    if existed:
        _write_json(users_file, users)

    revoked = 0
    if sessions_file is not None:
        sessions = _read_json(sessions_file, {})
        if isinstance(sessions, dict):
            keep = {t: r for t, r in sessions.items()
                    if not (isinstance(r, dict)
                            and str(r.get("user_id")) == str(user_id))}
            revoked = len(sessions) - len(keep)
            if revoked:
                _write_json(sessions_file, keep)
    return revoked


# ---------------------------------------------------------------------------
# 登录与令牌
# ---------------------------------------------------------------------------
def verify_login(users_file: Path, user_id: str, password: str) -> dict[str, Any]:
    """校验口令。成功返回该用户信息，失败抛 AuthError。"""
    users = load_users(users_file)
    info = users.get(str(user_id))
    if info is None:
        # 刻意不区分"用户不存在"与"口令错误" —— 避免帮人枚举用户名
        raise AuthError("用户名或口令不对")
    expected = str(info.get("hash") or "")
    actual = _hash_password(password, str(info.get("salt") or ""))
    if not hmac.compare_digest(expected, actual):
        raise AuthError("用户名或口令不对")
    return info


def issue_token(sessions_file: Path, user_id: str) -> str:
    """签发一个登录令牌并落盘（便于人工吊销）。"""
    sessions = _read_json(sessions_file, {})
    if not isinstance(sessions, dict):
        sessions = {}
    # 顺手清理过期令牌，避免文件无限增长
    now = int(time.time())
    sessions = {
        tok: rec for tok, rec in sessions.items()
        if isinstance(rec, dict) and int(rec.get("expires_at") or 0) > now
    }
    token = secrets.token_urlsafe(32)
    sessions[token] = {"user_id": str(user_id), "issued_at": now,
                       "expires_at": now + _TOKEN_TTL_S}
    _write_json(sessions_file, sessions)
    return token


def resolve_token(sessions_file: Path, token: str) -> str | None:
    """令牌 → user_id；无效或过期返回 None。"""
    if not token:
        return None
    sessions = _read_json(sessions_file, {})
    if not isinstance(sessions, dict):
        return None
    rec = sessions.get(token)
    if not isinstance(rec, dict):
        return None
    if int(rec.get("expires_at") or 0) <= int(time.time()):
        return None
    return str(rec.get("user_id") or "") or None


def token_owner(sessions_file: Path, token: str) -> str | None:
    """给 /admin 用：查这个令牌是谁。同 resolve_token，语义更清楚。"""
    return resolve_token(sessions_file, token)


def revoke_token(sessions_file: Path, token: str) -> bool:
    sessions = _read_json(sessions_file, {})
    if not isinstance(sessions, dict):
        return False
    existed = sessions.pop(token, None) is not None
    if existed:
        _write_json(sessions_file, sessions)
    return existed


# ---------------------------------------------------------------------------
# 邀请码
#
# 解决的是什么问题（这是主人实际提出来的）：
#     "给朋友用，是不是我还要给他们口令才行？不能他们自己创建？"
#   让管理员分发口令有两个真实危害，而且我们当场就撞上了：
#     1. 口令会进聊天记录/短信/微信 —— 那一刻它就该被视为已泄露；
#     2. 管理员**持有**朋友的登录凭据，这是不必要的风险：
#        能看对话是一回事，拿着他的密码是另一回事。
#   邀请码把这两条一起解决：管理员只发一个**一次性**的码，
#   朋友自己设口令，管理员全程不知道。
#
# 设计取舍：
#   · 不做邮箱/短信验证 —— 短信那条路政策上不允许个人申请（见文件顶部说明），
#     邮箱验证要发信服务，对这个规模都是过重设计。
#   · 码是**一次性**的：被转发出去也只能用一次，损失有上限。
#   · 码有有效期（默认 7 天）：避免一个半年前泄露的码在某个时刻突然能用。
# ---------------------------------------------------------------------------
# 生成码用的字母表：刻意**去掉容易看错的字符**（0/O、1/l/I），
# 因为主人很可能要口头念给朋友，或者朋友要手打。
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE_LEN = 8
_INVITE_TTL_S = 7 * 24 * 3600


def _new_code() -> str:
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"heytalk-{body}"


def load_invites(invites_file: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(invites_file, {})
    return data if isinstance(data, dict) else {}


def create_invite(invites_file: Path, *, name: str = "",
                  is_admin: bool = False,
                  ttl_s: int = _INVITE_TTL_S) -> str:
    """生成一个一次性邀请码并落盘。返回码本身。"""
    invites = load_invites(invites_file)
    now = int(time.time())
    # 顺手清掉已过期且没用过的码，避免文件无限增长
    invites = {
        c: rec for c, rec in invites.items()
        if isinstance(rec, dict)
        and (rec.get("used_at") or int(rec.get("expires_at") or 0) > now)
    }
    code = _new_code()
    while code in invites:            # 撞码概率极低，但不能假设它不发生
        code = _new_code()
    invites[code] = {
        "name": str(name or ""),
        "is_admin": bool(is_admin),
        "created_at": now,
        "expires_at": now + int(ttl_s),
        "used_by": None,
        "used_at": None,
    }
    _write_json(invites_file, invites)
    return code


def revoke_invite(invites_file: Path, code: str) -> bool:
    invites = load_invites(invites_file)
    existed = invites.pop(code, None) is not None
    if existed:
        _write_json(invites_file, invites)
    return existed


def _check_new_user_id(user_id: str) -> str:
    """校验/规范化一个新用户的 id（与 set_user 用同一套规则）。"""
    uid = str(user_id).strip()
    if not uid:
        raise AuthError("用户名不能为空")
    if len(uid) > 32:
        raise AuthError("用户名太长（最多 32 个字符）")
    if any(c in uid for c in ("/", "\\", "..", "\x00")):
        raise AuthError("用户名不能包含 / \\ 或 ..")
    if uid != uid.strip() or any(c.isspace() for c in uid):
        raise AuthError("用户名不能包含空格")
    return uid


def redeem_invite(invites_file: Path, users_file: Path, code: str,
                  user_id: str, password: str) -> dict[str, Any]:
    """用邀请码换取一个账号。成功返回该用户信息。

    失败一律抛 AuthError，且**不区分**"码不存在 / 码已用过 / 码过期"
    的具体措辞之外的细节 —— 但这里要区分，因为这个码是主人私下发的，
    说出"已用过"能帮他判断是不是被转发了。
    """
    invites = load_invites(invites_file)
    rec = invites.get(str(code).strip())
    if not isinstance(rec, dict):
        raise AuthError("邀请码不对")
    if rec.get("used_at"):
        raise AuthError("这个邀请码已经用过了")
    if int(rec.get("expires_at") or 0) <= int(time.time()):
        raise AuthError("这个邀请码过期了，请让服务提供者重新生成")

    uid = _check_new_user_id(user_id)
    users = load_users(users_file)
    if uid in users:
        raise AuthError(f"用户名 {uid} 已经有人用了，换一个")

    set_user(users_file, uid, password, name=str(rec.get("name") or uid),
             is_admin=bool(rec.get("is_admin")))

    # 标记已用（保留记录，便于主人日后查"谁什么时候用的哪个码"）
    rec["used_by"] = uid
    rec["used_at"] = int(time.time())
    invites[str(code).strip()] = rec
    _write_json(invites_file, invites)

    # ★ 必须**重新读**用户表。
    #   上面那个 `users` 是 set_user 之前读的快照，里面还没有新用户 ——
    #   直接拿它取值会 KeyError（我第一次就是这么写的，测试当场报出来）。
    created = load_users(users_file).get(uid)
    if not isinstance(created, dict):
        raise AuthError("账号创建失败，请重试")
    return {**created, "user_id": uid}
