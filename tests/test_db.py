"""数据层离线测试：schema、幂等 upsert、血缘记录（不依赖网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from findata.core.db import connect, upsert_dataframe
from findata.core.lineage import tracked_run


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
