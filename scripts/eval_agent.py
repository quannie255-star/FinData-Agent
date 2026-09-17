"""M12 agent 级评测：单 Agent vs 多智能体，同题同库对比。

评测设计（对齐 ROADMAP 验收门禁）：
- 同一份 fixture（data/eval_fixture.duckdb，确定性合成数据）
- 同一份 agent golden（eval/golden/agent.yaml，7 题含 2 道 deep 题）
- 两套架构各答一遍，五项 agent 级指标：答案正确率 / run_id 保留率 /
  平均工具轮数 / token 成本 / 端到端延迟
- 成本上升如实写进对比表——多智能体不是免费的，这是本阶段的诚实义务

用法：
    uv run python scripts/eval_agent.py --arch both            # 对比（默认）
    uv run python scripts/eval_agent.py --arch multi --out report.txt

退出码：0 全部通过；1 有 case 失败（--strict 时路由不符也算失败）；
2 LLM 未配置或仓库缺失。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# extract_numbers 为 tests/test_eval_agent.py 的导入面保留（re-export）
from findata.eval.agent_judge import (  # noqa: F401
    _RUN_ID_RE,
    extract_numbers,  # noqa: F401
    judge_answer,
    judge_run_ids,
)

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden" / "agent.yaml"
FIXTURE_DB = Path(__file__).resolve().parent.parent / "data" / "eval_fixture.duckdb"

# ─────────────────────────── 判分器 ───────────────────────────
# extract_numbers / judge_answer / judge_run_ids 已下沉到
# findata.eval.agent_judge（M12 评测与 M14 RL 奖励共用同一把尺子）。


# ─────────────────────────── 执行器 ───────────────────────────


def _answer_single(conn, question: str) -> dict[str, Any]:
    """单 Agent（对比基线，graph.py）。"""
    from findata.agent.graph import build_agent

    t0 = time.perf_counter()
    state = build_agent(conn).invoke(question)
    latency = time.perf_counter() - t0
    final = state["messages"][-1]
    return {
        "answer": getattr(final, "content", "") or "",
        "run_ids": state.get("run_ids", []),
        "tool_rounds": state.get("tool_rounds", 0),
        "usage": state.get("usage", {}),
        "queries": state.get("queries", []),
        "latency_s": latency,
        "mode": None,
    }


def _answer_multi(conn, question: str) -> dict[str, Any]:
    """多智能体（Supervisor 编排）。"""
    from findata.agent.supervisor import invoke_state

    t0 = time.perf_counter()
    state = invoke_state(conn, question)
    latency = time.perf_counter() - t0
    final = state["messages"][-1]
    return {
        "answer": getattr(final, "content", "") or "",
        "run_ids": state.get("run_ids", []),
        "tool_rounds": state.get("tool_rounds", 0),
        "usage": state.get("usage", {}),
        "usage_by_agent": state.get("usage_by_agent", {}),
        "queries": state.get("queries", []),
        "latency_s": latency,
        "mode": state.get("mode"),
        "verdict": state.get("verdict"),
    }


_ARCH_EXEC = {"single": _answer_single, "multi": _answer_multi}


def run_arch(arch: str, cases: list[dict], conn, strict: bool) -> dict[str, Any]:
    """跑一套架构的全部 case，返回该架构的汇总结果。

    除端到端指标外，产出**分步漏斗**（路由正确率 / 查询执行成功率 / 溯源
    通过率）——Anthropic 多智能体评测的核心教训是失败会逐环节复利放大
    （单 agent 0.7% vs 多智能体 14.5%），端到端正确率一个数字定位不了
    哪一环在漏，分步漏斗可以。
    """
    exec_case = _ARCH_EXEC[arch]
    rows: list[dict[str, Any]] = []
    for case in cases:
        expect = case["expect"]
        tol = float(case.get("tol", 1e-3))
        row: dict[str, Any] = {"name": case["name"], "question": case["question"]}
        try:
            out = exec_case(conn, case["question"])
        except Exception as exc:  # noqa: BLE001 — 单 case 失败不拖垮整场评测
            row.update(error=str(exc), correct=False, reasons=[f"执行失败：{exc}"])
            rows.append(row)
            continue
        ok, reasons = judge_answer(out["answer"], expect, tol)
        # 路由校验（仅 multi 有路由概念）：期望 direct 却走了 deep（或反之）
        route_ok = None
        if arch == "multi":
            route_ok = case.get("mode") in (None, out.get("mode"))
            if case.get("mode") and not route_ok:
                msg = f"路由不符：期望 {case['mode']} 实得 {out.get('mode')}"
                if strict:
                    ok = False
                    reasons.append(msg)
                else:
                    row["route_warn"] = msg
        queries = out.get("queries") or []
        n_attempted = len(queries)
        n_failed = sum(1 for q in queries if "error" in q)
        trace_ok = (not expect.get("want_run_id")) or bool(_RUN_ID_RE.search(out["answer"]))
        row.update(
            correct=ok,
            reasons=reasons,
            answer=out["answer"],
            tool_rounds=out["tool_rounds"],
            usage=out["usage"],
            usage_by_agent=out.get("usage_by_agent", {}),
            latency_s=out["latency_s"],
            run_id_kept=judge_run_ids(out["answer"], out["run_ids"]),
            mode=out.get("mode"),
            route_ok=route_ok,
            n_attempted=n_attempted,
            n_failed=n_failed,
            trace_ok=trace_ok,
        )
        rows.append(row)

    n = len(rows)
    usage = [r.get("usage", {}) for r in rows if "usage" in r]
    # 按 Agent 归因的成本（仅 multi 产出 usage_by_agent）
    agent_names = ("schema", "verifier", "finalize")
    agent_cost = {
        name: _mean(
            [
                r.get("usage_by_agent", {}).get(name, {}).get("total_tokens", 0)
                for r in rows
                if "usage_by_agent" in r
            ]
        )
        for name in agent_names
    }
    multi_rows = [r for r in rows if r.get("route_ok") is not None]
    return {
        "arch": arch,
        "rows": rows,
        "n": n,
        "n_correct": sum(1 for r in rows if r["correct"]),
        "run_id_kept": _mean([r.get("run_id_kept", 0.0) for r in rows]),
        "avg_tool_rounds": _mean([r.get("tool_rounds", 0) for r in rows]),
        "avg_latency_s": _mean([r.get("latency_s", 0.0) for r in rows]),
        "avg_total_tokens": _mean([u.get("total_tokens", 0) for u in usage]),
        "sum_total_tokens": sum(u.get("total_tokens", 0) for u in usage),
        "agent_cost": agent_cost,
        "route_ok": (
            sum(1 for r in multi_rows if r["route_ok"]) / len(multi_rows)
            if multi_rows
            else None
        ),
        "query_ok": _safe_ratio(
            sum(r.get("n_attempted", 0) - r.get("n_failed", 0) for r in rows),
            sum(r.get("n_attempted", 0) for r in rows),
        ),
        "trace_ok": _safe_ratio(sum(1 for r in rows if r.get("trace_ok")), n),
    }


def _safe_ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ─────────────────────────── 报告渲染 ───────────────────────────


def _fmt_pct(x: float, n: int = 1) -> str:
    return f"{x * 100:.{n}f}%"


def render_text(results: list[dict[str, Any]]) -> str:
    """渲染成 examples/ 归档格式的文本报告（对齐 eval-llm-triage-real.txt 风格）。"""
    arch_desc = {
        "single": "parse→tools→finalize 回环",
        "multi": "Supervisor + Schema/Query/Verifier/Triage",
    }
    lines = ["Findata 问数 Agent 评测 · 单 Agent vs 多智能体", "=" * 46]
    for r in results:
        acc = _fmt_pct(r["n_correct"] / r["n"]) if r["n"] else "n/a"
        lines += [
            "",
            f"架构：{r['arch']}（{arch_desc.get(r['arch'], r['arch'])}）",
            "-" * 46,
            f"答案正确率     {acc}  ({r['n_correct']}/{r['n']})",
            f"run_id 保留率  {_fmt_pct(r['run_id_kept'])}",
            f"平均工具轮数   {r['avg_tool_rounds']:.2f}",
            f"平均延迟       {r['avg_latency_s']:.2f}s",
            f"token 成本     平均 {r['avg_total_tokens']:.0f}/题，合计 {r['sum_total_tokens']}",
        ]
        # 分步漏斗：端到端正确率定位不了哪一环在漏，分步可以
        def _step(label: str, ratio: float | None) -> str:
            return f"  {label}   {('n/a' if ratio is None else _fmt_pct(ratio))}"

        lines += [
            "分步漏斗",
            _step("路由正确率", r["route_ok"]),
            _step("查询执行成功", r["query_ok"]),
            _step("溯源通过率", r["trace_ok"]),
        ]
        ac = r.get("agent_cost") or {}
        if any(v for v in ac.values()):
            lines.append(
                "成本归因       "
                + " / ".join(
                    f"{name} {ac.get(name, 0):.0f}" for name in ("schema", "verifier", "finalize")
                )
                + "（token/题，均值）"
            )
    if len(results) == 2:
        s, m = results[0], results[1]
        sc, mc = _fmt_pct(s["n_correct"] / s["n"]), _fmt_pct(m["n_correct"] / m["n"])
        delta_pct = (
            (m["avg_total_tokens"] / s["avg_total_tokens"] - 1) * 100
            if s["avg_total_tokens"]
            else 0
        )
        lines += [
            "",
            "单 → 多 增量",
            "-" * 46,
            f"答案正确率     {sc} → {mc}",
            f"run_id 保留率  {_fmt_pct(s['run_id_kept'])} → {_fmt_pct(m['run_id_kept'])}",
            f"平均工具轮数   {s['avg_tool_rounds']:.2f} → {m['avg_tool_rounds']:.2f}",
            f"平均延迟       {s['avg_latency_s']:.2f}s → {m['avg_latency_s']:.2f}s"
            f"（{m['avg_latency_s'] - s['avg_latency_s']:+.2f}s）",
            f"token/题       {s['avg_total_tokens']:.0f} → {m['avg_total_tokens']:.0f}"
            f"（{delta_pct:+.0f}%）",
        ]
    for r in results:
        bad = [row for row in r["rows"] if not row["correct"]]
        if bad:
            lines += ["", f"失败明细（{r['arch']}）："]
            for row in bad:
                lines.append(f"  ✗ {row['name']}: {'; '.join(row.get('reasons', []))}")
        warns = [row for row in r["rows"] if row.get("route_warn")]
        for row in warns:
            lines.append(f"  [warn] {row['name']}: {row['route_warn']}")
    return "\n".join(lines) + "\n"


# ─────────────────────────── 入口 ───────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="Findata Agent 级评测（单 vs 多智能体）")
    p.add_argument("--arch", choices=["single", "multi", "both"], default="both")
    p.add_argument("--db", default=None, help="数据仓库路径（默认合成 fixture）")
    p.add_argument("--out", default=None, help="报告输出路径（缺省打印 stdout）")
    p.add_argument(
        "--strict", action="store_true", help="路由不符也算失败（CI 门禁口径）"
    )
    args = p.parse_args()

    from findata.config import settings

    if not settings.llm_api_key:
        print("未配置 FINDATA_LLM_API_KEY，agent 级评测需要真实 LLM。", file=sys.stderr)
        return 2

    db = args.db or str(FIXTURE_DB)
    if not Path(db).exists():
        print(f"数据仓库不存在：{db}。先跑 scripts/build_eval_fixture.py。", file=sys.stderr)
        return 2

    cases = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]
    archs = ["single", "multi"] if args.arch == "both" else [args.arch]

    from findata.core.db import connect

    conn = connect(db)
    results = []
    try:
        for arch in archs:
            print(f"跑 {arch} ...", file=sys.stderr)
            results.append(run_arch(arch, cases, conn, args.strict))
    finally:
        conn.close()

    header = (
        f"生成时间: {datetime.now().isoformat(timespec='seconds')}  ·  "
        f"模型: {settings.llm_model}（temperature=0）  ·  数据: {Path(db).name}"
        "（合成 fixture，与 golden 同源）\n\n"
    )
    report = header + render_text(results)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"报告已写入 {args.out}", file=sys.stderr)
    print(report)

    bad = any(r["n_correct"] < r["n"] for r in results)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
