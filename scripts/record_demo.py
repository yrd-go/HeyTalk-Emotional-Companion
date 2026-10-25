#!/usr/bin/env python
"""Record the real HeyTalk UI through Edge CDP (local only; no .env access)."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import requests
import websocket

BASE = os.environ.get("HEYTALK_BASE", "http://127.0.0.1:8600")
USER = os.environ.get("HEYTALK_DEMO_USER", "demo-recorder")
PASSWORD = os.environ.get("HEYTALK_DEMO_PASSWORD", "")
OUT = Path(os.environ.get("HEYTALK_RECORD_OUT", "showcase/_recording")).resolve()
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BROWSER = (
    os.environ.get("HEYTALK_BROWSER")
    or os.environ.get("EDGE_PATH")
    or (CHROME if Path(CHROME).is_file() else EDGE)
)
VIEW = (1440, 900)
MOBILE = (390, 844)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_json(url: str, timeout: float = 20.0) -> list[dict]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open(url, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}")


def wait_devtools_ws(log_path: Path, timeout: float = 20.0) -> str:
    start = time.time()
    pattern = re.compile(r"DevTools listening on (ws://[^\s]+)")
    while time.time() - start < timeout:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        match = pattern.search(text)
        if match:
            return match.group(1).replace("localhost", "127.0.0.1")
        sleep(0.2)
    raise RuntimeError("Edge did not report a DevTools websocket")


def browser_ws_url(port: int, timeout: float = 20.0) -> str:
    """Return the browser-level CDP endpoint.

    Edge 126 on Windows can accept the page-level WebSocket and then drop it
    immediately after the first command.  The browser-level endpoint is stable;
    the page is attached below with Target.attachToTarget(flatten=True).
    """
    version = wait_json(f"http://127.0.0.1:{port}/json/version", timeout)
    if isinstance(version, list):
        version = version[0] if version else {}
    url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if not url:
        raise RuntimeError("Edge did not report a browser WebSocket")
    return url.replace("localhost", "127.0.0.1")


def page_ws_url(port: int, timeout: float = 20.0) -> str:
    targets = wait_json(f"http://127.0.0.1:{port}/json/list", timeout)
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
    raise RuntimeError("Browser did not expose a page WebSocket")


class CDP:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(
            url, timeout=15, suppress_origin=True,
            http_proxy_host=None, http_no_proxy=["127.0.0.1", "localhost"],
        )
        self.ws.settimeout(None)
        self.next_id = 1
        self.pending: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.session_id: str | None = None
        self.closed = False
        self.frames: list[dict] = []
        self.events: list[dict] = []
        self.started = time.monotonic()
        self.frame_dir = OUT / "frames"
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _send_raw(self, payload: dict) -> None:
        with self.lock:
            self.ws.send(json.dumps(payload))

    def _reader(self) -> None:
        try:
            while not self.closed:
                message = json.loads(self.ws.recv())
                if "id" in message:
                    waiting = self.pending.pop(int(message["id"]), None)
                    if waiting:
                        waiting["result"] = message.get("result", {})
                        waiting["error"] = message.get("error")
                        waiting["event"].set()
                    continue
                method = message.get("method")
                params = message.get("params", {})
                if method == "Page.screencastFrame":
                    frame = params.get("data", "")
                    if frame:
                        name = f"frame-{len(self.frames):05d}.jpg"
                        (self.frame_dir / name).write_bytes(base64.b64decode(frame))
                        self.frames.append({"t": time.monotonic() - self.started, "file": name})
                    self._send_raw({"id": self.next_id, "method": "Page.screencastFrameAck", "sessionId": self.session_id, "params": {"sessionId": params.get("sessionId", "")}})
                    self.next_id += 1
        except Exception as exc:
            if not self.closed:
                for waiting in list(self.pending.values()):
                    waiting["error"] = {"message": str(exc)}
                    waiting["event"].set()

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        event = threading.Event()
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            waiting = {"event": event}
            self.pending[request_id] = waiting
            payload = {"id": request_id, "method": method, "params": params or {}}
            if self.session_id:
                payload["sessionId"] = self.session_id
            self.ws.send(json.dumps(payload))
        if not event.wait(timeout):
            raise TimeoutError(method)
        if waiting.get("error"):
            raise RuntimeError(waiting["error"].get("message", method))
        return waiting.get("result", {})

    def evaluate(self, expression: str):
        result = self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True, "userGesture": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"].get("text", "Runtime error"))
        return result.get("result", {}).get("value")

    def screenshot(self, name: str) -> None:
        data = self.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"]
        (OUT / "screens" / name).write_bytes(base64.b64decode(data))

    def close(self) -> None:
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass
def login() -> dict:
    response = requests.post(f"{BASE}/api/login", json={"user_id": USER, "password": PASSWORD}, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"login failed: {response.status_code}")
    data = response.json()
    if not data.get("token"):
        raise RuntimeError("login did not return a token")
    return data


def wait_for(cdp: CDP, expression: str, timeout: float = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if cdp.evaluate(expression):
            return True
        sleep(0.25)
    return False


def click(cdp: CDP, expression: str) -> None:
    ok = cdp.evaluate("(() => { const el = " + expression + "; if (!el) return false; el.click(); return true; })()")
    if not ok:
        raise RuntimeError(f"element not found: {expression}")


def click_text(cdp: CDP, text: str) -> None:
    encoded = json.dumps(text, ensure_ascii=False)
    click(cdp, "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes(" + encoded + "))")


def main() -> None:
    if not PASSWORD:
        raise RuntimeError("HEYTALK_DEMO_PASSWORD is required")
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(OUT / "frames", ignore_errors=True)
    (OUT / "frames").mkdir(parents=True, exist_ok=True)
    (OUT / "screens").mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="heytalk-edge-"))
    port = free_port()
    print(f"[record] launch {Path(BROWSER).name} on {port}", flush=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    edge_log = profile / "edge.log"
    log_handle = open(edge_log, "w", encoding="utf-8")
    edge = subprocess.Popen([
        BROWSER,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-gpu-sandbox",
        "--disable-gpu-compositing",
        "--disable-extensions",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        "--remote-debugging-address=127.0.0.1",
        f"--window-size={VIEW[0]},{VIEW[1]}",
        "--force-device-scale-factor=1",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=log_handle, creationflags=flags)
    log_handle.close()
    cdp = None
    try:
        mode = os.environ.get("HEYTALK_CDP_MODE", "page")
        if mode == "browser":
            print("[record] connect CDP browser endpoint", flush=True)
            cdp = CDP(browser_ws_url(port))
            target_infos = cdp.send("Target.getTargets")["targetInfos"]
            pages = [item for item in target_infos if item.get("type") == "page"]
            print(
                "[record] page targets: " + json.dumps(
                    [item.get("url", "") for item in pages], ensure_ascii=False
                ),
                flush=True,
            )
            if pages:
                target = pages[0]
            else:
                created = cdp.send(
                    "Target.createTarget",
                    {"url": "about:blank", "width": VIEW[0], "height": VIEW[1]},
                )
                target = {"targetId": created["targetId"], "url": "about:blank"}
                print("[record] created blank page target", flush=True)
            attached = cdp.send(
                "Target.attachToTarget",
                {"targetId": target["targetId"], "flatten": True},
            )
            cdp.session_id = attached["sessionId"]
            print(f"[record] attached page session {cdp.session_id[:8]}", flush=True)
        else:
            print("[record] connect CDP page endpoint", flush=True)
            cdp = CDP(page_ws_url(port))
        cdp.send("Runtime.enable")
        print("[record] Runtime domain enabled", flush=True)
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": VIEW[0], "height": VIEW[1], "deviceScaleFactor": 1, "mobile": False})
        auth = login()
        print(f"[record] login ok: {auth.get('user_id')}", flush=True)
        cdp.send("Page.navigate", {"url": f"{BASE}/app"})
        if wait_for(cdp, "Boolean(document.querySelector('#loginSubmit'))", 15):
            sleep(1.4)
            cdp.screenshot("login.png")
            print("[record] login page captured", flush=True)
        cdp.evaluate("localStorage.setItem('heytalk_token', " + json.dumps(auth["token"]) + "); location.reload(); true")
        if not wait_for(cdp, "Boolean(document.querySelector('#input'))", 30):
            raise RuntimeError("composer did not appear")
        try:
            click_text(cdp, "开新对话")
            wait_for(cdp, "Boolean(document.querySelector('#welcomeLine'))", 10)
        except Exception:
            pass
        sleep(1.4)
        cdp.screenshot("desktop-home.png")
        print("[record] desktop home captured", flush=True)
# CONTINUE
        cdp.started = time.monotonic()
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 82, "maxWidth": VIEW[0], "maxHeight": VIEW[1], "everyNthFrame": 1})
        events = [{"t": 0.0, "type": "home"}]
        sleep(1.2)
        click_text(cdp, "今天有点累")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "seed"})
        sleep(0.9)
        click(cdp, "document.querySelector('#thinkingMode')")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "thinking-on"})
        sleep(0.7)
        click(cdp, "document.querySelector('#send')")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "send"})
        started_busy = wait_for(cdp, "Boolean(document.querySelector('#stop'))", 8)
        sleep(2.4)
        cdp.screenshot("desktop-thinking.png")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "thinking-shot", "busy": started_busy})
        finished = wait_for(cdp, "!document.querySelector('#stop')", 65)
        sleep(1.0)
        cdp.screenshot("desktop-chat.png")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "reply-finished", "finished": finished})

        copy_expr = "Array.from(document.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === '复制回复')"
        try:
            click(cdp, copy_expr)
            events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "copy"})
            sleep(0.45)
            cdp.screenshot("desktop-actions.png")
        except Exception:
            pass

        # Capture the shareable/history surfaces while the account still only
        # contains the normal conversation. The safety test is deliberately
        # last so it cannot leak into mobile or history screenshots.
        try:
            click_text(cdp, "开新对话")
            wait_for(cdp, "Boolean(document.querySelector('#welcomeLine'))", 10)
            click(cdp, "document.querySelector('#historySearchBtn')")
            if wait_for(cdp, "Boolean(document.querySelector('#historySearch'))", 8):
                sleep(0.5)
                cdp.screenshot("desktop-history-search.png")
                click(cdp, "document.querySelector('#historySearchBtn')")
            click_text(cdp, "开新对话")
            wait_for(cdp, "Boolean(document.querySelector('#welcomeLine'))", 10)
            click(cdp, "document.querySelector('#collapseBtn')")
            sleep(0.7)
            cdp.screenshot("desktop-collapsed.png")
            if wait_for(cdp, "Boolean(document.querySelector('#toggleBtn'))", 8):
                click(cdp, "document.querySelector('#toggleBtn')")
                sleep(0.6)
        except Exception:
            pass

        body_text = cdp.evaluate("document.body.innerText") or ""
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": MOBILE[0], "height": MOBILE[1], "deviceScaleFactor": 1, "mobile": True})
        cdp.evaluate("localStorage.setItem('heytalk_sidebar', 'collapsed'); location.reload(); true")
        if not wait_for(cdp, "Boolean(document.querySelector('#topNewChat'))", 30):
            raise RuntimeError("mobile top bar did not appear")
        sleep(1.2)
        cdp.screenshot("mobile-home.png")
        try:
            click(cdp, "document.querySelector('#toggleBtn')")
            if not wait_for(cdp, "Boolean(document.querySelector('aside [role=button]'))", 10):
                raise RuntimeError("mobile history drawer did not appear")
            cdp.screenshot("mobile-drawer.png")
            click(
                cdp,
                "Array.from(document.querySelectorAll('aside [role=button]')).find(el => el.textContent.trim())",
            )
            if not wait_for(cdp, "document.body.innerText.includes('已思考')", 15):
                raise RuntimeError("mobile history did not open")
        except Exception:
            pass
        sleep(1.2)
        cdp.screenshot("mobile-chat.png")
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": VIEW[0], "height": VIEW[1], "deviceScaleFactor": 1, "mobile": False})
        cdp.evaluate("localStorage.setItem('heytalk_sidebar', 'open'); location.reload(); true")
        if not wait_for(cdp, "Boolean(document.querySelector('#input'))", 30):
            raise RuntimeError("desktop composer did not return after mobile capture")
        sleep(0.9)

        try:
            click_text(cdp, "开新对话")
            if wait_for(cdp, "Boolean(document.querySelector('#welcomeLine'))", 10):
                cdp.evaluate(
                    "(() => { const el = document.querySelector('#input'); "
                    "const setter = Object.getOwnPropertyDescriptor("
                    "window.HTMLTextAreaElement.prototype, 'value').set; "
                    "setter.call(el, '我不想活了'); "
                    "el.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"
                )
                sleep(0.35)
                click(cdp, "document.querySelector('#send')")
                if wait_for(cdp, "document.body.innerText.includes('请现在就联系')", 20):
                    sleep(0.8)
                    cdp.screenshot("desktop-safety.png")
                    events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "safety-shot"})
            click_text(cdp, "开新对话")
            wait_for(cdp, "Boolean(document.querySelector('#welcomeLine'))", 10)
        except Exception as exc:
            events.append({
                "t": round(time.monotonic() - cdp.started, 3),
                "type": "safety-capture-skipped",
                "error": str(exc),
            })

        try:
            showcase_url = os.environ.get("HEYTALK_SHOWCASE_URL", "http://127.0.0.1:8088/")
            cdp.send("Page.navigate", {"url": showcase_url})
            if wait_for(cdp, "document.readyState === 'complete'", 20):
                sleep(1.0)
                cdp.screenshot("showcase-home.png")
                for anchor in ("demo", "capabilities", "architecture", "evidence", "limits"):
                    cdp.evaluate(
                        "document.querySelector(" + json.dumps("#" + anchor)
                        + ")?.scrollIntoView({block: 'start'}); true"
                    )
                    sleep(0.65)
                    cdp.screenshot(f"showcase-{anchor}.png")
                events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "showcase-captured"})
        except Exception as exc:
            events.append({
                "t": round(time.monotonic() - cdp.started, 3),
                "type": "showcase-capture-skipped",
                "error": str(exc),
            })
        cdp.send("Page.stopScreencast")
        events.append({"t": round(time.monotonic() - cdp.started, 3), "type": "end"})

        timeline = {
            "frames": cdp.frames,
            "events": events,
            "finished": finished,
            "busy_seen": started_busy,
            "viewport": {"width": VIEW[0], "height": VIEW[1]},
        }
        (OUT / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "reply.txt").write_text(body_text, encoding="utf-8")
        print(json.dumps({"out": str(OUT), "frames": len(cdp.frames), "finished": finished}, ensure_ascii=False), flush=True)
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            edge.terminate()
            edge.wait(timeout=8)
        except Exception:
            try:
                edge.kill()
            except Exception:
                pass
        if os.environ.get("HEYTALK_KEEP_PROFILE") == "1":
            print(f"[record] profile kept at {profile}", flush=True)
        else:
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
