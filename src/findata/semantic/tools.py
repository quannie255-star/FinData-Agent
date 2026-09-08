"""MCP 工具：metric_query。

这是语义层的对外接口。Agent（或 MCP client）调用它时：
1. 传入指标名 + 参数（不是 SQL）；
2. 工具内部走「编译 → 执行 → 溯源」确定性链路；
3. 返回数字 + run_id，绝不接受裸 SQL。
"""

from __future__ import annotations

from typing import Any

import duckdb

from findata.semantic.catalog import MetricCatalog
from findata.semantic.compiler import compile_metric
from findata.semantic.executor import QueryResult, execute_metric


def metric_query(
    conn: duckdb.DuckDBPyConnection,
    metric: str,
    params: dict[str, Any] | None = None,
    catalog: MetricCatalog | None = None,
) -> QueryResult:
    """查询一个指标，返回带溯源的结果。

    例：
        metric_query(conn, "close_price", {"symbol": "600519"})
        metric_query(conn, "pct_change",
                     {"symbol": "600519", "start": "2024-01-01", "end": "2024-12-31"})
    """
    catalog = catalog or MetricCatalog()
    compiled = compile_metric(catalog, metric, params or {})
    return execute_metric(conn, compiled, params or {})


def list_metrics(catalog: MetricCatalog | None = None) -> list[dict[str, str]]:
    """列出所有可用指标，供 Agent 选择（返回 name/label/description）。"""
    catalog = catalog or MetricCatalog()
    return [
        {"name": s.name, "label": s.label, "description": s.description}
        for s in [catalog.get(n) for n in catalog.names()]
        if s is not None
    ]
