"""M3.1 分层压缩 + 提交物（answer.csv 双 schema / 打包布局）。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from findata.finqa import fixture as fx
from findata.finqa.memory import _is_verbatim, run_memoryqa
from findata.finqa.qwen import CallRecord
from findata.finqa.submit import write_answer_csv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"


class TestIsVerbatim:
    def test_article_header_is_verbatim(self):
        assert _is_verbatim("第四十七条", "公司下列对外担保行为……")

    def test_digit_dense_is_verbatim(self):
        assert _is_verbatim("二、公司基本情况", "营业收入 803,964,958,000.00 元 777,102,455,000.00")

    def test_plain_narrative_not_verbatim(self):
        narrative = "本年度报告摘要来自年度报告全文，为全面了解公司经营成果与财务状况"
        assert not _is_verbatim("一、重要提示", narrative)


class _CaptureClient:
    """记录消息内容的假客户端（分层 e2e 用）。"""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, str]] = []  # (qid, purpose, user_text)

    def chat(self, qid, purpose, messages):
        self.calls.append((qid, purpose, messages[1]["content"]))
        reply = self.replies.get(purpose, "VERDICT: FALSE")
        usage = (100, 20) if purpose.startswith("memorize") else (50, 5)
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=usage[0], completion_tokens=usage[1],
        )


class TestLayeredMemory:
    def _load(self):
        return fx.load_questions(FIXTURE_ROOT), fx.load_docs(FIXTURE_ROOT)

    def test_verbatim_layer_reaches_judge_context(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")  # 锚 47 条（法条→逐字层）
        client = _CaptureClient({
            "memorize:1": "【036 · 说明】叙述事实",
            "judge:B": "依据【strict_csrc_035 · 第四十七条】。VERDICT: TRUE",
        })
        details, _, _, _ = run_memoryqa(
            [q], docs, client, k_pool=8, batch=8, layered=True
        )
        d = details[0]
        assert d["layered"] is True
        assert d["verbatim_count"] >= 1
        judge_msg = next(m for _q, p, m in client.calls if p == "judge:B")
        # 判定上下文必须含逐字层标记与法条原文，而非只有摘要
        assert "逐字保留" in judge_msg
        assert "第四十七条" in judge_msg
        # 摘要轮只喂叙述层：法条正文（非题干里的条款号字样）不进 memorize
        mem_msg = next(m for _q, p, m in client.calls if p == "memorize:1")
        assert "为资产负债率超过百分之七十的担保对象提供的担保" not in mem_msg

    def test_non_layered_keeps_old_behavior(self):
        questions, docs = self._load()
        q = next(x for x in questions if x.qid == "reg_s_002")
        client = _CaptureClient({
            "memorize:1": "【036 · 第六条】事实",
            "judge:B": "VERDICT: TRUE",
        })
        details, _, _, _ = run_memoryqa([q], docs, client, k_pool=4, batch=8)
        assert details[0]["layered"] is False
        assert details[0]["verbatim_count"] == 0


def _write_run_json(path: Path) -> None:
    payload = {
        "runs": {
            "isolate": {
                "report": {
                    "total": 2, "correct": 2, "accuracy": 1.0,
                    "prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330,
                },
                "details": [
                    {"qid": "q1", "pred": "AC", "prompt_tokens": 200, "completion_tokens": 20},
                    {"qid": "q2", "pred": "B", "prompt_tokens": 100, "completion_tokens": 10},
                ],
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestAnswerCsv:
    def test_5col_schema(self, tmp_path):
        run = tmp_path / "run.json"
        _write_run_json(run)
        out = tmp_path / "answer.csv"
        n = write_answer_csv(run, out, schema="5col")
        assert n == 2
        rows = list(csv.reader(out.open(encoding="utf-8")))
        assert rows[0] == ["qid", "answer", "prompt_tokens", "completion_tokens", "total_tokens"]
        # summary 紧跟表头（题面原文），token 取自真实台账
        assert rows[1] == ["summary", "", "300", "30", "330"]
        assert rows[2] == ["q1", "AC", "200", "20", "220"]
        assert rows[3] == ["q2", "B", "100", "10", "110"]

    def test_8col_schema(self, tmp_path):
        run = tmp_path / "run.json"
        _write_run_json(run)
        out = tmp_path / "answer.csv"
        write_answer_csv(run, out, schema="8col")
        rows = list(csv.reader(out.open(encoding="utf-8")))
        assert rows[0][0] == "qid" and rows[0][1] == "answer_1" and rows[0][4] == "answer_4"
        assert rows[1] == ["summary", "", "", "", "", "300", "30", "330"]
        assert rows[2] == ["q1", "AC", "", "", "", "200", "20", "220"]

    def test_bad_schema_rejected(self, tmp_path):
        run = tmp_path / "run.json"
        _write_run_json(run)
        import pytest

        with pytest.raises(ValueError, match="5col/8col"):
            write_answer_csv(run, tmp_path / "a.csv", schema="9col")
