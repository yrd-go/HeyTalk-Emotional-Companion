/**
 * src/App.tsx —— 主应用
 *
 * 布局：左侧栏 + 主区（顶部条 / 滚动区 / 输入区）
 *
 * 侧边栏收起状态存 localStorage：下次打开还是你习惯的样子。
 * ★ 开发钩子：URL 带 ?collapsed=1 / ?menu=1 时**模拟点击真实按钮**，
 *   而不是直接改状态 —— 这是上一轮踩坑后的修正：直接改状态会绕过
 *   按钮的点击链路，于是"按钮没接线"这类 bug 在截图里看不出来
 *   （我因此漏掉过一个真实的 bug，最后是主人点了一下才发现）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Composer from "./components/Composer";
import { DoubleChevronDownIcon } from "./components/icons";
import IntroOverlay from "./components/IntroOverlay";
import Login from "./components/Login";
import MemoryCard from "./components/MemoryCard";
import MessageList from "./components/MessageList";
import ReviewCard from "./components/ReviewCard";
import Sidebar from "./components/Sidebar";
import TopBar, { MobileTopBar } from "./components/TopBar";
import Welcome from "./components/Welcome";
import { cancelSpeech, primeSpeech, speakText, useSpeechState } from "./speech";
import { useChat } from "./useChat";

const LS_KEY = "heytalk_sidebar";
const SOUND_KEY = "heytalk_reply_sound";
const MOBILE_BREAKPOINT = "(max-width: 768px)";
const SCROLL_BOTTOM_THRESHOLD = 64;
const SCROLL_BUTTON_HIDE_THRESHOLD = 4;
const FONT_READY_TIMEOUT_MS = 900;
const INTRO_SETTLE_MS = 760;
const INTRO_EXIT_MS = 180;

export default function App() {
  const chat = useChat();
  const speech = useSpeechState();

  // 是不是手机/窄屏。用 matchMedia 而不是只测一次宽度：
  // 用户可能把浏览器窗口拉宽拉窄，得跟着变。
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" &&
          window.matchMedia(MOBILE_BREAKPOINT).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_BREAKPOINT);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // 侧边栏收起状态。★ 移动端**默认收起**：248px 的侧边栏在 375px 的
  // 手机上会占掉三分之二，把聊天区挤没（这曾经是旧版丢失的移动端适配，
  // 换前端时又丢了一次 —— 见 CAPABILITIES.md）。
  const [collapsed, setCollapsed] = useState(() => {
    const saved = localStorage.getItem(LS_KEY);
    if (saved) return saved === "collapsed";
    return isMobile; // 首次访问：移动端默认收起，桌面默认展开
  });
  const [seed, setSeed] = useState<string | undefined>();
  const [soundEnabled, setSoundEnabled] = useState(
    () => localStorage.getItem(SOUND_KEY) !== "off",
  );
  const [fontsReady, setFontsReady] = useState(false);
  const [introSettled, setIntroSettled] = useState(false);
  const [introDismissed, setIntroDismissed] = useState(false);
  const [showScrollDown, setShowScrollDown] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const devDone = useRef(false);

  // 首屏不能按“哪里先到就先画哪里”：欢迎文案是同步渲染，品牌图已经
  // 内联为 SVG，推荐话题要等 bootstrap。这里把字体与启动数据合成
  // 一个就绪门槛，等它们都结束后再一次性显示整页。
  useEffect(() => {
    let cancelled = false;
    let finished = false;
    const finish = () => {
      if (cancelled || finished) return;
      finished = true;
      setFontsReady(true);
    };
    const fallback = window.setTimeout(finish, FONT_READY_TIMEOUT_MS);
    Promise.resolve(document.fonts?.ready).then(finish).catch(finish);
    return () => {
      cancelled = true;
      window.clearTimeout(fallback);
    };
  }, []);

  useEffect(() => {
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const timer = window.setTimeout(
      () => setIntroSettled(true),
      reduceMotion ? 30 : INTRO_SETTLE_MS,
    );
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!chat.ready || !fontsReady || !introSettled) return;
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const timer = window.setTimeout(
      () => setIntroDismissed(true),
      reduceMotion ? 80 : INTRO_EXIT_MS,
    );
    return () => window.clearTimeout(timer);
  }, [chat.ready, fontsReady, introSettled]);

  useEffect(() => {
    primeSpeech();
    return () => cancelSpeech();
  }, []);

  const setCollapsedPersist = useCallback((next: boolean) => {
    setCollapsed(next);
    localStorage.setItem(LS_KEY, next ? "collapsed" : "open");
  }, []);

  const toggleSound = useCallback(() => {
    const next = !soundEnabled;
    setSoundEnabled(next);
    localStorage.setItem(SOUND_KEY, next ? "on" : "off");
    if (!next) {
      cancelSpeech();
      return;
    }
    primeSpeech();
    speakText("语音已开启，可以朗读回复了。", "sound-toggle");
  }, [soundEnabled]);

  const syncScrollPosition = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = Math.max(
      0,
      el.scrollHeight - el.scrollTop - el.clientHeight,
    );
    const atBottom = distance <= SCROLL_BOTTOM_THRESHOLD;
    atBottomRef.current = atBottom;
    setShowScrollDown(distance > SCROLL_BUTTON_HIDE_THRESHOLD);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const reduceMotion = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    el.scrollTo({
      top: el.scrollHeight,
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }, []);

  // 只有用户本来就在底部时才自动跟随；读到上面时不能被流式回复拉回来。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !atBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
    setShowScrollDown(false);
  }, [chat.turns]);

  // 切换会话时从底部开始，避免沿用上一条会话的“已向上滚动”状态。
  useEffect(() => {
    atBottomRef.current = true;
    setShowScrollDown(false);
    cancelSpeech();
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.sessionId]);

  // 开发截图钩子：点真实按钮，走真实链路
  useEffect(() => {
    if (devDone.current || !chat.ready) return;
    devDone.current = true;
    const p = new URLSearchParams(location.search);
    const want = p.get("collapsed");
    if (want === "1" || want === "0") {
      const btn = document.getElementById(
        want === "1" ? "collapseBtn" : "toggleBtn",
      );
      if (btn) btn.click();
    }
    if (p.get("menu") === "1") {
      setTimeout(() => document.getElementById("modelBtn")?.click(), 300);
    }
    // 视觉验收钩子：点真实胶囊，走与用户相同的点击链路。
    if (p.get("thinking") === "1") {
      setTimeout(() => document.getElementById("thinkingMode")?.click(), 320);
    }
    if (p.get("search") === "1") {
      setTimeout(() => document.getElementById("webSearchMode")?.click(), 360);
    }
  }, [chat.ready]);

  const suggestions = chat.boot?.opening_suggestions ?? [];

  // 输入区的 props 只写一份：首屏（标题下方）与聊天中（底部）复用，
  // 避免两处 props 慢慢漂移。
  const composerProps = {
    onSend: chat.send,
    onStop: chat.stop,
    busy: chat.sending,
    seed,
    onSeedConsumed: () => setSeed(undefined),
    voiceEnabled: chat.boot?.voice_enabled,
    tagline: chat.boot?.tagline,
    thinking: chat.thinking,
    onThinking: chat.setThinking,
    webSearch: chat.webSearch,
    onWebSearch: chat.setWebSearch,
  };

  const initialUiReady = chat.ready && fontsReady && introSettled;
  if (!initialUiReady) {
    return (
      <div className="h-full">
        <div className="flex h-full bg-white" />
        <IntroOverlay exiting={false} />
      </div>
    );
  }

  if (chat.needLogin) {
    return (
      <div className="h-full">
        <div className="h-full">
          <Login
            onLogin={chat.signIn}
            onRedeem={chat.signUp}
            error={chat.loginError}
          />
        </div>
        {!introDismissed && <IntroOverlay exiting />}
      </div>
    );
  }

  // 侧边栏只写一份，两个场景（桌面内联 / 移动端抽屉）复用 ——
  // 否则两处 props 会慢慢不一致（这类"两处漂移"已经出过好几次）。
  const sidebar = (
    <Sidebar
      history={chat.history}
      sessionId={chat.sessionId}
      hotlines={chat.boot?.hotlines_markdown ?? ""}
      degradedNote={chat.boot?.degraded_note}
      onNew={chat.startNewSession}
      onOpen={(sid) => {
        chat.openSession(sid);
        // 移动端点开会话后收起抽屉，直接看到对话内容
        if (isMobile) setCollapsedPersist(true);
      }}
      onDelete={chat.removeSession}
      onCollapse={() => setCollapsedPersist(true)}
      userName={chat.boot?.auth_required ? chat.userName : undefined}
      onSignOut={chat.boot?.auth_required ? chat.signOut : undefined}
    />
  );

  return (
    <div className="h-full">
      <div className="flex h-full bg-white">
      {!collapsed && (
        isMobile ? (
          /* 移动端：侧边栏作为**覆盖层抽屉**，底下垫半透明遮罩。
             点遮罩即关闭（等价于点折叠按钮）。不用 CSS media query 而用
             JS 控制，是因为这里还要管"遮罩"这个非侧边栏元素。 */
          <>
            <div
              className="fixed inset-0 bg-black/35 z-30"
              onClick={() => setCollapsedPersist(true)}
              aria-hidden="true"
            />
            <div className="fixed left-0 top-0 h-full z-40 shadow-2xl">
              {sidebar}
            </div>
          </>
        ) : (
          sidebar
        )
      )}

      <main className="flex-1 flex flex-col min-w-0 h-full">
        {/* 主区顶部条只在收起时出现 —— 展开时侧边栏里已有品牌，
            再显示一遍就是"重复出现"（主人明确批评过一次） */}
        {collapsed && (isMobile ? (
          <MobileTopBar
            onNew={chat.startNewSession}
            onExpand={() => setCollapsedPersist(false)}
            soundEnabled={soundEnabled}
            onToggleSound={toggleSound}
          />
        ) : (
          <TopBar
            onNew={chat.startNewSession}
            onExpand={() => setCollapsedPersist(false)}
            soundEnabled={soundEnabled}
            onToggleSound={toggleSound}
          />
        ))}

        <div
          ref={scrollRef}
          onScroll={syncScrollPosition}
          className="chat-scroll flex-1 overflow-y-auto min-h-0"
        >
          <div className="max-w-[720px] mx-auto px-5 min-h-full flex flex-col">
            {chat.turns.length === 0 ? (
              <>
                {/* 回顾只在**空屏**上出现 —— 不打断正在说的话。
                    四道闸门都在服务端判（chat/review.py），
                    这里只负责"有就显示"。 */}
                {chat.boot?.review && (
                  <div className="pt-8 flex justify-center">
                    <ReviewCard
                      text={chat.boot.review.text}
                      onOpened={chat.markReviewOpened}
                      onDismiss={chat.dismissReview}
                    />
                  </div>
                )}
                <Welcome
                  suggestions={isMobile ? [] : suggestions}
                  onPick={(s) => setSeed(s)}
                  mobile={isMobile}
                  showSuggestions={!isMobile}
                >
                  {!isMobile && (
                    <Composer {...composerProps} inline showTagline={false} />
                  )}
                </Welcome>
              </>
            ) : (
              <div className="py-6">
                <MessageList
                  turns={chat.turns}
                  mobile={isMobile}
                  voiceOutputEnabled={soundEnabled}
                  busy={chat.sending}
                  onRegenerate={chat.regenerate}
                />
                {/* 待确认的记忆：出现在对话下方，不打断。
                    不点也能继续聊 —— 它不该拦住任何事。 */}
                {chat.memoryPending && (
                  <MemoryCard
                    entry={chat.memoryPending}
                    onConfirm={chat.confirmPendingMemory}
                    onEdit={chat.editPendingMemory}
                    onReject={chat.rejectPendingMemory}
                    busy={chat.memoryBusy}
                    error={chat.memoryError}
                  />
                )}
              </div>
            )}
          </div>
        </div>

        {chat.error && (
          <p className="max-w-[720px] mx-auto px-5 pb-1 text-[12px] text-[#c0392b]">
            {chat.error}
          </p>
        )}

        {chat.turns.length === 0 ? (
          isMobile ? (
            <div className="w-full max-w-[720px] mx-auto px-3 pb-4 shrink-0">
              {suggestions.length > 0 && (
                <div className="mb-2">
                  <p className="text-[12px] font-medium text-muted mb-2 pl-1">推荐话题</p>
                  <div id="mobileSuggestions" className="flex flex-col items-start gap-2">
                    {suggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={() => setSeed(s)}
                        className="w-auto max-w-full self-start rounded-xl border border-line bg-white
                                   px-4 py-2.5 text-left text-[13px] text-[#4a5158]
                                   hover:border-brand/40 hover:text-brand-dark
                                   active:bg-panel transition-colors"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <Composer {...composerProps} flush showTagline={false} />
              {chat.boot?.tagline && (
                <p className="text-center text-[12px] text-muted mt-1.5">
                  {chat.boot.tagline}
                </p>
              )}
            </div>
          ) : (
            /* 桌面首屏保持原位：输入框在欢迎语下方，标语单独留在底部。 */
            <div className="w-full max-w-[720px] mx-auto px-2 pb-3">
              {chat.boot?.tagline && (
                <p className="text-center text-[12px] text-muted mt-2.5">
                  {chat.boot.tagline}
                </p>
              )}
            </div>
          )
        ) : (
          <div className="relative w-full max-w-[720px] mx-auto shrink-0">
            {showScrollDown && (
              <button
                id="scrollToBottom"
                type="button"
                onClick={scrollToBottom}
                title="回到底部"
                aria-label="回到底部"
                className="absolute -top-12 right-5 z-10 w-10 h-10 rounded-full
                           border border-line bg-white text-brand-dark
                           shadow-[0_6px_22px_rgba(11,31,58,0.16)]
                           flex items-center justify-center
                           hover:bg-panel active:bg-panel-2
                           transition-colors motion-reduce:transition-none"
              >
                <DoubleChevronDownIcon className="w-[19px] h-[19px]" />
              </button>
            )}
            <Composer {...composerProps} />
          </div>
        )}
      </main>
      </div>
      {speech.error && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-5 left-1/2 z-[120] -translate-x-1/2
                     max-w-[min(92vw,520px)] rounded-full border border-line
                     bg-white/95 px-4 py-2 text-center text-[12px] text-[#7a4b12]
                     shadow-[0_8px_28px_rgba(11,31,58,0.14)] backdrop-blur"
        >
          {speech.error}
        </div>
      )}
      {!introDismissed && <IntroOverlay exiting />}
    </div>
  );
}
