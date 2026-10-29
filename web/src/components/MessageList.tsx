/**
 * src/components/MessageList.tsx —— 消息气泡列表
 *
 * 上一版的一个真问题：助手回应上方那句"用 N 秒回了你"是用 Streamlit 的
 * 临时组件显示的，**重跑一次就没了**，刷新后更看不到。现在它是普通文本，
 * 跟着数据走，所以能持久显示。
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  CheckIcon, ChevronDownIcon, CopyIcon, RegenerateIcon, ShareIcon,
  SpeakerIcon, ThinkingIcon, ThumbDownIcon, ThumbUpIcon,
} from "./icons";
import RichText from "./RichText";
import { cancelSpeech, speakText, useSpeechState } from "../speech";
import type { Turn } from "../types";

/**
 * 思考状态行：流式期间的"正在思考… Ns" + 可折叠的思考过程。
 *
 * 为什么要有它（不是装饰）：
 *   · Maister 等待心理学：被占用的等待感觉更短，没有反馈的等待最熬人；
 *   · 官方 DeepSeek App 的 "Thought for Xs" 是同一件事 —— 透明 + 进度可见；
 *   · 思考文本较长时只显示末尾一段，不把正文顶出屏幕。
 */
function ThinkingLine({ turn, showIcon = true }: {
  turn: Turn;
  showIcon?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const thinking = turn.thinking ?? "";
  // 思考耗时在首个正文增量到达时记录；即使模型没返回思考文本，
  // 只要等待超过 1 秒，也要保留这一行 —— 之前它会整行消失。
  const doneSeconds = turn.thinkingMs
    ? Math.max(1, Math.round(turn.thinkingMs / 1000))
    : 0;

  useEffect(() => {
    if (!turn.streaming || !turn.phase) return;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, [turn.streaming, turn.phase]);

  if (turn.streaming && turn.phase) {
    const seconds = Math.max(
      0,
      Math.floor((now - (turn.startedAt ?? now)) / 1000),
    );
    const label = turn.phaseNote ?? turn.phase;
    return (
      <div className="text-[12px] text-muted mb-1.5 pl-0.5">
        <details className="group">
          <summary className="list-none cursor-pointer select-none flex items-center gap-1.5
                              [&::-webkit-details-marker]:hidden">
            {showIcon && (
              <ThinkingIcon className="w-[18px] h-[18px] motion-safe:animate-spin
                                       [animation-duration:3.4s]" />
            )}
            {label}
            {seconds > 0 && <span className="tabular-nums">{seconds}s</span>}
            <ChevronDownIcon className="w-3 h-3 opacity-60 transition-transform
                                       group-open:rotate-180" />
          </summary>
          {thinking && (
            <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap
                          opacity-75 pl-2.5 border-l border-line break-words">
              {thinking.slice(-400)}
            </p>
          )}
        </details>
      </div>
    );
  }

  // 完成态也要留住图标。之前只按"有思考文本 / 等满 1 秒"决定是否显示，
  // 于是历史轮次（没有 thinking 文本、thinkingMs 也没恢复）只剩一行耗时，
  // 用户看不出这条回复经历过"思考"这一步。
  // 主人的要求：图标要一直在 —— 让用户随时能看出这条回复确实经历过
  // "思考"这一步，而不是等满 1 秒才出现。只有被动中断的轮次不显示。
  const showDone = !turn.streaming && !turn.interrupted;
  const doneLabel = doneSeconds > 0 ? `已思考（用时 ${doneSeconds} 秒）` : "已思考";

  if (showDone && !thinking) {
    // 模型没返回可展开的思考文本，但确实等了这么久：保留图标与耗时，
    // 不给一个点开是空白的箭头。
    return (
      <div className="text-[12px] text-muted mb-1.5 pl-0.5 flex items-center gap-1.5 opacity-80">
        {showIcon && <ThinkingIcon className="w-[18px] h-[18px]" />}
        <span>{doneLabel}</span>
      </div>
    );
  }

  if (showDone) {
    return (
      <div className="text-[12px] text-muted mb-1.5 pl-0.5">
        <details className="group">
          <summary className="list-none cursor-pointer select-none flex items-center gap-1.5
                              opacity-80 hover:opacity-100 transition-opacity
                              [&::-webkit-details-marker]:hidden">
            {showIcon && <ThinkingIcon className="w-[18px] h-[18px]" />}
            <span>{doneLabel}</span>
            <ChevronDownIcon className="w-3 h-3 opacity-60 transition-transform
                                       group-open:rotate-180" />
          </summary>
          <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap
                        opacity-75 pl-2.5 border-l border-line break-words">
            {thinking}
          </p>
        </details>
      </div>
    );
  }

  return null;
}

function plainText(text: string): string {
  return text
    .replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/(^|\s)\*([^*\n]+)\*/g, "$1$2");
}

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

function ActionButton({
  label, onClick, active, disabled, children,
}: {
  label: string;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={`w-8 h-8 rounded-lg flex items-center justify-center
                  transition-colors disabled:opacity-30 disabled:cursor-not-allowed
                  ${active
                    ? "text-brand-dark bg-[#edf2f8]"
                    : "text-[#747c85] hover:bg-panel hover:text-brand-dark"}`}
    >
      {children}
    </button>
  );
}

function AssistantActions({
  turn, voiceOutputEnabled, canRegenerate, busy, onRegenerate,
}: {
  turn: Turn;
  voiceOutputEnabled: boolean;
  canRegenerate: boolean;
  busy?: boolean;
  onRegenerate?: (turnId: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<"up" | "down" | null>(null);
  const speech = useSpeechState();
  const speechActive = speech.activeId === turn.id
    && (speech.phase === "loading" || speech.phase === "speaking");
  const speaking = speech.activeId === turn.id && speech.phase === "speaking";
  const speechActiveRef = useRef(false);

  useEffect(() => {
    speechActiveRef.current = speechActive;
  }, [speechActive]);

  useEffect(() => () => {
    if (speechActiveRef.current) cancelSpeech();
  }, []);

  const copy = async () => {
    if (await writeClipboard(plainText(turn.content))) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  const speak = () => {
    if (!voiceOutputEnabled) return;
    if (speechActive) {
      cancelSpeech();
      return;
    }
    speakText(plainText(turn.content), turn.id);
  };

  const share = async () => {
    const text = plainText(turn.content);
    if (navigator.share) {
      try {
        await navigator.share({ title: "Heytalk", text });
        return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
    }
    await copy();
  };

  return (
    <div className="mt-1 flex items-center gap-0.5 pl-0.5">
      <ActionButton label={copied ? "已复制" : "复制回复"} onClick={copy}>
        {copied
          ? <CheckIcon className="w-[17px] h-[17px]" />
          : <CopyIcon className="w-[17px] h-[17px]" />}
      </ActionButton>
      <ActionButton
        label={!voiceOutputEnabled
          ? "先打开顶部音量"
          : speaking
            ? "停止朗读"
            : speechActive ? "正在准备朗读" : "朗读回复"}
        onClick={speak}
        disabled={!voiceOutputEnabled}
        active={speechActive}
      >
        <SpeakerIcon className={`w-[17px] h-[17px] ${
          speechActive && speech.phase === "loading" ? "animate-pulse" : ""
        }`} />
      </ActionButton>
      <ActionButton label="转发回复" onClick={() => void share()}>
        <ShareIcon className="w-[17px] h-[17px]" />
      </ActionButton>
      <ActionButton
        label="这条回复有帮助"
        onClick={() => setVote((v) => v === "up" ? null : "up")}
        active={vote === "up"}
      >
        <ThumbUpIcon className="w-[17px] h-[17px]" />
      </ActionButton>
      <ActionButton
        label="这条回复不够好"
        onClick={() => setVote((v) => v === "down" ? null : "down")}
        active={vote === "down"}
      >
        <ThumbDownIcon className="w-[17px] h-[17px]" />
      </ActionButton>
      <ActionButton
        label={canRegenerate ? "重新生成这条回复" : "这条消息无法重新生成"}
        onClick={() => onRegenerate?.(turn.id)}
        disabled={!canRegenerate || Boolean(busy)}
      >
        <RegenerateIcon className="w-[17px] h-[17px]" />
      </ActionButton>
    </div>
  );
}

function Bubble({
  turn, mobile, voiceOutputEnabled, canRegenerate, busy, onRegenerate,
}: {
  turn: Turn;
  mobile?: boolean;
  voiceOutputEnabled: boolean;
  canRegenerate: boolean;
  busy?: boolean;
  onRegenerate?: (turnId: string) => void;
}) {
  const isUser = turn.role === "user";
  const meta = !isUser && turn.meta.length > 0 ? (
    <div className="text-[12px] text-muted mb-1.5 flex gap-2 pl-0.5">
      {turn.meta.map((m, i) => (
        <span key={i} className="flex items-center gap-2">
          {i > 0 && <span className="text-line">·</span>}
          {m}
        </span>
      ))}
    </div>
  ) : null;

  return (
    <div className={`flex flex-col ${isUser ? "items-end" : "items-start"}`}>
      {!isUser && <ThinkingLine turn={turn} showIcon={!mobile} />}
      {!isUser && meta}

      <div
        className={
          isUser
            ? "max-w-[75%] rounded-2xl rounded-br-md bg-brand text-white px-4 py-2.5"
            : "max-w-[85%] text-body"
        }
      >
        {isUser ? (
          <p className="whitespace-pre-wrap break-words">
            {turn.content}
            {turn.streaming && (
              <span
                className="inline-block w-[7px] h-[15px] ml-0.5 align-[-2px]
                           bg-brand animate-pulse"
                aria-label="正在输入"
              />
            )}
          </p>
        ) : (
          <>
            <RichText text={turn.content} />
            {turn.streaming && (
              <span
                className="inline-block w-[7px] h-[15px] ml-0.5 align-[-2px]
                           bg-brand animate-pulse"
                aria-label="正在输入"
              />
            )}
          </>
        )}
      </div>

      {!isUser && !turn.streaming && (
        <AssistantActions
          turn={turn}
          voiceOutputEnabled={voiceOutputEnabled}
          canRegenerate={canRegenerate}
          busy={busy}
          onRegenerate={onRegenerate}
        />
      )}
    </div>
  );
}

export default function MessageList({
  turns, mobile, voiceOutputEnabled = true, busy, onRegenerate,
}: {
  turns: Turn[];
  mobile?: boolean;
  voiceOutputEnabled?: boolean;
  busy?: boolean;
  onRegenerate?: (turnId: string) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      {turns.map((turn, index) => (
        <Bubble
          key={turn.id}
          turn={turn}
          mobile={mobile}
          voiceOutputEnabled={voiceOutputEnabled}
          canRegenerate={
            turn.role === "assistant"
            && index > 0
            && turns[index - 1].role === "user"
          }
          busy={busy}
          onRegenerate={onRegenerate}
        />
      ))}
    </div>
  );
}
