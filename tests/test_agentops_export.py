"""样本集落盘测试：钉住「报告说的数」与「落盘的样本」是同一份。

分开存一份对照表就会漂移——改了判定忘了同步，样本和结论对不上号，而
这种错在训练完之前**不会有任何报错**。所以判定必须挂在样本上一起走。
"""

from __future__ import annotations

import json

from findata.agentops.adapters.xlam import to_training_example
from findata.agentops.export import (
    DEFECT_MISPLACED,
    DEFECT_UNDECLARED,
    VERDICT_FILES,
    SplitWriter,
    ToolDefectTally,
    iter_jsonl,
)
from findata.agentops.triage import (
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    V_SCHEMA_DEFECT,
    Diagnosis,
)


def _diag(cat: str, sev: str = "loud") -> Diagnosis:
    return Diagnosis("r1", cat, sev, ["证据 A"], "建议 B")


def test_four_verdicts_land_in_four_files(tmp_path):
    buckets = ((V_POSITIVE, 3), (V_REVIEW, 2), (V_DISCARD, 1), (V_NOT_FAILURE, 4))
    with SplitWriter(tmp_path) as w:
        for v, n in buckets:
            for _ in range(n):
                w.write(v, {"id": v})
        counts = dict(w.counts)
        m = json.loads(w.write_manifest(w.manifest(10, 20, "test-source")).read_text("utf-8"))

    assert counts[V_POSITIVE] == 3 and counts[V_SCHEMA_DEFECT] == 0
    for v, name in VERDICT_FILES.items():
        rows = list(iter_jsonl(tmp_path / name))
        assert len(rows) == counts[v], f"{name} 条数不符"
        assert all(r["id"] == v for r in rows), f"{name} 混进了别的处置"
    assert m["n_samples"] == 10


def test_tool_defect_tally_gives_an_actionable_list_not_a_sample_list(tmp_path):
    """同一份事实两种表述：几百条样本 vs 十几个工具。

    前者没人会看（等于没有复核队列），后者是可执行动作。这是"根因与处置
    分离"的最终落点——根因在数据源，处置就该作用在数据源上。
    """
    t = ToolDefectTally()
    for i in range(5):
        t.add("calculate_electric_field", "permitivity", "schema contradiction: ...", i)
    t.add("dice_roll_probability", "num_faces", "schema contradiction: ...", 9)
    p = t.write(tmp_path, "src")
    d = json.loads(p.read_text(encoding="utf-8"))

    assert d["n_tools"] == 2 and d["n_params"] == 2
    assert d["defects"][0]["tool"] == "calculate_electric_field"
    assert d["defects"][0]["hits"] == 5
    assert d["defects"][0]["sample_rows"] == [0, 1, 2], "只留几行出处，别把清单撑成第二个数据集"
    assert "不要丢样本" in d["note"]


def test_sample_rows_are_deduplicated(tmp_path):
    """同一条轨迹调同一工具两次，出处行不能记两遍。

    不去重就会出现 `[65, 65, 218]`——看起来三条出处，其实是两条轨迹。
    清单是给人照着复核的，出处行说谎比没有出处更糟。
    """
    t = ToolDefectTally()
    for row in (65, 65, 218, 65, 1061):
        t.add("a", "p", "schema contradiction: ...", row)
    d = t.to_dict("src")
    assert d["defects"][0]["sample_rows"] == [65, 218, 1061]
    assert d["defects"][0]["hits"] == 5, "hits 仍是命中次数，不因去重而少算"


def test_two_defect_kinds_get_two_different_actions(tmp_path):
    """「漏了 default」和「default 放错位置」改法不同，不能合成一句。

    合成一句「补上 default」，数据源方会把 `charge` 和 `permitivity` **同时**
    标上 8.854e-12——错得更彻底。这两类必须分开报。
    """
    t = ToolDefectTally()
    t.add("a", "p", "schema contradiction: p 的 description 称有默认值，schema 未标 default", 0)
    t.add(
        "b",
        "q",
        "schema contradiction: q 的 description 称默认值 8.854e-12，"
        "但该值被标在 r 上（q 自己没标 default）",
        1,
    )
    d = t.to_dict("src")

    assert d["n_by_kind"] == {DEFECT_UNDECLARED: 1, DEFECT_MISPLACED: 1}
    assert "补上" in d["action"][DEFECT_UNDECLARED]
    assert "移" in d["action"][DEFECT_MISPLACED]
    kinds = {e["param"]: e["defect_kind"] for e in d["defects"]}
    assert kinds == {"p": DEFECT_UNDECLARED, "q": DEFECT_MISPLACED}
    # 两类都要动数据源、都不丢样本——这是共同点，不能因为分了类就丢掉
    assert all("丢" not in v for v in d["action"].values())


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


def test_manifest_carries_the_three_easy_to_get_wrong_caveats(tmp_path):
    """口径说明写进产物本身——半年后回头看，不靠人记忆。"""
    with SplitWriter(tmp_path) as w:
        m = w.write_manifest(w.manifest(0, 0, "src"))
    note = json.loads(m.read_text(encoding="utf-8"))["note"]
    assert "not_failure" in note and "不是负样本" in note
    assert "schema_defects" in note and "不该作废" in note
    # 最容易犯的错：把"没查出问题"说成"确认正确"
    assert "没查出问题" in note and "确认正确" in note


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
