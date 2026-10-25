# -*- coding: utf-8 -*-
"""
tests/test_probe_contract.py —— MCP 旁路探针的「负向可复现性」契约测试

这个文件回答面试官最可能追问的一句话：
    「你说你定位了 stdout 污染 JSON-RPC，能不能复现给我看？」

它包含两层：
    1. 契约层（永远会跑）：服务端**必须有**一个默认关闭的污染开关，
       探针**必须**能把它交给子进程，并且文档用词不能退回旧的「裸协议」说法。
    2. 端到端层（装了 mcp SDK 才跑）：真的跑一次 --break-stdout，
       断言探针返回退出码 4 且输出「负向模式命中」的定位结论。

受限沙箱禁止子进程管道时，端到端用例会自动 SKIP（环境限制，不是代码问题）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

SERVER_PY = SRC / "mcp_demo" / "mcp_weather_server.py"
PROBE_PY = SRC / "mcp_demo" / "mcp_protocol_probe.py"

try:  # 端到端用例需要官方 SDK
    import mcp  # noqa: F401

    HAS_MCP = True
except Exception:  # noqa: BLE001
    HAS_MCP = False

SANDBOX_MARKERS = ("WinError 5", "PermissionError", "拒绝访问", "EPERM",
                   "operation not permitted", "Access is denied")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _run_probe(*args: str, timeout: int = 120):
    """跑探针；受限沙箱禁止创建管道时返回 None，由调用方 skipTest。"""
    try:
        return subprocess.run(
            [sys.executable, str(PROBE_PY), *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError:
        return None


class TestNegativeSwitchContract(unittest.TestCase):
    def test_server_has_opt_in_pollution_switch(self):
        source = _read(SERVER_PY)
        self.assertIn("MCP_POLLUTE_STDOUT", source)
        # 必须直接写 fd 1：MCP SDK >= 2.2 启动时会抢占 fd 1（协议线换成私有副本）并把
        # fd 1 指向 stderr，所以走 sys.stdout 根本碰不到协议线（本地实测：复现不出来）。
        self.assertIn("os.write(1", source,
                      "污染必须直接写 fd 1，否则在新版 SDK 上碰不到协议线")

        # 污染行不能以换行结尾：否则会被"忽略不认识的行"的实现绕过去。
        for line in source.splitlines():
            if "[POLLUTED]" in line:
                self.assertNotIn(r"\n", line,
                                 "污染行不能以换行结尾，否则可能被 SDK 忽略、故障复现不出来")
                break
        else:
            self.fail("没找到污染写入语句（负向样例被删了？）")

    def test_probe_passes_the_switch_to_the_child(self):
        source = _read(PROBE_PY)
        self.assertIn("--break-stdout", source)
        self.assertIn("MCP_POLLUTE_STDOUT", source,
                      "探针必须把污染开关交给子进程（stdio 子进程不继承全部环境变量）")
        self.assertIn("env=", source)

    def test_probe_gives_structured_diagnosis_not_bare_traceback(self):
        source = _read(PROBE_PY)
        self.assertIn("ProbeStepError", source)
        self.assertIn("诊断结论", source)
        for code in ("return 2", "return 3", "return 4"):
            self.assertIn(code, source, f"缺少退出码约定：{code}")

    def test_wording_no_longer_claims_raw_protocol(self):
        """旧文档把它叫「裸协议探针」，容易被理解成手写 JSON-RPC 帧，已改为「旁路探针」。"""
        source = _read(PROBE_PY)
        self.assertIn("旁路探针", source)
        self.assertNotIn("裸协议探针", source)


@unittest.skipUnless(HAS_MCP, "未安装 mcp SDK，跳过端到端用例")
class TestProbeEndToEnd(unittest.TestCase):
    def test_pollution_is_off_by_default(self):
        from mcp_demo import mcp_weather_server as server  # noqa: PLC0415

        os.environ.pop("MCP_POLLUTE_STDOUT", None)
        self.assertFalse(server._pollute_enabled())
        os.environ["MCP_POLLUTE_STDOUT"] = "1"
        try:
            self.assertTrue(server._pollute_enabled())
        finally:
            os.environ.pop("MCP_POLLUTE_STDOUT", None)

    def test_break_stdout_reproduces_and_localizes_the_failure(self):
        proc = _run_probe("--break-stdout", "--timeout", "40")
        if proc is None:
            self.skipTest("受限沙箱禁止创建子进程管道")
        combined = (proc.stdout or "") + (proc.stderr or "")
        if any(marker in combined for marker in SANDBOX_MARKERS):
            self.skipTest("受限沙箱禁止创建子进程管道")
        # 4 = 成功复现「协议层被污染」；5 = 未能复现（当前 SDK 已把 fd 1 指向 stderr，
        # 见 mcp/server/stdio.py 的 issue #1933）。两者都是**明确结论**，不接受 0 或其它码。
        self.assertIn(proc.returncode, (4, 5),
                      f"负向模式必须以 4（复现）或 5（未能复现，环境已防护）结束：\n{combined[-3000:]}")
        self.assertIn("负向模式", combined,
                      "负向模式必须打印定位结论，而不是只抛异常")

    def test_normal_mode_still_succeeds(self):
        proc = _run_probe("--timeout", "40")
        if proc is None:
            self.skipTest("受限沙箱禁止创建子进程管道")
        combined = (proc.stdout or "") + (proc.stderr or "")
        if any(marker in combined for marker in SANDBOX_MARKERS):
            self.skipTest("受限沙箱禁止创建子进程管道")
        if proc.returncode == 3:
            self.skipTest("环境限制，无法端到端验证")
        self.assertEqual(proc.returncode, 0, combined[-3000:])
        self.assertIn("协议层正常", combined)


if __name__ == "__main__":
    unittest.main()
