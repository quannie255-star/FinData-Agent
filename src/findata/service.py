"""findata 服务层：把巡检/评测/看板的高层调用封出来，给 API 和 MCP 共用。

API 不能直接调用 repo 内部函数（会让层边界变糊），所以这里集中做
"装 Snapshot → 跑流水线 → 返结果"这件事。这样后面 MCP Server 也能直接复用，
不会出现两份 inspect 实现漂移。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

import duckdb

from findata.dq.triage import BaselineTriage
from findata.eval.fixtures import SyntheticConfig
from findata.eval.runner import EvalResult, run_evaluation
from findata.report.dashboard import build_dashboard, render_dashboard
from findata.report.inspect import InspectionResult, Snapshot, run_inspection
from findata.report.render import render_html, render_markdown

Source = Literal["synthetic", "duckdb"]
TriageKind = Literal["baseline", "llm", "both"]


def inspect(
    source: Source,
    asof: date | None = None,
    seed: int = 20240102,
    db_path: str | None = None,
) -> InspectionResult:
    """跑巡检：装 Snapshot → 回放到 asof → 跑流水线。"""
    snap = _load_snapshot(source, asof, seed, db_path)
    target = snap.as_of(asof) if asof is not None else snap
    return run_inspection(target)


def render_report(
    source: Source,
    asof: date | None,
    fmt: Literal["md", "html"],
    seed: int = 20240102,
    db_path: str | None = None,
) -> tuple[str, str]:
    """渲染单日报告。返回 (content, media_type)。"""
    result = inspect(source, asof, seed, db_path)
    if fmt == "md":
        body = render_markdown(result, header_extra=_header(result, source))
        return body, "text/markdown; charset=utf-8"
    return render_html(result), "text/html; charset=utf-8"


def build_dashboard_html(
    source: Source,
    asof: date | None,
    days: int = 20,
    with_eval: bool = False,
    seed: int = 20240102,
    db_path: str | None = None,
) -> tuple[str, InspectionResult]:
    """组装看板。返回 (html, current_result) —— 后者给 API 当 JSON 摘要用。"""
    snap = _load_snapshot(source, asof, seed, db_path)
    eval_report = run_evaluation(SyntheticConfig()).report if with_eval else None
    data = build_dashboard(snap, n_days=days, eval_report=eval_report)
    return render_dashboard(data), data.current


def evaluate(
    seed: int = 20240102,
    triage: TriageKind = "baseline",
) -> EvalResult:
    """跑评测。triage=both 时同 batch 跑两份归因并算 κ。

    LLM 客户端缺省用项目内的 MockLLMClient，使外部 Agent 接入无需配置
    OPENAI_API_KEY 也能跑通对照；接入真实 LLM 时通过环境变量即可切换。
    """
    if triage == "baseline":
        return run_evaluation(fault_seed=seed)
    from findata.agent.llm import MockLLMClient
    from findata.agent.llm_triage import LLMTriage

    mock = MockLLMClient()
    llm_triage = LLMTriage(client=mock)
    if triage == "llm":
        return run_evaluation(fault_seed=seed, triage=llm_triage)
    # both: 同 batch 双归因，跑 κ
    return run_evaluation(
        fault_seed=seed,
        triage=BaselineTriage(),
        second_triage=llm_triage,
    )


# ─────────────────────────── 私有 ───────────────────────────


def _load_snapshot(
    source: Source, asof: date | None, seed: int, db_path: str | None
) -> Snapshot:
    if source == "synthetic":
        snap = Snapshot.from_synthetic(seed=seed)
    else:
        path = db_path or ":memory:"
        conn = duckdb.connect(path)
        snap = Snapshot.from_duckdb(conn, asof=asof)
    return snap


def _header(result: InspectionResult, source: Source) -> str:
    """Markdown 报告的摘要块。复用而非重写：dashboard 已经在画它了。"""
    s = result.summary
    return (
        f"\n**观察日**: {s.asof}  ·  **数据源**: {source}  ·  "
        f"**健康分**: {s.health_score} ({s.grade()})  ·  "
        f"**告警**: {s.n_alerts}  ·  **抑制**: {s.n_suppressed}\n\n"
    )


def result_to_json(result: InspectionResult) -> dict[str, Any]:
    """把 InspectionResult 序列化成 JSON 友好结构（dataclass / Enum → 原生类型）。"""
    return {
        "asof": result.asof.isoformat(),
        "summary": _summary_to_json(result.summary),
        "alerts": [
            {
                "symbol": str(a.finding.symbol),
                "table": a.finding.table,
                "window": a.windows,
                "severity": a.diagnosis.severity.value,
                "root_cause": a.diagnosis.root_cause.value,
                "explanation": a.diagnosis.explanation,
                "value": a.finding.value,
                "threshold": a.finding.threshold,
                "route": a.route,
                "action": a.action,
                "merged": a.merged,
            }
            for a in result.alerts
        ],
        "suppressed": [
            {
                "symbol": str(f.symbol),
                "window": f.window,
                "root_cause": d.root_cause.value,
                "explanation": d.explanation,
            }
            for f, d in result.suppressed
        ],
    }


def _summary_to_json(s) -> dict[str, Any]:
    return {
        "asof": s.asof.isoformat(),
        "n_symbols": s.n_symbols,
        "n_rows": s.n_rows,
        "n_findings": s.n_findings,
        "n_alerts": s.n_alerts,
        "n_suppressed": s.n_suppressed,
        "by_severity": dict(s.by_severity),
        "by_root_cause": dict(s.by_root_cause),
        "health_score": s.health_score,
        "noise_reduction": s.noise_reduction,
        "grade": s.grade(),
    }


def eval_to_json(result: EvalResult) -> dict[str, Any]:
    """评测结果序列化。EvalReport.as_rows() 已经是 (k, v) 元组。"""
    out: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": {"rows": result.report.as_rows()},
        "n_findings": len(result.findings),
        "n_injected_faults": len(result.faults),
        "n_injected_benigns": len(result.benigns),
    }
    if result.triage_agreement:
        out["triage_agreement"] = result.triage_agreement
    return out
