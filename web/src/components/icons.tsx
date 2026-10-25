/**
 * src/components/icons.tsx —— 图标集
 *
 * 为什么集中放一个文件：上一版（原生 HTML）图标散落在各处，
 * 同一个"折叠"图标出现在两个地方、还画得不一样，主人一眼就看出来重复了。
 * 集中定义之后，一个图标只有一个来源，不会再有"两处不一致"。
 *
 * 统一规格：24×24 viewBox、描边 1.9、round linecap —— 视觉重量才一致。
 * （用描边图标而不是 emoji：emoji 在不同系统上字形差异很大。）
 */
import type { SVGProps } from "react";
import { useId } from "react";
import emblemUrl from "../assets/thinking-emblem.png";

type IconProps = SVGProps<SVGSVGElement>;

function Base({ children, ...rest }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

/** 侧边栏折叠/展开：带竖线的圆角方框 */
export function PanelIcon(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="3" y="4.5" width="18" height="15" rx="3.5" />
      <path d="M9.5 4.5v15" />
    </Base>
  );
}

/** 搜索历史对话：标准放大镜。 */
export function SearchIcon(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="10.8" cy="10.8" r="6.8" />
      <path d="m16 16 4.2 4.2" />
    </Base>
  );
}

/** 联网搜索：一扇网页窗口 + 极简放大镜。
 *  放大镜手柄故意伸出窗口右下角，避免 16px 时被误读成“图片”图标；
 *  只保留窗口、顶部栏、镜片与手柄四个主轮廓，与历史搜索的裸放大镜区分。 */
export function WebSearchIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.9} {...props}>
      <rect x='3' y='4.2' width='15.4' height='13.6' rx='2.5' />
      <path d='M3 7.8h15.4' />
      <circle cx='15.1' cy='14' r='3.1' />
      <path d='m17.3 16.2 3.2 3.2' />
    </Base>
  );
}

/** 清除搜索词 / 关闭搜索 */
export function CloseIcon(props: IconProps) {
  return (
    <Base strokeWidth={2} {...props}>
      <path d="M6 6l12 12M18 6 6 18" />
    </Base>
  );
}

/** 开新对话：外圈回绕箭头（缺口+箭头），圈里是 + */
export function NewChatIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M20.6 12a8.6 8.6 0 1 1-2.9-6.5" />
      <path d="M20.8 3.4v4.4h-4.4" />
      <path d="M12 8.6v6.8M8.6 12h6.8" />
    </Base>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <Base strokeWidth={2.2} {...props}>
      <path d="M6 9l6 6 6-6" />
    </Base>
  );
}

/** 回到底部：两个上下错位的下箭头。
 *  上箭头更宽、下箭头略窄，避免和常见的等宽双箭头完全撞形；
 *  只用于滚动按钮，不复用页面上的普通单箭头。 */
export function DoubleChevronDownIcon(props: IconProps) {
  return (
    <Base strokeWidth={2.1} {...props}>
      <path d="M6.7 6.9 12 11.9l5.3-5" />
      <path d="M8.4 13.1 12 16.5l3.6-3.4" />
    </Base>
  );
}

/** 思考指示：品牌徽标原样（10 辐线框 + 白芯）。
 *  14px 时辐条会糊成噪点，18px 起清晰 —— 两处调用点都用 18px。
 *  流式时由外层加旋转；展开后静止，避免持续动效干扰阅读。 */
export function ThinkingIcon({ className }: { className?: string }) {
  return (
    <img
      src={emblemUrl}
      alt=""
      aria-hidden="true"
      draggable={false}
      className={`relative top-[1px] object-contain select-none ${className ?? ""}`}
    />
  );
}

/** 品牌标记：已选定的海军蓝“云语”云朵与两处白色负形。
 *  形状按主人确认的 B 方案保留，不再随页面改造重绘。 */
export function BrandMark(props: IconProps) {
  const rawId = useId();
  const maskId = `heytalk-cloud-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <svg viewBox="0 0 256 256" fill="none" aria-hidden="true" {...props}>
      <defs>
        <mask
          id={maskId}
          maskUnits="userSpaceOnUse"
          x="0"
          y="0"
          width="256"
          height="256"
        >
          <rect width="256" height="256" fill="white" />
          <path
            d="M 164 76 C 190 73 207 90 204 111 C 201 134 180 146 161 139 C 144 133 135 116 142 101 C 150 85 168 82 179 93 C 188 102 185 116 175 121 C 167 125 158 118 160 110 C 161 104 167 101 171 105"
            fill="none"
            stroke="black"
            strokeWidth="12"
            strokeLinecap="round"
          />
          <path
            d="M 101 83 C 93 91 91 103 96 112"
            fill="none"
            stroke="black"
            strokeWidth="8"
            strokeLinecap="round"
          />
        </mask>
      </defs>
      <path
        mask={`url(#${maskId})`}
        fill="#0b1f3a"
        d="M 52 151 C 37 146 27 132 29 116 C 17 101 23 82 40 74 C 44 55 61 44 79 48 C 92 31 116 27 133 42 C 149 28 174 34 184 53 C 205 51 221 67 222 88 C 238 99 239 120 227 133 C 226 151 211 164 194 164 C 181 179 161 182 146 172 C 134 188 115 193 99 185 L 72 196 C 80 181 82 169 78 159 C 68 159 59 156 52 151 Z"
      />
    </svg>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <Base strokeWidth={2.1} {...props}>
      <path d="M12 19V5" />
      <path d="M5.5 11.5L12 5l6.5 6.5" />
    </Base>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d="M4 7h16" />
      <path d="M9.5 7V5.6a1.1 1.1 0 0 1 1.1-1.1h2.8a1.1 1.1 0 0 1 1.1 1.1V7" />
      <path d="M6.5 7l.8 11.4a1.6 1.6 0 0 0 1.6 1.5h6.2a1.6 1.6 0 0 0 1.6-1.5L17.5 7" />
    </Base>
  );
}

export function ChatBubbleIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M20 14.5a2 2 0 0 1-2 2H8l-4 3.2V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z" />
    </Base>
  );
}

/** 停止：实心方框（与各家聊天产品的"停止生成"约定一致） */
export function StopIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" {...props}>
      <rect x="7" y="7" width="10" height="10" rx="2" />
    </svg>
  );
}

/** 麦克风（语音输入） */
export function MicIcon(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="9" y="3.2" width="6" height="10.6" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
      <path d="M12 18v3" />
    </Base>
  );
}

/** 正在识别：用一点动态的"波纹"，让等待看得出来 */
/** 回复操作：复制 */
export function CopyIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <rect x='8' y='8' width='11' height='11' rx='2.2' />
      <path d='M16 8V6.3A1.3 1.3 0 0 0 14.7 5H6.3A1.3 1.3 0 0 0 5 6.3v8.4A1.3 1.3 0 0 0 6.3 16H8' />
    </Base>
  );
}

/** 回复操作：朗读 */
export function SpeakerIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M4.5 9.3h3L12 5.8v12.4l-4.5-3.5h-3z' />
      <path d='M15.3 9.2a4 4 0 0 1 0 5.6' />
      <path d='M17.8 6.8a7.4 7.4 0 0 1 0 10.4' />
    </Base>
  );
}

/** 回复操作：转发 / 分享 */
export function ShareIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M12 15V3.5' />
      <path d='m7.8 7.7 4.2-4.2 4.2 4.2' />
      <path d='M5.2 12.5v6.1A1.9 1.9 0 0 0 7.1 20.5h9.8a1.9 1.9 0 0 0 1.9-1.9v-6.1' />
    </Base>
  );
}

/** 回复操作：赞同 */
export function ThumbUpIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M8.4 10.1 11.8 3a2.1 2.1 0 0 1 2 2.7l-.9 3h5.4a2 2 0 0 1 1.9 2.6l-1.7 5.5a2 2 0 0 1-1.9 1.4H8.4' />
      <path d='M3.5 10.1h4.9v8.2H3.5z' />
    </Base>
  );
}

/** 回复操作：不赞同 */
export function ThumbDownIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='m8.4 13.9 3.4 7.1a2.1 2.1 0 0 0 2-2.7l-.9-3h5.4a2 2 0 0 0 1.9-2.6l-1.7-5.5a2 2 0 0 0-1.9-1.4H8.4' />
      <path d='M3.5 5.7h4.9v8.2H3.5z' />
    </Base>
  );
}

/** 回复操作：重新生成 */
export function RegenerateIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M19.8 8.4A8.2 8.2 0 1 0 20 14' />
      <path d='M20.2 3.8v4.8H15.4' />
    </Base>
  );
}

/** 顶栏：声音开启 */
export function VolumeOnIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M4.5 9.3h3L12 5.8v12.4l-4.5-3.5h-3z' />
      <path d='M15.2 9.3a4 4 0 0 1 0 5.4' />
      <path d='M17.6 6.9a7.2 7.2 0 0 1 0 10.2' />
    </Base>
  );
}

/** 顶栏：声音关闭 */
export function VolumeOffIcon(props: IconProps) {
  return (
    <Base strokeWidth={1.8} {...props}>
      <path d='M4.5 9.3h3L12 5.8v12.4l-4.5-3.5h-3z' />
      <path d='m16 9.5 4.2 5' />
      <path d='m20.2 9.5-4.2 5' />
    </Base>
  );
}

/** 操作成功（复制完成等短暂反馈） */
export function CheckIcon(props: IconProps) {
  return (
    <Base strokeWidth={2.1} {...props}>
      <path d='m5 12.5 4.4 4.2L19 6.8' />
    </Base>
  );
}

export function WaveIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M4 12h2.5" />
      <path d="M9 7.5v9" />
      <path d="M12 5v14" />
      <path d="M15 7.5v9" />
      <path d="M19.5 12H22" />
    </Base>
  );
}
