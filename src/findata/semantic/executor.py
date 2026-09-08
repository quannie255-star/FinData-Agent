"""查询执行层：把编译后的查询落到 DuckDB，并产出带 run_id 溯源的结果。

每次查询都写入 query_run 表（血缘的"查询端"），返回的 QueryResult 携带
run_id，供上层把答案里的数字绑定回"哪次查询、哪个指标、什么参数、命中哪张表"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

import duckdb

from findata.semantic.catalog import MetricCatalog
from findata.semantic.compiler import CompiledQuery
from findata.semantic.verifier import Verification, format_verification, verify_metric


@dataclass(frozen=True)
class QueryResult:
    metric: str
    label: str
    value: float | None
    unit: str
    as_of: str | None
    run_id: str
    source: str
    params: dict
    # 附加时间信息（区间类指标用），如 start_date / end_date
    extra: dict | None = None
    # 交叉验证结论（M4 可信层）
    verification: Verification | None = None


def _record_run(
    conn: duckdb.DuckDBPyConnection,
    metric: str,
    params: dict,
    sql: str,
) -> str:
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO query_run
            (run_id, metric, params, sql, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [run_id, metric, json.dumps(params, ensure_ascii=False), sql, datetime.now()],
    )
    return run_id


def execute_metric(
    conn: duckdb.DuckDBPyConnection,
    compiled: CompiledQuery,
    params: dict,
) -> QueryResult:
    """执行已编译查询，记录血缘，交叉验证，返回带 run_id 的结果。"""
    spec = compiled.metric
    run_id = _record_run(conn, spec.name, params, compiled.sql)

    row = conn.execute(compiled.sql, list(compiled.params)).fetchone()
    if row is None:
        value, as_of = None, None
        extra = None
    else:
        value = row[0]
        as_of = str(row[1]) if len(row) > 1 and row[1] is not None else None
        # 区间类指标可能带 start_date/end_date（列名已固定）
        extra = None
        if len(row) > 2:
            cols = [d[0] for d in conn.description]
            extra = {cols[i]: str(row[i]) for i in range(2, len(row)) if row[i] is not None}

    conn.execute(
        "UPDATE query_run SET value=?, finished_at=? WHERE run_id=?",
        [value, datetime.now(), run_id],
    )

    # 交叉验证（M4）：有 verify 口径的指标自动核验，关联 query_run_id 供溯源
    verification = verify_metric(conn, MetricCatalog(), compiled, params, query_run_id=run_id)

    return QueryResult(
        metric=spec.name,
        label=spec.label,
        value=value,
        unit=spec.unit,
        as_of=as_of,
        run_id=run_id,
        source=spec.source,
        params=params,
        extra=extra,
        verification=verification,
    )


def format_result(r: QueryResult) -> str:
    """把结果渲染成一行可读、可复述的答案。"""
    if r.value is None:
        return f"{r.label}：无数据（{r.metric}）"
    base = f"{r.label} = {r.value} {r.unit}"
    if r.as_of:
        base += f"（截至 {r.as_of}）"
    if r.extra:
        detail = " / ".join(f"{k}={v}" for k, v in r.extra.items())
        base += f" [{detail}]"
    base += f"  [run_id={r.run_id}]"
    if r.verification is not None:
        base += f"  {format_verification(r.verification)}"
    return base
