"""scoring 的数学与标准化规则——评分口径必须有独立于 API 的单测。"""

from __future__ import annotations

import pytest

from findata.finqa.scoring import (
    TOKEN_BUDGET,
    ScoreReport,
    final_score,
    is_correct,
    normalize_answer,
    token_score,
)


class TestNormalizeAnswer:
    def test_multi_dedup_and_sort(self):
        assert normalize_answer("dac", "multi") == "ACD"

    def test_multi_with_noise_and_spaces(self):
        assert normalize_answer("answer: a c d", "multi") == "ACD"

    def test_multi_dedup_repeated(self):
        assert normalize_answer("ABAB", "multi") == "AB"

    def test_mcq_takes_first_valid_letter(self):
        assert normalize_answer("BAD", "mcq") == "B"

    def test_tf_takes_first_letter(self):
        assert normalize_answer("AB", "tf") == "A"

    def test_no_valid_letter_returns_empty(self):
        assert normalize_answer("对", "tf") == ""
        assert normalize_answer("E", "mcq") == ""
        assert normalize_answer("", "multi") == ""


class TestIsCorrect:
    def test_multi_no_partial_credit(self):
        # 题面口径：多选漏选/错选/多选均计错，无部分分
        assert not is_correct("AB", "ABD", "multi")
        assert not is_correct("ABCD", "ABD", "multi")
        assert is_correct("ABD", "ABD", "multi")

    def test_both_sides_normalized(self):
        # 两侧同尺子：gold 松散写法也不该改变判分结果
        assert is_correct("acd", "ACD", "multi")

    def test_empty_prediction_is_wrong(self):
        assert not is_correct("", "A", "mcq")

    def test_mcq_and_tf(self):
        assert is_correct("A", "A", "mcq")
        assert is_correct("B", "B", "tf")
        assert not is_correct("A", "B", "tf")


class TestTokenScore:
    def test_zero_tokens_full_score(self):
        assert token_score(0) == 1.0

    def test_at_budget_is_zero(self):
        assert token_score(TOKEN_BUDGET) == 0.0

    def test_over_budget_clamps_to_zero(self):
        # 官方 baseline 6,875,928 token 超预算 → 0
        assert token_score(6_875_928) == 0.0

    def test_half_budget(self):
        assert token_score(2_500_000) == pytest.approx(0.5)

    def test_bad_budget_raises(self):
        with pytest.raises(ValueError):
            token_score(100, budget=0)


class TestFinalScore:
    def test_official_baseline_shape(self):
        # 准确率 49%、token 超预算：100 × 0.49 × 0.7 = 34.3
        assert final_score(0.49, 0.0) == pytest.approx(34.3)

    def test_accuracy_dominant(self):
        assert final_score(0.8, 0.5) == pytest.approx(68.0)
        assert final_score(0.8, 0.0) == pytest.approx(56.0)

    def test_invalid_ranges_raise(self):
        with pytest.raises(ValueError):
            final_score(1.5, 0.0)
        with pytest.raises(ValueError):
            final_score(0.5, -0.1)


class TestScoreReport:
    def test_accumulates_and_domains(self):
        report = ScoreReport()
        report.add("q1", "regulatory", True, 100, 10)
        report.add("q2", "insurance", False, 200, 20)
        assert report.total == 2
        assert report.correct == 1
        assert report.accuracy == 0.5
        assert report.total_tokens == 330
        assert report.per_domain["regulatory"]["correct"] == 1
        assert report.per_domain["insurance"]["total"] == 1

    def test_summary_lines_mention_final_score(self):
        report = ScoreReport()
        report.add("q1", "regulatory", True, 1000, 100)
        joined = "\n".join(report.summary_lines())
        assert "FinalScore" in joined
        assert "1,100" in joined
