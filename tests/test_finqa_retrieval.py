"""M2 检索层：确定性、限定检索、与两种模式的端到端（假客户端，零网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from findata.finqa import fixture as fx
from findata.finqa.baseline import build_messages_from_chunks, query_text, run_baseline
from findata.finqa.qwen import CallRecord
from findata.finqa.retrieval import BM25Index, chunk_docs, tokenize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


class TestTokenize:
    def test_article_number_stays_whole(self):
        tokens = tokenize("依据第四十七条判断")
        assert "第四十七条" in tokens

    def test_cjk_bigrams(self):
        tokens = tokenize("担保")
        assert "担保" in tokens

    def test_latin_lowercased(self):
        assert "bm25" in tokenize("BM25 检索")


class TestChunkDocs:
    def test_splits_by_header_and_keeps_preamble(self, tmp_path):
        docs = {
            "d1": "# 某法规\n前言说明\n## 第一条\n甲内容\n## 第二条\n乙内容",
        }
        chunks = chunk_docs(docs)
        assert [(c.doc_id, c.header) for c in chunks] == [
            ("d1", "某法规"),
            ("d1", "第一条"),
            ("d1", "第二条"),
        ]
        assert "前言说明" in chunks[0].text
        assert "甲内容" in chunks[1].text


class TestBM25Index:
    def test_guarantee_query_hits_article_47(self):
        docs = fx.load_docs(FIXTURE_ROOT)
        index = BM25Index(chunk_docs(docs))
        q = next(q for q in fx.load_questions(FIXTURE_ROOT) if q.qid == "reg_s_002")
        hits = index.retrieve(query_text(q), k=4)
        assert hits, "检索应命中至少一个片段"
        assert any(h.header == "第四十七条" for h in hits)

    def test_restrict_to_doc_ids(self):
        docs = fx.load_docs(FIXTURE_ROOT)
        index = BM25Index(chunk_docs(docs))
        hits = index.retrieve("独立董事 独立性 不得担任", k=10, doc_ids=["strict_csrc_035"])
        # 章程指引里也有独立字样（第四十三条），但限定后不应混入独董办法的 chunk
        assert all(h.doc_id == "strict_csrc_035" for h in hits)

    def test_global_retrieve_spans_docs(self):
        docs = fx.load_docs(FIXTURE_ROOT)
        index = BM25Index(chunk_docs(docs))
        hits = index.retrieve("独立董事 最多 三家 境内上市公司", k=5)
        assert any(h.header == "第八条" and h.doc_id == "strict_csrc_036" for h in hits)

    def test_empty_pool_returns_empty(self):
        index = BM25Index(chunk_docs({"d": "# 标题\n内容"}))
        assert index.retrieve("任意", k=3, doc_ids=["不存在"]) == []


class TestRunBaselineModes:
    """假客户端端到端：验证两种模式的题目覆盖与台账记账，不看答案对错。"""

    class FakeClient:
        def __init__(self, reply="依据条文。\nANSWER: A") -> None:
            self.reply = reply
            self.calls: list[str] = []

        def chat(self, qid, purpose, messages):
            self.calls.append(qid)
            return self.reply, CallRecord(
                qid=qid, purpose=purpose, model="fake",
                prompt_tokens=10, completion_tokens=2,
            )

    def test_full_mode_skips_b_split(self):
        questions = fx.load_questions(FIXTURE_ROOT)
        docs = fx.load_docs(FIXTURE_ROOT)
        client = self.FakeClient()
        details, ledger, report, skipped = run_baseline(questions, docs, client)
        assert skipped == ["reg_s_010", "reg_s_011"]
        assert report.total == len(questions) - len(skipped)
        assert len(ledger.records) == report.total

    def test_retrieve_mode_covers_all(self):
        questions = fx.load_questions(FIXTURE_ROOT)
        docs = fx.load_docs(FIXTURE_ROOT)
        client = self.FakeClient()
        details, ledger, report, skipped = run_baseline(
            questions, docs, client, mode="retrieve", k=6
        )
        assert skipped == []
        assert report.total == len(questions)
        assert len(ledger.records) == len(questions)

    def test_bad_mode_rejected(self):
        with pytest.raises(ValueError, match="full/retrieve"):
            run_baseline([], {}, self.FakeClient(), mode=" hybrid")

    def test_chunk_messages_carry_doc_and_header(self):
        q = next(x for x in fx.load_questions(FIXTURE_ROOT) if x.qid == "reg_s_006")
        chunks = [chunk_docs({"strict_csrc_036": fx.load_docs(FIXTURE_ROOT)["strict_csrc_036"]})[0]]
        messages = build_messages_from_chunks(q, chunks)
        user = messages[1]["content"]
        assert "strict_csrc_036" in user
        assert "检索片段" in user
        assert "ANSWER" in user
