# -*- coding: utf-8 -*-
"""
tests/test_capabilities.py —— 能力清单的自动检查
================================================================

为什么要有这个测试（而不是只靠一张 CAPABILITIES.md 让人自觉对照）：
    我在换前端/迁移时**至少五次弄丢已有功能**（模型选择器、折叠按钮、
    天气/翻译工具、移动端适配），而单元测试全绿也测不出来 ——
    因为函数一直好好的，丢的是"有没有被接线 / 有没有被调用 / 有没有被搬过来"。
    人写清单会漏看，测试不会。

所以这里对**能静态检查的项**逐一断言：
    后端：路由是否都在；
    前端：关键控件与断点是否都在源码里。
至于"真的去调用了"（天气工具）这类**行为**，由 test_server_api.py 的
TestToolWiring 负责 —— 这里只负责"东西在不在"，两者互补。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WEB_SRC = PROJECT_ROOT / "web" / "src"


def _read(*parts: str) -> str:
    p = Path(WEB_SRC, *parts)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


class TestBackendRoutesExist(unittest.TestCase):
    """后端必须具备这些路由 —— 少一个就是某次迁移漏搬了。"""

    def setUp(self) -> None:
        import server.api as api  # type: ignore
        self.paths = {getattr(r, "path", None) for r in api.routes}
        # 同一路径可能有**多条路由**（例如 /api/history/{id} 一条 GET、一条 DELETE），
        # 所以这里要**并集**而不是覆盖 —— 我第一版写成字典推导，后者把前者盖掉了，
        # 于是测试报"缺少 GET 方法"，其实是测试自己看漏了。
        methods: dict[str, set[str]] = {}
        for r in api.routes:
            path = getattr(r, "path", None)
            if path is None:
                continue
            methods.setdefault(path, set()).update(getattr(r, "methods", None) or set())
        self.methods = methods

    def _require(self, path: str, method: str | None = None) -> None:
        self.assertIn(path, self.paths, f"路由 {path} 不见了 —— 迁移漏搬了？")
        if method is not None:
            self.assertIn(method, self.methods.get(path) or set(),
                          f"路由 {path} 缺少 {method} 方法")

    def test_chat_and_history(self):
        self._require("/api/chat", "POST")
        self._require("/api/history", "GET")
        self._require("/api/history/new", "POST")
        self._require("/api/history/{session_id}", "GET")
        self._require("/api/history/{session_id}", "DELETE")

    def test_auth_and_admin(self):
        self._require("/api/login", "POST")
        self._require("/api/redeem", "POST")
        self._require("/api/logout", "POST")
        self._require("/logout", "GET")
        self._require("/admin", "GET")

    def test_assets(self):
        self._require("/api/icon")
        self._require("/api/wordmark")

    def test_react_frontend_is_mounted(self):
        # /app 是 React 入口；构建产物挂 /static/app
        self.assertIn("/app", self.paths)
        self.assertIn("/app/", self.paths)

    def test_memory_routes_exist(self):
        """★ 记忆层 —— 曾经**整个没接**（`profile_block=None` 写死）。

        这是"迁移丢功能"里最大的一次，而且是**我的能力清单没覆盖到**的那一类。
        所以它必须进这张表：列能力要先问"产品答应了用户什么"。
        """
        self._require("/api/profile", "GET")
        self._require("/api/profile/confirm", "POST")
        self._require("/api/profile/edit", "POST")
        self._require("/api/profile/reject", "POST")
        self._require("/api/profile/{entry_id}", "DELETE")

    def test_review_routes_exist(self):
        """★ 回顾（可选、可关闭、不推送）。"""
        self._require("/api/review/opened", "POST")
        self._require("/api/review/dismiss", "POST")

    def test_voice_route_exists(self):
        self._require("/api/transcribe", "POST")

    def test_memory_is_actually_wired_into_the_prompt(self):
        """★★ 路由存在**不等于**接线了。

        第 15 项那次事故的本质是：代码全都在（`profile.py` 一整层、
        测试全绿），**但服务端把 `profile_block` 写死成 None** ——
        函数好好的，只是没人用它。
        所以这里**直接检查源码里那一行的形态**，而不是只看路由。
        """
        src = (PROJECT_ROOT / "src" / "server" / "api.py").read_text(
           encoding="utf-8")
        self.assertIn("profile_block=_profile_block(", src,
                      "★ 记忆没有接进 assemble_chat_messages —— 又变成'不会记得你'了")
        # 反向检查：不该再有写死的 None（注释除外）
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("profile_block=None", stripped,
                             "★ 记忆块又被写死成 None 了")


class TestFrontendCapabilitiesExist(unittest.TestCase):
    """前端源码里必须具备这些控件/行为 —— 少一个就是重写时漏搬了。"""

    def _has(self, *parts: str, needle: str) -> bool:
        return needle in _read(*parts)

    def test_mode_capsules_are_inside_the_composer(self):
        """两种能力入口必须在输入框内部，并且是真正可点的按钮。

        2026-10-19 起不再向用户展示默认模型或 Pro/Flash 字样；
        输入框里只保留「对话思考」「联网搜索」两个胶囊。
        """
        composer = _read("components", "Composer.tsx")
        self.assertIn('id="thinkingMode"', composer)
        self.assertIn('id="webSearchMode"', composer)
        self.assertIn("aria-pressed", composer)
        self.assertIn("onThinking", composer)
        self.assertIn("onWebSearch", composer)

    def test_sidebar_collapse_has_two_entry_points(self):
        """展开态折叠按钮在侧边栏，收起态在顶部条 —— 各一处，不重复。"""
        self.assertIn("collapseBtn", _read("components", "Sidebar.tsx"))
        self.assertIn("toggleBtn", _read("components", "TopBar.tsx"))

    def test_stop_generation_exists(self):
        """生成中必须能停止（发送键变停止键）。"""
        composer = _read("components", "Composer.tsx")
        self.assertIn("onStop", composer)
        self.assertIn("stop", composer.lower())

    def test_mobile_breakpoint_exists(self):
        """★ 移动端适配必须在（这一项重写时丢过一次）。

        检查的是 `matchMedia` 与断点 —— 不检查具体样式（那会锁死实现），
        只保证"手机屏幕会被识别出来"这件事没被删掉。
        """
        app = _read("App.tsx")
        self.assertIn("matchMedia", app)
        self.assertIn("max-width", app)

    def test_login_page_covers_login_and_invite(self):
        login = _read("components", "Login.tsx")
        self.assertIn("redeem", login, "邀请码开通入口不见了")

    def test_tool_labels_are_rendered(self):
        """工具痕迹要显示成中文（查了天气/用了翻译），不是函数名。"""
        self.assertIn("get_current_weather", _read("useChat.ts"))
        self.assertIn("查了实时天气", _read("useChat.ts"))


class TestPowerShellScriptsKeepTheirEncoding(unittest.TestCase):
    """★ 带中文的 .ps1 必须带 **UTF-8 BOM**。

    根因（实测过，不是猜的）：
        Windows PowerShell 5.1 在没有 BOM 时按**系统 ANSI(GBK)** 读 .ps1。
        于是中文变乱码，而乱码字节会**破坏引号配对** —— 报出来的是一堆
        完全看不懂的语法错误（错误信息里出现"椤圭洰鐩綍"这种就是它）。
        pwsh 7 默认按 UTF-8 读，所以**只在 5.1 上炸** ——
        而"右键 → 使用 PowerShell 运行"用的正是 5.1。

    这条测试防的是**回归**：
        以后任何人用编辑器/工具改一下这个文件、BOM 被顺手去掉，
        pwsh 7 下一切正常、测试全绿，直到主人右键运行才炸 ——
        而那时症状是一屏看不懂的乱码报错。
    """

    SCRIPTS = ("scripts/serve_public.ps1",)
    BOM = b"\xef\xbb\xbf"

    def test_scripts_have_utf8_bom(self):
        for rel in self.SCRIPTS:
            path = PROJECT_ROOT / rel
            self.assertTrue(path.is_file(), f"缺文件：{rel}")
            raw = path.read_bytes()
            self.assertTrue(
                raw.startswith(self.BOM),
                f"{rel} 没有 UTF-8 BOM —— 它在 Windows PowerShell 5.1 下会"
                f"因为中文被按 GBK 解码而报一堆看不懂的语法错误。"
                f"修法：以 UTF-8 **带 BOM** 保存。")

    def test_scripts_actually_contain_non_ascii(self):
        """只在真的含中文时才需要 BOM —— 顺带确认这条测试不是空转。"""
        for rel in self.SCRIPTS:
            raw = (PROJECT_ROOT / rel).read_bytes()
            body = raw[len(self.BOM):] if raw.startswith(self.BOM) else raw
            has_non_ascii = any(b > 0x7F for b in body)
            self.assertTrue(
                has_non_ascii,
                f"{rel} 里全是 ASCII —— 那这条 BOM 测试就失去意义了，"
                f"请确认文件没被清空或替换")


class TestCapabilityChecklistMatchesTests(unittest.TestCase):
    """★ 清单里声称"有测试守着"的项，测试文件必须真实存在。

    这一条防的是"清单写得很漂亮，实际根本没测试"的自我欺骗 ——
    清单的作用是让人对照，测试的作用是自动兜底，两者不能脱节。
    """

    def test_capabilities_file_exists(self):
        self.assertTrue((PROJECT_ROOT / "CAPABILITIES.md").is_file(),
                        "缺少 CAPABILITIES.md —— 迁移时没有对照清单")

    def test_claimed_test_files_exist(self):
        cap = (PROJECT_ROOT / "CAPABILITIES.md").read_text(encoding="utf-8")
        # 清单里提到的测试文件，逐个确认存在
        for name in ("test_server_api", "test_server_auth",
                     "test_chat_conversation", "test_capabilities",
                     "test_server_memory", "test_chat_review",
                     "test_transcribe", "test_ratelimit"):
            self.assertIn(name, cap, f"清单里没提 {name}")
            self.assertTrue(
                (PROJECT_ROOT / "tests" / f"{name}.py").is_file(),
                f"清单声称有 {name} 但它不存在")


if __name__ == "__main__":
    unittest.main()
