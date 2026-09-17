"""通用归因器：把通用探针的 Finding 判成 Diagnosis。

通用包的定义性约束（诚实边界，ROADMAP 第五节）：**它没有领域知识，
因此永远给不出 BENIGN_*/抑制，也永远到不了「✓ 已核验」**。它能说的
只有三句话：这是故障（✗/告警）、这是疑点（⚠️）、没查出毛病（✓ 基线
通过）。这三句话之间的差距——"缺 6 行是停牌还是漏采"——需要领域包。

这条边界有测试钉死（tests/test_generic.py::test_generic_never_verifies）：
任何 Finding 走完通用归因都不允许出现 benign 根因或 suppressed 结论。
"""

from __future__ import annotations

from findata.dq.models import Diagnosis, Finding, RootCause
from findata.generic.probes import GenericContext

_ROUTE_METRICS: dict[str, tuple[RootCause, str]] = {
    # metric 前缀 → (根因, 人话结论模板里的动词)
    "column_missing": (RootCause.SCHEMA_NULL_SWEEP, "声明列缺失"),
    "type_mismatch": (RootCause.SCHEMA_NULL_SWEEP, "类型漂移"),
    "null_rate": (RootCause.NULL_RATE_BREACH, "空值超容忍"),
    "duplicate_keys": (RootCause.DUPLICATE_WRITE, "主键重复"),
    "stale_days": (RootCause.UPSTREAM_STALE, "数据停更"),
    "future_timestamps": (RootCause.VALUE_OUT_OF_RANGE, "时间戳异常"),
    "outlier_rate": (RootCause.OUTLIER_BURST, "离群值超容忍"),
    "level_shift": (RootCause.DISTRIBUTION_DRIFT, "分布水平迁移"),
    "column_extra": (RootCause.SCHEMA_NULL_SWEEP, "未声明列"),
}


class GenericTriage:
    """通用规则归因器。与金融域 BaselineTriage 同约定：纯函数、可单测。

    唯一但它最重要的规则：**没有任何一条路由通向 BENIGN_***。通用包
    抑制率为 0 是特性不是缺陷——它没有资格宣称"这是合法业务事件"。
    """

    def diagnose(self, finding: Finding, ctx: GenericContext) -> Diagnosis:
        root = None
        for prefix, (cause, _verb) in _ROUTE_METRICS.items():
            if finding.metric == prefix or finding.metric.startswith(prefix + "::"):
                root = cause
                break
        if root is None:
            # 未识别的信号保守保留原级别——不许静默丢弃
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UNKNOWN,
                severity=finding.severity_hint,
                confidence=0.3,
                explanation=f"通用包未识别信号 {finding.metric}，保守保留原级别",
            )
        assert not root.is_benign, "通用归因器禁止输出合法事件结论（诚实边界）"
        severity = finding.severity_hint
        note = str(finding.evidence.get("note", ""))
        return Diagnosis(
            finding_key=finding.key,
            root_cause=root,
            severity=severity,
            confidence=0.75,
            explanation=note
            or f"{finding.metric} 命中（观测 {finding.value}，阈值 {finding.threshold}）",
        )

    def diagnose_all(self, findings: list[Finding], ctx: GenericContext) -> list[Diagnosis]:
        return [self.diagnose(f, ctx) for f in findings]
