"""数据质量看板：把巡检与评测结果渲染成单页 HTML。

刻意不引任何外部依赖（无 CDN、无图表库）：
- 面试官/同事双击就能打开，不需要网络，也不会遇到 CDN 被墙
- 趋势图用内联 SVG 手绘，产物是单个自包含文件，可以直接归档或发邮件

看板回答三个问题：**今天能不能用**、**什么时候开始坏的**、**这个系统本身准不准**。
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date

from findata.eval.metrics import EvalReport
from findata.report.inspect import InspectionResult, benign_label

_CSS = """
:root{
  --bg:#f6f7f9; --card:#fff; --line:#e6e8eb; --text:#1f2328; --muted:#6b7280;
  --p0:#dc2626; --p1:#d97706; --p2:#2563eb; --ok:#059669; --accent:#4f46e5;
}
*{box-sizing:border-box}
body{margin:0;padding:28px 22px 48px;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:26px;font-weight:650;margin-top:2px;line-height:1.2}
.card .n{color:var(--muted);font-size:12px;margin-top:2px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:16px}
.sec h2{font-size:15px;margin:0 0 14px}
.tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:1px solid var(--line);padding-bottom:0}
.tab{padding:7px 14px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;
  color:var(--muted);border:1px solid transparent;border-bottom:none;user-select:none}
.tab.on{background:#f3f4f6;color:var(--text);font-weight:600;border-color:var(--line)}
.pane{display:none}.pane.on{display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;color:#fff;font-weight:600}
.t-p0{background:var(--p0)}.t-p1{background:var(--p1)}.t-p2{background:var(--p2)}.t-ok{background:var(--ok)}
.route{color:var(--muted);font-size:12px;margin:-8px 0 10px}
.bar{display:flex;align-items:center;gap:8px;margin:5px 0}
.bar .lb{width:200px;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis}
.bar .tr{flex:1;background:#f3f4f6;border-radius:4px;height:9px;overflow:hidden}
.bar .fl{height:100%;background:#9ca3af}
.bar .ct{width:26px;text-align:right;font-size:12px}
.empty{color:var(--muted);padding:8px 0}
.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.kv{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 24px;margin-top:4px}
.kv .row{display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);
  padding:6px 0;font-size:13px}
.kv .row b{font-weight:600}
.gain{color:var(--ok);font-weight:600}
"""

_JS = """
document.querySelectorAll('.tab').forEach(function(t){
  t.onclick=function(){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('on')});
    document.querySelectorAll('.pane').forEach(function(x){x.classList.remove('on')});
    t.classList.add('on');
    document.getElementById(t.dataset.pane).classList.add('on');
  };
});
"""


@dataclass
class TrendPoint:
    asof: date
    health_score: float
    n_alerts: int
    n_suppressed: int


@dataclass
class DashboardData:
    trend: list[TrendPoint]
    current: InspectionResult
    eval_report: EvalReport | None = None


def _trend_svg(trend: list[TrendPoint]) -> str:
    """健康分折线 + 告警数柱状。手绘 SVG，无图表库。"""
    if not trend:
        return '<p class="empty">无趋势数据</p>'

    W, H = 980, 250
    pl, pr, pt, pb = 42, 18, 18, 40
    iw, ih = W - pl - pr, H - pt - pb
    n = len(trend)
    step = iw / max(n - 1, 1)

    def x_of(i: int) -> float:
        return pl + i * step

    def y_of(score: float) -> float:
        return pt + (100.0 - score) / 100.0 * ih

    parts: list[str] = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" '
        'preserveAspectRatio="xMidYMid meet" role="img" aria-label="健康分趋势">'
    ]

    for g in (0, 25, 50, 75, 100):
        y = y_of(g)
        parts.append(
            f'<line x1="{pl}" y1="{y:.1f}" x2="{W - pr}" y2="{y:.1f}" '
            f'stroke="#eceef1" stroke-width="1"/>'
            f'<text x="{pl - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#9ca3af">{g}</text>'
        )

    max_alerts = max((p.n_alerts for p in trend), default=0) or 1
    bw = max(step * 0.34, 2)
    for i, p in enumerate(trend):
        h = p.n_alerts / max_alerts * (ih * 0.22)
        if h <= 0:
            continue
        parts.append(
            f'<rect x="{x_of(i) - bw / 2:.1f}" y="{pt + ih - h:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" rx="2" fill="#f59e0b" opacity="0.28"/>'
        )

    pts = " ".join(f"{x_of(i):.1f},{y_of(p.health_score):.1f}" for i, p in enumerate(trend))
    parts.append(
        f'<polygon points="{pl},{pt + ih} {pts} {x_of(n - 1):.1f},{pt + ih}" '
        'fill="#4f46e5" opacity="0.1"/>'
    )
    parts.append(
        f'<polyline points="{pts}" fill="none" stroke="#4f46e5" '
        'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )

    last = trend[-1]
    lx, ly = x_of(n - 1), y_of(last.health_score)
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="#4f46e5"/>')
    parts.append(
        f'<text x="{lx - 6:.1f}" y="{ly - 10:.1f}" text-anchor="end" font-size="12" '
        f'font-weight="600" fill="#4f46e5">{last.health_score}</text>'
    )

    for i in (0, n // 2, n - 1):
        if i < 0 or i >= n:
            continue
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(
            f'<text x="{x_of(i):.1f}" y="{H - 14}" text-anchor="{anchor}" '
            f'font-size="11" fill="#9ca3af">{trend[i].asof}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _alert_rows(result: InspectionResult) -> str:
    if not result.alerts:
        return '<p class="empty">本轮无待处理告警。</p>'
    rows = []
    for a in result.alerts:
        f, d = a.finding, a.diagnosis
        cls = {"P0": "t-p0", "P1": "t-p1", "P2": "t-p2"}.get(d.severity.value, "t-p2")
        rows.append(
            "<tr>"
            f'<td><span class="tag {cls}">{d.severity.value}</span></td>'
            f"<td><code>{html.escape(str(f.symbol))}</code></td>"
            f"<td>{html.escape(f.table)}</td>"
            f"<td>{html.escape(a.windows)}</td>"
            f"<td><code>{html.escape(d.root_cause.value)}</code></td>"
            f"<td>{_fmt(f.value)} <span style='color:#6b7280'>({_fmt(f.threshold)})</span></td>"
            f"<td>{html.escape(d.explanation)}</td>"
            f"<td>{html.escape(a.route)}</td>"
            "</tr>"
        )
    body = "\n".join(rows)
    return (
        "<table><thead><tr><th>级别</th><th>标的</th><th>表</th><th>窗口</th>"
        "<th>根因</th><th>观测值(阈值)</th><th>判断依据</th><th>响应</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _suppress_rows(result: InspectionResult) -> str:
    if not result.suppressed:
        return '<p class="empty">本轮没有需要抑制的信号。</p>'
    rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(str(f.symbol))}</code></td>"
        f"<td>{html.escape(f.window)}</td>"
        f'<td><span class="tag t-ok">{html.escape(benign_label(d.root_cause))}</span></td>'
        f"<td>{html.escape(d.explanation)}</td>"
        "</tr>"
        for f, d in result.suppressed
    )
    return (
        "<table><thead><tr><th>标的</th><th>窗口</th><th>事件</th><th>说明</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _eval_rows(report: EvalReport | None) -> str:
    if report is None:
        return '<p class="empty">未附带评测结果（用 --with-eval 生成）。</p>'
    rows = "".join(
        f'<div class="row"><span>{html.escape(k)}</span><b>{html.escape(v)}</b></div>'
        for k, v in report.as_rows()
    )
    delta = report.alert_precision - report.naive_alert_precision
    return (
        f'<div class="kv">{rows}</div>'
        f'<p style="margin-top:12px;font-size:13px">'
        f"归因层带来的精确率增量："
        f'<span class="gain">+{delta:.1%}</span>'
        f"（不做归因时仅 {report.naive_alert_precision:.1%}，"
        f"意味着近四成告警会是噪音）</p>"
    )


def _fmt(v: float) -> str:
    if v != v:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4g}"


def _cause_bars(result: InspectionResult) -> str:
    causes = result.summary.by_root_cause
    if not causes:
        return '<p class="empty">无根因数据</p>'
    top = max(causes.values())
    return "".join(
        '<div class="bar">'
        f'<div class="lb"><code>{html.escape(c)}</code></div>'
        f'<div class="tr"><div class="fl" style="width:{int(n / top * 100)}%"></div></div>'
        f'<div class="ct">{n}</div>'
        "</div>"
        for c, n in sorted(causes.items(), key=lambda kv: -kv[1])
    )


def render_dashboard(data: DashboardData) -> str:
    cur = data.current
    s = cur.summary
    worst = min((p.health_score for p in data.trend), default=s.health_score)
    first = data.trend[0].asof if data.trend else s.asof

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据质量看板 · {s.asof}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>数据质量看板</h1>
  <div class="sub">观察日 {s.asof} · 趋势区间 {first} → {s.asof} ·
    覆盖 {s.n_symbols} 只标的 / {s.n_rows:,} 行行情</div>

  <div class="cards">
    <div class="card"><div class="k">当前健康分</div>
      <div class="v" style="color:{'var(--ok)' if s.health_score >= 95 else 'var(--p0)'}">
        {s.health_score}</div>
      <div class="n">{s.grade()} · 区间最低 {worst}</div></div>
    <div class="card"><div class="k">待处理告警</div><div class="v">{s.n_alerts}</div>
      <div class="n">P0 {s.by_severity.get('P0', 0)} ·
        P1 {s.by_severity.get('P1', 0)} · P2 {s.by_severity.get('P2', 0)}</div></div>
    <div class="card"><div class="k">抑制误报</div><div class="v">{s.n_suppressed}</div>
      <div class="n">合法业务事件，未告警</div></div>
    <div class="card"><div class="k">降噪率</div>
      <div class="v">{s.noise_reduction:.0%}</div>
      <div class="n">{s.n_findings} 个信号收敛为 {s.n_alerts}</div></div>
  </div>

  <div class="sec">
    <h2>健康分趋势</h2>
    {_trend_svg(data.trend)}
    <div class="legend">
      <span><i style="background:#4f46e5"></i>健康分</span>
      <span><i style="background:#f59e0b;opacity:.5"></i>当日告警数</span>
      <span>每天按"当天能看到的数据"重跑巡检，可回放任意历史时点</span>
    </div>
  </div>

  <div class="sec">
    <h2>根因分布</h2>
    {_cause_bars(cur)}
  </div>

  <div class="sec">
    <div class="tabs">
      <div class="tab on" data-pane="p-alert">待处理告警 ({s.n_alerts})</div>
      <div class="tab" data-pane="p-sup">已抑制 ({s.n_suppressed})</div>
      <div class="tab" data-pane="p-eval">系统自评</div>
    </div>
    <div class="pane on" id="p-alert">{_alert_rows(cur)}</div>
    <div class="pane" id="p-sup">{_suppress_rows(cur)}</div>
    <div class="pane" id="p-eval">{_eval_rows(data.eval_report)}</div>
  </div>
</div>
<script>{_JS}</script>
</body>
</html>
"""


def build_dashboard(
    snapshot,
    n_days: int = 20,
    eval_report: EvalReport | None = None,
) -> DashboardData:
    """回放最近 n 个交易日，逐日重跑巡检，得到趋势 + 当日详情。"""
    from findata.report.inspect import run_inspection

    days = snapshot.calendar.trading_days(
        snapshot.stock_daily["date"].min(), snapshot.asof
    )
    window = days[-n_days:] if len(days) > n_days else days

    trend: list[TrendPoint] = []
    for day in window:
        r = run_inspection(snapshot.as_of(day))
        trend.append(
            TrendPoint(
                asof=day,
                health_score=r.summary.health_score,
                n_alerts=r.summary.n_alerts,
                n_suppressed=r.summary.n_suppressed,
            )
        )

    current = run_inspection(snapshot.as_of(snapshot.asof))
    return DashboardData(trend=trend, current=current, eval_report=eval_report)
