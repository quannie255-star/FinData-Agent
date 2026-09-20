"""R5.0 / R5.1 runner：跑任务集、采 trace、算上下文账单。

用法：
    # 冒烟（2 个任务，全量工具）
    uv run python scripts/run_context_audit.py --limit 2

    # 单个臂（全量 32 任务）
    uv run python scripts/run_context_audit.py --tools all --out-dir examples/context-audit

    # 对照臂（只给 4 个活跃工具）
    uv run python scripts/run_context_audit.py --tools active --out-dir examples/context-audit

    # **推荐做法**：把语料冻结成快照，所有臂都读快照（见下）
    git worktree add --detach /tmp/ctxbench-snap <冻结提交>
    uv run python scripts/run_context_audit.py --tools all --corpus-root /tmp/ctxbench-snap \
        --tag all7b-fz

**两臂为什么必须分开跑**：这个实验要回答的是"工具清单里有 22 个用不到的定义，
代价是多少"。唯一可靠的量法是**差分**——同样 32 个任务，一臂带全量定义，
一臂只带活跃定义，其余全部不变，`prompt_tokens` 的差就是答案。
不是估算出来的比例，是实测出来的差。

**为什么默认读工作区是个陷阱**（2026-09-20 实测）：工具读的是**活的**本地仓库，
所以在跑臂的同时往仓库里提交文件，会让**同一臂的不同任务**读到不同的目录状态
（实测：`list_files` 三次调用分别对应三个不同的提交）。这样跑出来的"唯一变量"
是句空话，而且**当时的产物里没有任何东西能证伪它**。两条硬规矩：

1. **跑对照实验期间，工作区冻结**——不提交、不改文件；
2. 更好的做法是 `--corpus-root` 指向**冻结快照**，让"冻结"由路径保证而不是靠自律。

账单里的 `reproducibility` 段就是这件事的产物：它记录语料指纹，
`compare_audit_arms.py` 会**核对两臂指纹是否相同**，而不是打印一句声明。

**账单里哪些是实测、哪些是换算，必须分开写**：
  实测 = prompt_tokens / completion_tokens（provider 的 usage）
  实测 = 工具返回字符数、工具定义字符数（本项目自己数的）
  实测 = 语料指纹（内容 sha256，确定性）
  换算 = 字符 → token 的任何比例（报告里出现时必须标注）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from findata.contextbudget.agent import AgentRun, run_agent
from findata.contextbudget.corpus import corpus_manifest
from findata.contextbudget.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient
from findata.contextbudget.schemas import ACTIVE_TOOLS, ALL_TOOLS
from findata.contextbudget.tasks import TASKS, Task, is_hit
from findata.contextbudget.telemetry import SEMCONV, spans_to_records
from findata.contextbudget.tools import get_corpus_root, set_corpus_root
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
    parser.add_argument(
        "--corpus-root",
        default="",
        help=(
            "工具要读的语料根。默认 = 本仓库工作区。"
            "**做对照实验时应指向一份冻结快照**：读工作区时，边跑边提交会让"
            "同一臂的不同任务读到不同目录状态（2026-09-20 实测踩过，见 corpus.py）"
        ),
    )
    return parser.parse_args()


def _git(root: Path, *args: str) -> str:
    """在语料根里跑一条 git 命令；失败返回 "unknown"。

    **失败不抛异常是刻意的**：快照目录可能根本不是 git 仓库（例如
    `git archive` 解出来的纯净树）。那种情况下指纹仍然有效，只是拿不到提交号
    —— 不该因为拿不到提交号就让整批实验跑不起来。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def build_reproducibility(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """可复现三元组的**实验台版本**：代码（提交 + 是否脏）+ 口径 + 数据指纹。

    与 v3.0 的 `manifest.reproducibility` 同构，但"数据"这一项在这里是
    **语料指纹**——因为本实验的数据就是本地仓库的源码与文档，它会被作者
    （也就是我）随时修订。**没有指纹就复现不了**，而且更糟：**看不出两臂
    是不是同一个数据**（这正是本模块诞生的原因）。
    """
    root = get_corpus_root()
    manifest = corpus_manifest(root)
    tools_blob = json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "code_commit": _git(root, "rev-parse", "HEAD"),
        "code_dirty": _git(root, "status", "--porcelain") not in {"", "unknown"},
        "corpus_root": str(root),
        "corpus": manifest,
        "tools_list_sha256": hashlib.sha256(tools_blob).hexdigest()[:32],
        "n_tools_in_list": len(tools),
        "note": (
            "code_commit/code_dirty 取自**语料根**（不是调用方工作区）："
            "实验读的是语料根，提交号也必须是它的。"
            "corpus.sha256 是「相对路径:内容sha256」归并后的指纹，"
            "只记聚合值不记原文。"
        ),
    }


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
    rep = bill.get("reproducibility") or {}
    print("\n" + "=" * 78)
    print("R5.0 / R5.1 上下文账单")
    print("=" * 78)
    print(
        f"臂={tools_arg}  工具清单={scope['n_tools_in_list']} 个"
        f"（其中沉默 {scope['n_silent_tools']} 个）  "
        f"任务={scope['n_tasks']}  耗时={elapsed:.1f}s"
    )
    print(f"语义约定={scope['semconv']}  工具定义字符/次={scope['tools_chars_per_call']}")

    print("\n--- 可复现三元组（本批的「身份证」）---")
    print(f"  语料根        : {rep.get('corpus_root', '未记录')}")
    print(
        f"  语料指纹      : {rep.get('corpus', {}).get('sha256', '未记录')}"
        f"  （{rep.get('corpus', {}).get('n_files', '?')} 个文件，"
        f"{rep.get('corpus', {}).get('bytes', '?')} 字节）"
    )
    print(f"  语料提交      : {rep.get('code_commit', '未记录')}  脏={rep.get('code_dirty', '?')}")
    print(f"  工具清单指纹  : {rep.get('tools_list_sha256', '未记录')}")

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

    if args.corpus_root:
        set_corpus_root(args.corpus_root)
    root = get_corpus_root()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.tag}" if args.tag else f"-{args.tools}"

    setup_tracer(service_name="findata-contextbudget")
    exporter = get_in_memory_exporter()

    print(f"模型={args.model}  端点={args.base_url}")
    print(f"语料根={root}")
    print(
        f"臂={args.tools}  工具={len(tools)} 个  "
        f"任务={len(tasks)} 个  max_turns={args.max_turns}"
    )

    started = time.perf_counter()
    runs = run_all(tasks, tools, args.model, args.base_url, args.max_turns)
    elapsed = time.perf_counter() - started

    bill = build_bill(runs, tasks, tools, args.model)
    bill["reproducibility"] = build_reproducibility(tools)

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
