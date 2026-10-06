"""M4 引用观测归因：抽取模式、在场匹配规则、使用率统计与 e2e 接线。"""

from __future__ import annotations

from pathlib import Path

from findata.finqa import fixture as fx
from findata.finqa.attribution import (
    extract_citations,
    match_citations,
    usage_stats,
)
from findata.finqa.isolate import run_isolate
from findata.finqa.memory import _facts_units, run_memoryqa
from findata.finqa.qwen import CallRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


class TestExtractCitations:
    def test_bracket_form(self):
        reply = "依据【strict_csrc_035 · 第八十二条】第（三）项。VERDICT: TRUE"
        assert extract_citations(reply) == ["strict_csrc_035 · 第八十二条"]

    def test_bracket_content_not_double_counted(self):
        # 括号内的条款号不再作为裸条款号重复抽取
        reply = "见【strict_csrc_036 · 第六条】"
        assert extract_citations(reply) == ["strict_csrc_036 · 第六条"]

    def test_bare_article(self):
        assert extract_citations("根据第四十七条判断") == ["第四十七条"]

    def test_numbered_section(self):
        assert "3.2" in extract_citations("条款 3.2 宽限期约定")

    def test_no_citation(self):
        assert extract_citations("无依据可引用") == []


class TestMatchCitations:
    def test_only_in_context_counts(self):
        units = [("strict_csrc_035", "第四十七条"), ("strict_csrc_035", "第八十条")]
        # 引用第八十条 → 命中第 2 个；引用不在场的第六条 → 不命中任何
        used = match_citations(["第八十条", "第六条"], units)
        assert used == {1}

    def test_bracket_with_spaces_normalized(self):
        units = [("strict_csrc_035", "第四十七条")]
        used = match_citations(["strict_csrc_035·第四十七条"], units)
        assert used == {0}

    def test_same_article_across_docs_all_matched(self):
        # 判定没义务报文档号：同名条款记到所有在场同号单元（宁多勿漏，口径已注）
        units = [("doc_a", "第六条"), ("doc_b", "第六条"), ("doc_b", "第七条")]
        assert match_citations(["第六条"], units) == {0, 1}


class TestUsageStats:
    def test_rank_and_waste(self):
        records = [
            {
                "qid": "q1",
                "fed": [("d", "一", 100), ("d", "二", 200), ("d", "三", 50)],
                "used_idx": {0, 2},
            },
        ]
        stats = usage_stats(records)
        assert stats["fed_units"] == 3
        assert stats["used_units"] == 2
        assert stats["usage_rate"] == round(2 / 3, 4)
        assert stats["wasted_chars"] == 200  # 只有第 2 位没被用
        assert stats["by_rank"][2] == {"used": 0, "fed": 1, "rate": 0.0}


class TestFactsUnits:
    def test_prefixed_lines_attributable(self):
        facts = "【036 · 第六条】独立性负面清单\n【035 · 第四十七条】担保须股东会\n无前缀的行"
        units, unattr = _facts_units(facts)
        assert [h for _, h, _ in units] == ["第六条", "第四十七条"]
        assert unattr == len("无前缀的行")


class _AttributionClient:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def chat(self, qid, purpose, messages):
        self.calls.append((qid, purpose))
        reply = self.replies.get(purpose, "VERDICT: FALSE")
        usage = (50, 5) if purpose.startswith("judge") else (100, 20)
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=usage[0], completion_tokens=usage[1],
        )


class TestE2EAttribution:
    def _load(self):
        return fx.load_questions(FIXTURE_ROOT), fx.load_docs(FIXTURE_ROOT)

    def test_isolate_records_fed_and_used(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")  # gold B，锚 47 条
        client = _AttributionClient({
            "judge:B": "依据【strict_csrc_035 · 第四十七条】，B 正确。VERDICT: TRUE",
        })
        details, _, _, _ = run_isolate([q], docs, client)
        d = details[0]
        assert d["pred"] == "B"
        b_attr = next(a for a in d["attributions"] if a["purpose"] == "judge:B")
        fed_headers = [h for _, h, _ in b_attr["fed"]]
        assert "第四十七条" in fed_headers  # 判定上下文里确实喂了 47 条
        used_idx = b_attr["used_idx"]
        assert any(
            fed_headers[i] == "第四十七条" for i in used_idx
        ), "回复引用了 47 条，使用记录必须命中它"
        # 回复存档（M3 发现的可观测性缺口）
        assert "第四十七条" in d["replies"]["judge:B"]

    def test_memory_records_line_usage(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")
        facts = "【strict_csrc_035 · 第四十七条】担保四情形须股东会\n【036 · 第六条】独立性清单"
        client = _AttributionClient({
            "memorize:1": facts,
            "judge:B": "依据第六条与【strict_csrc_035 · 第四十七条】。VERDICT: TRUE",
        })
        details, _, _, _ = run_memoryqa([q], docs, client, k_pool=4, batch=8)
        d = details[0]
        b_attr = next(a for a in d["attributions"] if a["purpose"] == "judge:B")
        # 两条事实行都被引用（一条裸条款号、一条括号头）
        assert set(b_attr["used_idx"]) == {0, 1}
        assert d["unattributable_chars"] == 0
