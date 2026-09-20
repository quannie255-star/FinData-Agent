"""R5.0 前置探针：确认本机 Ollama 的 OpenAI 兼容端点可用，且支持 tool calling。

**为什么需要这个探针**：R5.0 的产出是「上下文账单」，而账单 = 每次调用的
token 从哪来。这要求两件事同时成立：
  ① 能拿到一次真实的 tool_call（否则没有工具返回可以记账）
  ② 能拿到精确的 token 用量（否则只能估算，估算出来的账单不能当证据）
任缺一个，后面的账都算不出来。所以先在最小面上把这两件事证出来，
再去搭 agent。

**环境坑（实测）**：本机有系统代理，走代理访问 localhost:11434 会拿到
HTTP 502（不是 Ollama 在报错，是代理在报错）。所以 httpx 必须
`trust_env=False`，不读 HTTP_PROXY / HTTPS_PROXY。

用法:
    uv run python scripts/probe_ollama_tools.py
退出码:
    0 = 端点可用且返回了 tool_call（R5.0 可以往下做）
    1 = HTTP 层不通
    2 = 通了但模型没有发起 tool_call（换更大的模型再试）
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE_URL = os.environ.get("FIN_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL = os.environ.get("FIN_LLM_MODEL", "qwen2.5:3b")
# Ollama 不校验 key，但客户端常以 available = bool(api_key) 判断可用性，故填占位串
API_KEY = os.environ.get("FIN_LLM_API_KEY", "ollama-local")

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a given city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "Name of the city"}},
            "required": ["city"],
        },
    },
}


def main() -> int:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "北京的天气怎么样？请调用工具查一下。"}],
        "tools": [WEATHER_TOOL],
        "stream": False,
    }

    with httpx.Client(trust_env=False, timeout=180.0) as client:
        resp = client.post(
            f"{BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

    print(f"endpoint : {BASE_URL}")
    print(f"model    : {MODEL}")
    print(f"HTTP     : {resp.status_code}")
    if resp.status_code != 200:
        print("--- body (前 600 字符) ---")
        print(resp.text[:600])
        return 1

    data = resp.json()
    message = data["choices"][0]["message"]
    calls = message.get("tool_calls") or []

    print(f"tool_calls: {len(calls)}")
    for call in calls:
        fn = call["function"]
        print(f"  - {fn['name']}({fn['arguments']})")
    content = (message.get("content") or "").strip()
    print(f"content   : {content[:160]!r}")
    print(f"usage     : {json.dumps(data.get('usage'), ensure_ascii=False)}")

    if not calls:
        print("\n[!] 端点通，但模型没有发起 tool_call —— 换 qwen2.5:latest 再试")
        return 2

    usage = data.get("usage") or {}
    if not usage.get("prompt_tokens"):
        print("\n[!] 没有 usage.prompt_tokens —— token 账单无法精确计算，需要换采集方式")
        return 2

    print("\n[OK] 端点可用 + tool_call 可用 + token 用量可采 —— R5.0 可以往下做")
    return 0


if __name__ == "__main__":
    sys.exit(main())
