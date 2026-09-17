"""Agent 层：LLM 归因器、模型客户端与多智能体可信问数系统。

M4 的定位：不是"用大模型替代规则"，而是让规则与模型在同一套评测上同台竞技。
因此本层对外只暴露 Triage 协议的实现，评测代码不需要知道背后是人写的规则还是模型。

M12 起，可信问数分析层升级为 Supervisor 编排的多智能体
（`supervisor.py` + `nodes/`）；单 Agent 回环（`graph.py`）保留为对比基线。
"""

from findata.agent.llm import (
    LLMClient,
    LLMError,
    MockLLMClient,
    OpenAICompatClient,
)
from findata.agent.llm_triage import LLMTriage, TriageStats, build_evidence

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMTriage",
    "MockLLMClient",
    "OpenAICompatClient",
    "TriageStats",
    "build_evidence",
]
