"""agentops 测试：轨迹落盘、流式读写、以及归因的**严重度判定**。

重点测严重度，因为它最容易判反，而判反会让结论完全相反：
把"安全地拒绝"误报成"危险地答错"，或反过来。归因器自己出错比不归因更糟。
"""

from __future__ import annotations

import json

from findata.agentops.probes import probe_retry_storm, probe_ungrounded_slot
from findata.agentops.schema import (
    STATUS_DEGRADED,
    STATUS_ERROR,
    Step,
    Trace,
    iter_traces,
    write_traces,
)
from findata.agentops.triage import (
    CAT_ABORT_AS_OF,
    CAT_DEGRADE_MISSING,
    CAT_DEGRADE_MONTH,
    CAT_ENV_TIMEOUT,
    CAT_HALLUCINATED,
    CAT_LUCKY_GUESS,
    CAT_OK,
    CAT_PARAM_ERROR,
    CAT_SCHEMA_CONTRADICTION,
    CAT_TOOL_MISSING,
    SEV_LOUD,
    SEV_OK,
    SEV_SILENT,
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    Diagnosis,
    _has_day,
    diagnose,
    verdict,
)


def _trace(task: str = "问句", **outcome) -> Trace:
    t = Trace(task=task)
    t.step(
        "parse_query",
        arguments={"question": task},
        result={"symbol": "600519", "intent": "price", "as_of": None, "start": None, "end": None},
    )
    t.finish(**outcome)
    return t


# ── schema ──


def test_write_and_iter_roundtrip(tmp_path):
    p = tmp_path / "t.jsonl"
    traces = [_trace("问题 A"), _trace("问题 B")]
    assert write_traces(p, iter(traces)) == 2
    back = list(iter_traces(p))
    assert len(back) == 2
    assert [t.task for t in back] == ["问题 A", "问题 B"]
    assert [t.run_id for t in back] == [t.run_id for t in traces]
    assert len(back[0].steps) == 1


def test_empty_fields_are_dropped():
    s = Step(seq=1, name="build_sql", arguments={"a": 1})
    d = s.to_dict()
    assert d["type"] == "tool_call"
    # 空的 result / error / latency 不该出现在落盘数据里，否则轨迹全是噪音
    assert "result" not in d
    assert "error" not in d


def test_degraded_step_is_counted():
    t = Trace(task="x")
    t.step("parse_query", status=STATUS_DEGRADED, error="区间缺 start/end")
    assert t.n_degraded == 1
    assert len(t.failed_steps) == 1


# ── 严重度判定 ──


def test_abort_is_loud_even_when_parse_mismatches():
    """一条数字都没给出来 = loud，不能因为槽位不符就报成静默错。"""
    t = _trace(error="aborted:slot_incomplete:as_of", parse_matches_golden=False)
    d = diagnose(t)
    assert d.category == CAT_ABORT_AS_OF
    assert d.severity == SEV_LOUD


def test_hallucinated_date_is_silent():
    """该拒绝的题编了个日期还照常出数——槽位完整，校验查不出，最危险。"""
    t = _trace(should_refuse=True, refused=False, value=1500.0)
    d = diagnose(t)
    assert d.category == CAT_HALLUCINATED
    assert d.severity == SEV_SILENT


def test_refused_correctly_is_not_a_failure():
    t = _trace(should_refuse=True, refused=True)
    assert diagnose(t).severity == SEV_OK


def test_parse_mismatch_without_abort_is_silent():
    t = _trace(parse_matches_golden=False, value=1.0, usable=True, badge="BASELINE")
    d = diagnose(t)
    assert d.severity == SEV_SILENT


# ── 降级归因的两种根因 ──


def test_month_only_question_is_month_expansion():
    t = Trace(task="中国神华 2025 年 8 月的日均成交量是多少？")
    t.step("parse_query", status=STATUS_DEGRADED, error="区间缺 start/end")
    t.finish()
    assert diagnose(t).category == CAT_DEGRADE_MONTH


def test_explicit_day_question_is_missing_slots():
    """问句给了完整日期却填不出槽位，那是漏填，不是"不会展开整月"。"""
    t = Trace(task="中国神华 2025 年 8 月 1 日到 8 月 22 日的涨跌幅是多少？")
    t.step("parse_query", status=STATUS_DEGRADED, error="区间缺 start/end")
    t.finish()
    assert diagnose(t).category == CAT_DEGRADE_MISSING


def test_has_day_ignores_the_ri_in_ri_jun():
    """「日均成交量」里有个「日」字，不能当成"问句给了具体某一天"。"""
    assert not _has_day("中国神华 2025 年 8 月的日均成交量是多少？")
    assert _has_day("中国神华 2025 年 8 月 1 日到 8 月 22 日涨了多少？")
    assert _has_day("中芯国际 2025 年 9 月 4 号收盘多少？")


def test_trace_json_is_one_line(tmp_path):
    """JSONL 的前提：一条一行，不能有换行把格式撑坏。"""
    p = tmp_path / "t.jsonl"
    write_traces(p, [_trace("问题 A")])
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    json.loads(lines[0])  # 每行必须是独立可解析的 JSON


# ── 探针（只观测不判断，重点测误报防线）──


def _parse_step(t: Trace, **result) -> Trace:
    t.step("parse_query", arguments={"question": t.task}, result=result)
    t.finish()
    return t


def test_symbol_is_not_grounding_checked():
    """问「中芯国际」却要求问句里出现 688981 是荒谬的——映射本身就是出处。"""
    t = _parse_step(Trace(task="中芯国际 2025 年 9 月 4 日的收盘价是多少？"), symbol="688981")
    assert probe_ungrounded_slot(t)["n"] == 0


def test_month_expansion_is_not_hallucination():
    """「8 月」推导出 08-01~08-31 是项目约定（合法推导），不是模型编的。"""
    t = _parse_step(
        Trace(task="中国神华 2025 年 8 月的日均成交量是多少？"),
        start="2025-08-01",
        end="2025-08-31",
    )
    assert probe_ungrounded_slot(t)["n"] == 0


def test_chinese_numeral_question_is_unverifiable_not_hallucination():
    """一个阿拉伯数字都没有 → 无从核对，标 unverifiable，不许指控。"""
    t = _parse_step(Trace(task="二〇二五年九月四日中芯国际的收盘价是多少？"), as_of="2025-09-04")
    p = probe_ungrounded_slot(t)
    assert p["unverifiable"] is True
    assert p["n"] == 0


def test_hallucinated_date_is_caught_without_golden():
    """不靠任何标注：问句只给了月份，模型补了个「4 日」。"""
    t = _parse_step(Trace(task="贵州茅台 2025 年 9 月的收盘价是多少？"), as_of="2025-09-04")
    p = probe_ungrounded_slot(t)
    assert p["n"] == 1
    assert p["suspicious"][0]["slot"] == "as_of"


def test_same_tool_different_args_is_not_a_retry_loop():
    """同一个工具带不同参数并行调用是常态，按名字计数会误判成死循环。

    xlam 上第一版就误报了 53 条（查 beta / 查 loot / 查 game 各一次被当成
    "绕了三圈"）。判据必须是"同名**且同参数**"。
    """
    t = Trace(task="x")
    t.step("live_giveaways_by_type", arguments={"type": "beta"})
    t.step("live_giveaways_by_type", arguments={"type": "loot"})
    t.step("live_giveaways_by_type", arguments={"type": "game"})
    assert probe_retry_storm(t)["storm"] == {}
    # 参数完全一样的原样重试才算——它没从上次结果里学到任何东西
    t.step("live_giveaways_by_type", arguments={"type": "game"})
    assert probe_retry_storm(t)["storm"] == {"live_giveaways_by_type": 2}


def test_schema_contradiction_is_not_blamed_on_the_agent():
    """同样是"必填参数没传"：description 承诺过默认值时，错的是数据源。

    这一条必须排在硬错误分类之前，否则会被笼统归成"参数写错"，
    给出的改进建议（换模型/收紧输出格式）就是朝反方向使劲。
    """
    t = Trace(task="electric field of 2C at 3m")
    t.step(
        "calculate_electric_field",
        arguments={"charge": 2, "distance": 3},
        status=STATUS_ERROR,
        error="schema contradiction: permitivity 的 description 称有默认值，schema 未标 default",
    )
    t.finish()
    d = diagnose(t)
    assert d.category == CAT_SCHEMA_CONTRADICTION
    assert "数据源" in d.suggestion
    # 丢弃是处置（样本不能喂），不等于归责 agent
    assert verdict(d) == V_DISCARD


def test_retry_storm_threshold():
    t = Trace(task="x")
    for _ in range(3):
        t.step("execute_sql")
    assert probe_retry_storm(t)["storm"] == {"execute_sql": 3}
    assert probe_retry_storm(Trace(task="y"))["max_repeats"] == 0




def test_verdict_separates_env_failure_from_ability_failure():
    """同样是失败：环境问题别当负样本，能力问题才丢。"""
    assert verdict(Diagnosis("x", CAT_ENV_TIMEOUT, SEV_LOUD)) == V_NOT_FAILURE
    assert verdict(Diagnosis("x", CAT_TOOL_MISSING, SEV_LOUD)) == V_NOT_FAILURE
    assert verdict(Diagnosis("x", CAT_PARAM_ERROR, SEV_LOUD)) == V_DISCARD
    assert verdict(Diagnosis("x", CAT_LUCKY_GUESS, SEV_LOUD)) == V_REVIEW
    assert verdict(Diagnosis("x", CAT_OK, SEV_OK)) == V_POSITIVE


def test_error_classification_survives_punctuation_and_camelcase():
    """报错不会照着关键词写：折叠空白与标点后才能同时覆盖两种写法。"""
    for msg, cat in (
        ("ToolNotFoundError: x", CAT_TOOL_MISSING),
        ("tool not found: x", CAT_TOOL_MISSING),
        ("no such tool: x", CAT_TOOL_MISSING),
        ("timed out after 30s", CAT_ENV_TIMEOUT),
        ("sandbox timeout after 30s", CAT_ENV_TIMEOUT),
        ("argument out of range: month must be 1-12", CAT_PARAM_ERROR),
    ):
        t = Trace(task="中芯国际 2025 年 9 月 4 日的收盘价是多少？")
        t.step("execute_sql", status="error", error=msg)
        t.finish()
        assert diagnose(t).category == cat, msg
