"""DuckDB 仓库连接与建表。

所有业务表遵循统一约定：
- 主键去重（symbol+date 等），保证幂等 upsert
- 数值单位在列注释中声明，语义层引用时不再猜单位
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from findata.config import settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id       VARCHAR PRIMARY KEY,
    source       VARCHAR,          -- 数据来源接口，如 ak.stock_zh_a_hist
    target_table VARCHAR,
    params       JSON,
    status       VARCHAR,          -- running / success / failed
    rows_written BIGINT,
    error        TEXT,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stock_universe (
    symbol      VARCHAR PRIMARY KEY,   -- 6 位代码，如 600519
    name        VARCHAR,
    industry    VARCHAR,
    listed_board VARCHAR               -- main / star / chinext
);

-- 个股日线（前复权，新浪口径）。单位：价格=元，成交量=股，成交额=元，换手率=小数
CREATE TABLE IF NOT EXISTS stock_daily (
    symbol     VARCHAR,
    date       DATE,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     DOUBLE,
    amount     DOUBLE,
    pct_chg    DOUBLE,                -- 涨跌幅 %
    turnover   DOUBLE,                -- 换手率 %
    PRIMARY KEY (symbol, date)
);

-- 指数日线（新浪源，无成交额字段）。单位：点位=点
CREATE TABLE IF NOT EXISTS index_daily (
    symbol  VARCHAR,                  -- 000300 沪深300 / 000001 上证 / 399006 创业板指
    date    DATE,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  DOUBLE,
    PRIMARY KEY (symbol, date)
);

-- 个股估值指标（东财口径）。total_mv 单位=元
CREATE TABLE IF NOT EXISTS valuation_daily (
    symbol    VARCHAR,
    date      DATE,
    pe        DOUBLE,                -- 静态市盈率
    pe_ttm    DOUBLE,
    pb        DOUBLE,
    total_mv  DOUBLE,                -- 总市值（元）
    PRIMARY KEY (symbol, date)
);

-- 查询血缘（M2 语义层起）：记录每次 metric_query 的 SQL 与结果，供引用溯源
CREATE TABLE IF NOT EXISTS query_run (
    run_id      VARCHAR PRIMARY KEY,
    metric      VARCHAR,
    params      JSON,
    sql         TEXT,
    value       DOUBLE,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP
);

-- 交叉验证血缘（M4 可信层起）：记录主路径与验证路径的比对结果
CREATE TABLE IF NOT EXISTS verify_run (
    run_id        VARCHAR PRIMARY KEY,
    query_run_id  VARCHAR,           -- 关联 query_run.run_id，便于一次查询回追验证
    metric        VARCHAR,
    params        JSON,
    main_value    DOUBLE,
    verify_value  DOUBLE,
    status        VARCHAR,           -- verified / mismatch / not_verifiable
    sql           TEXT,
    note          TEXT,               -- 验证口径说明 / 不一致原因
    finished_at   TIMESTAMP
);
"""

# 老库兼容迁移：已存在表用 ALTER 补列（SCHEMA_SQL 的 IF NOT EXISTS 不会改旧表）
MIGRATIONS = [
    "ALTER TABLE verify_run ADD COLUMN IF NOT EXISTS query_run_id VARCHAR",
    "ALTER TABLE verify_run ADD COLUMN IF NOT EXISTS note TEXT",
]


def connect(db_path: str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开（必要时创建）仓库并确保 schema 存在。测试里可指向临时文件。"""
    path = db_path or settings.dsn
    if not read_only:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(path, read_only=read_only)
    if not read_only:
        conn.execute(SCHEMA_SQL)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                # 列已存在（不同 duckdb 版本报错文案不同），忽略即可
                pass
    return conn


def upsert_dataframe(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    df,
    key_columns: list[str],
    tmp_name: str = "_tmp_upsert_df",
) -> int:
    """幂等写入：先删掉与 df 键冲突的旧行再插入，返回写入行数。"""
    conn.register(tmp_name, df)
    try:
        join_cond = " AND ".join(f"t.{c} = m.{c}" for c in key_columns)
        conn.execute("BEGIN")
        conn.execute(
            f"DELETE FROM {table} AS t "
            f"WHERE EXISTS (SELECT 1 FROM {tmp_name} m WHERE {join_cond})"
        )
        conn.execute(f"INSERT INTO {table} SELECT * FROM {tmp_name}")
        conn.execute("COMMIT")
        return len(df)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.unregister(tmp_name)
