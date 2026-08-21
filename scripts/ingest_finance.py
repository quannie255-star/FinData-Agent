"""金融数据采集入口。

用法：
    uv run python scripts/ingest_finance.py              # 增量采集（默认）
    uv run python scripts/ingest_finance.py --full       # 全量重采
    uv run python scripts/ingest_finance.py --symbols 600519,000858
"""

from __future__ import annotations

import argparse
import logging
import sys

from findata.config import settings
from findata.core.db import connect
from findata.domains.finance.ingest import UNIVERSE, ingest_all


def main() -> int:
    parser = argparse.ArgumentParser(description="采集金融数据到 DuckDB 仓库")
    parser.add_argument("--full", action="store_true", help="全量重采（默认增量）")
    parser.add_argument("--symbols", type=str, help="逗号分隔的股票代码，默认全部股票池")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )

    universe = UNIVERSE
    if args.symbols:
        wanted = {s.strip() for s in args.symbols.split(",")}
        universe = [u for u in UNIVERSE if u[0] in wanted]
        missing = wanted - {u[0] for u in universe}
        if missing:
            print(f"警告：以下代码不在股票池中，已忽略：{sorted(missing)}", file=sys.stderr)

    conn = connect(settings.dsn)
    try:
        summary = ingest_all(conn, full_refresh=args.full, universe=universe)
        print(summary.report())

        stats = conn.execute(
            """
            SELECT 'stock_daily' AS t, count(*), count(DISTINCT symbol) FROM stock_daily
            UNION ALL
            SELECT 'index_daily', count(*), count(DISTINCT symbol) FROM index_daily
            UNION ALL
            SELECT 'valuation_daily', count(*), count(DISTINCT symbol) FROM valuation_daily
            """
        ).fetchall()
        print("\n仓库现状：")
        for table, rows, symbols in stats:
            print(f"  {table:16s} {rows:>10d} 行  {symbols:>3d} 个标的")
        print(f"  最新交易日: {conn.execute('SELECT max(date) FROM stock_daily').fetchone()[0]}")
    finally:
        conn.close()
    return 0 if not summary.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
