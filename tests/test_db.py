"""数据层离线测试：schema、幂等 upsert、血缘记录（不依赖网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from findata.core.db import connect, upsert_dataframe
from findata.core.lineage import tracked_run
from findata.domains.finance.ingest import backfill_list_date


@pytest.fixture()
def conn(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    yield conn
    conn.close()


def _daily_df(symbol: str, dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": [pd.to_datetime(d).date() for d in dates],
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": closes,
            "volume": 100.0,
            "amount": 1000.0,
            "pct_chg": 0.0,
            "turnover": 0.0,
        }
    )


def test_schema_created(conn):
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert {
        "stock_universe",
        "stock_daily",
        "index_daily",
        "valuation_daily",
        "ingest_run",
    } <= tables


def test_upsert_idempotent(conn):
    df = _daily_df("600519", ["2024-01-02", "2024-01-03"], [1700.0, 1710.0])
    assert upsert_dataframe(conn, "stock_daily", df, ["symbol", "date"]) == 2
    # 同一批再写一次：仍是 2 行（覆盖而非追加）
    upsert_dataframe(conn, "stock_daily", df, ["symbol", "date"])
    assert conn.execute("SELECT count(*) FROM stock_daily").fetchone()[0] == 2

    # 修正其中一天 + 追加一天：旧行被替换、新行被追加
    df2 = _daily_df("600519", ["2024-01-03", "2024-01-04"], [1720.0, 1730.0])
    upsert_dataframe(conn, "stock_daily", df2, ["symbol", "date"])
    rows = dict(conn.execute("SELECT date, close FROM stock_daily").fetchall())
    assert len(rows) == 3
    assert rows[pd.to_datetime("2024-01-03").date()] == 1720.0


def test_upsert_no_cross_symbol_damage(conn):
    df1 = _daily_df("600519", ["2024-01-02"], [1.0])
    df2 = _daily_df("000858", ["2024-01-02"], [2.0])
    upsert_dataframe(conn, "stock_daily", df1, ["symbol", "date"])
    upsert_dataframe(conn, "stock_daily", df2, ["symbol", "date"])
    assert conn.execute("SELECT count(*) FROM stock_daily").fetchone()[0] == 2


def test_lineage_success_and_failure(conn):
    with tracked_run(conn, source="test.src", target_table="t", params={"k": 1}) as (_, t):
        t.add(42)
    row = conn.execute(
        "SELECT source, status, rows_written, error FROM ingest_run"
    ).fetchone()
    assert row == ("test.src", "success", 42, None)

    with pytest.raises(ValueError), tracked_run(conn, source="bad.src", target_table="t"):
        raise ValueError("boom")
    row = conn.execute(
        "SELECT status, error FROM ingest_run WHERE source='bad.src'"
    ).fetchone()
    assert row[0] == "failed"
    assert "boom" in row[1]


def _universe_df(symbol: str, list_date: str | None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "name": ["测试"],
            "industry": ["测试"],
            "listed_board": ["main"],
            "list_date": [pd.to_datetime(list_date).date() if list_date else None],
        }
    )


def _list_date_of(conn, symbol: str):
    return conn.execute(
        "SELECT list_date FROM stock_universe WHERE symbol = ?", [symbol]
    ).fetchone()[0]


def test_backfill_list_date_from_first_trading_day(conn):
    """上市日取 stock_daily 最早一天 —— 归因器靠它区分"新股"和"历史被删"。"""
    upsert_dataframe(conn, "stock_universe", _universe_df("600519", None), ["symbol"])
    upsert_dataframe(
        conn,
        "stock_daily",
        _daily_df("600519", ["2024-01-05", "2024-01-04", "2024-01-02"], [1.0, 2.0, 3.0]),
        ["symbol", "date"],
    )

    assert backfill_list_date(conn) == 1
    assert _list_date_of(conn, "600519") == pd.to_datetime("2024-01-02").date()


def test_backfill_list_date_idempotent(conn):
    upsert_dataframe(conn, "stock_universe", _universe_df("600519", None), ["symbol"])
    upsert_dataframe(
        conn, "stock_daily", _daily_df("600519", ["2024-01-02"], [1.0]), ["symbol", "date"]
    )

    assert backfill_list_date(conn) == 1
    assert backfill_list_date(conn) == 0  # 第二次无事可做
    assert _list_date_of(conn, "600519") == pd.to_datetime("2024-01-02").date()


def test_backfill_list_date_moves_earlier_when_history_extended(conn):
    """增量补采把历史推到更早时，已填的 list_date 要跟着往前挪。

    否则 expected 交易日数被低估，老股可能被误判成"新股历史完整"而误抑制告警。
    """
    upsert_dataframe(conn, "stock_universe", _universe_df("600519", "2024-06-03"), ["symbol"])
    upsert_dataframe(
        conn, "stock_daily", _daily_df("600519", ["2024-06-03"], [1.0]), ["symbol", "date"]
    )
    assert backfill_list_date(conn) == 0  # 已填且不更早，不动

    upsert_dataframe(
        conn, "stock_daily", _daily_df("600519", ["2024-01-02"], [2.0]), ["symbol", "date"]
    )
    assert backfill_list_date(conn) == 1
    assert _list_date_of(conn, "600519") == pd.to_datetime("2024-01-02").date()


def test_backfill_list_date_skips_without_daily(conn):
    """没有日线的股票不能回填（否则 min(date) 为 NULL，会把已有值覆盖成 NULL）。"""
    upsert_dataframe(conn, "stock_universe", _universe_df("600519", None), ["symbol"])
    assert backfill_list_date(conn) == 0
    assert _list_date_of(conn, "600519") is None
