"""金融域数据采集：akshare → DuckDB。

工程约定（面试考点）：
- 幂等：按主键 upsert，重复执行结果一致
- 增量：按表内已有最大日期续采，避免全量重拉
- 血缘：每次写入通过 tracked_run 记录来源接口与行数
- 韧性：单只失败不影响整批，失败清单汇总返回
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import akshare as ak
import duckdb
import pandas as pd

from findata.config import settings
from findata.core.db import upsert_dataframe
from findata.core.lineage import tracked_run

logger = logging.getLogger(__name__)

# 精选股票池：跨行业、高流动性、认知度高（便于演示与 golden set 出题）
UNIVERSE: list[tuple[str, str, str]] = [
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
    ("600887", "伊利股份", "食品饮料"),
    ("600276", "恒瑞医药", "创新药"),
    ("300760", "迈瑞医疗", "医疗器械"),
    ("688981", "中芯国际", "半导体"),
    ("002371", "北方华创", "半导体设备"),
    ("601899", "紫金矿业", "有色金属"),
    ("600309", "万华化学", "化工"),
    ("600900", "长江电力", "电力"),
    ("601088", "中国神华", "煤炭"),
    ("300059", "东方财富", "金融科技"),
    ("002415", "海康威视", "安防"),
    ("000063", "中兴通讯", "通信"),
    ("002475", "立讯精密", "消费电子"),
]

# 指数：标准代码 -> (名称, 新浪代码)。东财指数接口已失效，改用新浪
INDEXES: dict[str, tuple[str, str]] = {
    "000300": ("沪深300", "sh000300"),
    "000001": ("上证指数", "sh000001"),
    "399006": ("创业板指", "sz399006"),
}

# 新浪日线列（个股与指数同源，口径一致）。东财接口有反爬限流，弃用
# 个股比指数多 amount/turnover，缺失列在 _normalize 中自动丢弃
_SINA_DAILY_COLS = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
    "turnover": "turnover",
}

_INDEX_DAILY_COLS = _SINA_DAILY_COLS

_VALUATION_COLS = {
    "数据日期": "date",
    "PE(静)": "pe",
    "PE(TTM)": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_mv",
}


@dataclass
class IngestSummary:
    ok: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    total_rows: int = 0

    def report(self) -> str:
        lines = [
            f"采集完成：成功 {len(self.ok)} 项 / 失败 {len(self.failed)} 项"
            f" / 共写入 {self.total_rows} 行"
        ]
        for f in self.failed:
            lines.append(f"  [失败] {f['target']} {f['symbol']}: {f['error']}")
        return "\n".join(lines)


def _board(symbol: str) -> str:
    if symbol.startswith("688"):
        return "star"  # 科创板
    if symbol.startswith(("300", "301")):
        return "chinext"  # 创业板
    return "main"


def _retry(fn, *, attempts: int, tag: str, sleep: float):
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # akshare 的网络/解析异常统一走重试
            last_exc = exc
            if i < attempts - 1:
                wait = sleep * (i + 1)
                logger.warning("[%s] 第 %d 次失败：%s，%.1fs 后重试", tag, i + 1, exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"{tag} 重试 {attempts} 次仍失败") from last_exc


def _normalize(
    df: pd.DataFrame, col_map: dict[str, str], symbol: str | None = None
) -> pd.DataFrame:
    df = df.rename(columns=col_map)
    df = df[[c for c in col_map.values() if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if symbol is not None:
        df.insert(0, "symbol", symbol)
    return df


def _max_date(conn: duckdb.DuckDBPyConnection, table: str, symbol: str) -> date | None:
    row = conn.execute(
        f"SELECT max(date) FROM {table} WHERE symbol = ?", [symbol]
    ).fetchone()
    return row[0]


def _incremental_start(conn, table: str, symbol: str, default_start: str) -> str:
    """增量起点 = 已有最大日期 + 1 天；全量重跑则从配置起点开始。"""
    last = _max_date(conn, table, symbol)
    if last is None:
        return default_start
    return (last + timedelta(days=1)).strftime("%Y%m%d")


def _to_sina(symbol: str) -> str:
    """A 股代码转新浪格式：60/68 开头为沪市，其余为深市。"""
    prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
    return prefix + symbol


def fetch_stock_daily(symbol: str, start: str) -> pd.DataFrame:
    """新浪个股日线（前复权），全史返回后按增量起点过滤，涨跌幅自行计算。"""
    df = _retry(
        lambda: ak.stock_zh_a_daily(symbol=_to_sina(symbol), adjust="qfq"),
        attempts=settings.ingest_max_retries,
        tag=f"stock_daily:{symbol}",
        sleep=settings.ingest_sleep_seconds,
    )
    df = _normalize(df, _SINA_DAILY_COLS, symbol)
    df["pct_chg"] = (df["close"].pct_change() * 100).round(4)
    start_date = pd.to_datetime(start).date()
    df = df[df["date"] >= start_date].reset_index(drop=True)
    if df.empty:  # 增量模式下已采到最新，新浪 T+1 无新数据
        return df
    df.loc[df.index[0], "pct_chg"] = 0.0  # 过滤后首日无前收，涨跌幅置 0
    return df


def fetch_index_daily(code: str, sina_symbol: str, start: str) -> pd.DataFrame:
    """新浪指数日线返回全史，按增量起点过滤。"""
    df = _retry(
        lambda: ak.stock_zh_index_daily(symbol=sina_symbol),
        attempts=settings.ingest_max_retries,
        tag=f"index_daily:{sina_symbol}",
        sleep=settings.ingest_sleep_seconds,
    )
    df = _normalize(df, _INDEX_DAILY_COLS)
    df.insert(0, "symbol", code)
    start_date = pd.to_datetime(start).date()
    return df[df["date"] >= start_date].reset_index(drop=True)


def fetch_valuation(symbol: str, start: str) -> pd.DataFrame:
    """东财个股估值（PE/PB/总市值），全史返回后按起点过滤。"""
    df = _retry(
        lambda: ak.stock_value_em(symbol=symbol),
        attempts=settings.ingest_max_retries,
        tag=f"valuation:{symbol}",
        sleep=settings.ingest_sleep_seconds,
    )
    df = _normalize(df, _VALUATION_COLS, symbol)
    start_date = pd.to_datetime(start).date()
    return df[df["date"] >= start_date].reset_index(drop=True)


def ingest_universe(conn: duckdb.DuckDBPyConnection) -> int:
    # list_date 留空：akshare 需额外接口才能拿到上市日。
    # TODO: 可从 stock_daily 首日近似回填（首次采集后 COALESCE 一次即可），
    # 归因器需要它来区分"新股历史短"和"数据被删"。
    df = pd.DataFrame(
        [
            {
                "symbol": s,
                "name": n,
                "industry": ind,
                "listed_board": _board(s),
                "list_date": None,
            }
            for s, n, ind in UNIVERSE
        ]
    )
    with tracked_run(conn, source="static.universe", target_table="stock_universe") as (_, t):
        n = upsert_dataframe(conn, "stock_universe", df, ["symbol"])
        t.add(n)
    return n


def ingest_all(
    conn: duckdb.DuckDBPyConnection,
    end: str | None = None,
    full_refresh: bool = False,
    universe: list[tuple[str, str, str]] | None = None,
) -> IngestSummary:
    """采集股票池全部日线 + 估值 + 指数日线。默认增量模式。"""
    summary = IngestSummary()
    universe = universe if universe is not None else UNIVERSE
    end = end or date.today().strftime("%Y%m%d")
    ingest_universe(conn)

    def _do(source: str, table: str, symbol: str, fetch) -> None:
        try:
            with tracked_run(
                conn, source=source, target_table=table, params={"symbol": symbol}
            ) as (_, t):
                df = fetch()
                n = upsert_dataframe(conn, table, df, ["symbol", "date"])
                t.add(n)
            summary.ok.append({"target": table, "symbol": symbol, "rows": n})
            summary.total_rows += n
            logger.info("%s %s: %d 行", table, symbol, n)
        except Exception as exc:
            summary.failed.append(
                {"target": table, "symbol": symbol, "error": str(exc)[:200]}
            )
            logger.error("%s %s 采集失败: %s", table, symbol, exc)

    for symbol, _, _ in universe:
        if not full_refresh:
            start = _incremental_start(conn, "stock_daily", symbol, settings.ingest_start_date)
        else:
            start = settings.ingest_start_date
        if start > end:
            summary.ok.append({"target": "stock_daily", "symbol": symbol, "rows": 0})
            continue
        _do(
            "ak.stock_zh_a_daily",
            "stock_daily",
            symbol,
            lambda s=symbol, st=start: fetch_stock_daily(s, st),
        )
        time.sleep(settings.ingest_sleep_seconds)

        # 估值无增量接口，全史拉取后过滤；有数据则跳过（估值日频、变化慢）
        if _max_date(conn, "valuation_daily", symbol) is None or full_refresh:
            _do(
                "ak.stock_value_em",
                "valuation_daily",
                symbol,
                lambda s=symbol: fetch_valuation(s, settings.ingest_start_date),
            )
            time.sleep(settings.ingest_sleep_seconds)

    for symbol, (_, sina_symbol) in INDEXES.items():
        start = (
            settings.ingest_start_date
            if full_refresh
            else _incremental_start(conn, "index_daily", symbol, settings.ingest_start_date)
        )
        if start > end:
            summary.ok.append({"target": "index_daily", "symbol": symbol, "rows": 0})
            continue
        _do(
            "ak.stock_zh_index_daily",
            "index_daily",
            symbol,
            lambda c=symbol, ss=sina_symbol, st=start: fetch_index_daily(c, ss, st),
        )
        time.sleep(settings.ingest_sleep_seconds)

    return summary
