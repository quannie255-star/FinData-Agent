"""数据质量探针与归因的单测。

重点不是"能不能跑通"，而是**形态相同、结论相反**的对照场景：
同样是数据缺失，停牌该抑制、漏采该告警；
同样是涨跌幅超限，除权该抑制、解析错误该告警。

这几组对照就是本项目全部立论的基础，任何一条挂掉都意味着设计失效。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from findata.dq.calendar import TradingCalendar
from findata.dq.models import RootCause, Severity
from findata.dq.probes import ProbeContext, probe_drift, run_probes
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


def _exchange_calendar() -> TradingCalendar:
    """一张极简交易所日历：1/2/3 开市，1/31-2/4 春节休市，其余按周末。"""
    days: list[date] = []
    for d in (1, 2, 3):
        days.append(date(2022, 1, d))
    days.append(date(2022, 1, 28))  # 春节前最后一天
    for d in range(7, 11):  # 2/7 起恢复（7、8、9、10 中只取工作日）
        day = date(2022, 2, d)
        if day.weekday() < 5:
            days.append(day)
    return TradingCalendar.from_trading_days(days)


def test_exchange_calendar_is_lookup_not_inference():
    """真实日历模式直接查表：调休上班的周六也算交易日。"""
    days = [date(2024, 2, 4), date(2024, 2, 5)]  # 2024-02-04 是周日但春节调休开市
    cal = TradingCalendar.from_trading_days(days)
    assert cal.is_exchange_calendar
    assert cal.is_trading_day(date(2024, 2, 4))  # 周日，但日历里有
    assert not cal.is_trading_day(date(2024, 2, 6))  # 周二，但日历里没有
    assert not TradingCalendar.default().is_exchange_calendar


def test_exchange_calendar_kills_spring_festival_false_positive():
    """硬编码节假日表覆盖不到 2022，春节会被报成一串 missing_rows。

    这是拿真实数据巡检时**实际发生的误报**：4 条告警里 3 条落在 2022 春节窗口。
    春节是全市场同时休市，数据里没有任何观测能反推出来（with_observed 只认
    "出现过的日子"），所以唯一正解是接真实交易日历。
    """
    cal = TradingCalendar.default()
    # 硬编码表只到 2024，2022-02-01（周二）会被当成普通工作日
    assert cal.is_trading_day(date(2022, 2, 1))

    real = _exchange_calendar()
    assert not real.is_trading_day(date(2022, 2, 1))
    assert real.trading_days(date(2022, 1, 31), date(2022, 2, 4)) == []


def test_exchange_calendar_prev_day_does_not_hang_out_of_range():
    """查询早于日历覆盖范围时不能向后无限回溯。"""
    cal = _exchange_calendar()
    # 1980 年远早于日历；必须返回而不是死循环
    assert cal.prev_trading_day(date(1980, 1, 1)) == date(1979, 12, 31)


def test_with_observed_merges_into_exchange_calendar():
    """真实日历模式下 with_observed 仍然生效（补齐接口漏采的日子）。"""
    cal = _exchange_calendar()
    extra = date(2022, 2, 5)  # 日历里没有，但数据里出现过
    assert not cal.is_trading_day(extra)
    assert cal.with_observed([extra]).is_trading_day(extra)


# ── R1.2 截面共动归因（BENIGN_MARKET_EVENT）─────────────────────────────
# 阈值场景按 2026-09-15 真实窗口实测标定：924 = 全市场 24-25/25 ≥2x + 指数
# 2.2-2.8x；724 = 大金融板块 3 只同伴 ≥2x（全市场仅 6/25，指数 ≤1.45x）；
# 2023-03 中芯国际 = 孤立事件（同伴 ≤1.6x，指数 1.22x），保留为残留误报。

_CO_INDUSTRIES = [
    "券商", "银行", "保险", "金融科技", "白酒", "光伏", "家电", "化工", "煤炭", "通信",
]
# 10 标的、仅大金融 4 只构成板块：板块共动不满足全市场判据（4/10 < 0.5）


def _co_universe(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"{600000 + i}" for i in range(n)],
            "name": [f"股{i}" for i in range(n)],
            "industry": _CO_INDUSTRIES[:n],
            "listed_board": ["main"] * n,
            "list_date": [date(2023, 1, 2)] * n,
        }
    )


def _co_daily(n: int = 10, days: int = 60, base_vol: float = 1_000.0) -> pd.DataFrame:
    """量能恒定的干净日线：窗口倍率 = 人为乘数，不会被随机噪声稀释。"""
    # 先生成足够的自然日再滤掉周末，保证截取后足 days 个"交易日"
    raw = [date(2023, 1, 2) + timedelta(days=i) for i in range(days * 2)]
    cal_days = [d for d in raw if d.weekday() < 5][:days]
    rows = []
    for i in range(n):
        for d in cal_days:
            rows.append({"symbol": f"{600000 + i}", "date": d, "volume": base_vol})
    return pd.DataFrame(rows), cal_days


def _co_context(daily, universe, index=None):
    cal = TradingCalendar.from_trading_days(sorted(daily["date"].unique()))
    return ProbeContext.of(
        stock_daily=daily,
        valuation_daily=pd.DataFrame(),
        universe=universe,
        corporate_event=pd.DataFrame(),
        calendar=cal,
        asof=daily["date"].max(),
        index_daily=index if index is not None else pd.DataFrame(),
    )


def _surge(daily, symbols, lo, hi, x):
    out = daily.copy()
    mask = out["symbol"].isin(symbols)
    dates = pd.to_datetime(out["date"]).dt.date
    out.loc[mask & (dates >= lo) & (dates <= hi), "volume"] *= x
    return out


def _first_drift_finding(ctx):
    # 共动场景的日线只有 volume 列，全量探针会在 validity 上崩；
    # 本组测试只关心 drift 探针的归因，直接单跑它
    findings = probe_drift(ctx)
    assert findings, "放大窗口后必须有漂移信号，否则测试场景不成立"
    return findings[0]


def test_market_wide_surge_is_suppressed_as_market_event():
    """924 形态：全市场同步放量（≥半数标的 ≥2x）→ 抑制为市场行情。"""
    daily, cal_days = _co_daily()
    lo, hi = cal_days[45], cal_days[54]
    symbols = [f"{600000 + i}" for i in range(6)]
    daily = _surge(daily, symbols, lo, hi, 5.5)  # 全部 ≥2x：命中全市场判据
    ctx = _co_context(daily, _co_universe())
    d = BaselineTriage().diagnose(_first_drift_finding(ctx), ctx)
    assert d.root_cause is RootCause.BENIGN_MARKET_EVENT
    assert d.suppressed


def test_sector_surge_is_suppressed_even_when_market_is_calm():
    """724 形态：全市场平静（4/10 < 半数），但同板块 ≥2 只同伴共动 → 抑制。"""
    daily, cal_days = _co_daily()
    lo, hi = cal_days[45], cal_days[54]
    # 目标(券商) ×5.5 触发漂移探针；银行/保险/金融科技 3 只同伴 ×3 共动；
    # 漂移探针阈值 5x，同伴 3x 只参与共动统计、自身不产生信号
    daily = _surge(daily, {"600000"}, lo, hi, 5.5)
    daily = _surge(daily, {"600001", "600002", "600003"}, lo, hi, 3.0)
    ctx = _co_context(daily, _co_universe())
    finding = _first_drift_finding(ctx)
    assert finding.symbol == "600000"
    d = BaselineTriage().diagnose(finding, ctx)
    assert d.root_cause is RootCause.BENIGN_MARKET_EVENT
    assert d.suppressed
    assert "大金融" in d.explanation


def test_index_surge_confirms_market_event():
    """指数同窗放量 ≥1.5x 也是市场级行情的直接证据（复盘 P0b 口径）。"""
    daily, cal_days = _co_daily()
    lo, hi = cal_days[45], cal_days[54]
    daily = _surge(daily, {"600000"}, lo, hi, 5.5)  # 只有目标自己
    index = pd.DataFrame(
        [
            {"symbol": "000300", "date": d, "volume": 3.0 if lo <= d <= hi else 1.0}
            for d in cal_days
        ]
    )
    ctx = _co_context(daily, _co_universe(), index=index)
    d = BaselineTriage().diagnose(_first_drift_finding(ctx), ctx)
    assert d.root_cause is RootCause.BENIGN_MARKET_EVENT
    assert d.suppressed


def test_isolated_surge_stays_a_fault():
    """2023-03 中芯国际形态：孤立放量（无同伴、无指数证据）→ 保留漂移告警。"""
    daily, cal_days = _co_daily()
    lo, hi = cal_days[45], cal_days[54]
    daily = _surge(daily, {"600000"}, lo, hi, 5.5)
    ctx = _co_context(daily, _co_universe())
    d = BaselineTriage().diagnose(_first_drift_finding(ctx), ctx)
    assert d.root_cause is RootCause.DISTRIBUTION_DRIFT
    assert not d.suppressed


def test_benign_label_covers_market_event_and_action():
    from findata.report.inspect import benign_label

    assert benign_label(RootCause.BENIGN_MARKET_EVENT) == "市场行情"
    assert benign_label(RootCause.BENIGN_MARKET_ACTION) != RootCause.BENIGN_MARKET_ACTION.value
