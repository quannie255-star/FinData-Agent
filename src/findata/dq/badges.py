"""逐指标徽章层（R1.2）：findings → (table, metric) → 徽章。

徽章是本产品对外的最小可信单元（ROADMAP 第零节）：
报告上每个数字都能回答「这个数能不能引用、凭什么」。四档定义：

| 徽章 | 含义 | 证据要求 |
| --- | --- | --- |
| ✓ 已核验   | 领域知识归因背书             | 归因结论 + 证据引用（事件表/截面共动/日历） |
| ✓ 基线通过 | 仅通用检查通过，无领域知识参与 | 检查项清单 |
| ⚠️ 仅借鉴  | 有疑点，人工判断             | 疑点描述（归因解释 + 观测值） |
| ✗ 不可用   | 归因为真实故障               | 根因 + 建议动作 |

映射规则（探针按表/列产出观测，归因给出结论，本层聚合成每列一个徽章）：
- 行级信号（缺失/重复/停更/历史深度）→ 该表**全部列**：缺的行会让
  每一列的聚合值都错，不存在"价格没问题只是行缺了"这种事
- 列级信号按 metric 映射到具体列（如 pct_chg_out_of_limit → pct_chg）
- 聚合取最坏：一条 ✗ 压过所有 ✓；只有被领域知识抑制过的信号才给 ✓ 已核验

健康分口径（R1.2 验收，唯一权威定义在本文件）：
    score = 100 × Σ w(徽章) / 徽章总数
    w: 已核验 1.0 · 基线通过 1.0 · 仅借鉴 0.5 · 不可用 0.0
只对**已观测**（表非空且被探针覆盖）的列发徽章；未观测的列不发徽章、
不进分母——可信分只对已观测事实归因，不做无依据声明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from findata.dq.models import Diagnosis, Finding, Severity


class BadgeLevel(StrEnum):
    """四档徽章。语义见模块 docstring，对外只呈现 ✓/⚠️/✗ 与文案。"""

    VERIFIED = "verified"  # ✓ 已核验
    BASELINE = "baseline"  # ✓ 基线通过
    CAUTION = "caution"  # ⚠️ 仅借鉴
    UNUSABLE = "unusable"  # ✗ 不可用


BADGE_MARK: dict[BadgeLevel, str] = {
    BadgeLevel.VERIFIED: "✓ 已核验",
    BadgeLevel.BASELINE: "✓ 基线通过",
    BadgeLevel.CAUTION: "⚠️ 仅借鉴",
    BadgeLevel.UNUSABLE: "✗ 不可用",
}

# 健康分权重：可用性口径，见模块 docstring
_LEVEL_WEIGHT: dict[BadgeLevel, float] = {
    BadgeLevel.VERIFIED: 1.0,
    BadgeLevel.BASELINE: 1.0,
    BadgeLevel.CAUTION: 0.5,
    BadgeLevel.UNUSABLE: 0.0,
}

# 每张被探针覆盖的表的标准检查项清单（✓ 基线通过徽章的证据）
_BASELINE_CHECKS: dict[str, tuple[str, ...]] = {
    "stock_daily": (
        "新鲜度",
        "完整性",
        "唯一性",
        "值域",
        "分布漂移",
        "空值扫荡",
        "历史深度",
    ),
    "valuation_daily": ("唯一性", "分布漂移"),
}

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "stock_daily": ("open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"),
    "valuation_daily": ("pe", "pe_ttm", "pb", "total_mv"),
}


def _finding_columns(finding: Finding) -> tuple[str, ...]:
    """finding 影响的列。行级信号返回该表全部列（行缺则列皆不可信）。"""
    cols = _TABLE_COLUMNS.get(finding.table)
    if cols is None:
        return ()
    metric = finding.metric
    if finding.probe in ("freshness", "completeness", "uniqueness", "history_depth"):
        return cols
    if finding.probe == "null_sweep":
        col = metric.split("::")[-1]
        return (col,) if col in cols else ()
    column_map = {
        "non_positive_close": ("close",),
        "pct_chg_out_of_limit": ("pct_chg",),
        "high_lt_low": ("high", "low"),
        "non_positive_volume": ("volume",),
        "volume_recent_over_history": ("volume",),
        "total_mv_level_shift": ("total_mv",),
    }
    return column_map.get(metric, ())


@dataclass(frozen=True)
class MetricBadge:
    """一个 (table, column) 的可信判定，含可点开的证据条目。"""

    table: str
    column: str
    level: BadgeLevel
    summary: str  # 一句话判定依据
    evidence: tuple[tuple[str, str], ...] = ()  # (来源, 内容) 引用条目

    @property
    def mark(self) -> str:
        return BADGE_MARK[self.level]

    @property
    def metric_key(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class BadgeBoard:
    """一次巡检的全量徽章与健康分。"""

    badges: list[MetricBadge] = field(default_factory=list)

    def of(self, table: str, column: str) -> MetricBadge | None:
        for b in self.badges:
            if b.table == table and b.column == column:
                return b
        return None

    def by_level(self, level: BadgeLevel) -> list[MetricBadge]:
        return [b for b in self.badges if b.level is level]

    @property
    def health_score(self) -> float:
        """健康分 = 100 × Σ w / N，口径见模块 docstring。"""
        if not self.badges:
            return 100.0
        return round(100.0 * sum(_LEVEL_WEIGHT[b.level] for b in self.badges) / len(self.badges), 1)


def build_badges(
    findings: list[Finding],
    diagnoses: list[Diagnosis],
    observed_tables: dict[str, bool] | None = None,
) -> BadgeBoard:
    """把 findings + diagnoses 聚合成逐列徽章。

    observed_tables 声明哪些表真的有数据（探针跑在空表上等于什么都没查），
    未观测的表不发徽章。纯函数：相同输入 → 相同徽章。
    """
    observed = observed_tables or {}
    by_key = {d.finding_key: d for d in diagnoses}

    # (table, column) → 参与聚合的 (finding, diagnosis) 列表
    hits: dict[tuple[str, str], list[tuple[Finding, Diagnosis | None]]] = {}
    for f in findings:
        cols = _finding_columns(f)
        if not cols:
            continue
        for col in cols:
            hits.setdefault((f.table, col), []).append((f, by_key.get(f.key)))

    badges: list[MetricBadge] = []
    for table, cols in _TABLE_COLUMNS.items():
        if not observed.get(table):
            continue
        for col in cols:
            badges.append(
                aggregate_column_badge(
                    table, col, hits.get((table, col), []), _BASELINE_CHECKS.get(table, ())
                )
            )
    return BadgeBoard(badges=badges)


def aggregate_column_badge(
    table: str,
    column: str,
    entries: list[tuple[Finding, Diagnosis | None]],
    baseline_checks: tuple[str, ...] = (),
) -> MetricBadge:
    """单列徽章聚合——四档判定的**唯一实现**（口径见模块 docstring）。

    金融引擎（dq/badges.build_badges）与通用包（generic.engine）都调用
    这里，聚合规则不允许出现第二份——两套实现漂移就是两套口径。
    """
    active: list[tuple[Finding, Diagnosis]] = []
    suppressed: list[tuple[Finding, Diagnosis]] = []
    for f, d in entries:
        if d is None:
            continue
        if d.suppressed:
            suppressed.append((f, d))
        else:
            active.append((f, d))

    if active:
        # P0 真实故障 → ✗；P1/P2 疑点 → ⚠️。多条时取最坏级别、证据并列。
        # 注意是 P0 优先（next 找到即停）——用 max(key=...) 会反向选中非 P0
        worst = next(
            (e for e in active if e[1].severity is Severity.P0), active[0]
        )
        if worst[1].severity is Severity.P0:
            return MetricBadge(
                table=table,
                column=column,
                level=BadgeLevel.UNUSABLE,
                summary=f"{worst[1].root_cause.value}：{worst[1].explanation}",
                evidence=_evidence(active),
            )
        suspicious = active[0]
        return MetricBadge(
            table=table,
            column=column,
            level=BadgeLevel.CAUTION,
            summary=f"疑点：{suspicious[1].explanation}",
            evidence=_evidence(active),
        )

    if suppressed:
        # 领域知识归因背书：归因结论 + 证据引用（事件表/截面共动/日历）
        ev: list[tuple[str, str]] = []
        summaries = []
        for f, d in suppressed:
            summaries.append(d.explanation)
            for ref in d.evidence_refs:
                ev.append((ref, f"{benign_reason(d.root_cause.value)} · {f.symbol} {f.window}"))
        if not ev:
            ev.append(("triage", summaries[0] if summaries else "领域归因"))
        return MetricBadge(
            table=table,
            column=column,
            level=BadgeLevel.VERIFIED,
            summary="；".join(dict.fromkeys(summaries))[:200] or "异常信号已被领域知识归因",
            evidence=tuple(ev[:6]),
        )

    return MetricBadge(
        table=table,
        column=column,
        level=BadgeLevel.BASELINE,
        summary="通用基线检查全部通过（无领域知识参与）",
        evidence=tuple(("检查项", c) for c in baseline_checks),
    )


def benign_reason(cause_value: str) -> str:
    """根因值 → 简短中文事由，证据条目用。"""
    return {
        "benign_suspension": "已核验停牌",
        "benign_corporate_action": "已核验除权除息",
        "benign_new_listing": "已核验新股上市",
        "benign_non_trading_day": "已核验非交易日",
        "benign_market_action": "已核验放量脉冲",
        "benign_market_event": "已核验市场行情（截面共动）",
    }.get(cause_value, cause_value)


def _evidence(entries: list[tuple[Finding, Diagnosis]]) -> tuple[tuple[str, str], ...]:
    ev: list[tuple[str, str]] = []
    for f, d in entries:
        obs = f"{f.symbol} {f.window} 观测 {f.value}（阈值 {f.threshold}）"
        ev.append((f"{f.probe}:{f.metric}", obs))
        for ref in d.evidence_refs:
            ev.append((ref, d.explanation))
    return tuple(ev[:6])


# 可嵌入徽章芯片的配色（与日报 HTML 同一语义：绿=可用，琥珀=存疑，红=不可用）
_CHIP_COLOR = {
    BadgeLevel.VERIFIED: ("#059669", "#ecfdf5"),
    BadgeLevel.BASELINE: ("#059669", "#ecfdf5"),
    BadgeLevel.CAUTION: ("#b45309", "#fffbeb"),
    BadgeLevel.UNUSABLE: ("#b91c1c", "#fef2f2"),
}


def badge_chip_html(badge: MetricBadge) -> str:
    """可嵌入的徽章芯片（inline 样式，无类名依赖），宿主页面引用数字旁直接贴。

    这是「增强模块」面向非 MCP/非 API 场景的第三种接入面：宿主渲染报告时
    把芯片插在数字旁边，悬停 title 即证据摘要。样式内联是为了不污染宿主的
    CSS——增强模块不许要求宿主改样式表。
    """
    import html as _html

    fg, bg = _CHIP_COLOR[badge.level]
    title = _html.escape(f"{badge.summary}", quote=True)
    return (
        f'<span title="{title}" style="display:inline-block;margin-left:4px;'
        f'padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;'
        f'color:{fg};background:{bg};border:1px solid {fg}33;'
        f'vertical-align:middle;white-space:nowrap">{_html.escape(badge.mark)}</span>'
    )
