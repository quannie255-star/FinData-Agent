"""fixture 加载校验 + baseline 的纯解析部分（不碰网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from findata.finqa import fixture as fx
from findata.finqa.baseline import build_messages, extract_answer
from findata.finqa.fixture import Question

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


def _sample_question(**overrides) -> dict:
    base = {
        "qid": "tq_001",
        "domain": "regulatory",
        "split": "A",
        "question": "测试题干",
        "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
        "answer_format": "mcq",
        "type": "测试",
        "doc_ids": ["doc_a"],
        "gold": "B",
    }
    base.update(overrides)
    return base


class TestQuestionValidation:
    def test_ok(self):
        q = Question.model_validate(_sample_question())
        assert q.format_label() == "单选题"

    def test_bad_answer_format_rejected(self):
        with pytest.raises(ValidationError):
            Question.model_validate(_sample_question(answer_format="essay"))

    def test_missing_options_rejected(self):
        opts = {"A": "甲", "B": "乙"}
        with pytest.raises(ValidationError):
            Question.model_validate(_sample_question(options=opts))

    def test_loose_gold_rejected(self):
        # gold 必须是标准化形态，"acd" 这种松散写法在加载时拦下
        with pytest.raises(ValidationError):
            Question.model_validate(
                _sample_question(answer_format="multi", gold="acd")
            )

    def test_multi_single_letter_gold_rejected(self):
        with pytest.raises(ValidationError):
            Question.model_validate(_sample_question(answer_format="multi", gold="A"))

    def test_empty_doc_ids_allowed_for_b_split(self):
        # B 榜题目官方不给 doc_ids，模型层必须放行；A 榜约束在 baseline 层
        q = Question.model_validate(_sample_question(doc_ids=[]))
        assert q.doc_ids == []


class TestRealFixture:
    def test_loads_and_cross_checks(self):
        questions = fx.load_questions(FIXTURE_ROOT)
        docs = fx.load_docs(FIXTURE_ROOT)
        fx.check_docs(questions, docs)
        assert len(questions) >= 5
        assert set(docs) >= {"strict_csrc_035", "strict_csrc_023"}
        # 官方样例原题必须在，且标准答案与题面一致
        q1 = next(q for q in questions if q.qid == "reg_s_001")
        assert q1.gold == "AC"
        # evidence 引用的文档必须真实存在
        for q in questions:
            for ev in q.evidence:
                assert ev.doc_id in docs

    def test_docs_have_title_header(self):
        docs = fx.load_docs(FIXTURE_ROOT)
        for doc_id, text in docs.items():
            assert text.startswith("# "), doc_id
            assert fx.doc_title(text)

    def test_doc_missing_header_raises(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "bad.md").write_text("没有标题头", encoding="utf-8")
        with pytest.raises(ValueError, match="首行"):
            fx.load_docs(tmp_path)

    def test_max_chars_truncation_is_visible(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "d.md").write_text("# 标题\n" + "长" * 100, encoding="utf-8")
        docs = fx.load_docs(tmp_path, max_chars=20)
        assert "截断" in docs["d"]


class TestExtractAnswer:
    def test_simple(self):
        assert extract_answer("依据第四十七条。\nANSWER: B") == "B"

    def test_chinese_colon(self):
        assert extract_answer("推理……\nANSWER：AC") == "AC"

    def test_last_occurrence_wins(self):
        assert extract_answer("ANSWER: A\n再想想\nANSWER: ACD") == "ACD"

    def test_no_marker_returns_empty(self):
        assert extract_answer("我认为选 A") == ""


class TestBuildMessages:
    def test_a_split_includes_docs_and_options(self):
        q = Question.model_validate(
            _sample_question(answer_format="multi", gold="ACD", doc_ids=["doc_a", "doc_b"])
        )
        docs = {
            "doc_a": "# 文档甲\n甲的内容",
            "doc_b": "# 文档乙\n乙的内容",
        }
        messages = build_messages(q, docs)
        assert messages[0]["role"] == "system"
        user = messages[1]["content"]
        assert "文档甲" in user and "文档乙" in user
        assert "测试题干" in user
        assert "D. 丁" in user
        assert "ANSWER" in user
        assert "ACD" in user  # multi 的格式示例

    def test_a_split_requires_doc_ids(self):
        q = Question.model_validate(_sample_question(doc_ids=[]))
        with pytest.raises(ValueError, match="A 榜口径"):
            build_messages(q, {})


class TestQwenClientGuard:
    def test_missing_key_raises_with_instructions(self):
        from findata.finqa.qwen import QwenClient

        with pytest.raises(RuntimeError, match="FINDATA_DASHSCOPE_API_KEY"):
            QwenClient(api_key="")
