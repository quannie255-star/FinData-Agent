"""把 logs/daily/*.log 汇总成一份可核对的运行史。

为什么单独有这个脚本
--------------------
`logs/daily/` 里的日志是调度器**当场写下**的，不是重跑出来的：09-15 那次运行的
抑制=0 / 健康分=72 已经**无法复现**（重跑只会得到修复后的结果）。所以这段历史
本身就是证据，本脚本的职责只是把它读出来，**不做任何推算、不补任何缺失值**。

口径（引用这里任何数字都要一起引）
----------------------------------
- **一行 = 一次运行**，不是一天。同一自然日会被跑多次（自动一次 + 手动重跑），
  它们追加进同一个日志文件。早先按「文件」列一行时，`20260915.log` 里 6 次启动
  只呈现了第 1 次 —— 表上一个数字都没错，但读者会把「1 次」当成「1 天」。
- 数字一律取自该次运行的「巡检完成」行；**取不到就写「未记录」**，不许顶成 0
  （顶成 0 会让"没跑到巡检"和"抑制为 0"看起来一样）。没到巡检的启动单列，
  不混进统计。
- 启动时刻/耗时取日志自报的本地时刻，只到秒。
- 分段用**游程编码**（相邻同值合并计数），只陈述顺序，不主张因果。

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
# 做游程编码用的三元组：只取这三个，因为它们是"修复前后"故事的全部内容。
KEY_FIELDS = ("告警", "抑制", "健康分")


def parse_runs(path: pathlib.Path) -> list[dict[str, str]]:
    """把一个日志文件里的**每一次**运行拆成一行；缺的字段留「未记录」。

    切块依据是「每日管线开始」这行 —— 它是每次运行的唯一锚点，且由管线自己写，
    不依赖文件大小或时间间隔。文件里根本没有开始行时，返回一行全「未记录」：
    「读不出来」和「没跑过」必须长得不一样。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    starts = list(RE_START.finditer(text))
    if not starts:
        return [
            {
                "日志": path.name,
                "序": "-",
                "启动时刻": MISSING,
                "耗时": MISSING,
                **{k: MISSING for k in FIELDS},
            }
        ]

    rows: list[dict[str, str]] = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[m.start() : end]
        inspect = RE_INSPECT.search(block)
        duration = RE_DURATION.search(block)
        row = {
            "日志": path.name,
            "序": str(i + 1) if len(starts) > 1 else "",
            "启动时刻": m.group(1)[11:],
            "耗时": f"{duration.group(1)}s" if duration else MISSING,
        }
        if inspect:
            row.update(dict(zip(FIELDS, inspect.groups(), strict=True)))
        else:
            row.update({k: MISSING for k in FIELDS})
        rows.append(row)
    return rows


def collect() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for p in sorted(LOGS.glob("20*.log")):
        rows.extend(parse_runs(p))
    return rows


def run_length(values: list[tuple[str, str, str]]) -> list[tuple[tuple[str, str, str], int]]:
    """游程编码：相邻相同值合并成「值 × 次数」。"""
    groups: list[tuple[tuple[str, str, str], int]] = []
    for v in values:
        if groups and groups[-1][0] == v:
            groups[-1] = (groups[-1][0], groups[-1][1] + 1)
        else:
            groups.append((v, 1))
    return groups


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
        "> 口径：**一行 = 一次运行**（同一天会被跑多次，追进同一个日志文件）；数字只取自该次",
        "> 运行的「巡检完成」行，**取不到就写「未记录」**，不顶成 0。",
        "> `asof` 是巡检观察日，不是运行日 —— 非交易日管线照跑，`asof` 会停在最近一个交易日。",
        "",
        "## 逐次运行记录",
        "",
        "| 日志 | 序 | 启动时刻 | 耗时 | asof | 标的 | 信号 | 告警 | 抑制 | 健康分 |",
        "| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        out.append(
            f"| `{r['日志']}` | {r['序']} | {r['启动时刻']} | {r['耗时']} | {r['asof']} | "
            f"{r['标的']} | {r['信号']} | {r['告警']} | {r['抑制']} | {r['健康分']} |"
        )

    scored = [r for r in rows if r["健康分"] != MISSING]
    unscored = [r for r in rows if r["健康分"] == MISSING]
    out += ["", "## 按时间顺序的分段（游程编码）", ""]
    if not scored:
        out.append("没有任何一次运行记录到健康分 —— 这是「未记录」，不是「没有运行」。")
    else:
        groups = run_length([tuple(r[k] for k in KEY_FIELDS) for r in scored])
        out += [
            f"- 共 **{len(rows)} 次启动**，其中 **{len(scored)} 次**走到了巡检、"
            f"**{len(unscored)} 次**没有（未记录，单列，不计入下面）。",
            "",
            "```",
            " → ".join(
                f"(告警 {a} / 抑制 {s} / 健康分 {h}) × {n}" for (a, s, h), n in groups
            ),
            "```",
            "",
            "读数方式：**分段点只陈述顺序，不主张因果** —— 它与 R1.0 根因修复的时间吻合",
            "（见 `docs/reviews/2026-09-15-attribution.md`），但本表不替读者下这个结论。",
            "**每一段内部取值完全相同**（游程长度 >1）才叫「连续 N 次没有回退」；",
            "段长 1 只是一个观测点，不能叫「稳定」。",
        ]
        if unscored:
            out += [
                "",
                "未走到巡检的启动："
                + "、".join(f"`{r['日志']}` {r['启动时刻']}" for r in unscored)
                + "（当时的表现见 `logs/daily/README.md`）。",
            ]

    out += [
        "",
        "## 已知边界",
        "",
        "- 单一数据源（akshare）、25 只标的、外部用户 0 ⇒ 这是**自用规模**，不是产品规模。",
        "- 调度任务本身**不在本仓库里注册**（Windows 计划任务），仓库里看不到它的定义；",
        "  能看到的只有它每次运行留下的日志。**没有任何一个数字来自重跑。**",
        "- 「运行次数」≠「运行天数」：上表有多行属于同一自然日（含手动重跑与调试），",
        "  引用时要说是**次**还是**天**。",
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
