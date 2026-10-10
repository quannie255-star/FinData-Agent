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

# ── 查询同义扩展（L2 英文域）：题问与 10-K 行文的词形失配 ──
# 题问 "revenue"，10-K 写 "Net sales"——零 token 交集。扩展只在**查询侧**
# 追加同组 token（语料侧不动：对称扩展稀释 idf 且建索引变慢）。组内词
# 相互可替换；匹配按词边界（"eps" 不得命中 "epsilon"）。
QUERY_EXPANSION = True  # 消融开关：FinanceBench 归因测量用，产品默认开

_SYNONYM_GROUPS: list[list[str]] = [
    ["revenue", "revenues", "net sales", "total revenue", "net revenues"],
    ["net income", "net earnings", "net profit"],
    ["operating income", "income from operations", "operating profit"],
    ["earnings per share", "diluted eps", "eps"],
    ["cash flow", "cash flows from operating", "operating cash flow"],
    ["shareholders equity", "stockholders equity", "total equity"],
    ["cost of revenue", "cost of sales", "cost of goods sold", "cogs"],
    ["gross margin", "gross profit"],
    ["research and development", "r&d"],
    ["long-term debt", "long term debt", "notes payable"],
]


_WS_QUERY = re.compile(r"\s+")


def expand_query(query: str) -> str:
    """查询命中同组任一词时，把其余词追加到查询尾部（保序去重）。"""
    lowered = " " + _WS_QUERY.sub(" ", query.lower()) + " "
    extra: dict[str, None] = {}
    for group in _SYNONYM_GROUPS:
        for term in group:
            if _word_hit(lowered, term):
                for t in group:
                    if not _word_hit(lowered, t):
                        extra[t] = None
                break
    if not extra:
        return query
    return query + " " + " ".join(extra)


def _word_hit(lowered_query: str, term: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return re.search(pattern, lowered_query) is not None


@dataclass
class Chunk:
    doc_id: str
    header: str  # 形如「第四十七条」或文档标题（前言块）
    text: str  # header + 正文
    is_preamble: bool = False  # 首个 ## 前的前言/来源说明块——不进检索索引

    def block(self) -> str:
        return f"【{self.doc_id} · {self.header}】\n{self.text}"


def merge_rotating(
    ranked_lists: list[list[int]], chunks: list[Chunk], total_cap: int
) -> list[Chunk]:
    """多查询轮转合并：按名次轮转取各列表高位命中，去重后截断。

    轮转而非按查询拼接：不让第一个查询的 k 个命中把名额占满——多证据题
    的证据散在不同查询里（v2 的 reg_s_001/015 就是单查询被单一域占满，
    第二域证据进不了上下文，模型只能诚实判错）。BM25Index 与
    HybridIndex（hybrid.py）共用本函数，两臂合并行为逐位一致。
    """
    if not ranked_lists:
        return []
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
            merged.append(chunks[idx])
            if len(merged) >= total_cap:
                return merged
    return merged


def tokenize(text: str) -> list[str]:
    """CJK 二元组 + 条款号整token + 西文/数字词（轻量复数归一）。

    条款号必须整 token：query 里的「第四十七条」要和 chunk 里的精确对上，
    二元组会把它拆碎（第四/四十/十七条），锚点就丢了。
    英文词做复数归一（expenditure/expenditures 归并）——FinanceBench 校准
    实测：无词干归一时英文召回 6%，这是英文域的主要失败原因之一。
    """
    tokens: list[str] = _ARTICLE_TOKEN.findall(text)
    tokens.extend(_normalize_latin(m.group(0)) for m in _LATIN.finditer(text))
    chars = [c for c in text if _CJK.match(c)]
    tokens.extend(a + b for a, b in zip(chars, chars[1:], strict=False))
    return tokens


def _normalize_latin(word: str) -> str:
    w = word.lower()
    if len(w) > 4:
        if w.endswith("ies"):
            return w[:-3] + "y"
        if w.endswith("es") and not w.endswith("ses"):
            return w[:-2]
        if w.endswith("s") and not w.endswith("ss"):
            return w[:-1]
    return w


def _split_long(text: str, cap: int) -> list[str]:
    """超长块按行累积切分，相邻块尾部两行重叠。

    绝不切断行内（财报表格的数值与单位在同一行，行内切分会把
    「803,964,958,000.00」腰斩）；两行重叠是廉价保险——标签行与数值行
    相邻时，跨块也能至少有一侧看到完整配对。
    """
    lines = text.splitlines()
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        # 法规模块整条合成单行，可长达 2000+ 字符：超长行按字符硬切
        # （报表类文档行短，不会走到这条分支，数值完整性不受影响）
        if len(line) > cap:
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            for i in range(0, len(line), cap):
                pieces.append(line[i : i + cap])
            continue
        if current and size + len(line) > cap:
            pieces.append("\n".join(current))
            current = current[-2:]  # 尾部两行带入下一块
            size = sum(len(x) for x in current)
        current.append(line)
        size += len(line)
    if current:
        tail = "\n".join(current)
        # 避免产生过小的尾块：不足 cap/4 并入前一块
        if pieces and len(tail) < cap // 4:
            pieces[-1] = pieces[-1] + "\n" + tail
        else:
            pieces.append(tail)
    return pieces


def chunk_docs(docs: dict[str, str], max_chunk_chars: int = 0) -> list[Chunk]:
    """按 `## ` 标题切块；首个标题前的前言并入标题块。

    max_chunk_chars > 0 时，超过该长度的块再做二级切分（报表类文档整节
    可达数千字符，一个大块会吃掉整个判定上下文——v3 的成本主因）。
    """
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
        for block_index, (header, body) in enumerate(blocks):
            body_text = "\n".join(body).strip()
            if not body_text:
                continue
            pieces = (
                _split_long(body_text, max_chunk_chars) if max_chunk_chars else [body_text]
            )
            for i, piece in enumerate(pieces, start=1):
                if len(pieces) > 1:
                    piece = f"（{i}/{len(pieces)}）\n{piece}"
                chunks.append(
                    Chunk(
                        doc_id=doc_id, header=header, text=piece,
                        is_preamble=block_index == 0,
                    )
                )
    return chunks


_SHORT_LABEL = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9，、（）()\s%．.]{1,14}$")
# 英文短标签行：10-K 表格的指标名/期次行（"Total net sales"、
# "January 1-31, 2018"）。FinanceBench 校准第一期的定位：字段通道要求
# CJK，英文标签永不触发——英文域表格块拿不到低频高信号加成
_EN_SHORT_LABEL = re.compile(r"^[\sA-Za-z0-9$_(),.%/\-'\"]{1,48}$")

# 字段权重：v3 的失败证据是营收对比表块被「增长」等散文高频词挤出 top-4，
# 而表格标签行（营业收入 / 2025 年）是低频高信号——正文统计压不过散文，
# 需要独立字段通道抬一手
FIELD_WEIGHT = 2.0


def _field_tokens(chunk: Chunk) -> list[str]:
    """字段通道：节标题 + 块内短标签行（表格指标名 / 年份行）。

    与正文分开建索引再线性组合（BM25F 的形态）——若把加权 token 直接注入
    正文语料，BM25 的长度归一化会反噬多标签块（实测：表格块不升反降）。
    英文行要求含字母（纯数字行不做字段——年份与数值挂在含字母的期次行上）。
    重组后的管道行（``a | b | c``）按 ``|`` 拆回单元格再逐格判定——标签
    信号不因行重组丢失（实测：不拆则重组反而抵消字段通道的增益）。
    """
    tokens = tokenize(chunk.header)
    for line in chunk.text.splitlines():
        candidates = line.split("|") if "|" in line else [line]
        for cand in candidates:
            stripped = cand.strip()
            if _CJK.search(stripped):
                if _SHORT_LABEL.match(stripped):
                    tokens.extend(tokenize(stripped))
            elif _EN_SHORT_LABEL.match(stripped) and re.search(r"[A-Za-z]", stripped):
                tokens.extend(tokenize(stripped))
    return tokens


class BM25Index:
    """检索接口固定为 retrieve(query, k, doc_ids)；换实现只动这里。

    前言/来源说明块不进索引（M2.4 教训：其标题与来源行跟 query 的高频词
    重合，字段加权后被顶到 #1-#4，把真证据挤出召回——它们从来不是答题
    证据）。整份文档只有前言块的极端情况保留索引，否则空索引不可用。
    """

    def __init__(self, chunks: list[Chunk]) -> None:
        if not chunks:
            raise ValueError("chunk 列表为空，无法建索引")
        indexed = [c for c in chunks if not c.is_preamble]
        # v4 修的边界：整份文档没有任何 ## 分节时（如研报整文转换），
        # 全文就是那一个「前言块」——若照常排除，该文档的受限检索池为空，
        # 判定只能拿到「检索未命中」。只在该文档存在正文分节时才排除前言。
        covered = {c.doc_id for c in indexed}
        indexed += [c for c in chunks if c.is_preamble and c.doc_id not in covered]
        indexed = indexed or chunks
        self.chunks = indexed
        self._body_bm25 = BM25Okapi([tokenize(c.text) for c in indexed])
        self._field_bm25 = BM25Okapi([_field_tokens(c) for c in indexed])

    def _scores(self, query: str) -> list[float]:
        toks = tokenize(expand_query(query) if QUERY_EXPANSION else query)
        body = self._body_bm25.get_scores(toks)
        field = self._field_bm25.get_scores(toks)
        return [b + FIELD_WEIGHT * f for b, f in zip(body, field, strict=True)]

    def doc_scores(self, query: str) -> dict[str, float]:
        """文档级得分：该文档 top-3 片段得分之和（B 榜两阶段检索的粗筛信号）。"""
        by_doc: dict[str, list[float]] = {}
        for chunk, score in zip(self.chunks, self._scores(query), strict=True):
            by_doc.setdefault(chunk.doc_id, []).append(score)
        return {d: sum(sorted(ss, reverse=True)[:3]) for d, ss in by_doc.items()}

    def _top_docs(self, query: str, docs_top: int) -> list[str]:
        ranked = sorted(self.doc_scores(query).items(), key=lambda kv: kv[1], reverse=True)
        return [doc_id for doc_id, _ in ranked[:docs_top]]

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
        scores = self._scores(query)
        ranked = sorted(pool, key=lambda i: scores[i], reverse=True)
        return [self.chunks[i] for i in ranked[:k]]

    def retrieve_many(
        self,
        queries: list[str],
        k_per_query: int,
        total_cap: int,
        doc_ids: list[str] | None = None,
        docs_top: int = 0,
    ) -> list[Chunk]:
        """多查询合并检索（M2.1 逐选项）：轮转取各查询的高位命中，去重后截断。

        轮转而非按查询拼接：不让第一个查询的 k 个命中把名额占满——多证据题
        的证据散在不同查询里（v2 的 reg_s_001/015 就是单查询被单一域占满，
        第二域证据进不了上下文，模型只能诚实判错）。

        docs_top > 0 时启用**两阶段**（B 榜）：每个查询先按文档级得分粗筛
        top-N 文档，再在选中文档内做片段检索——治 v4 的 fin_s_004 病根：
        193 页合同文档在全局池用片段级打分挤占了财报表格块。doc_ids 显式
        给定时以它为准（A 榜限定强于粗筛），docs_top 被忽略。
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
            pool_q = pool
            if docs_top and not doc_ids:
                allowed = set(self._top_docs(query, docs_top))
                pool_q = [i for i in pool if self.chunks[i].doc_id in allowed]
            if not pool_q:
                continue
            scores = self._scores(query)
            ranked_lists.append(sorted(pool_q, key=lambda i: scores[i], reverse=True))
        return merge_rotating(ranked_lists, self.chunks, total_cap)
