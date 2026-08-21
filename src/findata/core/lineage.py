"""数据血缘：每次采集/写入记录 run_id，供可信层溯源引用。

Agent 最终答案中的每个数字都能回答"这份数据是哪次采集、从哪个接口来的"，
血缘表就是这条链路的起点。
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime

import duckdb


def _now() -> datetime:
    return datetime.now()


@contextmanager
def tracked_run(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    target_table: str,
    params: dict | None = None,
):
    """记录一次采集任务：通过 tracker.add(n) 汇报写入行数。

    正常退出记 success；异常记 failed（错误信息截断到 2000 字符）并原样抛出，
    保证血缘表永远能反映真实执行状态。
    """
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO ingest_run VALUES (?, ?, ?, ?, 'running', NULL, NULL, ?, NULL)",
        [run_id, source, target_table, json.dumps(params or {}, ensure_ascii=False), _now()],
    )

    class Tracker:
        rows = 0

        def add(self, n: int) -> None:
            self.rows += n

    tracker = Tracker()
    try:
        yield run_id, tracker
    except Exception as exc:
        conn.execute(
            "UPDATE ingest_run SET status='failed', error=?, finished_at=? WHERE run_id=?",
            [str(exc)[:2000], _now(), run_id],
        )
        raise
    else:
        conn.execute(
            "UPDATE ingest_run SET status='success', rows_written=?, finished_at=? WHERE run_id=?",
            [tracker.rows, _now(), run_id],
        )
