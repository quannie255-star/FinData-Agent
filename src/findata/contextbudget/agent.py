"""tool loop：多轮工具调用，全程记账。

**这个 loop 的唯一特殊之处**：它把每次模型调用的 `prompt_tokens` 和每次工具
返回的字符数**同时**记下来，并挂到同一个 trace 上。有了这两个量，才有可能
回答"这次 8000 token 的 prompt 里，有多少是工具定义、多少是工具返回、多少是
历史重发"。

**没有做重试**：`retry_count` 一律记 0。这是如实标注，不是遗漏——本 loop
不发起重试，所以本批数据里"重试造成的浪费"这一项**未观测**，账单里必须写
"未观测"而不是写 0。0 会被读成"没有这种浪费"，那是两回事。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from findata.contextbudget import telemetry
from findata.contextbudget.compaction import (
    POLICY_NONE,
    CompactResult,
    compact,
)
from findata.contextbudget.llm import ChatClient, payload_chars
from findata.contextbudget.tools import call_tool

SYSTEM_PROMPT = (
    "You are a code assistant working on a Python repository. "
    "Answer the user's question using the provided tools. "
    "Inspect the repository before answering — do not rely on memory. "
    "When you have enough information, reply with the final answer directly "
    "and stop calling tools."
)

DEFAULT_MAX_TURNS = 8


@dataclass
class ToolCallRecord:
    name: str
    call_id: str
    arguments: dict[str, Any]
    result_code: str
    payload_chars: int
    duration_ms: float
    parse_error: str = ""
    # 原生字段级记录（R5.1）。空表读作"未记录"，**不读作"没有字段"**。
    fields: list[dict[str, Any]] = field(default_factory=list)
    # 进上下文的字符数（**裁剪之后**）。不裁剪时等于 `payload_chars`。
    # 两个都要留：`payload_chars - compacted_chars` 是"这次调用省下多少"的实测值，
    # 而只有 `payload_chars` 才能和 R5.0 的旧批次对齐口径。
    compacted_chars: int = 0
    compact_policy: str = POLICY_NONE
    # 策略**确实改动了文本**才为真。`compacted_chars == payload_chars` 分不清
    # "这个工具没有上限"和"裁了但没省下来"，这笔账要能分开算。
    compact_applied: bool = False

    @property
    def n_fields(self) -> int:
        return len(self.fields)

    @property
    def saved_chars(self) -> int:
        return max(0, self.payload_chars - self.compacted_chars)


@dataclass
class TurnRecord:
    turn: int
    prompt_tokens: int
    completion_tokens: int
    messages_chars: int
    tools_chars: int
    duration_ms: float
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


@dataclass
class AgentRun:
    task: str
    final_answer: str
    stopped_reason: str
    turns: list[TurnRecord] = field(default_factory=list)
    error: str = ""

    @property
    def total_prompt_tokens(self) -> int:
        return sum(t.prompt_tokens for t in self.turns)

    @property
    def total_completion_tokens(self) -> int:
        return sum(t.completion_tokens for t in self.turns)

    @property
    def total_tool_payload_chars(self) -> int:
        return sum(c.payload_chars for t in self.turns for c in t.tool_calls)

    @property
    def total_compacted_chars(self) -> int:
        """真正进了上下文的工具返回字符数（裁剪后）。"""
        return sum(
            c.compacted_chars or c.payload_chars for t in self.turns for c in t.tool_calls
        )

    @property
    def total_saved_chars(self) -> int:
        return sum(c.saved_chars for t in self.turns for c in t.tool_calls)

    @property
    def n_tool_calls(self) -> int:
        return sum(len(t.tool_calls) for t in self.turns)

    @property
    def tool_names_used(self) -> list[str]:
        return [c.name for t in self.turns for c in t.tool_calls]


def run_agent(
    client: ChatClient,
    tracer: Any,
    task: str,
    *,
    tools: list[dict[str, Any]],
    max_turns: int = DEFAULT_MAX_TURNS,
    system_prompt: str = SYSTEM_PROMPT,
    compact_policy: str = POLICY_NONE,
    compact_caps: dict[str, int] | None = None,
) -> AgentRun:
    """跑一个任务，返回带完整账目的 AgentRun。

    `compact_policy` 是 R5.2 的**唯一自变量**。**裁剪发生在"进消息"这一步**，
    字段记录仍取自原始返回：这样"省了多少字符"（实测差）与"哪些字段被引用"
    （字段记录）是两个独立观测，可以互相核对。先裁再记账会让"裁剪没伤到
    被引用的字段"变成同义反复。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    tools_chars = payload_chars(tools) if tools else 0
    run = AgentRun(task=task, final_answer="", stopped_reason="max_turns")

    for turn in range(1, max_turns + 1):
        messages_chars = payload_chars(messages)
        started = time.perf_counter()
        try:
            reply = client.complete(messages, tools)
        except Exception as exc:  # noqa: BLE001 — 网络/服务异常要进账，不能吞
            run.stopped_reason = "error"
            run.error = f"{type(exc).__name__}: {exc}"
            return run
        duration_ms = (time.perf_counter() - started) * 1000

        telemetry.record_model_call(
            tracer,
            model=client.model,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            n_tool_calls=len(reply.tool_calls),
            messages_chars=messages_chars,
            tools_chars=tools_chars,
            turn=turn,
            duration_ms=duration_ms,
        )

        turn_record = TurnRecord(
            turn=turn,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            messages_chars=messages_chars,
            tools_chars=tools_chars,
            duration_ms=duration_ms,
        )
        run.turns.append(turn_record)

        if not reply.tool_calls:
            run.final_answer = reply.content
            run.stopped_reason = "answered"
            return run

        # 助手消息必须原样带回 tool_calls，否则下一轮的 tool 消息无法对应
        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": reply.tool_calls,
            }
        )

        for call in reply.tool_calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            call_id = str(call.get("id") or f"call_{turn}")
            raw_args = fn.get("arguments")
            parse_error = ""
            if isinstance(raw_args, dict):
                arguments = raw_args
            else:
                try:
                    arguments = json.loads(raw_args or "{}")
                except (json.JSONDecodeError, TypeError) as exc:
                    arguments = {}
                    parse_error = f"unparsable arguments: {exc}"

            tool_started = time.perf_counter()
            # `before` = 此刻上下文里的全部文本。**原生记录的第一步就在这里**：
            # 它必须在 append 本条 tool 消息**之前**取，否则工具自己的返回会被
            # 当成"调用前就在上下文里"而全部扣光（那样什么都判不出来）。
            before_text = "\n".join(
                str(m.get("content") or "") for m in messages if m.get("content")
            )
            result = call_tool(name, arguments, before_text=before_text)
            tool_duration = (time.perf_counter() - tool_started) * 1000

            # 裁剪（R5.2）。**原始返回已经进了字段记录**，这里只决定
            # "模型看到的那份文本"是什么。
            cut: CompactResult = compact(
                name,
                result.content,
                policy=compact_policy,
                caps=compact_caps,
                result_code=result.result_code,
            )

            telemetry.record_tool_call(
                tracer,
                tool_name=name,
                call_id=call_id,
                arguments=arguments,
                result_code=result.result_code,
                payload_chars=result.payload_chars,
                duration_ms=tool_duration,
                retry_count=0,  # 本 loop 不重试：账单里"重试浪费"记为未观测，不记为 0
                parallel_batch_size=1,
                fields_json=json.dumps(
                    [f.to_dict() for f in result.fields], ensure_ascii=False
                ),
            )
            turn_record.tool_calls.append(
                ToolCallRecord(
                    name=name,
                    call_id=call_id,
                    arguments=arguments,
                    result_code=result.result_code,
                    payload_chars=result.payload_chars,
                    duration_ms=tool_duration,
                    parse_error=parse_error,
                    fields=[f.to_dict() for f in result.fields],
                    compacted_chars=cut.chars_after,
                    compact_policy=cut.policy,
                    compact_applied=cut.applied,
                )
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": cut.text,
                }
            )

    return run
