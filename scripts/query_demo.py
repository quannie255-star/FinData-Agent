"""语义层查询演示：问一句 → 出数字 → 带 run_id 溯源。

用法：
    uv run python scripts/query_demo.py close_price --symbol 600519
    uv run python scripts/query_demo.py pct_change \\
        --symbol 600519 --start 2024-01-01 --end 2024-12-31
    uv run python scripts/query_demo.py valuation_pe_ttm --symbol 600519
    uv run python scripts/query_demo.py --list      # 列出所有指标
"""

from __future__ import annotations

import argparse

from findata.config import settings
from findata.core.db import connect
from findata.semantic.catalog import MetricCatalog
from findata.semantic.executor import format_result
from findata.semantic.tools import list_metrics, metric_query


def main() -> int:
    parser = argparse.ArgumentParser(description="语义层指标查询演示")
    parser.add_argument("metric", nargs="?", help="指标名")
    parser.add_argument("--symbol", help="股票代码，如 600519")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--list", action="store_true", help="列出可用指标")
    args = parser.parse_args()

    catalog = MetricCatalog()

    if args.list:
        print("可用指标：")
        for m in list_metrics(catalog):
            print(f"  {m['name']:18s} {m['label']:16s} {m['description']}")
        return 0

    if not args.metric:
        parser.print_help()
        return 1

    params: dict = {}
    if args.symbol:
        params["symbol"] = args.symbol
    if args.start:
        params["start"] = args.start
    if args.end:
        params["end"] = args.end

    conn = connect(settings.dsn)
    try:
        result = metric_query(conn, args.metric, params, catalog)
        print(format_result(result))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
