/**
 * src/useChat.ts —— 对话状态与流式收发
 *
 * 为什么把逻辑集中在一个 hook 里（而不是拆散到各组件）：
 *   "当前会话 / 轮次列表 / 流式中的那一条"这三样是强耦合的 ——
 *   上一版把它们分散在多个 DOM 操作里，于是出现"流式中的气泡在
 *   切换会话后串到别的会话"这类问题。集中放一处，状态只有一份来源。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type MemoryEntry,
  confirmMemory,
  createSession,
  deleteSession as apiDeleteSession,
  dismissReview as apiDismissReview,
  editMemory,
  fetchBootstrap,
  fetchHistory,
  fetchProfile,
  fetchSession,
  getToken,
  login as apiLogin,
  logout as apiLogout,
  markReviewOpened as apiMarkReviewOpened,
  NeedLoginError,
  newSessionId,
  redeem as apiRedeem,
  rejectMemory,
  sendMessage,
  type StreamHandlers,
} from "./api";
import type { Bootstrap, HistoryItem, Turn } from "./types";

/** 给每一条消息一个稳定的 key（不要用数组下标：切换会话时会错位） */
function makeId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/**
 * 工具名 → 给用户看的中文。
 *
 * 为什么不直接显示 `get_current_weather`：用户不需要看函数名。
 * 但也不能不说 —— 他有权知道"我这句话被送到了天气服务去"。
 * 所以翻译成一句人话，放在回应上方的小字里（与"用了几秒"同一行）。
 */
const TOOL_LABELS: Record<string, string> = {
  get_current_weather: "查了实时天气",
  translate_text: "用了翻译",
  web_search: "联网查了资料",
};

/** 后端阶段标记 → 给用户看的短句（等待被"占用"，感知时间更短） */
const PHASE_LABELS: Record<string, string> = {
  role: "正在理解你",
  tools: "正在判断要不要查资料",
  searching: "正在联网查资料",
  generating: "开始组织语言",
  routed: "已切快速通道",
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? `用了${name}`;
}

function toolStatusLabel(status: string): string | null {
  if (status === "unsupported-language") return "参考译法（未经核对）";
  if (status === "engine-error") return "翻译这次没查成";
  if (status === "empty") return "联网没有查到可用结果";
  return null;
}

/** 把后端存的轮次转成界面用的 Turn */
function toTurns(
  stored: { role: string; content: string; elapsed_ms?: number;
            persona_role?: string; interrupted?: boolean;
            tools?: string[]; thinking?: string }[],
): Turn[] {
  return stored.map((t) => {
    const meta: string[] = [];
    const interrupted = Boolean(t.interrupted);
    if (t.role === "assistant") {
      if (interrupted) {
        // 半句要如实标注：不标的话它看起来像一句正常说完的话
        meta.push("被你停掉了");
      }
      // ★ 刻意**不显示** persona_role（良师/见证者/同行者…）。
      //   姿态是**内部机制**：它的作用是让说话方式变合适，而不是让用户
      //   知道"系统给我切了个角色"。把标签显出来等于让用户去读仪表盘 ——
      //   这和提示词里那条"不许给情绪贴标签"是同一个错误
      //   （主人明确说过：用户不需要知道这个，要的是简洁有力的语言风格
      //   与优雅的聊天姿态，而不是角色名）。
      //   字段仍然留在数据里（排查问题时有用），只是不往界面上放。
      // 工具痕迹是补充信息，统一放在思考行下面
      for (const name of t.tools ?? []) meta.push(toolLabel(name));
    }
    return {
      id: makeId(),
      role: t.role === "user" ? "user" : "assistant",
      content: t.content,
      meta,
      interrupted,
      tools: t.tools,
      thinking: t.role === "assistant" ? t.thinking ?? "" : undefined,
      // 历史轮次没有流式思考文本，用后端记录的耗时把"已思考"这一行
      // 和图标一起恢复出来；否则刷新后用户只看得到正文。
      thinkingMs: t.role === "assistant" && !interrupted
        ? t.elapsed_ms ?? 0
        : undefined,
    };
  });
}

export interface ChatState {
  ready: boolean;
  boot: Bootstrap | null;
  turns: Turn[];
  history: HistoryItem[];
  sessionId: string;
  engine: string;
  thinking: boolean;
  webSearch: boolean;
  sending: boolean;
  error: string | null;
}

export function useChat() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [sessionId, setSessionId] = useState<string>("");
  const [engine, setEngine] = useState<string>("");
  const [thinking, setThinking] = useState(false);
  const [webSearch, setWebSearch] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  // ---- 登录态（只有托管模式会用到；本机模式一直是 false）----
  const [needLogin, setNeedLogin] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [userName, setUserName] = useState<string>("");

  /** 流式中的那条助手消息 id —— 用 ref 避免闭包拿到旧值 */
  const streamIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  /** 统一的鉴权失败处理：任何请求拿到 401 都回到登录页。 */
  const handleAuthError = useCallback((e: unknown): boolean => {
    if (e instanceof NeedLoginError) {
      setNeedLogin(true);
      return true;
    }
    return false;
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await fetchHistory());
    } catch (e) {
      // 历史拉不到不该阻塞聊天；但如果是"没登录"，必须让登录页出现
      handleAuthError(e);
    }
  }, [handleAuthError]);

  /** 拉配置。登录前后都要跑一次（登录后才会带上 user_name）。 */
  const loadBootstrap = useCallback(async (): Promise<Bootstrap | null> => {
    try {
      const b = await fetchBootstrap();
      setBoot(b);
      setEngine((prev) => prev || b.default_engine || b.engines[0]?.id || "");
      if (b.user_name) setUserName(b.user_name);
      // 需要登录但本地没有令牌 → 直接显示登录页，不去拉历史（会 401）
      const mustLogin = Boolean(b.auth_required) && !getToken();
      setNeedLogin(mustLogin);
      return b;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, []);

  // 启动：拉配置 + 历史
  useEffect(() => {
    let alive = true;
    (async () => {
      const b = await loadBootstrap();
      if (!alive) return;
      setReady(true);
      if (b && !(b.auth_required && !getToken())) {
        await refreshHistory();
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadBootstrap, refreshHistory]);

  /** 登录：成功后重新拉配置与历史，并把会话清空（换人不能看到上一个人的）。 */
  const signIn = useCallback(
    async (userId: string, password: string) => {
      setLoginError(null);
      try {
        const res = await apiLogin(userId, password);
        setUserName(res.name || res.user_id);
        setNeedLogin(false);
        // ★ 清空本地会话与轮次：换人绝不能残留上一个人的对话
        setTurns([]);
        setSessionId("");
        setHistory([]);
        await loadBootstrap();
        await refreshHistory();
      } catch (e) {
        setLoginError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadBootstrap, refreshHistory],
  );

  /** 用邀请码开通账号（成功后自动登录，无需再输一次）。 */
  const signUp = useCallback(
    async (code: string, userId: string, password: string) => {
      setLoginError(null);
      try {
        const res = await apiRedeem(code, userId, password);
        setUserName(res.name || res.user_id);
        setNeedLogin(false);
        setTurns([]);
        setSessionId("");
        setHistory([]);
        await loadBootstrap();
        await refreshHistory();
      } catch (e) {
        setLoginError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadBootstrap, refreshHistory],
  );

  const signOut = useCallback(async () => {
    abortRef.current?.abort();
    await apiLogout();
    setTurns([]);
    setHistory([]);
    setSessionId("");
    setUserName("");
    setNeedLogin(true);
  }, []);

  const startNewSession = useCallback(async () => {
    abortRef.current?.abort();
    try {
      const sid = await createSession();
      setSessionId(sid);
      setTurns([]);
      setError(null);
      await refreshHistory();
      // 新对话首屏的推荐话题要跟随刚聊过的内容刷新。
      await loadBootstrap();
    } catch (e) {
      if (handleAuthError(e)) return;
      // Even if the server rejects the new session, the UI must clear right
      // away: use a local valid id and retry on the next message.
      setSessionId(newSessionId());
      setTurns([]);
    }
  }, [loadBootstrap, refreshHistory, handleAuthError]);

  const openSession = useCallback(
    async (sid: string) => {
      abortRef.current?.abort();
      setSessionId(sid);
      setError(null);
      setTurns([]);
      try {
        const detail = await fetchSession(sid);
        if (detail) setTurns(toTurns(detail.turns));
      } catch (e) {
        handleAuthError(e);
      }
    },
    [handleAuthError],
  );

  const removeSession = useCallback(
    async (sid: string) => {
      try {
        await apiDeleteSession(sid);
      } catch (e) {
        if (handleAuthError(e)) return;
      }
      if (sid === sessionId) await startNewSession();
      await refreshHistory();
    },
    [sessionId, startNewSession, refreshHistory, handleAuthError],
  );

  // ==========================================================================
  // 记忆（关于用户的长期信息）
  //
  // ★ 确认门控在前端只体现为"显示候选、等用户点"；真正的写入由服务端做，
  //   而且服务端只接受"待确认队列"里的东西（见 api.py 的 profile_* 接口）。
  //
  // ⚠️ **这一段必须放在 `send` 之前**，不能挪到文件末尾。
  //    原因是 JavaScript 的**暂时性死区**：`send` 的 useCallback 依赖数组
  //    `[..., refreshMemory, ...]` 是在**渲染时同步求值**的；
  //    如果 `refreshMemory` 的 `const` 声明在它后面，那一刻它还没初始化，
  //    访问它不是得到 undefined，而是**直接抛 ReferenceError**：
  //        Cannot access 'refreshMemory' before initialization
  //    而这个错误在 React 渲染期间抛出 → `#root` 挂不上 → **整页白屏**。
  //
  //    我这次就是这么踩的，而且 tsc 通过、vite 构建通过、716 个测试全绿
  //    （它们都是 Python 测试，一行前端都不渲染）—— **全都拦不住它**。
  //    教训：**"构建成功"不等于"页面能用"。**
  // ==========================================================================
  const [memoryPending, setMemoryPending] = useState<MemoryEntry | null>(null);
  const [memoryEntries, setMemoryEntries] = useState<MemoryEntry[]>([]);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);

  /** 拉一次记忆状态（候选 + 已确认）。失败静默 —— 记忆是加分项。 */
  const refreshMemory = useCallback(async () => {
    try {
      const view = await fetchProfile();
      setMemoryPending(view.pending);
      setMemoryEntries(view.entries);
    } catch {
      /* 读不到就当作没有记忆，不要打扰用户 */
    }
  }, []);

  const runMemoryAction = useCallback(async (
    action: () => Promise<unknown>,
  ) => {
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      await action();
      await refreshMemory();
    } catch (e) {
      setMemoryError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy(false);
    }
  }, [refreshMemory]);

  const confirmPendingMemory = useCallback(
    () => runMemoryAction(() => confirmMemory()), [runMemoryAction]);
  const editPendingMemory = useCallback(
    (value: string) => runMemoryAction(() => editMemory(value)), [runMemoryAction]);
  const rejectPendingMemory = useCallback(
    () => runMemoryAction(() => rejectMemory()), [runMemoryAction]);

  const makeReplyHandlers = useCallback((replyId: string): StreamHandlers => ({
    onMeta: (p) => {
      if (p.retrying) {
        setTurns((prev) =>
          prev.map((t) =>
            t.id === replyId
              ? { ...t, phase: "网络抖了一下，正在重试", phaseNote: undefined }
              : t,
          ),
        );
        return;
      }
      if (!p.phase) return;
      const label = PHASE_LABELS[p.phase] ?? p.phase;
      setTurns((prev) =>
        prev.map((t) => {
          if (t.id !== replyId) return t;
          const meta =
            p.phase === "routed" && !t.meta.includes("快速通道")
              ? [...t.meta, "快速通道"]
              : t.meta;
          return { ...t, phase: label, phaseNote: p.note, meta };
        }),
      );
    },
    onThinking: (text) => {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === replyId
            ? { ...t, thinking: (t.thinking ?? "") + text, phase: "正在思考" }
            : t,
        ),
      );
    },
    onDelta: (text) => {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === replyId
            ? {
                ...t,
                content: t.content + text,
                phase: undefined,
                phaseNote: undefined,
                thinkingMs:
                  t.thinkingMs ??
                  (t.startedAt
                    ? Math.max(0, Date.now() - t.startedAt)
                    : undefined),
              }
            : t,
        ),
      );
    },
    onTools: (names, statuses) => {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === replyId
            ? {
                ...t,
                meta: [
                  ...names.map(toolLabel),
                  ...statuses
                    .map((item) => toolStatusLabel(item.status))
                    .filter((label): label is string => Boolean(label)),
                ],
                tools: names,
                toolStatuses: statuses,
              }
            : t,
        ),
      );
    },
    onError: (msg) => setError(msg),
    onDone: (p) => {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === replyId
            ? {
                ...t,
                streaming: false,
                phase: undefined,
                phaseNote: undefined,
                thinkingMs: Math.max(t.thinkingMs ?? 0, p.elapsed_ms ?? 0),
              }
            : t,
        ),
      );
    },
  }), []);

  const send = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content || sending) return;

      // 还没有会话就先开一条（把 id 显式拿回来，避免用 setState 的异步值）
      let sid = sessionId;
      if (!sid) {
        try {
          sid = await createSession();
        } catch {
          sid = newSessionId();
        }
        setSessionId(sid);
      }

      const userTurn: Turn = { id: makeId(), role: "user", content, meta: [] };
      const replyId = makeId();
      streamIdRef.current = replyId;
      setTurns((prev) => [
        ...prev,
        userTurn,
        {
          id: replyId,
          role: "assistant",
          content: "",
          meta: [],
          streaming: true,
          thinking: "",
          phase: "正在理解你",
          startedAt: Date.now(),
        },
      ]);
      setSending(true);
      setError(null);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        await sendMessage(
          {
            sessionId: sid,
            message: content,
            engine,
            thinking,
            webSearch,
            signal: controller.signal,
          },
          makeReplyHandlers(replyId),
        );
      } catch (e) {
        if (controller.signal.aborted) {
          // ★ 用户主动停止。
          // 这里**必须**更新状态，否则 streaming 会一直是 true ——
          // 那个闪烁的光标就会永远闪下去（我第一版漏了这一步，
          // 中断分支直接跳过了状态更新，看起来像卡死了）。
          // 已收到的文字**保留**（用户确实在屏幕上看到了），只是标注停掉了。
          setTurns((prev) =>
            prev.map((t) =>
              t.id === replyId
                ? { ...t, streaming: false, interrupted: true,
                    phase: undefined, phaseNote: undefined,
                    meta: ["被你停掉了"] }
                : t,
            ),
          );
        } else {
          const msg = e instanceof Error ? e.message : String(e);
          if (handleAuthError(e)) {
            setTurns((prev) =>
              prev.map((t) =>
                t.id === replyId ? { ...t, streaming: false } : t,
              ),
            );
          } else {
            setError(msg);
            // 出错的空气泡留着很怪：标成提示语而不是静默消失
            setTurns((prev) =>
              prev.map((t) =>
                t.id === replyId
                  ? {
                      ...t,
                      streaming: false,
                      content: t.content || "（这条没送出去，可以再试一次）",
                    }
                  : t,
              ),
            );
          }
        }
      } finally {
        streamIdRef.current = null;
        setSending(false);
        await refreshHistory();
        // 这一轮里可能抽出了新的候选 → 拉一次，让确认卡能出现。
        // 放在 finally 里：即使用户中途停止也要拉（他的话已经被处理过了）。
        await refreshMemory();
      }
    },
    [engine, makeReplyHandlers, refreshHistory, refreshMemory, sending,
     sessionId, thinking, webSearch],
  );

  /** 停止正在生成的回复。
   *
   * 为什么必须有：不能打断是**违反直觉**的（ChatGPT/Claude 都能停），
   * 用户会以为程序坏了。对焦虑型用户尤其重要 ——
   * "我能随时让这件事停下来"本身就是**自主权**，
   * 和项目里"把要不要继续说的决定权留给用户"是同一条原则。
   */
  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /** 重载指定助手回复：服务端截断它之后的对话，再用对应问题重跑。 */
  const regenerate = useCallback(async (turnId: string) => {
    if (sending || !sessionId) return;
    const targetIndex = turns.findIndex((turn) => turn.id === turnId);
    const target = turns[targetIndex];
    if (
      targetIndex < 1
      || !target
      || target.role !== "assistant"
      || turns[targetIndex - 1]?.role !== "user"
    ) return;

    setTurns((prev) =>
      prev.map((turn, index) =>
        index === targetIndex
          ? {
              id: turnId,
              role: "assistant" as const,
              content: "",
              meta: [],
              streaming: true,
              thinking: "",
              phase: "正在重新生成",
              startedAt: Date.now(),
            }
          : turn,
      ),
    );
    setSending(true);
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;
    streamIdRef.current = turnId;
    try {
      await sendMessage(
        {
          sessionId,
          message: "",
          engine,
          thinking,
          webSearch,
          regenerate: true,
          regenerateIndex: targetIndex,
          signal: controller.signal,
        },
        makeReplyHandlers(turnId),
      );
    } catch (e) {
      if (controller.signal.aborted) {
        setTurns((prev) =>
          prev.map((t) =>
            t.id === turnId
              ? {
                  ...t,
                  streaming: false,
                  interrupted: true,
                  phase: undefined,
                  phaseNote: undefined,
                  meta: ["被你停掉了"],
                }
              : t,
          ),
        );
      } else if (handleAuthError(e)) {
        setTurns((prev) =>
          prev.map((t) =>
            t.id === turnId ? { ...t, streaming: false } : t,
          ),
        );
      } else {
        setError(e instanceof Error ? e.message : String(e));
        setTurns((prev) =>
          prev.map((t) =>
            t.id === turnId
              ? {
                  ...t,
                  streaming: false,
                  content: t.content || "（重新生成失败，可以再试一次）",
                }
              : t,
          ),
        );
      }
    } finally {
      streamIdRef.current = null;
      setSending(false);
      await refreshHistory();
      await refreshMemory();
    }
  }, [
    engine, handleAuthError, makeReplyHandlers, refreshHistory, refreshMemory,
    sending, sessionId, thinking, turns, webSearch,
  ]);

  const reset = useCallback(async () => {
    abortRef.current?.abort();
    try {
      await fetch("/api/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch {
      /* 重置失败不影响本地清空 */
    }
    setTurns([]);
  }, [sessionId]);

  /** 用户点开了回顾 → 服务端记账，下次恢复常规间隔 */
  const markReviewOpened = useCallback(() => {
    void apiMarkReviewOpened();
  }, []);

  /** 用户关掉回顾 → 永久不再出现 */
  const dismissReview = useCallback(() => {
    void apiDismissReview();
  }, []);

  return {
    ready, boot, turns, history, sessionId, engine, thinking, webSearch,
    sending, error,
    needLogin, loginError, userName,
    setEngine, setThinking, setWebSearch, send, stop, regenerate,
    startNewSession, openSession, removeSession, reset,
    signIn, signUp, signOut,
    memoryPending, memoryEntries, memoryBusy, memoryError, refreshMemory,
    confirmPendingMemory, editPendingMemory, rejectPendingMemory,
    markReviewOpened, dismissReview,
  };
}
