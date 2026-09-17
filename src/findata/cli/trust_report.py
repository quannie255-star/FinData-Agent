"""findata trust-report：任意 CSV / Excel / DuckDB / SQLite 表 → 带徽章可信报告。

用法：
    findata-trust-report data.csv
    findata-trust-report data.csv --schema schema.yaml -o report.md --html report.html
    findata-trust-report warehouse.duckdb --table stock_daily
    findata-trust-report orders.db --table orders --strict   # 有 ✗ 不可用则退出码 1

退出码：0 = 无 ✗ 徽章；1 = 存在 ✗（--strict 时）或运行失败；2 = 输入错误。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from findata.dq.badges import BadgeLevel
from findata.generic.engine import render_html, render_markdown, run_generic_inspection
from findata.generic.io import load_table
from findata.generic.schema import from_yaml


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="findata-trust-report", description="任意数据表 → 带徽章可信报告（通用包）"
    )
    parser.add_argument("path", help="数据文件：.csv/.xlsx/.duckdb/.db(.sqlite)")
    parser.add_argument(
        "--table", default=None, help="库类来源的表名（csv/excel 可用于 sheet 名）"
    )
    parser.add_argument(
        "--schema", default=None, help="声明式 schema YAML（缺省自动推断，检查会降级）"
    )
    parser.add_argument(
        "-o", "--out", default=None, help="Markdown 报告输出路径（缺省打印到 stdout）"
    )
    parser.add_argument("--html", default=None, help="HTML 报告输出路径")
    parser.add_argument(
        "--strict", action="store_true", help="存在 ✗ 不可用徽章时退出码置 1（门禁用法）"
    )
    args = parser.parse_args(argv)

    try:
        df, source = load_table(args.path, args.table)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.schema:
        spec = from_yaml(args.schema)
        declared = True
    else:
        from findata.generic.schema import infer as infer_spec

        table_name = args.table or Path(args.path).stem
        spec = infer_spec(table_name, df)
        declared = False

    report = run_generic_inspection(df, spec, source=source, declared=declared)
    md = render_markdown(report)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"报告已写出：{args.out}", file=sys.stderr)
    else:
        print(md)
    if args.html:
        Path(args.html).write_text(render_html(report), encoding="utf-8")
        print(f"HTML 已写出：{args.html}", file=sys.stderr)

    unusable = len(report.board.by_level(BadgeLevel.UNUSABLE))
    print(
        f"[trust-report] {report.table}: {report.n_rows} 行 · 健康分 {report.board.health_score}"
        f" · 信号 {len(report.findings)} · ✗ {unusable}",
        file=sys.stderr,
    )
    if args.strict and unusable:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
