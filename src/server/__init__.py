# -*- coding: utf-8 -*-
"""src/server —— HTTP 后端（换前端的过渡层）。

这一层只做一件事：把 `src/chat/` 里那些**不依赖 Streamlit** 的模块
包成 HTTP 接口，让新的前端（原生 HTML 起步）能复用全部业务逻辑。

见 api.py 顶部的说明：为什么换、安全不变式是什么、以及技术选型为何从简。
"""

__all__ = ["api"]
