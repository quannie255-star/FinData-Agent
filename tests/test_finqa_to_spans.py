"""finqa → economist 双层 trace 适配：schema 正确性与坏输入拒绝。"""

from __future__ import annotations

import json

import pytest
from scripts.finqa_to_spans import run_to_trace


def _run_data() -> dict:
    return {
        "model": "qwen-plus",
        "runs": {
            "isolate": {
                "report": {"accuracy": 1.0, "total_tokens": 330},
                "details": [
                    {"qid": "q1", "pred": "AC", "correct": True,
                     "prompt_tokens": 200, "completion_tokens": 20},
                    {"qid": "q2", "pred": "B", "correct": True,
                     "prompt_tokens": 100, "completion_tokens": 10},
                ],
            }
        },
    }


def test_run_and_span_records_per_question():
    runs, spans = run_to_trace(_run_data())
    assert len(runs) == 2 and len(spans) == 2
    r = runs[0]
    assert r["task"] == "q1" and r["final_answer"] == "AC"
    turn = r["turns"][0]
    assert turn["prompt_tokens"] == 200 and turn["completion_tokens"] == 20
    # 归档没记录的维度以 0 写入（报告据此声明未观测），不许编造
    assert turn["messages_chars"] == 0 and turn["tool_calls"] == []
    s = spans[0]
    assert s["name"] == "gen_ai.chat"
    a = s["attributes"]
    assert a["gen_ai.usage.input_tokens"] == 200
    assert a["gen_ai.usage.output_tokens"] == 20


def test_missing_runs_rejected():
    with pytest.raises(ValueError, match="runs"):
        run_to_trace({"model": "x"})


def test_output_loads_as_jsonl_and_matches_economist_dispatch(tmp_path):
    runs, spans = run_to_trace(_run_data())
    runs_path = tmp_path / "runs-x.jsonl"
    spans_path = tmp_path / "spans-x.jsonl"
    runs_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in runs), encoding="utf-8"
    )
    spans_path.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in spans), encoding="utf-8"
    )
    # economist 按**内容**分派：有 turns 的是 run，有 attributes 的是 span
    from findata.economist.trace import load_bundle

    bundle = load_bundle(tmp_path, tag="x")
    assert len(bundle.runs) == 2
    assert len(bundle.model_calls) == 2
    assert bundle.runs["q1"].prompt_tokens == 200
