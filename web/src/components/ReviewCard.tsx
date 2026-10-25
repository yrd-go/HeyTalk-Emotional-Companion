/**
 * src/components/ReviewCard.tsx —— "回顾"卡片
 *
 * 这是什么：偶尔（**可预测**地）在他打开时提一件他之前说过的事。
 *
 * ★ 它为什么长这样（每一条都有依据，见 src/chat/review.py 顶部）：
 *   1. **不推送、不择时** —— 只在打开时空屏上出现。
 *      PLOS ONE 那篇随机对照（n=77）发现"智能择时"相对固定定时
 *      **没有显著优势**，所以为"不可预测"付出的复杂度买不到收益。
 *   2. **不做悬念钩子**（没有信封、没有"有一封信等你"）。
 *      内容有价值就不需要钓。
 *   3. **落点是他的生活，不是"我记得"** —— 句子主语是他的事。
 *   4. **可关掉，且关掉就永久不再出现**（用户说了就算）。
 *
 * 四道闸门都在服务端判（chat/review.py），这里只负责显示。
 */
import { useState } from "react";

interface Props {
  text: string;
  onOpened: () => void;
  onDismiss: () => void;
}

export default function ReviewCard({ text, onOpened, onDismiss }: Props) {
  const [closed, setClosed] = useState(false);
  if (closed) return null;

  return (
    <div className="w-full max-w-[560px] rounded-xl border border-line
                    bg-panel-2/50 px-4 py-3 text-left">
      <p className="text-[13px] text-[#4a5158] leading-relaxed">{text}</p>
      <div className="flex items-center gap-2 mt-2">
        <button
          id="reviewOpen"
          type="button"
          onClick={() => {
            // 点开 = 接受这次回顾 → 服务端恢复常规间隔（注意力闸的另一半）
            onOpened();
            setClosed(true);
          }}
          className="rounded-full px-3 py-1 text-[12px] text-brand-dark
                     hover:bg-white transition-colors"
        >
          说说看
        </button>
        <button
          id="reviewDismiss"
          type="button"
          onClick={() => {
            // 关掉 = 永久不再出现（可撤销性：用户说了就算）
            onDismiss();
            setClosed(true);
          }}
          className="rounded-full px-3 py-1 text-[12px] text-muted
                     hover:bg-white transition-colors"
        >
          以后不用提醒我这些
        </button>
      </div>
    </div>
  );
}
