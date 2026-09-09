"""评测 runner：合成基线 → 注入故障 → 跑探针 → 归因 → 比对标注。

triage 是可注入的，这是刻意设计：后面接 LLM 归因器时，
同一个 runner 换个 triage 就能直接对比，用同一把尺子量两种方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from findata.dq.models import Diagnosis, Finding
from findata.dq.probes import ProbeContext, run_probes
from findata.dq.protocol import Triage
from findata.dq.triage import BaselineTriage
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
    # 一致性 κ（修正随机一致，Cohen 1960）由本模块的 cohen_kappa 计算
    baseline_diagnoses: list[Diagnosis] = field(default_factory=list)
    llm_diagnoses: list[Diagnosis] = field(default_factory=list)
    triage_agreement: dict | None = None  # 根因 κ / 抑制 κ / 对比样本数
    # 参与对比的 LLM 后端："baseline" / "mock" / "openai_compat"，供调用方如实展示
    llm_backend: str = "baseline"


def run_evaluation(
    cfg: SyntheticConfig | None = None,
    fault_seed: int = 20240102,
    triage: Triage | None = None,
    second_triage: Triage | None = None,
) -> EvalResult:
    """跑一轮完整评测。相同参数必得相同结果。

    second_triage 给定时，同一份 findings 会再跑一次归因，产出两套
    diagnoses 的 Cohen's κ 一致性指标——这是评估"两套归因器差异"
    的统计量。归因器只依赖 dq.protocol.Triage 协议，换实现不动评测。
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

    κ 用本模块的 cohen_kappa（Cohen 1960 标准定义）。
    曾经依赖外部兄弟仓库的实现，因破坏"任何干净环境可复现"而收敛为本地实现。
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

    return {
        "kappa_root_cause": cohen_kappa(causes_a, causes_b),
        "kappa_suppress": cohen_kappa([int(x) for x in sup_a], [int(x) for x in sup_b]),
        "n_compared": len(causes_a),
    }


def cohen_kappa(y1: list, y2: list) -> float | None:
    """Cohen's κ（1960）：po 为观测一致率，pe 为边际频率期望一致率。

    完全一致 = 1.0；与随机抽签一致 = 0.0；< 0 = 比随机还差。
    n=0 或两序列类别退化到 pe=1（无可修正的一致）时返回 None。
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
