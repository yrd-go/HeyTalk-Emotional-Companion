#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HeyTalk 本地演示启动器。

用法：
    python run.py --demo

它做三件事：
    1. 把运行数据隔离到 `.demo-data/`；
    2. 缺前端产物时自动构建；
    3. 以零 Key 演示引擎启动 Starlette，并打开浏览器。

演示模式不会调用任何外部模型；即使环境里存在真实 Key 也会被忽略。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
WEB_DIR = PROJECT_ROOT / "web"


def _ensure_frontend(*, build: bool) -> None:
    index = WEB_DIR / "dist" / "index.html"
    if index.is_file():
        return
    if not build:
        raise SystemExit(
            "缺少 web/dist/index.html。去掉 --no-build，或先执行：\n"
            "  cd web && npm ci && npm run build"
        )

    npm = shutil.which("npm")
    if not npm:
        raise SystemExit(
            "未找到 npm。请先安装 Node.js 20+，或改用 Docker："
            "docker compose up --build"
        )

    if (WEB_DIR / "package-lock.json").is_file():
        install_cmd = [npm, "ci"]
    else:
        install_cmd = [npm, "install"]
    print("[demo] 首次启动，正在安装前端依赖……")
    subprocess.run(install_cmd, cwd=WEB_DIR, check=True)
    print("[demo] 正在构建前端……")
    subprocess.run([npm, "run", "build"], cwd=WEB_DIR, check=True)


def _open_browser_later(url: str, delay_s: float = 1.2) -> None:
    def _open() -> None:
        time.sleep(delay_s)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HeyTalk 零 Key 本地演示")
    parser.add_argument("--demo", action="store_true",
                        help="使用本地确定性演示引擎（无外部模型调用）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认只允许本机访问")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or os.environ.get("HEYTALK_PORT") or 8600))
    parser.add_argument("--data-dir", default=".demo-data",
                        help="演示数据目录，默认 .demo-data")
    parser.add_argument("--no-build", action="store_true",
                        help="缺少前端产物时直接报错，不自动构建")
    parser.add_argument("--no-open", action="store_true",
                        help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if not args.demo:
        parser.error("请加 --demo；真实模型模式请直接运行 python src/server/api.py")

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ["HEYTALK_DEMO"] = "1"
    os.environ["HEYTALK_DATA_DIR"] = str(data_dir.resolve())
    os.environ["HEYTALK_HOST"] = args.host
    os.environ["HEYTALK_PORT"] = str(args.port)

    _ensure_frontend(build=not args.no_build)

    for path in (str(PROJECT_ROOT), str(SRC_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)

    try:
        from server import api  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name in {"starlette", "uvicorn"}:
            raise SystemExit(
                "缺少 Web 依赖。请先执行：\n"
                "  pip install -r requirements-demo.txt\n"
                "或直接使用：docker compose up --build"
            ) from exc
        raise

    url = f"http://127.0.0.1:{args.port}/app"
    print("[demo] 零 Key 演示模式已启用；不会调用外部模型。")
    print(f"[demo] 演示数据：{data_dir.resolve()}")
    print(f"[demo] 打开：{url}")
    if not args.no_open:
        _open_browser_later(url)

    api.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
