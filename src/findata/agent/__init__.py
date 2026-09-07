"""Agent 层：LLM 归因器与模型客户端。

M4 的定位：不是"用大模型替代规则"，而是让规则与模型在同一套评测上同台竞技。
因此本层对外只暴露 Triage 协议的实现，评测代码不需要知道背后是人写的规则还是模型。
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
