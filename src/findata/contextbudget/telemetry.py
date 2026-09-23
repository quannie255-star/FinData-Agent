"""OTel 遥测：对齐 GenAI 语义约定，并如实区分"标准属性"与"本项目扩展"。

**为什么要区分标准与扩展**：GenAI 语义约定 2026 已 stable，`gen_ai.*` 是跨工具
互认的命名（Langfuse / Phoenix / 各云厂商都认）。本项目自己的量——比如
"这个工具返回了多少字符"——**约定里没有**，硬塞进 `gen_ai.*` 会污染标准命名，
让下游以为那是规范字段。所以扩展一律走 `findata.context.*` 前缀。

**`args_hash` 而不是参数原文**：GenAI 约定用 sha256 存参数摘要，因为参数里
常有用户数据、token、内部 id。记原文等于在自己的 trace 里种 PII。
本项目照此执行——要看参数就去看工具调用日志，trace 里只留可对账的摘要。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from opentelemetry.trace import Span, Tracer

# 对齐的语义约定版本。写死在代码里，换版本必须改这里 + 重跑（否则前后不可比）
SEMCONV = "gen-ai-stable-2026"

# 工具结果码：本项目自定，用于把"失败"分型（GenAI 约定未规定取值域）
RESULT_OK = "ok"
RESULT_NOT_FOUND = "not_found"
RESULT_ERROR = "error"
RESULT_NOT_IMPLEMENTED = "not_implemented"


def args_hash(arguments: Any) -> str:
    """参数的 sha256 前 16 位。稳定、可对账、不泄原文。"""
    blob = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record_model_call(
    tracer: Tracer,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    n_tool_calls: int,
    messages_chars: int,
    tools_chars: int,
    turn: int,
    duration_ms: float,
) -> Span:
    """一次模型调用（span 名 gen_ai.chat）。

    token 三个数字来自 provider 的 usage，实测；chars 是本项目扩展，确定性。
    """
    with tracer.start_as_current_span("gen_ai.chat") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", int(prompt_tokens))
        span.set_attribute("gen_ai.usage.output_tokens", int(completion_tokens))
        span.set_attribute("findata.context.semconv", SEMCONV)
        span.set_attribute("findata.context.turn", int(turn))
        span.set_attribute("findata.context.n_tool_calls", int(n_tool_calls))
        span.set_attribute("findata.context.messages_chars", int(messages_chars))
        span.set_attribute("findata.context.tools_chars", int(tools_chars))
        span.set_attribute("findata.context.duration_ms", round(duration_ms, 3))
        return span


def record_tool_call(
    tracer: Tracer,
    *,
    tool_name: str,
    call_id: str,
    arguments: Any,
    result_code: str,
    payload_chars: int,
    duration_ms: float,
    retry_count: int = 0,
    parallel_batch_size: int = 1,
    fields_json: str = "",
) -> Span:
    """一次工具调用（span 名 gen_ai.tool.call）。

    `payload_chars` 走扩展前缀：GenAI 约定没有"返回多大"这个字段，
    而本项目整份账单的核心正是这个量，所以务必让它可查、但不冒充标准。

    `fields_json` 是**原生字段级记录**（字段名 + 字符数 + 值指纹），
    同样走扩展前缀。**它不含返回原文**——GenAI 约定坚持摘要而非原文，
    理由（PII + 体积）对返回内容一样成立，见 `fields.py` 模块头。
    """
    with tracer.start_as_current_span("gen_ai.tool.call") as span:
        span.set_attribute("gen_ai.tool.name", tool_name)
        span.set_attribute("gen_ai.tool.call_id", call_id)
        span.set_attribute("gen_ai.tool.args_hash", args_hash(arguments))
        span.set_attribute("gen_ai.tool.result_code", result_code)
        span.set_attribute("gen_ai.tool.retry_count", int(retry_count))
        span.set_attribute("gen_ai.tool.parallel_batch_size", int(parallel_batch_size))
        span.set_attribute("gen_ai.tool.duration_ms", round(duration_ms, 3))
        span.set_attribute("findata.context.tool_payload_chars", int(payload_chars))
        if fields_json:
            span.set_attribute("findata.context.tool_fields_json", fields_json)
        return span


def spans_to_records(spans: list[Span] | Any) -> list[dict[str, Any]]:
    """把 span 摊平成可分析的记录（供账单脚本消费）。

    ReadableSpan 的 attributes 里有 None 值时会干扰聚合，这里统一剔掉。
    """
    records: list[dict[str, Any]] = []
    for span in spans:
        attrs = {
            key: value for key, value in dict(span.attributes or {}).items() if value is not None
        }
        records.append(
            {
                "name": span.name,
                "trace_id": format(span.context.trace_id, "032x"),
                "span_id": format(span.context.span_id, "016x"),
                "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
                "attributes": attrs,
            }
        )
    return records
