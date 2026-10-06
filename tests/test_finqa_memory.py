"""M3 记忆臂：滚动摘要约束、调用序列、压缩比与回退仲裁（零网络）。"""

from __future__ import annotations

from pathlib import Path

from findata.finqa import fixture as fx
from findata.finqa.memory import build_answer_from_memory, run_memoryqa, summarize_messages
from findata.finqa.qwen import CallRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


class TestSummarizeMessages:
    def test_constraints_preserved(self):
        messages = summarize_messages("问题", "旧事实", ["【片段】内容"], 400)
        user = messages[1]["content"]
        # 数字精确保留是硬约束——压缩丢数字就是丢证据
        assert "数值与单位" in user and "不得改写或省略" in user
        assert "条款号" in user
        assert "不超过 400 字" in user
        assert "旧事实" in user and "【片段】内容" in user


class TestAnswerFromMemory:
    def test_contains_facts_and_options(self):
        q = next(x for x in fx.load_questions(FIXTURE_ROOT) if x.qid == "reg_s_003")
        messages = build_answer_from_memory(q, "事实1\n事实2")
        user = messages[1]["content"]
        assert "事实1" in user and "ANSWER" in user and "A." in user


class _ScriptedClient:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def chat(self, qid, purpose, messages):
        self.calls.append((qid, purpose))
        reply = self.replies.get(purpose, "VERDICT: FALSE")
        usage = (100, 20) if purpose.startswith("memorize") else (50, 5)
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=usage[0], completion_tokens=usage[1],
        )


class TestRunMemoryqa:
    def _load(self):
        return fx.load_questions(FIXTURE_ROOT), fx.load_docs(FIXTURE_ROOT)

    def test_call_sequence_and_rounds(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")  # mcq, gold B
        client = _ScriptedClient({
            "memorize:1": "【036·第六条】事实A",
            "memorize:2": "【035·第四十七条】事实B",
            "memorize:3": "【035·第八十条】事实C",
            "judge:B": "VERDICT: TRUE",
        })
        details, ledger, report, skipped = run_memoryqa([q], docs, client, k_pool=10, batch=4)
        assert skipped == []
        purposes = [p for _, p in client.calls]
        # 证据池 10 片段、每轮 4 片 → 3 轮摘要 + 4 次判定，判定在摘要之后
        assert purposes[:3] == ["memorize:1", "memorize:2", "memorize:3"]
        assert purposes[3:] == ["judge:A", "judge:B", "judge:C", "judge:D"]
        assert details[0]["pred"] == "B"
        assert details[0]["used_fallback"] is False
        assert details[0]["memory_rounds"] == 3

    def test_compression_ratio_recorded(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")
        facts = "【036·第六条】事实A"  # 12 字
        client = _ScriptedClient({"memorize:1": facts, "judge:B": "VERDICT: TRUE"})
        details, _, _, _ = run_memoryqa([q], docs, client, k_pool=4, batch=8)
        d = details[0]
        assert d["memory_chars"] == len(facts)
        assert d["compression_ratio"] == round(d["pool_chars"] / len(facts), 2)

    def test_fallback_when_no_true(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_005")  # multi, gold AB
        client = _ScriptedClient({
            "memorize:1": "事实",
            "answer": "综合记忆判断。\nANSWER: AB",
        })
        details, ledger, _report, _ = run_memoryqa([q], docs, client, k_pool=4, batch=8)
        assert details[0]["used_fallback"] is True
        assert details[0]["pred"] == "AB"
        assert client.calls[-1][1] == "answer"
        # 台账覆盖全过程：摘要 + 判定 + 回退
        assert len(ledger.records) == len(client.calls)

    def test_tf_answers_directly_from_memory(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_003")  # tf
        client = _ScriptedClient({
            "memorize:1": "事实",
            "answer": "ANSWER: A",
        })
        details, _, _, _ = run_memoryqa([q], docs, client, k_pool=4, batch=8)
        purposes = [p for _, p in client.calls]
        assert purposes == ["memorize:1", "answer"]
        assert details[0]["pred"] == "A"
