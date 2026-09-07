"""评测指标：故障召回、根因准确、误报抑制、告警精确。

四个指标分别回答四个不同的问题，缺一个都会被自己的系统骗过去：

- **故障召回率**：注入的故障找出了几个？（低了说明探针瞎）
- **根因准确率**：找出的问题里根因说对几个？（低了说明归因瞎）
- **误报抑制率**：停牌/除权/新股这些合法异常，有没有正确地不告警？
  这是本项目最想证明的能力——告警疲劳才是行业真痛点
- **告警精确率**：每天收到的告警里，真问题占多少？
  这是决定"这个产品有没有人用"的指标。一个每天报 200 条、
  其中 190 条是噪音的系统，等价于没有系统
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from findata.dq.models import Diagnosis, Finding
from findata.eval.faults import InjectedFault


def _parse(w: str) -> tuple[date, date] | None:
    if ".." not in w:
        return None
    head, tail = w.split("..", 1)
    try:
        return date.fromisoformat(head), date.fromisoformat(tail)
    except ValueError:
        return None


def _overlap(a: str, b: str) -> bool:
    pa, pb = _parse(a), _parse(b)
    if pa is None or pb is None:
        return False
    return pa[0] <= pb[1] and pb[0] <= pa[1]


def _matches(finding: Finding, fault: InjectedFault) -> bool:
    """Finding 是否落在该故障的作用范围内。

    corporate_event 是事件表本身，不是被观测的表，匹配时跳过表名比对。
    """
    if finding.symbol != fault.symbol:
        return False
    if fault.table != "corporate_event" and finding.table != fault.table:
        return False
    return _overlap(finding.window, fault.window)


@dataclass
class EvalReport:
    n_findings: int = 0
    n_alerts: int = 0  # 未被抑制的告警数

    n_faults: int = 0
    n_fault_detected: int = 0
    fault_recall: float = 0.0

    n_root_cause_correct: int = 0
    root_cause_accuracy: float = 0.0

    n_severity_correct: int = 0
    severity_accuracy: float = 0.0

    n_benign: int = 0
    n_benign_signaled: int = 0  # 合法事件中确实触发了信号的个数
    n_benign_suppressed: int = 0
    benign_suppression: float = 0.0

    alert_precision: float = 0.0
    n_false_positive: int = 0

    # 朴素基线：不做归因，把所有探针信号都当告警。
    # 与 alert_precision 的差值，就是归因层带来的真实价值。
    naive_alert_precision: float = 0.0

    misses: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, str]]:
        """报告行，便于 CLI 打印。"""
        return [
            ("注入故障数", str(self.n_faults)),
            ("故障召回率", f"{self.fault_recall:.1%}  ({self.n_fault_detected}/{self.n_faults})"),
            (
                "根因准确率",
                f"{self.root_cause_accuracy:.1%}  ({self.n_root_cause_correct}/{self.n_faults})",
            ),
            (
                "分级准确率",
                f"{self.severity_accuracy:.1%}  ({self.n_severity_correct}/{self.n_faults})",
            ),
            ("合法事件数", str(self.n_benign)),
            (
                "误报抑制率",
                f"{self.benign_suppression:.1%}  "
                f"({self.n_benign_suppressed}/{self.n_benign})",
            ),
            (
                "告警精确率",
                f"{self.alert_precision:.1%}  "
                f"({self.n_root_cause_correct}"
                f"/{self.n_root_cause_correct + self.n_false_positive})",
            ),
            (
                "  其中朴素基线",
                f"{self.naive_alert_precision:.1%}（不做归因、全部告警）",
            ),
            ("总信号数", f"{self.n_findings}（归因后告警 {self.n_alerts}）"),
        ]


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def evaluate(
    findings: list[Finding],
    diagnoses: list[Diagnosis],
    faults: list[InjectedFault],
    benigns: list[InjectedFault],
) -> EvalReport:
    """比对系统输出与标注，产出评测报告。"""
    by_key = {d.finding_key: d for d in diagnoses}
    report = EvalReport(
        n_findings=len(findings),
        n_alerts=sum(1 for d in diagnoses if not d.suppressed),
    )

    fault_keys: set[str] = set()
    benign_keys: set[str] = set()

    report.n_faults = len(faults)
    for fault in faults:
        matched = [f for f in findings if _matches(f, fault)]
        if matched:
            report.n_fault_detected += 1
        else:
            report.misses.append(f"{fault.kind}@{fault.symbol} {fault.window}")
        hit_cause = False
        hit_sev = False
        for f in matched:
            fault_keys.add(f.key)
            d = by_key.get(f.key)
            if d is None:
                continue
            if d.root_cause == fault.expected_root_cause:
                hit_cause = True
            if d.severity == fault.expected_severity:
                hit_sev = True
        report.n_root_cause_correct += int(hit_cause)
        report.n_severity_correct += int(hit_sev)

    report.n_benign = len(benigns)
    for b in benigns:
        matched = [f for f in findings if _matches(f, b)]
        if matched:
            report.n_benign_signaled += 1
        for f in matched:
            benign_keys.add(f.key)
        ok = any(by_key[f.key].suppressed for f in matched if f.key in by_key)
        report.n_benign_suppressed += int(ok)

    for d in diagnoses:
        if d.suppressed or d.finding_key in fault_keys or d.finding_key in benign_keys:
            continue
        report.n_false_positive += 1
        report.false_positives.append(f"{d.root_cause.value}@{d.finding_key}")

    report.fault_recall = _ratio(report.n_fault_detected, report.n_faults)
    report.root_cause_accuracy = _ratio(report.n_root_cause_correct, report.n_faults)
    report.severity_accuracy = _ratio(report.n_severity_correct, report.n_faults)
    report.benign_suppression = _ratio(report.n_benign_suppressed, report.n_benign)
    tp = report.n_root_cause_correct
    report.alert_precision = _ratio(tp, tp + report.n_false_positive)
    # 朴素基线：探针报多少就告警多少，分母是全部信号
    report.naive_alert_precision = _ratio(len(fault_keys), len(findings))
    return report
