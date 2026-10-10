"""L2 深化：混合检索（BM25F + 向量 RRF 融合）——离线可跑（HashEmbedder）。

质量结论（answer@8/recall@8）不在这里测：哈希嵌入没有真实语义，CI 里
也没有 fastembed/网络。本文件钉死的是**机制**：RRF 数学、缓存命中、
不可用回退、产品接线字段——真模型消融归档在 examples/financebench-recall-v3/。
"""

from __future__ import annotations

import numpy as np
import pytest

from findata.finqa.embedding import (
    CachedEmbedder,
    DashScopeEmbedder,
    Embedder,
    EmbeddingUnavailable,
    FastEmbedder,
    HashEmbedder,
)
from findata.finqa.hybrid import RRF_K, HybridIndex
from findata.finqa.personal import ask_free
from findata.finqa.qwen import CallRecord
from findata.finqa.retrieval import BM25Index, chunk_docs, merge_rotating

# ── 哈希嵌入：确定性 + 子词重叠可感 ──


class TestHashEmbedder:
    def test_deterministic_and_named(self):
        emb = HashEmbedder(dims=128)
        assert emb.name == "hash-bigram-128"
        assert emb.embed_documents(["net sales"]) == emb.embed_documents(["net sales"])

    def test_subword_overlap_beats_disjoint(self):
        emb = HashEmbedder()
        query = emb.embed_queries(["net sales"])[0]
        near = emb.embed_documents(["network salesman"])[0]
        far = emb.embed_documents(["unrelated quarterly outlook"])[0]
        cos_near = float(np.dot(query, near))
        cos_far = float(np.dot(query, far))
        assert cos_near > cos_far

    def test_satisfies_protocol(self):
        assert isinstance(HashEmbedder(), Embedder)


# ── 缓存：第二次不再嵌入、doc/query 键不混用 ──


class _CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def _embed(self, texts):
        self.calls += len(texts)
        return super()._embed(texts)


class TestCachedEmbedder:
    def test_second_call_all_cache_hits(self, tmp_path):
        inner = _CountingEmbedder()
        cached = CachedEmbedder(inner, tmp_path)
        first = cached.embed_documents(["alpha", "beta"])
        assert cached.embed_calls == 2
        assert inner.calls == 2
        second = cached.embed_documents(["alpha", "beta"])
        assert cached.embed_calls == 2  # 无新嵌入
        assert second == first

    def test_persists_across_instances(self, tmp_path):
        inner = _CountingEmbedder()
        CachedEmbedder(inner, tmp_path).embed_documents(["alpha"])
        fresh = CachedEmbedder(_CountingEmbedder(), tmp_path)
        vecs = fresh.embed_documents(["alpha"])
        assert fresh.embed_calls == 0
        assert len(vecs[0]) == 256

    def test_doc_and_query_keys_distinct(self, tmp_path):
        inner = _CountingEmbedder()
        cached = CachedEmbedder(inner, tmp_path)
        cached.embed_documents(["net sales"])
        cached.embed_queries(["net sales"])
        assert cached.embed_calls == 2  # bge 查询前缀纪律：两侧不混用


# ── RRF 融合数学（白盒：两臂名次可控）──


def _three_chunk_index() -> HybridIndex:
    docs = {
        "DOC": (
            "# DOC\n\n## A\n\nfirst section body text for indexing\n\n"
            "## B\n\nsecond section body text for indexing\n\n"
            "## C\n\nthird section body text for indexing\n"
        )
    }
    return HybridIndex(chunk_docs(docs), HashEmbedder())


class TestRRFMath:
    def test_fused_order_matches_formula(self, monkeypatch):
        index = _three_chunk_index()
        monkeypatch.setattr(index.bm25, "_scores", lambda q: [3.0, 2.0, 1.0])
        monkeypatch.setattr(
            index, "_vector_scores", lambda q: np.array([0.2, 0.1, 0.3])
        )
        # bm25 名次 [0,1,2]，向量名次 [2,0,1]
        fused = {
            0: 1 / (RRF_K + 1) + 1 / (RRF_K + 2),
            1: 1 / (RRF_K + 2) + 1 / (RRF_K + 3),
            2: 1 / (RRF_K + 3) + 1 / (RRF_K + 1),
        }
        expected = sorted(fused, key=lambda i: fused[i], reverse=True)
        assert index.rank_pool("q", [0, 1, 2]) == expected
        assert expected[0] == 0  # 任一臂的第 1 名优先（1/x 凸性：两臂中游必输）

    def test_vector_only_arm_follows_vector_ranking(self, monkeypatch):
        index = HybridIndex(
            chunk_docs(
                {
                    "DOC": (
                        "# DOC\n\n## A\n\nfirst section body text\n\n"
                        "## B\n\nsecond section body text\n\n"
                        "## C\n\nthird section body text\n"
                    )
                }
            ),
            HashEmbedder(),
            bm25_weight=0.0,
        )
        monkeypatch.setattr(index.bm25, "_scores", lambda q: [3.0, 2.0, 1.0])
        monkeypatch.setattr(
            index, "_vector_scores", lambda q: np.array([0.2, 0.1, 0.3])
        )
        assert index.rank_pool("q", [0, 1, 2]) == [2, 0, 1]

    def test_symmetric_ranks_tie_breaks_by_chunk_order(self, monkeypatch):
        index = _three_chunk_index()
        monkeypatch.setattr(index.bm25, "_scores", lambda q: [2.0, 1.0, 0.0])
        monkeypatch.setattr(
            index, "_vector_scores", lambda q: np.array([0.1, 0.2, 0.3])
        )
        # 两臂名次完全相反 → 0 与 2 融合分相等，平分取块序靠前者
        assert index.rank_pool("q", [0, 1, 2])[0] == 0


# ── 黑盒机制：词法零命中的块被向量臂找回 ──


_SUBWORD_DOC = {
    "DOC": (
        "# DOC\n\n## Lexical\n\n"
        "sales revenue grew strongly this quarter according to consolidated statements\n\n"
        "## Subword\n\n"
        "network salesman allocations biweekly reconciliation entries quarterly\n"
    ),
    "OTHER": "# OTHER\n\n## Noise\n\ncompletely unrelated procurement logistics paragraph\n",
}


class TestHybridMechanism:
    def test_bm25_misses_subword_chunk(self):
        index = BM25Index(chunk_docs(_SUBWORD_DOC))
        hits = index.retrieve("net sales", k=2, doc_ids=["DOC"])
        assert hits[0].header == "Lexical"
        assert hits[-1].header == "Subword"  # 词法零命中 → 殿后

    def test_vector_arm_ranks_subword_first(self):
        index = HybridIndex(chunk_docs(_SUBWORD_DOC), HashEmbedder(), bm25_weight=0.0)
        hits = index.retrieve("net sales", k=2, doc_ids=["DOC"])
        assert hits[0].header == "Subword"

    def test_vector_weight_dominant_promotes_subword(self):
        """词法第 2 + 语义第 1 的块在足够向量权重下登顶（融合的价值主张）。"""
        index = HybridIndex(
            chunk_docs(_SUBWORD_DOC), HashEmbedder(), vector_weight=5.0
        )
        hits = index.retrieve("net sales", k=2, doc_ids=["DOC"])
        assert hits[0].header == "Subword"

    def test_doc_ids_pool_respected(self):
        index = HybridIndex(chunk_docs(_SUBWORD_DOC), HashEmbedder())
        hits = index.retrieve("salesman network", k=10, doc_ids=["OTHER"])
        assert hits and all(c.doc_id == "OTHER" for c in hits)

    def test_retrieve_many_dedup_and_cap(self):
        index = HybridIndex(chunk_docs(_SUBWORD_DOC), HashEmbedder())
        merged = index.retrieve_many(["net sales", "salesman network"], 6, total_cap=4)
        assert len(merged) == 3  # 语料共 3 块，cap 4 取尽且去重
        assert len({id(c) for c in merged}) == 3
        two_stage = index.retrieve_many(["net sales"], 6, total_cap=4, docs_top=1)
        assert 0 < len(two_stage) <= 4
        assert len({c.doc_id for c in two_stage}) == 1  # 粗筛后单文档池

    def test_doc_scores_covers_all_docs(self):
        index = HybridIndex(chunk_docs(_SUBWORD_DOC), HashEmbedder())
        scores = index.doc_scores("net sales")
        assert set(scores) == {"DOC", "OTHER"}
        assert scores["DOC"] > scores["OTHER"]


# ── 轮转合并（BM25Index 与 HybridIndex 共享，行为钉死）──


class TestMergeRotating:
    def test_rotation_dedup_cap(self):
        from findata.finqa.retrieval import Chunk

        chunks = [Chunk("d", f"h{i}", f"t{i}") for i in range(3)]
        assert merge_rotating([[0, 1], [1, 2]], chunks, 3) == [
            chunks[0],
            chunks[1],
            chunks[2],
        ]
        assert merge_rotating([[0, 1], [1, 2]], chunks, 2) == [chunks[0], chunks[1]]

    def test_empty(self):
        assert merge_rotating([], [], 3) == []


# ── 产品接线：ask_free 的混合检索开关与显式回退 ──


class _FakeClient:
    def chat(self, qid, purpose, messages):
        return "依据【DOC · Lexical】营收数据回答。", CallRecord(
            qid=qid, purpose=purpose, model="fake", prompt_tokens=10, completion_tokens=2
        )


class _BrokenEmbedder(HashEmbedder):
    def embed_documents(self, texts):
        raise EmbeddingUnavailable("模型未安装（测试注入）")


class TestAskFreeHybrid:
    DOCS = _SUBWORD_DOC

    def test_hybrid_marks_retriever(self):
        result = ask_free(
            "net sales 是多少", self.DOCS, _FakeClient(), embedder=HashEmbedder()
        )
        assert result["retriever"] == "hybrid:hash-bigram-256"
        assert result["retrieved_sections"] >= 1
        assert result["total_tokens"] > 0

    def test_unavailable_falls_back_to_bm25(self):
        result = ask_free(
            "net sales 是多少", self.DOCS, _FakeClient(), embedder=_BrokenEmbedder()
        )
        assert result["retriever"].startswith("bm25f")
        assert "fallback" in result["retriever"]
        assert result["total_tokens"] > 0  # 回退不是失败：照常作答

    def test_default_stays_bm25(self):
        result = ask_free("net sales 是多少", self.DOCS, _FakeClient())
        assert result["retriever"] == "bm25f"

    def test_refusal_labels_retriever(self):
        result = ask_free(
            "该不该买入这只股票", self.DOCS, _FakeClient(), embedder=HashEmbedder()
        )
        assert result["refused"] is True
        assert result["retriever"] == "refused"


# ── 后端可用性 ──


class TestBackends:
    def test_fastembed_missing_hint(self):
        try:
            import fastembed  # noqa: F401

            pytest.skip("fastembed 已安装（本机产品环境）")
        except ImportError:
            pass
        with pytest.raises(EmbeddingUnavailable, match="extra embed"):
            FastEmbedder()

    def test_unknown_backend_rejected(self):
        from findata.finqa.embedding import build_embedder

        with pytest.raises(EmbeddingUnavailable, match="未知嵌入后端"):
            build_embedder("magic", "m", "data/embed-cache")


class _StubEmbeddings:
    """接口形状 client.embeddings.create(...)；按 index 逆序返回 data
    （验证排序纪律），usage 按 batch 大小累计。"""

    class _Resp:
        def __init__(self, n):
            self.data = [
                type("D", (), {"index": n - 1 - i, "embedding": [float(n - 1 - i)]})()
                for i in range(n)
            ]
            self.usage = type("U", (), {"prompt_tokens": n})()

    @property
    def embeddings(self):
        return self

    def create(self, model, input):
        return self._Resp(len(input))


class TestDashScopeEmbedder:
    def test_batches_sorted_usage_counted(self):
        emb = DashScopeEmbedder(
            model="text-embedding-test", api_key="k", client=_StubEmbeddings(),
            batch_size=2,
        )
        vecs = emb.embed_documents(["a", "b", "c", "d", "e"])
        # 批内按 index 排序（stub 故意逆序返回）：3 批 = [0,1] + [0,1] + [0]
        assert vecs == [[0.0], [1.0], [0.0], [1.0], [0.0]]
        assert emb.usage_prompt_tokens == 5  # 3 批：2+2+1
        assert emb.embed_queries(["q"]) == [[0.0]]

    def test_missing_key(self, monkeypatch):
        from findata.config import settings

        monkeypatch.setattr(settings, "dashscope_api_key", "")
        with pytest.raises(EmbeddingUnavailable, match="API key"):
            DashScopeEmbedder(api_key=None, client=_StubEmbeddings())
