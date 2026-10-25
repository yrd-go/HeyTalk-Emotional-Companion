#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/shoot_ui.py —— 给界面截图（我开发时的"眼睛"）
================================================================

为什么需要这个脚本（这是本项目最重要的一次工具补强）
--------------------------------------------------------------------------
换前端之后，我一直在**盲改**：所有验证都是"测试通过、HTML 结构正确"，
但**没有一个环节能看到 CSS 渲染后的样子**。于是颜色、间距、图标、字距
这些纯视觉的东西全靠猜 —— 结果就是反复改、反复错，每次都要等主人发截图
才知道哪里不对。

这在业界是已知问题：做 Web 编码的 AI Agent 缺"视觉反馈回路"就会瞎改
（参见 ReLook: Vision-Grounded RL for Agentic Web Coding 等研究，
以及 frontend-visualqa / visual-harness 这类专门的视觉 QA 工具）。
标准解法就是**截图回路**：自己截图、自己看、再改。

为什么不用 playwright（装过，走不通）
--------------------------------------------------------------------------
playwright 的 Python 包能装上，但在这个受限环境里**启动浏览器会失败** ——
它用命名管道与浏览器通信，而沙箱禁止打开命名管道
（`Error` 且没有任何可用内核）。改用浏览器**自带的无头截图**：

    msedge --headless=new --screenshot=out.png --window-size=W,H URL

这条路不需要管道；Windows 上一定有 Edge/Chrome；也不用下载 150MB 内核。

★ 两个关键参数（都是踩出来的）
--------------------------------------------------------------------------
    --virtual-time-budget=9000
        让浏览器"快进"虚拟时间，等 JS 与接口把数据渲染上去。
        没有它，截图里会缺开场建议与聊天记录，模型名还停在"加载中…"。

    --user-data-dir=<每次不同>
        localStorage 会跨截图保留（侧边栏收起状态就存在那里）。
        复用同一个 profile 时，第二张图会沿用第一张的状态 ——
        我踩过这个坑：两张截图字节数完全一样，白折腾一轮。
        所以每次都用新的临时目录。

    --window-size=W,H —— ★ W **不要小于 500**
        这是最坑的一个：请求 320px 时，Edge 无头模式的实际**布局宽度**
        仍是 ~438px，但截图只按 320px 输出 —— 于是**右边被裁掉**，
        看起来就像"窄屏下内容溢出、标题和按钮被切了"。
        我为这个"假象"推理了好几轮 CSS（怀疑 flex / align-items /
        fit-content 撑宽），最后靠在页面上打一个临时浮层测量才发现：
            innerWidth = 438   scrollWidth = 438   ← 相等 = 根本没有溢出
        结论：**窄屏的视觉验收只能靠真机或浏览器开发者工具**；
        命令行截图在这个宽度区间不可信。要测"移动端行为"（断点 768px），
        用 600px 这种既触发移动端逻辑、又不会被裁的宽度。

★ 一条方法论（比上面三条都重要）
--------------------------------------------------------------------------
    当"看到的现象"与"代码的推断"对不上时，**不要继续加深推断** ——
    在页面上打一个临时浮层，把真实数字（innerWidth / scrollWidth /
    offsetWidth）渲染出来，截图读数字。
    我这次就是靠它才发现折腾了半天的"溢出"根本不存在。

用法：
    # 先确保服务在跑：python src/server/api.py
    .\\.venv-rag\\Scripts\\python.exe scripts/shoot_ui.py
    # 图片输出到 .devshots/（已在 .gitignore 忽略）
    #
    # 想做隔离验证（不碰真实数据/登录态），起一个探针实例
    # （HEYTALK_DATA_DIR 指向临时目录、HEYTALK_PORT 换端口）：
    #   $env:HEYTALK_DATA_DIR = "C:/tmp/heytalk-probe"
    #   $env:HEYTALK_PORT = "8601"
    #   .\\.venv-rag\\Scripts\\python.exe src/server/api.py
    # 然后把下面的 BASE_URL 改成 http://127.0.0.1:8601
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / ".devshots"
BASE_URL = "http://127.0.0.1:8600"
VIEWPORT = "1440,900"
WAIT_BUDGET_MS = 9000

# 本机浏览器候选（Windows 上基本必定存在其中之一）
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# 要截的状态：(文件名, URL 后缀, 视口)
SHOTS = (
    ("10-open", "", "1440,900"),
    ("11-collapsed", "?collapsed=1", "1440,900"),
    ("12-model-menu", "?menu=1", "1440,900"),
    # 移动端行为（断点 768px）。刻意用 600 而不是 390：
    # 低于 ~500 时 Edge 的布局宽度会被钳制，截图只输出请求宽度，
    # 右边被裁 —— 看起来像溢出，其实是假象（见文件顶部说明）。
    ("13-mobile-collapsed", "", "600,900"),
    ("14-mobile-drawer", "?collapsed=0", "600,900"),
)


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    return None


def shoot(browser: str, name: str, suffix: str,
          viewport: str = VIEWPORT) -> Path | None:
    """截一张图。每次用独立的 profile 目录，避免 localStorage 串状态。"""
    out = OUT_DIR / f"{name}.png"
    out.unlink(missing_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="heytalk-shot-"))
    try:
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--virtual-time-budget={WAIT_BUDGET_MS}",
                f"--screenshot={out}",
                f"--window-size={viewport}",
                f"--user-data-dir={profile}",
                f"{BASE_URL}/{suffix}",
            ],
            capture_output=True,      # 浏览器会往 stderr 吐一堆登录/导入噪声
            timeout=90,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  {name}: 启动失败 {type(exc).__name__}: {exc}")
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if out.is_file():
        print(f"  {name}: {out.stat().st_size // 1024} KB  [{viewport}]")
        return out
    print(f"  {name}: 没有产出文件")
    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    browser = find_browser()
    if browser is None:
        print("[错误] 没找到 Edge/Chrome，请改 BROWSERS 里的路径")
        return 2
    print(f"浏览器：{Path(browser).name}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made = [p for p in (shoot(browser, name, suffix, vp)
                        for name, suffix, vp in SHOTS) if p]
    print(f"\n共 {len(made)} 张，目录：{OUT_DIR}")
    print("提醒：命令行的窄屏截图（<500px）在 Edge 上会被裁，"
          "不要用它的观感判断移动端是否溢出 —— 见文件顶部说明。")
    print("注意：这些是开发用截图，已在 .gitignore 忽略。")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
