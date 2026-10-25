# -*- coding: utf-8 -*-
"""
tests/README.md（说明）
=======================

这一层测试只依赖 Python 标准库，因此「本机 pip 被限制 / CI 里不装第三方依赖」
都能跑：

    python -m unittest discover -s tests -v          # 全量
    python -m unittest tests.test_paths -v           # 单个文件（需要 tests/__init__.py）

各文件覆盖的内容与「防的是什么回归」：

| 文件 | 覆盖 | 防的回归 |
|---|---|---|
| test_paths.py | find_project_root 的 marker 搜索与兜底、mask_uri/mask_secrets、__all__ 导出 | 路径定位静默指错目录；凭据脱敏被改坏；公共模块导出清单再次漏项 |
| test_router.py | extract_student_id、翻译指令解析（中/英后缀）、语言对判定、路由表顺序 | 关键词路由与「到日语/to Russian」解析被改坏，只在真实调用时才暴露 |
| test_rag_scoring.py | 关键词抽取权重与停用词、IDF-lite 打分、双路召回去重合并、融合公式与排序 | RAG 检索编排逻辑被改坏导致召回质量下降，却因为「答案看起来还行」而无人发现 |
| test_security_masking.py | 源码级禁止 `{MONGO_URI}` / `{ZHIPU_API_KEY}` 裸插值；页面渲染必须脱敏 | 凭据再次被打进公网页面（P0 安全修复回退） |
| test_probe_contract.py | 服务端污染开关默认关闭、探针负向模式契约、文档用词；装了 mcp SDK 后跑真实端到端 | 「裸协议探针」重新退化成只会走 happy path 的脚本 |

受限说明：
    MCP 相关端到端用例需要能创建子进程管道 + 已安装 `mcp` SDK；在受限沙箱里
    会自动 SKIP，并明确打印原因，而不是伪装成通过。
"""
