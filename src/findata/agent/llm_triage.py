"""LLM 归因器。

设计上最重要的不是 prompt，而是**不信任模型**：

1. 输出必须是受控 JSON，根因与级别只能取白名单内的值
2. 任何一条不合法、缺失、解析失败 → 该条**降级为规则归因器的结论**
3. 整批调用失败（无 key / 超时 / 限流）→ 全批降级，巡检照常产出

理由很直接：归因层是全系统的判断中枢，让它依赖一个会幻觉的组件，
就必须保证它最坏情况退化成确定性规则，而不是退化成随机噪声。
这样"接 LLM"才是可上线的改动，而不是一个只能演示的玩具。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from findata.agent.llm import LLMClient, LLMError
from findata.dq.models import DIAGNOSABLE_CAUSES, Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import ProbeContext
from findata.dq.triage import (
    _coverage,
    _failed_run,
    _halted_days,
    _has_event,
    _list_date,
    _parse_window,
)

_BATCH = 8  # 每批信号数。太大容易触发截断，太小浪费往返。

_SYSTEM = """你是 A 股数据质量归因专家。

任务：给定若干数据质量信号及其证据，判断每个信号的**根因**与**告警级别**。

铁律：
1. 只依据证据判断，不要臆测证据里没有的事实。
2. 数据缺失不等于故障：停牌、非交易日、新股上市导致的缺失是合法的，必须判为 benign_*。
3. 证据里没有明确指向故障时，宁可判 benign_*，也不要为了"看起来有用"编造故障。
4. 只输出 JSON，不要任何解释性文字、不要代码块标记。

可选根因（必须原样取自此列表）：
{causes}

输出格式：
{{"results":[{{"index":0,"root_cause":"upstream_stale","severity":"P0","confidence":0.9,"explanation":"一句话说明判断依据"}}]}}"""

_USER = """观察日：{asof}

信号与证据：
{payload}

请逐个判断，index 必须与输入一致，不要遗漏。"""


@dataclass
class TriageStats:
    """归因过程可观测：到底有多少条是真由模型判的。"""

    total: int = 0
    from_llm: int = 0
    fallback: int = 0
    rejected: int = 0  # 模型给了答案但不合法，被拒收
    errors: list[str] = field(default_factory=list)

    @property
    def llm_coverage(self) -> float:
        return self.from_llm / self.total if self.total else 0.0


def build_evidence(finding: Finding, ctx: ProbeContext) -> dict:
    """把一个信号连同它的上下文证据打包成模型可见的事实。

    这一步是 LLM 归因器的真正价值所在：规则归因器用的也是同一批证据，
    因此两者的差异只可能来自"推理"，不会来自"看到的东西不一样"。
    """
    parsed = _parse_window(finding.window)
    ev: dict = {
        "index": 0,
        "probe": finding.probe,
        "table": finding.table,
        "symbol": finding.symbol,
        "window": finding.window,
        "metric": finding.metric,
        "value": finding.value,
        "threshold": finding.threshold,
    }
    if parsed is None:
        return ev
    start, end = parsed

    if finding.probe in ("freshness", "completeness", "history_depth"):
        halted = _halted_days(ctx, finding.symbol)
        ev["suspension_coverage"] = round(_coverage(ctx, start, end, halted), 3)
        ev["is_tail_window"] = end >= ctx.asof
    ev["has_ex_dividend"] = _has_event(ctx, finding.symbol, "ex_dividend", start, end)
    ld = _list_date(ctx, finding.symbol)
    ev["list_date"] = ld.isoformat() if isinstance(ld, date) else None
    ev["trading_days_in_window"] = len(ctx.calendar.trading_days(start, end))
    ev["present_days_in_window"] = _present_days(ctx, finding, start, end)
    run_id = _failed_run(ctx, finding.symbol, finding.table)
    ev["failed_ingest_run"] = run_id
    return ev


def _present_days(ctx: ProbeContext, finding: Finding, start: date, end: date) -> int:
    frame = ctx.stock_daily if finding.table == "stock_daily" else ctx.valuation_daily
    if frame.empty or "symbol" not in frame.columns:
        return 0
    sub = frame[
        (frame["symbol"] == finding.symbol) & (frame["date"] >= start) & (frame["date"] <= end)
    ]
    return int(len(sub))


class LLMTriage:
    """LLM 归因器。实现了 Triage 协议，可直接替换 BaselineTriage 跑同一套评测。"""

    def __init__(self, client: LLMClient, fallback=None) -> None:
        self.client = client
        from findata.dq.triage import BaselineTriage

        self.fallback = fallback or BaselineTriage()
        self.stats = TriageStats()

    def diagnose_all(self, findings: list[Finding], ctx: ProbeContext) -> list[Diagnosis]:
        self.stats = TriageStats(total=len(findings))
        if not findings:
            return []

        out: list[Diagnosis] = [self.fallback.diagnose(f, ctx) for f in findings]
        for batch_start in range(0, len(findings), _BATCH):
            batch = list(range(batch_start, min(batch_start + _BATCH, len(findings))))
            self._run_batch(findings, ctx, batch, out)
        # 凡是不是模型判出来的，都算降级——这是衡量 LLM 真实参与度的分子。
        self.stats.fallback = self.stats.total - self.stats.from_llm
        return out

    def _run_batch(
        self,
        findings: list[Finding],
        ctx: ProbeContext,
        indexes: list[int],
        out: list[Diagnosis],
    ) -> None:
        items = []
        for i in indexes:
            ev = build_evidence(findings[i], ctx)
            ev["index"] = i
            items.append({**ev, "evidence": findings[i].evidence})
        payload = json.dumps({"findings": items}, ensure_ascii=False, indent=1)

        try:
            raw = self.client.complete(
                _SYSTEM.format(causes=", ".join(DIAGNOSABLE_CAUSES)),
                _USER.format(asof=ctx.asof, payload=payload),
            )
            parsed = self._parse(raw)
        except LLMError as e:
            # 整批失败：保留规则结论，只记录，不打断巡检。
            self.stats.errors.append(str(e))
            return

        for i in indexes:
            item = parsed.get(i)
            if item is None:
                continue  # 模型漏答：保留规则结论，算降级但不算拒收
            diagnosis = self._validate(item, findings[i], ctx)
            if diagnosis is None:
                self.stats.rejected += 1
                continue
            out[i] = diagnosis
            self.stats.from_llm += 1

    def _parse(self, raw: str) -> dict[int, dict]:
        """从回复里提取 {index: 结论}。容忍代码块包裹与多余文本。"""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json", "", 1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            doc = json.loads(text[start : end + 1])
        except ValueError:
            return {}
        rows = doc.get("results") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return {}
        out: dict[int, dict] = {}
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("index"), int):
                out[row["index"]] = row
        return out

    def _validate(self, item: dict | None, finding: Finding, ctx: ProbeContext) -> Diagnosis | None:
        """把模型输出校验成可信结论；不合法返回 None，由调用方降级。"""
        if not item:
            return None
        cause_raw = str(item.get("root_cause", "")).strip()
        sev_raw = str(item.get("severity", "")).strip()
        try:
            cause = RootCause(cause_raw)
        except ValueError:
            return None
        try:
            severity = Severity(sev_raw)
        except ValueError:
            return None
        if cause.value not in _ALLOWED_CAUSES:
            return None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        explanation = str(item.get("explanation", "")).strip()
        if not explanation:
            return None
        return Diagnosis(
            finding_key=finding.key,
            root_cause=cause,
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            explanation=explanation,
            evidence_refs=("llm",),
        )


# UNKNOWN 不允许由模型直接给出：模型说"不知道"没有信息量，
# 这种时候降级给规则归因器反而更可靠。
_ALLOWED_CAUSES = frozenset(DIAGNOSABLE_CAUSES)
