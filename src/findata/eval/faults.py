"""故障注入：在干净基线上施加已知的、带标注的破坏。

每一类故障都对应一个真实运维场景，而不是随手造的脏数据：

| 注入类型   | 现实对应                                     |
|------------|----------------------------------------------|
| stale      | 上游接口挂了/限流，最近几个交易日没采到     |
| gap        | 回补任务漏跑，中间空洞                       |
| duplicate  | 任务重跑但没做幂等，主键重复                 |
| outlier    | 解析异常，价格出现负值或明显越界             |
| drift      | 单位或口径变了（如成交量单位由手改成股）     |
| null_sweep | 上游字段下线，某列整段变 NULL               |
| unit_shift | 估值接口改口径，总市值从元变成万元           |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from findata.dq.models import RootCause, Severity
from findata.eval.fixtures import Baseline


@dataclass(frozen=True)
class InjectedFault:
    """一个被注入的真实故障，即评测集里的"标准答案"。"""

    kind: str
    table: str
    symbol: str
    window: str
    expected_root_cause: RootCause
    expected_severity: Severity
    note: str = ""


@dataclass
class InjectedDataset:
    stock_daily: pd.DataFrame
    valuation_daily: pd.DataFrame
    faults: list[InjectedFault]


def _symbol_days(df: pd.DataFrame, symbol: str) -> list[date]:
    return sorted(df.loc[df["symbol"] == symbol, "date"].tolist())


def _window(days: list[date]) -> str:
    return f"{days[0]}..{days[-1]}" if days else "-"


def _pick(rng: np.random.Generator, days: list[date], size: int) -> list[date]:
    start = int(rng.integers(0, max(1, len(days) - size)))
    return days[start : start + size]


def inject_faults(baseline: Baseline, seed: int = 20240102) -> InjectedDataset:
    """在干净基线上注入全部故障类型（每类一只股票，互不重叠）。"""
    rng = np.random.default_rng(seed)
    daily = baseline.stock_daily.copy()
    val = baseline.valuation_daily.copy()
    faults: list[InjectedFault] = []

    # 只挑有完整历史的股票做载体：新股/停牌的合法缺失会污染标注
    uni = baseline.universe
    oldest = uni["list_date"].min()
    pool = list(uni.loc[uni["list_date"] == oldest, "symbol"])
    kinds = ["stale", "gap", "duplicate", "outlier", "drift", "null_sweep", "unit_shift"]
    k = min(len(kinds), len(pool))
    chosen = list(rng.choice(pool, size=k, replace=False))

    for kind, sym in zip(kinds[:k], chosen, strict=True):
        days = _symbol_days(daily, sym)
        # 漂移/空值类判定需要历史基线，数据开头本来就不具备判定条件。
        # 注入时避开前段，这不是迁就实现，而是"基线不足时无法判定"这一真实约束。
        offset = min(50, len(days) // 3)

        if kind == "stale":
            cut = _pick(rng, days, 1)[0]
            size = int(rng.integers(3, 7))
            dropped = [d for d in days if d >= cut][-size:]
            daily = daily[~((daily["symbol"] == sym) & (daily["date"].isin(dropped)))]
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=_window(dropped),
                    expected_root_cause=RootCause.UPSTREAM_STALE,
                    expected_severity=Severity.P0,
                    note="上游停更，最新交易日数据缺失",
                )
            )

        elif kind == "gap":
            size = int(rng.integers(2, 5))
            dropped = _pick(rng, days[:-8], size)
            daily = daily[~((daily["symbol"] == sym) & (daily["date"].isin(dropped)))]
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=_window(dropped),
                    expected_root_cause=RootCause.MISSING_ROWS,
                    expected_severity=Severity.P1,
                    note="回补任务漏跑，中间出现空洞",
                )
            )

        elif kind == "duplicate":
            size = int(rng.integers(2, 5))
            dup_days = _pick(rng, days[:-8], size)
            dup = daily[(daily["symbol"] == sym) & (daily["date"].isin(dup_days))].copy()
            daily = pd.concat([daily, dup], ignore_index=True)
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=_window(dup_days),
                    expected_root_cause=RootCause.DUPLICATE_WRITE,
                    expected_severity=Severity.P0,
                    note="任务重跑未做幂等，主键重复",
                )
            )

        elif kind == "outlier":
            day = _pick(rng, days[:-5], 1)[0]
            mask = (daily["symbol"] == sym) & (daily["date"] == day)
            daily.loc[mask, "close"] = -abs(daily.loc[mask, "close"].iloc[0])
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=f"{day}..{day}",
                    expected_root_cause=RootCause.VALUE_OUT_OF_RANGE,
                    expected_severity=Severity.P0,
                    note="解析异常导致价格为负",
                )
            )

        elif kind == "drift":
            size = 10
            span = _pick(rng, days[offset:-size], size)
            mask = (daily["symbol"] == sym) & (daily["date"].isin(span))
            daily.loc[mask, "volume"] = daily.loc[mask, "volume"] * 20.0
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=_window(span),
                    expected_root_cause=RootCause.DISTRIBUTION_DRIFT,
                    expected_severity=Severity.P1,
                    note="成交量单位变更，整体放大 20 倍",
                )
            )

        elif kind == "null_sweep":
            size = 15
            span = _pick(rng, days[offset:-size], size)
            mask = (daily["symbol"] == sym) & (daily["date"].isin(span))
            daily.loc[mask, "amount"] = np.nan
            faults.append(
                InjectedFault(
                    kind=kind,
                    table="stock_daily",
                    symbol=sym,
                    window=_window(span),
                    expected_root_cause=RootCause.SCHEMA_NULL_SWEEP,
                    expected_severity=Severity.P1,
                    note="上游字段下线，成交额整段为 NULL",
                )
            )

        elif kind == "unit_shift":
            vdays = _symbol_days(val, sym)
            if len(vdays) > 20:
                cut = vdays[int(len(vdays) * 0.6)]
                mask = (val["symbol"] == sym) & (val["date"] >= cut)
                val.loc[mask, "total_mv"] = val.loc[mask, "total_mv"] / 10000.0
                faults.append(
                    InjectedFault(
                        kind=kind,
                        table="valuation_daily",
                        symbol=sym,
                        window=f"{cut}..{vdays[-1]}",
                        expected_root_cause=RootCause.UNIT_SHIFT,
                        expected_severity=Severity.P0,
                        note="总市值口径由元改为万元",
                    )
                )

    return InjectedDataset(stock_daily=daily, valuation_daily=val, faults=faults)


def benign_expectations(baseline: Baseline) -> list[InjectedFault]:
    """合法业务事件清单：这些会触发异常信号，但正确的行为是抑制告警。

    它们不是被注入的，而是基线里天然存在的。评测时作为"应当被抑制"的对照。
    """
    mapping = {
        "suspension": RootCause.BENIGN_SUSPENSION,
        "ex_rights": RootCause.BENIGN_CORPORATE_ACTION,
        "listing": RootCause.BENIGN_NEW_LISTING,
    }
    out: list[InjectedFault] = []
    if baseline.corporate_event.empty:
        return out
    for row in baseline.corporate_event.itertuples(index=False):
        cause = mapping.get(row.kind)
        if cause is None:
            continue
        end = row.end_date if row.end_date is not None else row.date
        out.append(
            InjectedFault(
                kind=row.kind,
                table="corporate_event",
                symbol=row.symbol,
                window=f"{row.date}..{end}",
                expected_root_cause=cause,
                expected_severity=Severity.OK,
                note=f"合法业务事件：{row.detail}",
            )
        )
    return out
