"""FinanceBench 检索校准：覆盖率定义、评测循环与 k 曲线（合成语料）。"""

from __future__ import annotations

import pytest
from scripts.eval_financebench_recall import coverage, evaluate, normalize


def _docs() -> dict[str, str]:
    pages = [
        f"## Page {i}\nrevenue for page {i} was {i} million dollars. " * 30
        for i in range(1, 21)
    ]
    pages[6] = "## Page 7\nspecial clause about guarantee fees 2023"
    return {"docA": "# Doc A\n" + "\n".join(pages)}


def _questions() -> list[dict]:
    return [
        {
            "financebench_id": "f1",
            "doc_name": "docA",
            "question": "what are the guarantee fees in 2023",
            "evidence": [{"evidence_text": "special clause about guarantee fees 2023"}],
        },
        {
            "financebench_id": "f2",
            "doc_name": "missing_doc",
            "question": "anything",
            "evidence": [{"evidence_text": "anything at all"}],
        },
    ]


def test_normalize_collapses_case_and_space():
    assert normalize("Table  of\nContents ") == "table of contents"


def test_coverage_word_set_semantics():
    assert coverage("guarantee fees 2023", "the guarantee fees for 2023 were X") == 1.0
    assert coverage("guarantee fees 2023", "guarantee fees") == pytest.approx(2 / 3)
    assert coverage("", "anything") == 0.0


def test_evaluates_and_skips_missing_docs():
    result = evaluate(_questions(), _docs(), k_values=(2, 4))
    assert result["evaluated"] == 1
    assert result["skipped_no_doc"] == 1
    # 金证据独占第 7 页，任何合理 top-2 都该把它召回
    assert result["recall_at_k"][2]["rate"] == 1.0
    assert result["per_question"][0]["coverages"][2] >= 0.8
