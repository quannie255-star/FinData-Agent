"""钉死**返回内容裁剪**的口径（R5.2 的唯一自变量）。

这批测试要防的是三件事，每件都是这类实验真的会翻车的地方：

1. **裁掉了却没记账** → 读者以为模型看到了完整返回，其实没有。
2. **裁掉了"记录里被引用的字段"** → 得出"裁剪没伤到任何被引用的字段"这种
   **同义反复**结论（因为被裁的字段已经不在记录里了）。
   → 所以字段记录必须取自**原始**返回，裁剪只改"进消息的那份文本"。
3. **两臂其实没差**（某一段策略静默不生效）→ 差值 0 会被读成"结论稳健"。
   → 所以要有"这条策略确实改了文本"的断言，见 `test_each_policy_really_changes_the_text`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from findata.contextbudget import compaction
from findata.contextbudget.compaction import (
    POLICY_CAP,
    POLICY_HEAD,
    POLICY_HINT,
    POLICY_NONE,
    caps_from_profile,
    compact,
    load_profile,
)
from findata.contextbudget.fields import segment_payload

PAYLOAD_TWO_BLOCKS = (
    "def alpha():\n    return calculate_electric_field\n\n\ndef beta():\n    pass\n"
)
FENCED = f"```python\n{PAYLOAD_TWO_BLOCKS}\n```"
PATHS = "src/a.py\nsrc/b.py\nsrc/c.py\nsrc/d.py\nsrc/e.py"
# 40 个**互不相同**的路径。构造时注意每行都要有换行：
# `PATHS * 8` 会把上一块的最后一行和下一块的第一行粘成 `src/e.pysrc/a.py`，
# 于是"40 行"变成"33 段"——测试断言数量对不上时，先怀疑夹具，再怀疑被测代码。
MANY_PATHS = "".join(f"src/mod{i:02d}.py\n" for i in range(40))


# --------------------------------------------------------------------------
# 不裁剪：必须是**逐字节相同**，不能只是"看起来一样"
# --------------------------------------------------------------------------


def test_none_is_byte_identical() -> None:
    """`none` 臂不许改动一个字节 —— 它是所有差分的基准。

    `==` 对字符串就够，但这里仍然断言"同一个对象或完全相等"，因为一旦
    `none` 走了任何"重排/去尾空格"的路，R5.0 已经发布的读数就被静默改写了。
    """
    res = compact("read_file", FENCED, policy=POLICY_NONE)
    assert res.text == FENCED
    assert res.chars_before == res.chars_after == len(FENCED)
    assert res.saved_chars == 0
    assert res.applied is False


def test_unknown_policy_raises() -> None:
    with pytest.raises(ValueError, match="未知裁剪策略"):
        compact("read_file", FENCED, policy="magic")


# --------------------------------------------------------------------------
# 每一条策略都必须**真的改动了文本**（否则 0 差值分不清稳健还是不生效）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [POLICY_HEAD, POLICY_HINT, POLICY_CAP])
def test_each_policy_really_changes_the_text(policy: str) -> None:
    """**对照实验的第一步是证明两臂真的不一样。**

    这条测试的由来是反面教材：本项目早前跑过一次前提敏感度实验，patch 打在了
    一个判定路径根本不走的函数上，两臂跑出一模一样的数字，而当时脚本还在打印
    "结论稳健"。所以这里对**每一条**策略都断言它确实改了文本。
    """
    caps = {"list_files": 1}
    if policy == POLICY_CAP:
        tool, payload = "list_files", PATHS * 6  # 够长，才可能真省下来
    else:
        tool, payload = "read_file", FENCED * 12
    res = compact(tool, payload, policy=policy, caps=caps)
    assert res.applied is True, f"{policy} 没有生效 —— 两臂会跑出一模一样的数字"
    assert res.text != payload
    assert res.chars_after < res.chars_before


# --------------------------------------------------------------------------
# head：内容无关的"合理性判断"
# --------------------------------------------------------------------------


def test_head_keeps_exactly_the_head_and_declares_the_loss() -> None:
    res = compact("read_file", "x" * 900, policy=POLICY_HEAD, head_chars=500)
    assert res.text.startswith("x" * 500)
    assert "400 chars omitted" in res.text
    assert res.note and "500" in res.note


def test_head_does_not_touch_short_payloads() -> None:
    """返回本来就短 ⇒ 不裁。**"裁了 0 字符"和"没裁"要能分开**：这里 `applied` 为假。"""
    res = compact("read_file", "short", policy=POLICY_HEAD, head_chars=500)
    assert res.text == "short"
    assert res.applied is False


# --------------------------------------------------------------------------
# hint：协议仿真（判断者看不到内容）
# --------------------------------------------------------------------------


def test_hint_removes_all_content_but_keeps_the_accounting() -> None:
    res = compact("read_file", FENCED, policy=POLICY_HINT)
    assert "calculate_electric_field" not in res.text
    assert str(len(FENCED)) in res.text  # 字符数如实保留
    assert res.n_segments == 2  # 段数是**记下来的**，不是从内容里看出来的
    assert res.n_kept == 0


def test_hint_is_labelled_as_a_protocol_simulation_not_jev_itself() -> None:
    """产物里必须写明这是**协议仿真**。把仿真写成竞品本体是另一种造假。"""
    res = compact("read_file", FENCED, policy=POLICY_HINT)
    assert "仿真" in res.note


# --------------------------------------------------------------------------
# cap：证据型（上限来自 trace）
# --------------------------------------------------------------------------


def test_cap_keeps_the_first_n_segments_and_names_what_it_dropped() -> None:
    """超上限的段必须**真的不在文本里**，占位行只报账不报值。

    第一版占位行写的是 `-- path:src/c.py (8 chars omitted) --`，测试当场把它否掉：
    值又回到了上下文，`list_files` 这种"值比提示短"的段等于没裁。
    """
    payload = MANY_PATHS  # 40 个路径，逐段占位会"增肥"，只有合并才可能真省
    res = compact("list_files", payload, policy=POLICY_CAP, caps={"list_files": 2})
    assert "src/mod00.py" in res.text and "src/mod01.py" in res.text
    assert "src/mod02.py" not in res.text, "超上限的段必须真的不在文本里"
    assert "38 path" in res.text and "chars omitted" in res.text, "占位行要报清条数与字符数"
    assert res.n_segments == 40 and res.n_kept == 2
    assert res.n_mapped == 40, "40 段都应在原文里定位到"
    assert res.applied is True


def test_consecutive_drops_share_one_placeholder_line() -> None:
    """连续被丢的段合并成一行 —— 逐段一行会在小返回上变成"增肥"。"""
    res = compact("list_files", MANY_PATHS, policy=POLICY_CAP, caps={"list_files": 1})
    assert res.text.count("chars omitted") == 1
    assert "39 path" in res.text


def test_compaction_that_saves_nothing_falls_back_to_the_original() -> None:
    """**算下来没省就整条回退**，并且不许把它记成一次成功的裁剪。

    这是第二个被测试抓住的设计缺陷：占位行本身有字符成本（约 40 字符量级），
    在一段只有 8 个字符的返回上，"裁掉"反而会**变长**。若不回退，
    账单里会出一个负的节省值，或者更糟——把一个"字符数变小"当成裁剪成功的证据。
    """
    two = "src/a.py\nsrc/b.py"
    res = compact("list_files", two, policy=POLICY_CAP, caps={"list_files": 1})
    assert res.text == two, "裁了反而更长 ⇒ 回退成原文"
    assert res.applied is False
    assert "没省" in res.note


def test_cap_leaves_text_outside_segments_untouched() -> None:
    """段以外的文本（代码围栏、工具自己写的表头）**必须原样保留**：
    它不是我们要裁的东西，改动它等于伪造工具的输出。"""
    body = "".join(f"def f{i}():\n    x{i} = {i}\n\n\n" for i in range(20))
    payload = f"```python\n{body}\n```"
    res = compact("read_file", payload, policy=POLICY_CAP, caps={"read_file": 3})
    assert res.text.startswith("```python\n"), "围栏标记不是段，不许动"
    assert res.text.rstrip().endswith("```")
    assert "def f0()" in res.text and "def f2()" in res.text
    assert "def f5()" not in res.text
    assert res.applied is True


def test_cap_with_no_entry_for_the_tool_does_nothing() -> None:
    """profile 里没有这个工具 ⇒ 不裁。**不做默认上限**：凭空定一个上限就是拍脑袋。"""
    res = compact("read_file", FENCED, policy=POLICY_CAP, caps={"list_files": 2})
    assert res.text == FENCED
    assert res.applied is False


@pytest.mark.parametrize("code", ["error", "not_found", "not_implemented"])
def test_cap_does_not_touch_failed_returns(code: str) -> None:
    """报错的返回没有结构 ⇒ 没有"段"可裁 ⇒ 原样进上下文。"""
    payload = "No files under 'src/nope/'."
    res = compact("list_files", payload, policy=POLICY_CAP, caps={"list_files": 2},
                  result_code=code)
    assert res.text == payload
    assert res.applied is False


def test_cap_of_one_still_leaves_one_segment() -> None:
    """上限至少 1。留 0 个不再是"裁剪"，是把工具废掉 —— 那是另一条策略（hint）。"""
    res = compact("list_files", PATHS, policy=POLICY_CAP, caps={"list_files": 1})
    assert res.n_kept == 1


def test_cap_coverage_is_reported_when_a_segment_cannot_be_located() -> None:
    """**定位不到的段不许被"当成已裁"。**

    构造法：拿一个真段配上不存在的原文。正确行为是原样保留 + `n_mapped` 记少，
    让读报告的人能看见"这次裁剪没有按它声称的方式工作"。
    """
    res = compaction._apply_keep(
        "只有这一段",
        [("path:not-here.py", 13, "src/not-here.py")],
        keep=[False],
        trunc=0,
        policy=POLICY_CAP,
    )
    assert res.text == "只有这一段"
    assert res.n_mapped == 0 and res.n_segments == 1


# --------------------------------------------------------------------------
# profile：口径的输入也要能被复核
# --------------------------------------------------------------------------


def _profile(**over) -> dict:
    base = {
        "caps": {"list_files": 2},
        "table": [{"tool": "list_files", "kind": "path", "n": 10, "n_ref": 1}],
        "per_call": {"list_files": {"n_calls": 3, "n_ref_p50": 0, "n_ref_p90": 2, "n_ref_max": 9}},
    }
    base.update(over)
    return base


def test_caps_from_profile_rounds_up_to_at_least_one() -> None:
    """p50 可能是 0（多数调用一个段都没被引用）。上限 0 等于废掉工具，所以夹到 1。"""
    caps = caps_from_profile(_profile(), quantile="p50")
    assert caps == {"list_files": 1}


def test_caps_from_profile_requires_the_quantile() -> None:
    """要一个 profile 里没有的分位数 ⇒ 报错，**不静默退化成另一个分位数**。"""
    with pytest.raises(ValueError, match="n_ref_p99"):
        caps_from_profile(_profile(), quantile="p99")


def test_load_profile_rejects_a_file_without_caps(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="caps/table"):
        load_profile(bad)


# --------------------------------------------------------------------------
# 与 segment_payload 的分工：段只有一个定义处
# --------------------------------------------------------------------------


def test_compaction_segments_come_from_the_same_definition() -> None:
    """裁剪用的段 = 记录用的段。若各切一份，"留下的"和"记下的"可能对不上，
    而整个实验的可信度正建立在它们对得上。"""
    segs = segment_payload("list_files", PATHS)
    res = compact("list_files", PATHS, policy=POLICY_CAP, caps={"list_files": 3})
    assert res.n_segments == len(segs) == 5


# --------------------------------------------------------------------------
# 端到端：模型看到的是裁剪后的文本，**字段记录仍是原始返回的**
# --------------------------------------------------------------------------


class _Reply:
    def __init__(self, content: str, tool_calls: list[dict] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.prompt_tokens = 100
        self.completion_tokens = 20


class _StubClient:
    """按脚本回答；**把每次请求的 messages 原样存下来**——这是本测试的观测点。"""

    model = "stub"

    def __init__(self, script: list[_Reply]) -> None:
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        self.seen.append([dict(m) for m in messages])
        return self.script.pop(0)


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _run_with_policy(tmp_path: Path, policy: str, caps: dict[str, int] | None = None):
    """跑一次真 loop（工具是真的，模型是桩）：看模型收到什么、账上记了什么。"""
    from findata.contextbudget.agent import run_agent
    from findata.contextbudget.tools import set_corpus_root
    from findata.observability.tracing import get_tracer, reset_for_test, setup_tracer

    for i in range(60):
        (tmp_path / f"mod{i:02d}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    set_corpus_root(tmp_path)
    reset_for_test()
    setup_tracer(service_name="test-compaction")
    tracer = get_tracer("test-compaction")
    client = _StubClient(
        [
            _Reply("", [_tool_call("list_files", {"prefix": ""})]),
            _Reply("最终答复：目录下有 60 个文件。"),
        ]
    )
    try:
        return run_agent(
            client,
            tracer,
            "这个目录下有哪些文件？",
            tools=[],
            compact_policy=policy,
            compact_caps=caps,
        ), client
    finally:
        set_corpus_root(None)
        reset_for_test()


def test_model_sees_the_compacted_text_but_the_record_keeps_the_original(
    tmp_path: Path,
) -> None:
    """**这是整个 R5.2 的准入条件。**

    如果先裁再记账，"被裁掉的字段"就不在记录里了，于是
    "裁剪没有损失任何被引用的字段"变成**同义反复**——它必然成立，因为它没在看
    被裁掉的东西。所以两件事必须分开观测：
      · 模型看到的文本 = 裁剪后（`compacted_chars` 可证）
      · 字段记录 = **原始**返回（`payload_chars` 与 `n_fields` 可证）
    """
    run, client = _run_with_policy(tmp_path, POLICY_CAP, {"list_files": 2})

    tool_msgs = [
        m for req in client.seen for m in req if m.get("role") == "tool"
    ]
    assert len(tool_msgs) == 1
    seen_text = str(tool_msgs[0]["content"])

    call = run.turns[0].tool_calls[0]
    assert call.payload_chars > call.compacted_chars, "模型看到的确实变短了"
    assert call.compacted_chars == len(seen_text), "记账的数与模型看到的必须一致"
    assert call.n_fields == 60, "**字段记录仍是原始返回的 60 段**，不是裁剩下的 2 段"
    assert "chars omitted" in seen_text
    assert seen_text.count("mod") < 60, "被裁的段不许出现在模型看到的文本里"


def test_policy_none_gives_the_model_the_untouched_payload(tmp_path: Path) -> None:
    """`none` 臂：模型看到的**逐字节等于**工具原始返回。基准不许被动过。"""
    run, client = _run_with_policy(tmp_path, POLICY_NONE)
    seen_text = str(
        next(m for req in client.seen for m in req if m.get("role") == "tool")["content"]
    )
    call = run.turns[0].tool_calls[0]
    assert call.compacted_chars == call.payload_chars == len(seen_text)
    assert call.compact_policy == POLICY_NONE
    assert call.compact_applied is False
    assert run.total_saved_chars == 0
