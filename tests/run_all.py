# -*- coding: utf-8 -*-
"""
tests/run_all.py —— 不依赖 `python -m` 的测试入口

为什么需要它：
    某些受限环境（例如 DSH 的 workspace-write 沙箱）会拦截 `python -m <模块>`
    形式的调用，但允许直接执行脚本文件。CI 与普通终端用
    `python -m unittest discover -s tests -v` 即可；受限环境下改用：

        python tests/run_all.py

退出码：0 = 全部通过（含 skip），1 = 有失败或错误。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

for _path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main() -> int:
    suite = unittest.TestLoader().discover(str(TESTS_DIR), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\n" + "=" * 70)
    print(f"结果：用例 {result.testsRun} 个 | 失败 {len(result.failures)} | "
          f"错误 {len(result.errors)} | 跳过 {len(result.skipped)}")
    print("=" * 70)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
