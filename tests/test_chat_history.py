# -*- coding: utf-8 -*-
"""
tests/test_chat_history.py —— 对话历史持久化的回归保护（S3，纯标准库）
========================================================================

这一层盯的是**隐私与数据完整性**，不是功能好不好看：

    ① 会话隔离：读 A 的会话绝不能拿到 B 的内容；
    ② 恢复保真：存进去再读出来，对话与状态必须一致；
    ③ 过期删除：超过保留期的会话必须被 prune 掉；
    ④ 彻底删除：delete_all 之后目录里不许还有 .json；
    ⑤ **路径安全**：session_id 来自浏览器，属于不可信输入，
       绝不能靠它跳出 history 目录（`../` 之类）；
    ⑥ 损坏文件：半截 JSON / schema 不符时返回 None，不能炸掉整个列表。

测试写到 tests/.tmp_history/（.gitignore 已忽略 tests/.tmp_*/），
跑完自动清理，不碰真实的 data/chat_history/。

    python tests/run_all.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat import history  # noqa: E402
from chat.history import (  # noqa: E402
    MAX_STORED_TURNS,
    HistoryStore,
    new_session_id,
    replace_with_retry,
    validate_session_id,
)

TMP_ROOT = Path(__file__).resolve().parent / ".tmp_history"

SAMPLE_TURNS = [
    {"role": "user", "content": "最近好累", "hint": None, "safety_level": "none"},
    {"role": "assistant", "content": "听起来挺不容易的。", "hint": None},
]
SAMPLE_STATE = {"turn": 1, "dims": {"valence": 0.4}, "valence_history": [0.4]}


class HistoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = TMP_ROOT / self._testMethodName
        shutil.rmtree(self.base, ignore_errors=True)
        self.base.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def store(self, **kwargs) -> HistoryStore:
        return HistoryStore(self.base, **kwargs)


class TestSessionId(HistoryTestBase):
    """⑤ 路径安全：session_id 是不可信输入。"""

    def test_generated_ids_are_valid_and_unique(self):
        ids = {new_session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for value in ids:
            self.assertEqual(validate_session_id(value), value)

    def test_rejects_path_traversal(self):
        for bad in ("../../etc/passwd", "..", ".", "a/b", "a\\b",
                    "session.json", "", None, "x" * 100):
            with self.assertRaises(ValueError, msg=f"必须拒绝：{bad!r}"):
                validate_session_id(bad)

    def test_rejects_ids_that_are_not_uuid_shape(self):
        # 形状不对的一律拒绝：靠"看起来像"做校验最容易被绕过
        for bad in ("12345678-1234-1234-1234-1234567890",  # 少一位
                    "12345678-1234-1234-1234-1234567890ab-c",
                    "gggggggg-1234-1234-1234-1234567890ab"):
            with self.assertRaises(ValueError, msg=f"必须拒绝：{bad!r}"):
                validate_session_id(bad)

    def test_path_stays_inside_base_dir(self):
        store = self.store()
        sid = new_session_id()
        self.assertEqual(store.path_for(sid).parent.resolve(), self.base.resolve())


class TestSaveAndLoad(HistoryTestBase):
    """② 恢复保真。"""

    def test_round_trip(self):
        store = self.store()
        sid = new_session_id()
        store.save(sid, turns=SAMPLE_TURNS, state=SAMPLE_STATE)

        loaded = store.load(sid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.turns, SAMPLE_TURNS)
        self.assertEqual(loaded.state, SAMPLE_STATE)
        self.assertEqual(loaded.session_id, sid)
        self.assertTrue(loaded.created_at)
        self.assertTrue(loaded.updated_at)

    def test_saved_file_is_human_readable_json(self):
        """透明度本身是隐私保护：用户应该能自己打开看、自己删。"""
        store = self.store()
        sid = new_session_id()
        store.save(sid, turns=SAMPLE_TURNS)
        raw = store.path_for(sid).read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertEqual(payload["schema"], 1)
        self.assertIn("最近好累", raw)      # 明文可读，符合"本地透明"的设计
        self.assertIn("\n", raw)            # 格式化过，便于人工检查

    def test_created_at_is_preserved_across_saves(self):
        store = self.store()
        sid = new_session_id()
        first = store.save(sid, turns=SAMPLE_TURNS,
                           now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        later = store.save(sid, turns=SAMPLE_TURNS + SAMPLE_TURNS,
                           now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(first.created_at, later.created_at)
        self.assertNotEqual(first.updated_at, later.updated_at)

    def test_save_creates_directory(self):
        nested = self.base / "a" / "b"
        store = HistoryStore(nested)
        store.save(new_session_id(), turns=SAMPLE_TURNS)
        self.assertTrue(nested.is_dir())

    def test_missing_session_returns_none(self):
        self.assertIsNone(self.store().load(new_session_id()))

    def test_invalid_id_returns_none_instead_of_raising(self):
        store = self.store()
        self.assertIsNone(store.load("../../etc/passwd"))
        self.assertFalse(store.delete("../../etc/passwd"))
        self.assertFalse(store.exists("../../etc/passwd"))

    def test_turns_are_capped(self):
        store = self.store()
        sid = new_session_id()
        many = [{"role": "user", "content": f"m{i}"} for i in range(MAX_STORED_TURNS + 50)]
        saved = store.save(sid, turns=many)
        self.assertEqual(len(saved.turns), MAX_STORED_TURNS)
        # 保留的是最近的，不是最早的
        self.assertEqual(saved.turns[-1]["content"], f"m{MAX_STORED_TURNS + 49}")


class TestAtomicReplaceRetry(HistoryTestBase):
    """回归：抗瞬时文件占用（Windows 上真实发生过）。

    背景：杀软 / 搜索索引器会短暂占用刚写完的文件，于是 `os.replace`
    抛 `PermissionError [WinError 5]` 或 `WinError 1175`。
    这不是逻辑错误，但会让测试出现"随机红" —— 随机的失败比确定的失败更糟，
    因为它会训练人忽略红色。所以落盘改成带重试。

    这里用替身**强制前几次失败**，确定性地覆盖重试路径（不靠运气复现）。
    替身装在 history 模块的命名空间上，并在 tearDown 里严格恢复 ——
    否则会污染同一进程里其它测试（第一版就踩了这个坑）。
    """

    def setUp(self) -> None:
        super().setUp()
        # 打在模块命名空间上，而不是 os 模块上：更精确，也更好恢复
        self._real_replace = history.os.replace

    def tearDown(self) -> None:
        history.os.replace = self._real_replace
        super().tearDown()

    def test_retries_then_succeeds(self):
        src = self.base / "a.tmp"
        dst = self.base / "a.json"
        src.write_text("x", encoding="utf-8")

        attempts = {"n": 0}
        real = self._real_replace

        def flaky(source, target):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError(13, "拒绝访问。")
            return real(source, target)

        history.os.replace = flaky
        replace_with_retry(src, dst, delay=0.001)

        self.assertEqual(attempts["n"], 3, "应当重试到第 3 次才成功")
        self.assertTrue(dst.is_file())
        self.assertFalse(src.exists(), "成功后临时文件应已被移走")

    def test_gives_up_and_raises_after_all_attempts(self):
        src = self.base / "b.tmp"
        dst = self.base / "b.json"
        src.write_text("x", encoding="utf-8")

        attempts = {"n": 0}

        def always_fail(source, target):
            attempts["n"] += 1
            raise PermissionError(13, "拒绝访问。")

        history.os.replace = always_fail
        with self.assertRaises(PermissionError):
            replace_with_retry(src, dst, attempts=3, delay=0.001)

        self.assertEqual(attempts["n"], 3, "应当在用尽尝试次数后放弃")

    def test_succeeds_immediately_when_no_conflict(self):
        src = self.base / "c.tmp"
        dst = self.base / "c.json"
        src.write_text("hello", encoding="utf-8")
        replace_with_retry(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "hello")

    def test_real_save_still_works_after_restore(self):
        """确认替身被恢复：用真实 store 存一次必须成功。"""
        store = self.store()
        store.save(new_session_id(), turns=SAMPLE_TURNS)
        self.assertEqual(len(store.list_sessions()), 1)


class TestIsolation(HistoryTestBase):
    """① 会话隔离。"""

    def test_two_sessions_do_not_mix(self):
        store = self.store()
        a, b = new_session_id(), new_session_id()
        store.save(a, turns=[{"role": "user", "content": "A 的秘密"}])
        store.save(b, turns=[{"role": "user", "content": "B 的秘密"}])

        loaded_a = store.load(a)
        loaded_b = store.load(b)
        self.assertEqual(loaded_a.turns[0]["content"], "A 的秘密")
        self.assertEqual(loaded_b.turns[0]["content"], "B 的秘密")
        self.assertNotIn("B 的秘密", json.dumps(loaded_a.turns, ensure_ascii=False))

    def test_delete_only_affects_target(self):
        store = self.store()
        a, b = new_session_id(), new_session_id()
        store.save(a, turns=SAMPLE_TURNS)
        store.save(b, turns=SAMPLE_TURNS)
        self.assertTrue(store.delete(a))
        self.assertIsNone(store.load(a))
        self.assertIsNotNone(store.load(b))

    def test_list_is_sorted_by_recency(self):
        # 用超长保留期，隔离掉"过期清理"这条无关逻辑——
        # 否则写入 1 月的会话会被正常的 30 天规则删掉，就测不到排序了。
        store = self.store(retention_days=100000)
        old, new = new_session_id(), new_session_id()
        store.save(old, turns=SAMPLE_TURNS, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        store.save(new, turns=SAMPLE_TURNS, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
        listed = store.list_sessions()
        self.assertEqual([s.session_id for s in listed], [new, old])
        self.assertEqual(store.latest().session_id, new)


class TestRetention(HistoryTestBase):
    """③ 过期删除。"""

    def test_prune_removes_only_expired(self):
        store = self.store(retention_days=30)
        fresh, stale = new_session_id(), new_session_id()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        store.save(fresh, turns=SAMPLE_TURNS, now=now)
        store.save(stale, turns=SAMPLE_TURNS, now=now - timedelta(days=45))

        removed = store.prune(now=now)
        self.assertEqual(removed, 1)
        self.assertIsNotNone(store.load(fresh))
        self.assertIsNone(store.load(stale))

    def test_save_triggers_prune(self):
        store = self.store(retention_days=30)
        stale = new_session_id()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        store.save(stale, turns=SAMPLE_TURNS, now=now - timedelta(days=60))
        self.assertIsNotNone(store.load(stale))

        store.save(new_session_id(), turns=SAMPLE_TURNS, now=now)
        self.assertIsNone(store.load(stale), "写入时应顺手清掉过期会话")

    def test_prune_keeps_boundary_session(self):
        store = self.store(retention_days=30)
        sid = new_session_id()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        store.save(sid, turns=SAMPLE_TURNS, now=now - timedelta(days=29))
        self.assertEqual(store.prune(now=now), 0)
        self.assertIsNotNone(store.load(sid))

    def test_age_days(self):
        store = self.store()
        sid = new_session_id()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        saved = store.save(sid, turns=SAMPLE_TURNS, now=now - timedelta(days=10))
        self.assertAlmostEqual(saved.age_days(now=now), 10.0, places=2)


class TestDeleteAll(HistoryTestBase):
    """④ 彻底删除。"""

    def test_delete_all_removes_every_file(self):
        store = self.store()
        for _ in range(3):
            store.save(new_session_id(), turns=SAMPLE_TURNS)
        self.assertEqual(len(store.list_sessions()), 3)

        removed = store.delete_all()
        self.assertEqual(removed, 3)
        self.assertEqual(list(self.base.glob("*.json")), [])
        self.assertEqual(store.list_sessions(), [])

    def test_delete_all_on_empty_dir(self):
        self.assertEqual(self.store().delete_all(), 0)

    def test_delete_all_cleans_temp_files(self):
        store = self.store()
        store.ensure_dir()
        leftover = self.base / "abc.json.tmp"
        leftover.write_text("{}", encoding="utf-8")
        store.delete_all()
        self.assertFalse(leftover.exists())


class TestCorruptData(HistoryTestBase):
    """⑥ 损坏数据不能炸掉列表。"""

    def test_broken_json_is_skipped(self):
        store = self.store()
        good = new_session_id()
        store.save(good, turns=SAMPLE_TURNS)

        broken = self.base / f"{new_session_id()}.json"
        broken.write_text("{ 这不是合法 JSON", encoding="utf-8")

        listed = store.list_sessions()
        self.assertEqual([s.session_id for s in listed], [good])

    def test_wrong_schema_is_skipped(self):
        store = self.store()
        sid = new_session_id()
        (self.base / f"{sid}.json").write_text(
            json.dumps({"schema": 999, "turns": []}), encoding="utf-8")
        self.assertIsNone(store.load(sid))
        self.assertEqual(store.list_sessions(), [])

    def test_non_dict_payload_is_skipped(self):
        store = self.store()
        sid = new_session_id()
        (self.base / f"{sid}.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(store.load(sid))

    def test_missing_optional_fields_are_tolerated(self):
        store = self.store()
        sid = new_session_id()
        (self.base / f"{sid}.json").write_text(
            json.dumps({"schema": 1, "session_id": sid}), encoding="utf-8")
        loaded = store.load(sid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.turns, [])
        self.assertEqual(loaded.state, {})
        self.assertEqual(loaded.age_days(), 0.0)


class TestStats(HistoryTestBase):
    def test_stats_reports_counts_only(self):
        store = self.store(retention_days=15)
        store.save(new_session_id(), turns=SAMPLE_TURNS)
        stats = store.stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["retention_days"], 15)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(stats["dir"], str(self.base))
        # 统计里绝不能出现对话内容
        self.assertNotIn("最近好累", json.dumps(stats, ensure_ascii=False))

    def test_summary_does_not_leak_content(self):
        store = self.store()
        sid = new_session_id()
        saved = store.save(sid, turns=[{"role": "user", "content": "我不想活了"}])
        self.assertNotIn("我不想活了", saved.summary())


if __name__ == "__main__":
    unittest.main()
