"""交叉验证器：用第二条独立口径重算关键数字，产出验证结论与置信标注。

M4 可信层的核心。主路径（template）与验证路径（verify.template）算法不同，
结果在容差 tol 内一致 → verified；不一致 → mismatch；无法验证（无 verify
定义或数据不足）→ not_verifiable。每次验证写入 verify_run 血缘表。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import duckdb

from findata.semantic.catalog import MetricCatalog
from findata.semantic.compiler import CompiledQuery, compile_verify


class VerifyStatus(Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True)
class Verification:
    status: VerifyStatus
    main_value: float | None
    verify_value: float | None
    tol: float
    note: str
    verify_run_id: str | None = None


def _record_verify_run(
    conn: duckdb.DuckDBPyConnection,
    query_run_id: str,
    metric: str,
    params: dict,
    main_value: float | None,
    verify_value: float | None,
    status: str,
    sql: str,
    note: str = "",
) -> str:
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO verify_run
            (run_id, query_run_id, metric, params,
             main_value, verify_value, status, sql, note, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            query_run_id,
            metric,
            json.dumps(params, ensure_ascii=False),
            main_value,
            verify_value,
            status,
            sql,
            note,
            datetime.now(),
        ],
    )
    return run_id


def verify_metric(
    conn: duckdb.DuckDBPyConnection,
    catalog: MetricCatalog,
    compiled: CompiledQuery,
    params: dict,
    query_run_id: str | None = None,
) -> Verification:
    """对一次已执行的主查询做交叉验证。

    主查询结果（compiled 已执行得到 value）应作为参数传入 main_value；
    这里重新执行主路径取 main_value 以保证验证自包含。
    """
    spec = compiled.metric
    verify_compiled = compile_verify(catalog, spec.name, params)

    if verify_compiled is None:
        return Verification(
            status=VerifyStatus.NOT_VERIFIABLE,
            main_value=None,
            verify_value=None,
            tol=0.0,
            note="该指标未定义验证口径",
        )

    main_row = conn.execute(compiled.sql, list(compiled.params)).fetchone()
    main_value = main_row[0] if main_row else None

    verify_row = conn.execute(
        verify_compiled.sql, list(verify_compiled.params)
    ).fetchone()
    verify_value = verify_row[0] if verify_row else None

    if main_value is None or verify_value is None:
        status = VerifyStatus.NOT_VERIFIABLE
        note = "主路径或验证路径无数据"
    else:
        tol = spec.verify.tol
        if abs(main_value - verify_value) <= tol:
            status = VerifyStatus.VERIFIED
            note = spec.verify.note
        else:
            status = VerifyStatus.MISMATCH
            diff = abs(main_value - verify_value)
            note = f"{spec.verify.note}；两路径差异 {diff:.4f} 超出容差 {tol}"

    verify_run_id = _record_verify_run(
        conn,
        query_run_id or "unknown",
        spec.name,
        params,
        main_value,
        verify_value,
        status.value,
        verify_compiled.sql,
        note=note,
    )

    return Verification(
        status=status,
        main_value=main_value,
        verify_value=verify_value,
        tol=spec.verify.tol if spec.verify else 0.0,
        note=note,
        verify_run_id=verify_run_id,
    )


def format_verification(v: Verification) -> str:
    """把验证结论渲染成一句话，附加到最终答案后。"""
    if v.status is VerifyStatus.VERIFIED:
        return "✓ 已交叉验证（独立口径一致）"
    if v.status is VerifyStatus.MISMATCH:
        return (
            f"⚠ 交叉验证不一致：主值 {v.main_value} vs 验证值 {v.verify_value}"
            f"（容差 {v.tol}）"
        )
    return "○ 未交叉验证（该指标无独立验证口径或无数据）"
