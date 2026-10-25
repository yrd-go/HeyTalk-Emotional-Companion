/**
 * src/components/ErrorBoundary.tsx —— 崩溃时**自己把原因说出来**
 *
 * ════════════════════════════════════════════════════════════════════════════
 * 为什么要有它（来自一次真实的白屏事故）
 * ════════════════════════════════════════════════════════════════════════════
 * 我在 useChat.ts 里把 `refreshMemory` 的声明写在了 `send` 后面，而 `send` 的
 * useCallback 依赖数组**在渲染时同步求值** —— 那一刻 `const refreshMemory`
 * 还在暂时性死区里，访问它不是 undefined，而是直接抛：
 *
 *     ReferenceError: Cannot access 'refreshMemory' before initialization
 *
 * 这个错误在 React 渲染期间抛出 → `#root` 永远挂不上 → **整个页面一片纯白**，
 * 什么都不说。
 *
 * ★ 最要命的是**没有任何一道检查拦得住它**：
 *     · `tsc --noEmit` 通过（求值顺序不是类型问题）
 *     · `vite build` 通过（打包器不分析 TDZ）
 *     · **716 个测试全绿**（全是 Python 测试，一行前端都不渲染）
 *   我因此把"构建成功"当成了"页面能用"，直接报告完成 ——
 *   直到主人截了一张白屏给我。
 *
 * ★ 所以这个组件的价值不在"防崩"（崩了就是崩了），而在
 *   **把静默失败变成可诊断失败**：
 *     页面宁可显示一句"这里出错了 + 具体原因"，也不要给你一片白。
 *
 * ★ 顺带覆盖两类 React 边界抓不到的错：
 *     · 事件回调 / 定时器 / Promise 里的错（`window.onerror`、
 *       `unhandledrejection`）—— ErrorBoundary **只管渲染期**。
 *     这两类如果不管，症状同样是"看起来什么都没发生"。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  info: string;
}

/** 把错误渲染成一屏能读的东西（不依赖任何样式框架，崩了也要能显示）。 */
function CrashScreen({ title, detail }: { title: string; detail: string }) {
  return (
    <div
      style={{
        font: "14px/1.7 system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif",
        maxWidth: 720,
        margin: "0 auto",
        padding: "48px 24px",
        color: "#1f2328",
      }}
    >
      <h1 style={{ fontSize: 20, margin: "0 0 12px" }}>{title}</h1>
      <p style={{ margin: "0 0 16px", color: "#5b636d" }}>
        这是界面自己出的错，不是你操作的问题。下面这段信息可以直接发给我。
      </p>
      <pre
        style={{
          background: "#f6f8fa",
          border: "1px solid #e6e8ea",
          borderRadius: 8,
          padding: "12px 14px",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontSize: 12.5,
          color: "#c0392b",
          margin: "0 0 16px",
        }}
      >
        {detail}
      </pre>
      <button
        type="button"
        onClick={() => window.location.reload()}
        style={{
          border: "1px solid #d7dbe0",
          background: "#fff",
          borderRadius: 999,
          padding: "7px 18px",
          fontSize: 13,
          cursor: "pointer",
        }}
      >
        刷新试试
      </button>
    </div>
  );
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 打到控制台，方便用开发者工具看完整栈
    console.error("[heytalk] 渲染出错：", error, info.componentStack);
    this.setState({
      info: String(info.componentStack || "").slice(0, 800),
    });
  }

  render() {
    const { error, info } = this.state;
    if (!error) return this.props.children;
    const detail = [
      `${error.name}: ${error.message}`,
      info ? `\n组件栈：${info}` : "",
    ].join("");
    return <CrashScreen title="界面出了点问题" detail={detail} />;
  }
}

/**
 * 装上"渲染期之外"的错误捕获：事件回调、定时器、Promise。
 *
 * 为什么单独做：ErrorBoundary **只捕获渲染期的错**。而事件回调里的异常
 * 会变成一个静默的 console 报错 —— 用户那边看起来就是"点了没反应"，
 * 和这次白屏一样属于**沉默的失败**。
 */
export function installGlobalErrorHandlers(): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { __heytalkErrorHandler?: boolean };
  if (w.__heytalkErrorHandler) return;   // 避免热更新时重复装
  w.__heytalkErrorHandler = true;

  const show = (label: string, detail: string) => {
    const root = document.getElementById("root");
    if (!root) return;
    // 只在**页面确实是空的**时候接管 —— 否则不该拿一个报错盖住正常界面
    if (root.childElementCount > 0) {
      console.error(`[heytalk] ${label}`, detail);
      return;
    }
    root.innerHTML = "";
    const holder = document.createElement("div");
    root.appendChild(holder);
    // 用 React 之外的方式渲染，避免再次经过可能已经坏掉的树
    holder.innerHTML =
      `<div style="font:14px/1.7 system-ui,'Segoe UI','Microsoft YaHei',sans-serif;` +
      `max-width:720px;margin:0 auto;padding:48px 24px;color:#1f2328">` +
      `<h1 style="font-size:20px;margin:0 0 12px">页面没能启动</h1>` +
      `<p style="margin:0 0 16px;color:#5b636d">界面自己出的错，不是你操作的问题。` +
      `下面这段信息可以直接发给我。</p>` +
      `<pre style="background:#f6f8fa;border:1px solid #e6e8ea;border-radius:8px;` +
      `padding:12px 14px;white-space:pre-wrap;word-break:break-word;font-size:12.5px;` +
      `color:#c0392b;margin:0">${escapeHtml(label + " " + detail)}</pre></div>`;
  };

  window.addEventListener("error", (e) => {
    show("未捕获的错误：", `${e.message}（${e.filename}:${e.lineno}）`);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = (e as PromiseRejectionEvent).reason;
    show("未处理的 Promise 异常：",
         r instanceof Error ? `${r.name}: ${r.message}` : String(r));
  });
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
