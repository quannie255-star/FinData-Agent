"""P0 复盘：对 2026-09-15 日报的 7 条真实告警逐条归因。

内部证据（仓库自证）：
- 缺失窗口是否"全市场都有数据、只有它缺" → symbol 级缺口的直接证据
- corporate_event 有没有停牌记录、ingest_run 有没有 failed run
- 漂移窗口是否"全市场同步放量" → 全市场行情事件（合法）的直接证据
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# noqa: E402 —— 项目包必须等 sys.path 注入后才能 import
import pandas as pd  # noqa: E402

from findata.config import settings  # noqa: E402
from findata.core.db import connect  # noqa: E402
from findata.dq.probes import ProbeContext  # noqa: E402
from findata.report.inspect import Snapshot, run_inspection  # noqa: E402

DB = Path(settings.db_path)

with connect(str(DB), read_only=True) as conn:
    # ── 元数据层体检：抑制规则靠这三张表吃饭 ──
    for t in ("trading_calendar", "corporate_event", "ingest_run", "stock_universe"):
        try:
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"[meta] {t}: {n} 行")
        except Exception as e:
            print(f"[meta] {t}: 不存在（{type(e).__name__}）")
    try:
        kinds = conn.execute(
            "SELECT kind, count(*) FROM corporate_event GROUP BY kind"
        ).fetchall()
        print(f"[meta] corporate_event 事件类型分布: {kinds}")
    except Exception:
        pass
    try:
        runs = conn.execute(
            "SELECT status, count(*) FROM ingest_run GROUP BY status"
        ).fetchall()
        print(f"[meta] ingest_run 状态分布: {runs}")
    except Exception:
        pass

    snapshot = Snapshot.from_duckdb(conn)

print(f"\n[asof] {snapshot.asof}  标的数={snapshot.universe['symbol'].nunique()} "
      f"行数={len(snapshot.stock_daily)}")

result = run_inspection(snapshot)
print(f"\n[repro] 信号={result.summary.n_findings} 告警={result.summary.n_alerts} "
      f"抑制={result.summary.n_suppressed} 健康分={result.summary.health_score}")

ctx: ProbeContext = snapshot.to_probe_context()
daily = snapshot.stock_daily
cal = snapshot.calendar
by_symbol = {s: g for s, g in daily.groupby("symbol")}

# 预计算：每个 (symbol, date) 是否有行，用于交叉验证
have = daily.groupby("date")["symbol"].apply(set).to_dict()


def gap_internal_evidence(sym: str, start: date, end: date) -> None:
    print(f"\n=== missing_rows 归因：{sym} {start}..{end} ===")
    span = cal.trading_days(start, end)
    print(f"  交易日历判定窗口含 {len(span)} 个交易日")
    g = by_symbol.get(sym)
    dates = set(g["date"].tolist()) if g is not None else set()
    missing = [d for d in span if d not in dates]
    print(f"  实缺 {len(missing)} 天: {[str(d) for d in missing[:12]]}")
    # 全市场对照：这些天其他标的是否都有数据
    for d in missing[:6]:
        others = have.get(d, set())
        if len(others) < 25:
            lack = sorted(others ^ set(by_symbol))[:6]
            print(f"    {d}: 全市场 {len(others)}/25 有数据，缺的: {lack}")
        else:
            print(f"    {d}: 全市场 25/25 有数据")
    # 停牌记录
    ev = snapshot.corporate_event
    if not ev.empty:
        sub = ev[ev["symbol"] == sym]
        print(f"  corporate_event 中 {sym} 共 {len(sub)} 条事件"
              + (f"，类型分布 {sub['kind'].value_counts().to_dict()}" if len(sub) else ""))
    else:
        print("  corporate_event 为空表 → 停牌抑制规则在生产上是死代码")
    # 采集血缘
    runs = snapshot.ingest_run
    print(f"  ingest_run 共 {len(runs)} 行"
          + (f"，failed: {(runs['status'] == 'failed').sum()}" if not runs.empty else ""))


def drift_cross_section(start: date, end: date, sym: str, ratio: float) -> None:
    print(f"\n=== distribution_drift 归因：{sym} {start}..{end} (ratio={ratio}) ===")
    # 对每只票算同窗口的 10日均量/前30日均量，看是否全市场同步放量
    stats = []
    for s, g in by_symbol.items():
        g = g.sort_values("date").reset_index(drop=True)
        vol = g["volume"].to_numpy(dtype=float)
        dates = g["date"].tolist()
        idx = [i for i, d in enumerate(dates) if start <= d <= end]
        if not idx or len(vol) < 40:
            continue
        lo, hi = min(idx), max(idx)
        if lo < 30:
            continue
        recent = float(pd.Series(vol[lo : hi + 1]).mean())
        hist = float(pd.Series(vol[max(0, lo - 30) : lo]).mean())
        if hist > 0:
            stats.append((s, recent / hist))
    stats.sort(key=lambda x: -x[1])
    n5 = sum(1 for _, r in stats if r >= 5.0)
    n3 = sum(1 for _, r in stats if r >= 3.0)
    n2 = sum(1 for _, r in stats if r >= 2.0)
    print(
        f"  同窗口全市场放量倍数: ≥5x {n5} 只 / ≥3x {n3} 只"
        f" / ≥2x {n2} 只（共 {len(stats)} 只可比）"
    )
    top = ", ".join(f"{s}={r:.1f}x" for s, r in stats[:5])
    print(f"  放量前五: {top}")


for a in result.alerts:
    f, d = a.finding, a.diagnosis
    parsed = None
    if ".." in f.window:
        h, t = f.window.split("..", 1)
        try:
            parsed = (date.fromisoformat(h), date.fromisoformat(t))
        except ValueError:
            pass
    windows = [parsed] if parsed else []
    for extra in a.extra_windows:
        if extra and ".." in extra:
            h, t = extra.split("..", 1)
            try:
                windows.append((date.fromisoformat(h), date.fromisoformat(t)))
            except ValueError:
                pass
    for start, end in windows:
        if d.root_cause.value == "missing_rows":
            gap_internal_evidence(f.symbol, start, end)
        elif d.root_cause.value == "distribution_drift":
            drift_cross_section(start, end, f.symbol, f.value)
        else:
            print(f"\n=== 其他告警 {f.symbol} {f.window} {d.root_cause.value} ===")
    print(f"  → 规则结论: {d.root_cause.value} conf={d.confidence} | {d.explanation}")
