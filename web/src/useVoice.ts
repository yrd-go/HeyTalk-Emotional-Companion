/**
 * src/useVoice.ts —— 语音输入（录在本机、识别在本机）
 *
 * 设计要点
 * ---------------------------------------------------------------------------
 * ★ 浏览器只负责**录音**，识别交给本机服务（`/api/transcribe`）。
 *   为什么不用 Web Speech API：它在 Chrome/Edge 上把**音频送到 Google**，
 *   而这个项目唯一的护城河就是"你的话不出这台机器"。
 *
 * ★ 麦克风需要**安全上下文**（HTTPS 或 localhost）。这是浏览器强制的，
 *   不是我们能绕的。走 `http://内网IP` 时 `getUserMedia` 根本不存在 ——
 *   所以这里必须**先判断再决定要不要显示按钮**，并且给出一句
 *   用户看得懂的解释，而不是让按钮点了没反应。
 *
 * ★ 录完要**主动关掉麦克风轨道**（stop tracks）。不然浏览器地址栏上的
 *   "正在录音"标记会一直亮着 —— 用户会以为还在偷听。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { transcribe } from "./api";

export type VoiceState = "idle" | "recording" | "transcribing";

/** 单次录音上限（秒）。太长的音频上传慢、识别也慢。 */
const MAX_SECONDS = 60;

function pickMimeType(): string {
  // 各家支持的容器不同（Safari 上是 mp4/aac）。挑一个能用的，
  // 挑不出来就让浏览器自己决定（MediaRecorder 支持无参构造）。
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
  ];
  const MR = (window as unknown as { MediaRecorder?: typeof MediaRecorder })
    .MediaRecorder;
  if (!MR?.isTypeSupported) return "";
  for (const t of candidates) {
    try {
      if (MR.isTypeSupported(t)) return t;
    } catch {
      /* 继续试下一个 */
    }
  }
  return "";
}

export function useVoice(onText: (text: string) => void) {
  const [state, setState] = useState<VoiceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [secureContext, setSecureContext] = useState(true);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);

  // 识别失败提示只停留两三秒：把原因说清楚，但不让它长期占住输入区。
  useEffect(() => {
    if (!error) return;
    const id = window.setTimeout(() => setError(null), 2600);
    return () => window.clearTimeout(id);
  }, [error]);

  useEffect(() => {
    // localhost 也算安全上下文，所以本机开发时是 true
    setSecureContext(typeof window !== "undefined"
      ? window.isSecureContext !== false
      : true);
  }, []);

  /** 环境是否**可能**支持（真正的权限还要用户点同意） */
  const supported = typeof window !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof window.MediaRecorder !== "undefined";

  const releaseMic = useCallback(() => {
    // 关掉轨道：否则浏览器的"正在录音"标记不灭
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => releaseMic, [releaseMic]);

  const start = useCallback(async () => {
    setError(null);
    if (!supported) {
      setError(secureContext
        ? "这个浏览器不支持录音，可以换 Chrome/Edge/Safari 试试。"
        : "网页录音需要 HTTPS（或 localhost）。当前这个地址不是，"
          + "浏览器不会允许访问麦克风 —— 这不是程序的问题。");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;
      chunksRef.current = [];

      const mime = pickMimeType();
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime })
                       : new MediaRecorder(stream);
      recorderRef.current = rec;

      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = async () => {
        releaseMic();
        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        chunksRef.current = [];
        if (blob.size < 1200) {
          // 太短基本是误按；直接放过，别浪费一次识别
          setState("idle");
          setSeconds(0);
          return;
        }
        setState("transcribing");
        try {
          const text = await transcribe(blob);
          if (text) onText(text);
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setState("idle");
          setSeconds(0);
        }
      };

      rec.start();
      setState("recording");
      setSeconds(0);
      timerRef.current = window.setInterval(() => {
        setSeconds((s) => {
          const next = s + 1;
          // 到上限自动停止并识别（避免用户忘了按停止）
          if (next >= MAX_SECONDS) recorderRef.current?.stop();
          return next;
        });
      }, 1000);
    } catch (e) {
      releaseMic();
      setState("idle");
      const name = e instanceof Error ? e.name : "";
      if (name === "NotAllowedError") {
        setError("你拒绝了麦克风权限。想用的话在浏览器地址栏那里允许一下。");
      } else if (name === "NotFoundError") {
        setError("没找到麦克风设备。");
      } else {
        setError(`打不开麦克风（${name || "未知原因"}）。`);
      }
    }
  }, [onText, releaseMic, secureContext, supported]);

  const stop = useCallback(() => {
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") {
      rec.stop();   // 触发 onstop → 上传识别
    } else {
      releaseMic();
      setState("idle");
    }
  }, [releaseMic]);

  const toggle = useCallback(() => {
    if (state === "recording") stop();
    else if (state === "idle") void start();
  }, [start, state, stop]);

  return { state, error, seconds, supported, secureContext, toggle, setError };
}
