/**
 * src/api.ts —— 后端接口客户端（含 SSE 流式收数据）
 *
 * 为什么 SSE 用 fetch 而不是 EventSource：
 *   EventSource 只能发 GET，而我们需要 POST 把消息体发过去。
 *   所以用 fetch + ReadableStream 手工解析 SSE 帧。
 *   （这一点在原生版里已经验证过，这里照搬同一套逻辑。）
 */
import type {
  Bootstrap,
  DonePayload,
  HistoryItem,
  MetaPayload,
  SessionDetail,
  ToolStatus,
} from "./types";

const BASE = ""; // 与页面同源

/**
 * 登录令牌存在 localStorage。
 *
 * 为什么不用 cookie：前后端同源没错，但 cookie 会被浏览器自动带上，
 * 反而不好排查"到底有没有登录"；放在这里可以随时在 devtools 里看到、
 * 也可以一键删掉重来。托管模式的令牌是 7 天有效，朋友的浏览器会一直记住。
 */
const TOKEN_KEY = "heytalk_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下 localStorage 可能不可用 —— 那就退化成"每次都要登录" */
  }
}

/** 统一的请求头：带上令牌（没有令牌时不带，交给后端判 401）。 */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken();
  return {
    ...(extra ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/** 后端用 401 + need_login 表示"要登录"，前端据此弹登录页。 */
export class NeedLoginError extends Error {
  constructor() {
    super("请先登录");
    this.name = "NeedLoginError";
  }
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: authHeaders() });
  if (res.status === 401) throw new NeedLoginError();
  if (!res.ok) throw new Error(`GET ${path} → HTTP ${res.status}`);
  return (await res.json()) as T;
}

export interface LoginResult {
  token: string;
  user_id: string;
  name: string;
  is_admin: boolean;
}

/** 登录。口令错时后端返回 401，这里转成可显示的中文提示。 */
export async function login(
  userId: string,
  password: string,
): Promise<LoginResult> {
  const res = await fetch(`${BASE}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, password }),
  });
  const data = (await res.json().catch(() => ({}))) as Partial<LoginResult> & {
    error?: string;
  };
  if (!res.ok || !data.token) {
    throw new Error(data.error || "登录失败");
  }
  setToken(data.token);
  return data as LoginResult;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${BASE}/api/logout`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch {
    /* 网络失败也要清本地令牌，否则会一直卡在"已登录但无效"的状态 */
  }
  setToken("");
}

/**
 * 语音转文字（**服务端本地识别，录音不出本机**）。
 *
 * 为什么不用浏览器自带的 Web Speech API：它在 Chrome/Edge 上会把**音频
 * 送到 Google**。这个项目唯一的护城河就是"你的话不出这台机器"，
 * 用浏览器识别等于把这条承诺打折（文字没出本机，但录音出了）。
 *
 * 所以：浏览器只负责**录音**（MediaRecorder），识别放到本机服务上跑。
 */
export async function transcribe(blob: Blob): Promise<string> {
  const res = await fetch(`${BASE}/api/transcribe`, {
    method: "POST",
    // 直接 POST 原始字节：只有一路数据，用 multipart 反而多一层出错的地方
    headers: authHeaders({ "Content-Type": blob.type || "audio/webm" }),
    body: blob,
  });
  const data = (await res.json().catch(() => ({}))) as {
    text?: string; error?: string;
  };
  if (!res.ok) {
    throw new Error(data.error || `识别失败（HTTP ${res.status}）`);
  }
  return data.text ?? "";
}

// ---------------------------------------------------------------------------
// 记忆（关于用户的长期信息）
//
// ★ 这里所有写操作都只作用于"待确认队列"里的东西 —— 确认门控是红线：
//   从对话里抽出来的只是**候选**，用户点了"记住"才进档案。
//   一条错的记忆会在之后的**每一次**对话里持续生效，比不记更糟。
// ---------------------------------------------------------------------------
export interface MemoryEntry {
  entry_id: string;
  kind: string;
  key: string;
  value: string;
  updated_at: string;
  times_used: number;
}

export interface MemoryView {
  entries: MemoryEntry[];
  pending: MemoryEntry | null;
}

export async function fetchProfile(): Promise<MemoryView> {
  const res = await fetch(`${BASE}/api/profile`, { headers: authHeaders() });
  if (res.status === 401) throw new NeedLoginError();
  const data = (await res.json().catch(() => ({}))) as Partial<MemoryView>;
  return { entries: data.entries ?? [], pending: data.pending ?? null };
}

async function memoryWrite(
  path: string, body?: Record<string, unknown>, method = "POST",
): Promise<MemoryEntry | null> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = (await res.json().catch(() => ({}))) as {
    entry?: MemoryEntry; error?: string;
  };
  if (!res.ok) throw new Error(data.error || `操作失败（HTTP ${res.status}）`);
  return data.entry ?? null;
}

export const confirmMemory = () => memoryWrite("/api/profile/confirm");
export const editMemory = (value: string) =>
  memoryWrite("/api/profile/edit", { value });
export const rejectMemory = () => memoryWrite("/api/profile/reject");
export const deleteMemory = (entryId: string) =>
  memoryWrite(`/api/profile/${encodeURIComponent(entryId)}`, undefined, "DELETE");

// ---------------------------------------------------------------------------
// 回顾（可选、可关闭、**不推送**）
// ---------------------------------------------------------------------------
/** 用户点开了这次回顾 → 记为"被接受"，下次恢复常规间隔 */
export async function markReviewOpened(): Promise<void> {
  try {
    await fetch(`${BASE}/api/review/opened`, {
      method: "POST", headers: authHeaders(),
    });
  } catch { /* 记账失败无所谓，不影响使用 */ }
}

/** 用户关掉回顾 → 永久不再出现（用户说了就算） */
export async function dismissReview(): Promise<void> {
  try {
    await fetch(`${BASE}/api/review/dismiss`, {
      method: "POST", headers: authHeaders(),
    });
  } catch { /* 同上 */ }
}

/**
 * 用一次性邀请码开通账号。
 *
 * 为什么不是"注册"：这个服务由主人在自己机器上跑，没有发信/短信能力
 * （短信那条路政策上不允许个人申请），所以用主人私下发的**一次性码**
 * 当门槛。好处是**主人全程不需要知道你的口令** —— 你自己设。
 */
export async function redeem(
  code: string,
  userId: string,
  password: string,
): Promise<LoginResult> {
  const res = await fetch(`${BASE}/api/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, user_id: userId, password }),
  });
  const data = (await res.json().catch(() => ({}))) as Partial<LoginResult> & {
    error?: string;
  };
  if (!res.ok || !data.token) {
    throw new Error(data.error || "开通失败");
  }
  setToken(data.token);
  return data as LoginResult;
}

export function fetchBootstrap(): Promise<Bootstrap> {
  return getJSON<Bootstrap>("/api/bootstrap");
}

export function fetchHistory(): Promise<HistoryItem[]> {
  return getJSON<{ sessions: HistoryItem[] }>("/api/history").then(
    (d) => d.sessions ?? [],
  );
}

export async function fetchSession(sessionId: string): Promise<SessionDetail | null> {
  const res = await fetch(`${BASE}/api/history/${encodeURIComponent(sessionId)}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) throw new NeedLoginError();
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as SessionDetail;
}

export async function createSession(): Promise<string> {
  const res = await fetch(`${BASE}/api/history/new`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (res.status === 401) throw new NeedLoginError();
  const data = (await res.json()) as { session_id?: string };
  return data.session_id ?? newSessionId();
}

export async function deleteSession(sessionId: string): Promise<void> {
  await fetch(`${BASE}/api/history/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
}

/**
 * 生成一个与服务端格式一致的会话 id（带连字符的 UUID）。
 * 服务端对 id 是严格白名单（它会被用于拼文件路径），
 * 所以这里**必须**用标准 UUID 形式 —— 无连字符的 hex 会被拒收。
 */
export function newSessionId(): string {
  const rand = globalThis.crypto;
  if (rand && typeof rand.randomUUID === "function") {
    return rand.randomUUID();
  }
  const bytes = new Uint8Array(16);
  rand.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

export interface StreamHandlers {
  onMeta?: (p: MetaPayload) => void;
  onDelta?: (text: string) => void;
  /** 模型思考过程增量（reasoning_content） */
  onThinking?: (text: string) => void;
  onError?: (text: string) => void;
  onDone?: (p: DonePayload) => void;
  /** 这轮用了哪些工具（如 ["get_current_weather"]）及可读状态 */
  onTools?: (names: string[], statuses: ToolStatus[]) => void;
}

export interface SendOptions {
  sessionId: string;
  message: string;
  engine: string;
  thinking?: boolean;
  webSearch?: boolean;
  regenerate?: boolean;
  /** 要重载的 assistant 轮次在服务端 turns 里的下标 */
  regenerateIndex?: number;
  signal?: AbortSignal;
}

/** 发一轮对话，按 SSE 回调流式结果。 */
export async function sendMessage(
  opts: SendOptions,
  handlers: StreamHandlers,
): Promise<void> {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      session_id: opts.sessionId,
      message: opts.message,
      engine: opts.engine,
      thinking: Boolean(opts.thinking),
      web_search: Boolean(opts.webSearch),
      regenerate: Boolean(opts.regenerate),
      regenerate_index: opts.regenerateIndex,
    }),
    signal: opts.signal,
  });
  if (res.status === 401) throw new NeedLoginError();
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // SSE 以**空行**分隔每条事件
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) dispatch(frame, handlers);
  }
  if (buffer.trim()) dispatch(buffer, handlers);
}

function dispatch(frame: string, handlers: StreamHandlers): void {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return;

  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }

  if (event === "delta") {
    handlers.onDelta?.((payload as { text?: string }).text ?? "");
  } else if (event === "thinking") {
    handlers.onThinking?.((payload as { text?: string }).text ?? "");
  } else if (event === "meta") {
    handlers.onMeta?.(payload as MetaPayload);
  } else if (event === "tools") {
    const toolPayload = payload as { names?: string[]; statuses?: ToolStatus[] };
    handlers.onTools?.(toolPayload.names ?? [], toolPayload.statuses ?? []);
  } else if (event === "error") {
    handlers.onError?.((payload as { text?: string }).text ?? "");
  } else if (event === "done") {
    handlers.onDone?.(payload as DonePayload);
  }
}
