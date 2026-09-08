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
    list_date_backfilled: int = 0  # 本次回填了上市日的股票数
    trading_calendar_rows: int = 0  # 交易日历天数（0 = 采失败，节假日误报会回来）
    corporate_event_rows: int = 0  # 公司事件条数（0 = 没开 --events 或采失败）

    def report(self) -> str:
        lines = [
            f"采集完成：成功 {len(self.ok)} 项 / 失败 {len(self.failed)} 项"
            f" / 共写入 {self.total_rows} 行"
        ]
        if self.list_date_backfilled:
            lines.append(
                f"  上市日回填：{self.list_date_backfilled} 只（取自 stock_daily 首日）"
            )
        lines.append(
            f"  交易日历：{self.trading_calendar_rows} 个交易日"
            + ("（采集失败，节假日会误报）" if not self.trading_calendar_rows else "")
        )
        if self.corporate_event_rows:
            lines.append(f"  公司事件：{self.corporate_event_rows} 条")
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


def fetch_trading_calendar() -> pd.DataFrame:
    """交易所真实交易日历（1990 至今，约 8800 天）。

    硬编码节假日表只能覆盖写死的年份，往前一点（如 2022 春节）就会把
    **全市场同时休市**报成"缺失行情"——而"所有标的同一天缺失"恰恰是
    靠数据本身推不出来的（with_observed 会把有数据的日子并进来，但集体
    休市的日子没有任何观测）。节假日是查表问题，不是推断问题。
    """
    df = _retry(
        lambda: ak.tool_trade_date_hist_sina(),
        attempts=3,
        tag="ak.tool_trade_date_hist_sina",
        sleep=settings.ingest_sleep_seconds,
    )
    out = pd.DataFrame({"date": pd.to_datetime(df["trade_date"]).dt.date})
    return out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)


def fetch_dividend_events(symbol: str) -> pd.DataFrame:
    """历史除权除息日。归因器用它解释"价格跳变"是正常事件而非脏数据。"""
    df = _retry(
        lambda: ak.stock_history_dividend_detail(symbol=symbol, indicator="分红"),
        attempts=2,
        tag=f"ak.stock_history_dividend_detail({symbol})",
        sleep=settings.ingest_sleep_seconds,
    )
    if df is None or df.empty or "除权除息日" not in df.columns:
        return pd.DataFrame(columns=["symbol", "date", "kind", "end_date", "detail"])
    out = df[df["除权除息日"].notna()].copy()
    out["date"] = pd.to_datetime(out["除权除息日"]).dt.date
    out["symbol"] = symbol
    out["kind"] = "ex_rights"
    out["end_date"] = None
    out["detail"] = out.apply(
        lambda r: f"派息{_fmt(r['派息'])} 送股{_fmt(r['送股'])} 转增{_fmt(r['转增'])}",
        axis=1,
    )
    return out[["symbol", "date", "kind", "end_date", "detail"]].drop_duplicates(
        subset=["symbol", "date", "kind"]
    )


def fetch_suspension_snapshot() -> pd.DataFrame:
    """当日停牌快照（东财口径）。

    注意语义：这是"此刻谁停着"的快照，不是历史事件流——接口不返回停牌
    起止日期。所以 end_date 留空，归因器按 `end_date IS NULL → 当日` 处理。
    对历史回溯帮助有限，但至少让"停牌导致缺行"这条归因在真实数据上可达。
    """
    df = _retry(
        lambda: ak.stock_zh_a_stop_em(),
        attempts=2,
        tag="ak.stock_zh_a_stop_em",
        sleep=settings.ingest_sleep_seconds,
    )
    if df is None or df.empty or "代码" not in df.columns:
        return pd.DataFrame(columns=["symbol", "date", "kind", "end_date", "detail"])
    today = date.today()
    out = pd.DataFrame(
        {
            "symbol": df["代码"].astype(str).str.zfill(6),
            "date": today,
            "kind": "suspension",
            "end_date": None,
            "detail": "当日停牌快照：" + df["名称"].astype(str),
        }
    )
    return out.drop_duplicates(subset=["symbol", "date", "kind"])


def _fmt(v: object) -> str:
    try:
        if v is None or pd.isna(v):
            return "-"
    except (TypeError, ValueError):
        return "-"
    return str(v)


def ingest_trading_calendar(conn: duckdb.DuckDBPyConnection) -> int:
    df = fetch_trading_calendar()
    with tracked_run(
        conn, source="ak.tool_trade_date_hist_sina", target_table="trading_calendar"
    ) as (_, t):
        n = upsert_dataframe(conn, "trading_calendar", df, ["date"])
        t.add(n)
    return n


def ingest_corporate_events(
    conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None
) -> int:
    """采集公司事件（除权除息 + 停牌快照）到 corporate_event。

    没有这个，corporate_event 永远是空表 —— 停牌/除权的抑制能力在真实数据
    上就只是"设计过的对照"，而不是能跑的链路。
    """
    symbols = symbols or [s for s, _, _ in UNIVERSE]
    frames: list[pd.DataFrame] = []
    with tracked_run(
        conn, source="ak.corporate_events", target_table="corporate_event"
    ) as (_, t):
        for symbol in symbols:
            try:
                frames.append(fetch_dividend_events(symbol))
            except Exception as exc:  # 单只失败不影响整批
                logger.warning("除权除息 %s 采集失败: %s", symbol, exc)
            time.sleep(settings.ingest_sleep_seconds)
        try:
            frames.append(fetch_suspension_snapshot())
        except Exception as exc:
            logger.warning("停牌快照采集失败: %s", exc)

        if not frames:
            return 0
        df = pd.concat(frames, ignore_index=True)
        n = upsert_dataframe(conn, "corporate_event", df, ["symbol", "date", "kind"])
        t.add(n)
    return n


def ingest_universe(conn: duckdb.DuckDBPyConnection) -> int:
    # list_date 先留空：akshare 拿上市日要额外调接口，而日线还没采，
    # 此刻也没有素材可推算。ingest_all 末尾会调 backfill_list_date 回填。
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


def backfill_list_date(conn: duckdb.DuckDBPyConnection) -> int:
    """用 stock_daily 的首个交易日回填 `stock_universe.list_date`。

    为什么需要：归因器靠 list_date 区分「新股历史天然短」（BENIGN_NEW_LISTING，
    抑制告警）和「历史数据被删」（MISSING_ROWS，要报）。没有它，真实数据路径上
    这条分支永远走不到 —— 只有 fixture 能触发，等于主线能力是哑的。

    为什么敢用首个交易日近似：akshare 没有顺手的上市日接口，而"表内最早的
    交易日"误差最多一天（首日停牌除外），对"是不是新股"这个二值判断足够。

    性质：
    - **幂等**：只在 list_date 为空时写，重复执行第二次返回 0
    - **自我修正**：增量补采把历史推到更早时，list_date 跟着往前挪，不会
      锁死在首次采集看到的那个值
    """
    # 命中条件：没填过，或日线里出现了比已填值更早的日期
    _COND = """
        (SELECT min(date) FROM stock_daily d WHERE d.symbol = u.symbol) IS NOT NULL
        AND (u.list_date IS NULL
             OR u.list_date > (SELECT min(date) FROM stock_daily d WHERE d.symbol = u.symbol))
    """
    changed = conn.execute(
        f"SELECT count(*) FROM stock_universe u WHERE {_COND}"
    ).fetchone()[0]
    if not changed:
        return 0

    conn.execute(
        f"""
        UPDATE stock_universe u
        SET list_date = (SELECT min(date) FROM stock_daily d WHERE d.symbol = u.symbol)
        WHERE {_COND}
        """
    )
    return int(changed)


def ingest_all(
    conn: duckdb.DuckDBPyConnection,
    end: str | None = None,
    full_refresh: bool = False,
    universe: list[tuple[str, str, str]] | None = None,
    with_events: bool = False,
) -> IngestSummary:
    """采集股票池全部日线 + 估值 + 指数日线。默认增量模式。

    `with_events=True` 额外采公司事件（除权除息/停牌）。默认关：它要按标的
    逐个调接口，比日线慢一个量级，而日线才是巡检的主输入。
    """
    summary = IngestSummary()
    universe = universe if universe is not None else UNIVERSE
    end = end or date.today().strftime("%Y%m%d")
    ingest_universe(conn)

    # 交易日历很便宜（一次调用 8800 行）且是消除节假日误报的前提，总是采。
    try:
        summary.trading_calendar_rows = ingest_trading_calendar(conn)
    except Exception as exc:
        logger.error("交易日历采集失败（节假日误报会回来）: %s", exc)

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

    # 日线都采完了才有素材推算上市日；幂等，重复跑返回 0
    summary.list_date_backfilled = backfill_list_date(conn)

    if with_events:
        try:
            summary.corporate_event_rows = ingest_corporate_events(
                conn, [s for s, _, _ in universe]
            )
        except Exception as exc:
            logger.error("公司事件采集失败（停牌/除权抑制将无据可依）: %s", exc)

    return summary

    return summary
