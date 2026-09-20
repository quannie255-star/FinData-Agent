"""`findata-context-economist` 的测试。

**这些测试钉的是"口径"，不是"功能"。** 每一例都对应一个会让报告里的数字
悄悄变错的地方，而不是"函数能不能跑通"：

- 重建对账不通过时，那次调用必须**退出**字段级统计（否则报告会基于一份
  "重建出来的、当时并不存在的内容"下结论，而读者无从分辨）
- `before` / `after` 的边界（同轮兄弟调用互不可见）
- 节省量的乘数是「剩余轮数」而不是 1（错了就少报一个数量级）
- `--usd-per-mtok` 缺省时**不产生**金额字段（不给假精确）
- 两臂对照必须按任务配对（把"少跑了一个任务"记成"变便宜了"是典型错法）
- 判分器覆盖不全时必须暴露覆盖数，而不是把未判分的当失败

**写这批测试时的一个教训**：第一版有 4 例红了，全部是**测试写错**而不是代码错
（期望的 payload 少了一个换行、造的数据块没到大段阈值、以及两例没构造出提案
却去断言提案文案）。这本身值得记下来：断言写错时，第一反应必须是回去核对
被测实现的真实行为，而不是把实现改成符合自己想象的样子。
"""

from __future__ import annotations

import json

import pytest

from findata.economist.analyze import (
    GRADER_KEYWORD,
    GRADER_NONE,
    CharsToTokens,
    analyze,
    reconstruct_calls,
    segment_kind,
    segment_payload,
    verify,
)
from findata.economist.report import render, render_json
from findata.economist.trace import RunObs, TraceBundle, TurnObs, load_bundle

# --------------------------------------------------------------------------
# 造 trace 的小工具（写盘走 write_bytes，避免 \n 被翻成 \r\n 影响断言）
# --------------------------------------------------------------------------


def make_run(
    task: str,
    *,
    turns: list[dict],
    final_answer: str = "",
    stopped_reason: str = "answered",
) -> dict:
    return {
        "task": task,
        "final_answer": final_answer,
        "stopped_reason": stopped_reason,
        "turns": turns,
        "error": "",
    }


def make_turn(turn: int, calls: list[dict], *, ptok: int = 100, chars: int = 300) -> dict:
    return {
        "turn": turn,
        "prompt_tokens": ptok,
        "completion_tokens": 10,
        "messages_chars": chars,
        "tools_chars": 500,
        "duration_ms": 1.0,
        "tool_calls": calls,
    }


def make_call(
    name: str, arguments: dict, *, payload_chars: int, code: str = "ok", turn: int = 1, idx: int = 1
) -> dict:
    return {
        "name": name,
        "call_id": f"c{turn}_{idx}",
        "arguments": arguments,
        "result_code": code,
        "payload_chars": payload_chars,
        "duration_ms": 1.0,
        "parse_error": "",
    }


def read_file_payload(body: str) -> str:
    """`tools.read_file` 的真实返回形状（**末尾那个换行是它加的，别漏**）。"""
    return f"```python\n{body}\n```"


def write_trace(tmp_path, name: str, runs: list[dict]) -> TraceBundle:
    path = tmp_path / f"runs-{name}.jsonl"
    blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in runs) + "\n"
    path.write_bytes(blob.encode("utf-8"))
    return load_bundle(path, label=name)


def _big_tool_list() -> list[dict]:
    return [
        {"type": "function", "function": {"name": f"tool_{i}", "description": "d" * 40}}
        for i in range(20)
    ]


def _small_tool_list() -> list[dict]:
    return _big_tool_list()[:4]


def trace_with_definition_lever(tmp_path, name: str) -> TraceBundle:
    """造一份能触发「定义侧裁撤」提案的 trace（清单里有未被调用的工具）。"""
    big = _big_tool_list()
    turn = make_turn(1, [make_call("tool_0", {}, payload_chars=5)])
    turn["tools_chars"] = len(json.dumps(big, ensure_ascii=False))
    return write_trace(tmp_path, name, [make_run("任务甲", turns=[turn])])


# --------------------------------------------------------------------------
# 段切分：结构未知就不猜
# --------------------------------------------------------------------------


def test_read_file_segments_are_top_level_blocks() -> None:
    payload = "```python\ndef alpha():\n    pass\n\nclass Beta:\n    pass\n```"
    names = [fv.name for fv in segment_payload("read_file", payload)]
    assert names == ["block:alpha", "block:Beta"]
    assert all(segment_kind(n) == "block" for n in names)


def test_unknown_shape_returns_no_segments_instead_of_guessing() -> None:
    """`read_file` 的失败返回没有块结构 ⇒ 空表（读作"结构未观测"，不是"可删"）。"""
    assert segment_payload("read_file", "File not found: foo.py") == []
    assert segment_payload("get_signature", "File not found: foo.py") == []
    assert segment_payload("some_tool_we_never_saw", "whatever payload") == []


def test_list_files_segments_are_paths() -> None:
    segments = segment_payload("list_files", "src/a.py\nsrc/b.py\n")
    assert [fv.name for fv in segments] == ["path:src/a.py", "path:src/b.py"]


def test_markdown_without_code_blocks_falls_back_to_headings() -> None:
    payload = "```\n# 标题一\n正文甲\n\n## 标题二\n正文乙\n```"
    names = [fv.name for fv in segment_payload("read_file", payload)]
    assert names == ["section:标题一", "section:标题二"]


# --------------------------------------------------------------------------
# 重建对账：不通过就退出统计
# --------------------------------------------------------------------------


def test_reconstruction_gate_passes_on_an_exact_replay(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = "def alpha():\n    pass\n"
    (corpus / "a.py").write_bytes(body.encode("utf-8"))
    payload = read_file_payload(body)

    runs = [
        make_run(
            "任务甲",
            turns=[
                make_turn(
                    1,
                    [make_call("read_file", {"path": "a.py"}, payload_chars=len(payload))],
                )
            ],
        )
    ]
    bundle = write_trace(tmp_path, "gate", runs)
    contents, report = reconstruct_calls(bundle, corpus)
    assert report.n_ok == 1
    assert report.n_mismatch == 0
    assert len(contents) == 1


def test_reconstruction_gate_drops_calls_whose_replay_disagrees(tmp_path) -> None:
    """记录值与重放值差 1 个字符也必须判为对不上，并让该调用退出字段级统计。

    这是整个工具最关键的闸门：没有它，报告会基于一份"重建出来的、
    当时并不存在的内容"下结论，而报告读者无从分辨。
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = "def alpha():\n    pass\n"
    (corpus / "a.py").write_bytes(body.encode("utf-8"))
    payload = read_file_payload(body)

    runs = [
        make_run(
            "任务甲",
            turns=[
                make_turn(
                    1,
                    [
                        make_call(
                            "read_file", {"path": "a.py"}, payload_chars=len(payload) + 1
                        )
                    ],
                )
            ],
        )
    ]
    bundle = write_trace(tmp_path, "gate2", runs)
    contents, report = reconstruct_calls(bundle, corpus)
    assert report.n_ok == 0
    assert report.n_mismatch == 1
    assert contents == {}


def test_no_corpus_root_means_field_level_is_unobserved_not_zero(tmp_path) -> None:
    runs = [
        make_run(
            "任务甲",
            turns=[make_turn(1, [make_call("read_file", {"path": "a.py"}, payload_chars=10)])],
        )
    ]
    bundle = write_trace(tmp_path, "noroot", runs)
    contents, report = reconstruct_calls(bundle, None)
    assert contents == {}
    assert report.n_skipped_no_root == 1
    assert report.n_ok == 0
    analysis = analyze(bundle, corpus_root=None)
    assert analysis.verdicts == []
    assert "未观测" in render(analysis)


# --------------------------------------------------------------------------
# before / after 的边界
# --------------------------------------------------------------------------


def test_value_used_by_a_later_tool_call_counts_as_referenced(tmp_path) -> None:
    """list_files 返回的路径出现在后续 read_file 的参数里 —— 最硬的那条证据。"""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = "def alpha():\n    return 1\n"
    (corpus / "module_x.py").write_bytes(body.encode("utf-8"))
    listing = "module_x.py"
    read_payload = read_file_payload(body)

    runs = [
        make_run(
            "有哪些文件？",
            turns=[
                make_turn(1, [make_call("list_files", {}, payload_chars=len(listing))]),
                make_turn(
                    2,
                    [
                        make_call(
                            "read_file",
                            {"path": "module_x.py"},
                            payload_chars=len(read_payload),
                            turn=2,
                        )
                    ],
                ),
                make_turn(3, []),
            ],
            final_answer="有 module_x.py",
        )
    ]
    bundle = write_trace(tmp_path, "flow", runs)
    analysis = analyze(bundle, corpus_root=corpus)

    flows = {(f.from_tool, f.to_tool) for f in analysis.flows.values()}
    assert ("list_files", "read_file") in flows
    assert "module_x.py" in analysis.flows[("list_files", "read_file")].samples


def test_sibling_calls_in_the_same_turn_cannot_see_each_other(tmp_path) -> None:
    """同一轮里并发发出的两个调用，A 的返回不能算作 B 的 `before`。

    模型是在同一条消息里同时发出它们的，所以 B 的参数不可能"因为看到 A 的返回"
    而含有 A 的值。判错方向：把同轮兄弟算进 before 会让引用率**虚低**。
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body_one = "def alpha():\n    pass\n"
    body_two = "def beta():\n    pass\n"
    (corpus / "one.py").write_bytes(body_one.encode("utf-8"))
    (corpus / "two.py").write_bytes(body_two.encode("utf-8"))
    p1 = read_file_payload(body_one)
    p2 = read_file_payload(body_two)

    runs = [
        make_run(
            "同时读两个文件",
            turns=[
                make_turn(
                    1,
                    [
                        make_call("read_file", {"path": "one.py"}, payload_chars=len(p1)),
                        make_call("read_file", {"path": "two.py"}, payload_chars=len(p2), idx=2),
                    ],
                ),
                make_turn(2, []),
            ],
            final_answer="",
        )
    ]
    bundle = write_trace(tmp_path, "siblings", runs)
    analysis = analyze(bundle, corpus_root=corpus)
    # 同轮兄弟的返回不进 before ⇒ 不该出现"可区分值此前已在上下文里"这类判定
    assert not any("已在上下文里" in v.reason for v in analysis.verdicts)


def test_saving_multiplier_is_remaining_turns_not_one(tmp_path) -> None:
    """第 2 轮产生的段，在 5 轮任务里要被重发 3 次 ⇒ 乘数 = 剩余轮数。

    乘数写成 1 会把节省量少报一个数量级，而"省了多少"正是本工具的价值主张。
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # 造 5 个足够大的函数块（每块 ≥200 字符，才进"大段"的候选）
    big_body = "".join(
        f"def unused_func_{i}():\n"
        + "".join(f"    variable_{j} = {j}\n" for j in range(24))
        + "\n"
        for i in range(5)
    )
    (corpus / "big.py").write_bytes(big_body.encode("utf-8"))
    payload = read_file_payload(big_body)

    runs = [
        make_run(
            "读一个大文件",
            turns=[
                make_turn(1, [], chars=100),
                make_turn(
                    2,
                    [
                        make_call(
                            "read_file",
                            {"path": "big.py"},
                            payload_chars=len(payload),
                            turn=2,
                        )
                    ],
                    chars=100,
                ),
                make_turn(3, [], chars=100),
                make_turn(4, [], chars=100),
                make_turn(5, [], chars=100),
            ],
            final_answer="完成",
        )
    ]
    bundle = write_trace(tmp_path, "saving", runs)
    analysis = analyze(bundle, corpus_root=corpus)

    unused_chars = sum(
        v.chars
        for v in analysis.verdicts
        if v.tool == "read_file"
        and v.status == "not_referenced_this_run"
        and v.chars >= 200
    )
    assert unused_chars > 0
    lever = next(
        p for p in analysis.proposals if p.lever.startswith("返回侧") and "read_file" in p.target
    )
    # 段在第 2 轮产生、共 5 轮 ⇒ 乘 3。写成乘 1 的话总量只有三分之一。
    assert lever.saving_chars == unused_chars * 3


# --------------------------------------------------------------------------
# 换算基准与金额
# --------------------------------------------------------------------------


def test_chars_to_tokens_uses_only_pairs_with_both_numbers() -> None:
    """chars=0 或 tokens=0 的轮次不进比值 —— 否则会算出无穷大或 0 的假比值。"""
    bundle = TraceBundle(label="t")
    bundle.runs["x"] = RunObs(
        task="x",
        final_answer="",
        stopped_reason="answered",
        turns=[
            TurnObs(
                turn=1,
                prompt_tokens=100,
                completion_tokens=1,
                messages_chars=200,
                tools_chars=0,
                duration_ms=0.0,
            ),
            TurnObs(
                turn=2,
                prompt_tokens=0,
                completion_tokens=0,
                messages_chars=0,
                tools_chars=0,
                duration_ms=0.0,
            ),
        ],
    )
    measured = analyze(bundle).chars_to_tokens
    assert measured.n_turns == 1
    assert measured.ratio == pytest.approx(100 / 200)


def test_no_proposal_means_no_dollar_figure_at_all(tmp_path) -> None:
    """没有任何提案时，报告里连一个美元符号都不该出现。"""
    runs = [
        make_run("任务甲", turns=[make_turn(1, [make_call("list_files", {}, payload_chars=9)])])
    ]
    bundle = write_trace(tmp_path, "nop", runs)
    analysis = analyze(bundle)
    assert analysis.proposals == []
    text = render(analysis, usd_per_mtok=0.0)
    assert "$" not in text
    assert render_json(analysis, usd_per_mtok=0.0)["usd_est"] is None


def test_no_price_means_no_dollar_figure_even_when_proposals_exist(tmp_path) -> None:
    """有提案但不给单价 ⇒ 只报 token，不折算金额，并明写"金额不折算"。"""
    bundle = trace_with_definition_lever(tmp_path, "nop2")
    analysis = analyze(bundle, offered={"all": _big_tool_list(), "active": _small_tool_list()})
    assert analysis.proposals
    text = render(analysis, usd_per_mtok=0.0)
    assert "金额不折算" in text
    assert "$" not in text
    assert render_json(analysis, usd_per_mtok=0.0)["usd_est"] is None


def test_with_price_the_figure_traces_back_to_the_rate(tmp_path) -> None:
    bundle = trace_with_definition_lever(tmp_path, "withp")
    analysis = analyze(bundle, offered={"all": _big_tool_list(), "active": _small_tool_list()})
    payload = render_json(analysis, usd_per_mtok=0.5)
    assert payload["saving_tokens_est"] > 0
    assert payload["usd_est"] == pytest.approx(
        payload["saving_tokens_est"] * 0.5 / 1e6, rel=1e-3
    )


# --------------------------------------------------------------------------
# 工具清单识别：用字符数核对，不用人说
# --------------------------------------------------------------------------


def test_tool_list_is_identified_by_exact_char_count(tmp_path) -> None:
    bundle = trace_with_definition_lever(tmp_path, "li")
    analysis = analyze(bundle, offered={"all": _big_tool_list(), "active": _small_tool_list()})
    assert len(analysis.offered_tools) == 20
    assert "完全匹配" in analysis.tool_list_match_note


def test_unmatchable_tool_list_produces_no_definition_side_proposal(tmp_path) -> None:
    turn = make_turn(1, [make_call("tool_0", {}, payload_chars=5)])
    turn["tools_chars"] = 12345  # 与任何候选都对不上
    bundle = write_trace(tmp_path, "li2", [make_run("任务", turns=[turn])])
    analysis = analyze(bundle, offered={"all": _big_tool_list()})
    assert analysis.offered_tools == ()
    assert not any(p.lever.startswith("定义侧") for p in analysis.proposals)
    assert "不猜" in analysis.tool_list_match_note


# --------------------------------------------------------------------------
# 调用侧复用（确定性判据）
# --------------------------------------------------------------------------


def test_repeated_identical_call_is_proposed_for_reuse(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = "def alpha():\n    pass\n"
    (corpus / "a.py").write_bytes(body.encode("utf-8"))
    payload = read_file_payload(body)

    runs = [
        make_run(
            "重复读同一个文件",
            turns=[
                make_turn(
                    1, [make_call("read_file", {"path": "a.py"}, payload_chars=len(payload))]
                ),
                make_turn(
                    2,
                    [
                        make_call(
                            "read_file",
                            {"path": "a.py"},
                            payload_chars=len(payload),
                            turn=2,
                        )
                    ],
                ),
                make_turn(3, []),
            ],
        )
    ]
    bundle = write_trace(tmp_path, "dup", runs)
    analysis = analyze(bundle, corpus_root=corpus)
    lever = next(p for p in analysis.proposals if p.lever.startswith("调用侧"))
    assert lever.affected_calls == 1
    # 第 2 轮重复产生的结果，只会在第 3 轮的 prompt 里再出现一次 ⇒ 乘数 1
    assert lever.saving_chars == len(payload) * 1


# --------------------------------------------------------------------------
# 两臂对照
# --------------------------------------------------------------------------


def _pair_runs(before_ptok: list[int], after_ptok: list[int], answer: str = "x") -> tuple:
    before = [
        make_run(f"t{i}", turns=[make_turn(1, [], ptok=p)], final_answer=answer)
        for i, p in enumerate(before_ptok)
    ]
    after = [
        make_run(f"t{i}", turns=[make_turn(1, [], ptok=p)], final_answer=answer)
        for i, p in enumerate(after_ptok)
    ]
    return before, after


def test_verification_pairs_by_task_and_counts_direction(tmp_path) -> None:
    before, after = _pair_runs([1000, 1000, 1000], [900, 900, 1100])
    v = verify(
        write_trace(tmp_path, "bef", before),
        write_trace(tmp_path, "aft", after),
        grader=GRADER_NONE,
    )
    assert v.n_paired == 3
    assert v.n_neg == 2
    assert v.n_pos == 1
    assert v.after_tokens - v.before_tokens == -100
    assert v.n_graded == 0
    assert any("未测得" in n for n in v.notes)


def test_unpaired_tasks_are_reported_not_silently_dropped(tmp_path) -> None:
    """一臂少跑了一个任务时，必须把"独有"数报出来。

    直接比总量会把"少跑一个"记成"变便宜了"——这是两臂对照最经典的错法。
    """
    before, after = _pair_runs([1000, 1000, 1000], [900, 900])
    v = verify(
        write_trace(tmp_path, "bef", before),
        write_trace(tmp_path, "aft", after),
        grader=GRADER_NONE,
    )
    assert v.n_paired == 2
    assert v.n_unpaired_before == 1
    assert any("不完全相同" in n for n in v.notes)


def test_grader_coverage_is_declared_not_assumed(tmp_path) -> None:
    """任务文本对不上内置任务集 ⇒ 只判能判的，并暴露覆盖数。"""
    before, after = _pair_runs([10, 10], [10, 10], answer="无关")
    v = verify(
        write_trace(tmp_path, "b", before),
        write_trace(tmp_path, "a", after),
        grader=GRADER_KEYWORD,
    )
    assert v.n_graded == 0
    assert v.mcnemar_p == 1.0


def test_report_compares_estimate_against_measured_when_both_arms_given(tmp_path) -> None:
    """§7 必须把估算与实测放在一起 —— 那是估算值唯一该被信任的用法。"""
    big = _big_tool_list()
    tools_chars = len(json.dumps(big, ensure_ascii=False))
    before_turn = make_turn(1, [make_call("tool_0", {}, payload_chars=5)], ptok=10000, chars=100)
    before_turn["tools_chars"] = tools_chars
    after_turn = make_turn(1, [], ptok=4000, chars=100)
    after_turn["tools_chars"] = len(json.dumps(_small_tool_list(), ensure_ascii=False))

    b = write_trace(tmp_path, "vb", [make_run("任务", turns=[before_turn])])
    a = write_trace(tmp_path, "va", [make_run("任务", turns=[after_turn])])
    analysis = analyze(b, offered={"all": big, "active": _small_tool_list()})
    text = render(analysis, verification=verify(b, a, grader=GRADER_NONE))
    assert "估算 vs 实测" in text


# --------------------------------------------------------------------------
# 输入层的能力边界必须从数据层推导出来
# --------------------------------------------------------------------------


def test_span_only_input_declares_field_level_is_unobserved(tmp_path) -> None:
    span = {
        "name": "gen_ai.tool.call",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "parent_id": None,
        "attributes": {
            "gen_ai.tool.name": "read_file",
            "gen_ai.tool.result_code": "ok",
            "findata.context.tool_payload_chars": 100,
        },
    }
    path = tmp_path / "spans-only.jsonl"
    path.write_bytes((json.dumps(span) + "\n").encode("utf-8"))
    bundle = load_bundle(path)
    assert not bundle.has_runs
    assert bundle.has_spans
    assert any("字段级归因" in g and "未观测" in g for g in bundle.gaps)


def test_missing_run_level_parent_span_is_detected_from_the_data(tmp_path) -> None:
    """每个工具 span 自成一根 trace ⇒ 判定"缺 run 级父 span"，并给出可核对的数字。"""
    spans = [
        {
            "name": "gen_ai.tool.call",
            "trace_id": f"{i:032x}",
            "span_id": f"{i:016x}",
            "parent_id": None,
            "attributes": {"gen_ai.tool.name": "read_file"},
        }
        for i in range(3)
    ]
    path = tmp_path / "spans-np.jsonl"
    path.write_bytes(("\n".join(json.dumps(s) for s in spans) + "\n").encode("utf-8"))
    bundle = load_bundle(path)
    assert bundle.has_run_level_parent_span is False
    assert bundle.n_distinct_trace_ids == 3
    assert any("run 级父 span" in g for g in bundle.gaps)


def test_ambiguous_trace_selection_raises_instead_of_guessing(tmp_path) -> None:
    (tmp_path / "runs-a.jsonl").write_bytes(b"{}\n")
    (tmp_path / "runs-b.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="不替你猜"):
        load_bundle(tmp_path)


def test_tag_prefers_exact_filename_over_substring(tmp_path) -> None:
    """`--tag all7b` 不许命中 `runs-all7b-fz.jsonl`（实测踩过这个坑）。"""
    runs = [make_run("任务甲", turns=[make_turn(1, [])])]
    exact = "\n".join(json.dumps(r, ensure_ascii=False) for r in runs) + "\n"
    (tmp_path / "runs-all7b.jsonl").write_bytes(exact.encode("utf-8"))
    (tmp_path / "runs-all7b-fz.jsonl").write_bytes(exact.encode("utf-8"))
    bundle = load_bundle(tmp_path, tag="all7b")
    assert bundle.source_runs.endswith("runs-all7b.jsonl")


# --------------------------------------------------------------------------
# 报告：措辞纪律
# --------------------------------------------------------------------------


def test_report_never_claims_unmeasured_saving(tmp_path) -> None:
    bundle = trace_with_definition_lever(tmp_path, "words")
    analysis = analyze(bundle, offered={"all": _big_tool_list(), "active": _small_tool_list()})
    text = render(analysis)
    assert "未实测" in text
    assert "成功率变化" in text and "未测得" in text
    # 不许出现把未观测当结论的说法
    assert "证明了没变" not in text
    # 上界式估算必须与它的偏差说明同屏
    assert "上界" in text and "漏判" in text


def test_report_discloses_that_layered_loading_costs_extra_calls(tmp_path) -> None:
    """分层加载省的是 payload，但可能多调一次工具 —— 这一侧代价必须写出来。"""
    bundle = trace_with_definition_lever(tmp_path, "layer")
    analysis = analyze(bundle, offered={"all": _big_tool_list(), "active": _small_tool_list()})
    assert "还缺一侧代价" in render(analysis)


def test_report_lists_declared_gaps_and_blind_spots(tmp_path) -> None:
    runs = [
        make_run("任务甲", turns=[make_turn(1, [make_call("list_files", {}, payload_chars=9)])])
    ]
    bundle = write_trace(tmp_path, "gaps", runs)
    text = render(analyze(bundle))
    assert "不可迁移" in text
    assert "漏判" in text
    assert "不可判" in text


def test_report_uses_the_measured_delta_once_both_arms_are_given(tmp_path) -> None:
    """给了改后一臂 ⇒ §2.2 必须改口报实测，不许一边说"未实测"一边在 §7 给出实测。

    写这个工具时实测踩到：两臂齐全时 §2.2 仍写着"未实测"，而 §7 明明白白
    列着 -123,208 tokens —— 报告自相矛盾，读者只会信那个更保守的说法，
    于是最有价值的那个数字被自己藏起来了。
    """
    big = _big_tool_list()
    before_turn = make_turn(1, [make_call("tool_0", {}, payload_chars=5)], ptok=10000, chars=100)
    before_turn["tools_chars"] = len(json.dumps(big, ensure_ascii=False))
    after_turn = make_turn(1, [], ptok=4000, chars=100)
    after_turn["tools_chars"] = len(json.dumps(_small_tool_list(), ensure_ascii=False))

    b = write_trace(tmp_path, "mb", [make_run("任务", turns=[before_turn])])
    a = write_trace(tmp_path, "ma", [make_run("任务", turns=[after_turn])])
    analysis = analyze(b, offered={"all": big, "active": _small_tool_list()})
    text = render(analysis, verification=verify(b, a, grader=GRADER_NONE))

    assert "未实测" not in text
    assert "**实测**（配对任务 1 个" in text
    assert "10,000 → 4,000" in text


def test_tiny_p_value_is_not_printed_as_zero(tmp_path) -> None:
    """p ≈ 2.6e-6 不许打成 `p = 0.0000` —— 那读起来像"恰好为零"。"""
    before = [make_run(f"t{i}", turns=[make_turn(1, [], ptok=1000)]) for i in range(32)]
    after = [make_run(f"t{i}", turns=[make_turn(1, [], ptok=500)]) for i in range(32)]
    # 造 3 个反向样本，让符号检验落在 3 vs 29
    for i in range(3):
        after[i] = make_run(f"t{i}", turns=[make_turn(1, [], ptok=1100)])

    v = verify(
        write_trace(tmp_path, "pb", before),
        write_trace(tmp_path, "pa", after),
        grader=GRADER_NONE,
    )
    assert v.n_pos == 3 and v.n_neg == 29
    text = render(
        analyze(write_trace(tmp_path, "pb2", before)), verification=v
    )
    assert "0.0000" not in text
    assert "e-06" in text or "e-07" in text


def test_chars_to_tokens_dict_pins_the_denominator() -> None:
    measured = CharsToTokens(
        n_turns=2, total_prompt_tokens=200, total_chars=1000, per_turn=[0.2, 0.2]
    )
    payload = measured.to_dict()
    assert payload["tokens_per_char"] == pytest.approx(0.2)
    assert "messages_chars + tools_chars" in str(payload["note"])
