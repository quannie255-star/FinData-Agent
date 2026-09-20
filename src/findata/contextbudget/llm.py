"""薄 OpenAI 兼容客户端 —— 只做三件事：发请求、取 tool_calls、把 usage 带回来。

**为什么不用 langchain-openai / langgraph**（尽管依赖已装、项目别处也在用）：

本项目要算的是「上下文经济学」——必须能逐条说清"这次 `prompt_tokens` 里，
哪些是工具定义贡献的、哪些是工具返回贡献的、哪些是历史重发贡献的"。
框架会在 payload 里加入自己的东西（隐式模板、工具序列化差异、消息重排、重试），
**这些都会混进 `prompt_tokens` 且无法事后剥离**，账单就不可归因了。

所以这里手写一个薄循环：payload 完全由本项目组装，每一段都能记账。
代价是要自己处理 tool loop（见 `agent.py`），收益是每个 token 都有出处。

**环境坑（实测）**：本机有系统代理，走代理访问 `localhost:11434` 会拿到
HTTP 502 —— 那是代理报的错，不是 Ollama 报的错。所以 httpx 必须
`trust_env=False`，不读 `HTTP_PROXY` / `HTTPS_PROXY`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"
# Ollama 不校验 key。填占位串是因为不少客户端以 bool(api_key) 判断"是否可用"，
# 留空会被误判成不可用。
DEFAULT_API_KEY = "ollama-local"


@dataclass
class LLMReply:
    """一次模型调用的结果。

    `prompt_tokens` / `completion_tokens` **直接来自 provider 的 usage 字段，
    不是估算**。整份账单的可信度就建立在这上面——凡是估出来的数，本文档与
    上层报告都必须另行标注，不许与实测数混用。
    """

    content: str
    tool_calls: list[dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatClient:
    """最小可用的 OpenAI 兼容 chat 客户端。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        api_key: str = DEFAULT_API_KEY,
        timeout: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        # trust_env=False 是必需的：否则系统代理会把 localhost 也拦成 502
        self._client = httpx.Client(trust_env=False, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ChatClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        tool_choice: str | None = None,
    ) -> LLMReply:
        """发一次调用。tools 为空时不带 tools 字段（省掉无谓的定义开销）。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        return LLMReply(
            content=(message.get("content") or "").strip(),
            tool_calls=list(message.get("tool_calls") or []),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or ""),
            raw=data,
        )


def payload_chars(payload: Any) -> int:
    """序列化后的字符数。

    **刻意用字符数而不是 token 数**：字符数是确定性的，换台机器、换个 tokenizer
    都是同一个值，可以进断言；token 数依赖 tokenizer，只能在特定模型下比较。
    两个量都有用，但**不许互相冒充**——报告里说"某工具返回占 X token"时，
    那个 X 必须是实测的，不是用字符数除 4 估出来的。
    """
    return len(json.dumps(payload, ensure_ascii=False))
