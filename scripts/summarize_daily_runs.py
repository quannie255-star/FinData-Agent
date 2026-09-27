"""把 logs/daily/*.log 汇总成一份可核对的运行史。

为什么单独有这个脚本
--------------------
`logs/daily/` 里的日志是调度器**当场写下**的，不是重跑出来的：09-15 那次运行的
抑制=0 / 健康分=72 已经**无法复现**（重跑只会得到修复后的结果）。所以这段历史
本身就是证据，本脚本的职责只是把它读出来，**不做任何推算、不补任何缺失值**。

口径（引用这里任何数字都要一起引）
----------------------------------
- 数字一律取自日志的「巡检完成」行；**取不到就写「未记录」**，不许顶成 0
  （顶成 0 会让"那天没跑"和"那天抑制为 0"看起来一样）。
- 启动时刻取日志自报的本地时刻，只到秒。
- 耗时取日志自报的「管线结束，耗时 Ns」，含采集 + 事件 + 巡检 + 报告 + 归档。
- 同一自然日可能被跑多次（自动一次 + 手动重跑），本表按**日志文件**列，不合并。

用法
----
    uv run python scripts/summarize_daily_runs.py
    uv run python scripts/summarize_daily_runs.py --out examples/daily-pipeline-run-history.md
"""

from __future__ import annotations

import argparse
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs" / "daily"

# 巡检行是唯一的数字来源；匹配失败一律走「未记录」，不猜。
RE_INSPECT = re.compile(
    r"巡检完成：asof=(\S+) 标的=(\d+) 信号=(\d+) 告警=(\d+) 抑制=(\d+) 健康分=([\d.]+)"
)
RE_START = re.compile(r"每日管线开始 (\S+)")
RE_DURATION = re.compile(r"管线结束，耗时 (\d+)s")

FIELDS = ("asof", "标的", "信号", "告警", "抑制", "健康分")
MISSING = "未记录"


def parse_one(path: pathlib.Path) -> dict[str, str]:
    """读一个日志文件，返回一行记录；缺的字段留「未记录」。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    start = RE_START.search(text)
    duration = RE_DURATION.search(text)
    inspect = RE_INSPECT.search(text)
    row = {
        "日志": path.name,
        "启动时刻": start.group(1)[11:] if start else MISSING,
        "耗时": f"{duration.group(1)}s" if duration else MISSING,
    }
    if inspect:
        row.update(dict(zip(FIELDS, inspect.groups(), strict=True)))
    else:
        row.update({k: MISSING for k in FIELDS})
    return row


def collect() -> list[dict[str, str]]:
    return [parse_one(p) for p in sorted(LOGS.glob("20*.log"))]


def render(rows: list[dict[str, str]]) -> str:
    """输出 Markdown。刻意把「未记录」原样打出来，不美化。"""
    out = [
        "# 每日管线运行史（真实运行证据）",
        "",
        "> **这份表不是重跑出来的。** 它由调度器每次运行时写入 `logs/daily/`，本文件是",
        "> `uv run python scripts/summarize_daily_runs.py --out 本文件` 的产物。",
        "> 为什么必须入库：**这段历史不可复现** —— 2026-09-15 那次的「抑制 0 / 健康分 72」",
        "> 是一个已经不存在了的状态，今天重跑只会得到修复后的结果。",
        ">",
        "> 口径：数字只取自日志的「巡检完成」行；**取不到就写「未记录」**，不顶成 0",
        "> （顶成 0 会让「那天没跑」和「那天抑制为 0」长得一样）。",
        "> `asof` 是巡检观察日，不是运行日 —— 非交易日管线照跑，`asof` 会停在最近一个交易日。",
        "",
        "## 全量运行记录",
        "",
        "| 日志 | 启动时刻 | 耗时 | asof | 标的 | 信号 | 告警 | 抑制 | 健康分 |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        out.append(
            f"| `{r['日志']}` | {r['启动时刻']} | {r['耗时']} | {r['asof']} | {r['标的']} | "
            f"{r['信号']} | {r['告警']} | {r['抑制']} | {r['健康分']} |"
        )

    scored = [r for r in rows if r["健康分"] != MISSING]
    out += ["", "## 前 / 后对照", ""]
    if not scored:
        out.append("没有任何一次运行记录到健康分 —— 这是「未记录」，不是「没有运行」。")
    else:
        first, rest = scored[0], scored[1:]
        out += [
            f"- **第一次运行**（`{first['日志']}`，asof={first['asof']}）："
            f"告警 {first['告警']} / 抑制 {first['抑制']} / 健康分 {first['健康分']}",
            f"- **其后 {len(rest)} 次**："
            f"告警 {sorted({r['告警'] for r in rest})} / "
            f"抑制 {sorted({r['抑制'] for r in rest})} / "
            f"健康分 {sorted({r['健康分'] for r in rest})}（去重后的取值集合）",
            "",
            "读数方式：第一次运行就是「真实数据首次考试」的原始现场；之后是根因修复后的状态。",
            "**取值集合只有一个元素**才叫「连续 N 次没有回退」；有多个取值就必须逐个列出来。",
        ]
    out += [
        "",
        "## 已知边界",
        "",
        "- 单一数据源（akshare）、25 只标的、外部用户 0 ⇒ 这是**自用规模**，不是产品规模。",
        "- 调度任务本身**不在本仓库里注册**（Windows 计划任务），仓库里看不到它的定义；",
        "  能看到的只有它每次运行留下的日志。",
        "- `reports/daily/` 仍被 gitignore（那是**可重生成**的产物）；",
        "  本文件与 `logs/daily/` 不是，因为它们是**历史**。",
        "- 报告的**形态**见 `examples/daily-report-snapshot-2026-09-24.md`（快照，不是活产物）。",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总 logs/daily/*.log 成运行史")
    ap.add_argument("--out", default="", help="输出 Markdown 路径；不给就打到 stdout")
    args = ap.parse_args()

    rows = collect()
    text = render(rows)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
        print(f"写入 {args.out}（{len(rows)} 次运行）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
