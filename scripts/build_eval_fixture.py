"""CI 用 fixture：合成最小的离线数据让 golden set 可评测。

CI runner 无 akshare 网络，生产数据采集脚本在这里跑不通。
本脚本生成受控的合成数据，确保 golden.yaml 里的指标能跑出已知结果。
仅用于评测门禁；生产仓库走 scripts/ingest_finance.py。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from findata.config import settings
from findata.core.db import connect

FIXTURE_DB = "data/eval_fixture.duckdb"


def build(db_path: str | None = None) -> None:
    target = db_path or FIXTURE_DB
    # 防护：fixture 第一步会清空 stock_daily/valuation_daily，
    # 误指向生产仓库会直接毁掉真实数据，这里直接拒绝。
    resolved = Path(target).resolve()
    if resolved == Path(settings.dsn).resolve():
        raise SystemExit(
            f"拒绝执行：{resolved} 是生产仓库路径。"
            "fixture 构建会清空其中的行情表，请使用 data/eval_fixture.duckdb 等独立路径。"
        )
    conn = connect(target)

    # 清空（fixture 表），保证幂等
    conn.execute("DELETE FROM stock_daily")
    conn.execute("DELETE FROM valuation_daily")

    # 茅台：3 个交易日，close 从 100 涨到 121（前复权序列自洽：100 → 110 → 121）
    daily = pd.DataFrame(
        {
            "symbol": ["600519"] * 3,
            "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": [100.0, 110.0, 121.0],
            "volume": 1e6,
            "amount": 1e8,
            "pct_chg": [0.0, 10.0, 10.0],
            "turnover": 1.0,
        }
    )
    conn.register("_d", daily)
    conn.execute("INSERT INTO stock_daily SELECT * FROM _d")
    conn.unregister("_d")

    # 五粮液：2 个交易日
    wuliangye = pd.DataFrame(
        {
            "symbol": ["000858"] * 2,
            "date": [date(2024, 1, 2), date(2024, 1, 3)],
            "open": 70.0,
            "high": 73.0,
            "low": 69.0,
            "close": [70.0, 72.0],
            "volume": 1e6,
            "amount": 1e8,
            "pct_chg": [0.0, 2.857],
            "turnover": 1.0,
        }
    )
    conn.register("_w", wuliangye)
    conn.execute("INSERT INTO stock_daily SELECT * FROM _w")
    conn.unregister("_w")

    # 宁德时代
    catl = pd.DataFrame(
        {
            "symbol": ["300750"],
            "date": [date(2024, 1, 3)],
            "open": 380.0,
            "high": 390.0,
            "low": 380.0,
            "close": [385.0],
            "volume": 1e6,
            "amount": 1e8,
            "pct_chg": [0.0],
            "turnover": 1.0,
        }
    )
    conn.register("_c", catl)
    conn.execute("INSERT INTO stock_daily SELECT * FROM _c")
    conn.unregister("_c")

    # 茅台估值
    val = pd.DataFrame(
        {
            "symbol": ["600519"],
            "date": [date(2024, 1, 4)],
            "pe": 20.0,
            "pe_ttm": 18.5,
            "pb": 5.0,
            "total_mv": 2e12,
        }
    )
    conn.register("_v", val)
    conn.execute("INSERT INTO valuation_daily SELECT * FROM _v")
    conn.unregister("_v")

    # universe（list_date 给足历史，避免被归因器当成新股短历史）
    universe = pd.DataFrame(
        {
            "symbol": ["600519", "000858", "300750"],
            "name": ["贵州茅台", "五粮液", "宁德时代"],
            "industry": ["白酒", "白酒", "动力电池"],
            "listed_board": ["main", "main", "chinext"],
            "list_date": [date(2001, 8, 27), date(1998, 4, 3), date(2011, 6, 10)],
        }
    )
    conn.register("_u", universe)
    conn.execute("INSERT INTO stock_universe SELECT * FROM _u")
    conn.unregister("_u")

    conn.close()
    print(f"fixture 写入：{target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 CI 评测用合成 fixture")
    parser.add_argument(
        "--db", default=None, help="输出 duckdb 路径（默认 data/eval_fixture.duckdb）"
    )
    args = parser.parse_args()
    build(db_path=args.db)