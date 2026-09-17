"""findata 服务层：把巡检/评测/看板的高层调用封出来，给 API 和 MCP 共用。

API 不能直接调用 repo 内部函数（会让层边界变糊），所以这里集中做
"装 Snapshot → 跑流水线 → 返结果"这件事。这样后面 MCP Server 也能直接复用，
不会出现两份 inspect 实现漂移。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from findata.config import settings
from findata.dq.badges import BadgeLevel
from findata.dq.triage import BaselineTriage
from findata.eval.fixtures import SyntheticConfig
from findata.eval.runner import EvalResult, run_evaluation
from findata.observability import get_tracer, setup_tracer
from findata.report.dashboard import build_dashboard, render_dashboard
from findata.report.inspect import InspectionResult, Snapshot, run_inspection
from findata.report.render import render_html, render_markdown

Source = Literal["synthetic", "duckdb"]
TriageKind = Literal["baseline", "llm", "both"]

# service 层一加载就把 TracerProvider 装上。幂等,重复 import 不重置。
setup_tracer()
_tracer = get_tracer()


def inspect(
    source: Source,
    asof: date | None = None,
    seed: int = 20240102,
    db_path: str | None = None,
) -> InspectionResult:
    """跑巡检：装 Snapshot → 回放到 asof → 跑流水线。"""
    with _tracer.start_as_current_span("service.inspect") as span:
        span.set_attribute("findata.source", source)
        span.set_attribute("findata.asof", asof.isoformat() if asof else "latest")
        span.set_attribute("findata.seed", seed)
        snap = _load_snapshot(source, asof, seed, db_path)
        target = snap.as_of(asof) if asof is not None else snap
        result = run_inspection(target)
        if source == "duckdb":
            _persist_signals(result, db_path)
        # 把核心健康指标塞进 span attribute,便于 trace ↔ alert 双向跳转
        s = result.summary
        span.set_attribute("findata.health_score", s.health_score)
        span.set_attribute("findata.n_findings", s.n_findings)
        span.set_attribute("findata.n_alerts", s.n_alerts)
        span.set_attribute("findata.n_suppressed", s.n_suppressed)
        span.set_attribute("findata.grade", s.grade())
        return result


def _persist_signals(result: InspectionResult, db_path: str | None) -> None:
    """巡检产物落 dq_signal 表（M12：给 VerifierAgent 的运行时背书供数）。

    best-effort：写历史失败只记日志，绝不打挂巡检本身——巡检的主职责是
    出报告，历史表是增强，不能本末倒置。
    synthetic 源不落库（调用方保证）：合成故障写进历史会污染真实信号。
    """
    import logging

    from findata.core.db import connect
    from findata.dq.history import record_signals

    pairs = [(a.finding, a.diagnosis) for a in result.alerts]
    pairs += list(result.suppressed)
    if not pairs:
        return
    try:
        conn = connect(str(db_path or settings.db_path))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("dq_signal 落库失败（打开仓库）：%s", exc)
        return
    try:
        record_signals(conn, pairs, result.asof)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("dq_signal 落库失败：%s", exc)
    finally:
        conn.close()


def render_report(
    source: Source,
    asof: date | None,
    fmt: Literal["md", "html"],
    seed: int = 20240102,
    db_path: str | None = None,
) -> tuple[str, str]:
    """渲染单日报告。返回 (content, media_type)。"""
    with _tracer.start_as_current_span("service.render_report") as span:
        span.set_attribute("findata.source", source)
        span.set_attribute("findata.format", fmt)
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
    with _tracer.start_as_current_span("service.build_dashboard") as span:
        span.set_attribute("findata.source", source)
        span.set_attribute("findata.days", days)
        span.set_attribute("findata.with_eval", with_eval)
        snap = _load_snapshot(source, asof, seed, db_path)
        eval_report = run_evaluation(SyntheticConfig()).report if with_eval else None
        data = build_dashboard(snap, n_days=days, eval_report=eval_report)
        html = render_dashboard(data)
        # html bytes 也是可观察指标:同样参数下若它大幅增长,说明报告内容变了
        span.set_attribute("findata.html_bytes", len(html.encode("utf-8")))
        return html, data.current


def evaluate(
    seed: int = 20240102,
    triage: TriageKind = "baseline",
) -> EvalResult:
    """跑评测。triage=both 时同 batch 跑两份归因并算 κ。

    LLM 后端选择（结果里通过 result.llm_backend 如实暴露）：
    - 配置了 FINDATA_LLM_API_KEY → OpenAICompatClient 调真实模型；
    - 未配置 → MockLLMClient 确定性桩（全静默，归因退化为纯规则），
      保证无 key 环境下对照链路依然可跑，但 κ 的含义是"规则 vs 静默桩"，
      不是"规则 vs 真实大模型"——演示与解读时必须区分。
    """
    with _tracer.start_as_current_span("service.evaluate") as span:
        span.set_attribute("findata.fault_seed", seed)
        span.set_attribute("findata.triage_kind", triage)
        if triage == "baseline":
            return run_evaluation(fault_seed=seed)
        from findata.agent.llm import MockLLMClient, OpenAICompatClient
        from findata.agent.llm_triage import LLMTriage

        real_client = OpenAICompatClient()
        if real_client.available:
            client, backend = real_client, "openai_compat"
        else:
            client, backend = MockLLMClient(), "mock"
        span.set_attribute("findata.llm_backend", backend)
        llm_triage = LLMTriage(client=client)
        if triage == "llm":
            result = run_evaluation(fault_seed=seed, triage=llm_triage)
        else:
            # both: baseline + llm 双归因；κ 记在子 span 上便于 trace 检索
            with _tracer.start_as_current_span("service.evaluate.both_triage") as sub:
                result = run_evaluation(
                    fault_seed=seed,
                    triage=BaselineTriage(),
                    second_triage=llm_triage,
                )
                if result.triage_agreement:
                    k = result.triage_agreement.get("kappa_root_cause")
                    if k is not None:
                        sub.set_attribute("findata.llm_kappa_root_cause", float(k))
        result.llm_backend = backend
        if result.triage_agreement:
            k = result.triage_agreement.get("kappa_root_cause")
            if k is not None:
                span.set_attribute("findata.llm_kappa_root_cause", float(k))
        return result


# ─────────────────────────── 私有 ───────────────────────────


def _load_snapshot(
    source: Source, asof: date | None, seed: int, db_path: str | None
) -> Snapshot:
    if source == "synthetic":
        return Snapshot.from_synthetic(seed=seed)
    # duckdb 源：默认连生产仓库。仓库缺失必须显式报错——
    # 悄悄巡检一个空库然后给出 100 分，是"可信"项目最不该有的失败模式。
    path = Path(str(db_path or settings.db_path))
    if not path.exists():
        raise FileNotFoundError(
            f"duckdb 仓库不存在：{path}。请先跑 scripts/ingest_finance.py 采集，"
            "或改用 source='synthetic'。"
        )
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return Snapshot.from_duckdb(conn, asof=asof)
    finally:
        conn.close()


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
        "badges": badges_to_json(result),
    }


def trust_check(
    table: str,
    metric: str,
    source: Source = "duckdb",
    asof: date | None = None,
    seed: int = 20240102,
    db_path: str | None = None,
) -> dict[str, Any]:
    """查询单个 (table, metric) 的可信徽章——外部 Agent 引用数字前的标准动作。

    这是「增强模块」定位（v2.1）的最小接入面：任何外部 Agent 在引用一个
    数字前调用本接口，拿到四档徽章 + 证据链，决定「直接引用 / 仅借鉴 /
    拒绝引用」。找不到该指标时抛 ValueError（MCP/API 层负责转成各自
    的错误形态），错误信息带全部可用指标键，调用方可以自愈。
    """
    result = inspect(source, asof, seed, db_path)
    badge = result.badges.of(table, metric)
    if badge is None:
        known = ", ".join(b.metric_key for b in result.badges.badges)
        raise ValueError(f"未知指标 {table}.{metric}。可用指标：{known}")
    return {
        "asof": result.asof.isoformat(),
        "source": source,
        "table": table,
        "metric": metric,
        "badge": badge.level.value,
        "mark": badge.mark,
        "usable": badge.level in (BadgeLevel.VERIFIED, BadgeLevel.BASELINE),
        "summary": badge.summary,
        "evidence": [[src, text] for src, text in badge.evidence],
        "health_score": result.summary.health_score,
        "grade": result.summary.grade(),
    }


def trust_board(
    source: Source = "duckdb",
    asof: date | None = None,
    seed: int = 20240102,
    db_path: str | None = None,
) -> dict[str, Any]:
    """整板徽章（逐指标可信度）。外部 Agent 会话开始时拉一次，批量引用不再逐个查。"""
    result = inspect(source, asof, seed, db_path)
    out = badges_to_json(result)
    out["source"] = source
    return out


def badges_to_json(result: InspectionResult) -> dict[str, Any]:
    """徽章板序列化：四档计数 + 逐列徽章（含证据链）。"""
    board = result.badges
    return {
        "asof": result.asof.isoformat(),
        "health_score": result.summary.health_score,
        "grade": result.summary.grade(),
        "counts": {
            "verified": len(board.by_level(BadgeLevel.VERIFIED)),
            "baseline": len(board.by_level(BadgeLevel.BASELINE)),
            "caution": len(board.by_level(BadgeLevel.CAUTION)),
            "unusable": len(board.by_level(BadgeLevel.UNUSABLE)),
        },
        "badges": [
            {
                "table": b.table,
                "metric": b.column,
                "key": b.metric_key,
                "badge": b.level.value,
                "mark": b.mark,
                "usable": b.level in (BadgeLevel.VERIFIED, BadgeLevel.BASELINE),
                "summary": b.summary,
                "evidence": [[src, text] for src, text in b.evidence],
            }
            for b in board.badges
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
    out["llm_backend"] = result.llm_backend
    return out
