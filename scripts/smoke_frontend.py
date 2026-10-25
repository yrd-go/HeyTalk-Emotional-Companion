# -*- coding: utf-8 -*-
"""
scripts/smoke_frontend.py —— 前端冒烟检查：**页面到底渲染出来没有**
================================================================

★ 为什么需要这个脚本（它来自一次真实事故）
    我把 `useChat.ts` 里 `refreshMemory` 的声明写在了 `send` 后面，而
    `send` 的 useCallback 依赖数组**在渲染时同步求值** —— 那一刻
    `const refreshMemory` 还在暂时性死区里，于是抛：

        ReferenceError: Cannot access 'refreshMemory' before initialization

    React 渲染失败 → `#root` 挂不上 → **整页纯白，什么都不说**。

    而**四道检查全都放过了它**：
        · `tsc --noEmit`  ✅  求值顺序不是类型错误
        · `vite build`    ✅  打包器不分析 TDZ
        · 716 个测试      ✅  **全是 Python 测试，一行前端都不渲染**
        · 我自己          ❌  构建通过就报告完成，没截图
    最后是主人截了一张白屏给我，我才知道。

    **"构建成功"不等于"页面能用"。** 这个脚本填的就是这个结构性缺口。

★ 判据为什么是"非白像素占比"
    白屏 = 几乎 100% 都是白像素。
    有内容的页面一定有文字/图标/边框 —— 必然产生一批非白像素。
    这个指标**不依赖 DOM 接口**（本机 Edge 的 `--dump-dom` 不可用），
    也**不需要 Playwright**（这个环境里跑不起来），只需一张截图 + Pillow。

★ 它是启发式，不是断言具体内容
    它能抓的是"整页空白 / JS 没跑起来"这一类，**抓不到**"渲染了但内容是错的"。
    所以它是**补充**，不是替代截图人工验收。

用法
--------------------------------------------------------------------------------
    python scripts/smoke_frontend.py                 # 默认查 http://127.0.0.1:8600/app
    python scripts/smoke_frontend.py --port 8614
    python scripts/smoke_frontend.py --threshold 0.01
    python scripts/smoke_frontend.py --keep          # 保留截图，方便自己看

退出码：0 = 有内容；1 = 疑似白屏；2 = 环境问题（浏览器/服务不可用）
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 本机浏览器候选（与 scripts/shoot_ui.py 保持同一套探测）
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# 非白像素低于这个比例就判为"疑似白屏"。
# 0.5% 是很保守的门槛：一屏文字至少占 2~5%，白屏是 0%。
DEFAULT_THRESHOLD = 0.005


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    return None


def server_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/bootstrap", timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def shoot(browser: str, url: str, out: Path, size: str) -> bool:
    """无头截一张图。每次用独立 profile —— localStorage 会跨截图串状态。"""
    profile = Path(tempfile.mkdtemp(prefix="heytalk-smoke-"))
    try:
        subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--virtual-time-budget=9000",
             f"--screenshot={out}", f"--window-size={size}",
             f"--user-data-dir={profile}", url],
            capture_output=True, timeout=120, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 浏览器启动失败：{type(exc).__name__}: {exc}")
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return out.is_file()


def non_white_ratio(path: Path) -> float:
    """算非白像素占比。空白页接近 0。"""
    from PIL import Image   # 项目已依赖 Pillow

    with Image.open(path) as im:
        rgb = im.convert("RGB")
        # 缩到小图再统计：结果几乎一样，但快得多（大图逐像素很慢）
        rgb.thumbnail((400, 400))
        # 用 tobytes() 而不是 getdata()：后者在 Pillow 12 已弃用
        # （官方说 Pillow 14 会移除），而 tobytes 各版本一致且更快。
        raw = rgb.tobytes()
    total = len(raw) // 3
    if total == 0:
        return 0.0
    non_white = 0
    for i in range(0, total * 3, 3):
        if raw[i] < 250 or raw[i + 1] < 250 or raw[i + 2] < 250:
            non_white += 1
    return non_white / total


# ---------------------------------------------------------------------------
# ★ 源码 ↔ 构建产物 一致性检查
#
# 为什么需要它（这是被一次真实误判逼出来的）：
#   主人说"欢迎页文案怎么又滑回中文了？"，我查了半天发现**代码没回退** ——
#   磁盘产物是新的、服务端返回的 HTML 也指向新的。真正原因是他浏览器缓存。
#
#   但那次经历暴露了一个**真实且我抓不到的故障类型**：
#   如果哪天我改了源码**忘了重新构建**，现象会和"缓存"一模一样 ——
#   "我明明改了，怎么没生效"。而当时我**没有任何检查能区分这两者**。
#
# 做法：期望的文案**从源码里自动提取**，不写死在脚本里。
#   好处：改动源码后忘记构建 → 立刻报错；而文案改了什么，脚本不用跟着改。
# ---------------------------------------------------------------------------
# (说明, 源文件, 用于抓取用户可见文案的正则) —— 正则**必须有一个捕获组**
SOURCE_EXPECTATIONS: tuple[tuple[str, str, str], ...] = (
    ("首屏那一句", "web/src/components/Welcome.tsx",
     r">\s*(Click me[^<]{0,80})\s*<"),
    ("输入框占位符", "web/src/components/Composer.tsx",
     r'placeholder="([^"]{2,40})"'),
    ("侧边栏新建按钮", "web/src/components/Sidebar.tsx",
     r">\s*(开新对话)\s*<"),
)


def find_built_bundle() -> Path | None:
    """找 dist 里那个入口 JS（Vite 会带内容哈希）。"""
    assets = PROJECT_ROOT / "web" / "dist" / "assets"
    if not assets.is_dir():
        return None
    js = sorted(assets.glob("index-*.js"),
                key=lambda p: p.stat().st_mtime, reverse=True)
    return js[0] if js else None


def check_build_is_fresh() -> list[str]:
    """★ 构建产物必须比**所有**前端源码都新。

    这是"改了源码忘了重新构建"最直接的判据 —— 比匹配具体文案可靠得多，
    因为**无论改的是文案、样式还是逻辑，它都管用**。

    为什么把这条放在最前面：我原本只做了"文案匹配"检查，结果反例测试里
    报出来的原因是"正则过期了"，而真实原因是"改了源码" ——
    **归因错误会把人带偏**。时间戳不会说谎。
    """
    bundle = find_built_bundle()
    if bundle is None:
        return ["找不到 web/dist/assets/index-*.js —— 前端还没构建过"]

    src_root = PROJECT_ROOT / "web" / "src"
    if not src_root.is_dir():
        return [f"找不到前端源码目录 {src_root}"]

    built_at = bundle.stat().st_mtime
    newer: list[Path] = []
    for p in src_root.rglob("*"):
        if p.is_file() and p.suffix in {".ts", ".tsx", ".css", ".html"}:
            if p.stat().st_mtime > built_at + 1:   # +1s 容忍文件系统时间精度
                newer.append(p)
    if not newer:
        return []

    worst = max(newer, key=lambda p: p.stat().st_mtime)
    rel = worst.relative_to(PROJECT_ROOT)
    return [
        f"★ 有 {len(newer)} 个前端源码文件比构建产物新"
        f"（最新的：{rel}）",
        "   → 改了源码但**没有重新构建**，浏览器拿到的还是旧界面。",
        "     修法：cd web && npx vite build",
    ]


def check_source_consistency() -> list[str]:
    """返回问题列表（空 = 没问题）。"""
    problems: list[str] = []
    bundle = find_built_bundle()
    if bundle is None:
        return ["找不到 web/dist/assets/index-*.js —— 前端还没构建过"]

    text = bundle.read_text(encoding="utf-8", errors="replace")

    for label, rel, pattern in SOURCE_EXPECTATIONS:
        src = PROJECT_ROOT / rel
        if not src.is_file():
            problems.append(f"{label}：源码文件不存在 {rel}")
            continue
        m = re.search(pattern, src.read_text(encoding="utf-8"))
        if not m:
            # 源码里都抓不到 → 说明选择器过期了，要人来修（不能静默跳过）
            problems.append(
                f"{label}：在 {rel} 里没抓到这个文案（正则过期了？），"
                f"请更新 scripts/smoke_frontend.py 的 SOURCE_EXPECTATIONS")
            continue
        if m.re.groups < 1 or m.lastindex is None:
            # 正则写错了（没有捕获组）—— 这是脚本的 bug，要说清楚而不是崩
            problems.append(
                f"{label}：正则没有捕获组，脚本配置有误，请修 "
                f"SOURCE_EXPECTATIONS")
            continue
        needle = m.group(1).strip()
        if needle and needle not in text:
            problems.append(
                f"★ {label}：源码里有「{needle}」，但**构建产物里没有** "
                f"→ 多半是改了源码没重新构建（cd web && npx vite build）")
    return problems


def check_served_matches_dist(port: int) -> list[str]:
    """服务端返回的 HTML 必须指向 dist 里真实存在的产物。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/app", timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return [f"取 /app 失败：{type(exc).__name__}"]

    names = re.findall(r"assets/(index-[A-Za-z0-9_\-]+\.(?:js|css))", html)
    if not names:
        return ["★ /app 的 HTML 里没有任何 index-*.js / .css 引用"]
    assets = PROJECT_ROOT / "web" / "dist" / "assets"
    problems = []
    for n in names:
        if not (assets / n).is_file():
            problems.append(
                f"★ 服务端 HTML 引用了 {n}，但 dist 里没有这个文件 "
                f"→ dist 与服务不一致（重新构建，或服务指向了旧的 dist）")
    return problems


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="前端冒烟检查：页面是否真的渲染出内容")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--path", default="/app")
    parser.add_argument("--url", default="", help="直接给完整 URL（覆盖 port/path）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"非白像素占比下限（默认 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--size", default="1200,800")
    parser.add_argument("--keep", action="store_true", help="保留截图")
    args = parser.parse_args(argv)

    url = args.url or f"http://127.0.0.1:{args.port}{args.path}"

    browser = find_browser()
    if browser is None:
        print("[错误] 没找到 Edge/Chrome，请改 BROWSERS 里的路径")
        return 2

    if not args.url and not server_alive(args.port):
        print(f"[错误] {url} 上的服务不可达。先启动它：")
        print(f"       .\\.venv-rag\\Scripts\\python.exe src/server/api.py")
        print(f"       或：.\\scripts\\serve_public.ps1 -NoTunnel -Port {args.port}")
        return 2

    out_dir = PROJECT_ROOT / ".devshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "smoke-frontend.png"
    out.unlink(missing_ok=True)

    print(f"浏览器 : {Path(browser).name}")
    print(f"目标   : {url}")

    # ---- ⓪ 构建是否新鲜（最直接的"忘了构建"判据）----
    print("\n[0/3] 构建产物是否比源码新")
    fresh_problems = check_build_is_fresh()
    if fresh_problems:
        for p in fresh_problems:
            print("  " + p if p.startswith(" ") else "  ✗ " + p)
    else:
        print("  ✓ 构建产物比所有前端源码都新")

    # ---- ① 源码 ↔ 构建产物 ----
    print("\n[1/3] 源码 ↔ 构建产物一致性")
    src_problems = check_source_consistency()
    if src_problems:
        for p in src_problems:
            print("  ✗ " + p)
    else:
        print("  ✓ 源码里的关键文案都在构建产物里")

    # ---- ② 服务端 HTML ↔ dist ----
    print("\n[2/3] 服务端 HTML ↔ dist 一致性")
    dist_problems = [] if args.url else check_served_matches_dist(args.port)
    if args.url:
        print("  （指定了 --url，跳过）")
    elif dist_problems:
        for p in dist_problems:
            print("  ✗ " + p)
    else:
        print("  ✓ 服务端引用的产物在 dist 里都存在")

    # ---- ③ 真的渲染出来了吗 ----
    print("\n[3/3] 页面是否真的渲染出内容")
    if not shoot(browser, url, out, args.size):
        print("  ✗ 没产出截图")
        return 2
    ratio = non_white_ratio(out)
    pct = ratio * 100
    print(f"  截图 {out.stat().st_size} 字节，非白像素 {pct:.3f}%"
          f"（门槛 {args.threshold * 100:.3f}%）")

    failed = bool(fresh_problems or src_problems or dist_problems) or ratio < args.threshold
    print("\n" + "=" * 60)
    if failed:
        print("✗ 检查未通过：")
        for p in fresh_problems + src_problems + dist_problems:
            print("   " + p if p.startswith(" ") else "   · " + p)
        if ratio < args.threshold:
            print("   · 疑似白屏（页面没渲染出内容）")
            print("     常见原因：渲染期抛异常（控制台会有 ReferenceError）")
        print(f"  截图：{out}")
        return 1

    print("✓ 三项都通过")
    print("  注意：这只证明「构建一致 + 渲染出来了」，")
    print("  **不证明内容好看或正确** —— 布局与文案仍需人眼验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
