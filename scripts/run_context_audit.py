"""R5.0 / R5.1 runner：跑任务集、采 trace、算上下文账单。

用法：
    # 冒烟（2 个任务，全量工具）
    uv run python scripts/run_context_audit.py --limit 2

    # 单个臂（全量 32 任务）
    uv run python scripts/run_context_audit.py --tools all --out-dir examples/context-audit

    # 对照臂（只给 4 个活跃工具）
    uv run python scripts/run_context_audit.py --tools active --out-dir examples/context-audit

**两臂为什么必须分开跑**：这个实验要回答的是"工具清单里有 22 个用不到的定义，
代价是多少"。唯一可靠的量法是**差分**——同样 32 个任务，一臂带全量定义，
一臂只带活跃定义，其余全部不变，`prompt_tokens` 的差就是答案。
不是估算出来的比例，是实测出来的差。

**账单里哪些是实测、哪些是换算，必须分开写**：
  实测 = prompt_tokens / completion_tokens（provider 的 usage）
  实测 = 工具返回字符数、工具定义字符数（本项目自己数的）
  换算 = 字符 → token 的任何比例（报告里出现时必须标注）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from findata.contextbudget.agent import AgentRun, run_agent
from findata.contextbudget.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient
from findata.contextbudget.schemas import ACTIVE_TOOLS, ALL_TOOLS
from findata.contextbudget.tasks import TASKS, Task, is_hit
from findata.contextbudget.telemetry import SEMCONV, spans_to_records
from findata.observability.tracing import (
    get_in_memory_exporter,
    get_tracer,
    setup_tracer,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="上下文账单：跑任务集并计量")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务，0 = 全部")
    parser.add_argument(
        "--tools",
        choices=("all", "active"),
        default="all",
        help="all = 全量清单（含沉默工具）；active = 只有 4 个活跃工具",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--out-dir", default="examples/context-audit")
    parser.add_argument("--tag", default="", help="输出文件名后缀，便于区分臂")
    return parser.parse_args()


def select_tasks(limit: int) -> tuple[Task, ...]:
    if limit and limit > 0:
        return TASKS[:limit]
    return TASKS


def run_all(
    tasks: tuple[Task, ...],
    tools: list[dict[str, Any]],
    model: str,
    base_url: str,
    max_turns: int,
) -> list[AgentRun]:
    tracer = get_tracer("findata.contextbudget")
    runs: list[AgentRun] = []
    with ChatClient(base_url=base_url, model=model) as client:
        for idx, task in enumerate(tasks, start=1):
            started = time.perf_counter()
            run = run_agent(client, tracer, task.question, tools=tools, max_turns=max_turns)
            elapsed = time.perf_counter() - started
            hit = is_hit(task, run.final_answer)
            runs.append(run)
            flag = "HIT " if hit else "miss"
            print(
                f"[{idx:>2}/{len(tasks)}] {task.id} {flag} "
                f"turns={len(run.turns)} calls={run.n_tool_calls} "
                f"ptok={run.total_prompt_tokens} "
                f"payload={run.total_tool_payload_chars}ch "
                f"{elapsed:.1f}s {run.stopped_reason}",
                flush=True,
            )
            if run.error:
                print(f"         error: {run.error}", flush=True)
    return runs


def build_bill(
    runs: list[AgentRun],
    tasks: tuple[Task, ...],
    tools: list[dict[str, Any]],
    model: str,
) -> dict[str, Any]:
    """把 runs 汇总成账单。所有键名都标清实测 / 换算。"""
    by_question = {t.question: t for t in tasks}
    per_tool: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "payload_chars": 0, "codes": Counter()}
    )
    tools_chars_per_call = len(json.dumps(tools, ensure_ascii=False))

    total_turns = 0
    total_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    payload_chars = 0
    tools_chars_total = 0
    hits = 0

    for run in runs:
        total_turns += len(run.turns)
        prompt_tokens += run.total_prompt_tokens
        completion_tokens += run.total_completion_tokens
        payload_chars += run.total_tool_payload_chars
        # 工具定义每一轮都要重发，所以是「每轮字符数 × 轮数」而不是只算一次
        tools_chars_total += tools_chars_per_call * len(run.turns)
        task = by_question.get(run.task)
        if task and is_hit(task, run.final_answer):
            hits += 1
        for turn in run.turns:
            for call in turn.tool_calls:
                total_calls += 1
                entry = per_tool[call.name]
                entry["calls"] += 1
                entry["payload_chars"] += call.payload_chars
                entry["codes"][call.result_code] += 1

    tool_rows = [
        {
            "tool": name,
            "calls": data["calls"],
            "payload_chars": data["payload_chars"],
            "avg_chars": round(data["payload_chars"] / data["calls"], 1) if data["calls"] else 0,
            "codes": dict(data["codes"]),
        }
        for name, data in sorted(per_tool.items(), key=lambda kv: -kv[1]["calls"])
    ]

    active_names = {t["function"]["name"] for t in ACTIVE_TOOLS}
    silent_names = {t["function"]["name"] for t in tools} - active_names
    silent_calls = sum(r["calls"] for r in tool_rows if r["tool"] in silent_names)

    return {
        "scope": {
            "n_tasks": len(runs),
            "model": model,
            "n_tools_in_list": len(tools),
            "n_silent_tools": len(silent_names),
            "tools_chars_per_call": tools_chars_per_call,
            "semconv": SEMCONV,
        },
        # --- 以下是实测 ---
        "measured": {
            "n_answered": sum(1 for r in runs if r.stopped_reason == "answered"),
            "n_errored": sum(1 for r in runs if r.stopped_reason == "error"),
            "keyword_hits": hits,
            "total_turns": total_turns,
            "total_tool_calls": total_calls,
            "silent_tool_calls": silent_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tool_payload_chars": payload_chars,
            "tools_def_chars_carried": tools_chars_total,
        },
        "per_tool": tool_rows,
        "runs": [asdict(r) for r in runs],
    }


def print_report(bill: dict[str, Any], tools_arg: str, elapsed: float) -> None:
    scope = bill["scope"]
    m = bill["measured"]
    print("\n" + "=" * 78)
    print("R5.0 / R5.1 上下文账单")
    print("=" * 78)
    print(
        f"臂={tools_arg}  工具清单={scope['n_tools_in_list']} 个"
        f"（其中沉默 {scope['n_silent_tools']} 个）  "
        f"任务={scope['n_tasks']}  耗时={elapsed:.1f}s"
    )
    print(f"语义约定={scope['semconv']}  工具定义字符/次={scope['tools_chars_per_call']}")

    print("\n--- 实测总量 ---")
    print(f"  完成（模型给出最终答复）  : {m['n_answered']}")
    print(f"  异常中断                  : {m['n_errored']}")
    print(f"  关键词命中（弱判据）      : {m['keyword_hits']} / {scope['n_tasks']}")
    print(f"  模型调用轮数              : {m['total_turns']}")
    print(f"  工具调用次数              : {m['total_tool_calls']}")
    print(f"    其中沉默工具被调用      : {m['silent_tool_calls']}")
    print(f"  prompt_tokens 合计        : {m['prompt_tokens']:,}")
    print(f"  completion_tokens 合计    : {m['completion_tokens']:,}")
    print(f"  工具返回字符合计          : {m['tool_payload_chars']:,}")
    print(f"  工具定义字符（累计携带）  : {m['tools_def_chars_carried']:,}")

    if m["prompt_tokens"]:
        share = m["tools_def_chars_carried"] / (
            m["tools_def_chars_carried"] + m["tool_payload_chars"]
        )
        print(
            f"\n  [换算·字符口径] 工具定义占「工具定义+工具返回」字符的 "
            f"{share:.1%} —— 这不是 token 占比，仅作量级参考"
        )

    print("\n--- 逐工具（实测）---")
    if not bill["per_tool"]:
        print("  （本批没有工具调用）")
    for row in bill["per_tool"]:
        codes = ",".join(f"{k}:{v}" for k, v in row["codes"].items())
        print(
            f"  {row['tool']:<18} calls={row['calls']:<4} "
            f"chars={row['payload_chars']:<9,} avg={row['avg_chars']:<9} [{codes}]"
        )

    print("\n--- 诚实说明 ---")
    print("  · prompt/completion tokens 来自 provider usage，是实测值")
    print("  · 字符数是本项目自己数的，确定性可复现；字符≠token，报告不混用")
    print("  · 关键词命中是弱判据，写的是「命中」不是「答对」")
    print("  · retry_count 恒为 0：本 loop 不重试，「重试造成的浪费」是未观测，不是零")
    print("  · 沉默工具是构造的（见 schemas.py 标注），比例只能算实验条件下的量")


def main() -> int:
    args = parse_args()
    tasks = select_tasks(args.limit)
    tools = ALL_TOOLS if args.tools == "all" else ACTIVE_TOOLS

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.tag}" if args.tag else f"-{args.tools}"

    setup_tracer(service_name="findata-contextbudget")
    exporter = get_in_memory_exporter()

    print(f"模型={args.model}  端点={args.base_url}")
    print(
        f"臂={args.tools}  工具={len(tools)} 个  "
        f"任务={len(tasks)} 个  max_turns={args.max_turns}"
    )

    started = time.perf_counter()
    runs = run_all(tasks, tools, args.model, args.base_url, args.max_turns)
    elapsed = time.perf_counter() - started

    bill = build_bill(runs, tasks, tools, args.model)

    spans = exporter.get_finished_spans() if exporter else []
    span_records = spans_to_records(list(spans))

    runs_path = out_dir / f"runs{suffix}.jsonl"
    spans_path = out_dir / f"spans{suffix}.jsonl"
    bill_path = out_dir / f"bill{suffix}.json"

    with runs_path.open("w", encoding="utf-8") as fh:
        for run in bill["runs"]:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
    with spans_path.open("w", encoding="utf-8") as fh:
        for record in span_records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    bill_path.write_text(
        json.dumps({k: v for k, v in bill.items() if k != "runs"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print_report(bill, args.tools, elapsed)
    print(f"\n落盘：{runs_path}")
    print(f"      {spans_path}  （{len(span_records)} 个 span）")
    print(f"      {bill_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
