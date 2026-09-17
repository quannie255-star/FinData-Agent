"""巡检报告渲染：Markdown（值班可读）与 HTML（演示/归档）。

报告的叙事顺序是刻意设计的：先给结论和健康分，再给要处理的告警，
最后才列出被抑制的合法事件。
把"没告警的东西"放在最后，是为了让值班的人第一眼看到的是行动项，
而不是一屏噪音——这正是本项目区别于裸检测脚本的地方。
"""

from __future__ import annotations

import html
from datetime import datetime

from findata.dq.badges import BadgeLevel, MetricBadge
from findata.dq.models import Severity
from findata.report.inspect import ROUTES, Alert, InspectionResult, benign_label

_SEV_LABEL = {
    Severity.P0: "P0 阻断",
    Severity.P1: "P1 重要",
    Severity.P2: "P2 提示",
}


def _sev(severity: Severity) -> str:
    return _SEV_LABEL.get(severity, severity.value)


def _fmt(v: float) -> str:
    if v != v:  # NaN
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4g}"


def knowledge_line(s) -> str:
    """知识层状态行（P0d）：抑制能力降级必须对值班的人可见。

    事件表为空时，停牌/除权归因没有事实可依，误报无法被抑制——
    这时报告必须显式降级，而不是让读者对着未抑制的误报猜原因。
    """
    if s.knowledge_degraded:
        return (
            "⚠️ **知识层未上线**：`corporate_event` 0 行——停牌/除权归因无事实可依，"
            "该类误报无法抑制（修复见 `docs/reviews/2026-09-15-attribution.md` P0a）"
        )
    latest = s.knowledge_latest_event or "未知"
    return f"**知识层**：`corporate_event` {s.knowledge_event_rows} 行 · 最新事件 {latest}"


def badge_counts(board) -> str:
    """结论行的徽章汇总：✓N ⚠️N ✗N（已核验与基线通过都算 ✓，档位在表格里区分）。"""
    if not board.badges:
        return ""
    ok = len(board.by_level(BadgeLevel.VERIFIED)) + len(board.by_level(BadgeLevel.BASELINE))
    c = len(board.by_level(BadgeLevel.CAUTION))
    u = len(board.by_level(BadgeLevel.UNUSABLE))
    return f"✓{ok} ⚠️{c} ✗{u}"


def _badge_section_md(result: InspectionResult) -> list[str]:
    """逐指标徽章表（Markdown）。基线通过的清单内联，疑点/故障证据可展开。"""
    board = result.badges
    if not board.badges:
        return []
    lines = [
        f"## 指标可信度（{len(board.badges)} 项，逐数字带徽章）",
        "",
        "| 表 | 指标 | 徽章 | 依据 |",
        "|---|---|---|---|",
    ]
    details: list[str] = []
    for b in board.badges:
        lines.append(f"| {b.table} | `{b.column}` | {b.mark} | {b.summary} |")
        if b.level is not BadgeLevel.BASELINE and b.evidence:
            items = "".join(f"\n  - `{src}` — {txt}" for src, txt in b.evidence)
            details.append(
                f"<details>\n<summary>证据链：{b.metric_key}（{b.mark}）</summary>\n{items}\n</details>"
            )
    lines.append("")
    lines.extend(details)
    if details:
        lines.append("")
    return lines


def _badge_rows_html(badges: list[MetricBadge]) -> str:
    rows = []
    for b in badges:
        cls = {
            BadgeLevel.VERIFIED: "t-ok",
            BadgeLevel.BASELINE: "t-ok",
            BadgeLevel.CAUTION: "t-p1",
            BadgeLevel.UNUSABLE: "t-p0",
        }[b.level]
        evidence = ""
        if b.evidence:
            items = "".join(
                f"<li><code>{html.escape(src)}</code> — {html.escape(txt)}</li>"
                for src, txt in b.evidence
            )
            evidence = (
                f"<details><summary>证据链</summary><ul class='ev'>{items}</ul></details>"
                if b.level is not BadgeLevel.BASELINE
                else f"<span class='route'>{' · '.join(src for src, _ in b.evidence)}</span>"
            )
        rows.append(
            "<tr>"
            f"<td>{html.escape(b.table)}</td>"
            f"<td><code>{html.escape(b.column)}</code></td>"
            f'<td><span class="tag {cls}">{b.mark}</span></td>'
            f"<td>{html.escape(b.summary)}{evidence}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _badge_section_html(result: InspectionResult) -> str:
    board = result.badges
    if not board.badges:
        return ""
    order = {
        BadgeLevel.UNUSABLE: 0,
        BadgeLevel.CAUTION: 1,
        BadgeLevel.VERIFIED: 2,
        BadgeLevel.BASELINE: 3,
    }
    badges = sorted(board.badges, key=lambda b: (order[b.level], b.table, b.column))
    return (
        '<div class="sec"><h2>指标可信度 · 逐数字徽章</h2>'
        '<p class="route">✓ 已核验 = 领域知识归因背书 · ✓ 基线通过 = 通用检查通过 · '
        "⚠️ 仅借鉴 = 有疑点需人工判断 · ✗ 不可用 = 真实故障（口径见 dq/badges.py）</p>"
        "<table><thead><tr><th>表</th><th>指标</th><th>徽章</th><th>依据 / 证据</th></tr></thead>"
        f"<tbody>{_badge_rows_html(badges)}</tbody></table>"
        '<style>details.ev, td details { margin-top:4px; font-size:12px; } '
        "td details ul.ev { margin:4px 0 0 16px; padding:0; }</style>"
        "</div>"
    )


def render_markdown(
    result: InspectionResult, header_extra: str = "", footer_extra: str = ""
) -> str:
    s = result.summary
    lines: list[str] = [
        "# 数据质量巡检报告",
        "",
        f"- **观察日**：{s.asof}",
        f"- **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- **覆盖**：{s.n_symbols} 只标的 / {s.n_rows:,} 行行情",
        f"- {knowledge_line(s)}",
        "",
    ]
    if header_extra:
        lines.append(header_extra)
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.extend(
        [
            "## 结论",
            "",
            f"**健康分 {s.health_score}（{s.grade()}）** · "
            f"信号 {s.n_findings} → 告警 {s.n_alerts} · "
            f"抑制误报 {s.n_suppressed}（降噪 {s.noise_reduction:.0%}）"
            f" · 徽章 {badge_counts(result.badges)}",
            "",
        ]
    )

    lines.extend(_badge_section_md(result))

    if not result.alerts:
        lines += ["本轮无待处理告警。", ""]
    else:
        lines += [f"## 待处理告警（{s.n_alerts}）", ""]
        for severity in (Severity.P0, Severity.P1, Severity.P2):
            group = [a for a in result.alerts if a.diagnosis.severity is severity]
            if not group:
                continue
            route, action = ROUTES[severity]
            lines += [f"### {_sev(severity)} · {route}", "", f"> {action}", ""]
            lines.append("| 标的 | 表 | 窗口 | 根因 | 观测值(阈值) | 判断依据 |")
            lines.append("|---|---|---|---|---|---|")
            for a in group:
                f, d = a.finding, a.diagnosis
                lines.append(
                    f"| `{f.symbol}` | {f.table} | {a.windows} | "
                    f"`{d.root_cause.value}` | {_fmt(f.value)} ({_fmt(f.threshold)}) | "
                    f"{d.explanation} |"
                )
            lines.append("")

    if result.suppressed:
        lines += [f"## 已抑制（{s.n_suppressed}）", ""]
        lines += [
            "以下信号形态上像故障，但归因后确认为合法业务事件，**不告警**：",
            "",
            "| 标的 | 窗口 | 事件 | 说明 |",
            "|---|---|---|---|",
        ]
        for f, d in result.suppressed:
            lines.append(
                f"| `{f.symbol}` | {f.window} | {benign_label(d.root_cause)} | {d.explanation} |"
            )
        lines.append("")

    if s.by_root_cause:
        lines += ["## 根因分布", ""]
        for cause, n in sorted(s.by_root_cause.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{cause}` × {n}")
        lines.append("")

    if footer_extra:
        lines += ["---", "", footer_extra, ""]
    return "\n".join(lines)


_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据质量巡检报告 · {asof}</title>
<style>
  :root {{
    --bg:#f7f8fa; --card:#fff; --line:#e5e7eb; --text:#1f2328;
    --muted:#6b7280; --p0:#dc2626; --p1:#d97706; --p2:#2563eb; --ok:#059669;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px 20px; background:var(--bg); color:var(--text);
    font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:960px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:24px; }}
  .card {{ background:var(--card); border:1px solid var(--line);
    border-radius:10px; padding:14px 16px; }}
  .card .k {{ color:var(--muted); font-size:12px; }}
  .card .v {{ font-size:24px; font-weight:600; margin-top:2px; }}
  .card .n {{ color:var(--muted); font-size:12px; }}
  .sec {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:18px 20px; margin-bottom:16px; }}
  .sec h2 {{ font-size:15px; margin:0 0 12px; display:flex; align-items:center; gap:8px; }}
  .tag {{ font-size:11px; padding:2px 8px; border-radius:999px; color:#fff; font-weight:600; }}
  .t-p0 {{ background:var(--p0); }} .t-p1 {{ background:var(--p1); }}
  .t-p2 {{ background:var(--p2); }} .t-ok {{ background:var(--ok); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:8px 10px; vertical-align:top;
    border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; font-size:12px; }}
  code {{ background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .route {{ color:var(--muted); font-size:12px; margin:-6px 0 10px; }}
  .kstatus {{ font-size:12px; padding:6px 12px; border-radius:6px; margin:-14px 0 18px;
    color:var(--muted); background:var(--card); border:1px solid var(--line); }}
  .empty {{ color:var(--muted); }}
  .bar {{ display:flex; align-items:center; gap:8px; margin:6px 0; }}
  .bar .lb {{ width:190px; font-size:12px; color:var(--muted); }}
  .bar .tr {{ flex:1; background:#f3f4f6; border-radius:4px; height:8px; overflow:hidden; }}
  .bar .fl {{ height:100%; background:#9ca3af; }}
  .bar .ct {{ width:28px; text-align:right; font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>数据质量巡检报告</h1>
  <div class="sub">观察日 {asof} · 生成于 {now} · 覆盖 {n_symbols} 只标的 / {n_rows} 行行情</div>
  {knowledge_html}

  <div class="cards">
    <div class="card">
      <div class="k">健康分</div><div class="v">{score}</div><div class="n">{grade}</div>
    </div>
    <div class="card">
      <div class="k">待处理告警</div><div class="v">{n_alerts}</div>
      <div class="n">P0 {p0} · P1 {p1} · P2 {p2}</div>
    </div>
    <div class="card">
      <div class="k">抑制误报</div><div class="v">{n_sup}</div><div class="n">合法业务事件</div>
    </div>
    <div class="card">
      <div class="k">降噪率</div><div class="v">{noise}</div>
      <div class="n">{n_findings} 个信号收敛为 {n_alerts}</div>
    </div>
  </div>

  {alert_sections}
  {suppress_section}
  {badge_section}
  {cause_section}
</div>
</body>
</html>
"""


def _alert_rows(alerts: list[Alert]) -> str:
    rows = []
    for a in alerts:
        f, d = a.finding, a.diagnosis
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(f.symbol))}</code></td>"
            f"<td>{html.escape(f.table)}</td>"
            f"<td>{html.escape(a.windows)}</td>"
            f"<td><code>{html.escape(d.root_cause.value)}</code></td>"
            f"<td>{_fmt(f.value)} <span style='color:#6b7280'>({_fmt(f.threshold)})</span></td>"
            f"<td>{html.escape(d.explanation)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _knowledge_html(s) -> str:
    """知识层状态的 HTML 形态。降级时必须醒目（琥珀色警示条）。"""
    if s.knowledge_degraded:
        return (
            '<div class="kstatus" style="color:#b45309;background:#fef3c7;'
            'border:1px solid #f59e0b">⚠️ 知识层未上线：corporate_event 0 行——'
            "停牌/除权归因无事实可依，该类误报无法抑制</div>"
        )
    latest = s.knowledge_latest_event or "未知"
    return (
        '<div class="kstatus">知识层：corporate_event '
        f"{s.knowledge_event_rows} 行 · 最新事件 {latest}</div>"
    )


def render_html(result: InspectionResult, extra_sections: str = "") -> str:
    s = result.summary
    parts: list[str] = ['<div class="sec"><h2>待处理告警</h2>']
    if not result.alerts:
        parts.append('<p class="empty">本轮无待处理告警。</p>')
    for severity in (Severity.P0, Severity.P1, Severity.P2):
        group = [a for a in result.alerts if a.diagnosis.severity is severity]
        if not group:
            continue
        route, action = ROUTES[severity]
        cls = {Severity.P0: "t-p0", Severity.P1: "t-p1", Severity.P2: "t-p2"}[severity]
        parts.append(
            f'<h2 style="margin-top:18px"><span class="tag {cls}">{_sev(severity)}</span>'
            f'<span style="font-weight:500">{html.escape(route)}</span></h2>'
            f'<div class="route">{html.escape(action)}</div>'
            "<table><thead><tr><th>标的</th><th>表</th><th>窗口</th>"
            "<th>根因</th><th>观测值(阈值)</th><th>判断依据</th></tr></thead>"
            f"<tbody>{_alert_rows(group)}</tbody></table>"
        )
    alert_sec = "".join(parts) + "</div>"

    suppress_sec = ""
    if result.suppressed:
        rows = "\n".join(
            "<tr>"
            f"<td><code>{html.escape(str(f.symbol))}</code></td>"
            f"<td>{html.escape(f.window)}</td>"
            f'<td><span class="tag t-ok">{html.escape(benign_label(d.root_cause))}</span></td>'
            f"<td>{html.escape(d.explanation)}</td>"
            "</tr>"
            for f, d in result.suppressed
        )
        suppress_sec = (
            '<div class="sec"><h2>已抑制 · 合法业务事件</h2>'
            '<p class="route">形态与故障一致，但归因后确认合理，不产生告警。</p>'
            "<table><thead><tr><th>标的</th><th>窗口</th><th>事件</th><th>说明</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    cause_sec = ""
    if s.by_root_cause:
        top = max(s.by_root_cause.values())
        bars = "\n".join(
            '<div class="bar"><div class="lb">'
            f"<code>{html.escape(c)}</code></div>"
            f'<div class="tr"><div class="fl" style="width:{int(n / top * 100)}%"></div></div>'
            f'<div class="ct">{n}</div></div>'
            for c, n in sorted(s.by_root_cause.items(), key=lambda kv: -kv[1])
        )
        cause_sec = f'<div class="sec"><h2>根因分布</h2>{bars}</div>'

    return _HTML_TMPL.format(
        asof=s.asof,
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        n_symbols=s.n_symbols,
        n_rows=f"{s.n_rows:,}",
        score=s.health_score,
        grade=s.grade(),
        n_alerts=s.n_alerts,
        p0=s.by_severity.get("P0", 0),
        p1=s.by_severity.get("P1", 0),
        p2=s.by_severity.get("P2", 0),
        n_sup=s.n_suppressed,
        noise=f"{s.noise_reduction:.0%}",
        n_findings=s.n_findings,
        knowledge_html=_knowledge_html(s),
        alert_sections=alert_sec,
        suppress_section=suppress_sec,
        badge_section=_badge_section_html(result),
        cause_section=cause_sec + extra_sections,
    )
