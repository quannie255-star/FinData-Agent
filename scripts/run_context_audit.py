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
from findata.contextbudget.compaction import (
    POLICIES,
    POLICY_NONE,
    caps_from_profile,
    load_profile,
)
from findata.contextbudget.corpus import corpus_manifest, manifest_of
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
    parser.add_argument(
        "--tools-from",
        default="",
        help=(
            "从 `fetch_mcp_corpus.py` 产出的真实 server 语料里取工具清单"
            "（**优先于 --tools**）。用它替换手写的 27 个定义，"
            "`n_tools_in_list` 与 `tools_list_sha256` 都按这份清单算。"
            "账单里会记下语料自报的指纹与重算的指纹是否一致——"
            "这是「实验跑的就是这份语料」的一次核对"
        ),
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
    parser.add_argument(
        "--compact",
        choices=POLICIES,
        default=POLICY_NONE,
        help=(
            "返回内容裁剪策略（R5.2 的唯一自变量）："
            "none=不裁；head=保头部（合理性判断，设计者常数）；"
            "hint=整体换成一行提示（合理性判断的极端形态，协议仿真）；"
            "cap=按 trace 学出的上限删（证据型）"
        ),
    )
    parser.add_argument(
        "--compact-profile",
        default="",
        help="`--compact cap` 所需的 profile JSON（由 build_compact_profile.py 产出）",
    )
    parser.add_argument(
        "--compact-quantile",
        default="p90",
        help="cap 取哪个分位数（正式臂 p90；p50 属于敏感性检验，必须标注）",
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


def _executed_code_files() -> list[Path]:
    """**真正被执行的**那些文件（代码指纹的覆盖面）。

    刻意给的是"整个 `src/findata/` 树 + 本脚本"，不是"我 import 到的那几个模块"：
    多覆盖只会让指纹更敏感（更容易报不等），少覆盖会让"不等"漏掉。
    """
    repo = Path(__file__).resolve().parents[1]
    return sorted((repo / "src" / "findata").rglob("*.py")) + [Path(__file__).resolve()]


def load_tools_from_corpus(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 `fetch_mcp_corpus.py` 产出的语料里取工具清单，返回 `(工具清单, 身份信息)`。

    身份信息里**同时**带语料自报的指纹与本次重算的指纹，并在调用处断言二者一致。
    这不是多此一举：语料文件与账单是两份独立落盘的东西，
    "实验跑的就是这份语料"这句话此前没有任何产物能证伪它。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    tools = payload.get("tools") or []
    meta = payload.get("meta") or {}
    return list(tools), {
        "kind": "mcp_corpus",
        "path": str(path),
        "source": meta.get("source", ""),
        "source_kind": meta.get("source_kind", ""),
        "n_servers_ok": meta.get("n_servers_ok"),
        "n_servers_failed": meta.get("n_servers_failed"),
        "corpus_tools_list_sha256": meta.get("tools_list_sha256", ""),
        "note": (
            "工具定义来自**真实公开 server**（`page_declared` = 别人仓库声明的 schema，"
            "`introspected` = 本机对真 server 走 stdio tools/list 当场报的）。"
            "抓失败的 server 在语料的 failures 里单列，**没有**被当成 0 个工具。"
        ),
    }


def build_reproducibility(
    tools: list[dict[str, Any]],
    *,
    compact_policy: str = POLICY_NONE,
    caps: dict[str, int] | None = None,
    profile_path: str = "",
    tools_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """可复现三元组的**实验台版本**：代码 + 口径 + 数据指纹。

    与 v3.0 的 `manifest.reproducibility` 同构，但"数据"这一项在这里是
    **语料指纹**——因为本实验的数据就是本地仓库的源码与文档，它会被作者
    （也就是我）随时修订。**没有指纹就复现不了**，而且更糟：**看不出两臂
    是不是同一个数据**（这正是本模块诞生的原因）。

    R5.2 之后"口径"这一项多了一维：**裁剪策略与它的输入**。
    `cap` 臂的行为完全由 profile 决定，所以 profile 本身也要指纹化——
    否则别人拿到同一批 runs 也复现不出同一个裁剪。

    **2026-09-22 补上的一个真缺口**：`code_commit` 取自**语料根**，而语料根在这套
    实验里是一份冻结快照 —— **被执行的那份代码在调用方工作区**，此前完全没有被
    记录，于是"同批各臂用的是同一份代码"这句话**没有产物能证伪它**，只剩 mtime
    这种弱证据（`git checkout` 也会改 mtime）。现在补了 `code` 一项（`manifest_of`
    口径与语料指纹相同）：它测的是**被执行的源码文本**，与语料根无关。
    `code_root` 一并记下，用来解释"为什么两个指纹可能来自不同目录"。
    """
    root = get_corpus_root()
    manifest = corpus_manifest(root)
    code_root = Path(__file__).resolve().parents[1]
    code = manifest_of(_executed_code_files(), base=code_root)
    tools_blob = json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    profile_sha = ""
    profile_corpus = ""
    if profile_path:
        try:
            raw = Path(profile_path).read_bytes()
            profile_sha = hashlib.sha256(raw).hexdigest()[:32]
            profile_corpus = str(json.loads(raw).get("corpus_root", ""))
        except (OSError, json.JSONDecodeError):
            profile_sha = "unreadable"
    return {
        "code_commit": _git(root, "rev-parse", "HEAD"),
        "code_dirty": _git(root, "status", "--porcelain") not in {"", "unknown"},
        "corpus_root": str(root),
        "corpus": manifest,
        "executed_code_root": str(code_root),
        "executed_code": code,
        "executed_code_commit": _git(code_root, "rev-parse", "HEAD"),
        "executed_code_dirty": _git(code_root, "status", "--porcelain")
        not in {"", "unknown"},
        "tools_list_sha256": hashlib.sha256(tools_blob).hexdigest()[:32],
        "n_tools_in_list": len(tools),
        # 工具清单**从哪来**，以及（用真实语料时）语料自报指纹与重算指纹是否一致。
        # 只有 `tools_list_sha256` 一个数是不够的：手写的 27 个与真实抓来的 230 个
        # 都会有指纹，但它们的可外推性完全不同。
        "tools_source": tools_source or {"kind": "builtin", "note": "schemas.py 内置清单"},
        "compact_policy": compact_policy,
        "compact_caps": dict(caps or {}),
        "compact_profile_path": profile_path,
        "compact_profile_sha256": profile_sha,
        "compact_profile_corpus_root": profile_corpus,
        "note": (
            "code_commit/code_dirty 取自**语料根**（不是调用方工作区）："
            "实验读的是语料根，提交号也必须是它的。"
            "corpus.sha256 是「相对路径:内容sha256」归并后的指纹，"
            "只记聚合值不记原文。"
            "**executed_code 才是真正跑起来的那份源码**（口径同语料指纹，"
            "覆盖整个 src/findata/ 树 + 本脚本）：语料根与代码根常常不是同一个目录"
            "（语料是快照、代码是工作区），所以这两个身份必须分开记。"
            "两批读数只有在 corpus.sha256、executed_code.sha256、"
            "compact_policy/caps 都相同时才可比。"
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
    *,
    compact_policy: str = POLICY_NONE,
    compact_caps: dict[str, int] | None = None,
) -> list[AgentRun]:
    tracer = get_tracer("findata.contextbudget")
    runs: list[AgentRun] = []
    with ChatClient(base_url=base_url, model=model) as client:
        for idx, task in enumerate(tasks, start=1):
            started = time.perf_counter()
            run = run_agent(
                client,
                tracer,
                task.question,
                tools=tools,
                max_turns=max_turns,
                compact_policy=compact_policy,
                compact_caps=compact_caps,
            )
            elapsed = time.perf_counter() - started
            hit = is_hit(task, run.final_answer)
            runs.append(run)
            flag = "HIT " if hit else "miss"
            print(
                f"[{idx:>2}/{len(tasks)}] {task.id} {flag} "
                f"turns={len(run.turns)} calls={run.n_tool_calls} "
                f"ptok={run.total_prompt_tokens} "
                f"payload={run.total_tool_payload_chars}ch "
                f"into_ctx={run.total_compacted_chars}ch "
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
    *,
    compact_policy: str = POLICY_NONE,
    compact_caps: dict[str, int] | None = None,
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
    compacted_chars = 0
    tools_chars_total = 0
    hits = 0

    for run in runs:
        total_turns += len(run.turns)
        prompt_tokens += run.total_prompt_tokens
        completion_tokens += run.total_completion_tokens
        payload_chars += run.total_tool_payload_chars
        compacted_chars += run.total_compacted_chars
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
            # R5.2 的唯一自变量。**必须进账单**：只有它能让两批读数可比/不可比。
            "compact_policy": compact_policy,
            "compact_caps": dict(compact_caps or {}),
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
            # 进上下文的字符数（裁剪后）。不裁剪时两者相等——相等本身也是信息。
            "tool_compacted_chars": compacted_chars,
            "tool_saved_chars": max(0, payload_chars - compacted_chars),
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
    src = rep.get("tools_source") or {}
    if src.get("kind") == "mcp_corpus":
        match = src.get("fingerprint_matches_corpus")
        print(
            f"  工具清单来源  : **真实 MCP server 语料** {src.get('path')}"
            f"  （{src.get('source_kind', '?')}，server {src.get('n_servers_ok')} 台）"
        )
        print(
            f"                 语料自报指纹 == 本次重算：{'✓ 是' if match else '✗ 不是'}"
            f"  重算值 {src.get('computed_tools_list_sha256', '?')}"
        )
    else:
        print(f"  工具清单来源  : 内置清单（{src.get('detail', 'schemas.py')}）")

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
    print(
        f"  工具返回字符（原始）      : {m['tool_payload_chars']:,}"
        f"   → 进上下文 {m['tool_compacted_chars']:,}"
        f"（省 {m['tool_saved_chars']:,}，裁剪={scope['compact_policy']}）"
    )

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
    src = (bill.get("reproducibility") or {}).get("tools_source") or {}
    if src.get("kind") == "mcp_corpus":
        ok = src.get("n_servers_ok") or 0
        bad = src.get("n_servers_failed") or 0
        total = ok + bad
        rate = f"{ok / total:.1%}" if total else "n/a"
        print("  · 工具清单来自**真实公开 MCP server**（声明式 schema 或本机 introspection）")
        print(
            f"    ⇒ server 成功 {ok}/{total}（成功率 **{rate}**），失败 {bad} 台"
            " —— 失败的**没有**被当成「该 server 有 0 个工具」"
        )
    else:
        print("  · 沉默工具是构造的（见 schemas.py 标注），比例只能算实验条件下的量")


def main() -> int:
    args = parse_args()
    tasks = select_tasks(args.limit)
    tools = ALL_TOOLS if args.tools == "all" else ACTIVE_TOOLS
    tools_arg = args.tools
    tools_source: dict[str, Any] = {
        "kind": "builtin",
        "detail": "4 个真实实现 + 23 个构造的沉默工具（schemas.py）",
        "note": "沉默工具是构造的 ⇒ 比例只能算实验条件下的量，不是行业事实",
    }
    if args.tools_from:
        try:
            tools, tools_source = load_tools_from_corpus(args.tools_from)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"--tools-from 读不了：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if not tools:
            # 空工具清单会静默跑出一批"没用工具"的读数，那比报错危险得多。
            print(f"--tools-from 里一个工具都没有：{args.tools_from}", file=sys.stderr)
            return 2
        recomputed = hashlib.sha256(
            json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32]
        declared = str(tools_source.get("corpus_tools_list_sha256") or "")
        tools_source["computed_tools_list_sha256"] = recomputed
        tools_source["fingerprint_matches_corpus"] = recomputed == declared
        tools_arg = f"{Path(args.tools_from).name}({len(tools)})"
        print(f"工具清单来自真实语料：{args.tools_from}")
        print(
            f"  证据类型={tools_source.get('source_kind', '?')}  "
            f"server 成功={tools_source.get('n_servers_ok')} / "
            f"失败={tools_source.get('n_servers_failed')}"
        )
        if recomputed != declared:
            # 语料自报的指纹与本次重算不一致 ⇒ 中间被动过，或指纹算法变了。
            # 这不是警告，是**这批读数不能与别的批次比**的理由。
            print(
                f"  ✗ 语料自报指纹 {declared or '(缺)'} ≠ 重算指纹 {recomputed}\n"
                "    ⇒ 这份语料与它自己的 meta 对不上，先查清楚再比读数",
                file=sys.stderr,
            )
        else:
            print(f"  ✓ 指纹核对一致：{recomputed}（语料自报 == 本次重算）")

    if args.corpus_root:
        set_corpus_root(args.corpus_root)
    root = get_corpus_root()

    caps: dict[str, int] = {}
    if args.compact == "cap":
        if not args.compact_profile:
            print(
                "--compact cap 需要 --compact-profile（学出上限的 profile JSON）",
                file=sys.stderr,
            )
            return 2
        caps = caps_from_profile(
            load_profile(args.compact_profile), quantile=args.compact_quantile
        )
        print(f"cap 上限（来自 profile，分位 {args.compact_quantile}）：{caps}")
        # 上限是**从另一批 trace 学出来的**，那批 trace 未必跑在同一个语料上。
        # 不一致不是错误（跨语料反而是防自泄漏），但**必须说出来**：
        # 否则"上限学自本次评估语料"会成为一个没人能证伪的默认假设。
        prof_corpus = load_profile(args.compact_profile).get("corpus_root", "")
        if prof_corpus and Path(prof_corpus).resolve() != Path(root).resolve():
            print(
                "⚠ cap 上限学自**另一个语料根**：\n"
                f"    profile: {prof_corpus}\n"
                f"    本次   : {root}\n"
                "  这不是错误（跨语料学到上限反而排除了自泄漏），"
                "但上线限的人必须知道它不是同分布的。"
            )

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
        f"  裁剪={args.compact}"
    )

    started = time.perf_counter()
    runs = run_all(
        tasks,
        tools,
        args.model,
        args.base_url,
        args.max_turns,
        compact_policy=args.compact,
        compact_caps=caps,
    )
    elapsed = time.perf_counter() - started

    bill = build_bill(
        runs,
        tasks,
        tools,
        args.model,
        compact_policy=args.compact,
        compact_caps=caps,
    )
    bill["reproducibility"] = build_reproducibility(
        tools,
        compact_policy=args.compact,
        caps=caps,
        profile_path=args.compact_profile,
        tools_source=tools_source,
    )

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

    print_report(bill, tools_arg, elapsed)
    print(f"\n落盘：{runs_path}")
    print(f"      {spans_path}  （{len(span_records)} 个 span）")
    print(f"      {bill_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
