"""通用数据接入：CSV / Excel / DuckDB / SQLite → DataFrame。

与金融域采集（domains/finance）的分工：这里是**读既有文件/库**的薄接入层，
不做网络采集、不做幂等入库——通用包的第一句话就是"把你已有的表给我，
我告诉你它能信到什么程度"。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_table(path: str | Path, table: str | None = None) -> tuple[pd.DataFrame, str]:
    """按扩展名/协议装载表。返回 (DataFrame, 数据源描述)。

    - *.csv / *.txt  → CSV（utf-8，自动识别分隔符失败时回退逗号）
    - *.xlsx / *.xls → Excel（第一个 sheet 或 table 指定的 sheet 名）
    - *.duckdb / *.ddb → DuckDB 库中的表（table 必填）
    - *.db / *.sqlite / *.sqlite3 → SQLite 库中的表（table 必填）

    库类来源不给 table 名直接报错——猜"第一张表"在可信场景是反面模式。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix in (".csv", ".txt"):
        return _read_csv_lenient(p), f"csv:{p.name}"
    if suffix in (".xlsx", ".xls"):
        sheet = table or 0
        return pd.read_excel(p, sheet_name=sheet), f"excel:{p.name}#{table or 'sheet1'}"
    if suffix in (".duckdb", ".ddb"):
        if not table:
            raise ValueError("DuckDB 来源必须用 --table 指明表名（不做猜测）")
        import duckdb

        con = duckdb.connect(str(p), read_only=True)
        try:
            return con.execute(f'SELECT * FROM "{table}"').df(), f"duckdb:{p.name}:{table}"
        finally:
            con.close()
    if suffix in (".db", ".sqlite", ".sqlite3"):
        if not table:
            raise ValueError("SQLite 来源必须用 --table 指明表名（不做猜测）")
        import sqlite3

        con = sqlite3.connect(str(p))
        try:
            return pd.read_sql_query(f'SELECT * FROM "{table}"', con), f"sqlite:{p.name}:{table}"
        finally:
            con.close()
    raise ValueError(
        f"不识别的数据源后缀 {suffix!r}。支持：csv/txt、xlsx/xls、duckdb、db/sqlite"
    )


def _read_csv_lenient(p: Path) -> pd.DataFrame:
    """CSV 宽容读取：先按默认解析，失败再试 sep=None（python 引擎自动嗅探）。"""
    try:
        return pd.read_csv(p)
    except Exception:  # noqa: BLE001 — pandas 对脏 CSV 抛的异常种类太多，兜底重试
        return pd.read_csv(p, sep=None, engine="python")
