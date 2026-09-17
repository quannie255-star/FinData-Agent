"""停牌历史种子入库：corporate_event 的知识层初始化（幂等，可重复执行）。

用法：
    uv run python scripts/seed_events.py

为什么需要：akshare 只能采「除权除息历史 + 当日停牌快照」，历史停牌的
起止区间拿不到；而 2026-09-15 复盘证明这张表为空时，停牌抑制规则在
生产上是死代码（4 条真实缺口全部误报为漏采）。4 笔种子来自公告级人工
核实，来源链接见 docs/reviews/2026-09-15-attribution.md。

每日管线的 --events 步骤也会自动补种子（见 ingest_corporate_events），
本脚本用于首次初始化与手工修复。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from findata.config import settings  # noqa: E402
from findata.core.db import connect  # noqa: E402
from findata.domains.finance.seeds import (  # noqa: E402
    SUSPENSION_SEEDS,
    apply_suspension_seeds,
)


def main() -> int:
    with connect(settings.dsn) as conn:
        n = apply_suspension_seeds(conn)
        rows = conn.execute(
            "SELECT symbol, date, end_date FROM corporate_event "
            "WHERE kind = 'suspension' ORDER BY date"
        ).fetchall()
    print(f"停牌种子：本次新增 {n} 条，表内现有 {len(rows)} 条"
          f"（种子共 {len(SUSPENSION_SEEDS)} 笔）")
    for s, d, e in rows:
        print(f"  {s}  {d} .. {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
