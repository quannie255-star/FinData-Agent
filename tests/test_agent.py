"""Agent 编排层测试：图结构、工具绑定、路由（不依赖真实 LLM）。"""

from __future__ import annotations

import pandas as pd
import pytest

from findata.agent import graph as g
from findata.core.db import connect, upsert_dataframe


@pytest.fixture()
def conn(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    daily = pd.DataFrame(
        {
            "symbol": ["600519"],
            "date": [pd.to_datetime("2024-01-04").date()],
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [121.0],
            "volume": 1000.0,
            "amount": 1e6,
            "pct_chg": 0.0,
            "turnover": 0.0,
        }
    )
    upsert_dataframe(conn, "stock_daily", daily, ["symbol", "date"])
    yield conn
    conn.close()


def test_tools_whitelist_only_two(conn):
    tools = g._build_tools(conn)
    names = {t.name for t in tools}
    assert names == {"metric_query", "list_metrics"}


def test_metric_query_tool_returns_run_id(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["metric_query"].invoke({"metric": "close_price", "params": {"symbol": "600519"}})
    assert "121.0" in out
    assert "run_id=" in out


def test_list_metrics_tool(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["list_metrics"].invoke({})
    assert "close_price" in out
    assert "pct_change" in out
    assert "valuation_pe_ttm" in out


def test_invalid_metric_tool_returns_error_text(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["metric_query"].invoke({"metric": "not_exist", "params": {}})
    assert "查询失败" in out or "未知指标" in out


def test_graph_compiles(conn):
    agent = g.build_agent(conn)
    assert agent is not None
    assert agent._app is not None


def test_allowed_tools_constant():
    assert g.ALLOWED_TOOLS == {"metric_query", "list_metrics"}
