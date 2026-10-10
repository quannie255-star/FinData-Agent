"""L1/L2：表格保真 + 英文字段通道 + 查询同义扩展。"""

from __future__ import annotations

from findata.finqa.retrieval import (
    QUERY_EXPANSION,
    BM25Index,
    _field_tokens,
    chunk_docs,
    expand_query,
    tokenize,
)
from findata.finqa.tables import reassemble_flattened, render_markdown_table

# ── 表格行重组 ──


class TestReassemble:
    def test_flattened_cells_become_pipe_rows(self):
        text = (
            "Intro paragraph that stays untouched.\n"
            "\n"
            " Announced Plans \n\n or Programs \n\n Period \n\n (1) \n\n"
            " Share or Programs (2) \n\n (Millions) \n\n January 1-31, 2018 \n\n"
            " 714,575 \n\n $ \n\n 245.98 \n\n 714,138 \n\n"
            "\n"
            "Closing prose paragraph here.\n"
        )
        out = reassemble_flattened(text)
        assert "| Announced Plans | or Programs | Period |" in out
        assert "Intro paragraph that stays untouched." in out
        assert "Closing prose paragraph here." in out
        # 单元格之间的空行被吞掉，表格块紧凑
        assert "January 1-31, 2018" in out

    def test_short_scattered_lines_untouched(self):
        """不足表格签名（<4 连续单元格行）的零散短行原样保留。"""
        text = "one two\n\nshort\n\n正常散文一行。\n"
        assert reassemble_flattened(text) == text

    def test_chinese_prose_never_touched(self):
        """中文条款正文（含短行）不进重组——中文不走剥平语料路径。"""
        text = "第四十七条\n董事会成员为奇数。\n\n第五条\n股东大会每年召开。\n"
        assert reassemble_flattened(text) == text

    def test_sentence_lines_not_cells(self):
        """句号收尾的散文句不算单元格（防止把散文段落拼进表格）。"""
        text = "This is a full sentence.\n\nAnother sentence ends here.\n\nok\n"
        assert reassemble_flattened(text) == text

    def test_long_run_wrapped_by_window(self):
        cells = "\n\n".join(f" cell{i} " for i in range(20))
        out = reassemble_flattened(cells)
        # 每行最多 8 个单元格；20 个单元格 → 3 行
        rows = [ln for ln in out.splitlines() if ln.startswith("|")]
        assert len(rows) == 3


class TestRenderMarkdownTable:
    def test_renders_header_and_separator(self):
        out = render_markdown_table([["Item", "Value"], ["Revenue", "100"]])
        assert out.splitlines()[0] == "| Item | Value |"
        assert out.splitlines()[1] == "|---|---|"
        assert out.splitlines()[2] == "| Revenue | 100 |"

    def test_pipe_in_cell_escaped(self):
        out = render_markdown_table([["A|B", "1"]])
        assert "A｜B" in out

    def test_ragged_rows_padded(self):
        out = render_markdown_table([["A", "B", "C"], ["x"]])
        assert out.splitlines()[2] == "| x |  |  |"

    def test_empty(self):
        assert render_markdown_table([]) == ""


# ── 查询同义扩展 ──


class TestExpandQuery:
    def test_revenue_expands_to_net_sales(self):
        out = expand_query("What was the total revenue for 2022?")
        assert "net sales" in out
        assert "total revenue" in out

    def test_no_hit_unchanged(self):
        assert expand_query("公司章程修订需要多少表决权？") == "公司章程修订需要多少表决权？"

    def test_word_boundary_no_false_hit(self):
        """eps 不得命中 epsilon（词边界纪律）。"""
        assert expand_query("epsilon decay schedule") == "epsilon decay schedule"

    def test_expansion_adds_not_dedupes_group(self):
        out = expand_query("net income and diluted eps")
        assert out.count("net income") == 1  # 原词不重复追加

    def test_cogs_abbreviation(self):
        out = expand_query("What were cogs in fiscal 2021?")
        assert "cost of goods sold" in out


# ── 英文字段通道 ──


class TestEnglishFieldChannel:
    def _doc(self) -> dict[str, str]:
        # 模拟 10-K 剥平后「单元格一行」的表块 + 一段同话题散文（分属两节）
        return {
            "DOC_10K": (
                "# DOC 10K\n\n"
                "## Item 8 Financial Statements\n\n"
                " Net sales \n\n 3,535.0 \n\n 2022 \n\n"
                " Total revenue \n\n 3,540.2 \n\n 2022 \n\n"
                " Share or Programs (2) \n\n 245.98 \n\n"
                "\n"
                "## MD&A\n\n"
                "The company discussed general business trends and outlook "
                "for the coming years across its segments.\n"
            )
        }

    def test_english_short_label_gets_field_tokens(self):
        chunks = chunk_docs(self._doc())
        assert len(chunks) == 2
        table_chunk = chunks[0]  # 前言块无正文被跳过，表格节是首块
        tokens = _field_tokens(table_chunk)
        # sales 经 -es 复数归一为 sal（查询侧同一把尺子，两侧对齐）
        assert "net" in tokens and "sal" in tokens
        assert "revenue" in tokens  # Total revenue 标签行进字段通道

    def test_prose_chunk_field_channel_lighter(self):
        chunks = chunk_docs(self._doc())
        prose = chunks[1]
        # 散文长行不进字段通道（无短标签行）
        assert _field_tokens(prose) == tokenize(prose.header)

    def test_table_chunk_outranks_prose_for_label_query(self):
        index = BM25Index(chunk_docs(self._doc()))
        hits = index.retrieve("total revenue", k=3)
        assert hits[0].text.startswith("Net sales") or "Total revenue" in hits[0].text

    def test_query_expansion_toggle(self):
        index = BM25Index(chunk_docs(self._doc()))
        global QUERY_EXPANSION
        old = QUERY_EXPANSION
        try:
            QUERY_EXPANSION = False
            assert index._scores("revenue") is not None
        finally:
            QUERY_EXPANSION = old
