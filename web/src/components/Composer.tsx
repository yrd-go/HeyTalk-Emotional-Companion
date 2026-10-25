/**
 * src/components/Composer.tsx —— 输入区
 *
 * 结构要点（主人反复强调过的）：
 *   **模型选择器必须在输入框里面**，像 DeepSeek 那样 —— 不是放在下面，
 *   也不是放在侧边栏。上一版为了这个在 Streamlit 上折腾了很久，最后发现
 *   官方 issue（#13601/#10737）就是不支持在 chat_input 里嵌控件，
 *   这也是改用 React 的直接原因之一。现在它就是普通 DOM，想放哪就放哪。
 */
import { useEffect, useRef, useState } from "react";
import {
  MicIcon, SendIcon, StopIcon, ThinkingIcon, WaveIcon,
  WebSearchIcon,
} from "./icons";
import { useVoice } from "../useVoice";

interface Props {
  onSend: (text: string) => void;
  /** 生成中时点这个按钮 → 停止（按钮同时变成方形停止图标） */
  onStop: () => void;
  /** 正在生成中（此时发送键变成停止键） */
  busy?: boolean;
  seed?: string;
  onSeedConsumed?: () => void;
  /** 服务端是否具备本地语音识别能力（决定要不要显示麦克风按钮） */
  voiceEnabled?: boolean;
  /** 界面标语（服务端给，前端只显示 —— 单一真源，避免两处漂移） */
  tagline?: string;
  /** 首屏内联样式：去掉底部外边距，让输入框紧跟在标题下面 */
  inline?: boolean;
  /** 手机底部布局：与上方话题列表共用同一条左边缘 */
  flush?: boolean;
  /** 是否显示底部标语（首屏由 App 单独渲染，避免标语跟着输入框上移） */
  showTagline?: boolean;
  /** 是否让这一轮真正联网检索 */
  webSearch?: boolean;
  onWebSearch?: (enabled: boolean) => void;
  /** 是否启用对话思考（开启时最终回答使用 Pro） */
  thinking?: boolean;
  onThinking?: (enabled: boolean) => void;
}

export default function Composer({
  onSend, onStop, busy, seed, onSeedConsumed,
  voiceEnabled, tagline, inline, flush, showTagline,
  webSearch, onWebSearch, thinking, onThinking,
}: Props) {
  const [text, setText] = useState("");
  const areaRef = useRef<HTMLTextAreaElement>(null);

  // 语音：录在本机、识别在本机（录音不出这台机器）。
  // 识别出来的文字**追加**到已有内容后面 —— 用户可以边打边说。
  const voice = useVoice((spoken) => {
    setText((prev) => (prev ? prev + spoken : spoken));
    requestAnimationFrame(() => {
      const el = areaRef.current;
      if (el) {
        el.focus();
        el.style.height = "auto";
        el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
        el.setSelectionRange(el.value.length, el.value.length);
      }
    });
  });

  // 点开场建议 → 把文字填进输入框，并把光标放到末尾
  useEffect(() => {
    if (!seed) return;
    setText(seed);
    onSeedConsumed?.();
    requestAnimationFrame(() => {
      const el = areaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
      }
    });
  }, [seed, onSeedConsumed]);

  /** 高度自适应：输入多行时长高，但有个上限（否则会把对话挤没） */
  const autoGrow = () => {
    const el = areaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  const submit = () => {
    const value = text.trim();
    if (!value || busy) return;
    onSend(value);
    setText("");
    requestAnimationFrame(() => {
      const el = areaRef.current;
      if (el) el.style.height = "auto";
    });
  };

  return (
    <div className={inline
      ? "w-full max-w-[720px] mx-auto"
      : `${flush ? "px-0" : "px-2"} pb-3 w-full max-w-[720px] mx-auto`}>
      <div
        className="rounded-2xl border border-line bg-white px-4 pt-3 pb-2
                   focus-within:border-brand/45 transition-colors"
      >
        <textarea
          id="input"
          ref={areaRef}
          rows={1}
          value={text}
          placeholder="说点什么…"
          onChange={(e) => { setText(e.target.value); autoGrow(); }}
          onKeyDown={(e) => {
            // Enter 发送、Shift+Enter 换行（聊天软件的通用约定）
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          className="w-full resize-none border-0 outline-none text-[15px]
                     leading-[1.7] text-body placeholder:text-muted bg-transparent"
        />

        <div className="flex items-center justify-between mt-1.5">
          {/* 桌面与手机共用同一组模式胶囊，避免两套输入区慢慢漂移。 */}
          <div className="flex items-center gap-1.5">
            <button
              id="thinkingMode"
              type="button"
              onClick={() => onThinking?.(!thinking)}
              aria-pressed={Boolean(thinking)}
              title={thinking
                ? "本轮对话思考已开启"
                : "开启对话思考"}
              style={thinking
                ? { backgroundColor: "#d9e6f6", color: "#0b1f3a",
                    boxShadow: "inset 0 0 0 1px #c6d8ee" }
                : { backgroundColor: "#ffffff", color: "#181c22",
                    boxShadow: "inset 0 0 0 1px #e6e8ea" }}
              className={`h-7 px-2.5 rounded-full text-[12px]
                          flex items-center gap-1.5 transition-colors
                          ${thinking
                            ? "font-medium shadow-sm"
                            : "hover:bg-[#f7f9fc]"}`}
            >
              <span className="w-[18px] h-[18px] flex items-center justify-center">
                <ThinkingIcon className="w-[17px] h-[17px]" />
              </span>
              对话思考
            </button>
            <button
              id="webSearchMode"
              type="button"
              onClick={() => onWebSearch?.(!webSearch)}
              aria-pressed={Boolean(webSearch)}
              title={webSearch
                ? "本轮联网搜索已开启"
                : "开启联网搜索"}
              style={webSearch
                ? { backgroundColor: "#d9e6f6", color: "#0b1f3a",
                    boxShadow: "inset 0 0 0 1px #c6d8ee" }
                : { backgroundColor: "#ffffff", color: "#181c22",
                    boxShadow: "inset 0 0 0 1px #e6e8ea" }}
              className={`h-7 px-2.5 rounded-full text-[12px]
                          flex items-center gap-1.5 transition-colors
                          ${webSearch
                            ? "font-medium shadow-sm"
                            : "hover:bg-[#f7f9fc]"}`}
            >
              <WebSearchIcon className="w-4 h-4" />
              联网搜索
            </button>
          </div>

          {/* 生成中 → 变成停止键；空闲 → 发送键（发送键在没有文字时禁用）。
              用同一个位置切换，而不是并排两个按钮：并排会让用户犹豫点哪个。 */}
          <div className="flex items-center gap-2">
            {/* ---- 语音输入：只在服务端装了本地识别时出现 ----
                为什么只在那种情况出现：识别在本机跑（录音不出本机），
                没装就点了也没用。而"没装"这件事在界面上不解释，
                用户只会以为按钮坏了。 */}
            {voiceEnabled && (
              <button
                id="voiceBtn"
                type="button"
                onClick={voice.toggle}
                disabled={voice.state === "transcribing" || busy}
                title={
                  voice.state === "recording" ? "停止并识别"
                    : voice.secureContext
                      ? "按住说话（录音不会离开这台电脑）"
                      : "网页录音需要 HTTPS"
                }
                className={`h-8 rounded-full flex items-center justify-center
                            gap-1 transition-colors px-2.5
                            ${voice.state === "recording"
                              ? "bg-[#e5484d] text-white"
                              : "text-[#5b636d] hover:bg-panel-2"}`}
              >
                {voice.state === "recording" ? (
                  <>
                    <WaveIcon className="w-[16px] h-[16px]" />
                    <span className="text-[12px] tabular-nums">
                      {voice.seconds}s
                    </span>
                  </>
                ) : voice.state === "transcribing" ? (
                  <WaveIcon className="w-[16px] h-[16px] animate-pulse" />
                ) : (
                  <MicIcon className="w-[17px] h-[17px]" />
                )}
              </button>
            )}

            {busy ? (
              <button
                id="stop"
                type="button"
                onClick={onStop}
                title="停止生成"
                className="w-8 h-8 rounded-full bg-panel-2 text-[#4a5158]
                           flex items-center justify-center
                           hover:bg-[#e2e5e8] transition-colors"
              >
                <StopIcon className="w-[15px] h-[15px]" />
              </button>
            ) : (
              <button
                id="send"
                type="button"
                onClick={submit}
                disabled={!text.trim()}
                title="发送"
                className="w-8 h-8 rounded-full bg-brand text-white flex items-center
                           justify-center disabled:opacity-40 hover:bg-brand-dark
                           transition-colors"
              >
                <SendIcon className="w-[18px] h-[18px]" />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* 语音出错时把原因说清楚（权限被拒 / 不是 HTTPS / 设备没有）。
          这类失败如果只让按钮"点了没反应"，用户会以为程序坏了。 */}
      {voice.error && (
        <p className="text-center text-[12px] text-[#c0392b] mt-1.5">
          {voice.error}
        </p>
      )}

      {/* 标语（原来是"我不是心理咨询师或医生…"那条职业边界声明）。
          为什么去掉边界声明、只留标语：
            · **边界是功能性的，不是声明式的** —— 危机分层（safety.py）
              在真正需要时会把求助热线给出来，那比一条常驻声明**时机更准**；
              研究（Laestadius 2026）也主张"提醒该发时发、该停时停"。
            · 常驻的"我不是真正的帮助"在用户难过时可能被读成
              "你正在做的事不算数"，而"因使用 AI 而羞耻"是一个被量表
              单独测量过的构念（SAG-AI, IJHCI 2026）。
          文案由服务端给（tagline），前端不写死 —— 避免两处漂移。 */}
      {showTagline !== false && tagline && (
        <p className="text-center text-[12px] text-muted mt-2.5">
          {tagline}
        </p>
      )}
    </div>
  );
}
