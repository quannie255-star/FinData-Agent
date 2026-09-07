"""确定性合成数据：评测语料的「干净基线」。

为什么评测用合成数据而不是直接采 akshare：
1. 可复现 —— 评测要进 CI，上游接口抽风会让门禁随机失败
2. 可标注 —— 故障注入前的数据是干净的，每个异常都有确定答案
3. 可控 —— 停牌、除权、新股这些低频但关键的业务事件，靠真实数据撞运气等不到

代价是分布不如真实数据真实。所以分工是：
合成数据跑评测门禁，真实数据（M1 已通的 akshare 链路）跑演示。

刻意不 import domains.finance.ingest：评测路径不应该依赖 akshare，
否则离线环境和 CI 都会挂。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from findata.dq.calendar import TradingCalendar

# 与 domains/finance/ingest.UNIVERSE 前 12 只一致（跨行业，含主板/创业板/科创板）
_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("600519", "贵州茅台", "白酒"),
    ("000858", "五粮液", "白酒"),
    ("000568", "泸州老窖", "白酒"),
    ("300750", "宁德时代", "动力电池"),
    ("002594", "比亚迪", "新能源汽车"),
    ("601012", "隆基绿能", "光伏"),
    ("601318", "中国平安", "保险"),
    ("600036", "招商银行", "银行"),
    ("601166", "兴业银行", "银行"),
    ("600030", "中信证券", "券商"),
    ("000333", "美的集团", "家电"),
    ("000651", "格力电器", "家电"),
)

_TRADING_DAYS_PER_YEAR = 252


def _board(symbol: str) -> str:
    if symbol.startswith("688"):
        return "star"
    if symbol.startswith(("300", "301")):
        return "chinext"
    return "main"


def _price_limit(board: str) -> float:
    """涨跌停幅度：主板 10%，创业板/科创板 20%。"""
    return 0.20 if board in ("star", "chinext") else 0.10


@dataclass(frozen=True)
class SyntheticConfig:
    seed: int = 20240101
    start: str = "2024-01-01"
    end: str = "2024-06-30"
    n_symbols: int = 12
    n_suspension: int = 2
    n_ex_rights: int = 2
    n_new_listing: int = 1
    suspension_min_days: int = 4
    suspension_max_days: int = 9


@dataclass
class Baseline:
    """一份干净的合成数据集，供故障注入前使用。"""

    universe: pd.DataFrame
    stock_daily: pd.DataFrame
    valuation_daily: pd.DataFrame
    corporate_event: pd.DataFrame
    calendar: TradingCalendar
    config: SyntheticConfig


@dataclass
class _Events:
    listing: dict[str, date]
    halts: dict[str, set[date]]
    ex_rights: dict[str, dict[date, float]]
    rows: list[dict]


def _plan_events(
    rng: np.random.Generator,
    picked: list[tuple[str, str, str]],
    days: list[date],
    cfg: SyntheticConfig,
) -> _Events:
    """先规划业务事件，再据此生成行情。

    顺序很重要：先有停牌/除权计划，生成价格时才能让数据形态与事件一致，
    这样归因器才有可查的"标准答案"。
    """
    symbols = [s for s, _, _ in picked]
    listing = {s: days[0] for s in symbols}
    halts: dict[str, set[date]] = {s: set() for s in symbols}
    ex_rights: dict[str, dict[date, float]] = {s: {} for s in symbols}
    rows: list[dict] = []

    # 新股：临近观察日才上市，造成"历史长度不足"这一合法异常。
    # 形态上和"数据被删"一模一样，只有结合 list_date 才能区分。
    for sym in symbols[len(symbols) - cfg.n_new_listing :]:
        ld = days[max(0, len(days) - 12)]
        listing[sym] = ld
        rows.append(
            {
                "symbol": sym,
                "date": ld,
                "kind": "listing",
                "end_date": None,
                "detail": "新股上市，此前无行情",
            }
        )

    old = [s for s in symbols if listing[s] == days[0]]

    # 停牌：合法缺失。数据形态与"采集失败"完全一致，只有事件表能区分
    if cfg.n_suspension and old:
        k = min(cfg.n_suspension, len(old))
        for sym in rng.choice(old, size=k, replace=False):
            start = int(rng.integers(10, max(11, len(days) - 15)))
            length = int(rng.integers(cfg.suspension_min_days, cfg.suspension_max_days + 1))
            span = days[start : start + length]
            halts[sym].update(span)
            rows.append(
                {
                    "symbol": sym,
                    "date": span[0],
                    "kind": "suspension",
                    "end_date": span[-1],
                    "detail": f"停牌 {len(span)} 个交易日",
                }
            )

    # 除权除息：复权因子更新导致价格跳空。形态上像"值域越界"，但合法
    if cfg.n_ex_rights and old:
        k = min(cfg.n_ex_rights, len(old))
        for sym in rng.choice(old, size=k, replace=False):
            idx = int(rng.integers(15, max(16, len(days) - 5)))
            day = days[idx]
            factor = float(rng.choice([0.5, 0.6, 0.75, 0.8]))
            ex_rights[sym][day] = factor
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "kind": "ex_rights",
                    "end_date": None,
                    "detail": f"除权除息，复权因子 {factor}",
                }
            )

    return _Events(listing=listing, halts=halts, ex_rights=ex_rights, rows=rows)


def _gen_stock(
    rng: np.random.Generator,
    symbol: str,
    days: list[date],
    events: _Events,
    shares: float,
    p0: float,
    sigma: float,
    limit: float,
    eps: float,
    bps: float,
) -> tuple[list[dict], list[dict]]:
    """生成单只股票的日线与估值。返回 (daily_rows, valuation_rows)。"""
    daily: list[dict] = []
    val: list[dict] = []
    prev_close = p0
    first = True

    for day in days:
        if day < events.listing[symbol]:
            continue
        if day in events.halts[symbol]:
            continue

        ret = float(rng.normal(0.0, sigma))
        pct = float(np.clip(ret, -limit, limit))
        close = prev_close * (1.0 + pct)

        factor = events.ex_rights[symbol].get(day)
        if factor is not None:
            close *= factor

        open_ = prev_close * (1.0 + float(rng.normal(0.0, sigma * 0.3)))
        high = max(open_, close) * (1.0 + abs(float(rng.normal(0.0, 0.004))))
        low = min(open_, close) * (1.0 - abs(float(rng.normal(0.0, 0.004))))
        turnover_rate = float(np.clip(rng.lognormal(-4.6, 0.5), 0.001, 0.25))
        volume = shares * turnover_rate
        vwap = (open_ + high + low + close) / 4.0

        daily.append(
            {
                "symbol": symbol,
                "date": day,
                "open": round(open_, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": round(volume, 2),
                "amount": round(volume * vwap, 2),
                "pct_chg": 0.0 if first else round((close / prev_close - 1.0) * 100, 4),
                "turnover": round(turnover_rate * 100, 4),
            }
        )
        val.append(
            {
                "symbol": symbol,
                "date": day,
                "pe": round(close / eps, 4),
                "pe_ttm": round(close / eps, 4),
                "pb": round(close / bps, 4),
                "total_mv": round(close * shares, 2),
            }
        )
        prev_close = close
        first = False

    return daily, val


def build_baseline(cfg: SyntheticConfig | None = None) -> Baseline:
    """生成一份干净的合成数据集。相同 config 必得相同结果。"""
    cfg = cfg or SyntheticConfig()
    rng = np.random.default_rng(cfg.seed)
    cal = TradingCalendar.default()
    days = cal.trading_days(date.fromisoformat(cfg.start), date.fromisoformat(cfg.end))

    picked = list(_SYMBOLS[: max(1, min(cfg.n_symbols, len(_SYMBOLS)))])
    events = _plan_events(rng, picked, days, cfg)

    universe_rows: list[dict] = []
    daily_rows: list[dict] = []
    val_rows: list[dict] = []

    for symbol, name, industry in picked:
        board = _board(symbol)
        shares = float(rng.uniform(2e8, 8e9))
        p0 = float(rng.uniform(8.0, 1200.0))
        sigma = float(rng.uniform(0.22, 0.55)) / np.sqrt(_TRADING_DAYS_PER_YEAR)
        eps = float(p0 / rng.uniform(12.0, 45.0))
        bps = float(p0 / rng.uniform(1.5, 8.0))

        universe_rows.append(
            {
                "symbol": symbol,
                "name": name,
                "industry": industry,
                "listed_board": board,
                "list_date": events.listing[symbol],
            }
        )
        d, v = _gen_stock(
            rng,
            symbol,
            days,
            events,
            shares=shares,
            p0=p0,
            sigma=sigma,
            limit=_price_limit(board),
            eps=eps,
            bps=bps,
        )
        daily_rows.extend(d)
        val_rows.extend(v)

    return Baseline(
        universe=pd.DataFrame(universe_rows),
        stock_daily=pd.DataFrame(daily_rows),
        valuation_daily=pd.DataFrame(val_rows),
        corporate_event=pd.DataFrame(
            events.rows,
            columns=["symbol", "date", "kind", "end_date", "detail"],
        ),
        calendar=cal,
        config=cfg,
    )
