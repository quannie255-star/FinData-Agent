"""M2.2 选项级证据隔离：三态抽取、确定性拼装、回退仲裁与台账形态。"""

from __future__ import annotations

from pathlib import Path

from findata.finqa import fixture as fx
from findata.finqa.isolate import (
    assemble_mcq,
    assemble_multi,
    build_judge_messages,
    extract_verdict,
    run_isolate,
)
from findata.finqa.qwen import CallRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


class TestExtractVerdict:
    def test_simple(self):
        assert extract_verdict("依据82条（三）。\nVERDICT: TRUE") == "TRUE"

    def test_chinese_colon_and_case(self):
        assert extract_verdict("VERDICT：false") == "FALSE"
        assert extract_verdict("verdict: Uncertain") == "UNCERTAIN"

    def test_last_occurrence_wins(self):
        assert extract_verdict("VERDICT: TRUE\n再想\nVERDICT: FALSE") == "FALSE"

    def test_garbage_returns_empty(self):
        assert extract_verdict("我认为正确") == ""


class TestAssemble:
    def test_multi_takes_all_true_only(self):
        # UNCERTAIN 不入选——保守，防 v2.1 的过包含
        assert assemble_multi({"A": "TRUE", "B": "FALSE", "C": "TRUE", "D": "UNCERTAIN"}) == "AC"

    def test_multi_empty_when_no_true(self):
        assert assemble_multi({"A": "FALSE", "B": "UNCERTAIN", "C": "FALSE", "D": ""}) == ""

    def test_mcq_single_true(self):
        assert assemble_mcq({"A": "FALSE", "B": "TRUE", "C": "UNCERTAIN", "D": "FALSE"}) == "B"

    def test_mcq_ambiguous_returns_none(self):
        assert assemble_mcq({"A": "TRUE", "B": "TRUE", "C": "FALSE", "D": "FALSE"}) is None
        assert assemble_mcq({"A": "FALSE", "B": "UNCERTAIN", "C": "FALSE", "D": "FALSE"}) is None


class TestJudgeMessages:
    def test_contains_statement_and_rules(self):
        messages = build_judge_messages(
            "背景问题", "D", "陈述内容", "【片段】……", "多选题"
        )
        user = messages[1]["content"]
        assert "选项D：陈述内容" in user
        assert "VERDICT: UNCERTAIN" in user
        assert "背景问题" in user

    def test_false_covers_true_but_out_of_scope(self):
        # M2.3 语义：FALSE 必须明确涵盖「陈述虽真实但不属于题目所问」
        messages = build_judge_messages("下列属于不得担任情形", "C", "每年自查", "片段", "多选题")
        user = messages[1]["content"]
        assert "不属于题目所问" in user
        assert "应作为本题正确答案的一部分被选入" in user


class _ScriptedClient:
    """按 (qid, purpose) 脚本化回复的假客户端。"""

    def __init__(self, replies: dict[tuple[str, str], str]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def chat(self, qid, purpose, messages):
        self.calls.append((qid, purpose))
        # 默认 FALSE：不触发复审路径，让各用例聚焦自己的断言
        reply = self.replies.get((qid, purpose), "依据不足。\nVERDICT: FALSE")
        usage = (50, 5) if purpose.startswith("judge") else (80, 8)
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=usage[0], completion_tokens=usage[1],
        )


class TestRunIsolate:
    def _load(self):
        return fx.load_questions(FIXTURE_ROOT), fx.load_docs(FIXTURE_ROOT)

    def test_mcq_single_true_no_fallback(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")  # mcq, gold B
        client = _ScriptedClient({
            ("reg_s_002", "judge:B"): "依据47条（四）。\nVERDICT: TRUE",
        })
        details, ledger, report, skipped = run_isolate([q], docs, client)
        assert skipped == []
        assert details[0]["pred"] == "B"
        assert details[0]["used_fallback"] is False
        # 每选项一次判定调用，无仲裁调用
        assert [p for _, p in client.calls] == ["judge:A", "judge:B", "judge:C", "judge:D"]

    def test_multi_fallback_when_no_true(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_005")  # multi, gold AB
        client = _ScriptedClient({
            ("reg_s_005", "answer"): "依据不足，综合判断。\nANSWER: AB",
        })
        details, ledger, report, skipped = run_isolate([q], docs, client)
        assert details[0]["used_fallback"] is True
        assert details[0]["pred"] == "AB"
        assert client.calls[-1] == ("reg_s_005", "answer")

    def test_tf_skips_judge_and_answers_directly(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_003")  # tf, gold A
        client = _ScriptedClient({
            ("reg_s_003", "answer"): "依据82条（三）。\nANSWER: A",
        })
        details, _, _, _ = run_isolate([q], docs, client)
        # tf 选项无信息量：不逐选项判定，直接单次作答
        assert [p for _, p in client.calls] == ["answer"]
        assert details[0]["pred"] == "A"

    def test_uncertain_retry_flips_verdict(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")  # mcq, gold B
        client = _ScriptedClient({
            ("reg_s_002", "judge:A"): "证据不足。\nVERDICT: UNCERTAIN",
            ("reg_s_002", "judge:A:retry"): "复审加宽证据后：47 条不支持 A。\nVERDICT: FALSE",
            ("reg_s_002", "judge:B"): "VERDICT: TRUE",
        })
        details, ledger, _report, _ = run_isolate([q], docs, client)
        assert details[0]["pred"] == "B"
        assert details[0]["retried"] == {"A": True, "B": False, "C": False, "D": False}
        # 复审调用进台账（token 统计覆盖全过程，题面要求）
        assert ("reg_s_002", "judge:A:retry") in client.calls
        p, c, _t = ledger.totals()
        assert p == 5 * 50 and c == 5 * 5

    def test_ledger_covers_all_calls(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")
        client = _ScriptedClient({
            ("reg_s_002", "judge:B"): "VERDICT: TRUE",
        })
        details, ledger, _report, _ = run_isolate([q], docs, client)
        p, c, t = ledger.totals()
        assert p == 4 * 50 and c == 4 * 5
        assert details[0]["prompt_tokens"] == p
