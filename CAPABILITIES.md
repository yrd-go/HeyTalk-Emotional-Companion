# 能力清单（CAPABILITIES）

> 这份清单存在的唯一原因：**换前端/迁移时，我至少五次把已有的功能弄丢了**。
> 具体是：模型选择器、侧边栏折叠按钮、天气/翻译工具、移动端适配。
> 每次都不是"写坏了"，而是"照着重画界面时漏搬了一整块"。
> 单元测试全绿也测不出来，因为函数一直好好的，丢的是"有没有被接线/被调用"。
>
> **规则**：任何人（包括未来的我）动前端或迁移入口时，必须对照这张表逐项验收。
> `tests/test_capabilities.py` 会对**能自动检查的项**做静态断言，作为最低保障。

## 必须永续存在的能力

| # | 能力 | 在哪 | 谁在守 |
|---|---|---|---|
| 1 | 流式聊天（SSE） | `src/server/api.py` `/api/chat` | test_server_api |
| 2 | **天气工具真的被调用** | `api.py` 里 `run_tool_phase` | TestToolWiring |
| 3 | **翻译工具真的被调用** | 同上（与天气同一个工具层） | TestToolWiring |
| 4 | 历史持久化（刷新不丢） | `/api/history*` + HistoryStore | test_server_api |
| 5 | 会话按用户隔离（托管模式） | `history_for(user_id)` | test_server_auth |
| 6 | 登录 + 邀请码自助开通 | `/api/login` `/api/redeem` | test_server_auth |
| 7 | 历史搜索（按标题定位旧对话） | `Sidebar.tsx` + `/api/history` | test_capabilities |
| 8 | 模型选择器**在输入框内部** | `web/src/components/Composer.tsx` | test_capabilities |
| 9 | 侧边栏可折叠（展开/收起各一个入口） | `Sidebar.tsx` `collapseBtn` + `TopBar.tsx` `toggleBtn` | test_capabilities |
| 10 | 停止生成（发送键变停止键） | `Composer.tsx` `stop` | test_capabilities |
| 11 | 被中断的半句：保留显示、不进上下文 | `conversation.py` `interrupted` | test_chat_conversation |
| 12 | **移动端适配**（小屏默认收起 + 抽屉） | `App.tsx` `matchMedia` | test_capabilities |
| 13 | 口令可空、邀请码一次性、幽灵令牌失效 | `server/auth.py` | test_server_auth |
| 14 | 图标/词标可被 `/api/icon` `/api/wordmark` 取到 | `api.py` | test_server_api |
| 15 | ★ **记忆**（关于用户的长期信息，**确认门控**） | `api.py` `_profile_block` + `chat/profile.py` | test_server_memory |
| 16 | ★ **回顾**（四道闸门 + 永久关闭，不推送） | `chat/review.py` | test_chat_review |
| 17 | 语音输入（本地识别、录音不留痕） | `server/transcribe.py` + `useVoice.ts` | test_transcribe |
| 18 | 登录限速（账号 + 来源两个维度） | `server/ratelimit.py` | test_ratelimit |
| 19 | 一键启动给朋友用（服务 + 隧道 + **服务自动重启**；隧道退出需重跑，URL 会变） | `scripts/serve_public.ps1` | test_capabilities |
| 20 | ★ 带中文的 `.ps1` 必须带 **UTF-8 BOM** | `scripts/*.ps1` | test_capabilities |
| 21 | ★ **前端真的渲染出来了**（白屏护栏） | `scripts/smoke_frontend.py` | 手动/CI 跑 |
| 22 | 联网用例的判据是"**连续 3 次都失败**" | `tests/test_chat_weather.py` | 自身 |
| 23 | **思考过程实时可见**（thinking 事件 + 可折叠 + 计时） | `api.py` `_iter_model_deltas` + `MessageList.tsx` | TestLatencyRouting |
| 24 | **查询类快速通道**（工具/天气/翻译/查定义自动走快引擎，可关） | `api.py` `_routing_on` / `_looks_like_lookup` | TestLatencyRouting |
| 25 | **对话思考开关**（默认快速模型；开启后才走推理模型） | `Composer.tsx` `thinkingMode` + `conversation.py` `mode_engine` | test_capabilities |
| 26 | **联网搜索开关**（搜索结果作为上下文，前端显示搜索状态） | `Composer.tsx` `webSearch` + `chat/web_search.py` | test_web_search |
| 27 | **逐条朗读 + 顶部音量总开关**（关闭总开关时单条朗读禁用） | `speech.ts` + `TopBar.tsx` + `MessageList.tsx` | 手动浏览器验证 |
| 28 | **回复操作栏**（复制 / 朗读 / 转发 / 赞同 / 反对 / 重新生成） | `MessageList.tsx` | test_capabilities |
| 29 | **动态推荐话题**（按最近对话生成，不是固定文案） | `api.py` `build_opening_suggestions` + `opening_topics.py` | test_opening_topics |
| 30 | **回到底部按钮 + 一次性开场动效** | `App.tsx` `scrollToBottom` + `IntroOverlay.tsx` | 手动浏览器验证 |

## 为什么会有这张表（血泪记录）

1. **模型选择器**：Streamlit 里官方不支持在 `st.chat_input` 内嵌控件，为它在原生前端折腾了很久；换 React 时差点又放回侧边栏。
2. **折叠按钮**：主人说"去掉重复的那个"，我删错了，后来把按钮加回来却**忘了加监听** —— 而且我的截图验证用 `?collapsed=1` 直接调函数，绕过了点击，所以一直没发现。
3. **天气/翻译**：换 React 服务端时，整个工具阶段（`run_tool_phase`）没搬过去，模型说"我没有实时天气的工具"——**那是真话**。
4. **移动端**：旧版有 `@media (max-width:760px){.sidebar{display:none}}`，React 重写时没带过来。
5. **★ 记忆（第 15 项）—— 最大的一次**：React 服务端把 `profile_block` **写死为 `None`**，
   `src/server/` 里 `ProfileStore` **零引用**。也就是说朋友用的那个版本
   **不会记住任何关于他的事** —— 而"它记得我"是这个产品最核心的承诺之一。
   是主人问"能不能做回顾"时才被撞出来的（回顾的素材正是"用户确认过的记忆"）。

## ★ 清单本身的教训（比上面五条更重要）

第 15 项暴露了一件我一开始没想到的事：**清单也会漏。**

我按"**我想到的功能**"列这张表，而不是按"**产品承诺**"列。
所以"它记得我"这种**从没进过我的清单**的承诺，就永远不会有测试守它 ——
而它恰恰是最大的那一块。

**规则**：列能力时先问"**这个产品答应了用户什么**"，再问"我实现了什么"。
承诺清单从 README 的"核心功能"和产品定位里抄，不从我的记忆里抄。

## ★★ 第六次：**白屏**（第 21 项）—— 这次连"构建通过"都骗过了我

我把 `useChat.ts` 里 `refreshMemory` 的声明写在了 `send` 后面，而 `send` 的
useCallback 依赖数组**在渲染时同步求值** —— 那一刻 `const` 还在
**暂时性死区**里，抛 `ReferenceError` → React 渲染失败 → **整页纯白**。

**四道检查全部放过它**：

| 检查 | 结果 | 为什么看不见 |
|---|---|---|
| `tsc --noEmit` | ✅ | 求值顺序不是类型错误 |
| `vite build` | ✅ | 打包器不分析 TDZ |
| **716 个测试** | ✅ | **全是 Python 测试，一行前端都不渲染** |
| 我 | ❌ | **构建通过就报告完成，没截图** |

**规则**：前端改动**必须以"截图看到它渲染出来"收尾**，不能用"构建通过"代替。
`scripts/smoke_frontend.py` 是这条规则的自动化版本（非白像素占比 < 0.5% 判白屏）。

**同时加的一道保险**：`ErrorBoundary` + `window.onerror` ——
以后渲染崩溃会**在页面上显示原因**，而不是一片白。
**这次事故的全部代价来自沉默**，所以这条保险比修 bug 本身更重要。

## 教训（写成规则）

- **迁移 = 照能力清单验收，不是照界面重画。**
- **测试要测"有没有接线/被调用"，不是只测函数本身。** 函数一直绿，不代表它被用了。
- **截图验证要走真实点击路径**（点按钮），不能直接改状态 —— 否则"按钮没接线"看不见。
