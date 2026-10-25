# -*- coding: utf-8 -*-
"""
src/chat/character.py —— 角落里的陪伴小动物（随姿态变化）
================================================================

它是什么
--------------------------------------------------------------------------
屏幕右下角一只很小的动物，随着对话姿态（roles.py 的六个角色）改变形象。
没人理它时它做自己的事（发呆、打盹、踱步、整理羽毛）；被点一下会吃惊地跳一下。

设计红线（比"好看"重要得多）
--------------------------------------------------------------------------
**它绝不能表现出"因为你不理我而失落/努力"。**

一个角色在用户没理它时显得委屈或"在努力等你"，传递的信息是
"它有需求没被满足，而满足它是你的责任"。这对情绪陪伴是反效果：
正在低落、自责的人看到它，最可能的感觉是**愧疚**，以及"又多了一个要照顾的东西"。

所以本模块只允许三种状态，且全都不带索取：
    idle     —— 做自己的事（发呆/打盹/踱步/整理羽毛）
    happy    —— 被点之后的惊喜欢快（回应，不是索取）
    sleeping —— 长时间没互动就睡着（最中性：睡觉意味着"我不需要你"）

**没有任何"期待/失落/努力"状态**，并且有测试钉住这一点。

交互为什么是纯 CSS（不是 JavaScript）
--------------------------------------------------------------------------
`st.markdown(..., unsafe_allow_html=True)` 会**执行 `<style>`、却不执行 `<script>`**
—— 用 JS 写点击反应的结果是：那段代码被当**纯文本打印在页面上**（真实发生过，
用户一眼看到一串脚本）。所以现在用"隐藏 checkbox + label 热区"的纯 CSS 方案，
零脚本。有测试钉住"输出里永远不许出现 `<script>`"。

关于形象
--------------------------------------------------------------------------
形象是**手写 SVG**（不是位图）：离线可用、零外部依赖、无版权问题。
代价是艺术水准有限——所以 `CHARACTERS` 设计成**完全可替换**：
以后找人画好了，替换对应的 SVG 字符串即可，逻辑一行都不用改。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Character",
    "CHARACTERS",
    "IDLE_STATES",
    "get_character",
    "build_character_html",
    "render",
]

# 允许的状态（故意只有这三种）
IDLE_STATES: tuple[str, ...] = ("idle", "happy", "sleeping")

# 右下角尺寸与透明度：小、半透明，鼠标移上去才变清楚——
# 这样它不会和"第一屏不给用户压力"这条设计原则打架。
_SIZE_PX = 76
_IDLE_OPACITY = 0.45
_HOVER_OPACITY = 0.95


@dataclass(frozen=True)
class Character:
    role_id: str
    name: str                # 给人看的名字
    animal: str              # 形象说明（也用于测试与替代实现的对照）
    accent: str              # 主色（十六进制）
    soft: str                # 辅色
    idle_animation: str      # 闲时动作的 CSS 类名后缀
    svg: str                 # 可替换的 SVG 片段


# ---------------------------------------------------------------------------
# SVG 片段
#
# 约定：需要动起来的部件用 class 标记，CSS 里按姿态挂不同动画：
#     .c-body  身体（呼吸/跳跃）
#     .c-head  头（点头/转头）
#     .c-ear   耳朵/翅膀（抖一下）
#     .c-eye   眼睛（眨眼）
# 这样"同一个形象 + 不同动画"就能复用，不必为每个状态画一张图。
# ---------------------------------------------------------------------------

_BEAR = """
<g class="c-body">
  <circle class="c-ear" cx="22" cy="20" r="9" fill="{accent}"/>
  <circle class="c-ear" cx="58" cy="20" r="9" fill="{accent}"/>
  <ellipse cx="40" cy="42" rx="26" ry="23" fill="{accent}"/>
  <ellipse cx="40" cy="50" rx="13" ry="10" fill="{soft}"/>
  <circle class="c-eye" cx="31" cy="38" r="2.6" fill="#3A3733"/>
  <circle class="c-eye" cx="49" cy="38" r="2.6" fill="#3A3733"/>
  <ellipse cx="40" cy="47" rx="3.4" ry="2.4" fill="#3A3733"/>
</g>
<ellipse class="c-body" cx="40" cy="76" rx="20" ry="7" fill="{soft}" opacity="0.55"/>
"""

_CAT = """
<g class="c-body">
  <path class="c-ear" d="M18 30 L22 12 L36 24 Z" fill="{accent}"/>
  <path class="c-ear" d="M62 30 L58 12 L44 24 Z" fill="{accent}"/>
  <ellipse cx="40" cy="42" rx="25" ry="21" fill="{accent}"/>
  <circle class="c-eye" cx="32" cy="40" r="2.4" fill="#3A3733"/>
  <circle class="c-eye" cx="48" cy="40" r="2.4" fill="#3A3733"/>
  <path d="M36 47 q4 3 8 0" stroke="#3A3733" stroke-width="1.6" fill="none"
        stroke-linecap="round"/>
  <path d="M14 44 h10 M14 49 h10 M56 44 h10 M56 49 h10" stroke="{soft}"
        stroke-width="1.4" stroke-linecap="round"/>
</g>
<ellipse class="c-body" cx="40" cy="76" rx="22" ry="7" fill="{soft}" opacity="0.55"/>
"""

_OWL = """
<g class="c-body">
  <path class="c-ear" d="M20 26 L24 10 L38 22 Z" fill="{accent}"/>
  <path class="c-ear" d="M60 26 L56 10 L42 22 Z" fill="{accent}"/>
  <ellipse cx="40" cy="44" rx="26" ry="24" fill="{accent}"/>
  <circle cx="31" cy="40" r="8" fill="{soft}"/>
  <circle cx="49" cy="40" r="8" fill="{soft}"/>
  <circle class="c-eye" cx="31" cy="40" r="3.4" fill="#3A3733"/>
  <circle class="c-eye" cx="49" cy="40" r="3.4" fill="#3A3733"/>
  <path d="M40 46 l-4 6 h8 z" fill="#E0A24A"/>
  <path d="M30 66 q10 6 20 0" stroke="{soft}" stroke-width="1.6" fill="none"
        stroke-linecap="round"/>
</g>
"""

_HEN = """
<g class="c-body">
  <path class="c-ear" d="M40 12 q4 -8 8 0 q4 -6 6 2 h-14 z" fill="#E0A24A"/>
  <ellipse cx="40" cy="44" rx="25" ry="23" fill="{accent}"/>
  <circle class="c-eye" cx="32" cy="40" r="2.4" fill="#3A3733"/>
  <circle class="c-eye" cx="48" cy="40" r="2.4" fill="#3A3733"/>
  <path d="M36 48 l8 0 l-4 5 z" fill="#E0A24A"/>
  <path class="c-ear" d="M16 46 q-10 8 -2 16 q8 -4 10 -12 z" fill="{soft}"/>
  <path class="c-ear" d="M64 46 q10 8 2 16 q-8 -4 -10 -12 z" fill="{soft}"/>
</g>
"""

_FOX = """
<g class="c-body">
  <path class="c-ear" d="M18 30 L20 8 L36 22 Z" fill="{accent}"/>
  <path class="c-ear" d="M62 30 L60 8 L44 22 Z" fill="{accent}"/>
  <ellipse cx="40" cy="42" rx="25" ry="21" fill="{accent}"/>
  <ellipse cx="40" cy="51" rx="12" ry="9" fill="{soft}"/>
  <circle class="c-eye" cx="31" cy="38" r="2.6" fill="#3A3733"/>
  <circle class="c-eye" cx="49" cy="38" r="2.6" fill="#3A3733"/>
  <ellipse cx="40" cy="48" rx="3.2" ry="2.2" fill="#3A3733"/>
</g>
<path class="c-ear" d="M64 66 q16 2 14 -10 q-10 6 -14 2 z" fill="{soft}"/>
"""

_BIRD = """
<g class="c-body">
  <ellipse cx="40" cy="44" rx="23" ry="21" fill="{accent}"/>
  <path class="c-ear" d="M52 34 q18 -4 16 6 q-12 4 -18 -2 z" fill="{soft}"/>
  <path class="c-ear" d="M28 46 q-14 4 -12 12 q10 2 14 -6 z" fill="{soft}"/>
  <circle class="c-eye" cx="32" cy="38" r="2.6" fill="#3A3733"/>
  <path d="M20 44 l-8 3 l8 3 z" fill="#E0A24A"/>
  <path d="M36 66 q4 6 8 0" stroke="{soft}" stroke-width="1.4" fill="none"
        stroke-linecap="round"/>
</g>
"""


def _character(role_id: str, name: str, animal: str, accent: str, soft: str,
               idle_animation: str, template: str) -> Character:
    return Character(
        role_id=role_id,
        name=name,
        animal=animal,
        accent=accent,
        soft=soft,
        idle_animation=idle_animation,
        svg=template.replace("{accent}", accent).replace("{soft}", soft),
    )


# 六个姿态各一只。配色沿用整站的低饱和暖色，避免变成"彩色玩具"。
CHARACTERS: dict[str, Character] = {
    "friend": _character(
        "friend", "朋友", "圆耳朵小熊", "#A9825C", "#E4CDB4", "nod", _BEAR),
    "listener": _character(
        "listener", "倾听者", "蜷着的猫", "#D9CFC4", "#B8AA9B", "curl", _CAT),
    "mentor": _character(
        "mentor", "良师", "猫头鹰", "#8C8B86", "#D8C9A6", "tilt", _OWL),
    "parent": _character(
        "parent", "无条件接纳", "护雏的母鸡", "#E3C88A", "#F2E3C0", "brood", _HEN),
    "companion": _character(
        "companion", "同行者", "小狐狸", "#C98A5B", "#F0DCC8", "pace", _FOX),
    "witness": _character(
        "witness", "见证者", "小鸟", "#9FB6C4", "#E2EAF0", "hop", _BIRD),
}


def get_character(role_id: str | None) -> Character:
    """取角色对应的形象；未知或缺失一律回落到「朋友」（不抛异常）。"""
    if role_id and role_id in CHARACTERS:
        return CHARACTERS[role_id]
    return CHARACTERS["friend"]


# ---------------------------------------------------------------------------
# 影子戏（动画）：全部是"做自己的事"，没有任何索取意味
# ---------------------------------------------------------------------------
_IDLE_KEYFRAMES = {
    "nod": "c-nod",        # 小熊：慢慢点头
    "curl": "c-curl",      # 猫：蜷着呼吸、耳朵偶尔动
    "tilt": "c-tilt",      # 猫头鹰：转头看别处
    "brood": "c-brood",    # 母鸡：轻轻晃、护着翅膀
    "pace": "c-pace",      # 狐狸：原地踱步
    "hop": "c-hop",        # 小鸟：蹦一下、抖翅膀
}

_ANIMATION_CSS = """
/* 呼吸：所有形象共用的"活着"感 */
@keyframes pet-breathe {
  0%, 100% { transform: translateY(0) scale(1); }
  50%      { transform: translateY(-1.5px) scale(1.015); }
}
/* 眨眼：每 ~5 秒一次，很轻 */
@keyframes pet-blink {
  0%, 92%, 100% { transform: scaleY(1); }
  95%           { transform: scaleY(0.1); }
}
/* 被点之后的惊喜欢快：跳一下再落回 */
@keyframes pet-startle {
  0%   { transform: translateY(0) rotate(0deg); }
  18%  { transform: translateY(-12px) rotate(-5deg); }
  38%  { transform: translateY(0) rotate(4deg); }
  58%  { transform: translateY(-6px) rotate(-2deg); }
  100% { transform: translateY(0) rotate(0deg); }
}

/* 各自的小动作 */
@keyframes pet-nod  { 0%,100% { transform: rotate(0deg);} 50% { transform: rotate(5deg);} }
@keyframes pet-curl { 0%,100% { transform: scale(1);}      50% { transform: scale(1.03,0.98);} }
@keyframes pet-tilt { 0%,45%,100% { transform: rotate(0deg);} 60%,80% { transform: rotate(-9deg);} }
@keyframes pet-brood{ 0%,100% { transform: translateX(0);}  50% { transform: translateX(2.5px);} }
@keyframes pet-pace { 0% { transform: translateX(-3px);}  50% { transform: translateX(3px);}
                      100% { transform: translateX(-3px);} }
@keyframes pet-hop  { 0%,70%,100% { transform: translateY(0);} 82% { transform: translateY(-7px);} }

.pet-idle .c-body { animation: pet-breathe 3.6s ease-in-out infinite; }
.pet-idle .c-eye  { animation: pet-blink 5.2s ease-in-out infinite; transform-origin: center; }
.pet-idle .c-head { animation: pet-breathe 4.4s ease-in-out infinite; }

.pet-idle.pet-nod   .c-body { animation-name: pet-nod; animation-duration: 4.2s; }
.pet-idle.pet-curl  .c-body { animation-name: pet-curl; animation-duration: 3.1s; }
.pet-idle.pet-tilt  .c-body { animation-name: pet-tilt; animation-duration: 6.0s; }
.pet-idle.pet-brood .c-body { animation-name: pet-brood; animation-duration: 3.4s; }
.pet-idle.pet-pace  .c-body { animation-name: pet-pace; animation-duration: 4.8s; }
.pet-idle.pet-hop   .c-body { animation-name: pet-hop; animation-duration: 3.9s; }

/* 被点之后跳到开心态——由 CSS 单独实现，见下方 #dsh-pet-tap:checked 规则
   （原本这里挂的是 .pet-happy 类，由脚本添加；脚本方案已废弃，原因见下方注释） */

/* 长时间没互动：睡着（最中性的状态——睡觉意味着"我不需要你"） */
.pet-sleeping .c-body { animation: pet-breathe 5.5s ease-in-out infinite; }
.pet-sleeping .c-eye  { animation: none; transform: scaleY(0.12); }

/* 等模型回应时：轻轻闪一下（"我在想"）。
   刻意做得很轻——0.55 倍缩放到 1 倍、两秒一轮、而且不是全透明，
   避免变成催促或让人焦虑的闪烁。 */
@keyframes pet-think {
  0%, 100% { transform: scale(0.94); }
  50%      { transform: scale(1.02); }
}
"""


def build_character_html(role_id: str | None, *, state: str = "idle",
                         enable_interaction: bool = True,
                         flash: bool = False) -> str:
    """生成角色的 HTML（固定定位在右下角）。

    state 只接受 IDLE_STATES；其它值一律回落到 idle。

    ⚠ 为什么交互是**纯 CSS**、不是 JavaScript（这是一个真实事故的修复）：
        最初"点一下 → 吃惊地跳一下"是用 <script> + addEventListener 实现的。
        但 `st.markdown(..., unsafe_allow_html=True)` 会**执行 <style>、
        却不会执行 <script>** —— 后果是那段 JS 源代码被当**纯文本**
        直接打印在页面标题下方，用户一眼就看到一串脚本，非常难看。

        所以改成纯 CSS 交互：一个视觉上隐藏的 checkbox +
        覆盖在形象上的 label（点击热区）。点击切换 checkbox 状态，
        CSS 里 `:checked ~ ...` 负责播放"吃惊"动画。全程零脚本，
        也**不会触发 Streamlit 重跑**（对角落里的小东西正合适）。
    """
    character = get_character(role_id)
    safe_state = state if state in IDLE_STATES else "idle"
    animation_class = _IDLE_KEYFRAMES.get(character.idle_animation, "c-nod")
    interactive = bool(enable_interaction) and safe_state != "sleeping"

    # 等模型回应时的"我在想"轻微闪烁：只在 flash=True 时叠加。
    # 它盖住闲时动画（animation 只能有一个），但这是有意的——
    # "在想"本来就该取代"发呆"。
    flash_css = ""
    if flash:
        flash_css = f"""
#dsh-pet-{character.role_id} .c-body {{
  animation: pet-think 2.1s ease-in-out infinite;
}}
#dsh-pet-{character.role_id} .c-eye {{ animation: none; }}
"""

    # 点击热区（label）与"吃惊"动画的 CSS：只在允许交互时输出
    tap_css = ""
    tap_html = ""
    if interactive:
        tap_css = f"""
#dsh-pet-tap-{character.role_id} {{
  position: absolute;
  opacity: 0;
  width: 1px;
  height: 1px;
  pointer-events: none;
}}
#dsh-pet-hit-{character.role_id} {{
  position: absolute;
  inset: 0;
  cursor: pointer;
  border-radius: 50%;
  -webkit-tap-highlight-color: transparent;
}}
/* 点击（或键盘回车）后：跳一下，动画结束回到常态。
   不需要脚本，因此也不会触发页面重跑。 */
#dsh-pet-tap-{character.role_id}:active ~ svg .c-body,
#dsh-pet-tap-{character.role_id}:checked ~ svg .c-body {{
  animation: pet-startle 900ms ease-out;
}}
#dsh-pet-tap-{character.role_id}:active ~ svg .c-eye,
#dsh-pet-tap-{character.role_id}:checked ~ svg .c-eye {{
  transform: scaleY(1.5);
}}
"""
        tap_html = f"""
  <input type="checkbox" id="dsh-pet-tap-{character.role_id}"
         aria-label="{character.name}（装饰用，点一下会有反应）">
  <label for="dsh-pet-tap-{character.role_id}"
         id="dsh-pet-hit-{character.role_id}"></label>
"""

    return f"""
<style>
{_ANIMATION_CSS}
{tap_css}
{flash_css}
#dsh-pet-{character.role_id} {{
  position: fixed;
  right: 18px;
  bottom: 16px;
  width: {_SIZE_PX}px;
  height: {_SIZE_PX}px;
  opacity: {_IDLE_OPACITY};
  transition: opacity 260ms ease;
  cursor: pointer;
  z-index: 900;
  user-select: none;
  -webkit-tap-highlight-color: transparent;
}}
#dsh-pet-{character.role_id}:hover {{ opacity: {_HOVER_OPACITY}; }}
#dsh-pet-{character.role_id} svg {{ width: 100%; height: 100%; display: block; }}

/* 尊重系统的"减少动态效果"偏好：不动的静态形象依然可用 */
@media (prefers-reduced-motion: reduce) {{
  #dsh-pet-{character.role_id} * {{ animation: none !important; }}
}}
@media (max-width: 640px) {{
  #dsh-pet-{character.role_id} {{
    width: 56px; height: 56px; right: 10px; bottom: 78px;
  }}
}}
</style>

<div id="dsh-pet-{character.role_id}"
     class="pet-{safe_state} {animation_class}">
{tap_html}  <svg viewBox="0 0 80 84" xmlns="http://www.w3.org/2000/svg"
       aria-hidden="true" focusable="false">
    {character.svg}
  </svg>
</div>
"""


def render(st_module: Any, role_id: str | None, *, state: str = "idle",
           enable_interaction: bool = True, flash: bool = False) -> None:
    """把角色挂到页面上。样式失败绝不能影响对话。"""
    try:
        st_module.markdown(
            build_character_html(role_id, state=state,
                                 enable_interaction=enable_interaction,
                                 flash=flash),
            unsafe_allow_html=True,
        )
    except Exception:  # noqa: BLE001 - 装饰性组件，失败就当作不存在
        pass
