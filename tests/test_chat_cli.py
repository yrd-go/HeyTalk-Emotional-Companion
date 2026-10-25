# -*- coding: utf-8 -*-
"""
tests/test_chat_cli.py —— 入口与冒烟测试（P0/P1）

为什么需要这一层：模块级单元测试全绿**不等于**用户能跑起来。
实际踩到的两个坑都不是单测能发现的：

    1. `python src/chat/state.py` 首次实跑直接崩（`ModuleNotFoundError: chat`）——
       因为直接运行脚本时 `<root>/src` 不在 sys.path 上，而单测是带着正确
       sys.path 导入的，恰好绕过了这个坑；
    2. 修完路径引导又暴露 `NameError: Path`（忘了导入）。

所以这里专门在**真实子进程**里跑一遍入口脚本。
受限环境（沙箱禁止创建管道/启动解释器）下自动 SKIP 并说明原因，不伪装成通过。

    python tests/run_all.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import (  # noqa: E402
    CHAT_CONVERSATION_SCRIPT,
    CHAT_PERSONA_SCRIPT,
    CHAT_SAFETY_SCRIPT,
    CHAT_STATE_SCRIPT,
)

SKIP_REASON = "沙箱禁止创建子进程管道 / 无法启动解释器，跳过真实子进程冒烟"




def _run(target: Path, args: list[str], *, interpreter: Path | None = None
         ) -> subprocess.CompletedProcess[str]:
    python_bin = str(interpreter or sys.executable)
    return subprocess.run(
        [python_bin, str(target), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


class TestChatFilesExist(unittest.TestCase):
    def test_paths_constants_point_to_real_files(self):
        for path in (CHAT_SAFETY_SCRIPT, CHAT_STATE_SCRIPT, CHAT_PERSONA_SCRIPT,
                     CHAT_CONVERSATION_SCRIPT):
            self.assertTrue(path.is_file(), f"缺少文件：{path}")


class TestCliSmoke(unittest.TestCase):
    def _run_or_skip(self, target: Path, args: list[str],
                     interpreter: Path | None = None):
        try:
            return _run(target, args, interpreter=interpreter)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"{SKIP_REASON}（{type(exc).__name__}: {exc}）")
            return None

    def test_safety_cli_high_risk(self):
        proc = self._run_or_skip(CHAT_SAFETY_SCRIPT, ["我不想活了"])
        if proc is None:
            return
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HIGH", proc.stdout)
        self.assertIn("12356", proc.stdout)

    def test_safety_cli_missing_argument(self):
        proc = self._run_or_skip(CHAT_SAFETY_SCRIPT, [])
        if proc is None:
            return
        self.assertEqual(proc.returncode, 2)

    def test_state_cli_direct_run_does_not_crash(self):
        """回归：直接运行 state.py 曾因 `from chat.safety import ...` 崩掉。"""
        proc = self._run_or_skip(CHAT_STATE_SCRIPT, ["今天很累，有点撑不住"])
        if proc is None:
            return
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        self.assertIn("valence", proc.stdout)

    def test_state_cli_demo_run(self):
        proc = self._run_or_skip(CHAT_STATE_SCRIPT, [])
        if proc is None:
            return
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

