/**
 * 浏览器语音合成：统一选择声线、分段、状态和失败反馈。
 *
 * 为什么集中一份：顶部开关、手机端与电脑端的逐条朗读必须共用同一个
 * “谁在播、怎么停、用哪条声线”的事实源，否则很容易出现按钮亮着但没声音。
 */
import { useSyncExternalStore } from "react";

export type SpeechPhase = "idle" | "loading" | "speaking" | "error";

export interface SpeechSnapshot {
  activeId: string | null;
  phase: SpeechPhase;
  error: string | null;
}

const IDLE: SpeechSnapshot = { activeId: null, phase: "idle", error: null };

let snapshot: SpeechSnapshot = IDLE;
const listeners = new Set<() => void>();
let runToken = 0;
let cachedVoices: SpeechSynthesisVoice[] = [];
let voicesListenerBound = false;

const MALE_VOICE = /(yunyang|yunjian|yunxi|yunye|kangkang|li-mu|male|男)/i;
const FEMALE_VOICE = /(xiaoxiao|xiaoyi|huihui|ting-ting|tingting|mei-jia|meijia|female|女)/i;

function synth(): SpeechSynthesis | null {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) {
    return null;
  }
  return window.speechSynthesis;
}

function emit(next: SpeechSnapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSpeechState(): SpeechSnapshot {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
}

function readVoices(source: SpeechSynthesis): SpeechSynthesisVoice[] {
  try {
    const next = source.getVoices();
    if (next.length > 0) cachedVoices = next;
  } catch {
    // 某些移动浏览器在首次手势前不允许读声线，保留默认 lang 朗读即可。
  }
  return cachedVoices;
}

/** 在不发声的情况下预热声线列表；调用它本身不消耗用户手势权限。 */
export function primeSpeech() {
  const source = synth();
  if (!source) return;
  readVoices(source);
  if (voicesListenerBound) return;
  voicesListenerBound = true;
  source.addEventListener("voiceschanged", () => readVoices(source));
}

function languageFor(text: string): string {
  if (/[\u3400-\u9fff]/.test(text)) return "zh-CN";
  if (/[\u3040-\u30ff]/.test(text)) return "ja-JP";
  if (/[\uac00-\ud7af]/.test(text)) return "ko-KR";
  return "en-US";
}

function chooseVoice(text: string, voices: SpeechSynthesisVoice[]) {
  const lang = languageFor(text).toLowerCase();
  const base = lang.split("-")[0];
  let best: SpeechSynthesisVoice | null = null;
  let bestScore = Number.NEGATIVE_INFINITY;

  for (const voice of voices) {
    const voiceLang = (voice.lang || "").replace("_", "-").toLowerCase();
    let score = 0;
    if (voiceLang === lang) score += 60;
    else if (voiceLang.startsWith(`${base}-`)) score += 40;
    else if (voiceLang === base) score += 30;
    else continue;

    if (MALE_VOICE.test(voice.name)) score += 80;
    if (FEMALE_VOICE.test(voice.name)) score -= 30;
    if (/natural|online/i.test(voice.name)) score += 15;
    if (voice.localService) score += 5;
    if (score > bestScore) {
      best = voice;
      bestScore = score;
    }
  }
  return best;
}

function cleanForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/[*_~>|]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** 手机浏览器对超长 utterance 支持不稳，按句子切成短段再顺序播放。 */
function splitForSpeech(text: string, limit = 180): string[] {
  const sentences = text.match(/[^。！？!?；;.\n]+[。！？!?；;.]?/g) ?? [text];
  const chunks: string[] = [];
  let current = "";

  for (const raw of sentences) {
    const sentence = raw.trim();
    if (!sentence) continue;
    if (current && current.length + sentence.length > limit) {
      chunks.push(current);
      current = sentence;
    } else {
      current += (current ? " " : "") + sentence;
    }
    while (current.length > limit) {
      chunks.push(current.slice(0, limit));
      current = current.slice(limit);
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

function speechError(id: string, message: string) {
  emit({ activeId: id, phase: "error", error: message });
}

export function cancelSpeech() {
  runToken += 1;
  const source = synth();
  try {
    source?.cancel();
  } catch {
    // cancel 在部分浏览器会抛“not started”，忽略即可。
  }
  if (
    snapshot.activeId !== null ||
    snapshot.phase !== "idle" ||
    snapshot.error !== null
  ) {
    emit(IDLE);
  }
}

export function speakText(text: string, id: string) {
  const source = synth();
  if (!source || typeof window.SpeechSynthesisUtterance !== "function") {
    speechError(id, "当前浏览器不支持语音朗读");
    return;
  }

  const clean = cleanForSpeech(text);
  if (!clean) return;

  const token = ++runToken;
  const wasActive = snapshot.activeId !== null || snapshot.phase !== "idle";
  if (wasActive) {
    try {
      source.cancel();
    } catch {
      // 同上：没有正在播的 utterance 时 cancel 可能无副作用地失败。
    }
  }

  emit({ activeId: id, phase: "loading", error: null });
  const voice = chooseVoice(clean, readVoices(source));
  const chunks = splitForSpeech(clean);
  let index = 0;

  const playNext = () => {
    if (token !== runToken) return;
    if (index >= chunks.length) {
      emit(IDLE);
      return;
    }

    const chunk = chunks[index];
    const utterance = new SpeechSynthesisUtterance(chunk);
    const chinese = languageFor(chunk) === "zh-CN";
    utterance.lang = languageFor(chunk);
    if (voice) utterance.voice = voice;
    utterance.rate = chinese ? 0.94 : 1;
    utterance.pitch = chinese ? 0.9 : 0.98;
    utterance.volume = 1;

    let settled = false;
    let watchdog = 0;
    const finish = (ok: boolean, message?: string) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(watchdog);
      if (token !== runToken) return;
      if (!ok) {
        if (message) speechError(id, message);
        return;
      }
      index += 1;
      playNext();
    };

    watchdog = window.setTimeout(() => {
      try {
        source.cancel();
      } catch {
        // 忽略停止失败；失败提示仍然给出。
      }
      finish(false, "朗读没有启动，请再点一次");
    }, 4500);

    utterance.onstart = () => {
      window.clearTimeout(watchdog);
      if (token === runToken) {
        emit({ activeId: id, phase: "speaking", error: null });
      }
    };
    utterance.onend = () => finish(true);
    utterance.onerror = (event) => {
      if (event.error === "canceled" || event.error === "interrupted") return;
      const message = event.error === "not-allowed"
        ? "浏览器拦截了朗读，请再点一次"
        : event.error === "synthesis-failed"
          ? "当前设备未成功启用中文语音，请改用 Edge 或安装中文语音包"
          : "朗读没有完成，请再试一次";
      finish(false, message);
    };

    try {
      source.speak(utterance);
      if (source.paused) source.resume();
    } catch {
      finish(false, "朗读没有启动，请再点一次");
    }
  };

  if (wasActive) {
    window.setTimeout(playNext, 40);
  } else {
    playNext();
  }
}
