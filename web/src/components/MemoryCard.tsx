/**
 * src/components/MemoryCard.tsx —— "要不要记住这个？"确认卡
 *
 * ★ 这是记忆的**唯一入口**，也是这个项目的一条红线：
 *   从对话里抽出来的只是**候选**，用户点了"记住"才进档案。
 *   为什么这么严（src/chat/profile.py 里有完整论证）：
 *   一条**错的**记忆会在之后的**每一次**对话里持续生效 —— 比不记更糟。
 *
 * 设计要点：
 *   · 默认按钮是"记住"，但"不用"同样显眼 —— 不做暗黑模式（Dark Pattern）
 *   · "改一下"让用户用自己的话重写：**他的措辞才是准的**
 *   · 卡片出现在回复下方，不打断对话；不点也不影响继续聊
 */
import { useState } from "react";
import type { MemoryEntry } from "../api";

interface Props {
  entry: MemoryEntry;
  onConfirm: () => void;
  onEdit: (value: string) => void;
  onReject: () => void;
  busy?: boolean;
  error?: string | null;
}

export default function MemoryCard({
  entry, onConfirm, onEdit, onReject, busy, error,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.value);

  return (
    <div className="rounded-xl border border-line bg-panel-2/60 px-3.5 py-3 mt-3
                    text-[13px]">
      <p className="text-muted text-[12px] mb-1.5">
        要不要记住这个？以后聊天时我会记得。
      </p>

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={2}
          autoFocus
          className="w-full rounded-lg border border-line bg-white px-2.5 py-1.5
                     text-[13px] resize-none focus:outline-none
                     focus:border-brand/50"
        />
      ) : (
        <p className="text-ink leading-relaxed">{entry.value}</p>
      )}

      {error && (
        <p className="text-[12px] text-[#c0392b] mt-1.5">{error}</p>
      )}

      <div className="flex items-center gap-2 mt-2.5">
        {editing ? (
          <>
            <button
              id="memorySaveEdit"
              type="button"
              disabled={busy || !draft.trim()}
              onClick={() => onEdit(draft.trim())}
              className="rounded-full bg-brand text-white px-3 py-1 text-[12px]
                         disabled:opacity-40 hover:bg-brand-dark transition-colors"
            >
              就这样
            </button>
            <button
              type="button"
              onClick={() => { setEditing(false); setDraft(entry.value); }}
              className="rounded-full px-3 py-1 text-[12px] text-muted
                         hover:bg-white transition-colors"
            >
              返回
            </button>
          </>
        ) : (
          <>
            <button
              id="memoryConfirm"
              type="button"
              disabled={busy}
              onClick={onConfirm}
              className="rounded-full bg-brand text-white px-3 py-1 text-[12px]
                         disabled:opacity-40 hover:bg-brand-dark transition-colors"
            >
              记住
            </button>
            <button
              id="memoryEdit"
              type="button"
              disabled={busy}
              onClick={() => setEditing(true)}
              className="rounded-full border border-line bg-white px-3 py-1
                         text-[12px] text-[#4a5158] hover:border-brand/40
                         transition-colors"
            >
              改一下
            </button>
            <button
              id="memoryReject"
              type="button"
              disabled={busy}
              onClick={onReject}
              className="rounded-full px-3 py-1 text-[12px] text-muted
                         hover:bg-white transition-colors"
            >
              不用
            </button>
          </>
        )}
      </div>
    </div>
  );
}
