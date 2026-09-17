"""逐指标徽章层（R1.2）测试：四档判定、列映射、健康分口径、报告渲染。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from findata.domains.finance.seeds import seeds_frame
from findata.dq.badges import BadgeLevel, build_badges
from findata.dq.calendar import TradingCalendar
from findata.dq.models import Finding, Severity
from findata.report import Snapshot, render_html, render_markdown, run_inspection
from tests.test_report import _clean_snapshot


def _finding(**kw) -> Finding:
    defaults = dict(
        probe="validity",
        table="stock_daily",
        symbol="600519",
        window="2024-03-01..2024-03-01",
        metric="pct_chg_out_of_limit",
        value=1.0,
        threshold=1.0,
        severity_hint=Severity.P0,
    )
    return Finding(**{**defaults, **kw})


def test_column_mapping_covers_all_probes():
    """finding → 列的映射：行级信号覆盖全列，列级信号精确落列。"""
    from findata.dq.badges import _finding_columns

    all_cols = ("open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover")
    row_level = dict(probe="completeness", metric="missing_trading_days")
    assert _finding_columns(_finding(**row_level)) == all_cols
    assert _finding_columns(
        _finding(probe="uniqueness", metric="duplicate_keys")
    ) == all_cols
    assert _finding_columns(_finding(metric="pct_chg_out_of_limit")) == ("pct_chg",)
    assert _finding_columns(_finding(metric="non_positive_volume")) == ("volume",)
    assert _finding_columns(_finding(metric="high_lt_low")) == ("high", "low")
    assert _finding_columns(
        _finding(probe="null_sweep", metric="null_rate::close")
    ) == ("close",)
    assert _finding_columns(
        _finding(probe="drift", metric="volume_recent_over_history")
    ) == ("volume",)
    assert _finding_columns(
        _finding(probe="drift", table="valuation_daily", metric="total_mv_level_shift")
    ) == ("total_mv",)


def test_clean_corpus_all_pass_with_score_100():
    result = run_inspection(_clean_snapshot())
    board = result.badges
    assert len(board.badges) == 12  # stock_daily 8 列 + valuation 4 列
    assert board.health_score == 100.0
    levels = {b.level for b in board.badges}
    assert levels <= {BadgeLevel.BASELINE, BadgeLevel.VERIFIED}
    # 干净语料上被抑制的合法事件应产生「✓ 已核验」而不是全部基线
    assert board.by_level(BadgeLevel.VERIFIED), "合成基线自带合法事件，应有已核验徽章"


def test_faulty_corpus_drops_score_and_flags_columns():
    result = run_inspection(Snapshot.from_synthetic(seed=20240102))
    board = result.badges
    assert result.summary.health_score == board.health_score
    assert board.health_score < 100.0
    flagged = board.by_level(BadgeLevel.CAUTION) + board.by_level(BadgeLevel.UNUSABLE)
    assert flagged, "注入故障后必须有列被打了 ⚠️/✗"


def test_suppressed_suspension_verifies_all_stock_columns():
    """R1.2 核心命题：停牌被知识层归因后，整表列拿到「✓ 已核验」。"""
    calendar_days = []
    d = date(2025, 6, 2)
    while d <= date(2025, 9, 30):
        if d.weekday() < 5:
            calendar_days.append(d)
        d += timedelta(days=1)
    gap = {date(2025, 9, x) for x in (1, 2, 4, 5, 8)}
    rows = [
        {
            "symbol": "688981",
            "date": d,
            "open": 50.0,
            "high": 51.0,
            "low": 49.0,
            "close": 50.0,
            "volume": 1_000_000.0,
            "amount": 5.0e7,
            "pct_chg": 0.5,
            "turnover": 1.0,
        }
        for d in calendar_days
        if d not in gap
    ]
    snap = Snapshot(
        stock_daily=pd.DataFrame(rows),
        valuation_daily=pd.DataFrame(),
        universe=pd.DataFrame(
            {
                "symbol": ["688981"],
                "name": ["中芯国际"],
                "industry": ["半导体"],
                "listed_board": ["star"],
                "list_date": [date(2020, 7, 16)],
            }
        ),
        corporate_event=seeds_frame(),
        ingest_run=pd.DataFrame(),
        calendar=TradingCalendar.from_trading_days(calendar_days),
        asof=date(2025, 9, 30),
    )
    result = run_inspection(snap)
    assert result.alerts == [], "停牌缺口应被抑制"
    board = result.badges
    stock = [b for b in board.badges if b.table == "stock_daily"]
    assert len(stock) == 8
    for b in stock:
        assert b.level is BadgeLevel.VERIFIED, "已核验停牌应给全列 ✓ 已核验"
        assert b.evidence, "已核验徽章必须带证据条目"
    assert board.health_score == 100.0


def test_unobserved_table_gets_no_badge():
    """空表不发徽章：探针没跑过任何东西，✓ 是无依据声明。"""
    board = build_badges([], [], observed_tables={"stock_daily": True, "valuation_daily": False})
    assert [b for b in board.badges if b.table == "valuation_daily"] == []
    assert len(board.badges) == 8


def test_p0_fault_makes_column_unusable():
    from findata.dq.models import Diagnosis, RootCause

    f = _finding()
    d = Diagnosis(
        finding_key=f.key,
        root_cause=RootCause.DUPLICATE_WRITE,
        severity=Severity.P0,
        confidence=0.95,
        explanation="主键重复",
    )
    board = build_badges([f], [d], observed_tables={"stock_daily": True, "valuation_daily": True})
    badge = board.of("stock_daily", "pct_chg")
    assert badge is not None and badge.level is BadgeLevel.UNUSABLE
    assert board.health_score == round(100.0 * 11 / 12, 1)


def test_badges_rendered_in_report_with_evidence():
    result = run_inspection(Snapshot.from_synthetic(seed=20240102))
    md = render_markdown(result)
    html = render_html(result)
    assert "指标可信度" in md
    assert "证据链" in md
    assert "指标可信度" in html
    assert "逐数字徽章" in html
    # 健康分口径：报告的健康分等于徽章板聚合值
    assert result.summary.health_score == result.badges.health_score


def test_badge_chip_html_is_embeddable_and_escapes():
    """可嵌入徽章芯片：内联样式、无类名依赖、证据摘要进 title 且转义。"""
    from findata.dq.badges import badge_chip_html

    result = run_inspection(_clean_snapshot())
    badge = result.badges.of("stock_daily", "close")
    chip = badge_chip_html(badge)
    assert chip.startswith("<span")
    assert badge.mark in chip
    assert "title=" in chip and "class=" not in chip  # 不依赖宿主样式表
    xss = badge_chip_html(badge.__class__(badge.table, badge.column, badge.level,
                                          '"><script>alert(1)</script>', badge.evidence))
    assert "<script>" not in xss  # summary 注入必须被转义
