"""生成数据质量看板（单文件 HTML，离线可用）。

用法：
    uv run python scripts/build_dashboard.py
    uv run python scripts/build_dashboard.py --days 40 --with-eval --open
    uv run python scripts/build_dashboard.py --source warehouse
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import date
from pathlib import Path

from findata.config import settings
from findata.core.db import connect
from findata.report import Snapshot, render_dashboard
from findata.report.dashboard import build_dashboard

OUT_DIR = Path("reports")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="生成数据质量看板")
    p.add_argument("--source", choices=("synthetic", "warehouse"), default="synthetic")
    p.add_argument("--seed", type=int, default=20240102)
    p.add_argument("--days", type=int, default=20, help="趋势回溯的交易日数")
    p.add_argument("--asof", help="观察日 YYYY-MM-DD")
    p.add_argument("--with-eval", action="store_true", help="附带系统自评结果")
    p.add_argument("--out", default=str(OUT_DIR))
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)

    if args.source == "warehouse":
        db = Path(settings.db_path)
        if not db.exists():
            print(f"仓库不存在：{db}\n先运行 uv run python scripts/ingest_finance.py")
            return 1
        asof = date.fromisoformat(args.asof) if args.asof else None
        with connect(str(db), read_only=True) as conn:
            snapshot = Snapshot.from_duckdb(conn, asof=asof)
        if snapshot.stock_daily.empty:
            print("仓库为空，无可看板数据。")
            return 1
    else:
        snapshot = Snapshot.from_synthetic(seed=args.seed)
        if args.asof:
            snapshot = snapshot.as_of(date.fromisoformat(args.asof))

    report = None
    if args.with_eval:
        from findata.eval.runner import run_evaluation

        report = run_evaluation(fault_seed=args.seed).report

    data = build_dashboard(snapshot, n_days=args.days, eval_report=report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"dashboard-{data.current.summary.asof}.html"
    out.write_text(render_dashboard(data), encoding="utf-8")

    s = data.current.summary
    print(
        f"观察日 {s.asof} · 健康分 {s.health_score}（{s.grade()}）· "
        f"告警 {s.n_alerts} · 抑制 {s.n_suppressed}"
    )
    print(f"趋势 {len(data.trend)} 个交易日 · 看板已生成：{out}")

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
