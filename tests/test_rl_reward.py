"""RL 奖励测试：满分口径、分项扣分、超轮惩罚、verl 签名分发、判分等价。"""

from __future__ import annotations

import pytest

from findata.dq.models import Diagnosis, RootCause, Severity
from findata.eval.agent_judge import judge_answer
from findata.rl.reward import (
    ASK_WEIGHTS,
    ATTRIBUTION_WEIGHTS,
    ask_reward,
    attribution_reward,
    compute_reward,
)


def _gt(**overrides) -> dict:
    gt = {"finding_key": "k", "expected_cause": "upstream_stale", "expected_suppressed": False}
    gt.update(overrides)
    return gt


def _dx(cause: RootCause, severity: Severity = Severity.P0, benign: bool = False) -> Diagnosis:
    return Diagnosis(
        finding_key="p|t|s|w",
        root_cause=cause,
        severity=Severity.OK if benign else severity,
        confidence=0.9,
        explanation="测试",
    )


class TestAttributionReward:
    def test_perfect_submission_is_full_score(self):
        r, bd = attribution_reward(
            _dx(RootCause.UPSTREAM_STALE),
            RootCause.UPSTREAM_STALE,
            expected_suppressed=False,
        )
        assert r == 1.0
        assert bd["root_cause"] == 1.0 and bd["suppress"] == 1.0 and bd["format"] == 1.0

    def test_wrong_cause_loses_main_weight(self):
        r, bd = attribution_reward(
            _dx(RootCause.MISSING_ROWS),
            RootCause.UPSTREAM_STALE,
            expected_suppressed=False,
        )
        w = ATTRIBUTION_WEIGHTS
        expected = (w.suppress + w.format) / w.max_total
        assert r == pytest.approx(expected)
        assert bd["root_cause"] == 0.0

    def test_wrong_suppression_decision(self):
        r, _ = attribution_reward(
            _dx(RootCause.UPSTREAM_STALE),
            RootCause.UPSTREAM_STALE,
            expected_suppressed=True,  # 标注说该抑制，提交却说故障
        )
        w = ATTRIBUTION_WEIGHTS
        expected = (w.root_cause + w.format) / w.max_total
        assert r == pytest.approx(expected)

    def test_timeout_submission_scores_zero_with_penalty(self):
        r, bd = attribution_reward(None, RootCause.UPSTREAM_STALE, False, extra_turns=100)
        assert r == 0.0  # floor：惩罚不产生负奖励
        assert bd["penalty"] == ATTRIBUTION_WEIGHTS.extra_turn_cap  # 惩罚封顶
        assert bd["submitted"] is False

    def test_benign_suppression_correct(self):
        r, bd = attribution_reward(
            _dx(RootCause.BENIGN_SUSPENSION, benign=True),
            RootCause.BENIGN_SUSPENSION,
            expected_suppressed=True,
        )
        assert r == 1.0
        assert bd["suppress"] == 1.0


class TestAskReward:
    _EXPECT = {
        "values": [121.0],
        "unit": "元",
        "verification_mark": True,
        "want_run_id": True,
        "tol": 0.01,
    }

    def test_perfect_answer(self):
        answer = "茅台最新收盘价 121.0 元 [run_id=abcd1234] ✓"
        r, bd = ask_reward(answer, self._EXPECT, run_ids=["abcd1234"], tool_rounds=1)
        assert r == 1.0
        assert bd["values"] == 1.0 and bd["run_id_kept"] == 1.0

    def test_wrong_number(self):
        r, bd = ask_reward("收盘价 99.0 元 run_id=abcd1234 ✓", self._EXPECT, run_ids=["abcd1234"])
        assert bd["values"] == 0.0
        assert r == pytest.approx((ASK_WEIGHTS.run_id + ASK_WEIGHTS.format) / ASK_WEIGHTS.max_total)

    def test_missing_run_id(self):
        r, bd = ask_reward("收盘价 121.0 元 ✓", self._EXPECT, run_ids=["abcd1234"])
        assert bd["run_id_kept"] == 0.0

    def test_tool_round_penalty_capped(self):
        answer = "收盘价 121.0 元 run_id=abcd1234 ✓"
        r_over, bd = ask_reward(answer, self._EXPECT, run_ids=["abcd1234"], tool_rounds=100)
        assert bd["penalty"] == ASK_WEIGHTS.extra_round_cap
        r_ok, _ = ask_reward(answer, self._EXPECT, run_ids=["abcd1234"], tool_rounds=2)
        assert r_ok == 1.0 > r_over  # 超轮该罚但不翻转成败


class TestComputeReward:
    """verl custom_reward_function 兼容签名。"""

    def test_attribution_dispatch(self):
        solution = (
            '{"root_cause": "upstream_stale", "severity": "P0", '
            '"confidence": 0.9, "explanation": "上游停更"}'
        )
        gt = {"finding_key": "k", "expected_cause": "upstream_stale", "expected_suppressed": False}
        assert compute_reward("attribution", solution, gt, {"extra_turns": 0}) == 1.0

    def test_attribution_unparsable_is_zero(self):
        gt = _gt()
        assert compute_reward("attribution", "我不知道", gt) == 0.0

    def test_attribution_tolerates_code_fence(self):
        solution = (
            '```json\n{"root_cause": "upstream_stale", '
            '"severity": "P0", "confidence": 0.9, "explanation": "停更"}\n```'
        )
        gt = _gt()
        assert compute_reward("attribution", solution, gt) == 1.0

    def test_ask_dispatch(self):
        gt = {"values": [121.0], "unit": "元", "want_run_id": True, "tol": 0.01}
        extra = {"run_ids": ["abcd1234"], "tool_rounds": 1}
        r = compute_reward("ask", "收盘价 121.0 元 run_id=abcd1234 ✓", gt, extra)
        assert r == 1.0

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError):
            compute_reward("mystery", "x", {})


class TestJudgeParity:
    """奖励与 M12 评测共用同一把尺子（agent_judge），行为必须等价。"""

    def test_judge_answer_agrees_with_reward_components(self):
        expect = {"values": [121.0], "unit": "元", "verification_mark": True, "want_run_id": True}
        good = "收盘价 121.0 元 [run_id=abcd1234] ✓"
        bad = "收盘价 99.0 元"
        ok_good, _ = judge_answer(good, expect, tol=0.01)
        ok_bad, reasons = judge_answer(bad, expect, tol=0.01)
        assert ok_good and not ok_bad
        assert len(reasons) >= 2  # 数值 + run_id + 标记
        _, bd_good = ask_reward(good, {**expect, "tol": 0.01}, run_ids=["abcd1234"])
        assert bd_good["values"] == 1.0
