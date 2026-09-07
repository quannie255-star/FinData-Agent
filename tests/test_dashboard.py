"""看板测试。

最关键的断言是回放：站在历史上某天做巡检，绝不能看到那天之后的数据。
否则趋势图和故障复盘都是假的。
"""

from __future__ import annotations

import pytest

from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.eval.runner import run_evaluation
from findata.report import Snapshot, build_dashboard, render_dashboard


def test_as_of_hides_future_data():
    snap = Snapshot.from_synthetic(seed=20240102)
    all_days = sorted(set(snap.stock_daily["date"]))
    mid = all_days[len(all_days) // 2]

    past = snap.as_of(mid)
    assert past.asof == mid
    assert past.stock_daily["date"].max() <= mid
    assert len(past.stock_daily) < len(snap.stock_daily)
    # 未来的事件也不能被看到，否则停牌归因会用到尚未发生的信息
    if not past.corporate_event.empty:
        assert past.corporate_event["date"].max() <= mid


def test_as_of_earlier_day_has_fewer_rows():
    snap = Snapshot.from_synthetic(seed=20240102)
    days = sorted(set(snap.stock_daily["date"]))
    early = snap.as_of(days[5])
    late = snap.as_of(days[-1])
    assert len(early.stock_daily) < len(late.stock_daily)


def test_build_dashboard_produces_trend():
    snap = Snapshot.from_synthetic(seed=20240102)
    data = build_dashboard(snap, n_days=6)
    assert len(data.trend) == 6
    assert [p.asof for p in data.trend] == sorted(p.asof for p in data.trend)
    for p in data.trend:
        assert 0.0 <= p.health_score <= 100.0
        assert p.n_alerts >= 0
    assert data.current.summary.asof == snap.asof


def test_build_dashboard_handles_short_history():
    """请求的回溯天数超过数据历史时，不应报错或产生空趋势。"""
    snap = Snapshot.from_synthetic(seed=20240102)
    data = build_dashboard(snap, n_days=100_000)
    assert len(data.trend) > 0


def test_dashboard_html_has_core_sections():
    snap = Snapshot.from_synthetic(seed=20240102)
    html = render_dashboard(build_dashboard(snap, n_days=5))
    assert "数据质量看板" in html
    assert "健康分趋势" in html
    assert "根因分布" in html
    assert "系统自评" in html
    assert "<svg" in html and "polyline" in html
    # 三个 tab 的面板都要存在，切换脚本才会生效
    for pid in ("p-alert", "p-sup", "p-eval"):
        assert f'id="{pid}"' in html


def test_dashboard_shows_eval_gain_when_provided():
    snap = Snapshot.from_synthetic(seed=20240102)
    report = run_evaluation(fault_seed=20240102).report
    html = render_dashboard(build_dashboard(snap, n_days=3, eval_report=report))
    assert "归因层带来的精确率增量" in html
    assert "系统自评" in html


def test_dashboard_escapes_symbols():
    evil = "<img src=x onerror=alert(1)>"
    snap = Snapshot.from_synthetic(seed=20240102)
    data = build_dashboard(snap, n_days=2)
    f = Finding(
        probe="freshness",
        table="stock_daily",
        symbol=evil,
        window="2024-06-21..2024-06-28",
        metric="stale_trading_days",
        value=3.0,
        threshold=2.0,
        severity_hint=Severity.P0,
    )
    d = Diagnosis(
        finding_key=f.key,
        root_cause=RootCause.UPSTREAM_STALE,
        severity=Severity.P0,
        confidence=0.9,
        explanation="测试",
    )
    data.current.alerts = [
        type(data.current.alerts[0])(
            finding=f, diagnosis=d, route="立即响应", action="阻断"
        )
    ]
    html = render_dashboard(data)
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_dashboard_renders_without_alerts():
    """无任何告警时（干净数据）看板要能正常渲染，而不是生成一张空壳。"""
    from findata.eval.fixtures import build_baseline

    base = build_baseline()
    snap = Snapshot(
        stock_daily=base.stock_daily,
        valuation_daily=base.valuation_daily,
        universe=base.universe,
        corporate_event=base.corporate_event,
        ingest_run=base.stock_daily.iloc[0:0].copy(),
        calendar=base.calendar,
        asof=base.stock_daily["date"].max(),
    )
    data = build_dashboard(snap, n_days=3)
    html = render_dashboard(data)
    assert data.current.summary.n_alerts == 0
    assert "本轮无待处理告警" in html


@pytest.mark.parametrize("n_days", [3, 8])
def test_trend_is_deterministic(n_days):
    snap = Snapshot.from_synthetic(seed=20240102)
    a = build_dashboard(snap, n_days=n_days)
    b = build_dashboard(snap, n_days=n_days)
    assert [p.health_score for p in a.trend] == [p.health_score for p in b.trend]
