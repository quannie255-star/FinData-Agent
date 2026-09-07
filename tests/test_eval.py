"""评测闭环单测：确定性、质量门禁、归因层的增量价值。

第三条最重要：如果归因层的告警精确率不高于"探针报什么就告警什么"的朴素基线，
那这一层就是白做的——这正是要被 CI 拦住的回归。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from findata.agent import LLMTriage, MockLLMClient
from findata.eval.runner import run_evaluation


def test_evaluation_is_deterministic():
    """同参数两次运行结果必须完全一致，否则 CI 门禁没有意义。"""
    first = run_evaluation()
    second = run_evaluation()
    assert asdict(first.report) == asdict(second.report)


def test_corpus_is_populated():
    result = run_evaluation()
    assert result.report.n_faults >= 5, "注入故障数不足，评测集失效"
    assert result.report.n_benign >= 3, "合法事件数不足，误报抑制无从验证"


def test_quality_gate_passes():
    report = run_evaluation().report
    assert report.fault_recall >= 0.9, f"故障召回率过低：{report.fault_recall:.1%}"
    assert report.root_cause_accuracy >= 0.9, f"根因准确率过低：{report.root_cause_accuracy:.1%}"
    assert report.benign_suppression >= 0.9, f"误报抑制率过低：{report.benign_suppression:.1%}"


def test_triage_beats_naive_baseline():
    """归因层必须显著优于朴素基线，否则这一层不值得存在。"""
    report = run_evaluation().report
    assert report.alert_precision > report.naive_alert_precision, (
        f"归因层未带来增益：{report.alert_precision:.1%} "
        f"vs 朴素基线 {report.naive_alert_precision:.1%}"
    )
    assert report.n_alerts < report.n_findings, "归因层未抑制任何告警"


def test_triage_agreement_kappa_via_curation_eval():
    """双归因器一致性 κ 必须经由 mm-curation 的 curation_eval 计算。

    source=curation_eval 是 B1 的"真实借用"证据——如果哪天
    import_curation_eval 退化失败，本测试会立刻报警。
    """
    # 让 LLM 把所有 finding 都判成同一个合法根因
    flat_reply = json.dumps(
        {
            "results": [
                {
                    "index": i,
                    "root_cause": "upstream_stale",
                    "severity": "P1",
                    "confidence": 0.5,
                    "explanation": "mock",
                }
                for i in range(20)
            ]
        }
    )
    llm = LLMTriage(MockLLMClient(default=flat_reply))
    result = run_evaluation(fault_seed=20240102, second_triage=llm)
    agreement = result.triage_agreement
    assert agreement is not None
    assert agreement["source"] == "curation_eval", (
        "B1 借用失败：未走 curation_eval.cohen_kappa，"
        "可能 import_curation_eval() 已退化"
    )
    assert agreement["n_compared"] > 0
    # 至少要算出一个 κ（n>0 且 label 类别数>1）；退化为单类别时为 None 也算通过
    assert agreement["kappa_root_cause"] is not None


def test_triage_agreement_zero_when_llm_returns_nothing():
    """LLM 全静默（不可用）时，runner 里 κ 应等于规则归因器自身的 κ = 1.0。

    这不是 bug，是"模型不可用时系统退化为纯规则"承诺的统计证据。
    """
    llm = LLMTriage(MockLLMClient())  # 没有任何 replies，模型全静默
    result = run_evaluation(fault_seed=20240102, second_triage=llm)
    assert result.triage_agreement["kappa_root_cause"] == 1.0
    assert result.triage_agreement["source"] == "curation_eval"
