# -*- coding: utf-8 -*-
"""
tests/test_security_masking.py —— 凭据泄露的回归保护（P0 安全修复）

背景：修复前 src/core/query_student.py 会把含账号密码的 MONGO_URI 直接打印，
而 scripts/../ui/dashboard.py 会把子进程 stdout 原样渲染到**公网页面**上。
本文件用「源码级断言」把这条路堵死，避免以后有人不小心改回去。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import paths  # noqa: E402

# 禁止的写法：把敏感值直接插进 f-string / format（只允许经过 mask_* 处理）
FORBIDDEN_INTERPOLATIONS = (
    re.compile(r"\{\s*MONGO_URI\s*\}"),
    re.compile(r"\{\s*ZHIPU_API_KEY\s*\}"),
    re.compile(r"\{\s*os\.environ\[[^\]]*\]\s*\}"),
)


class TestNoRawSecretInterpolation(unittest.TestCase):
    def _iter_sources(self):
        for path in sorted(SRC.rglob("*.py")):
            yield path, path.read_text(encoding="utf-8", errors="replace")

    def test_no_raw_secret_interpolation_in_src(self):
        offenders = []
        for path, text in self._iter_sources():
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for pattern in FORBIDDEN_INTERPOLATIONS:
                    if pattern.search(line):
                        offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "发现未脱敏的敏感值插值：\n" + "\n".join(offenders))

    def test_mongo_printers_go_through_mask_uri(self):
        for rel in ("src/core/query_student.py", "src/core/init_db.py"):
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("mask_uri", text, f"{rel} 必须用 mask_uri() 打码连接串")

    def test_api_error_paths_mask_model_errors(self):
        text = (SRC / "server" / "api.py").read_text(encoding="utf-8")
        self.assertIn("mask_secrets(str(exc))", text, "模型连接错误必须经 mask_secrets 脱敏后再返回给客户端")
        self.assertNotIn('yield sse("error", {"text": str(exc)})', text, "不要直接返回未脱敏的模型错误")


class TestMaskHelpersAreExported(unittest.TestCase):
    def test_mask_functions_are_public(self):
        self.assertTrue(callable(paths.mask_uri))
        self.assertTrue(callable(paths.mask_secrets))
        self.assertIn("mask_uri", paths.__all__)
        self.assertIn("mask_secrets", paths.__all__)


if __name__ == "__main__":
    unittest.main()
