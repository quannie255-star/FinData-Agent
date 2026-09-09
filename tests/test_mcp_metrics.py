"""MCP server 的 metric 工具测试 (findata_list_metrics + findata_execute_metric)。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from findata.mcp import server as mcp_server

DEFAULT_YAML = Path(__file__).parent.parent / "src" / "findata" / "dq" / "metrics.yaml"


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个测试清掉 module-level 单例, 避免 YAML env 干扰。"""
    saved = mcp_server._METRICS_REGISTRY
    mcp_server._METRICS_REGISTRY = None
    yield
    mcp_server._METRICS_REGISTRY = saved


def test_list_metrics_returns_yaml_content():
    r = mcp_server.findata_list_metrics()
    assert r["count"] >= 10
    names = [m["name"] for m in r["metrics"]]
    # 关键指标必现
    for required in ("freshness", "row_count", "null_rate", "distinct_symbol", "freshness_sql"):
        assert required in names
    # 每条 metric 必含关键字段
    for m in r["metrics"]:
        assert {"name", "kind", "description", "default_severity", "threshold"} <= m.keys()


def test_list_metrics_honors_env_var(tmp_path):
    """FINDATA_METRICS_YAML 环境变量覆盖默认 yaml。"""
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "metrics:\n"
        "  - name: my_metric\n"
        "    kind: sql\n"
        "    description: test\n"
        "    default_severity: P1\n"
        "    sql_template: 'SELECT 1'\n",
        encoding="utf-8",
    )
    os.environ["FINDATA_METRICS_YAML"] = str(custom)
    try:
        r = mcp_server.findata_list_metrics()
        assert r["count"] == 1
        assert r["metrics"][0]["name"] == "my_metric"
    finally:
        del os.environ["FINDATA_METRICS_YAML"]


def test_execute_metric_row_count():
    """跑 row_count: 编译 SQL, 跑 duckdb, 拿结果。"""
    r = mcp_server.findata_execute_metric("row_count", source="synthetic", asof="2024-01-02")
    assert r["name"] == "row_count"
    assert r["kind"] == "sql"
    assert "?" in r["sql"]
    assert r["rows"]  # 非空
    # row_count 输出是 [{'n': int}]
    assert "n" in r["rows"][0]


def test_execute_metric_null_rate_runs_against_synthetic():
    r = mcp_server.findata_execute_metric("null_rate", source="synthetic", asof="2024-01-02")
    assert r["kind"] == "sql"
    # 合成数据下 close 不为 null, n_null=0
    row = r["rows"][0]
    assert row["n_total"] >= 1
    assert row["n_null"] >= 0


def test_execute_metric_rejects_probe():
    r = mcp_server.findata_execute_metric("freshness", source="synthetic")
    assert "error" in r
    assert "kind=" in r["error"]


def test_execute_metric_unknown_name():
    r = mcp_server.findata_execute_metric("nope_metric", source="synthetic")
    assert "error" in r
    assert "nope_metric" in r["error"]

