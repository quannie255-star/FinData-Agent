"""M2 检索层：确定性、限定检索、与两种模式的端到端（假客户端，零网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from findata.finqa import fixture as fx
from findata.finqa.baseline import (
    build_messages_from_chunks,
    option_queries,
    query_text,
    run_baseline,
)
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

    def test_secondary_split_for_long_blocks(self):
        # v3：报表整节数千字符 → 二级切分，块头带（i/n），行不被切断
        long_body = "\n".join(f"第{i}行数据 803,964,958,000.00 元" for i in range(120))
        docs = {"fin": f"# 某摘要\n## 二、公司基本情况\n{long_body}"}
        chunks = chunk_docs(docs, max_chunk_chars=1000)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.text) < 1600  # cap + 尾部重叠的余量
            assert "803,964,958,000.00" not in c.text.replace("803,964,958,000.00 元", "")
        assert any("（1/" in c.text for c in chunks)
        # 行级重叠：相邻块共享尾部两行
        assert chunks[0].text.splitlines()[-1] in chunks[1].text

    def test_short_blocks_untouched(self):
        docs = {"d": "# 标题\n## 第一条\n短内容"}
        chunks = chunk_docs(docs, max_chunk_chars=1000)
        # 标题块无正文被跳过；短条文不做二级切分、不带（i/n）标记
        assert [(c.header, c.text) for c in chunks] == [("第一条", "短内容")]


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


class TestRetrieveMany:
    """M2.1 逐选项检索的验收：v2 两道错题缺失的证据必须能被召回（离线确定性）。"""

    def _index(self):
        # 必须与生产同配置（含二级切分）——曾经漏掉 max_chunk_chars，
        # 验收测试测的是另一套分块，表格块排名失真（v4 抓出的测试分叉）
        from findata.finqa.baseline import MAX_CHUNK_CHARS

        docs = fx.load_docs(FIXTURE_ROOT)
        return (
            BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS)),
            fx.load_questions(FIXTURE_ROOT),
        )

    def test_round_robin_not_first_query_dominant(self):
        # 两个查询各强命中不同文档时，合并结果必须两头都进，不被第一个查询占满
        docs = {
            "a": "# 甲法\n担保 资产负债率 担保 资产负债率 担保",
            "b": "# 乙法\n独立董事 独立性 独立董事",
        }
        index = BM25Index(chunk_docs(docs))
        merged = index.retrieve_many(
            ["资产负债率 担保", "独立董事 独立性"], k_per_query=3, total_cap=6
        )
        doc_order = [c.doc_id for c in merged]
        assert doc_order[0] == "a" and "b" in doc_order[:2]
        assert len(merged) == len({(c.doc_id, c.header) for c in merged})

    def test_cap_truncates(self):
        index, _ = self._index()
        merged = index.retrieve_many(["担保 股东会", "独立董事"], k_per_query=6, total_cap=3)
        assert len(merged) == 3

    def test_v2_wrong_q15_cross_domain_evidence_recalled(self):
        # v2 错因：选项 A（比亚迪总资产）的财报证据没进上下文 → 逐选项必须召回
        index, questions = self._index()
        q15 = next(q for q in questions if q.qid == "reg_s_015")
        chunks = index.retrieve_many(
            option_queries(q15), k_per_query=6, total_cap=12
        )
        assert any(c.doc_id == "fin_rep_byd_2025" for c in chunks), "跨域财报证据未召回"
        assert any(
            c.doc_id == "strict_csrc_035" and c.header == "第八十三条" for c in chunks
        ), "法规侧证据（83 条）未召回"

    def test_v2_wrong_q001_option_c_evidence_recalled(self):
        # v2 错因：独立性证据（036 第六条 / 023）没进上下文 → 逐选项必须召回
        index, questions = self._index()
        q1 = next(q for q in questions if q.qid == "reg_s_001")
        chunks = index.retrieve_many(
            option_queries(q1), k_per_query=6, total_cap=12, doc_ids=q1.doc_ids
        )
        headers = {(c.doc_id, c.header) for c in chunks}
        assert ("strict_csrc_036", "第六条") in headers, "独董办法第六条未召回"
        assert all(c.doc_id in set(q1.doc_ids) for c in chunks), "A 榜限定失效"

    def test_v3_wrong_fin_table_chunk_recalled(self):
        # M2.4 验收：v3 错因是营收对比表块被散文词频挤出 top-4——
        # 字段加权后表格块必须进 fin_s_001 选项 A 的 top-4
        index, questions = self._index()
        q = next(x for x in questions if x.qid == "fin_s_001")
        chunks = index.retrieve_many(
            [f"{q.question} {q.options['A']}"], k_per_query=4, total_cap=4,
            doc_ids=q.doc_ids,
        )
        joined = "\n".join(c.text for c in chunks)
        assert "803,964" in joined, "2025 年营收值未召回"
        assert "777,102" in joined, "2024 年营收值未召回"

    def test_preamble_chunks_excluded_from_index(self):
        # M2.4 教训：前言/来源块的标题跟 query 高频词重合，字段加权后被顶进
        # 召回挤掉真证据——**有分节的文档**其前言块不进索引；无分节文档的
        # 前言块是全文，保留（见下一条用例）
        index, questions = self._index()
        multi_section_docs = {
            c.doc_id for c in index.chunks if not c.is_preamble
        }
        stray = [
            c for c in index.chunks
            if c.is_preamble and c.doc_id in multi_section_docs
        ]
        assert not stray, f"有分节文档的前言块不应进索引：{[(c.doc_id, c.header) for c in stray]}"
        assert all(c.header != "说明" for c in index.chunks)

    def test_single_section_doc_keeps_preamble_indexed(self):
        # v4 修的边界：整份文档无 ## 分节（研报整文）时，唯一的前言块必须
        # 保留索引——否则该文档的受限检索池为空，判定拿到「检索未命中」
        docs = {"res": "# 某研报\n买入（维持）\n营业总收入 2026E 19,435 百万元"}
        index = BM25Index(chunk_docs(docs))
        hits = index.retrieve("2026E 营业总收入 预测", k=2, doc_ids=["res"])
        assert hits, "无分节文档的受限检索不应为空"

    def test_two_stage_recalls_fin_table_for_b_split(self):
        # v4 fin_s_004 病根：B 榜全局池里 193 页合同文档挤占财报表块——
        # 两阶段（文档级粗筛 top-3）后，比亚迪营收表必须回到 top-4
        index, questions = self._index()
        q = next(x for x in questions if x.qid == "fin_s_004")
        chunks = index.retrieve_many(
            [f"{q.question} {q.options['C']}"], k_per_query=4, total_cap=4, docs_top=1
        )
        assert any("803,964" in c.text for c in chunks), "两阶段后营收表块仍未召回"

    def test_doc_ids_override_docs_top(self):
        # A 榜显式 doc_ids 优先于粗筛（docs_top 被忽略）
        index, questions = self._index()
        q = next(x for x in questions if x.qid == "reg_s_002")
        chunks = index.retrieve_many(
            [f"{q.question} {q.options['B']}"], k_per_query=4, total_cap=4,
            doc_ids=q.doc_ids, docs_top=1,
        )
        assert all(c.doc_id in set(q.doc_ids) for c in chunks)

    def test_field_weight_zero_keeps_body_ranking(self):
        # 字段权重关掉时应退回纯正文排序（对照口径，供调参时复核）
        docs = fx.load_docs(FIXTURE_ROOT)
        import findata.finqa.retrieval as R

        old = R.FIELD_WEIGHT
        R.FIELD_WEIGHT = 0.0
        try:
            index = R.BM25Index(R.chunk_docs(docs, max_chunk_chars=1200))
            hits = index.retrieve("担保 资产负债率", k=2, doc_ids=["strict_csrc_035"])
            assert hits and hits[0].header == "第四十七条"
        finally:
            R.FIELD_WEIGHT = old

    def test_tf_falls_back_to_single_query(self):
        index, questions = self._index()
        q3 = next(q for q in questions if q.qid == "reg_s_003")
        assert len(option_queries(q3)) == 1


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
        b_split = {q.qid for q in questions if not q.doc_ids}
        assert b_split, "脚手架应包含 B 榜口径题（无 doc_ids）"
        docs = fx.load_docs(FIXTURE_ROOT)
        client = self.FakeClient()
        details, ledger, report, skipped = run_baseline(questions, docs, client)
        # 期望从 fixture 推导，不硬编码清单——题集扩缩时这条不该跟着改
        assert set(skipped) == b_split
        assert report.total == len(questions) - len(b_split)
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
