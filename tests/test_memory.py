"""经验记忆（M13）单测：两阶段写入、召回预算、污染防护、冲突消解。

验收门禁（ROADMAP M13）明确要求：记忆机制有单测 + 记忆污染防护用例
（低置信记忆不得改变抑制决策）。本文件里带「污染」字样的用例就是那条门禁。
"""

import json
from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from findata.agent.llm import MockLLMClient
from findata.agent.llm_triage import LLMTriage
from findata.core.db import SCHEMA_SQL
from findata.dq.memory import (
    MIN_CONFIDENCE,
    TriageMemory,
    fingerprint,
    value_bucket,
)
from findata.dq.models import Diagnosis, Finding, RootCause, Severity


def _finding(**kw) -> Finding:
    base = dict(
        probe="drift",
        table="stock_daily",
        symbol="600519",
        window="2024-06-01..2024-06-10",
        metric="volume_recent_over_history",
        value=5.3,
        threshold=5.0,
        severity_hint=Severity.P1,
    )
    base.update(kw)
    return Finding(**base)


def _diagnosis(cause: RootCause, confidence: float = 0.9, **kw) -> Diagnosis:
    return Diagnosis(
        finding_key=kw.pop("finding_key", "k"),
        root_cause=cause,
        severity=Severity.OK if cause.is_benign else Severity.P1,
        confidence=confidence,
        explanation=kw.pop("explanation", "测试结论"),
    )


def _fake_ctx(asof: date = date(2024, 6, 30)):
    """最小 ProbeContext 替身：fingerprint 只用 finding 字段 + asof。"""

    class C:
        pass

    c = C()
    c.asof = asof
    c.calendar = type("K", (), {"trading_days": staticmethod(lambda a, b: [])})()
    empty = pd.DataFrame()
    c.stock_daily = empty
    c.valuation_daily = empty
    c.universe = empty
    c.corporate_event = empty
    c.ingest_run = empty
    return c


@pytest.fixture()
def mem():
    conn = duckdb.connect()
    conn.execute(SCHEMA_SQL)
    yield TriageMemory(conn)
    conn.close()


# ---- 指纹 ----


def test_fingerprint_generalizes_across_symbol_and_date():
    """同形态不同标的/日期必须撞在同一个指纹上，先例才有泛化价值。"""
    a = fingerprint(_finding(symbol="600519", window="2024-06-01..2024-06-10"), date(2024, 6, 30))
    b = fingerprint(_finding(symbol="000001", window="2024-03-01..2024-03-10"), date(2024, 6, 30))
    assert a == b


def test_fingerprint_separates_value_magnitude_and_position():
    inner = fingerprint(_finding(window="2024-06-01..2024-06-10"), date(2024, 6, 30))
    tail = fingerprint(_finding(window="2024-06-28..2024-06-30"), date(2024, 6, 30))
    assert inner != tail
    assert value_bucket(5.2) == value_bucket(5.4)  # 混淆对的比值必须同桶
    assert value_bucket(5.2) != value_bucket(1.2)


def test_fingerprint_extras_distinguish_confusable_pair():
    """量额一致性标签进指纹：drift 故障与合法放量不 conflated。"""
    from findata.agent.llm_triage import _memory_extras

    fault_ev = {"volume_amount_ratio_shift": 4.6, "same_window_peers": 0}
    surge_ev = {"volume_amount_ratio_shift": 1.0, "same_window_peers": 2}
    fault = fingerprint(_finding(), date(2024, 6, 30), _memory_extras(fault_ev))
    surge = fingerprint(_finding(), date(2024, 6, 30), _memory_extras(surge_ev))
    assert fault != surge


# ---- 两阶段写入 ----


def test_first_observe_is_pending_and_not_recalled(mem):
    asof = date(2024, 6, 30)
    status = mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof, lesson="l1")
    assert status == "pending"
    assert mem.recall("fp1", asof).same == []


def test_recurrence_confirms_and_enables_recall(mem):
    asof = date(2024, 6, 30)
    mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof - timedelta(days=7))
    status = mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof, lesson="l2")
    assert status == "confirmed"
    r = mem.recall("fp1", asof)
    assert len(r.same) == 1
    assert r.same[0]["hits"] == 2
    assert "l2" in r.render()


def test_observe_is_idempotent_no_duplicate_rows(mem):
    asof = date(2024, 6, 30)
    for _ in range(3):
        mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof)
    assert len(mem.all_entries()) == 1


# ---- 污染防护 ----


def test_low_confidence_memory_never_recalled(mem):
    """低置信记忆即使被（异常路径）确认，也不得参与召回。"""
    asof = date(2024, 6, 30)
    mem.observe("fp1", _diagnosis(RootCause.BENIGN_SUSPENSION, confidence=0.3), asof)
    mem.conn.execute("UPDATE triage_memory SET status = 'confirmed'")
    assert mem.recall("fp1", asof).same == []


def test_low_confidence_memory_cannot_change_decisions(mem):
    """验收门禁用例：毒化记忆存在时，LLMTriage 的输出与无记忆完全一致。"""
    ctx = _fake_ctx()
    f = _finding()
    reply = json.dumps(
        {"results": [{"index": 0, "root_cause": "distribution_drift", "severity": "P1",
                      "confidence": 0.9, "explanation": "量级突变"}]}
    )
    # 无记忆基线
    clean = LLMTriage(client=MockLLMClient(default=reply))
    base_dx = clean.diagnose_all([f], ctx)
    base_calls = list(clean.client.calls)

    # 毒化记忆：低置信 + 强行确认，声称这个形态是合法的
    mem.observe(
        fingerprint(f, ctx.asof),
        _diagnosis(RootCause.BENIGN_CORPORATE_ACTION, confidence=0.3, explanation="毒化"),
        ctx.asof - timedelta(days=1),
        lesson="毒化：此形态其实是合法的",
    )
    mem.conn.execute("UPDATE triage_memory SET status = 'confirmed'")
    poisoned = LLMTriage(client=MockLLMClient(default=reply), memory=mem)
    poison_dx = poisoned.diagnose_all([f], ctx)

    assert base_dx[0].suppressed == poison_dx[0].suppressed
    assert base_dx[0].root_cause == poison_dx[0].root_cause
    # prompt 也必须逐字节一致：低置信记忆根本没进上下文
    assert poisoned.client.calls[0][1] == base_calls[0][1]


def test_pending_memory_not_injected_into_prompt(mem):
    ctx = _fake_ctx()
    f = _finding()
    reply = json.dumps(
        {"results": [{"index": 0, "root_cause": "distribution_drift", "severity": "P1",
                      "confidence": 0.9, "explanation": "x"}]}
    )
    mem.observe(fingerprint(f, ctx.asof), _diagnosis(RootCause.DISTRIBUTION_DRIFT), ctx.asof)
    t = LLMTriage(client=MockLLMClient(default=reply), memory=mem)
    t.diagnose_all([f], ctx)
    assert "历史先例" not in t.client.calls[0][1]


# ---- 冲突消解与时间点 ----


def test_conflicting_label_halves_weight_until_excluded(mem):
    asof = date(2024, 6, 30)
    early = asof - timedelta(days=14)
    middle = asof - timedelta(days=7)
    # 先确认一条 benign 经验
    mem.observe("fp1", _diagnosis(RootCause.BENIGN_SUSPENSION, confidence=0.9), early)
    mem.observe("fp1", _diagnosis(RootCause.BENIGN_SUSPENSION, confidence=0.9), middle)
    assert mem.recall("fp1", asof).same  # 已可召回

    # 之后同指纹被判为故障：对立记忆每次权重减半
    mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof)
    benign_rows = [r for r in mem.all_entries() if r["label"] == "benign"]
    assert benign_rows[0]["weight"] == pytest.approx(0.5, abs=1e-9)

    mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof)
    benign_rows = [r for r in mem.all_entries() if r["label"] == "benign"]
    assert benign_rows[0]["weight"] < 0.5
    # 跌破阈值的 benign 经验不再召回（新 fault 条目两次观察已确认，可召回）
    recalled = {r["root_cause"] for r in mem.recall("fp1", asof).same}
    assert "benign_suspension" not in recalled


def test_recall_is_point_in_time(mem):
    """回放历史时不得引用"事后才确认"的根因（防时间泄漏）。"""
    t1, t2 = date(2024, 6, 1), date(2024, 6, 30)
    mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), t2 - timedelta(days=7))
    mem.observe("fp1", _diagnosis(RootCause.DISTRIBUTION_DRIFT), t2)
    assert mem.recall("fp1", t1).same == []  # t1 时这些记忆还不存在
    assert mem.recall("fp1", t2).same


# ---- 预算与轮转 ----


def test_recall_budget(mem):
    asof = date(2024, 6, 30)
    for i in range(8):
        fp = f"fp{i}"
        mem.observe(fp, _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof - timedelta(days=2))
        mem.observe(fp, _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof)
    r = mem.recall("fp0", asof)
    assert len(r.same) <= 5  # 同指纹预算
    assert len(r.cross) <= 3  # 跨指纹一句话教训预算


def test_prune_rotates(mem):
    asof = date(2024, 6, 30)
    for i in range(60):
        mem.observe(f"p{i}", _diagnosis(RootCause.DISTRIBUTION_DRIFT), asof)
    removed = mem.prune()
    assert removed > 0
    assert len(mem.all_entries()) <= 50


# ---- 门禁基线常量 ----


def test_min_confidence_is_sane():
    assert 0.0 < MIN_CONFIDENCE < 1.0
