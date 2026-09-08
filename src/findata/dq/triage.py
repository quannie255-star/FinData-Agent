"""规则归因器（baseline）：把 Finding 判成 Diagnosis。

它存在的三个理由，缺一不可：

1. **零成本可跑**：不依赖 LLM，离线、确定性、CI 里能当门禁
2. **作为对比基线**：后面接 LLM 归因器时，用同一套评测集证明增量价值，
   而不是拍脑袋说"加了大模型效果更好"——那句话面试官听过一百遍
3. **把领域知识显式化**：规则写清楚之后，LLM 到底该学什么也就清楚了。
   规则覆盖不到的部分（模糊归因、跨源推理）才是 LLM 真正的用武之地

核心思路：同一个"数据缺失"信号，结合不同证据会指向完全不同的结论——
- 窗口落在停牌区间 → 合法，抑制
- 窗口内本就没有交易日 → 合法，抑制
- 数据完整但上市晚 → 新股，抑制
- 采集血缘里有 failed run → 上游故障，P0
- 尾部停更 → 停更；中间空洞 → 漏采
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import _MIN_HISTORY_DAYS, ProbeContext

# 窗口内被合法事件覆盖达到这个比例，即判定为可抑制
_BENIGN_COVERAGE = 0.9
# 上市后数据完整率达到这个比例，才敢认定"历史短"是上市晚导致而非数据缺失
_COMPLETE_RATIO = 0.9


def _parse_window(window: str) -> tuple[date, date] | None:
    if ".." not in window:
        return None
    head, tail = window.split("..", 1)
    try:
        return date.fromisoformat(head), date.fromisoformat(tail)
    except ValueError:
        return None


def _list_date(ctx: ProbeContext, symbol: str) -> date | None:
    if ctx.universe.empty or "list_date" not in ctx.universe.columns:
        return None
    sub = ctx.universe.loc[ctx.universe["symbol"] == symbol, "list_date"]
    if len(sub) and pd.notna(sub.iloc[0]):
        return sub.iloc[0]
    return None


def _halted_days(ctx: ProbeContext, symbol: str) -> set[date]:
    """该股票处于停牌状态的全部交易日。"""
    if ctx.corporate_event.empty or "kind" not in ctx.corporate_event.columns:
        return set()
    sub = ctx.corporate_event[
        (ctx.corporate_event["symbol"] == symbol) & (ctx.corporate_event["kind"] == "suspension")
    ]
    out: set[date] = set()
    for row in sub.itertuples(index=False):
        end = row.end_date if pd.notna(row.end_date) else row.date
        out |= set(ctx.calendar.trading_days(row.date, end))
    return out


def _coverage(ctx: ProbeContext, start: date, end: date, day_set: set[date]) -> float:
    days = ctx.calendar.trading_days(start, end)
    if not days:
        return 0.0
    return sum(1 for d in days if d in day_set) / len(days)


def _has_event(ctx: ProbeContext, symbol: str, kind: str, start: date, end: date) -> bool:
    if ctx.corporate_event.empty or "kind" not in ctx.corporate_event.columns:
        return False
    sub = ctx.corporate_event[
        (ctx.corporate_event["symbol"] == symbol)
        & (ctx.corporate_event["kind"] == kind)
        & (ctx.corporate_event["date"] >= start)
        & (ctx.corporate_event["date"] <= end)
    ]
    return not sub.empty


def _failed_run(ctx: ProbeContext, symbol: str, table: str) -> str | None:
    """血缘表里是否存在失败采集。这是"数据缺失"能归因到上游的直接证据。"""
    if ctx.ingest_run.empty or "status" not in ctx.ingest_run.columns:
        return None
    sub = ctx.ingest_run[
        (ctx.ingest_run["target_table"] == table) & (ctx.ingest_run["status"] == "failed")
    ]
    for row in sub.itertuples(index=False):
        raw = getattr(row, "params", None)
        try:
            params = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            params = {}
        if params.get("symbol") in (None, symbol):
            return str(row.run_id)
    return None


class BaselineTriage:
    """规则归因器。diagnose 是纯函数，便于单测与替换。"""

    def diagnose(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        route = {
            "freshness": self._diagnose_gap,
            "completeness": self._diagnose_gap,
            "history_depth": self._diagnose_depth,
            "validity": self._diagnose_validity,
            "uniqueness": self._diagnose_uniqueness,
            "drift": self._diagnose_drift,
            "null_sweep": self._diagnose_null,
        }.get(finding.probe)
        if route is None:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UNKNOWN,
                severity=finding.severity_hint,
                confidence=0.3,
                explanation=f"探针 {finding.probe} 无匹配规则，保守保留原级别",
            )
        return route(finding, ctx)

    def diagnose_all(self, findings: list[Finding], ctx: ProbeContext) -> list[Diagnosis]:
        return [self.diagnose(f, ctx) for f in findings]

    # ---- 各类信号的归因规则 ----

    def _diagnose_gap(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        parsed = _parse_window(finding.window)
        if parsed is None:
            return self._unknown(finding, "窗口格式无法解析")

        start, end = parsed
        halted = _halted_days(ctx, finding.symbol)
        cov = _coverage(ctx, start, end, halted)
        if cov >= _BENIGN_COVERAGE:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_SUSPENSION,
                severity=Severity.OK,
                confidence=0.95,
                explanation=(
                    f"窗口内 {cov:.0%} 交易日处于停牌区间，缺失为合法业务状态，告警已抑制"
                ),
                evidence_refs=(f"corporate_event:suspension:{finding.symbol}",),
            )

        if not ctx.calendar.trading_days(start, end):
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_NON_TRADING_DAY,
                severity=Severity.OK,
                confidence=0.95,
                explanation="窗口内不含交易日，本就不应有数据，告警已抑制",
            )

        run_id = _failed_run(ctx, finding.symbol, finding.table)
        if run_id:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UPSTREAM_OUTAGE,
                severity=Severity.P0,
                confidence=0.9,
                explanation=f"血缘表记录采集任务 {run_id} 失败，数据缺失由上游故障导致",
                evidence_refs=(f"ingest_run:{run_id}",),
            )

        if finding.probe == "freshness":
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UPSTREAM_STALE,
                severity=Severity.P0 if finding.value >= 2 else Severity.P1,
                confidence=0.8,
                explanation=(
                    f"最新数据落后 {int(finding.value)} 个交易日，且无停牌/新股可解释，"
                    "判定为上游停更"
                ),
            )

        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.MISSING_ROWS,
            severity=finding.severity_hint,
            confidence=0.75,
            explanation=f"中间出现 {int(finding.value)} 个交易日空洞，判定为回补漏采",
        )

    def _diagnose_depth(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        n = int(finding.value)
        ld = _list_date(ctx, finding.symbol)
        if ld is not None:
            expected = ctx.calendar.trading_days(ld, ctx.asof)
            if expected:
                ratio = n / len(expected)
                if ratio >= _COMPLETE_RATIO and len(expected) <= _MIN_HISTORY_DAYS:
                    return Diagnosis(
                        finding_key=finding.key,
                        root_cause=RootCause.BENIGN_NEW_LISTING,
                        severity=Severity.OK,
                        confidence=0.9,
                        explanation=(
                            f"上市日 {ld}，应到 {len(expected)} 个交易日、实到 {n} 个，"
                            "数据完整，历史短属正常，告警已抑制"
                        ),
                        evidence_refs=(f"stock_universe:list_date:{finding.symbol}",),
                    )
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.MISSING_ROWS,
            severity=Severity.P1,
            confidence=0.6,
            explanation=f"历史仅 {n} 个交易日且无法用上市日解释，怀疑数据被删",
        )

    def _diagnose_validity(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        parsed = _parse_window(finding.window)
        if (
            finding.metric == "pct_chg_out_of_limit"
            and parsed is not None
            and _has_event(ctx, finding.symbol, "ex_rights", parsed[0], parsed[1])
        ):
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_CORPORATE_ACTION,
                severity=Severity.OK,
                confidence=0.9,
                explanation="当日存在除权除息事件，价格跳空为合法复权行为，告警已抑制",
                evidence_refs=(f"corporate_event:ex_rights:{finding.symbol}",),
            )
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.VALUE_OUT_OF_RANGE,
            severity=Severity.P0,
            confidence=0.85,
            explanation=f"{finding.metric} 命中 {int(finding.value)} 条，且无公司事件可解释",
        )

    def _diagnose_uniqueness(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.DUPLICATE_WRITE,
            severity=Severity.P0,
            confidence=0.95,
            explanation=f"{int(finding.value)} 个主键重复，任务重跑未做幂等",
        )

    def _diagnose_drift(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        if finding.metric == "total_mv_level_shift":
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UNIT_SHIFT,
                severity=Severity.P0,
                confidence=0.9,
                explanation=f"量级突变至 {finding.value:.6g} 倍，判定为单位或口径变更",
            )

        # 注意（已知局限）：音量放大无法在当前特征集下区分「单位/口径变更」与
        # 「市场放量」。探针算的是 10 日均量 / 前 30 日均量，会把注入的 20x
        # 稀释成 5.14x，而真实政策行情的放量是 5.40x —— 两者数值上几乎重合；
        # 价格判据同样无效（合成语料的价格本就是随机漫步，10 日涨跌 10% 很常见）。
        # 要有把握地区分，得引入外部业务事实（交易所口径变更公告），
        # 与停牌 / 上市日属于同一类问题：缺的不是算法，是事实来源。
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.DISTRIBUTION_DRIFT,
            severity=Severity.P1,
            confidence=0.8,
            explanation=f"近期均值/历史均值 = {finding.value:.2f}，超出漂移阈值",
        )

    def _diagnose_null(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        col = finding.metric.split("::")[-1]
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.SCHEMA_NULL_SWEEP,
            severity=Severity.P1,
            confidence=0.85,
            explanation=f"列 {col} 近期空值率 {finding.value:.0%}，远高于历史，"
            "判定为上游字段下线",
        )

    def _unknown(self, finding: Finding, why: str) -> Diagnosis:
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.UNKNOWN,
            severity=finding.severity_hint,
            confidence=0.3,
            explanation=why,
        )
