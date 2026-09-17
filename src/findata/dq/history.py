"""DQ 信号历史：巡检产物（Finding + Diagnosis）落到 DuckDB 的 dq_signal 表。

为什么值得一张表（M12）：VerifierAgent 要在运行时回答「这个标的的数据
最近有没有问题、有没有刚被抑制过的误报」——这要求 DQ 产物**可查询**，
而不是巡检跑完就散落在内存和报告里。M13 的归因经验记忆也以这张表为地基。

边界约定：
- 写入方：service.inspect（duckdb 源）。synthetic 源不落库——合成故障写进
  历史会污染真实信号，这是「记忆污染」的预防从建表就开始做。
- 读取方：VerifierAgent 的 dq_context 工具、triage_trust 工具。
- 本模块只认 (Finding, Diagnosis) 对，不 import report 层——依赖方向
  保持 report → dq，不反向。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import duckdb

from findata.dq.models import Diagnosis, Finding

# 默认回看窗口：回答「最近有没有问题」用 7 天足够，
# 「3 天前刚被抑制过一次误报」这类背书就在这个窗口里。
DEFAULT_LOOKBACK_DAYS = 7


def record_signals(
    conn: duckdb.DuckDBPyConnection,
    pairs: Iterable[tuple[Finding, Diagnosis]],
    asof: date,
) -> int:
    """把一批 (Finding, Diagnosis) 幂等写入 dq_signal，返回写入行数。

    幂等策略：先删同 (asof, finding_key) 再插入——同一天重跑巡检以最新结论覆盖。
    """
    rows = [
        (
            asof,
            f.key,
            f.probe,
            f.table,
            f.symbol,
            f.window,
            f.metric,
            float(f.value),
            float(f.threshold),
            f.severity_hint.value,
            d.root_cause.value,
            d.severity.value,
            bool(d.suppressed),
            float(d.confidence),
            d.explanation,
        )
        for f, d in pairs
    ]
    if not rows:
        return 0
    conn.execute("BEGIN")
    try:
        conn.execute(
            'DELETE FROM dq_signal WHERE "asof" = ? AND finding_key IN '
            "(" + ", ".join("?" for _ in rows) + ")",
            [asof, *[r[1] for r in rows]],
        )
        conn.executemany(
            """
            INSERT INTO dq_signal
                ("asof", finding_key, probe, table_name, symbol, "window", metric,
                 value, threshold, severity_hint, root_cause, severity,
                 suppressed, confidence, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def recent_signals(
    conn: duckdb.DuckDBPyConnection,
    symbol: str | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
    asof: date | None = None,
) -> list[dict]:
    """查询最近 N 天的 DQ 信号，可按 symbol 过滤，新的在前。

    asof 锚点缺省取表内最新观察日（巡检跑到的最后一天），而不是今天——
    回放历史时，「最近」必须相对数据的时间轴，不是相对挂钟。
    """
    anchor_row = conn.execute('SELECT max("asof") FROM dq_signal').fetchone()
    anchor = asof or (anchor_row[0] if anchor_row and anchor_row[0] else None)
    if anchor is None:
        return []
    # 上界必须钉在锚点上：历史回放时不能读到"未来"的信号
    sql = 'SELECT * FROM dq_signal WHERE "asof" >= ? AND "asof" <= ?'
    params: list = [anchor - timedelta(days=days), anchor]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += ' ORDER BY "asof" DESC, finding_key'
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(_DQ_SIGNAL_COLS, r, strict=True)) for r in rows]


# recent_signals 用固定列序取数，避免每次查询都 describe 一遍
_DQ_SIGNAL_COLS = (
    "asof",
    "finding_key",
    "probe",
    "table_name",
    "symbol",
    "window",
    "metric",
    "value",
    "threshold",
    "severity_hint",
    "root_cause",
    "severity",
    "suppressed",
    "confidence",
    "explanation",
)


def signal_to_row(d: dict) -> dict:
    """裁剪成给 LLM 看的最小事实集（explanation 可能很长，保留——它是背书本体）。"""
    return {
        "asof": str(d.get("asof") or ""),
        "symbol": d.get("symbol"),
        "probe": d.get("probe"),
        "metric": d.get("metric"),
        "window": d.get("window"),
        "root_cause": d.get("root_cause"),
        "severity": d.get("severity"),
        "suppressed": bool(d.get("suppressed")),
        "explanation": d.get("explanation", ""),
    }
