"""LLM 客户端：OpenAI 兼容接口封装（DeepSeek / Qwen / GLM 等）。

统一返回字符串文本，屏蔽不同供应商差异。M3 Agent 编排层只用这一个入口，
方便后续评测层替换模型或 mock。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from findata.config import settings


def make_chat_model(temperature: float = 0.0) -> ChatOpenAI:
    """构建 OpenAI 兼容的 Chat 模型。temperature=0 保证选指标/填参的确定性。"""
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature,
        timeout=settings.llm_timeout_seconds,
    )
