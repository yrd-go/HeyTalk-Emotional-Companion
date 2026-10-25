# -*- coding: utf-8 -*-
"""
src/chat/history.py —— 对话历史的本机持久化（S3）
================================================================

为什么需要它（一句话）：情绪陪伴场景里，**刷新页面就丢失全部对话是不可接受的**。
聊到一半发现记录没了，用户不会再用第二次。所以历史必须能恢复。

但它同时要满足三条约束（这是本项目对"敏感数据"的底线）：

    1. **只写本机**：文件落在 <仓库根>/data/chat_history/ 下，永不上传云端。
       陪伴对话属于敏感个人信息，放在你能物理接触的机器上，
       比放在任何服务器上都更可控。
    2. **会自动过期**：默认 30 天前的会话在启动/清理时删除，
       避免"用户早忘了，数据还留着"。
    3. **可一键彻底删除**：不止清空当前会话，而是**把文件从磁盘删掉**。

设计取舍：为什么是文件而不是数据库
--------------------------------------------------------------------------
    - 会话数最多几十条、单条几 KB～几百 KB，文件系统完全够用；
    - 不需要额外依赖（sqlite 也能做，但调试时不便肉眼查看）；
    - **最重要的**：用户随时能自己打开看、自己删。透明度本身就是隐私保护。

与 Streamlit 的关系
--------------------------------------------------------------------------
    本模块**不 import streamlit**，是纯标准库实现。原因是：
        - 会话身份（浏览器如何记住自己是谁）属于 UI 层的事，放 chat_app.py；
        - 纯标准库才能被普通 unittest 直接覆盖（见 tests/test_chat_history.py）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "MAX_STORED_TURNS",
    "HistoryStore",
    "StoredSession",
    "validate_session_id",
    "new_session_id",
    "replace_with_retry",
]

# 保留天数：超过这个天数的会话在 prune() 时被删除
DEFAULT_RETENTION_DAYS = 30

# 单个会话最多保存多少条消息（一条用户消息 + 一条回答 = 2 条）。
# 定得足够大，正常使用碰不到；目的是防止异常情况（脚本刷）把文件写到失控。
MAX_STORED_TURNS = 2000

SCHEMA_VERSION = 1

# 会话 id 只允许这些字符：它会被拼进文件名，必须防目录穿越
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def new_session_id() -> str:
    """生成一个不可猜测的会话 id（UUID4）。

    为什么不用自增编号或时间戳：那会让人能猜到别人的会话文件名。
    即使现在只有你一个人用，给朋友用之后这就是个真实问题。
    """
    return str(uuid.uuid4())


def validate_session_id(session_id: Any) -> str:
    """校验会话 id；不合法直接抛 ValueError（不静默放行）。

    这是**安全边界**：session_id 来自 URL / 浏览器，属于不可信输入，
    而它会被用于拼文件路径。必须严格白名单，否则 `../` 就能写到别处去。
    """
    text = str(session_id or "").strip().lower()
    if not _ID_RE.match(text):
        raise ValueError(f"非法的会话 id：{session_id!r}")
    return text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def replace_with_retry(src: Path, dst: Path, *, attempts: int = 5,
                       delay: float = 0.06) -> None:
    """原子替换文件，带短暂重试。

    为什么需要重试（实测踩到的）：
        Windows 上杀软 / 搜索索引器会**短暂占用**刚写完的文件，
        于是 `os.replace` 抛 `PermissionError [WinError 5]` 或
        `WinError 1175`。这不是逻辑错误，但会让测试出现"随机红"，
        而随机的失败比确定的失败更糟 —— 它会训练你忽略红色。
        重试 5 次（累计约 0.3 秒）足够让那个占用窗口过去。

    放在 history.py 而不是 profile.py：history 更底层，
    profile 已经 import 它；反过来放会绕成循环导入。
    """
    last: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:  # WinError 5 / 1175 / 32 都属此类
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    if last is not None:
        raise last


def _to_iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:      # 兼容手写/旧数据里的裸时间
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class StoredSession:
    """一条从磁盘读出来的会话（元数据 + 原始内容）。"""

    session_id: str
    created_at: str
    updated_at: str
    payload: dict[str, Any]
    path: Path

    @property
    def turns(self) -> list[dict[str, Any]]:
        turns = self.payload.get("turns")
        return list(turns) if isinstance(turns, list) else []

    @property
    def state(self) -> dict[str, Any]:
        state = self.payload.get("state")
        return dict(state) if isinstance(state, Mapping) else {}

    @property
    def updated_dt(self) -> datetime | None:
        return _parse_iso(self.updated_at)

    def age_days(self, *, now: datetime | None = None) -> float:
        moment = self.updated_dt
        if moment is None:
            return 0.0
        return ((now or _utc_now()) - moment).total_seconds() / 86400.0

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def summary(self) -> str:
        """给界面用的一行摘要（不含对话内容，避免在列表里暴露隐私）。"""
        users = sum(1 for t in self.turns if t.get("role") == "user")
        when = self.updated_at.replace("T", " ")[:16]
        return f"{when} UTC｜{users} 轮"


class HistoryStore:
    """对话历史的文件存储。

    目录结构：
        <base_dir>/<session_id>.json

    文件格式（schema 1）：
        {
          "schema": 1,
          "session_id": "...",
          "created_at": "2026-10-12T16:00:00+00:00",
          "updated_at": "2026-10-12T16:20:00+00:00",
          "state": {...},      # EmotionState.to_dict()
          "turns": [ {...}, ... ]   # Turn.to_message() + 判定元数据
        }
    """

    def __init__(self, base_dir: Path | str, *,
                 retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.base_dir = Path(base_dir)
        self.retention_days = max(1, int(retention_days))

    # ---- 基础 ----
    def ensure_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    def path_for(self, session_id: str) -> Path:
        return self.base_dir / f"{validate_session_id(session_id)}.json"

    def exists(self, session_id: str) -> bool:
        try:
            return self.path_for(session_id).is_file()
        except ValueError:
            return False

    # ---- 写 ----
    def save(self, session_id: str, *, turns: Iterable[Mapping[str, Any]],
             state: Mapping[str, Any] | None = None,
             retention_days: int | None = None,
             now: datetime | None = None,
             prune_now: datetime | None = None) -> StoredSession:
        """写入（覆盖）一条会话；返回写后的记录。

        原子写：先写临时文件再 os.replace，避免写一半断电留下半个 JSON
        （半截 JSON 会让下次启动读不出来，等于对话丢了）。

        关于两个时间参数（这是修过的一个真 bug）：
            now       既用于写入 updated_at，也作为清理的时间基准；
            prune_now 可单独覆盖清理基准，默认与 now 一致。
        一开始我把"清理基准"写死成真实当前时间，结果：
            - 用注入时间（测试/回放）时，刚写进去的记录会被立刻当成"过期"删掉；
        正确语义是：不显式指定就以 now 为准，跟随调用方的时间线。
        """
        sid = validate_session_id(session_id)
        moment = now or _utc_now()
        self.ensure_dir()
        target = self.path_for(sid)

        previous = self.load(sid) if target.is_file() else None
        created_at = previous.created_at if previous else _to_iso(moment)

        items = [dict(t) for t in turns]
        if len(items) > MAX_STORED_TURNS:          # 只保留最近的部分
            items = items[-MAX_STORED_TURNS:]

        payload = {
            "schema": SCHEMA_VERSION,
            "session_id": sid,
            "created_at": created_at,
            "updated_at": _to_iso(moment),
            "state": dict(state or {}),
            "turns": items,
        }

        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        replace_with_retry(tmp, target)

        # 顺手清理过期文件（只在写的时候做，避免每次启动都扫盘）
        self.prune(retention_days=retention_days,
                   now=prune_now if prune_now is not None else moment)

        return StoredSession(sid, payload["created_at"], payload["updated_at"],
                             payload, target)

    # ---- 读 ----
    def load(self, session_id: str) -> StoredSession | None:
        """读取一条会话；文件不存在或内容损坏时返回 None（不抛异常）。"""
        try:
            path = self.path_for(session_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_VERSION:
            return None
        return StoredSession(
            session_id=str(payload.get("session_id") or session_id),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            payload=dict(payload),
            path=path,
        )

    def list_sessions(self) -> list[StoredSession]:
        """列出全部会话，按最近更新倒序。损坏的文件被跳过（不炸列表）。"""
        if not self.base_dir.is_dir():
            return []
        found: list[StoredSession] = []
        for path in sorted(self.base_dir.glob("*.json")):
            session = self.load(path.stem)
            if session is not None:
                found.append(session)
        found.sort(key=lambda s: s.updated_at, reverse=True)
        return found

    def latest(self) -> StoredSession | None:
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    # ---- 删 ----
    def delete(self, session_id: str) -> bool:
        """从磁盘删除一条会话。返回是否真的删掉了东西。"""
        try:
            path = self.path_for(session_id)
        except ValueError:
            return False
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def delete_all(self) -> int:
        """删除**全部**历史（用户点"彻底删除"时用）。返回删除条数。"""
        if not self.base_dir.is_dir():
            return 0
        count = 0
        for path in list(self.base_dir.glob("*.json")):
            try:
                path.unlink()
                count += 1
            except OSError:
                continue
        # 顺手清掉可能残留的临时文件
        for path in self.base_dir.glob("*.json.tmp"):
            try:
                path.unlink()
            except OSError:
                continue
        return count

    def prune(self, *, retention_days: int | None = None,
              now: datetime | None = None) -> int:
        """删除超过保留期的会话，返回删除条数。"""
        days = max(1, int(retention_days or self.retention_days))
        cutoff = (now or _utc_now()) - timedelta(days=days)
        if not self.base_dir.is_dir():
            return 0
        removed = 0
        for session in self.list_sessions():
            moment = session.updated_dt
            if moment is not None and moment < cutoff:
                if self.delete(session.session_id):
                    removed += 1
        return removed

    def total_size_bytes(self) -> int:
        if not self.base_dir.is_dir():
            return 0
        total = 0
        for path in self.base_dir.glob("*.json"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def stats(self) -> dict[str, Any]:
        """给界面/日志用的统计（**只报数量与体积，不含任何对话内容**）。"""
        sessions = self.list_sessions()
        return {
            "dir": str(self.base_dir),
            "sessions": len(sessions),
            "bytes": self.total_size_bytes(),
            "retention_days": self.retention_days,
            "oldest": sessions[-1].updated_at if sessions else "",
            "newest": sessions[0].updated_at if sessions else "",
        }
