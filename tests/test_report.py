"""巡检流水线与报告渲染测试。

核心断言是第一条：干净数据上必须零告警。
一个数据质量工具如果在健康数据上乱叫，它所有的告警都会失去可信度。
"""

from __future__ import annotations

from datetime import date

import pytest

from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.eval.fixtures import build_baseline
from findata.report import (
    ROUTES,
    Snapshot,
    render_html,
    render_markdown,
    run_inspection,
)
from findata.report.inspect import Alert, _merge_alerts

SYNTH_SEED = 20240102


def _clean_snapshot() -> Snapshot:
    base = build_baseline()
    return Snapshot(
        stock_daily=base.stock_daily,
        valuation_daily=base.valuation_daily,
        universe=base.universe,
        corporate_event=base.corporate_event,
        ingest_run=base.stock_daily.iloc[0:0].copy(),
        calendar=base.calendar,
        asof=base.stock_daily["date"].max(),
    )


def test_clean_data_produces_no_alert():
    """干净语料：信号应全部被抑制，一条告警都不该有。"""
    result = run_inspection(_clean_snapshot())
    assert result.summary.n_findings > 0, "干净语料也应有信号，否则抑制能力无从体现"
    assert result.alerts == []
    assert result.summary.health_score == 100.0
    assert result.summary.grade() == "健康"


def test_faulty_data_produces_alerts_with_p0():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    assert result.summary.n_alerts > 0
    assert result.p0, "注入了 P0 级故障，却没有任何 P0 告警"
    assert result.summary.health_score < 100.0


def test_suppressed_signals_never_reach_alerts():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    assert result.suppressed, "合法事件应被识别并抑制"
    alert_keys = {a.diagnosis.finding_key for a in result.alerts}
    for f, d in result.suppressed:
        assert d.root_cause.is_benign or d.severity is Severity.OK
        assert f.key not in alert_keys, "被抑制的信号不该同时出现在告警里"


def test_alerts_are_routed_by_severity():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    for a in result.alerts:
        route, action = ROUTES[a.diagnosis.severity]
        assert a.route == route
        assert a.action == action
    for a in result.p0:
        assert a.route == "立即响应"


def test_alerts_sorted_by_severity_desc():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    order = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}
    ranks = [order[a.diagnosis.severity] for a in result.alerts]
    assert ranks == sorted(ranks)


def _alert(symbol: str, cause: RootCause, severity: Severity, window: str) -> Alert:
    f = Finding(
        probe="freshness",
        table="stock_daily",
        symbol=symbol,
        window=window,
        metric="stale_trading_days",
        value=3.0,
        threshold=2.0,
        severity_hint=severity,
    )
    d = Diagnosis(
        finding_key=f.key,
        root_cause=cause,
        severity=severity,
        confidence=0.9,
        explanation="测试用",
    )
    return Alert(finding=f, diagnosis=d, route="r", action="a")


def test_merge_alerts_collapses_same_symbol_and_cause():
    """同一标的同一根因只占一行，避免一次故障刷屏。"""
    merged = _merge_alerts(
        [
            _alert("600030", RootCause.UPSTREAM_STALE, Severity.P0, "2024-06-21..2024-06-28"),
            _alert("600030", RootCause.UPSTREAM_STALE, Severity.P1, "2024-06-24..2024-06-28"),
            _alert("600036", RootCause.UPSTREAM_STALE, Severity.P1, "2024-06-24..2024-06-28"),
        ]
    )
    assert len(merged) == 2
    head = next(a for a in merged if a.finding.symbol == "600030")
    assert head.merged == 2
    assert head.diagnosis.severity is Severity.P0, "合并应保留最严重的定级"
    assert head.windows.endswith("等 2 段")


def test_merge_alerts_keeps_distinct_causes_apart():
    """不同根因是不同故障，不能为了好看合并掉。"""
    merged = _merge_alerts(
        [
            _alert("600030", RootCause.UPSTREAM_STALE, Severity.P0, "2024-06-21..2024-06-28"),
            _alert("600030", RootCause.MISSING_ROWS, Severity.P1, "2024-06-24..2024-06-28"),
        ]
    )
    assert len(merged) == 2


def test_markdown_report_structure():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    md = render_markdown(result)
    assert "# 数据质量巡检报告" in md
    assert "## 待处理告警" in md
    assert "## 已抑制" in md
    assert f"{result.summary.asof}" in md
    assert "P0 阻断" in md


def test_html_report_escapes_symbols():
    """报告会被放进浏览器/工单系统渲染，符号必须转义。"""
    evil = "<script>alert(1)</script>"
    a = _alert(evil, RootCause.UPSTREAM_STALE, Severity.P0, "2024-06-21..2024-06-28")
    result = run_inspection(_clean_snapshot())
    result.alerts = [a]
    html = render_html(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_report_contains_summary_cards():
    result = run_inspection(Snapshot.from_synthetic(seed=SYNTH_SEED))
    html = render_html(result)
    assert "健康分" in html
    assert "降噪率" in html
    assert str(result.summary.health_score) in html


def test_snapshot_from_duckdb_tolerates_missing_tables(tmp_path):
    """元数据表缺失时巡检应降级而不是崩溃——巡检不该被元数据问题打挂。"""
    import duckdb

    con = duckdb.connect(str(tmp_path / "empty.duckdb"))
    con.execute("CREATE TABLE stock_daily (symbol VARCHAR, date DATE, close DOUBLE)")
    con.execute("INSERT INTO stock_daily VALUES ('600519', DATE '2024-01-02', 100.0)")
    snap = Snapshot.from_duckdb(con)
    assert snap.asof == date(2024, 1, 2)
    assert snap.universe.empty
    assert snap.corporate_event.empty
    con.close()


@pytest.mark.parametrize("seed", [20240102, 20240103, 20240104])
def test_inspection_is_deterministic(seed):
    a = run_inspection(Snapshot.from_synthetic(seed=seed))
    b = run_inspection(Snapshot.from_synthetic(seed=seed))
    assert a.summary.health_score == b.summary.health_score
    assert a.summary.n_alerts == b.summary.n_alerts
