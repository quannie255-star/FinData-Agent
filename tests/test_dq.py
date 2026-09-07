"""数据质量探针与归因的单测。

重点不是"能不能跑通"，而是**形态相同、结论相反**的对照场景：
同样是数据缺失，停牌该抑制、漏采该告警；
同样是涨跌幅超限，除权该抑制、解析错误该告警。

这几组对照就是本项目全部立论的基础，任何一条挂掉都意味着设计失效。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from findata.dq.calendar import TradingCalendar
from findata.dq.models import RootCause, Severity
from findata.dq.probes import ProbeContext, run_probes
from findata.dq.triage import BaselineTriage
from findata.eval.fixtures import SyntheticConfig, build_baseline


def _context(
    baseline,
    daily: pd.DataFrame | None = None,
    val: pd.DataFrame | None = None,
) -> ProbeContext:
    return ProbeContext.of(
        stock_daily=daily if daily is not None else baseline.stock_daily,
        valuation_daily=val if val is not None else baseline.valuation_daily,
        universe=baseline.universe,
        corporate_event=baseline.corporate_event,
        calendar=baseline.calendar,
    )


def _diagnose(baseline, daily=None, val=None):
    ctx = _context(baseline, daily, val)
    return BaselineTriage().diagnose_all(run_probes(ctx), ctx)


def _clean_config() -> SyntheticConfig:
    """不含任何合法事件的干净配置，便于单独验证故障路径。"""
    return SyntheticConfig(n_suspension=0, n_ex_rights=0, n_new_listing=0)


def test_calendar_ignores_weekend():
    """周五收盘到周一，隔 3 个自然日但 0 个交易日，不能算停更。"""
    cal = TradingCalendar.default()
    friday = date(2024, 3, 1)
    monday = date(2024, 3, 4)
    assert friday.weekday() == 4 and monday.weekday() == 0
    assert cal.staleness(friday, monday) == 0


def test_calendar_accepts_pandas_timestamp():
    """真实仓库路径下日期会混着 `date` 和 `pd.Timestamp`，不能一比大小就炸。

    合成语料类型单一，CI 跑不出来；只有 `--source warehouse`（DuckDB → pandas）
    才会在 `cur <= end` 抛 "Cannot compare Timestamp with datetime.date"。
    """
    cal = TradingCalendar.default()
    assert cal.trading_days(pd.Timestamp("2024-03-01"), date(2024, 3, 4)) == [
        date(2024, 3, 1),
        date(2024, 3, 4),
    ]
    assert cal.is_trading_day(pd.Timestamp("2024-03-02")) is False  # 周六
    assert cal.prev_trading_day(pd.Timestamp("2024-03-04")) == date(2024, 3, 1)


def test_suspension_is_suppressed():
    """停牌造成的缺失必须被识别为合法，而不是报成采集故障。"""
    baseline = build_baseline(SyntheticConfig(n_suspension=2, n_ex_rights=0, n_new_listing=0))
    diags = _diagnose(baseline)
    hits = [d for d in diags if d.root_cause is RootCause.BENIGN_SUSPENSION]
    assert hits, "停牌造成的缺失应触发信号并被识别"
    assert all(d.suppressed and d.severity is Severity.OK for d in hits)


def test_missing_rows_is_alerted():
    """同样形态的缺失，没有停牌事件就该判为漏采并告警。"""
    baseline = build_baseline(_clean_config())
    sym = str(baseline.universe["symbol"].iloc[0])
    daily = baseline.stock_daily.copy()
    days = sorted(daily.loc[daily["symbol"] == sym, "date"])
    drop = days[50:53]
    daily = daily[~((daily["symbol"] == sym) & (daily["date"].isin(drop)))]

    diags = _diagnose(baseline, daily=daily)
    hits = [
        d
        for d in diags
        if d.root_cause is RootCause.MISSING_ROWS and d.finding_key.split("|")[2] == sym
    ]
    assert hits, "无事件可解释的空洞应判为漏采"
    assert any(not d.suppressed for d in hits)


def test_ex_rights_is_suppressed():
    """除权除息导致的价格跳空是合法的，不能报成值域异常。"""
    baseline = build_baseline(SyntheticConfig(n_suspension=0, n_ex_rights=2, n_new_listing=0))
    diags = _diagnose(baseline)
    hits = [d for d in diags if d.root_cause is RootCause.BENIGN_CORPORATE_ACTION]
    assert hits, "除权跳空应触发信号并被识别"
    assert all(d.suppressed for d in hits)


def test_new_listing_is_suppressed():
    """新股历史短是上市晚导致的，不是数据被删。"""
    baseline = build_baseline(SyntheticConfig(n_suspension=0, n_ex_rights=0, n_new_listing=1))
    diags = _diagnose(baseline)
    hits = [d for d in diags if d.root_cause is RootCause.BENIGN_NEW_LISTING]
    assert hits, "新股历史不足应触发信号并被识别"
    assert all(d.suppressed for d in hits)


def test_duplicate_write_is_alerted():
    """主键重复必须报 P0，通常是重跑没做幂等。"""
    baseline = build_baseline(_clean_config())
    sym = str(baseline.universe["symbol"].iloc[1])
    daily = baseline.stock_daily.copy()
    days = sorted(daily.loc[daily["symbol"] == sym, "date"])
    dup = daily[(daily["symbol"] == sym) & (daily["date"].isin(days[40:42]))]
    daily = pd.concat([daily, dup], ignore_index=True)

    diags = _diagnose(baseline, daily=daily)
    hits = [d for d in diags if d.root_cause is RootCause.DUPLICATE_WRITE]
    assert hits
    assert hits[0].severity is Severity.P0


def test_unit_shift_is_alerted():
    """总市值整体缩到万分之一，只能解释为单位/口径变更。"""
    baseline = build_baseline(_clean_config())
    sym = str(baseline.universe["symbol"].iloc[2])
    val = baseline.valuation_daily.copy()
    days = sorted(val.loc[val["symbol"] == sym, "date"])
    cut = days[int(len(days) * 0.6)]
    val.loc[(val["symbol"] == sym) & (val["date"] >= cut), "total_mv"] /= 10000.0

    diags = _diagnose(baseline, val=val)
    hits = [d for d in diags if d.root_cause is RootCause.UNIT_SHIFT]
    assert hits
    assert hits[0].severity is Severity.P0


def test_upstream_outage_from_lineage():
    """血缘里有 failed run 时，缺失必须归因到上游故障而不是漏采。"""
    baseline = build_baseline(_clean_config())
    sym = str(baseline.universe["symbol"].iloc[0])
    daily = baseline.stock_daily.copy()
    daily = daily[~((daily["symbol"] == sym) & (daily["date"] >= sorted(
        daily.loc[daily["symbol"] == sym, "date"])[-5]))]

    lineage = pd.DataFrame(
        [
            {
                "run_id": "abc123",
                "target_table": "stock_daily",
                "status": "failed",
                "params": f'{{"symbol": "{sym}"}}',
            }
        ]
    )
    ctx = ProbeContext.of(
        stock_daily=daily,
        valuation_daily=baseline.valuation_daily,
        universe=baseline.universe,
        corporate_event=baseline.corporate_event,
        calendar=baseline.calendar,
        ingest_run=lineage,
    )
    diags = BaselineTriage().diagnose_all(run_probes(ctx), ctx)
    hits = [d for d in diags if d.root_cause is RootCause.UPSTREAM_OUTAGE]
    assert hits, "血缘记录采集失败时应归因到上游故障"
    assert hits[0].severity is Severity.P0
