"""LLM 客户端：统一接口 + OpenAI 兼容实现 + 确定性桩。

桩（MockLLMClient）不是凑数的：CI 里没有 key、也不该花钱调真模型，
但归因协议、降级链路、评测对比框架必须能被自动验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests

from findata.config import settings


class LLMError(RuntimeError):
    """调用失败。归因器必须捕获它并降级，绝不能让它冒泡打挂巡检。"""


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:
        """返回原始文本；由调用方负责解析与校验。"""
        ...


@dataclass
class OpenAICompatClient:
    """OpenAI 兼容 /chat/completions 客户端（DeepSeek、智谱、通义等均可）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0
    temperature: float = 0.0

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or settings.llm_base_url).rstrip("/")
        self.api_key = self.api_key or settings.llm_api_key
        self.model = self.model or settings.llm_model
        self.timeout = self.timeout or settings.llm_timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str:
        if not self.available:
            raise LLMError("未配置 FINDATA_LLM_API_KEY")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:  # 超时、连接失败、DNS
            raise LLMError(f"请求失败: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"响应格式异常: {e}") from e


class MockLLMClient:
    """确定性桩：按队列返回预置回复，用于解析、校验与降级链路的自动化测试。

    它不假装自己有推理能力——那是自欺欺人。它的职责只有一个：
    让归因器在没有 key、不花钱、不联网的前提下也能被完整验证。
    """

    def __init__(self, replies: list[str] | None = None, default: str = "[]") -> None:
        self._replies = list(replies or [])
        self._default = default
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self._replies:
            return self._replies.pop(0)
        return self._default
