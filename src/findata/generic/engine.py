"""通用可信引擎编排与报告：load → 探针 → 归因 → 徽章 → 报告。

与金融引擎（report/inspect.py）同构但独立：金融域的 ProbeContext/探针
深度绑定交易日历与公司事件，通用包一分钱领域知识都不该有——两者共用
的只有徽章模型与聚合口径（dq/badges，单一实现）。

诚实边界的落地点：run_generic_inspection 产出的徽章里 **不可能出现
✓ 已核验**——归因器无 BENIGN_ 路由（generic.triage），聚合函数便永远
走不到 VERIFIED 分支。通用包的健康分满格是「全部基线通过」，那是
"没查出毛病"，不是"证明可靠"。
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from findata.dq.badges import (
    BADGE_MARK,
    BadgeBoard,
    BadgeLevel,
    MetricBadge,
    aggregate_column_badge,
)
from findata.dq.models import Diagnosis, Finding
from findata.generic.probes import GenericContext, run_probes
from findata.generic.schema import TableSpec
from findata.generic.triage import GenericTriage


@dataclass
class GenericReport:
    """一次通用可信检查的完整产出。"""

    table: str
    source: str
    n_rows: int
    n_cols: int
    asof: date
    declared: bool
    spec: TableSpec
    findings: list[Finding]
    diagnoses: list[Diagnosis]
    board: BadgeBoard
    skipped: list[str] = field(default_factory=list)  # 显式降级的检查项

    @property
    def active(self) -> list[tuple[Finding, Diagnosis]]:
        by_key = {d.finding_key: d for d in self.diagnoses}
        return [(f, by_key[f.key]) for f in self.findings if f.key in by_key]


def run_generic_inspection(
    df: pd.DataFrame,
    spec: TableSpec,
    source: str = "memory",
    declared: bool = True,
    asof: date | None = None,
) -> GenericReport:
    """跑一轮通用可信检查。纯函数：相同输入 → 相同报告。"""
    asof = asof or spec.asof or date.today()
    ctx = GenericContext(df=df, spec=spec, asof=asof, declared=declared)
    findings = run_probes(ctx)
    diagnoses = GenericTriage().diagnose_all(findings, ctx)
    board, skipped = _build_board(ctx, findings, diagnoses)
    return GenericReport(
        table=spec.table,
        source=source,
        n_rows=len(df),
        n_cols=len(df.columns),
        asof=asof,
        declared=declared,
        spec=spec,
        findings=findings,
        diagnoses=diagnoses,
        board=board,
        skipped=skipped,
    )


def _build_board(
    ctx: GenericContext, findings: list[Finding], diagnoses: list[Diagnosis]
) -> tuple[BadgeBoard, list[str]]:
    """逐列徽章。行级信号（主键重复/停更）映射到全部声明列——一行脏，
    每一列的聚合值都不可信（与金融域同一映射原则）。"""
    skipped: list[str] = []
    by_key = {d.finding_key: d for d in diagnoses}
    entries: dict[str, list[tuple[Finding, Diagnosis | None]]] = {}

    for f in findings:
        cols = ctx.spec.column_names if f.symbol == "*" else (f.symbol,)
        for col in cols:
            entries.setdefault(col, []).append((f, by_key.get(f.key)))

    # 每列实际跑过的检查项（✓ 基线通过的证据 = 检查项清单）
    table_checks = ["完整性"]
    if ctx.declared:
        table_checks.append("Schema 一致")
    else:
        skipped.append("Schema 一致检查未执行（schema 为自动推断，未声明）")
    if ctx.spec.primary_key and all(c in ctx.df.columns for c in ctx.spec.primary_key):
        table_checks.append("唯一性")
    else:
        skipped.append("唯一性检查未执行（未声明 primary_key——不知道什么该唯一，就查不了重复）")
    if ctx.spec.timestamp_column and ctx.spec.timestamp_column in ctx.df.columns:
        table_checks.append("新鲜度")
    else:
        skipped.append("新鲜度检查未执行（未声明 timestamp_column——不知道时间锚点，就查不了停更）")
    numeric_checks = [*table_checks, "离群值"]

    badges: list[MetricBadge] = []
    for col in ctx.spec.columns:
        if col.name not in ctx.df.columns:
            # 声明列缺失时数据里根本没有这列，发徽章是无依据声明——跳过
            continue
        checks = numeric_checks if col.kind == "numeric" else table_checks
        badges.append(
            aggregate_column_badge(
                ctx.spec.table, col.name, entries.get(col.name, []), tuple(checks)
            )
        )
    return BadgeBoard(badges=badges), skipped


# ─────────────────────────── 报告渲染 ───────────────────────────


def render_markdown(r: GenericReport) -> str:
    b = r.board
    counts = {lvl: len(b.by_level(lvl)) for lvl in BadgeLevel}
    lines = [
        f"# 通用数据可信报告 · {r.table}",
        "",
        f"- **数据源**：{r.source}",
        f"- **规模**：{r.n_rows:,} 行 × {r.n_cols} 列",
        f"- **观察日**：{r.asof} · schema {'声明式' if r.declared else '自动推断（建议补声明）'}",
        "",
        "## 结论",
        "",
        f"**健康分 {b.health_score}（{_grade(b.health_score)}）** · "
        f"徽章 ✓{counts[BadgeLevel.VERIFIED] + counts[BadgeLevel.BASELINE]} "
        f"⚠️{counts[BadgeLevel.CAUTION]} ✗{counts[BadgeLevel.UNUSABLE]} · "
        f"信号 {len(r.findings)} 条",
        "",
        "> **通用包上限声明**：无领域知识，最高只能给「✓ 基线通过」（= 没查出"
        "毛病，不是证明可靠）。「✓ 已核验」需要领域包的归因背书。",
        "",
    ]
    if r.skipped:
        lines += ["**检查覆盖降级**："] + [f"- {s}" for s in r.skipped] + [""]

    lines += [
        "## 指标可信度（逐列徽章）",
        "",
        "| 列 | 类型 | 徽章 | 依据 |",
        "|---|---|---|---|",
    ]
    for col in r.spec.columns:
        badge = b.of(r.table, col.name)
        if badge is None:
            lines.append(f"| {col.name} | {col.kind} | — | 列缺失，无法检查 |")
            continue
        lines.append(f"| {col.name} | {col.kind} | {badge.mark} | {badge.summary} |")
    lines.append("")

    details: list[str] = []
    for badge in b.badges:
        if badge.level is not BadgeLevel.BASELINE and badge.evidence:
            items = "".join(f"\n  - `{src}` — {txt}" for src, txt in badge.evidence)
            details.append(
                f"<details>\n<summary>证据链：{badge.metric_key}（{badge.mark}）</summary>"
                f"\n{items}\n</details>"
            )
    if details:
        lines += details + [""]

    if r.findings:
        by_key = {d.finding_key: d for d in r.diagnoses}
        lines += [
            f"## 待处理信号（{len(r.findings)}）",
            "",
            "| 探针 | 位置 | 级别 | 根因 | 说明 |",
            "|---|---|---|---|---|",
        ]
        for f in r.findings:
            d = by_key[f.key]
            lines.append(
                f"| {f.probe} | {f.symbol} | {f.severity_hint.value} | "
                f"`{d.root_cause.value}` | {d.explanation} |"
            )
        lines.append("")
    else:
        lines += ["本轮无待处理信号（不等于数据可靠——见上方检查覆盖与上限声明）。", ""]
    return "\n".join(lines)


def render_html(r: GenericReport) -> str:
    b = r.board
    rows = []
    order = {
        BadgeLevel.UNUSABLE: 0,
        BadgeLevel.CAUTION: 1,
        BadgeLevel.VERIFIED: 2,
        BadgeLevel.BASELINE: 3,
    }
    for col in sorted(r.spec.columns, key=lambda c: (order.get(
            (b.of(r.table, c.name).level if b.of(r.table, c.name) else BadgeLevel.BASELINE), 9),
            c.name)):
        badge = b.of(r.table, col.name)
        if badge is None:
            rows.append(f"<tr><td>{_html.escape(col.name)}</td><td>{col.kind}</td>"
                        "<td>—</td><td>列缺失，无法检查</td></tr>")
            continue
        fg = {
            BadgeLevel.VERIFIED: "#059669", BadgeLevel.BASELINE: "#059669",
            BadgeLevel.CAUTION: "#b45309", BadgeLevel.UNUSABLE: "#b91c1c",
        }[badge.level]
        rows.append(
            f"<tr><td><code>{_html.escape(col.name)}</code></td><td>{col.kind}</td>"
            f'<td><span style="color:{fg};font-weight:600">{_html.escape(badge.mark)}</span></td>'
            f"<td>{_html.escape(badge.summary)}</td></tr>"
        )
    skipped = "".join(f"<li>{_html.escape(s)}</li>" for s in r.skipped)
    findings = ""
    if r.findings:
        by_key = {d.finding_key: d for d in r.diagnoses}
        rows2 = "".join(
            f"<tr><td>{f.probe}</td><td>{_html.escape(str(f.symbol))}</td>"
            f"<td>{f.severity_hint.value}</td><td><code>{by_key[f.key].root_cause.value}</code></td>"
            f"<td>{_html.escape(by_key[f.key].explanation)}</td></tr>"
            for f in r.findings
        )
        findings = (
            "<div class='sec'><h2>待处理信号</h2>"
            "<table><thead><tr><th>探针</th><th>位置</th><th>级别</th><th>根因</th>"
            f"<th>说明</th></tr></thead><tbody>{rows2}</tbody></table></div>"
        )
    counts = {lvl: len(b.by_level(lvl)) for lvl in BadgeLevel}
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>通用数据可信报告 · {_html.escape(r.table)}</title>
<style>
body{{margin:0;padding:28px 20px;background:#f7f8fa;color:#1f2328;
font:14px/1.6 -apple-system,"Segoe UI","PingFang SC",sans-serif}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:21px;margin:0 0 4px}}
.sub{{color:#6b7280;font-size:13px;margin-bottom:18px}}
.sec{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;
padding:16px 20px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top}}
th{{color:#6b7280;font-weight:500;font-size:12px}}
code{{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}}
.note{{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:10px 14px;
font-size:13px;color:#b45309;margin-bottom:14px}}
.skip{{color:#6b7280;font-size:13px}}
</style></head><body><div class="wrap">
<h1>通用数据可信报告 · {_html.escape(r.table)}</h1>
<div class="sub">{_html.escape(r.source)} · {r.n_rows:,} 行 × {r.n_cols} 列 · 观察日 {r.asof}
 · schema {'声明式' if r.declared else '自动推断'}</div>
<div class="sec"><h2>结论</h2>
<p style="font-size:17px;font-weight:600;margin:0 0 6px">健康分 {b.health_score}
（{_grade(b.health_score)}）</p>
<p class="skip">徽章 ✓{counts[BadgeLevel.VERIFIED] + counts[BadgeLevel.BASELINE]}
 ⚠️{counts[BadgeLevel.CAUTION]} ✗{counts[BadgeLevel.UNUSABLE]}
 · 信号 {len(r.findings)} 条</p></div>
<div class="note"><b>通用包上限声明</b>：无领域知识，最高只能给「✓ 基线通过」
（= 没查出毛病，不是证明可靠）。「✓ 已核验」需要领域包的归因背书。</div>
{f"<div class='sec'><h2>检查覆盖降级</h2><ul class='skip'>{skipped}</ul></div>" if skipped else ""}
<div class="sec"><h2>指标可信度（逐列徽章）</h2>
<table><thead><tr><th>列</th><th>类型</th><th>徽章</th><th>依据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="skip">徽章口径：{' · '.join(BADGE_MARK[lv] for lv in BadgeLevel)}</p></div>
{findings}
</div></body></html>"""


def _grade(score: float) -> str:
    if score >= 95:
        return "健康"
    if score >= 80:
        return "关注"
    if score >= 60:
        return "异常"
    return "严重"
