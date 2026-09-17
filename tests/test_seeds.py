"""停牌历史种子（R1.0 知识层）的三个契约：可入库、幂等、能抑制真实缺口。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from findata.core.db import connect
from findata.domains.finance.seeds import SUSPENSION_SEEDS, apply_suspension_seeds, seeds_frame
from findata.dq.calendar import TradingCalendar
from findata.dq.models import Finding, Severity
from findata.dq.probes import ProbeContext
from findata.dq.triage import BaselineTriage

# 4 笔种子的缺口窗口（与 2026-09-15 真实告警一一对应）
_GAP_WINDOWS = [
    ("600030", date(2022, 1, 19), date(2022, 1, 26)),
    ("600900", date(2022, 10, 26), date(2022, 10, 26)),
    ("601088", date(2025, 8, 4), date(2025, 8, 15)),
    ("688981", date(2025, 9, 1), date(2025, 9, 8)),
]

_REVIEW_DOC = Path(__file__).parent.parent / "docs" / "reviews" / "2026-09-15-attribution.md"


def _weekday_calendar(start: date, end: date) -> TradingCalendar:
    """用工作日近似交易日历：种子窗口内不含未知节假日，覆盖率判定不受影响。

    不能用 TradingCalendar.default()——它的硬编码节假日只覆盖 2024-25，
    2022 年的窗口会退化成纯工作日推断；直接全量工作日保证窗口内
    trading_days 与缺失日一致，coverage 恒为 1.0。
    """
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return TradingCalendar.from_trading_days(days)


def _gap_context(calendar: TradingCalendar) -> ProbeContext:
    """构造 4 个缺口窗口全部缺失、其余数据正常的探针上下文。"""
    rows = []
    for sym, start, end in _GAP_WINDOWS:
        d = date(2022, 1, 4)
        while d <= end + timedelta(days=5):
            if calendar.trading_days(d, d) and not (start <= d <= end):
                rows.append({"symbol": sym, "date": d, "volume": 1_000_000.0})
            d += timedelta(days=1)
    return ProbeContext.of(
        stock_daily=pd.DataFrame(rows),
        valuation_daily=pd.DataFrame(),
        universe=pd.DataFrame(),
        corporate_event=seeds_frame(),
        calendar=calendar,
        asof=max(end for _, _, end in _GAP_WINDOWS) + timedelta(days=5),
    )


def test_seeds_apply_and_idempotent(tmp_path):
    with connect(str(tmp_path / "w.duckdb")) as conn:
        n1 = apply_suspension_seeds(conn)
        assert n1 == len(SUSPENSION_SEEDS) == 4
        rows = conn.execute(
            "SELECT symbol, date, end_date, detail FROM corporate_event "
            "WHERE kind = 'suspension' ORDER BY symbol"
        ).fetchall()
        assert len(rows) == 4
        by_sym = {r[0]: r for r in rows}
        # end_date 必须覆盖缺口最后一个交易日，否则抑制覆盖率不足
        assert by_sym["600030"][2] == date(2022, 1, 26)
        assert by_sym["688981"][2] == date(2025, 9, 8)
        # 公告链接入 detail：证据链的最底层事实
        for r in rows:
            assert "http" in r[3]
        n2 = apply_suspension_seeds(conn)
        assert n2 == 0
        after = conn.execute("SELECT count(*) FROM corporate_event").fetchone()[0]
        assert after == 4


def test_seeds_suppress_all_four_real_gaps():
    """复盘的验收命题：种子落地后，4 条 missing_rows 归因为停牌并抑制。"""
    calendar = _weekday_calendar(date(2022, 1, 1), date(2025, 9, 30))
    ctx = _gap_context(calendar)
    findings = [
        Finding(
            probe="completeness",
            table="stock_daily",
            symbol=sym,
            window=f"{start}..{end}",
            metric="missing_trading_days",
            value=float(len(calendar.trading_days(start, end))),
            threshold=1.0,
            severity_hint=Severity.P1,
        )
        for sym, start, end in _GAP_WINDOWS
    ]
    diagnoses = BaselineTriage().diagnose_all(findings, ctx)
    assert len(diagnoses) == 4
    for d in diagnoses:
        assert d.root_cause.value == "benign_suspension"
        assert d.suppressed


def test_every_seed_source_is_documented_in_review():
    """种子的每条来源链接必须能在归因复盘中找到——防止种子凭记忆杜撰。"""
    text = _REVIEW_DOC.read_text(encoding="utf-8")
    for seed in SUSPENSION_SEEDS:
        assert seed["kind"] == "suspension"
        assert seed["symbol"] in text
        for url in re.findall(r"https?://\S+?(?= ; |$)", seed["detail"]):
            assert url.rstrip(" ;") in text, f"种子来源未在复盘文档中登记: {url}"
