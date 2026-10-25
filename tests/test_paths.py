# -*- coding: utf-8 -*-
"""
tests/test_paths.py —— src/paths.py 的纯函数回归测试

覆盖两块最容易「改坏了没人发现」的逻辑：
    1. find_project_root：marker 搜索（取最近命中）、自定义 marker、兜底分支
    2. mask_uri / mask_secrets：凭据脱敏（P0 安全修复的回归保护）

设计约束（重要）：
    本文件**不做任何运行时写盘**，marker 目录全部用仓库里的静态 fixture
    （tests/fixtures/...）。原因：受限沙箱下 Python 子进程新建目录后无法在其下
    再建文件（新建目录 ACL 受限），用 tempfile 造目录会让用例在受限环境假失败。
    静态 fixture 在受限沙箱、普通终端、CI 里行为完全一致。

只用标准库，因此不装任何第三方依赖也能跑：
    python -m unittest discover -s tests -v
    python tests/run_all.py            # 受限环境（不能用 python -m）时用这个
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"

for _path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import paths  # noqa: E402


class TestFindProjectRoot(unittest.TestCase):
    def test_finds_root_by_requirements_marker(self):
        start = FIXTURES / "proj_marker" / "src" / "core"
        self.assertEqual(
            paths.find_project_root(start), (FIXTURES / "proj_marker").resolve()
        )

    def test_nearest_marker_wins_when_nested(self):
        """嵌套目录里应当命中最近的那个根，而不是最外层。"""
        start = FIXTURES / "nested" / "sub" / "repo" / "src"
        self.assertEqual(
            paths.find_project_root(start),
            (FIXTURES / "nested" / "sub" / "repo").resolve(),
        )

    def test_custom_marker_name_is_honoured(self):
        """marker 列表是可配置的：换成自定义文件名后依然能定位。"""
        original = paths._ROOT_MARKERS
        paths._ROOT_MARKERS = ("MY_ROOT_MARKER.txt",)
        self.addCleanup(setattr, paths, "_ROOT_MARKERS", original)
        start = FIXTURES / "custom_marker" / "sub"
        self.assertEqual(
            paths.find_project_root(start), (FIXTURES / "custom_marker").resolve()
        )

    def test_fallback_when_no_marker_found(self):
        """一个 marker 都找不到时兜底到「paths.py 的上两级」，即真实仓库根。"""
        original = paths._ROOT_MARKERS
        paths._ROOT_MARKERS = ("THIS_MARKER_FILE_DOES_NOT_EXIST.xyz",)
        self.addCleanup(setattr, paths, "_ROOT_MARKERS", original)
        self.assertEqual(
            paths.find_project_root(FIXTURES),
            Path(paths.__file__).resolve().parents[1],
        )

    def test_real_project_root_is_this_repo(self):
        self.assertTrue((paths.PROJECT_ROOT / "requirements.txt").is_file())
        self.assertEqual(paths.SRC_DIR, paths.PROJECT_ROOT / "src")


class TestMaskUri(unittest.TestCase):
    def test_hides_user_and_password(self):
        out = paths.mask_uri(
            "mongodb+srv://yrd:s3cr3t@cluster0.abc.mongodb.net/?retryWrites=true"
        )
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("yrd:", out)
        self.assertIn("cluster0.abc.mongodb.net", out)
        self.assertTrue(out.startswith("mongodb+srv://***:***@"))

    def test_uri_without_credentials_is_unchanged(self):
        self.assertEqual(
            paths.mask_uri("mongodb://localhost:27017/"), "mongodb://localhost:27017/"
        )

    def test_empty_and_none(self):
        self.assertEqual(paths.mask_uri(""), "")
        self.assertEqual(paths.mask_uri(None), "")


class TestMaskSecrets(unittest.TestCase):
    def test_covers_common_secret_shapes(self):
        text = (
            "key=sk-abcdefgh12345678 Bearer abcdefgh1234567 "
            "MONGO_URI=mongodb://user:pw@host:27017/ password=hunter2"
        )
        out = paths.mask_secrets(text)
        for secret in ("sk-abcdefgh12345678", "abcdefgh1234567", "user:pw", "hunter2"):
            self.assertNotIn(secret, out)
        self.assertIn("host:27017", out)

    def test_normal_text_is_untouched(self):
        self.assertEqual(paths.mask_secrets("普通日志 12345 完成"), "普通日志 12345 完成")

    def test_empty(self):
        self.assertEqual(paths.mask_secrets(""), "")
        self.assertEqual(paths.mask_secrets(None), "")


class TestExports(unittest.TestCase):
    def test_all_covers_script_paths_and_helpers(self):
        """__all__ 曾经漏列 6 个脚本常量与 bootstrap，这里做回归保护。"""
        for name in (
            "SERVER_SCRIPT", "PROBE_SCRIPT", "RAG_SCRIPT", "MULTI_AGENT_SCRIPT",
            "ROUTER_SCRIPT", "QUERY_STUDENT_SCRIPT", "AGENT_TOOL_SCRIPT",
            "bootstrap", "mask_uri", "mask_secrets",
        ):
            self.assertIn(name, paths.__all__)
            self.assertTrue(hasattr(paths, name), name)


if __name__ == "__main__":
    unittest.main()
