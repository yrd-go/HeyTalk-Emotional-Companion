/**
 * 轻量 Markdown 渲染：只处理回复里实际会出现的粗体、斜体、行内代码、
 * 标题与有序/无序列表。
 *
 * 为什么不用 dangerouslySetInnerHTML：
 *   回复内容来自模型与用户，任何 HTML 注入都不该被当成页面结构执行。
 *   这里全部走 React 节点，未匹配的标记仍然按普通文字显示。
 */
import type { ReactNode } from "react";

type ListBlock = { type: "ul" | "ol"; items: string[] };
type Block =
  | { type: "p"; text: string }
  | { type: "h"; level: number; text: string }
  | ListBlock;

function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: ListBlock | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ type: "p", text: paragraph.join("\n") });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };

  for (const raw of text.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = line.match(/^\s*(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ type: "h", level: heading[1].length, text: heading[2] });
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s*(.+)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.+)$/);

    // 列表项的续行（缩进 2 格以上）并入上一项，避免把一句完整的话拆成两段。
    if (list && /^\s{2,}/.test(raw) && !ordered && !bullet) {
      list.items[list.items.length - 1] += `\n${line.trim()}`;
      continue;
    }

    if (ordered) {
      flushParagraph();
      if (!list || list.type !== "ol") {
        flushList();
        list = { type: "ol", items: [] };
      }
      list.items.push(ordered[1]);
      continue;
    }
    if (bullet) {
      flushParagraph();
      if (!list || list.type !== "ul") {
        flushList();
        list = { type: "ul", items: [] };
      }
      list.items.push(bullet[1]);
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  return blocks;
}

const INLINE_RE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)/g;

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of text.matchAll(INLINE_RE)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(text.slice(cursor, index));
    const token = match[0];
    if (token.startsWith("**")) {
      nodes.push(<strong key={index} className="font-semibold text-ink">{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      nodes.push(
        <code key={index} className="rounded bg-panel px-1 py-0.5 text-[0.92em]">
          {token.slice(1, -1)}
        </code>,
      );
    } else {
      nodes.push(<em key={index}>{token.slice(1, -1)}</em>);
    }
    cursor = index + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

export default function RichText({ text }: { text: string }) {
  return (
    <div className="space-y-2">
      {parseBlocks(text).map((block, index) => {
        if ("items" in block) {
          const List = block.type === "ul" ? "ul" : "ol";
          return (
            <List
              key={index}
              className={`${block.type === "ul" ? "list-disc" : "list-decimal"} pl-5 space-y-1`}
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex} className="whitespace-pre-wrap break-words">
                  {renderInline(item)}
                </li>
              ))}
            </List>
          );
        }
        if (block.type === "h") {
          const Tag = "h3";
          return (
            <Tag key={index} className="font-semibold text-ink pt-1">
              {renderInline(block.text)}
            </Tag>
          );
        }
        return (
          <p key={index} className="whitespace-pre-wrap break-words">
            {renderInline(block.text)}
          </p>
        );
      })}
    </div>
  );
}
