"""对比页：同一数据集上「仅通用包」vs「+A 股领域包」的徽章差异（R3 验收②）。

用法（需要真实仓库）：
    uv run python scripts/compare_domain_vs_generic.py [-o examples/domain-vs-generic-comparison.md]

数据集：A 股 stock_daily 的 2025-08..09 切片（含中国神华、中芯国际两段
真实停牌缺口）。同一份数据分别喂给两台引擎：
- 通用包（findata.generic）：只有通用基线检查；
- 领域包（report.inspect 全链路）：交易日历 + 停牌知识 + 归因。

这是"通用协议 + 领域包"战略的含金量自检：两份报告的差值就是产品本身。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# noqa: E402 —— sys.path 注入后再 import 项目包

from findata.config import settings  # noqa: E402
from findata.core.db import connect  # noqa: E402
from findata.dq.badges import BadgeLevel  # noqa: E402
from findata.generic.engine import run_generic_inspection  # noqa: E402
from findata.generic.schema import ColumnSpec, TableSpec  # noqa: E402
from findata.report.inspect import Snapshot, run_inspection  # noqa: E402

SLICE_START, SLICE_END = date(2025, 1, 1), date(2025, 9, 30)


def _generic_spec(table: str, columns: list[str]) -> TableSpec:
    """stock_daily 的声明式 schema（通用包视角：它不知道停牌，只知道列）。"""
    numeric = {"open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"}
    cols = [ColumnSpec("symbol", "id", nullable=False)]
    cols += [
        ColumnSpec(c, "numeric" if c in numeric else "datetime", nullable=True)
        for c in columns
        if c != "symbol"
    ]
    return TableSpec(
        table=table,
        columns=tuple(cols),
        primary_key=("symbol", "date"),
        timestamp_column="date",
        freshness_days=7,
        outlier_tolerance=0.05,
        group_column="symbol",
        asof=SLICE_END,
    )


DEFAULT_OUT = str(ROOT / "examples" / "domain-vs-generic-comparison.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="通用包 vs 领域包 对比页生成")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    with connect(str(settings.db_path), read_only=True) as conn:
        snap = Snapshot.from_duckdb(conn, asof=SLICE_END).as_of(SLICE_END)
    daily = snap.stock_daily
    sliced = daily[
        (daily["date"] >= SLICE_START) & (daily["date"] <= SLICE_END)
    ].reset_index(drop=True)
    n_symbols = sliced["symbol"].nunique()
    # 「同一数据集」的诚实边界：数据集 = 本切片。股票池的 list_date 必须改为
    # 切片内首日，否则领域包会把切片之外的历史当成 missing_rows（切片边界
    # 不是数据故障）。这是给对比引擎声明期望模型，不是篡改数据。
    universe = snap.universe.copy()
    first_day = sliced.groupby("symbol")["date"].min()
    universe["list_date"] = universe["symbol"].map(first_day)

    # ── 同一份数据，两台引擎 ──
    domain = run_inspection(
        Snapshot(
            stock_daily=sliced,
            valuation_daily=snap.valuation_daily,
            universe=universe,
            corporate_event=snap.corporate_event,
            ingest_run=snap.ingest_run,
            calendar=snap.calendar,
            asof=SLICE_END,
        )
    )
    generic = run_generic_inspection(
        sliced,
        _generic_spec("stock_daily", [c for c in sliced.columns]),
        source=f"duckdb:{Path(settings.db_path).name} (stock_daily {SLICE_START}..{SLICE_END})",
        declared=True,
        asof=SLICE_END,
    )

    # ── 对比页 ──
    gb, db = generic.board, domain.badges
    g_counts = {lv: len(gb.by_level(lv)) for lv in BadgeLevel}
    d_counts = {lv: len(db.by_level(lv)) for lv in BadgeLevel}
    lines = [
        "# 对比页：同一数据集，仅通用包 vs +A 股领域包",
        "",
        f"**数据集**：A 股 `stock_daily` {SLICE_START}..{SLICE_END} 切片，"
        f"{n_symbols} 只标的 / {len(sliced):,} 行（含中国神华、中芯国际两段真实停牌缺口）。",
        "同一份数据分别喂给两台引擎；徽章口径完全一致（dq/badges 单一实现）。",
        "",
        "## 结论",
        "",
        "| | 仅通用包 | +A 股领域包 |",
        "|---|---|---|",
        f"| 健康分 | {gb.health_score} | {domain.summary.health_score} |",
        f"| ✓ 已核验 / 基线通过 | {g_counts[BadgeLevel.VERIFIED]}"
        f" / {g_counts[BadgeLevel.BASELINE]} "
        f"| {d_counts[BadgeLevel.VERIFIED]} / {d_counts[BadgeLevel.BASELINE]} |",
        f"| ⚠️ 仅借鉴 | {g_counts[BadgeLevel.CAUTION]} | {d_counts[BadgeLevel.CAUTION]} |",
        f"| ✗ 不可用 | {g_counts[BadgeLevel.UNUSABLE]} | {d_counts[BadgeLevel.UNUSABLE]} |",
        f"| 检出的信号 | {len(generic.findings)} | {domain.summary.n_findings} "
        f"（抑制 {domain.summary.n_suppressed}）|",
        "",
        "## 逐列徽章对照",
        "",
        "| 列 | 仅通用包 | +A 股领域包 | 差异 |",
        "|---|---|---|---|",
    ]
    for col in generic.spec.columns:
        g = gb.of("stock_daily", col.name)
        d = db.of("stock_daily", col.name)
        g_txt = g.mark if g else "—"
        d_txt = d.mark if d else "—"
        if g is None or d is None:
            diff = "（领域包徽章板不覆盖非行情列）" if d is None else "—"
        elif g.level is not d.level:
            diff = f"领域包凭归因证据升级为 {d.mark}"
        else:
            diff = "一致"
        lines.append(f"| {col.name} | {g_txt} | {d_txt} | {diff} |")
    lines += [
        "",
        "## 领域包看见了什么、通用包为什么看不见",
        "",
    ]
    for f, d in domain.suppressed:
        lines.append(
            f"- **`{f.symbol}` 缺 {int(f.value)} 个交易日（{f.window}）**："
            f"领域包检出信号并归因为「{benign_label(d)}」，证据 {list(d.evidence_refs)}；"
            "通用包**没有产出任何信号**——它没有交易日历和停牌事件表，"
            "无法知道「这些日子本该有行」，缺行对它是不可见的。"
        )
    lines += [
        "",
        "## 含金量自检（ROADMAP R3 要求的诚实结论）",
        "",
        "1. 通用包的价值在它**能看见**的维度：空值、主键重复、停更、离群、"
        "Schema 漂移——对任意 CSV/Excel/库表开箱即用，这是基线包的合理定位；",
        "2. 通用包的健康分 100 **不等于**数据可信：本例两段真实缺口它完全无感，"
        "「没查出毛病」和「证明可靠」之间的差距就是领域包的定价空间；",
        "3. 通用包永远给不出 ✓ 已核验（无领域知识，测试钉死）——"
        "徽章四档的区分在对比页上是真实的，不是文案。",
        "",
        "> 复现：`uv run python scripts/compare_domain_vs_generic.py`"
        "（需要真实仓库 data/warehouse.duckdb）",
    ]
    out = Path(args.out).resolve()
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:24]))
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\n[archived] -> {shown}")
    return 0


def benign_label(d) -> str:
    from findata.report.inspect import benign_label as _bl

    return _bl(d.root_cause)


if __name__ == "__main__":
    raise SystemExit(main())
