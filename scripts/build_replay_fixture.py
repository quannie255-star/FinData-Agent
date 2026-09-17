"""把 2026-09-15 真实仓库快照固化为回放评测 fixture（复盘 P0c）。

用法（在真实仓库所在的机器上跑一次；产物入库进 git）：
    uv run python scripts/build_replay_fixture.py

产出 `eval/fixtures/replay_20260915/*.parquet`（行情/估值/指数/股票池/日历）。
corporate_event **不入** fixture：回放时的知识层由 seeds.py 的 4 笔停牌种子
确定性重建——仓库里的事件表会随采集增长，种子才是这笔评测钉死的事实。

安全约定：只读打开生产仓库；build_eval_fixture.py 误写生产库的事故
（v0.7.1）不能再发生。合成语料证明「机制对」，真实回放证明「事实够」，
两者缺一不可（docs/reviews/2026-09-15-attribution.md）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from findata.config import settings  # noqa: E402
from findata.core.db import connect  # noqa: E402

OUT_DIR = ROOT / "eval" / "fixtures" / "replay_20260915"
ASOF = "2026-09-15"

_TABLES = ("stock_daily", "valuation_daily", "index_daily", "stock_universe", "trading_calendar")


def build(out_dir: Path = OUT_DIR, db_path: str | None = None) -> int:
    target = Path(db_path) if db_path else Path(settings.db_path)
    if not target.exists():
        print(f"真实仓库不存在：{target}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    # read_only=True：本脚本对生产仓库只有读权限，写入只发生在 fixture 目录
    with connect(str(target), read_only=True) as conn:
        asof = conn.execute("SELECT max(date) FROM stock_daily").fetchone()[0]
        if str(asof) != ASOF:
            print(
                f"警告：仓库最新日期 {asof} 与钉死的回放日 {ASOF} 不同；"
                "回放用 as_of 截断，新增数据不影响结论，但建议核对后重新固化",
                file=sys.stderr,
            )
        for table in _TABLES:
            dest = out_dir / f"{table}.parquet"
            conn.execute(
                f"COPY (SELECT * FROM {table}) TO '{dest.as_posix()}' (FORMAT PARQUET)"
            )
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:18s} {n:>7d} 行 -> {dest.relative_to(ROOT)}")
    print(f"回放 fixture 固化完成：{out_dir.relative_to(ROOT)}（asof={ASOF}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
