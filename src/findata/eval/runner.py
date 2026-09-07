"""评测 runner：合成基线 → 注入故障 → 跑探针 → 归因 → 比对标注。

triage 是可注入的，这是刻意设计：后面接 LLM 归因器时，
同一个 runner 换个 triage 就能直接对比，用同一把尺子量两种方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from findata.dq.models import Diagnosis, Finding
from findata.dq.probes import ProbeContext, run_probes
from findata.dq.triage import BaselineTriage
from findata.eval._interop import import_curation_eval
from findata.eval.faults import InjectedFault, benign_expectations, inject_faults
from findata.eval.fixtures import SyntheticConfig, build_baseline
from findata.eval.metrics import EvalReport, evaluate


@dataclass
class EvalResult:
    report: EvalReport
    findings: list[Finding]
    diagnoses: list[Diagnosis]
    faults: list[InjectedFault]
    benigns: list[InjectedFault]
    # 当 baseline + llm 两条归因都跑了，这里有两份 diagnoses；
    # 一致性 κ（修正随机一致）由 curation-eval 的 Cohen's κ 实现
    baseline_diagnoses: list[Diagnosis] = field(default_factory=list)
    llm_diagnoses: list[Diagnosis] = field(default_factory=list)
    triage_agreement: dict | None = None  # 根因 κ / 抑制 κ / 来源


def run_evaluation(
    cfg: SyntheticConfig | None = None,
    fault_seed: int = 20240102,
    triage: BaselineTriage | None = None,
    second_triage: BaselineTriage | None = None,
) -> EvalResult:
    """跑一轮完整评测。相同参数必得相同结果。

    second_triage 给定时，同一份 findings 会再跑一次归因，产出两套
    diagnoses 的 Cohen's κ 一致性指标——这是评估"两套归因器差异"
    的统计量，借自 mm-curation 的 curation-eval。
    """
    cfg = cfg or SyntheticConfig()
    triage = triage or BaselineTriage()

    baseline = build_baseline(cfg)
    injected = inject_faults(baseline, seed=fault_seed)

    ctx = ProbeContext.of(
        stock_daily=injected.stock_daily,
        valuation_daily=injected.valuation_daily,
        universe=baseline.universe,
        corporate_event=baseline.corporate_event,
        calendar=baseline.calendar,
    )

    findings = run_probes(ctx)
    baseline_dx = triage.diagnose_all(findings, ctx)
    benigns = benign_expectations(baseline)
    report = evaluate(findings, baseline_dx, injected.faults, benigns)

    result = EvalResult(
        report=report,
        findings=findings,
        diagnoses=baseline_dx,
        baseline_diagnoses=baseline_dx,
        faults=injected.faults,
        benigns=benigns,
    )

    if second_triage is not None:
        llm_dx = second_triage.diagnose_all(findings, ctx)
        result.llm_diagnoses = llm_dx
        result.triage_agreement = _agreement(baseline_dx, llm_dx)

    return result


def _agreement(
    a: list[Diagnosis], b: list[Diagnosis]
) -> dict:
    """两套归因器的一致性：根因标签 κ + 抑制决策 κ。

    借自 mm-curation-pipeline 的 `curation_eval.cohen_kappa`：
    见 findata/eval/_interop.py 的边界说明。包未就绪时优雅降级。
    """
    by_key = {d.finding_key: d for d in a}
    causes_a: list[str] = []
    causes_b: list[str] = []
    sup_a: list[bool] = []
    sup_b: list[bool] = []
    for dx in b:
        peer = by_key.get(dx.finding_key)
        if peer is None:
            continue
        causes_a.append(peer.root_cause.value)
        causes_b.append(dx.root_cause.value)
        sup_a.append(peer.suppressed)
        sup_b.append(dx.suppressed)

    source = "local"  # 默认用本地实现
    k_cause = _local_kappa(causes_a, causes_b)
    k_suppress = _local_kappa([int(x) for x in sup_a], [int(x) for x in sup_b])

    ce = import_curation_eval()
    if ce is not None:
        k_cause_ce = ce.cohen_kappa(causes_a, causes_b)
        k_suppress_ce = ce.cohen_kappa([int(x) for x in sup_a], [int(x) for x in sup_b])
        if k_cause_ce is not None:
            k_cause = k_cause_ce
        if k_suppress_ce is not None:
            k_suppress = k_suppress_ce
        source = "curation_eval"

    return {
        "kappa_root_cause": k_cause,
        "kappa_suppress": k_suppress,
        "n_compared": len(causes_a),
        "source": source,
    }


def _local_kappa(y1, y2):
    """与 curation_eval.cohen_kappa 同语义，本地副本用于包未就绪时降级。

    边际概率用 empirical 频率；n=0 或 p_e=1 时返回 None（无可评）。
    """
    if len(y1) != len(y2) or len(y1) == 0:
        return None
    n = len(y1)
    labels = set(y1) | set(y2)
    po = sum(1 for a, b in zip(y1, y2, strict=True) if a == b) / n
    pe = sum(
        (sum(1 for a in y1 if a == c) / n) * (sum(1 for b in y2 if b == c) / n)
        for c in labels
    )
    if pe == 1.0:
        return None
    return (po - pe) / (1.0 - pe)
