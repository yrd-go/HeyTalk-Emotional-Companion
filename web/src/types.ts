/**
 * src/types.ts —— 前后端共享的数据形状
 *
 * 这些类型与 src/server/api.py 的返回结构一一对应。
 * 单独放一个文件是为了让"接口契约"显式可见：
 * 后端改字段时，前端的类型检查会立刻报错，而不是运行时才发现。
 */

export interface Engine {
  id: string;
  label: string;
}

export interface Bootstrap {
  engines: Engine[];
  default_engine: string | null;
  opening_suggestions: string[];
  hotlines_markdown: string;
  /**
   * 界面标语（原来是职业边界声明）。
   *
   * 为什么换掉：边界是**功能性**的 —— 危机分层会在真正需要时给出求助热线，
   * 那比常驻声明时机更准；而常驻的"我不是真正的帮助"在用户难过时可能被读成
   * "你正在做的事不算数"（"因使用 AI 而羞耻"是被单独测量过的构念）。
   */
  tagline: string;
  /** 是否需要登录（配置了用户才为 true；本机模式为 false，直接进聊天） */
  auth_required?: boolean;
  user_id?: string;
  user_name?: string;
  is_admin?: boolean;
  /** 主用模型都不可用、已退到备用时的说明（要如实显示给用户） */
  degraded?: boolean;
  degraded_note?: string;
  /** 服务端是否具备本地语音识别能力（决定要不要显示麦克风按钮） */
  voice_enabled?: boolean;
  voice_note?: string;
  /**
   * 回顾（可选）。
   *
   * 四道闸门都在服务端判（chat/review.py），前端只负责"有就显示"。
   * ★ 只在**新会话的空屏**上显示 —— 不打断当下正在说的话。
   */
  review?: { text: string; entry_id: string } | null;
}

export interface HistoryItem {
  session_id: string;
  /** 第一句用户说的话（后端取的）——聊天软件的惯例是拿它当标题 */
  title: string;
  turns: number;
  updated_at: string;
  age_days: number;
}

export interface StoredTurn {
  role: "user" | "assistant";
  content: string;
  fixed_reply: boolean;
  persona_role: string;
  elapsed_ms: number;
  safety_level: string;
  /** 用户中途按了停止 → 这条回复**只说了一半**。
   *  界面照常显示，但服务端不会把它放进后续上下文
   *  （半句可能截断在语义中间，冒充完整回复会误导模型）。 */
  interrupted?: boolean;
  /** 这一轮用了哪些工具（如 ["get_current_weather"]）。
   *  对用户是重要信息：他说的话被送到了哪里，应该是可审计的。 */
  tools?: string[];
}

export interface SessionDetail {
  session_id: string;
  turns: StoredTurn[];
}

/** SSE 事件（后端用 event: 区分） */
export interface MetaPayload {
  safety?: string;
  fixed_reply?: boolean;
  role?: string;
  retrieved?: boolean;
  medium?: boolean;
  /** 阶段标记：role（理解中）/ tools（判断工具）/ generating（组织语言）/
   *  routed（已切快速通道）/ retrying（重试中） */
  phase?: string;
  /** 阶段附带的说明文字（如"这条走快速通道，马上就好"） */
  note?: string;
  retrying?: boolean;
  attempt?: number;
  reason?: string;
}

export interface DonePayload {
  ok: boolean;
  elapsed_ms: number;
  first_token_ms?: number | null;
  role?: string;
  engine?: string;
}

/** 工具执行后的机器状态（目前翻译会给出 unsupported-language / engine-error）。 */
export interface ToolStatus {
  name: string;
  status: string;
}

/** 界面里的一条消息 */
export interface Turn {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** 显示在回应上方的小字，如"用 3 秒回了你" */
  meta: string[];
  /** 正在流式接收中 */
  streaming?: boolean;
  /** 被用户中途停止（半句） */
  interrupted?: boolean;
  /** 模型思考过程（DeepSeek reasoning_content 的累计文本） */
  thinking?: string;
  /** 当前阶段（供"正在思考…"状态行显示） */
  phase?: string;
  /** 阶段说明文字 */
  phaseNote?: string;
  /** 这轮回复开始流式的时刻（前端计时器用） */
  startedAt?: number;
  /** 从发问到首个正文增量的等待时长（"已思考（用时 N 秒）"） */
  thinkingMs?: number;
  /** 这一轮用了哪些工具（可审计） */
  tools?: string[];
  /** 工具状态（前端映射成"参考译法·未经核对"等提示） */
  toolStatuses?: ToolStatus[];
}
