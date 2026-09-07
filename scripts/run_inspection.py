"""每日巡检 CLI。

    uv run python scripts/inspect.py                    # 用合成语料演示（零数据可跑）
    uv run python scripts/inspect.py --source warehouse # 巡检真实 DuckDB 仓库
    uv run python scripts/inspect.py --format html --open

设计取舍：默认跑合成语料而不是真实仓库，因为真实仓库需要先采集几分钟且依赖外网。
让 clone 仓库的人第一条命令就能看到完整产出，比"先去准备数据"重要得多。
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from findata.config import settings
from findata.core.db import connect
from findata.report import (
    Snapshot,
    render_html,
    render_markdown,
    run_inspection,
)

REPORT_DIR = Path("reports")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Findata 每日数据质量巡检")
    p.add_argument(
        "--source",
        choices=("synthetic", "warehouse"),
        default="synthetic",
        help="数据来源：synthetic=确定性语料（默认，零依赖）；warehouse=真实 DuckDB 仓库",
    )
    p.add_argument("--seed", type=int, default=20240102, help="合成语料随机种子")
    p.add_argument("--asof", help="观察日 YYYY-MM-DD，默认取数据中最新日期")
    p.add_argument("--format", choices=("md", "html", "both"), default="both")
    p.add_argument("--out", default=str(REPORT_DIR), help="报告输出目录")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开 HTML")
    p.add_argument("--quiet", action="store_true", help="不打印报告摘要")
    args = p.parse_args(argv)

    if args.source == "warehouse":
        db = Path(settings.db_path)
        if not db.exists():
            print(f"仓库不存在：{db}\n先运行 uv run python scripts/ingest_finance.py")
            return 1
        from datetime import date

        asof = date.fromisoformat(args.asof) if args.asof else None
        with connect(str(db), read_only=True) as conn:
            snapshot = Snapshot.from_duckdb(conn, asof=asof)
        if snapshot.stock_daily.empty:
            print("仓库为空，无数据可巡检。")
            return 1
    else:
        snapshot = Snapshot.from_synthetic(seed=args.seed)

    result = run_inspection(snapshot)
    s = result.summary

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if args.format in ("md", "both"):
        md = out_dir / f"inspection-{s.asof}.md"
        md.write_text(render_markdown(result), encoding="utf-8")
        written.append(md)
    if args.format in ("html", "both"):
        hp = out_dir / f"inspection-{s.asof}.html"
        hp.write_text(render_html(result), encoding="utf-8")
        written.append(hp)

    if not args.quiet:
        print(f"观察日 {s.asof} · 覆盖 {s.n_symbols} 只标的 / {s.n_rows:,} 行")
        print(
            f"健康分 {s.health_score}（{s.grade()}）· "
            f"信号 {s.n_findings} → 告警 {s.n_alerts} · "
            f"抑制 {s.n_suppressed}（降噪 {s.noise_reduction:.0%}）"
        )
        for a in result.alerts:
            print(
                f"  [{a.diagnosis.severity.value}] {a.finding.symbol} "
                f"{a.finding.window} {a.diagnosis.root_cause.value} — {a.diagnosis.explanation}"
            )
    for path in written:
        print(f"报告已生成：{path}")

    if args.open:
        html_files = [p for p in written if p.suffix == ".html"]
        if html_files:
            webbrowser.open(html_files[0].resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
