"""钉死**原生字段级记录**的口径（R5.1 的前置）。

R5.1 之前，字段级归因靠**重放**工具：拿 trace 里记的参数把工具再跑一遍，
字符数对上才准进统计。那个办法对本项目有效（工具是对本地语料的确定性只读），
但对真实用户**不成立**——他们的工具来自第三方 MCP server，可能有副作用、
可能非确定性、可能需要鉴权。

所以这一层要换成"调用发生时原生记下来"。测试要钉的就是**这次替换没有偷偷
改变判定口径**，以及**它确实不再依赖重放**。
"""

from __future__ import annotations

import json

import pytest

from findata.contextbudget import attribution
from findata.contextbudget.fields import (
    extract_blocks,
    record_fields,
    segment_kind,
    segment_payload,
)
from findata.contextbudget.tools import call_tool, set_corpus_root

PAYLOAD_TWO_BLOCKS = (
    "def alpha():\n    return calculate_electric_field\n\n\ndef beta():\n    pass\n"
)


# --------------------------------------------------------------------------
# 记录到底记了什么
# --------------------------------------------------------------------------


def test_records_have_name_chars_and_tokens_but_no_raw_text() -> None:
    """每条记录必须有名字、字符数、指纹；**指纹里不许出现原文**。"""
    records = record_fields("read_file", f"```python\n{PAYLOAD_TWO_BLOCKS}\n```")
    assert [r.name for r in records] == ["block:alpha", "block:beta"]
    assert [r.kind for r in records] == ["block", "block"]
    assert all(r.chars > 0 for r in records)
    # 指纹是标识符，不是行原文
    assert "calculate_electric_field" in records[0].tokens
    assert all("\n" not in t for r in records for t in r.tokens)
    # 落盘形态是可 JSON 化的（要进 trace）
    blob = json.dumps([r.to_dict() for r in records], ensure_ascii=False)
    assert "calculate_electric_field" in blob


def test_before_text_is_subtracted_at_record_time() -> None:
    """**已在上下文里的值必须当场扣掉**——否则引用率虚高。

    这是原生记录最容易做错的一步：它只发生一次，事后没法补。
    """
    payload = f"```python\n{PAYLOAD_TWO_BLOCKS}\n```"
    clean = record_fields("read_file", payload, before_text="")
    dirty = record_fields(
        "read_file", payload, before_text="刚才你已经读到 calculate_electric_field 了"
    )
    assert "calculate_electric_field" in clean[0].tokens
    assert "calculate_electric_field" not in dirty[0].tokens
    assert dirty[0].n_preexisting >= 1


def test_empty_before_text_keeps_the_old_optimistic_behaviour() -> None:
    """不传 `before_text` 不是报错，但**方向是引用率偏高**，必须能被看出来。"""
    payload = f"```python\n{PAYLOAD_TWO_BLOCKS}\n```"
    optimistic = record_fields("read_file", payload, before_text="")
    assert optimistic[0].n_preexisting == 0  # 什么都没扣 ⇒ 可区分值偏多


@pytest.mark.parametrize("result_code", ["error", "not_found", "not_implemented"])
def test_failed_returns_record_no_fields(result_code: str) -> None:
    """报错的结果没有结构可切 ⇒ 空表 = **结构未观测**，不是"没字段所以能删"。"""
    assert record_fields("list_files", "No files under 'x'.", result_code=result_code) == ()


def test_unknown_tool_records_nothing() -> None:
    """没见过的工具**不猜结构**。"""
    assert record_fields("some_tool_we_never_saw", "whatever payload") == ()


def test_truncation_is_declared() -> None:
    """指纹超上限时截断，**并且必须报出来**（否则 n_candidates 会被当成全量）。"""
    big = " ".join(f"symbol_{i:04d}" for i in range(attribution.MAX_FIELD_TOKENS + 50))
    records = record_fields("read_file", f"```python\ndef big():\n    {big}\n```")
    assert records and records[0].truncated is True
    assert records[0].n_candidates == attribution.MAX_FIELD_TOKENS


# --------------------------------------------------------------------------
# 最关键的一条：两条路径的判定口径必须同源
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("我决定调用 calculate_electric_field 来验证", attribution.STATUS_REFERENCED),
        ("这里什么都没提到，只有一段无关的话。", attribution.STATUS_NOT_REFERENCED),
        ("", attribution.STATUS_NOT_REFERENCED),
    ],
)
def test_native_and_replay_agree(after: str, expected: str) -> None:
    """同一份内容，原生记录与重放路径必须给出**同一个判定**。

    这是本次替换的准入条件。两条路各维护一份逻辑的话，同一个 trace 会算出
    两个数——而且没人会发现，因为两边看起来都"有实现"。
    """
    payload = f"```python\n{PAYLOAD_TWO_BLOCKS}\n```"
    before = "system\n任务：看看有多少个函数"

    # 原生路径
    rec = next(r for r in record_fields("read_file", payload, before_text=before)
               if r.name == "block:alpha")
    native = attribution.judge_record(rec, after=after)

    # 重放路径
    fv = next(f for f in segment_payload("read_file", payload) if f.name == "block:alpha")
    replay = attribution.judge_field(fv, before=before, after=after)

    assert native.status == replay.status == expected
    assert native.n_matched == replay.n_matched


def test_storage_cap_does_not_change_the_replay_judgement() -> None:
    """**上限是存储约束，不是判定约束。**

    重放路径手里有原文、没有存储压力；若让它也吃 120 的截断，v0.1 已经发布的
    那些读数会被静默改写（同一份 trace 算出两个数）。
    """
    big = " ".join(f"symbol_{i:04d}" for i in range(300))
    fv = attribution.FieldValue("content", big)
    verdict = attribution.judge_field(fv, before="", after="只用 symbol_0200")
    assert verdict.n_candidates >= 300, "重放路径不该被存储上限截断"


# --------------------------------------------------------------------------
# 与工具层的接线
# --------------------------------------------------------------------------


def test_call_tool_fills_fields(tmp_path) -> None:
    """`call_tool` 是唯一入口，它负责补字段记录。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "badge.md").write_text("# badge\n", encoding="utf-8")
    (tmp_path / "docs" / "roadmap.md").write_text("# roadmap\n", encoding="utf-8")
    set_corpus_root(tmp_path)
    try:
        result = call_tool("list_files", {"prefix": "docs"}, before_text="")
        assert result.result_code == "ok"
        assert [f.name for f in result.fields]
        assert all(f.kind == "path" for f in result.fields)
    finally:
        set_corpus_root(None)


def test_call_tool_leaves_no_fields_for_silent_tools() -> None:
    """沉默工具如实回 not_implemented ⇒ 字段表为空（未观测）。"""
    result = call_tool("findata_trust_board", {})
    assert result.result_code == "not_implemented"
    assert result.fields == ()


# --------------------------------------------------------------------------
# 从 tools.py 搬过来的切段逻辑：别把搬迁搞成行为变化
# --------------------------------------------------------------------------


def test_extract_blocks_still_available_from_tools() -> None:
    """`extract_blocks` 搬到 fields.py，但**旧 import 路径必须继续有效**。"""
    from findata.contextbudget.tools import extract_blocks as via_tools

    assert via_tools is extract_blocks
    names = [b[0] for b in extract_blocks(PAYLOAD_TWO_BLOCKS)]
    assert names == ["alpha", "beta"]


def test_segment_kind_reads_the_prefix() -> None:
    assert segment_kind("block:foo") == "block"
    assert segment_kind("path:src/a.py") == "path"
    assert segment_kind("nocolon") == "other"
