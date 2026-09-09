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


def test_triage_agreement_flat_llm():
    """双归因器一致性 κ：LLM 全部判同一根因时，κ 必须被计算且可解释。"""
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
    assert agreement["n_compared"] > 0
    # 根因维度：一边类别多样、一边全同 → 必然可算出 κ（非 None）
    assert agreement["kappa_root_cause"] is not None


def test_triage_agreement_one_when_llm_returns_nothing():
    """LLM 全静默（不可用）时，runner 里 κ 应等于规则归因器自身的 κ = 1.0。

    这不是 bug，是"模型不可用时系统退化为纯规则"承诺的统计证据。
    """
    llm = LLMTriage(MockLLMClient())  # 没有任何 replies，模型全静默
    result = run_evaluation(fault_seed=20240102, second_triage=llm)
    assert result.triage_agreement["kappa_root_cause"] == 1.0
    assert result.llm_backend == "mock" or result.llm_backend == "baseline"


def test_cohen_kappa_textbook_cases():
    """κ 数学性质：教科书用例直接钉死，防止实现漂移。"""
    from findata.eval.runner import cohen_kappa

    # 完全一致 → 1.0
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    # 教科书 2x2：po=0.5, pe=0.5 → κ=0.0（与随机一致）
    assert cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    # 两边都只有一个类别（pe=1，无可修正）→ None
    assert cohen_kappa(["a", "a"], ["a", "a"]) is None
    # 长度不等 → None
    assert cohen_kappa(["a"], ["a", "b"]) is None
    # 空序列 → None
    assert cohen_kappa([], []) is None
