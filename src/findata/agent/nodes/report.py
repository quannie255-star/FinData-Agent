"""可信日报 Agent（R1.3）：分析 → 可视化 → 可信审校 的三节点报告链。

复用 Supervisor + nodes 的编排模式，但拓扑是刻意的**线性**：
报告生成没有问答的分支回环，三步分工是真实的——

1. AnalysisAgent（指标解读）：基于巡检结果 + 仓库事实写市场解读。
   有 LLM 用 LLM，没 key 用确定性模板——日报管线无人值守，
   LLM 挂了报告不能挂。引用契约：每个数字后跟 «表.列»（或
   «summary.字段»）引用标记，交给审校节点裁决。
2. VizAgent（可视化）：图表选择与生成。选择规则是确定性的
   （近 30 日、最近涨幅 top5 的收盘归一化折线）——图表服务于
   可读性，确定性让「选了什么图」可解释可回归。
3. ReviewAgent（可信审校）：**纯规则**，不信任 LLM。逐条裁决
   引用标记：有徽章 → 数字带徽章放行；无徽章/徽章 ✗ → 数字整段
   拦截（ROADMAP 铁律：无徽章不得引用）。裸奔的带单位/小数数字
   一律拦截。这是 v0.9 eval-gate「判官校准」思想在本节点的落地。

审校核心 review_and_enforce 是纯函数，不依赖 LLM、可全分支单测。
"""

from __future__ import annotations

import html as _html
import operator
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, TypedDict

from findata.agent.llm import LLMClient, LLMError
from findata.agent.tools import merge_usage
from findata.dq.badges import BadgeBoard, BadgeLevel
from findata.report.inspect import InspectionResult, Snapshot

# ─────────────────────────── 引用契约 ───────────────────────────

# 引用标记：«表.列» 或 «summary.字段»。数字与标记之间允许空白与单位。
_CITE_KEY = r"[a-z_]+\.[a-z_]+"
_CITE_RE = re.compile(rf"«\s*({_CITE_KEY})\s*»")
# 裸奔数字：小数、千分位或带金额/百分比类单位；日期与纯整数计数不拦
_BARE_NUM_RE = re.compile(
    r"(?<![\w.»])([0-9]+\.[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+\s*(?:元|%|倍|亿|万|点|手))(?![\w.«])"
)
# 日期是元数据（观察日/窗口），不是被引用的数据结论
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}年(?:[0-9]{1,2}月)?(?:[0-9]{1,2}日)?")

# summary.* 引用键：健康分这类「报告自己的数字」也要有出处
_SUMMARY_KEYS: dict[str, tuple[str, str]] = {
    "summary.health_score": ("健康分", "徽章聚合口径见 docs/badge.md"),
    "summary.n_symbols": ("标的覆盖数", "巡检快照 stock_universe"),
    "summary.n_alerts": ("待处理告警数", "巡检结果"),
    "summary.n_suppressed": ("抑制误报数", "巡检结果"),
}

_INTERCEPT_MARK = "■"
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


# ─────────────────────────── 审校（纯规则）───────────────────────────


def resolve_citation(key: str, board: BadgeBoard) -> tuple[bool, str]:
    """裁决一条引用：(是否有效, 展示用的徽章文本)。"""
    if key in _SUMMARY_KEYS:
        name, basis = _SUMMARY_KEYS[key]
        return True, f"✓ 口径｜{name}（{basis}）"
    if "." not in key:
        return False, ""
    table, column = key.split(".", 1)
    badge = board.of(table, column)
    if badge is None or badge.level is BadgeLevel.UNUSABLE:
        return False, ""
    return True, f"{badge.mark}｜{table}.{column}"


def review_and_enforce(draft: str, board: BadgeBoard) -> tuple[str, list[str]]:
    """可信审校：裁决草稿里的每个数字。返回 (定稿文本, 审校记录)。

    - 引用有效 → 数字加 ⟦⟧ 放行并附徽章标注；
    - 引用缺失/无徽章/徽章 ✗ → 数字替换为 ■（记录里写明原因）；
    - 无引用的小数/带单位数字 → 一律拦截（无徽章不得引用）；
    - 日期与不带单位的整数计数（如「25 只标的」）不拦。
    """
    notes: list[str] = []
    text = draft

    # 第一遍：逐个引用键裁决。「数字+引用」成对处理——引用无效时数字一并拦截，
    # 引用有效时数字入 ⟦⟧ 保护并附徽章；孤立的引用标记（前面没有数字）直接删除
    for key in dict.fromkeys(m.group(1) for m in _CITE_RE.finditer(text)):
        ok, mark = resolve_citation(key, board)
        quoted = re.compile(
            rf"([0-9][0-9,，]*(?:\.[0-9]+)?(?:\s*(?:元|%|倍|亿|万|点|手|股|只|日|天))?)\s*«\s*{re.escape(key)}\s*»"
        )
        if ok:

            def _keep(m: re.Match, mk: str = mark) -> str:
                return f"⟦{m.group(1)}⟧〔{mk}〕"

            text = quoted.sub(_keep, text)
        else:

            def _drop(m: re.Match, k: str = key) -> str:
                notes.append(f"拦截：引用 «{k}» 无有效徽章，数字「{m.group(1)}」不予引用")
                return f"{_INTERCEPT_MARK}（«{k}» 引用被可信审校拦截：无有效徽章）"

            text = quoted.sub(_drop, text)
        text = text.replace(f"«{key}»", "")

    # 第二遍：裸数字拦截。先屏蔽日期与已放行片段，拦完再恢复
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    masked = _DATE_RE.sub(_stash, text)
    masked = re.sub(r"⟦[^⟧]*⟧〔[^〕]*〕", _stash, masked)

    def _intercept_bare(m: re.Match) -> str:
        notes.append(f"拦截：数字「{m.group(1)}」无徽章引用，不予展示")
        return _INTERCEPT_MARK

    text = _BARE_NUM_RE.sub(_intercept_bare, masked)
    text = _PLACEHOLDER.sub(lambda m: stash[int(m.group(1))], text)
    return text, notes


# ─────────────────────────── 共享状态 ───────────────────────────


class ReportState(TypedDict, total=False):
    draft: str  # 分析草稿（含 «引用» 标记）
    viz_md: str
    viz_html: str
    review_notes: Annotated[list[str], operator.add]  # 累加（降级/审校记录）
    final_md: str
    usage: Annotated[dict, merge_usage]
    llm_used: bool
    elapsed_ms: int


@dataclass
class ReportSection:
    """报告 Agent 的产出：md 追加到日报正文，html 注入日报页面。"""

    md: str
    html: str
    review_notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    llm_used: bool = False
    elapsed_ms: int = 0


# ─────────────────────────── 分析节点 ───────────────────────────


def market_facts(snapshot: Snapshot, top: int = 3) -> dict:
    """从仓库快照提取市场事实（确定性）。分析只准用这里的事实。"""
    daily = snapshot.stock_daily
    empty = {
        "asof": snapshot.asof,
        "last_day": None,
        "n_symbols": 0,
        "gainers": [],
        "active": [],
        "days": 0,
    }
    if daily.empty or "close" not in daily.columns:
        return empty
    last_day = daily["date"].max()
    last = daily[daily["date"] == last_day]
    gainers: list[tuple[str, float, float]] = []
    active: list[tuple[str, float]] = []
    if "pct_chg" in last.columns:
        top_g = last.dropna(subset=["pct_chg"]).nlargest(top, "pct_chg")
        gainers = [
            (str(r.symbol), float(r.pct_chg), float(r.close))
            for r in top_g.itertuples(index=False)
        ]
    if "amount" in last.columns:
        top_a = last.dropna(subset=["amount"]).nlargest(top, "amount")
        active = [(str(r.symbol), float(r.amount)) for r in top_a.itertuples(index=False)]
    return {
        "asof": snapshot.asof,
        "last_day": last_day,
        "n_symbols": int(last["symbol"].nunique()),
        "gainers": gainers,
        "active": active,
        "days": int(daily["date"].nunique()),
    }


def template_draft(facts: dict, board: BadgeBoard) -> str:
    """无 LLM 的确定性草稿：每个数字都按引用契约带 «key»。"""
    lines = [
        f"市场概览（{facts['last_day'] or facts['asof']}）："
        f"覆盖 {facts['n_symbols']} 只标的«summary.n_symbols»。"
    ]
    if facts["gainers"]:
        parts = [
            f"{sym} {pct:+.2f}%«stock_daily.pct_chg»（收盘 {close:.2f} 元«stock_daily.close»）"
            for sym, pct, close in facts["gainers"]
        ]
        lines.append("涨幅前列：" + "、".join(parts) + "。")
    if facts["active"]:
        parts = [f"{sym} {amt / 1e8:.2f} 亿«stock_daily.amount»" for sym, amt in facts["active"]]
        lines.append("成交活跃：" + "、".join(parts) + "。")
    levels = (BadgeLevel.VERIFIED, BadgeLevel.BASELINE, BadgeLevel.CAUTION, BadgeLevel.UNUSABLE)
    counts = {lvl: len(board.by_level(lvl)) for lvl in levels}
    lines.append(
        f"数据可信度：健康分 {board.health_score}«summary.health_score»，"
        f"已核验 {counts[BadgeLevel.VERIFIED]} 项、基线通过 {counts[BadgeLevel.BASELINE]} 项、"
        f"仅借鉴 {counts[BadgeLevel.CAUTION]} 项、不可用 {counts[BadgeLevel.UNUSABLE]} 项"
        "（逐项见「指标可信度」表）。"
    )
    return "\n".join(lines)


_ANALYSIS_SYSTEM = (
    "你是 Findata 可信日报的分析员。只依据给定事实写 3-6 句中文市场解读，"
    "不得使用事实清单之外的任何数字。引用契约（必须遵守）：每个数据数字后"
    "紧跟引用标记 «表.列»（行情数字用 stock_daily.close / stock_daily.pct_chg / "
    "stock_daily.amount，健康分用 summary.health_score，标的数用 "
    "summary.n_symbols）。无有效徽章的数字会被审校拦截，宁可不写。"
)


def _analysis_prompt(facts: dict, board: BadgeBoard) -> str:
    lines = ["# 事实清单"]
    lines.append(
        f"- 观察日 {facts['last_day'] or facts['asof']}，"
        f"覆盖 {facts['n_symbols']} 只标的«summary.n_symbols»"
    )
    for sym, pct, close in facts["gainers"]:
        lines.append(
            f"- {sym} 涨幅 {pct:+.2f}%«stock_daily.pct_chg»，收盘 {close:.2f} 元«stock_daily.close»"
        )
    for sym, amt in facts["active"]:
        lines.append(f"- {sym} 成交额 {amt / 1e8:.2f} 亿«stock_daily.amount»")
    lines.append(f"- 健康分 {board.health_score}«summary.health_score»")
    lines.append("# 徽章口径")
    lines.append(
        "✓ 已核验 = 领域知识背书；✓ 基线通过 = 通用检查通过；"
        "⚠️ 仅借鉴 = 有疑点；✗ 不可用 = 真实故障"
    )
    return "\n".join(lines)


def make_analysis_node(result: InspectionResult, snapshot: Snapshot, llm_client: LLMClient | None):
    """指标解读节点：LLM 写作，无 LLM 用模板；只准引用给定事实。"""

    def analysis_node(state: ReportState) -> dict:
        board = result.badges
        facts = market_facts(snapshot)
        zero = {"calls": 0, "chars_in": 0, "chars_out": 0}
        if llm_client is not None:
            user = _analysis_prompt(facts, board)
            try:
                t0 = time.perf_counter()
                text = llm_client.complete(_ANALYSIS_SYSTEM, user)
                usage = {
                    "calls": 1,
                    "chars_in": len(_ANALYSIS_SYSTEM) + len(user),
                    "chars_out": len(text),
                }
                return {
                    "draft": text,
                    "llm_used": True,
                    "usage": usage,
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                }
            except LLMError as exc:
                # 日报管线无人值守：LLM 挂了降级模板，降级本身写进审校记录
                return {
                    "draft": template_draft(facts, board),
                    "llm_used": False,
                    "review_notes": [f"分析降级：LLM 不可用（{exc}），使用确定性模板"],
                    "usage": zero,
                }
        return {"draft": template_draft(facts, board), "llm_used": False, "usage": zero}

    return analysis_node


# ─────────────────────────── 可视化节点 ───────────────────────────

# 折线图取数窗口：45 个自然日 ≈ 30 个交易日
_VIZ_WINDOW_DAYS = 45
_VIZ_TOP = 5


def make_viz_node(result: InspectionResult, snapshot: Snapshot):
    """可视化节点：确定性选图——近 30 日、最近涨幅 top5 收盘归一化折线。"""

    def viz_node(state: ReportState) -> dict:
        daily = snapshot.stock_daily
        if daily.empty or "close" not in daily.columns:
            note = "样本不足，未生成图表"
            return {"viz_md": f"> {note}", "viz_html": f'<p class="route">{note}</p>'}
        last_day = daily["date"].max()
        last = daily[daily["date"] == last_day]
        syms: list[str] = []
        if "pct_chg" in last.columns:
            top_g = last.dropna(subset=["pct_chg"]).nlargest(_VIZ_TOP, "pct_chg")
            syms = [str(s) for s in top_g["symbol"]]
        recent = daily[daily["symbol"].isin(syms)]
        recent = recent[recent["date"] >= last_day - timedelta(days=_VIZ_WINDOW_DAYS)]
        series: dict[str, list[float]] = {}
        for sym, g in recent.groupby("symbol"):
            closes = [float(c) for c in g.sort_values("date")["close"]]
            if len(closes) >= 5:
                series[str(sym)] = closes
        if len(series) < 2:
            note = "样本不足，未生成图表"
            return {"viz_md": f"> {note}", "viz_html": f'<p class="route">{note}</p>'}
        badge = result.badges.of("stock_daily", "close")
        mark = badge.mark if badge else "✓ 基线通过"
        caption = (
            f"涨幅前 {len(series)} 标的 · 近 30 交易日收盘归一化（起点=100）"
            f" · 数据源 stock_daily.close〔{mark}〕"
        )
        md_lines = [
            "**" + caption + "**",
            "",
            "| 标的 | 最新收盘（元） | 区间涨跌 |",
            "|---|---|---|",
        ]
        for sym, closes in series.items():
            md_lines.append(
                f"| {sym} | {closes[-1]:.2f} | {(closes[-1] / closes[0] - 1) * 100:+.1f}% |"
            )
        return {
            "viz_md": "\n".join(md_lines),
            "viz_html": (
                f'<div style="margin-top:12px">{_lines_svg(series, caption)}'
                f'<p class="route">{_html.escape(caption)}</p></div>'
            ),
        }

    return viz_node


def _lines_svg(
    series: dict[str, list[float]], aria: str, width: int = 960, height: int = 240
) -> str:
    """多折线归一化 SVG（起点 =100），零图表库依赖（对齐 dashboard 风格）。"""
    colors = ["#4f46e5", "#d97706", "#059669", "#dc2626", "#2563eb"]
    pl, pr, pt, pb = 40, 16, 14, 26
    iw, ih = width - pl - pr, height - pt - pb
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="{_html.escape(aria)}">'
    ]
    all_norm = [
        [c / (closes[0] or 1.0) * 100 for c in closes]
        for closes in series.values()
    ]
    lo = min(min(n) for n in all_norm)
    hi = max(max(n) for n in all_norm)
    span = max(hi - lo, 1e-9)

    def y_of(v: float) -> float:
        return pt + (hi - v) / span * ih

    for g in (lo, (lo + hi) / 2, hi):
        y = y_of(g)
        parts.append(
            f'<line x1="{pl}" y1="{y:.1f}" x2="{width - pr}" y2="{y:.1f}" '
            f'stroke="#eceef1" stroke-width="1"/>'
            f'<text x="{pl - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="#9ca3af">{g:.0f}</text>'
        )
    legend: list[str] = []
    # 同源构造，长度恒相等；strict=True 钉死这个前提
    for i, (sym, norm) in enumerate(zip(series, all_norm, strict=True)):
        step = iw / max(len(norm) - 1, 1)
        pts = " ".join(f"{pl + j * step:.1f},{y_of(v):.1f}" for j, v in enumerate(norm))
        color = colors[i % len(colors)]
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        legend.append(
            f'<text x="{pl + i * 130:.0f}" y="{height - 8}" font-size="12" fill="{color}">'
            f"{_html.escape(sym)}</text>"
        )
    parts.extend(legend)
    parts.append("</svg>")
    return "".join(parts)


# ─────────────────────────── 审校节点与图组装 ───────────────────────────


def make_review_node(result: InspectionResult):
    """可信审校节点：纯规则裁决（无徽章不得引用），并产出最终 md。

    返回值只带本节点新增的审校记录——review_notes 是累加通道，
    把上游（分析降级）记录一起返回会双计数。
    """

    def review_node(state: ReportState) -> dict:
        prior = state.get("review_notes", [])
        final, fresh = review_and_enforce(state["draft"], result.badges)
        notes = [*prior, *fresh]
        viz_md = state.get("viz_md", "")
        body = final + ("\n\n" + viz_md if viz_md else "")
        head = "## 市场解读（报告 Agent · 逐数字带徽章）\n\n"
        tail = ""
        if notes:
            tail = "\n\n> 可信审校：\n" + "\n".join(f"> - {n}" for n in notes)
        meta = "\n\n<sub>分析：" + ("LLM" if state.get("llm_used") else "确定性模板")
        meta += f" · 审校：规则拦截 {len(notes)} 条 · 徽章口径见 docs/badge.md</sub>"
        usage = state.get("usage", {})
        if usage.get("calls"):
            meta += (
                f" · 成本：{usage['calls']} 次 LLM 调用，入 {usage.get('chars_in', 0)} 字符"
                f" / 出 {usage.get('chars_out', 0)} 字符 / {state.get('elapsed_ms', 0)} ms"
            )
        return {"final_md": head + body + tail + meta, "review_notes": fresh}

    return review_node


def build_report_graph(
    result: InspectionResult, snapshot: Snapshot, llm_client: LLMClient | None = None
):
    """分析 → 可视化 → 可信审校 的线性图。审校是放行前的最后一道闸。"""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReportState)
    g.add_node("analysis", make_analysis_node(result, snapshot, llm_client))
    g.add_node("viz", make_viz_node(result, snapshot))
    g.add_node("review", make_review_node(result))
    g.add_edge(START, "analysis")
    g.add_edge("analysis", "viz")
    g.add_edge("viz", "review")
    g.add_edge("review", END)
    return g.compile()


def generate_report_section(
    result: InspectionResult,
    snapshot: Snapshot,
    llm_client: LLMClient | None = None,
) -> ReportSection:
    """一条命令产出报告 Agent 段落（md + html）。LLM 客户端为 None 时用模板。"""
    t0 = time.perf_counter()
    app = build_report_graph(result, snapshot, llm_client)
    state = app.invoke({"review_notes": [], "usage": {}})
    section_html = (
        '<div class="sec" style="margin:16px auto 0;max-width:960px">'
        "<h2>市场解读 · 报告 Agent</h2>"
        f"<pre style='white-space:pre-wrap;font:inherit;margin:0'>"
        f"{_html.escape(state['final_md'])}</pre>"
        f"{state.get('viz_html', '')}"
        "</div>"
    )
    return ReportSection(
        md=state["final_md"],
        html=section_html,
        review_notes=list(state.get("review_notes", [])),
        usage=dict(state.get("usage", {})),
        llm_used=bool(state.get("llm_used")),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
