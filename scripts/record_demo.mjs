import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const BASE = process.env.HEYTALK_BASE || "http://127.0.0.1:8600";
const USER = process.env.HEYTALK_DEMO_USER || "demo-recorder";
const PASSWORD = process.env.HEYTALK_DEMO_PASSWORD || "";
const EDGE = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = path.resolve(process.env.HEYTALK_RECORD_OUT || "showcase/_recording");
const VIEW = { width: 1440, height: 900 };
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

if (!PASSWORD) throw new Error("HEYTALK_DEMO_PASSWORD is required");

async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
    server.on("error", reject);
  });
}

async function waitJson(url, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch {}
    await delay(250);
  }
  throw new Error(`Timed out waiting for ${url}`);
}

async function login() {
  const response = await fetch(`${BASE}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER, password: PASSWORD }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.token) throw new Error(data.error || `login failed (${response.status})`);
  return data;
}

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      const handlers = this.listeners.get(message.method) || [];
      handlers.forEach((handler) => handler(message.params || {}));
    });
  }
  async send(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    return await new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, handler) {
    const handlers = this.listeners.get(method) || [];
    handlers.push(handler);
    this.listeners.set(method, handlers);
  }
  close() { try { this.ws.close(); } catch {} }
}

function launchEdge(port, profile) {
  const args = [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-allow-origins=*",
    "--remote-debugging-address=127.0.0.1",
    `--window-size=${VIEW.width},${VIEW.height}`,
    "--force-device-scale-factor=1",
    "about:blank",
  ];
  return spawn(EDGE, args, { stdio: "ignore", windowsHide: true });
}

async function main() {
  console.log("[record] prepare output");
  await fsp.rm(OUT, { recursive: true, force: true });
  await fsp.mkdir(path.join(OUT, "frames"), { recursive: true });
  await fsp.mkdir(path.join(OUT, "screens"), { recursive: true });

  const profile = await fsp.mkdtemp(path.join(os.tmpdir(), "heytalk-edge-"));
  const port = await freePort();
  console.log(`[record] launch Edge on CDP ${port}`);
  const edge = launchEdge(port, profile);
  const targets = await waitJson(`http://127.0.0.1:${port}/json/list`);
  console.log(`[record] page targets: ${targets.length}`);
  const target = targets.find((item) => item.type === "page");
  if (!target) throw new Error("No Edge page target");
  const wsUrl = target.webSocketDebuggerUrl
    .replace("://localhost:", "://127.0.0.1:")
    .replace("://[::1]:", "://127.0.0.1:");
  const cdp = new CDP(wsUrl);
  console.log("[record] connect CDP");
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: VIEW.width, height: VIEW.height, deviceScaleFactor: 1, mobile: false,
  });

  const auth = await login();
  console.log(`[record] login ok: ${auth.user_id}`);
  await cdp.send("Page.navigate", { url: `${BASE}/app` });
  await delay(1500);
  await cdp.send("Runtime.evaluate", {
    expression: `localStorage.setItem('heytalk_token', ${JSON.stringify(auth.token)}); location.reload(); true`,
    awaitPromise: true,
    userGesture: true,
  });

  const js = async (expression) => {
    const result = await cdp.send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Runtime error");
    return result.result ? result.result.value : undefined;
  };
  const waitFor = async (expression, timeoutMs = 30000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (await js(expression)) return true;
      await delay(250);
    }
    return false;
  };
  const shot = async (name) => {
    const result = await cdp.send("Page.captureScreenshot", { format: "png", fromSurface: true });
    await fsp.writeFile(path.join(OUT, "screens", name), Buffer.from(result.data, "base64"));
  };
  const click = async (expression) => {
    const ok = await js(`(() => { const el = ${expression}; if (!el) return false; el.click(); return true; })()`);
    if (!ok) throw new Error(`Element not found: ${expression}`);
  };
  const clickText = (text) => click(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes(${JSON.stringify(text)}))`);

  const ready = await waitFor("Boolean(document.querySelector('#input'))", 30000);
  console.log(`[record] composer ready: ${ready}`);
  if (!ready) throw new Error("App did not reach composer");
  await delay(1400);
  await shot("desktop-home.png");

  const frames = [];
  const events = [];
  const started = Date.now();
  cdp.on("Page.screencastFrame", (params) => {
    const file = `frame-${String(frames.length).padStart(5, "0")}.jpg`;
    fs.writeFileSync(path.join(OUT, "frames", file), Buffer.from(params.data, "base64"));
    frames.push({ t: (Date.now() - started) / 1000, file });
    cdp.send("Page.screencastFrameAck", { sessionId: params.sessionId }).catch(() => {});
  });
  await cdp.send("Page.startScreencast", {
    format: "jpeg", quality: 82, maxWidth: VIEW.width, maxHeight: VIEW.height, everyNthFrame: 1,
  });
  events.push({ t: 0, type: "home" });

  await delay(1200);
  await clickText("今天有点累");
  events.push({ t: (Date.now() - started) / 1000, type: "seed" });
  await delay(1000);
  await click("document.querySelector('#thinkingMode')");
  events.push({ t: (Date.now() - started) / 1000, type: "thinking-on" });
  await delay(700);
  await click("document.querySelector('#send')");
  events.push({ t: (Date.now() - started) / 1000, type: "send" });

  await waitFor("Boolean(document.querySelector('#stop'))", 8000);
  await delay(2600);
  await shot("desktop-thinking.png");
  events.push({ t: (Date.now() - started) / 1000, type: "thinking-shot" });

  const finished = await waitFor("!document.querySelector('#stop')", 65000);
  await delay(1000);
  await shot("desktop-chat.png");
  events.push({ t: (Date.now() - started) / 1000, type: "reply-finished", finished });

  const copy = "Array.from(document.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === '复制回复')";
  try {
    await click(copy);
    events.push({ t: (Date.now() - started) / 1000, type: "copy" });
    await delay(450);
    await shot("desktop-actions.png");
  } catch {}

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
  });
  await delay(1000);
  await shot("mobile-chat.png");
  try { await click("document.querySelector('#topNewChat')"); } catch {}
  await delay(1000);
  await shot("mobile-home.png");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: VIEW.width, height: VIEW.height, deviceScaleFactor: 1, mobile: false,
  });
  await delay(500);
  await cdp.send("Page.stopScreencast").catch(() => {});
  events.push({ t: (Date.now() - started) / 1000, type: "end" });

  await fsp.writeFile(path.join(OUT, "timeline.json"), JSON.stringify({ frames, events }, null, 2));
  await fsp.writeFile(path.join(OUT, "reply.txt"), await js("document.body.innerText"), "utf8");
  cdp.close();
  edge.kill();
  await fsp.rm(profile, { recursive: true, force: true }).catch(() => {});
  console.log(JSON.stringify({ out: OUT, frames: frames.length, events: events.length }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
