"""M2 检索层：BM25 条款级检索（轮子：rank-bm25，不手写打分器）。

赛题约束（docs/afac-track4.md §0.1）：正式答题阶段禁 embedding，检索只能是
规则型（BM25/关键词/结构化索引）——本模块零模型调用、零 token 成本。

设计：
- **条款级分块**：文档按 markdown `## ` 标题切块（法规文档天然以条为单位），
  条款编号是检索锚点，切块时刻意与条文对齐，不许把条文切碎；
- **中文分词**：CJK 二元组（char-bigram）+ 条款号整token + 西文/数字词。
  二元组是 CJK 检索的标准基线，确定性、零依赖，不需要分词模型；
- **限定检索**：A 榜题给了 doc_ids 时只在给定文档内检索（省 token），
  B 榜题全局检索（盲检）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

_ARTICLE_TOKEN = re.compile(r"第[一二三四五六七八九十百千]+条")
_LATIN = re.compile(r"[A-Za-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class Chunk:
    doc_id: str
    header: str  # 形如「第四十七条」或文档标题（前言块）
    text: str  # header + 正文

    def block(self) -> str:
        return f"【{self.doc_id} · {self.header}】\n{self.text}"


def tokenize(text: str) -> list[str]:
    """CJK 二元组 + 条款号整 token + 西文/数字小写词。

    条款号必须整 token：query 里的「第四十七条」要和 chunk 里的精确对上，
    二元组会把它拆碎（第四/四十/十七条），锚点就丢了。
    """
    tokens: list[str] = _ARTICLE_TOKEN.findall(text)
    tokens.extend(m.group(0).lower() for m in _LATIN.finditer(text))
    chars = [c for c in text if _CJK.match(c)]
    tokens.extend(a + b for a, b in zip(chars, chars[1:], strict=False))
    return tokens


def chunk_docs(docs: dict[str, str]) -> list[Chunk]:
    """按 `## ` 标题切块；首个标题前的前言并入标题块。"""
    chunks: list[Chunk] = []
    for doc_id, text in sorted(docs.items()):
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else doc_id
        blocks: list[tuple[str, list[str]]] = [(title, [])]
        for line in lines[1:]:
            if line.startswith("## "):
                blocks.append((line[3:].strip(), []))
            else:
                blocks[-1][1].append(line)
        for header, body in blocks:
            body_text = "\n".join(body).strip()
            if not body_text:
                continue
            chunks.append(Chunk(doc_id=doc_id, header=header, text=body_text))
    return chunks


class BM25Index:
    """检索接口固定为 retrieve(query, k, doc_ids)；换实现只动这里。"""

    def __init__(self, chunks: list[Chunk]) -> None:
        if not chunks:
            raise ValueError("chunk 列表为空，无法建索引")
        self.chunks = chunks
        self._corpus = [tokenize(c.text) for c in chunks]
        self._bm25 = BM25Okapi(self._corpus)

    def retrieve(
        self, query: str, k: int = 6, doc_ids: list[str] | None = None
    ) -> list[Chunk]:
        """按相关度返回前 k 个 chunk；doc_ids 非空时只在给定文档内检索。"""
        pool = (
            [i for i, c in enumerate(self.chunks) if c.doc_id in set(doc_ids)]
            if doc_ids
            else range(len(self.chunks))
        )
        pool = list(pool)
        if not pool:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(pool, key=lambda i: scores[i], reverse=True)
        return [self.chunks[i] for i in ranked[:k]]

    def retrieve_many(
        self,
        queries: list[str],
        k_per_query: int,
        total_cap: int,
        doc_ids: list[str] | None = None,
    ) -> list[Chunk]:
        """多查询合并检索（M2.1 逐选项）：轮转取各查询的高位命中，去重后截断。

        轮转而非按查询拼接：不让第一个查询的 k 个命中把名额占满——多证据题
        的证据散在不同查询里（v2 的 reg_s_001/015 就是单查询被单一域占满，
        第二域证据进不了上下文，模型只能诚实判错）。
        """
        if not queries:
            return []
        pool = (
            [i for i, c in enumerate(self.chunks) if c.doc_id in set(doc_ids)]
            if doc_ids
            else list(range(len(self.chunks)))
        )
        if not pool:
            return []
        ranked_lists: list[list[int]] = []
        for query in queries:
            scores = self._bm25.get_scores(tokenize(query))
            ranked_lists.append(sorted(pool, key=lambda i: scores[i], reverse=True))
        seen: set[int] = set()
        merged: list[Chunk] = []
        max_len = max(len(r) for r in ranked_lists)
        for rank in range(max_len):
            for ranked in ranked_lists:
                if rank >= len(ranked):
                    continue
                idx = ranked[rank]
                if idx in seen:
                    continue
                seen.add(idx)
                merged.append(self.chunks[idx])
                if len(merged) >= total_cap:
                    return merged
        return merged
