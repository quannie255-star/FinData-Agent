"""进化 harness（M13）单测：标注对齐、Pareto 接受、门禁、内置引擎全流程。

引擎全流程用「真相感知」假模型驱动：它能解析批内信号、按语料真相作答。
携带 MARKER 的 prompt 得到全对回答，不携带的得到固定错误回答——
于是「进化是否有概率被接受」「门禁是否拦住回归」都可以确定性断言。
"""

import json

import pytest

from findata.agent.llm import LLMError
from findata.eval.evolution import (
    Budget,
    Candidate,
    CountingClient,
    assemble_system,
    build_labeled_items,
    evolve_builtin,
    gate_report,
    pareto_accepts,
    parse_proposal,
    split_system,
    split_train_held,
    train_accuracy,
)
from findata.eval.metrics import EvalReport, _matches
from findata.eval.runner import run_evaluation

_MARKER = "MARKER_V2_判别要领"

# 扩充语料（canonical seed）是确定性的：一次构建，真相表全程复用
_RESULT = run_evaluation(extended=True)


def _truth_table() -> dict[tuple, tuple[str, str]]:
    """(probe, table, symbol, window) -> (root_cause, severity)。"""
    table: dict[tuple, tuple[str, str]] = {}
    for fault in _RESULT.faults:
        for f in _RESULT.findings:
            if _matches(f, fault):
                table[(f.probe, f.table, f.symbol, f.window)] = (
                    fault.expected_root_cause.value,
                    fault.expected_severity.value,
                )
    for b in _RESULT.benigns:
        for f in _RESULT.findings:
            if (f.probe, f.table, f.symbol, f.window) in table:
                continue
            if _matches(f, b):
                table[(f.probe, f.table, f.symbol, f.window)] = (
                    b.expected_root_cause.value,
                    b.expected_severity.value,
                )
    return table


_TRUTH = _truth_table()


class TruthAwareFake:
    """假 LLM：system 带 MARKER → 按真相作答（未标注信号答合法抑制）；
    不带 MARKER → 全部答 upstream_stale。对反射提议的调用
    （system 含「提示词优化专家」）返回带 MARKER 的提案。"""

    def __init__(self, marker: str = _MARKER):
        self.marker = marker
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if "提示词优化专家" in system:
            return json.dumps({"head": f"改进版要领。{_MARKER}", "rationale": "test 改写"})
        start, end = user.find("{"), user.rfind("}")
        doc = json.loads(user[start : end + 1])
        results = []
        for item in doc["findings"]:
            key = (item["probe"], item["table"], item["symbol"], item["window"])
            if _MARKER in system:
                cause, sev = _TRUTH.get(key, ("benign_suspension", "OK"))
            else:
                cause, sev = "upstream_stale", "P0"
            results.append(
                {"index": item["index"], "root_cause": cause, "severity": sev,
                 "confidence": 0.9, "explanation": "fake"}
            )
        return json.dumps({"results": results})


# ---- 标注对齐与切分 ----


def test_build_labeled_items_counts():
    items = build_labeled_items(_RESULT)
    assert len(_RESULT.faults) == 7
    assert len(_RESULT.benigns) == 8  # base 5 + 放量脉冲 3
    assert len(items) >= 12  # 每个标注至少对一个信号


def test_split_train_held_only_late_surge_symbols():
    items = build_labeled_items(_RESULT)
    train, held = split_train_held(items, _RESULT)
    surge_syms = sorted({b.symbol for b in _RESULT.benigns if b.kind == "volume_surge"})
    held_syms = {it.finding.symbol for it in held}
    assert held_syms == set(surge_syms[1:])
    assert surge_syms[0] in {it.finding.symbol for it in train}  # 训练集见过脉冲形态


def test_train_accuracy_counts_exact_matches():
    items = build_labeled_items(_RESULT)
    dx = {d.finding_key: d for d in _RESULT.llm_diagnoses}
    assert dx == {}  # 没跑 LLM 归因时 accuracy 为 0
    assert train_accuracy(items, dx) == 0.0


def test_train_accuracy_rejected_fallback_not_credited():
    """被拒收降级的条目不得记为模型的成功——否则高拒收率反被奖励。"""
    from findata.dq.models import Diagnosis, Severity

    items = build_labeled_items(_RESULT)
    it = items[0]
    llm_answer = Diagnosis(
        finding_key=it.finding.key, root_cause=it.expected_cause,
        severity=Severity.P1, confidence=0.9, explanation="x", evidence_refs=("llm",),
    )
    fallback_answer = Diagnosis(
        finding_key=it.finding.key, root_cause=it.expected_cause,
        severity=Severity.P1, confidence=0.9, explanation="x", evidence_refs=(),
    )
    dx = {it.finding.key: llm_answer}
    assert train_accuracy([it], dx) == 1.0  # 模型亲判且正确 → 记分
    dx = {it.finding.key: fallback_answer}
    assert train_accuracy([it], dx) == 0.0  # 规则兜底答对 → 不记模型分


# ---- Pareto 接受与门禁 ----


def _report(rc: float, sup: float, prec: float) -> EvalReport:
    return EvalReport(root_cause_accuracy=rc, benign_suppression=sup, alert_precision=prec)


def test_pareto_accepts():
    inc = _report(0.8, 0.6, 1.0)
    assert pareto_accepts(_report(0.9, 0.7, 1.0), inc)  # 全面不差且严格更好
    assert not pareto_accepts(_report(0.8, 0.6, 1.0), inc)  # 无严格改进
    assert not pareto_accepts(_report(0.7, 0.7, 1.0), inc)  # 一项退化即拒


def test_gate_report_detects_regression():
    inc = type("R", (), {})()
    inc.extended = _report(1.0, 0.6, 1.0)
    inc.base = _report(0.857, 1.0, 1.0)
    ok = type("R", (), {})()
    ok.extended = _report(1.0, 0.6, 1.0)
    ok.base = _report(0.857, 1.0, 1.0)
    bad = type("R", (), {})()
    bad.extended = _report(1.0, 0.6, 1.0)
    bad.base = _report(0.7, 1.0, 1.0)
    passed, failures = gate_report(ok, inc)
    assert passed and failures == []
    passed, failures = gate_report(bad, inc)
    assert not passed and any("base" in f and "根因" in f for f in failures)


# ---- 提议解析与模板切分 ----


def test_parse_proposal_tolerates_fence():
    raw = "```json\n{\"head\": \"H\", \"rationale\": \"R\"}\n```"
    assert parse_proposal(raw) == ("H", "R")
    assert parse_proposal("完全不是 JSON") is None


def test_split_and_assemble_system_roundtrip():
    system = _default_system()
    head, tail = split_system(system)
    assert tail.startswith("可选根因")
    assert assemble_system(head, tail) == system


def _default_system() -> str:
    from findata.agent import llm_triage

    return llm_triage._SYSTEM


# ---- 内置引擎全流程（真相感知假模型） ----


def test_evolve_builtin_accepts_truth_aware_candidate():
    """现任全错 → 带 MARKER 的候选全对：应被提议、胜出、Pareto 接受。"""
    from findata.agent import llm_triage as lt

    budget = Budget(max_calls=200)
    client = CountingClient(TruthAwareFake(), budget)
    propose_client = CountingClient(TruthAwareFake(), budget)
    head, tail = split_system(lt._SYSTEM)
    trace = evolve_builtin(
        client,
        propose_client,
        head,
        tail,
        lt._USER,
        fault_seed=20240102,
        rounds=1,
        k_per_round=1,
    )
    # 现任不带 MARKER → 训练集全错；候选带 MARKER → 全对，应被接受
    assert trace.accepted_head, trace.rounds
    assert _MARKER in trace.accepted_head
    assert trace.baseline_reports is not None
    assert trace.accepted_reports.extended.root_cause_accuracy == pytest.approx(1.0)
    # 门禁：进化产物相对现任全量不回归
    passed, failures = gate_report(trace.accepted_reports, trace.baseline_reports)
    assert passed, failures
    assert budget.calls == trace.llm_calls


def test_budget_exhaustion_stops_evolution():
    budget = Budget(max_calls=3)  # 基线评测就用完
    client = CountingClient(TruthAwareFake(), budget)
    from findata.agent import llm_triage as lt

    head, tail = split_system(lt._SYSTEM)
    trace = evolve_builtin(
        client,
        CountingClient(TruthAwareFake(), budget),
        head,
        tail,
        lt._USER,
        fault_seed=20240102,
        rounds=2,
        k_per_round=2,
    )
    assert trace.accepted_head == ""
    assert trace.llm_calls <= 3


class _Echo:
    """只会应答的桩：给预算计数测试用，不参与 JSON 解析。"""

    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str) -> str:
        return "ok"


def test_counting_client_charges_shared_budget():
    budget = Budget(max_calls=2)
    c1 = CountingClient(_Echo(), budget)
    c2 = CountingClient(_Echo(), budget)
    c1.complete("s", "u")
    c2.complete("s", "u")
    assert budget.calls == 2
    with pytest.raises(LLMError):
        c2.complete("s", "u")


def test_candidate_dataclass_defaults():
    c = Candidate(head="h")
    assert c.train_score is None and c.rationale == ""
