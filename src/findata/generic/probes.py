"""通用探针：完整性 / 唯一性 / 新鲜度 / 离群值 / Schema 一致。

与金融域同一铁律：探针只观测，不做判断——它不知道"空值超容忍"该算
故障还是疑点以上的什么，判断全部在 generic.triage。探针产出的 Finding
复用 dq.models 的证据契约（没有证据的告警等于没有告警）。

能力边界（设计使然，不是缺陷）：通用探针没有"每个时间点该有哪些行"
的期望模型，**看不见行级缺失**（比如某只股票停牌 6 天）——它只能看
列内空值、主键重复、时间停更、分布离群。这条边界就是它与领域包的
含金量差距，对比页（scripts/compare_domain_vs_generic.py）如实展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from findata.dq.models import Finding, Severity
from findata.generic.schema import TableSpec

# 离群值：IQR 围栏倍数（Tukey 标准）
_IQR_K = 1.5
# 均值水平漂移：近期窗口均值 / 历史均值 超出该比值即命中（与金融漂移探针同思想）
_LEVEL_UP = 3.0
_LEVEL_DOWN = 1 / 3.0
# 水平漂移窗口：近期 = 最近 20%，最少历史 20 行
_RECENT_FRACTION = 0.2
_MIN_HISTORY = 20


@dataclass
class GenericContext:
    """通用探针输入：一张表 + 一份声明。"""

    df: pd.DataFrame
    spec: TableSpec
    asof: date
    declared: bool = True  # schema 是声明的还是推断的（推断态不做 schema 一致检查）


def _finding(
    ctx: GenericContext,
    probe: str,
    symbol: str,
    metric: str,
    value: float,
    threshold: float,
    severity: Severity,
    evidence: dict,
) -> Finding:
    return Finding(
        probe=probe,
        table=ctx.spec.table,
        symbol=symbol,
        window="全表",
        metric=metric,
        value=float(value),
        threshold=float(threshold),
        severity_hint=severity,
        evidence=evidence,
    )


def probe_schema_conformance(ctx: GenericContext) -> list[Finding]:
    """Schema 一致：声明的列缺失 / 多出未声明的列 / 类型不符。"""
    out: list[Finding] = []
    if not ctx.declared:
        return out
    declared = set(ctx.spec.column_names)
    observed = set(map(str, ctx.df.columns))
    for name in sorted(declared - observed):
        out.append(
            _finding(
                ctx, "schema", name, "column_missing", 1, 1, Severity.P1,
                {"note": "声明列在数据中不存在（上游字段下线或改名）"},
            )
        )
    for name in sorted(observed - declared):
        out.append(
            _finding(
                ctx, "schema", name, "column_extra", 1, 1, Severity.P2,
                {"note": "未声明的列出现（schema 与数据不同步）"},
            )
        )
    for col in ctx.spec.columns:
        if col.name not in ctx.df.columns:
            continue
        s = ctx.df[col.name].dropna()
        if s.empty:
            continue
        if col.kind == "numeric":
            bad = float((~pd.to_numeric(s, errors="coerce").notna()).mean())
        elif col.kind == "datetime":
            bad = float((~pd.to_datetime(s, errors="coerce").notna()).mean())
        else:
            continue
        if bad > 0.01:  # 1% 以上不可解析才算类型漂移，容忍偶发脏值
            out.append(
                _finding(
                    ctx, "schema", col.name, f"type_mismatch::{col.name}",
                    bad, 0.01, Severity.P1,
                    {"note": f"声明 {col.kind} 但 {bad:.0%} 的值不可解析",
                     "sample": [str(v) for v in s.head(3)]},
                )
            )
    return out


def probe_completeness(ctx: GenericContext) -> list[Finding]:
    """完整性：列内空值率。非空声明被违反 = P1；超容忍 = P2 疑点。"""
    out: list[Finding] = []
    if ctx.df.empty:
        return out
    n = len(ctx.df)
    for col in ctx.spec.columns:
        if col.name not in ctx.df.columns:
            continue
        rate = float(ctx.df[col.name].isna().mean())
        if rate <= 0:
            continue
        if not col.nullable and rate > 0:
            out.append(
                _finding(
                    ctx, "completeness", col.name, f"null_rate::{col.name}",
                    rate, 0.0, Severity.P1,
                    {"nulls": int(rate * n), "rows": n,
                     "note": f"声明 NOT NULL 但空值率 {rate:.1%}"},
                )
            )
        elif rate > col.null_tolerance:
            out.append(
                _finding(
                    ctx, "completeness", col.name, f"null_rate::{col.name}",
                    rate, col.null_tolerance, Severity.P2,
                    {"nulls": int(rate * n), "rows": n,
                     "note": f"空值率 {rate:.1%} 超容忍 {col.null_tolerance:.0%}"},
                )
            )
    return out


def probe_uniqueness(ctx: GenericContext) -> list[Finding]:
    """唯一性：主键重复。未声明主键 → 不产出（engine 在报告里注明降级）。"""
    out: list[Finding] = []
    pk = ctx.spec.primary_key
    if not pk or any(c not in ctx.df.columns for c in pk):
        return out
    sizes = ctx.df.groupby(list(pk), dropna=False).size()
    dups = sizes[sizes > 1]
    if dups.empty:
        return out
    n_dup_rows = int(dups.sum() - len(dups))
    out.append(
        _finding(
            ctx, "uniqueness", "*",
            "duplicate_keys", n_dup_rows, 0, Severity.P0,
            {"pk": list(pk), "dup_keys": int(len(dups)),
             "sample": [str(k) for k in dups.index[:5]]},
        )
    )
    return out


def probe_freshness(ctx: GenericContext) -> list[Finding]:
    """新鲜度：时间锚点列的最新值落后观察日多少天。声明了才查。"""
    ts = ctx.spec.timestamp_column
    if not ts or ts not in ctx.df.columns or ctx.df.empty:
        return out_empty()
    parsed = pd.to_datetime(ctx.df[ts], errors="coerce").dropna()
    if parsed.empty:
        return out_empty()
    latest = parsed.max()
    asof_ts = pd.Timestamp(ctx.asof)
    stale = (asof_ts - latest).days
    if stale > ctx.spec.freshness_days:
        return [
            _finding(
                ctx, "freshness", "*", "stale_days", stale,
                ctx.spec.freshness_days, Severity.P1,
                {"latest": str(latest), "asof": str(ctx.asof),
                 "note": f"数据停更 {stale} 天，超容忍 {ctx.spec.freshness_days:.0f} 天"},
            )
        ]
    if stale < 0:
        return [
            _finding(
                ctx, "freshness", "*", "future_timestamps", float(-stale), 0,
                Severity.P2,
                {"latest": str(latest), "asof": str(ctx.asof),
                 "note": "存在比观察日新的时间戳（时钟/时区问题？）"},
            )
        ]
    return []


def out_empty() -> list[Finding]:  # noqa: N802 — 小助手，命名让调用点读起来像句子
    return []


def probe_outliers(ctx: GenericContext) -> list[Finding]:
    """离群值：IQR 离群率 + 近期/历史均值水平漂移。数值声明列才查。

    声明 group_column 时在同组内计算（如按城市/标的分组）——把不同
    量级的实体混在一个分布里找离群点，是通用检查最常见的误报来源。
    """
    out: list[Finding] = []
    ts = ctx.spec.timestamp_column
    for col in ctx.spec.columns:
        if col.kind != "numeric" or col.name not in ctx.df.columns:
            continue
        s = pd.to_numeric(ctx.df[col.name], errors="coerce").dropna()
        if len(s) < 20:
            continue
        # 组内离群率（声明了分组列且列存在时）
        if ctx.spec.group_column and ctx.spec.group_column in ctx.df.columns:
            ratios = []
            for _, g in ctx.df.groupby(ctx.spec.group_column):
                v = pd.to_numeric(g[col.name], errors="coerce").dropna()
                if len(v) >= 20:
                    ratios.append(_iqr_outlier_rate(v))
            if not ratios:
                continue
            rate = max(ratios)
            scope = f"组内({ctx.spec.group_column})"
        else:
            rate = _iqr_outlier_rate(s)
            scope = "全列"
        if rate > ctx.spec.outlier_tolerance:
            out.append(
                _finding(
                    ctx, "outlier", col.name, f"outlier_rate::{col.name}",
                    rate, ctx.spec.outlier_tolerance, Severity.P2,
                    {"scope": scope, "note": f"IQR 离群率 {rate:.1%} 超容忍 "
                     f"{ctx.spec.outlier_tolerance:.0%}（{_IQR_K}×IQR 围栏）"},
                )
            )
        # 水平漂移：需要时间锚点排序
        if ts and ts in ctx.df.columns:
            shifted = _level_shift(ctx.df, ts, col.name, ctx.spec.group_column)
            if shifted is not None:
                ratio, recent_mean, hist_mean = shifted
                out.append(
                    _finding(
                        ctx, "outlier", col.name, f"level_shift::{col.name}",
                        round(ratio, 4), _LEVEL_UP, Severity.P1,
                        {"recent_mean": round(recent_mean, 4),
                         "history_mean": round(hist_mean, 4),
                         "note": f"近期均值/历史均值 = {ratio:.2f}，分布疑似整体迁移"},
                    )
                )
    return out


def _iqr_outlier_rate(s: pd.Series) -> float:
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return 0.0
    lo, hi = q1 - _IQR_K * iqr, q3 + _IQR_K * iqr
    return float(((s < lo) | (s > hi)).mean())


def _level_shift(df: pd.DataFrame, ts: str, col: str, group: str | None):
    """近期(最近 20%)均值 / 之前均值。组内漂移取最坏组。"""
    def _one(g: pd.DataFrame):
        g = g.sort_values(ts)
        v = pd.to_numeric(g[col], errors="coerce").dropna()
        n = len(v)
        if n < _MIN_HISTORY + 5:
            return None
        cut = int(n * (1 - _RECENT_FRACTION))
        hist, recent = v.iloc[:cut], v.iloc[cut:]
        if len(hist) < _MIN_HISTORY or hist.mean() <= 0:
            return None
        ratio = float(recent.mean() / hist.mean())
        if _LEVEL_DOWN < ratio < _LEVEL_UP:
            return None
        return ratio, float(recent.mean()), float(hist.mean())

    candidates = []
    if group and group in df.columns:
        for _, g in df.groupby(group):
            r = _one(g)
            if r is not None:
                candidates.append(r)
    else:
        r = _one(df)
        if r is not None:
            candidates.append(r)
    if not candidates:
        return None
    # 最坏 = 比值离 1 最远
    return max(candidates, key=lambda t: max(t[0], 1 / t[0]))


PROBES = (
    probe_schema_conformance,
    probe_completeness,
    probe_uniqueness,
    probe_freshness,
    probe_outliers,
)


def run_probes(ctx: GenericContext) -> list[Finding]:
    """跑全部通用探针。单探针异常不影响其他探针（与金融域同约定）。"""
    findings: list[Finding] = []
    for probe in PROBES:
        try:
            findings.extend(probe(ctx))
        except Exception:  # noqa: BLE001 — 探针健壮性优先，别让脏数据打挂巡检
            continue
    return findings
