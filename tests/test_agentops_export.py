"""样本集落盘测试：钉住「报告说的数」与「落盘的样本」是同一份。

分开存一份对照表就会漂移——改了判定忘了同步，样本和结论对不上号，而
这种错在训练完之前**不会有任何报错**。所以判定必须挂在样本上一起走。
"""

from __future__ import annotations

import json

from findata.agentops.adapters.xlam import to_training_example
from findata.agentops.export import VERDICT_FILES, SplitWriter, iter_jsonl
from findata.agentops.triage import (
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    Diagnosis,
)


def _diag(cat: str, sev: str = "loud") -> Diagnosis:
    return Diagnosis("r1", cat, sev, ["证据 A"], "建议 B")


def test_four_verdicts_land_in_four_files(tmp_path):
    with SplitWriter(tmp_path) as w:
        for v, n in ((V_POSITIVE, 3), (V_REVIEW, 2), (V_DISCARD, 1), (V_NOT_FAILURE, 4)):
            for _ in range(n):
                w.write(v, {"id": v})
        counts = dict(w.counts)
        m = json.loads(w.write_manifest(w.manifest(10, 20, "test-source")).read_text("utf-8"))

    assert counts == {V_POSITIVE: 3, V_REVIEW: 2, V_DISCARD: 1, V_NOT_FAILURE: 4}
    for v, name in VERDICT_FILES.items():
        rows = list(iter_jsonl(tmp_path / name))
        assert len(rows) == counts[v], f"{name} 条数不符"
        assert all(r["id"] == v for r in rows), f"{name} 混进了别的处置"
    assert m["n_samples"] == 10


def test_verdict_is_attached_to_the_sample_not_kept_separately(tmp_path):
    """判定随样本走：下游拿到样本就知道为什么留/为什么丢。"""
    with SplitWriter(tmp_path) as w:
        obj = w.attach({"id": "x"}, _diag("schema_contradiction"), V_DISCARD)
        w.write(V_DISCARD, obj)
    row = next(iter(iter_jsonl(tmp_path / "discard.jsonl")))
    assert row["findata"]["verdict"] == V_DISCARD
    assert row["findata"]["root_cause"] == "schema_contradiction"
    assert row["findata"]["evidence"] == ["证据 A"]
    assert row["findata"]["suggestion"] == "建议 B"


def test_manifest_carries_the_two_easy_to_get_wrong_caveats(tmp_path):
    """口径说明写进产物本身——半年后回头看，不靠人记忆。"""
    with SplitWriter(tmp_path) as w:
        m = w.write_manifest(w.manifest(0, 0, "src"))
    note = json.loads(m.read_text(encoding="utf-8"))["note"]
    assert "not_failure" in note and "不是负样本" in note
    assert "schema_contradiction" in note


def test_unknown_verdict_is_rejected(tmp_path):
    with SplitWriter(tmp_path) as w:
        try:
            w.write("瞎写的处置", {"id": 1})
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("未知处置必须报错，不能静默丢进某个文件")


def test_training_example_is_openai_shaped():
    """arguments 在 OpenAI 协议里是 **JSON 字符串**，不是对象。

    这个细节错了，下游加载会整批失败——而且失败在训练脚本里，很难回溯到
    是我们导出时写错的。
    """
    rec = {
        "id": "abc",
        "query": "roll a die",
        "tools": [{"name": "roll", "description": "", "parameters": {}}],
        "answers": [{"name": "roll", "arguments": {"sides": 6}}],
    }
    ex = to_training_example(rec, 7)
    assert ex["id"] == "abc"
    assert ex["row"] == 7
    assert ex["messages"][0] == {"role": "user", "content": "roll a die"}
    call = ex["messages"][1]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "roll"
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"sides": 6}
