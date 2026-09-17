"""扩充语料（M13 难例靶子）单测：确定性、混淆对的「无缝隙」性质、判别证据。

核心断言是那条形式化性质：drift 故障的探针比值必须**落在**放量脉冲的
比值区间内部——这保证任何阈值规则都必然牺牲一边（要么漏掉故障，
要么放行脉冲）。这个性质坏了，语料就不再难，M13 的实验就失去靶子。
"""

import pytest

from findata.agent.llm_triage import build_evidence
from findata.dq.probes import _DRIFT_UP, ProbeContext, run_probes
from findata.dq.triage import BaselineTriage
from findata.eval.extended import SURGE_SYMBOLS, build_extended_corpus
from findata.eval.fixtures import build_baseline
from findata.eval.runner import run_evaluation


def _load():
    baseline = build_baseline()
    corpus = build_extended_corpus(baseline)
    ctx = ProbeContext.of(
        stock_daily=corpus.injected.stock_daily,
        valuation_daily=corpus.injected.valuation_daily,
        universe=baseline.universe,
        corporate_event=baseline.corporate_event,
        calendar=baseline.calendar,
    )
    return baseline, corpus, ctx


def test_corpus_is_deterministic():
    c1 = build_extended_corpus(build_baseline())
    c2 = build_extended_corpus(build_baseline())
    assert [str(f.symbol) for f in c1.surge_benigns] == [str(f.symbol) for f in c2.surge_benigns]
    assert c1.injected.stock_daily.equals(c2.injected.stock_daily)


def test_surge_symbols_are_clean_carriers():
    """脉冲载体必须无故障、无公司事件——否则标注会被污染。"""
    baseline, corpus, _ = _load()
    fault_syms = {f.symbol for f in corpus.injected.faults}
    event_syms = set(baseline.corporate_event["symbol"])
    for b in corpus.surge_benigns:
        assert b.symbol not in fault_syms
        assert b.symbol not in event_syms


def test_confusable_pair_no_threshold_gap():
    """形式化性质：故障比值落在脉冲比值区间内 → 阈值规则无缝隙可插。"""
    _, corpus, ctx = _load()
    findings = run_probes(ctx)
    surge_syms = {str(b.symbol) for b in corpus.surge_benigns}
    fault_syms = {f.symbol for f in corpus.injected.faults if f.kind == "drift"}

    surge_vals, fault_vals = [], []
    for f in findings:
        if f.probe == "drift" and f.table == "stock_daily":
            if str(f.symbol) in surge_syms:
                surge_vals.append(f.value)
                assert f.value > _DRIFT_UP  # 每只脉冲标的都触发探针
            elif f.symbol in fault_syms:
                fault_vals.append(f.value)

    assert len(surge_vals) == SURGE_SYMBOLS
    assert fault_vals
    for fv in fault_vals:
        assert min(surge_vals) <= fv <= max(surge_vals), (
            "drift 故障比值不再落在脉冲区间内：语料失去了「阈值无解」性质"
        )


def test_evidence_discriminates_surge_from_fault():
    """判别证据分离：量额一致性（~1 vs >3）与市场同步（peers 2 vs 0）。"""
    _, corpus, ctx = _load()
    findings = run_probes(ctx)
    surge_syms = {str(b.symbol) for b in corpus.surge_benigns}
    fault_syms = {f.symbol for f in corpus.injected.faults if f.kind == "drift"}

    for f in findings:
        if f.probe != "drift" or f.table != "stock_daily":
            continue
        ev = build_evidence(f, ctx)
        assert "volume_amount_ratio_shift" in ev and "same_window_peers" in ev
        if str(f.symbol) in surge_syms:
            assert ev["volume_amount_ratio_shift"] < 2.0  # 量额同涨 → 一致
            assert ev["same_window_peers"] >= SURGE_SYMBOLS - 1  # 共振
        elif f.symbol in fault_syms:
            assert ev["volume_amount_ratio_shift"] > 3.0  # 只改 volume → 跳变
            assert ev["same_window_peers"] == 0  # 单点事故


def test_rule_engine_on_extended_corpus_honest_numbers():
    """规则归因在扩充语料上的真实水平：召回/根因不丢，但压不住脉冲。"""
    result = run_evaluation(extended=True)
    r = result.report
    assert r.n_faults == 7
    assert r.fault_recall == pytest.approx(1.0)
    assert r.root_cause_accuracy == pytest.approx(1.0)
    # base 5 个合法事件 + 3 只放量脉冲；脉冲压不住是规则已知局限的如实测量
    assert r.n_benign == 8
    assert r.benign_suppression == pytest.approx(5 / 8)
    assert r.alert_precision == pytest.approx(1.0)


def test_base_corpus_untouched_by_default():
    """不开 extended 时行为与 M12 完全一致——CI 门禁不受扩充语料影响。"""
    result = run_evaluation()
    r = result.report
    assert r.n_faults == 7
    assert r.n_benign == 5
    assert r.benign_suppression == pytest.approx(1.0)
    assert not any(b.kind == "volume_surge" for b in result.benigns)


def test_rule_triage_still_labels_surge_as_drift():
    """规则引擎把脉冲判成故障形态（不抑制）——它的已知局限，一行未改。"""
    _, corpus, ctx = _load()
    findings = run_probes(ctx)
    surge_syms = {str(b.symbol) for b in corpus.surge_benigns}
    dx = BaselineTriage().diagnose_all(
        [f for f in findings if str(f.symbol) in surge_syms], ctx
    )
    assert all(d.root_cause.value == "distribution_drift" for d in dx)
    assert not any(d.suppressed for d in dx)
