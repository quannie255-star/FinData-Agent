"""agent 级评测判分器测试：纯函数，不依赖 LLM 与仓库。"""

from __future__ import annotations

import pytest
from scripts.eval_agent import extract_numbers, judge_answer, judge_run_ids, render_text

_EXPECT = {
    "values": [121.0],
    "unit": "元",
    "verification_mark": True,
    "want_run_id": True,
}


def test_extract_numbers_tokenizes_not_substrings():
    # "121.0" 是一个 token，不得误中期望值 21
    assert extract_numbers("收盘价 121.0 元") == [121.0]
    assert extract_numbers("涨幅 21%，跌 3.5 元，-0.2") == [21.0, 3.5, -0.2]
    assert extract_numbers("无数字") == []


def test_judge_answer_pass():
    ok, reasons = judge_answer(
        "茅台最新收盘价 121.0 元 [run_id=abcd1234] ✓ 已交叉验证",
        _EXPECT,
        tol=0.01,
    )
    assert ok and reasons == []


def test_judge_answer_tolerates_formatting():
    ok, _ = judge_answer(
        "收盘价为 121 元（run_id=ff0099aa），✓",
        _EXPECT,
        tol=0.01,
    )
    assert ok


def test_judge_answer_missing_value():
    ok, reasons = judge_answer(
        "收盘价 120.0 元 [run_id=abcd1234] ✓",
        _EXPECT,
        tol=0.01,
    )
    assert not ok and any("121.0" in r for r in reasons)


def test_judge_answer_missing_run_id_and_mark():
    ok, reasons = judge_answer("收盘价 121.0 元", _EXPECT, tol=0.01)
    assert not ok
    assert any("run_id" in r for r in reasons)
    assert any("标记" in r for r in reasons)


def test_judge_answer_missing_unit():
    expect = {"values": [121.0], "unit": "元", "verification_mark": False, "want_run_id": False}
    ok, reasons = judge_answer("答案是 121.0", expect, tol=0.01)
    assert not ok and any("单位" in r for r in reasons)


def test_judge_run_ids_coverage():
    answer = "121.0 元 [run_id=aaaa] [run_id=bbbb]"
    assert judge_run_ids(answer, ["aaaa", "bbbb", "cccc"]) == pytest.approx(2 / 3)
    assert judge_run_ids("没有溯源", ["aaaa"]) == 0.0
    assert judge_run_ids("没有溯源", []) == 0.0


def test_render_text_contains_comparison_and_costs():
    results = [
        {
            "arch": "single",
            "rows": [{"name": "case1", "correct": True, "reasons": []}],
            "n": 1,
            "n_correct": 1,
            "run_id_kept": 1.0,
            "avg_tool_rounds": 1.0,
            "avg_latency_s": 2.0,
            "avg_total_tokens": 100.0,
            "sum_total_tokens": 100,
            "agent_cost": {"schema": 0, "verifier": 0, "finalize": 0},
            "route_ok": None,
            "query_ok": None,
            "trace_ok": 1.0,
        },
        {
            "arch": "multi",
            "rows": [
                {
                    "name": "case1",
                    "correct": False,
                    "reasons": ["答案未包含期望数值 121.0（tol=0.01）"],
                }
            ],
            "n": 1,
            "n_correct": 0,
            "run_id_kept": 0.5,
            "avg_tool_rounds": 2.0,
            "avg_latency_s": 4.0,
            "avg_total_tokens": 250.0,
            "sum_total_tokens": 250,
            "agent_cost": {"schema": 100.0, "verifier": 80.0, "finalize": 70.0},
            "route_ok": 1.0,
            "query_ok": 0.75,
            "trace_ok": 0.5,
        },
    ]
    text = render_text(results)
    assert "单 Agent vs 多智能体" in text
    assert "token 成本" in text
    assert "+150%" in text, "成本上升必须如实出现在对比表里"
    assert "失败明细（multi）" in text
    assert "分步漏斗" in text
    assert "75.0%" in text, "查询执行成功率必须出现在漏斗里"
    assert "n/a" in text, "单 Agent 没有查询尝试记录，漏斗该如实标 n/a"
    assert "成本归因" in text and "verifier 80" in text
