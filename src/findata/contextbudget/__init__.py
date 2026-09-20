"""上下文预算子包（v4.0 主线）。

回答一个问题：**一个 agent 的上下文账单里，钱花在哪、哪些是白花的。**

三个组件：
- `llm.py`        薄 OpenAI 兼容客户端（本地 Ollama），token 用量实测而非估算
- `schemas.py`    工具清单（4 个活跃 + N 个沉默），复现"工具膨胀"这一量的代价
- `tools.py`      活跃工具的真实实现（读本项目自己的代码仓库）
- `telemetry.py`  OTel span，对齐已成 stable 的 GenAI 语义约定
- `agent.py`      tool loop，全程记账
"""

from __future__ import annotations

__all__ = ["agent", "llm", "schemas", "telemetry", "tools"]
