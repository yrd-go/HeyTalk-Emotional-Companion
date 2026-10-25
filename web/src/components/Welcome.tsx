/**
 * src/components/Welcome.tsx —— 首屏
 *
 * 布局教训：上一版写 `min-height:100%` 想让这块垂直居中，但父元素没有
 * 确定高度所以**没生效**，结果内容全顶在上面、下面一大片空白
 * （是截图才发现的）。现在用 flex-1 + justify-center，父容器有高度，
 * 居中才真的成立。
 */
import type { ReactNode } from "react";
import { BrandMark } from "./icons";

interface Props {
  suggestions: string[];
  onPick: (text: string) => void;
  children?: ReactNode;
  mobile?: boolean;
  showSuggestions?: boolean;
}

export default function Welcome({
  suggestions, onPick, children, mobile, showSuggestions,
}: Props) {
  /**
   * 点这句话 → 光标落到输入框。
   *
   * 为什么这么做：首屏只保留一句欢迎语，它本身就是动作承诺。
   * 如果它只是**摆在那里不能点**，那这句话就是空话 ——
   * 文案承诺了动作，界面就得给得出这个动作。
   *
   * 为什么是"聚焦输入框"而不是别的：用户已经在聊天界面里了
   * （空屏就是聊天区），所以"进去"这件事剩下的唯一含义
   * 就是**把光标放到说话的地方**，让他可以立刻开口。
   */
  const enterAndTalk = () => {
    const el = document.getElementById("input") as HTMLTextAreaElement | null;
    if (el) {
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
  };

  return (
    <section className={`flex-1 flex flex-col items-center justify-center
      ${mobile ? "gap-5 px-4 py-8" : "gap-6 px-5 py-8"}`}>
      {/* ★ 每个子块都要**先受父容器约束**（w-full / max-w-full），
          不能只写 max-w-[Npx]。
          为什么（320px 窄屏实测出来的 bug）：
            父级是列向 flex + items-center，子元素的宽度会按
            **fit-content** 计算 —— 那个"建议药丸"容器是 flex-wrap，
            它的 fit-content 等于**全部药丸排成一行的宽度（约 680px）**，
            于是整个页面被撑到 680px，标题、药丸、发送按钮全被切掉。
            `max-w-[700px]` 只是上限，**不会**把它压回父容器宽度；
            必须配 `w-full` 才会先服从父容器、再由 max-w 封顶。 */}
      <div className="w-full max-w-full flex items-center justify-center gap-3">
        {!mobile && (
          <BrandMark className="w-[36px] h-[36px] block shrink-0" />
        )}
        <p
          id="welcomeLine"
          role="button"
          tabIndex={0}
          onClick={enterAndTalk}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              enterAndTalk();
            }
          }}
          className="welcome-line cursor-text select-none px-2 text-center
                     transition-opacity hover:opacity-80"
        >
          <span className="welcome-accent">嗨，我们今天聊什么</span>
        </p>
      </div>

      {children}

      {showSuggestions !== false && (
      <div className={`justify-center gap-2.5 w-full max-w-[700px]
                       ${mobile ? "flex flex-col" : "flex flex-wrap"}`}>
        {suggestions.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            className="rounded-full border border-line bg-white px-4 py-2
                       text-[13px] text-[#4a5158] hover:border-brand/40
                       hover:text-brand-dark transition-colors"
          >
            {s}
          </button>
        ))}
      </div>
      )}
    </section>
  );
}
