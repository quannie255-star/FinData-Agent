"""语义层离线测试：指标字典、编译器（防注入）、执行与溯源。不依赖网络。"""

from __future__ import annotations

import pandas as pd
import pytest

from findata.core.db import connect, upsert_dataframe
from findata.semantic.catalog import MetricCatalog
from findata.semantic.compiler import CompileError, compile_metric
from findata.semantic.tools import list_metrics, metric_query


@pytest.fixture()
def conn(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    # 造少量离线数据
    daily = pd.DataFrame(
        {
            "symbol": ["600519"] * 3,
            "date": [pd.to_datetime(d).date() for d in ["2024-01-02", "2024-01-03", "2024-01-04"]],
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [100.0, 110.0, 121.0],
            "volume": 1000.0,
            "amount": 1e6,
            "pct_chg": [0.0, 10.0, 10.0],
            "turnover": 0.0,
        }
    )
    upsert_dataframe(conn, "stock_daily", daily, ["symbol", "date"])
    valuation = pd.DataFrame(
        {
            "symbol": ["600519"],
            "date": [pd.to_datetime("2024-01-04").date()],
            "pe": 20.0,
            "pe_ttm": 18.5,
            "pb": 5.0,
            "total_mv": 2e12,
        }
    )
    upsert_dataframe(conn, "valuation_daily", valuation, ["symbol", "date"])
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def catalog():
    return MetricCatalog()


def test_catalog_has_three_metrics(catalog):
    assert set(catalog.names()) == {"close_price", "pct_change", "valuation_pe_ttm"}


def test_list_metrics_shape(catalog):
    items = list_metrics(catalog)
    assert len(items) == 3
    assert all({"name", "label", "description"} <= set(m) for m in items)


def test_compile_close_price(catalog):
    c = compile_metric(catalog, "close_price", {"symbol": "600519"})
    assert "?" in c.sql
    assert c.params == ("600519",)
    assert c.metric.unit == "元"


def test_compile_pct_change(catalog):
    c = compile_metric(
        catalog,
        "pct_change",
        {"symbol": "600519", "start": "2024-01-01", "end": "2024-12-31"},
    )
    # 绑定值按模板占位符出现顺序收集，且值齐全、无注入
    assert len(c.params) == c.sql.count("?")
    assert set(c.params) == {"600519", "2024-01-01", "2024-12-31"}
    assert c.params[0] == "600519"  # 首个占位符是 symbol


def test_unknown_metric_rejected(catalog):
    with pytest.raises(CompileError):
        compile_metric(catalog, "nope", {})


def test_missing_required_param(catalog):
    with pytest.raises(CompileError):
        compile_metric(catalog, "close_price", {})


def test_undeclared_param_rejected(catalog):
    # 传入未声明参数（越权列尝试）应被拒绝
    with pytest.raises(CompileError):
        compile_metric(catalog, "close_price", {"symbol": "600519", "drop_table": "x"})


def test_sql_injection_not_possible(catalog):
    # 即便 symbol 带注入载荷，也被当作普通字符串绑定，而非拼进 SQL
    evil = "600519'; DROP TABLE stock_daily; --"
    c = compile_metric(catalog, "close_price", {"symbol": evil})
    assert "DROP" not in c.sql
    assert c.params == (evil,)


def test_wrong_type_rejected(catalog):
    with pytest.raises(CompileError):
        compile_metric(catalog, "close_price", {"symbol": 123})
    with pytest.raises(CompileError):
        compile_metric(
            catalog,
            "pct_change",
            {"symbol": "600519", "start": "01/01/2024", "end": "2024-12-31"},
        )


def test_query_close_price_returns_value_and_run_id(conn, catalog):
    r = metric_query(conn, "close_price", {"symbol": "600519"}, catalog)
    assert r.value == 121.0
    assert r.unit == "元"
    assert r.run_id
    assert r.as_of == "2024-01-04"


def test_query_pct_change(conn, catalog):
    r = metric_query(
        conn,
        "pct_change",
        {"symbol": "600519", "start": "2024-01-02", "end": "2024-01-04"},
        catalog,
    )
    # (121 / 100 - 1) * 100 = 21.0
    assert r.value == pytest.approx(21.0)
    assert r.unit == "%"


def test_query_valuation(conn, catalog):
    r = metric_query(conn, "valuation_pe_ttm", {"symbol": "600519"}, catalog)
    assert r.value == 18.5
    assert r.unit == "倍"


def test_query_records_lineage(conn, catalog):
    metric_query(conn, "close_price", {"symbol": "600519"}, catalog)
    rows = conn.execute(
        "SELECT metric, params, sql, value FROM query_run WHERE metric='close_price'"
    ).fetchall()
    assert len(rows) == 1
    metric, params, sql, value = rows[0]
    assert metric == "close_price"
    assert "600519" in params
    assert "stock_daily" in sql
    assert value == 121.0
