/**
 * src/components/Sidebar.tsx —— 左侧栏
 *
 * 内容按主人这几轮的要求收敛过：
 *   · 去掉了"说明"整块（主人说"这里也不用，去掉这个说明"）
 *   · 聊天记录用**第一句话**当标题（不是时间戳），时间放右边
 *   · "需要真人帮忙时"**默认折叠**（主人说"太长了不美观"）
 *   · 折叠按钮在侧边栏里（展开态唯一一处）
 */
import { useEffect, useRef, useState } from "react";
import {
  BrandMark, ChevronDownIcon, CloseIcon, NewChatIcon, PanelIcon, SearchIcon,
  TrashIcon,
} from "./icons";
import type { HistoryItem } from "../types";

/** 把时间戳变成人话：今天显示时刻，今年显示月日，更早显示年月日 */
function humanTime(iso: string, ageDays?: number): string {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) {
    return ageDays != null ? `${ageDays} 天前` : "";
  }
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) {
    return `${d.getMonth() + 1}月${d.getDate()}日`;
  }
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function groupOf(item: HistoryItem): string {
  if (item.age_days < 1) return "今天";
  if (item.age_days < 7) return "7 天内";
  if (item.age_days < 30) return "30 天内";
  return "更早";
}

interface Props {
  history: HistoryItem[];
  sessionId: string;
  hotlines: string;
  degradedNote?: string;
  onNew: () => void;
  onOpen: (sid: string) => void;
  onDelete: (sid: string) => void;
  onCollapse: () => void;
  /** 托管模式下显示"当前是谁 + 退出"；本机模式不传，整行不渲染 */
  userName?: string;
  onSignOut?: () => void;
}

export default function Sidebar({
  history, sessionId, hotlines, degradedNote,
  onNew, onOpen, onDelete, onCollapse, userName, onSignOut,
}: Props) {
  const [hotlinesOpen, setHotlinesOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  const closeSearch = () => {
    setSearchOpen(false);
    setQuery("");
  };

  const openItem = (sid: string) => {
    onOpen(sid);
    closeSearch();
  };

  useEffect(() => {
    if (!searchOpen) return;
    const id = window.setTimeout(() => searchRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [searchOpen]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (searchOpen) {
          setSearchOpen(false);
          setQuery("");
        } else {
          setSearchOpen(true);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [searchOpen]);

  const q = query.trim().toLocaleLowerCase();
  const visible = q
    ? history.filter((item) =>
        (item.title || "").toLocaleLowerCase().includes(q))
    : history;

  // 分组（保持后端给的顺序：已按最近更新排好）
  const groups: { name: string; items: HistoryItem[] }[] = [];
  if (q) {
    if (visible.length > 0) groups.push({ name: "", items: visible });
  } else {
    for (const item of visible) {
      const name = groupOf(item);
      const last = groups[groups.length - 1];
      if (last && last.name === name) last.items.push(item);
      else groups.push({ name, items: [item] });
    }
  }

  return (
    <aside className="w-[248px] shrink-0 bg-panel flex flex-col h-full">
      {/* ---- 品牌区 ---- */}
      <div className="flex items-center justify-between px-4 pt-4 pb-3">
        <div className="flex items-center gap-2">
          {/* 图标不加底板、不加外框（主人明确要求去掉那个方框）。
              白心靠它自己的蓝色描边在浅灰底上立住。 */}
          <BrandMark className="w-[30px] h-[30px] block shrink-0" />
          {/* 词标用 CSS 文字而不是图片：上一版做成 164×72 的图再缩到 22px
              显示，糊成一团"歪八七扭"。文字任何尺寸都清晰，紧凑感靠
              letter-spacing 与 font-weight 做即可。 */}
          <span className="text-[18px] font-extrabold tracking-[-0.45px] text-ink leading-none">
            Heytalk
          </span>
        </div>
        <div className="flex items-center gap-0.5">
          <button
            id="historySearchBtn"
            type="button"
            onClick={() => (searchOpen ? closeSearch() : setSearchOpen(true))}
            title={searchOpen ? "关闭搜索" : "搜索聊天记录（Ctrl K）"}
            aria-expanded={searchOpen}
            aria-controls="historySearch"
            className={`w-7 h-7 rounded-md flex items-center justify-center
                       transition-colors ${searchOpen
                         ? "bg-white text-brand-dark"
                         : "text-[#5b636d] hover:bg-panel-2 hover:text-body"}`}
          >
            <SearchIcon className="w-[17px] h-[17px]" />
          </button>
          <button
            id="collapseBtn"
            type="button"
            onClick={onCollapse}
            title="收起侧边栏"
            className="w-7 h-7 rounded-md text-[#5b636d] hover:bg-panel-2
                       hover:text-body flex items-center justify-center transition-colors"
          >
            <PanelIcon className="w-[17px] h-[17px]" />
          </button>
        </div>
      </div>

      {searchOpen && (
        <div className="px-3 pb-3">
          <div className="relative">
            <SearchIcon className="absolute left-2.5 top-1/2 -translate-y-1/2
                                 w-4 h-4 text-muted pointer-events-none" />
            <input
              id="historySearch"
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") closeSearch();
                if (e.key === "Enter" && visible[0]) openItem(visible[0].session_id);
              }}
              placeholder="搜索聊天记录"
              aria-label="搜索聊天记录"
              className="w-full h-9 rounded-xl border border-line bg-white
                         pl-8 pr-8 text-[13px] text-body placeholder:text-muted
                         outline-none focus:border-brand/45"
            />
            {query && (
              <button
                type="button"
                title="清除搜索词"
                onClick={() => setQuery("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 w-6 h-6
                           rounded-md flex items-center justify-center text-muted
                           hover:text-body hover:bg-panel-2"
              >
                <CloseIcon className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      )}

      {/* ---- 开新对话 ---- */}
      <div className="px-3">
        <button
          id="newChat"
          type="button"
          onClick={onNew}
          className="w-full flex items-center justify-center gap-2 rounded-xl
                     bg-brand text-white text-[14px] py-2.5 hover:bg-brand-dark
                     transition-colors"
        >
          <NewChatIcon className="w-[17px] h-[17px]" />
          开新对话
        </button>
      </div>

      {degradedNote && (
        <p className="mx-4 mt-3 text-[12px] leading-[1.6] text-[#8a6d1f]
                      bg-[#fdf6e3] rounded-lg px-2.5 py-2">
          {degradedNote}
        </p>
      )}

      {/* ---- 聊天记录 ---- */}
      <div className="flex-1 overflow-y-auto mt-4 px-3 min-h-0">
        <div className="text-[12px] font-medium text-muted px-1 pb-1.5">
          {q ? `搜索结果 · ${visible.length}` : "聊天记录"}
        </div>
        {groups.length === 0 && (
          <p className="text-[12px] text-muted px-1 py-1">
            {q ? "没有匹配的对话" : "还没有记录"}
          </p>
        )}
        {groups.map((g) => (
          <div key={g.name}>
            {!q && (
              <div className="text-[12px] text-muted px-1 pt-2 pb-1">
                {g.name}
              </div>
            )}
            {g.items.map((item) => (
              <div
                key={item.session_id}
                role="button"
                tabIndex={0}
                onClick={() => openItem(item.session_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") openItem(item.session_id);
                }}
                className={`group flex items-center gap-2 rounded-lg px-2 py-1.5
                            cursor-pointer text-[13px] transition-colors
                            ${item.session_id === sessionId
                              ? "bg-panel-2 text-body"
                              : "text-[#4a5158] hover:bg-panel-2"}`}
              >
                <span className="flex-1 truncate" title={item.title}>
                  {item.title || "（还没有说话）"}
                </span>
                <span className="text-[11px] text-muted shrink-0">
                  {humanTime(item.updated_at, item.age_days)}
                </span>
                <button
                  type="button"
                  title="删除这条记录"
                  onClick={(e) => { e.stopPropagation(); onDelete(item.session_id); }}
                  className="hidden group-hover:flex w-5 h-5 shrink-0 rounded
                             items-center justify-center text-muted
                             hover:text-[#e5484d] hover:bg-[#fdecec]"
                >
                  <TrashIcon className="w-[14px] h-[14px]" />
                </button>
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* ---- 求助渠道：默认折叠（内容没删，只是收起） ---- */}
      <div className="border-t border-line mt-2">
        {/* 当前是谁 + 退出：**只在托管模式下出现**（本机模式不传 userName）。
            这是功能而不是提示 —— 多个账号共用一台服务器时，
            你必须能看出自己现在是谁、以及怎么换人。 */}
        {userName && onSignOut && (
          <div className="flex items-center justify-between px-4 pt-2.5">
            <span className="text-[12px] text-muted truncate">
              当前：{userName}
            </span>
            <button
              id="signOut"
              type="button"
              onClick={onSignOut}
              className="text-[12px] text-muted hover:text-brand-dark
                         transition-colors shrink-0"
            >
              退出
            </button>
          </div>
        )}
        <button
          type="button"
          onClick={() => setHotlinesOpen((v) => !v)}
          aria-expanded={hotlinesOpen}
          className="w-full flex items-center justify-between px-4 py-2.5
                     text-[12px] font-medium text-muted hover:text-brand-dark
                     transition-colors"
        >
          <span>需要真人帮忙时</span>
          <ChevronDownIcon
            className={`w-3.5 h-3.5 transition-transform duration-150
                        ${hotlinesOpen ? "rotate-180" : ""}`}
          />
        </button>
        {hotlinesOpen && (
          <div
            id="hotlines"
            className="px-4 pb-4 text-[12px] leading-[1.7] text-muted
                       whitespace-pre-wrap"
          >
            {hotlines}
          </div>
        )}
      </div>
    </aside>
  );
}
