"""LLM 归因器测试。

最重要的一条是最后一个用例：模型彻底答错时，评测指标必须与纯规则完全一致。
这条保证"接入 LLM"不会让系统变糟——否则这个改动根本不该上线。
"""

from __future__ import annotations

import json

import pytest

from findata.agent import LLMTriage, MockLLMClient, OpenAICompatClient, build_evidence
from findata.agent.llm import LLMError
from findata.dq.models import DIAGNOSABLE_CAUSES, RootCause, Severity
from findata.dq.protocol import Triage
from findata.dq.triage import BaselineTriage
from findata.eval.runner import run_evaluation
from findata.report import Snapshot


def _ctx_and_findings():
    from findata.dq.probes import run_probes

    ctx = Snapshot.from_synthetic(seed=20240102).to_probe_context()
    return ctx, run_probes(ctx)


def test_llm_triage_satisfies_protocol():
    assert isinstance(LLMTriage(MockLLMClient()), Triage)


def test_openai_client_requires_key():
    client = OpenAICompatClient(api_key="")
    assert not client.available
    with pytest.raises(LLMError):
        client.complete("sys", "user")


def test_valid_llm_output_is_adopted():
    ctx, findings = _ctx_and_findings()
    reply = json.dumps(
        {
            "results": [
                {
                    "index": i,
                    "root_cause": RootCause.UPSTREAM_STALE.value,
                    "severity": Severity.P0.value,
                    "confidence": 0.88,
                    "explanation": "模型判断为上游停更",
                }
                for i in range(len(findings))
            ]
        },
        ensure_ascii=False,
    )
    # 用 default 而非 replies：信号按批发送，每批都要拿到同样的回复
    triage = LLMTriage(MockLLMClient(default=reply))
    out = triage.diagnose_all(findings, ctx)
    assert triage.stats.from_llm == len(findings)
    assert all(d.root_cause is RootCause.UPSTREAM_STALE for d in out)
    assert all("llm" in d.evidence_refs for d in out)


def test_invalid_root_cause_is_rejected_and_falls_back():
    """模型编造了一个不存在的根因，必须拒收而不是照单全收。"""
    ctx, findings = _ctx_and_findings()
    # 只让 index 0 答错，其余合法：验证降级是逐条的，而不是整批放弃
    results = [
        {
            "index": i,
            "root_cause": (
                "definitely_not_a_real_cause" if i == 0 else RootCause.UPSTREAM_STALE.value
            ),
            "severity": "P0",
            "confidence": 0.9,
            "explanation": "幻觉" if i == 0 else "模型判断为上游停更",
        }
        for i in range(len(findings))
    ]
    reply = json.dumps({"results": results}, ensure_ascii=False)
    triage = LLMTriage(MockLLMClient(default=reply))
    out = triage.diagnose_all(findings, ctx)
    baseline = BaselineTriage().diagnose_all(findings, ctx)
    assert out[0] == baseline[0], "非法输出应退化为规则结论"
    assert triage.stats.rejected == 1
    assert triage.stats.from_llm == len(findings) - 1, "其余合法结论不应被连坐"
    assert out[1].root_cause is RootCause.UPSTREAM_STALE


def test_invalid_severity_is_rejected():
    ctx, findings = _ctx_and_findings()
    reply = json.dumps(
        {
            "results": [
                {
                    "index": 0,
                    "root_cause": RootCause.UPSTREAM_STALE.value,
                    "severity": "P99",
                    "confidence": 0.9,
                    "explanation": "级别瞎填",
                }
            ]
        }
    )
    triage = LLMTriage(MockLLMClient(replies=[reply]))
    out = triage.diagnose_all(findings, ctx)
    assert out[0] == BaselineTriage().diagnose(findings[0], ctx)


def test_garbage_reply_falls_back_entirely():
    ctx, findings = _ctx_and_findings()
    triage = LLMTriage(MockLLMClient(replies=["抱歉，我无法回答这个问题"]))
    out = triage.diagnose_all(findings, ctx)
    assert out == BaselineTriage().diagnose_all(findings, ctx)
    assert triage.stats.from_llm == 0


def test_client_error_never_propagates():
    """模型服务挂了，巡检必须照常出报告，而不是崩掉。"""
    ctx, findings = _ctx_and_findings()

    class Boom:
        def complete(self, system: str, user: str) -> str:
            raise LLMError("HTTP 429")

    triage = LLMTriage(Boom())  # type: ignore[arg-type]
    out = triage.diagnose_all(findings, ctx)
    assert out == BaselineTriage().diagnose_all(findings, ctx)
    assert triage.stats.errors


def test_code_fenced_json_is_parsed():
    ctx, findings = _ctx_and_findings()
    body = json.dumps(
        {
            "results": [
                {
                    "index": 0,
                    "root_cause": RootCause.MISSING_ROWS.value,
                    "severity": "P1",
                    "confidence": 0.7,
                    "explanation": "中间空洞",
                }
            ]
        }
    )
    triage = LLMTriage(MockLLMClient(replies=[f"```json\n{body}\n```"]))
    out = triage.diagnose_all(findings, ctx)
    assert out[0].root_cause is RootCause.MISSING_ROWS


def test_evidence_carries_decisive_facts():
    """证据必须包含区分故障与合法事件的关键事实，否则模型无从判断。"""
    ctx, findings = _ctx_and_findings()
    ev = build_evidence(findings[0], ctx)
    assert {"suspension_coverage", "failed_ingest_run", "list_date", "trading_days_in_window"} <= (
        ev.keys()
    )


def test_diagnosable_causes_excludes_unknown():
    assert RootCause.UNKNOWN.value not in DIAGNOSABLE_CAUSES
    assert RootCause.BENIGN_SUSPENSION.value in DIAGNOSABLE_CAUSES


@pytest.mark.parametrize("seed", [20240102, 20240103])
def test_broken_llm_never_worsens_metrics(seed):
    """核心承诺：模型完全不可用时，评测指标不得低于纯规则。"""
    baseline_report = run_evaluation(fault_seed=seed).report

    broken = LLMTriage(MockLLMClient(replies=["这不是 JSON"]))
    llm_report = run_evaluation(fault_seed=seed, triage=broken).report

    assert llm_report.fault_recall == baseline_report.fault_recall
    assert llm_report.root_cause_accuracy == baseline_report.root_cause_accuracy
    assert llm_report.benign_suppression == baseline_report.benign_suppression
    assert llm_report.alert_precision == baseline_report.alert_precision
