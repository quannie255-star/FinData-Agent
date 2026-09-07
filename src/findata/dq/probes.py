"""数据质量检测探针。

探针只做观测，不做判断——它不知道什么是停牌、什么是采集故障。
所有判断交给 triage 层。这样切分带来两个好处：

1. 探针可以独立评测（故障召回率）
2. 归因器可以从规则整体替换为 LLM，探针一行都不用改

每个 Finding 都必须带 evidence，回答"凭什么这么说"。
没有证据的告警等于没有告警，人工复核时无法做任何决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from findata.dq.calendar import TradingCalendar
from findata.dq.models import Finding, Severity

# 漂移判定：近期窗口均值与历史均值的比值超出这个区间即告警
_DRIFT_UP = 5.0
_DRIFT_DOWN = 0.2
# 单位跳变：相邻两段量级相差 100 倍以上，正常行情不可能出现
_UNIT_SHIFT_UP = 100.0
_UNIT_SHIFT_DOWN = 0.01

# 统计基线所需的最少样本天数
_MIN_HISTORY_DAYS = 30

_NULL_COLS = ("close", "volume", "amount", "turnover")


@dataclass
class ProbeContext:
    """探针的输入快照。评测传合成 DataFrame，生产从 DuckDB 读出同样结构。"""

    stock_daily: pd.DataFrame
    valuation_daily: pd.DataFrame
    universe: pd.DataFrame
    corporate_event: pd.DataFrame
    calendar: TradingCalendar
    asof: date
    # 采集血缘，可选。有它才能把"数据缺失"直接归因到"这次采集挂了"
    ingest_run: pd.DataFrame = field(default_factory=pd.DataFrame)

    @classmethod
    def of(
        cls,
        stock_daily: pd.DataFrame,
        valuation_daily: pd.DataFrame,
        universe: pd.DataFrame,
        corporate_event: pd.DataFrame,
        calendar: TradingCalendar | None = None,
        asof: date | None = None,
        ingest_run: pd.DataFrame | None = None,
    ) -> ProbeContext:
        cal = calendar or TradingCalendar.default()
        if asof is None:
            candidates = [d for d in (stock_daily["date"].max(),) if pd.notna(d)]
            asof = max(candidates) if candidates else date.today()
        return cls(
            stock_daily=stock_daily,
            valuation_daily=valuation_daily,
            universe=universe,
            corporate_event=corporate_event,
            calendar=cal,
            asof=asof,
            ingest_run=ingest_run if ingest_run is not None else pd.DataFrame(),
        )


def _limits(ctx: ProbeContext) -> dict[str, float]:
    """按板块取涨跌停幅度。"""
    out: dict[str, float] = {}
    if ctx.universe.empty or "listed_board" not in ctx.universe:
        return out
    for row in ctx.universe.itertuples(index=False):
        board = getattr(row, "listed_board", "main")
        out[row.symbol] = 0.20 if board in ("star", "chinext") else 0.10
    return out


def _list_start(ctx: ProbeContext, symbol: str, fallback: date) -> date:
    if not ctx.universe.empty and "list_date" in ctx.universe.columns:
        sub = ctx.universe.loc[ctx.universe["symbol"] == symbol, "list_date"]
        if len(sub) and pd.notna(sub.iloc[0]):
            return sub.iloc[0]
    return fallback


def _collapse(days: list[date], cal: TradingCalendar) -> list[list[date]]:
    """把离散日期折叠成连续段：段内若不存在交易日，则视为同一段。

    用交易日历而不是自然日，否则每个周末都会把一段缺失切成两段。
    """
    spans: list[list[date]] = []
    for d in sorted(days):
        if spans:
            gap = cal.trading_days(spans[-1][-1] + timedelta(days=1), d - timedelta(days=1))
            if not gap:
                spans[-1].append(d)
                continue
        spans.append([d])
    return spans


def _merge_hits(hits: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """合并滑动窗口的连续命中，保留每个区段首次命中时的观测值。

    取首次命中值是因为：异常刚开始时"近期窗口全是异常、历史窗口全是正常"，
    这时的比值最干净；继续滑动下去历史窗口也会被污染，比值反而失真。
    """
    if not hits:
        return []
    out: list[list] = [list(hits[0])]
    for lo, hi, val in hits[1:]:
        if lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi, val])
    return [(a, b, c) for a, b, c in out]


def _span_window(span: list[date]) -> str:
    return f"{span[0]}..{span[-1]}"


def probe_freshness(ctx: ProbeContext) -> list[Finding]:
    """新鲜度：数据最后日期落后了几个交易日。停牌/新股也会命中，交给归因。"""
    if ctx.stock_daily.empty:
        return []
    out: list[Finding] = []
    for symbol, g in ctx.stock_daily.groupby("symbol"):
        last = g["date"].max()
        stale = ctx.calendar.staleness(last, ctx.asof)
        if stale < 1:
            continue
        # 落后 1 个交易日：昨天的没到，P1；落后 2 个以上：连续多日没到，P0
        severity = Severity.P0 if stale >= 2 else Severity.P1
        out.append(
            Finding(
                probe="freshness",
                table="stock_daily",
                symbol=str(symbol),
                window=f"{last}..{ctx.asof}",
                metric="stale_trading_days",
                value=float(stale),
                threshold=2.0,
                severity_hint=severity,
                evidence={"last_data_date": str(last), "asof": str(ctx.asof)},
            )
        )
    return out


def probe_completeness(ctx: ProbeContext) -> list[Finding]:
    """完整性：上市日到观察日之间应到未到的交易日空洞。"""
    if ctx.stock_daily.empty:
        return []
    out: list[Finding] = []
    for symbol, g in ctx.stock_daily.groupby("symbol"):
        actual = set(g["date"].tolist())
        start = _list_start(ctx, str(symbol), min(actual))
        expected = ctx.calendar.trading_days(start, ctx.asof)
        missing = [d for d in expected if d not in actual]
        if not missing:
            continue
        for span in _collapse(missing, ctx.calendar):
            # 缺失是确定性事实，不像漂移有程度之分，因此不按天数分档降级：
            # 少一天也是缺，下游做区间统计一样会算错
            severity = Severity.P1
            out.append(
                Finding(
                    probe="completeness",
                    table="stock_daily",
                    symbol=str(symbol),
                    window=_span_window(span),
                    metric="missing_trading_days",
                    value=float(len(span)),
                    threshold=1.0,
                    severity_hint=severity,
                    evidence={"missing_dates": [str(d) for d in span[:10]]},
                )
            )
    return out


def probe_uniqueness(ctx: ProbeContext) -> list[Finding]:
    """唯一性：主键重复，通常是重跑任务没做幂等。"""
    out: list[Finding] = []
    for table, df in (("stock_daily", ctx.stock_daily), ("valuation_daily", ctx.valuation_daily)):
        if df.empty:
            continue
        sizes = df.groupby(["symbol", "date"]).size()
        dups = sizes[sizes > 1]
        if dups.empty:
            continue
        for symbol, sub in dups.reset_index().groupby("symbol"):
            days = sorted(sub["date"].tolist())
            out.append(
                Finding(
                    probe="uniqueness",
                    table=table,
                    symbol=str(symbol),
                    window=_span_window(days),
                    metric="duplicate_keys",
                    value=float(len(sub)),
                    threshold=1.0,
                    severity_hint=Severity.P0,
                    evidence={"sample_dates": [str(d) for d in days[:5]]},
                )
            )
    return out


def probe_validity(ctx: ProbeContext) -> list[Finding]:
    """值域：负价、涨跌幅超板、high<low、非正成交量。

    除权除息会造成涨跌幅超限，形态上与解析异常一致，由归因器区分。
    """
    if ctx.stock_daily.empty:
        return []
    limits = _limits(ctx)
    out: list[Finding] = []
    for symbol, g in ctx.stock_daily.groupby("symbol"):
        g = g.sort_values("date")
        sym = str(symbol)
        limit = limits.get(sym, 0.10) * 100

        checks = {
            "non_positive_close": g["close"] <= 0,
            "pct_chg_out_of_limit": g["pct_chg"].abs() > limit + 1e-6,
            "high_lt_low": g["high"] < g["low"],
            "non_positive_volume": g["volume"] <= 0,
        }
        for metric, mask in checks.items():
            bad = g.loc[mask, "date"].tolist()
            if not bad:
                continue
            out.append(
                Finding(
                    probe="validity",
                    table="stock_daily",
                    symbol=sym,
                    window=_span_window(bad),
                    metric=metric,
                    value=float(len(bad)),
                    threshold=1.0,
                    severity_hint=Severity.P0,
                    evidence={
                        "price_limit_pct": round(limit, 2),
                        "sample_dates": [str(d) for d in bad[:5]],
                    },
                )
            )
    return out


def probe_drift(ctx: ProbeContext) -> list[Finding]:
    """分布漂移：成交量量级突变，以及估值表的单位跳变。

    用滑动窗口而不是只看最后 N 天：故障可以发生在历史的任意位置
    （比如某段历史被重新计算），只盯尾部会留下整段盲区。
    这个盲区就是首轮评测跑出来的漏检暴露的。
    """
    out: list[Finding] = []

    if not ctx.stock_daily.empty:
        for symbol, g in ctx.stock_daily.groupby("symbol"):
            g = g.sort_values("date").reset_index(drop=True)
            vol = g["volume"].to_numpy(dtype=float)
            if len(vol) < 40:
                continue
            dates = g["date"].tolist()
            hits: list[tuple[int, int, float]] = []
            for i in range(40, len(vol) + 1):
                recent = float(np.nanmean(vol[i - 10 : i]))
                history = float(np.nanmean(vol[i - 40 : i - 10]))
                if history <= 0 or np.isnan(recent):
                    continue
                ratio = recent / history
                if ratio > _DRIFT_UP or ratio < _DRIFT_DOWN:
                    hits.append((i - 10, i - 1, round(ratio, 4)))
            for lo, hi, ratio in _merge_hits(hits):
                base = vol[max(0, lo - 30) : lo]
                out.append(
                    Finding(
                        probe="drift",
                        table="stock_daily",
                        symbol=str(symbol),
                        window=f"{dates[lo]}..{dates[hi]}",
                        metric="volume_recent_over_history",
                        value=ratio,
                        threshold=_DRIFT_UP,
                        severity_hint=Severity.P1,
                        evidence={
                            "recent_mean": round(float(np.nanmean(vol[lo : hi + 1])), 2),
                            "history_mean": round(float(np.nanmean(base)), 2)
                            if len(base)
                            else 0.0,
                        },
                    )
                )

    if not ctx.valuation_daily.empty:
        for symbol, g in ctx.valuation_daily.groupby("symbol"):
            g = g.sort_values("date")
            mv = g["total_mv"].to_numpy(dtype=float)
            if len(mv) < 40:
                continue
            for i in range(30, len(mv) - 4):
                prev = float(np.nanmean(mv[i - 30 : i]))
                nxt = float(np.nanmean(mv[i : i + 5]))
                if prev <= 0 or nxt <= 0:
                    continue
                ratio = nxt / prev
                if _UNIT_SHIFT_DOWN < ratio < _UNIT_SHIFT_UP:
                    continue
                out.append(
                    Finding(
                        probe="drift",
                        table="valuation_daily",
                        symbol=str(symbol),
                        window=f"{g['date'].iloc[i]}..{g['date'].iloc[-1]}",
                        metric="total_mv_level_shift",
                        value=round(ratio, 6),
                        threshold=_UNIT_SHIFT_UP,
                        severity_hint=Severity.P0,
                        evidence={
                            "before_mean": round(prev, 2),
                            "after_mean": round(nxt, 2),
                            "note": "量级突变，怀疑单位或口径变更",
                        },
                    )
                )
                break
    return out


def probe_null_sweep(ctx: ProbeContext) -> list[Finding]:
    """空值扫荡：某段窗口空值率远高于历史，通常是上游字段下线。

    同样用滑动窗口：字段下线后又恢复的情况，"只看最近"会整段漏掉。
    """
    win, hist = 15, 45
    if ctx.stock_daily.empty:
        return []
    out: list[Finding] = []
    for symbol, g in ctx.stock_daily.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        dates = g["date"].tolist()
        for col in _NULL_COLS:
            if col not in g.columns:
                continue
            flags = g[col].isna().to_numpy(dtype=float)
            n = len(flags)
            if n < win + hist:
                continue
            hits: list[tuple[int, int, float]] = []
            for i in range(win + hist, n + 1):
                r_rate = float(np.nanmean(flags[i - win : i]))
                h_rate = float(np.nanmean(flags[i - win - hist : i - win]))
                if r_rate > 0.5 and (r_rate - h_rate) > 0.3:
                    hits.append((i - win, i - 1, round(r_rate, 4)))
            for lo, hi, rate in _merge_hits(hits):
                base = flags[max(0, lo - 30) : lo]
                out.append(
                    Finding(
                        probe="null_sweep",
                        table="stock_daily",
                        symbol=str(symbol),
                        window=f"{dates[lo]}..{dates[hi]}",
                        metric=f"null_rate::{col}",
                        value=rate,
                        threshold=0.5,
                        severity_hint=Severity.P1,
                        evidence={
                            "history_null_rate": round(float(np.nanmean(base)), 4)
                            if len(base)
                            else 0.0,
                            "column": col,
                        },
                    )
                )
    return out


def probe_history_depth(ctx: ProbeContext) -> list[Finding]:
    """历史深度：样本不足以支撑统计基线。

    新股天然命中这一条。它和"数据被删"在形态上完全一样——
    区别只有结合 list_date 才能看出来，这正是归因器的价值所在。
    """
    if ctx.stock_daily.empty:
        return []
    out: list[Finding] = []
    for symbol, g in ctx.stock_daily.groupby("symbol"):
        n = len(g)
        if n >= _MIN_HISTORY_DAYS:
            continue
        days = sorted(g["date"].tolist())
        out.append(
            Finding(
                probe="history_depth",
                table="stock_daily",
                symbol=str(symbol),
                window=_span_window(days),
                metric="history_trading_days",
                value=float(n),
                threshold=float(_MIN_HISTORY_DAYS),
                severity_hint=Severity.P2,
                evidence={"note": "样本不足，漂移等统计判定不可靠"},
            )
        )
    return out


PROBES = (
    probe_freshness,
    probe_completeness,
    probe_uniqueness,
    probe_validity,
    probe_drift,
    probe_null_sweep,
    probe_history_depth,
)


def run_probes(ctx: ProbeContext) -> list[Finding]:
    """跑全部探针并汇总。单个探针异常不影响其他探针。"""
    findings: list[Finding] = []
    for probe in PROBES:
        findings.extend(probe(ctx))
    return findings
