"""混合检索：BM25F（词法）+ 向量（语义）RRF 名次融合（L2 深化，产品口径）。

为什么是 RRF 而非分数线性组合：BM25 分数无界且随语料分布漂移，余弦
相似度有界 [-1,1]——两个量纲直接加权需要分布敏感的归一化（min-max /
z-score），调参面大还不可迁移；RRF 只用**名次**（Σ w/(k+rank)，rank
从 1 起），对量纲与分布不敏感、零调参（k=60 是 TREC 系标准默认）、
逐位确定性可复现——与「评测数字必须可复现」铁律对齐。

产品口径声明：embedding 只进本模块（产品模式）；赛题口径评测线
（isolate/baseline/submit）不 import 这里——对外引用赛题纪律
（禁 embedding）时必须注明产品形态的差异
（docs/next-cycle-roadmap.md §3.1.1，2026-10-10 所有者裁决）。
"""

from __future__ import annotations

import numpy as np

from findata.finqa.embedding import Embedder
from findata.finqa.retrieval import BM25Index, Chunk, merge_rotating

RRF_K = 60

# 文档嵌入分批上限：缓存按批落盘（进程中断不丢已嵌入批），内存有界
_EMBED_BATCH = 256


class HybridIndex:
    """检索接口与 BM25Index 对齐（chunks / retrieve / retrieve_many / doc_scores）。

    词法臂复用 :class:`BM25Index`（含 L2 的字段通道与查询同义扩展），
    向量臂嵌入「标题 + 正文」（节标题是强语义信号）；``bm25_weight=0``
    即纯向量臂（消融用），``vector_weight=0`` 退化为纯 BM25。
    """

    def __init__(
        self,
        chunks: list[Chunk],
        embedder: Embedder,
        rrf_k: int = RRF_K,
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> None:
        if not chunks:
            raise ValueError("chunk 列表为空，无法建索引")
        self.bm25 = BM25Index(chunks)
        self.chunks = self.bm25.chunks  # 复用同一份切块与前言排除规则
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        texts = [f"{c.header}\n{c.text}" for c in self.chunks]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            # 分批嵌入而非一次性全量：缓存增量落盘（万块级语料嵌入分钟级，
            # 中断重启不丢已完成批），峰值内存只有单批的向量
            vectors.extend(embedder.embed_documents(texts[i : i + _EMBED_BATCH]))
        vecs = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        self._matrix = vecs / np.where(norms == 0, 1.0, norms)

    def _vector_scores(self, query: str) -> np.ndarray:
        vec = np.asarray(self.embedder.embed_queries([query])[0], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return self._matrix @ (vec / (norm or 1.0))

    def _pool_fused(self, query: str, pool: list[int]) -> dict[int, float]:
        """池内两臂名次 → RRF 融合分。名次在**池内**计：限定检索（A 榜）
        与全局检索（B 榜）下同名块的名次口径不同，池内排名才与实际
        候选集一致。"""
        bm25_scores = self.bm25._scores(query)  # noqa: SLF001 — 组合复用同一打分入口
        vec_scores = self._vector_scores(query)
        by_bm25 = sorted(pool, key=lambda i: bm25_scores[i], reverse=True)
        by_vec = sorted(pool, key=lambda i: vec_scores[i], reverse=True)
        rank_bm25 = {i: r for r, i in enumerate(by_bm25, start=1)}
        rank_vec = {i: r for r, i in enumerate(by_vec, start=1)}
        fused: dict[int, float] = {}
        for i in pool:
            score = 0.0
            if self.bm25_weight:
                score += self.bm25_weight / (self.rrf_k + rank_bm25[i])
            if self.vector_weight:
                score += self.vector_weight / (self.rrf_k + rank_vec[i])
            fused[i] = score
        return fused

    def rank_pool(self, query: str, pool: list[int]) -> list[int]:
        """池内按融合分降序；平分时取块序靠前者（确定性）。

        评测脚本（eval_financebench_recall.py）直接消费本方法做三臂对比。
        """
        fused = self._pool_fused(query, list(pool))
        return sorted(pool, key=lambda i: (fused[i], -i), reverse=True)

    def doc_scores(self, query: str) -> dict[str, float]:
        """文档级得分：该文档 top-3 融合分之和（B 榜两阶段检索的粗筛信号）。"""
        fused = self._pool_fused(query, list(range(len(self.chunks))))
        by_doc: dict[str, list[float]] = {}
        for i, score in fused.items():
            by_doc.setdefault(self.chunks[i].doc_id, []).append(score)
        return {d: sum(sorted(ss, reverse=True)[:3]) for d, ss in by_doc.items()}

    def _pool_for(self, doc_ids: list[str] | None) -> list[int]:
        if not doc_ids:
            return list(range(len(self.chunks)))
        return [i for i, c in enumerate(self.chunks) if c.doc_id in set(doc_ids)]

    def retrieve(
        self, query: str, k: int = 6, doc_ids: list[str] | None = None
    ) -> list[Chunk]:
        """按融合相关度返回前 k 个 chunk；doc_ids 非空时只在给定文档内检索。"""
        pool = self._pool_for(doc_ids)
        if not pool:
            return []
        return [self.chunks[i] for i in self.rank_pool(query, pool)[:k]]

    def retrieve_many(
        self,
        queries: list[str],
        k_per_query: int,
        total_cap: int,
        doc_ids: list[str] | None = None,
        docs_top: int = 0,
    ) -> list[Chunk]:
        """多查询轮转合并（与 BM25Index 同语义，共享 merge_rotating）。

        docs_top 两阶段粗筛与 BM25Index.retrieve_many 同口径：doc_ids
        显式给定时以它为准。
        """
        if not queries:
            return []
        pool = self._pool_for(doc_ids)
        if not pool:
            return []
        ranked_lists: list[list[int]] = []
        for query in queries:
            pool_q = pool
            if docs_top and not doc_ids:
                allowed = set(
                    doc for doc, _ in sorted(
                        self.doc_scores(query).items(), key=lambda kv: kv[1], reverse=True
                    )[:docs_top]
                )
                pool_q = [i for i in pool if self.chunks[i].doc_id in allowed]
            if not pool_q:
                continue
            ranked_lists.append(self.rank_pool(query, pool_q))
        return merge_rotating(ranked_lists, self.chunks, total_cap)
