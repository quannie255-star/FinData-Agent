"""contextbudget 子包的测试。

重点钉两类判据：
  ① **会静默出错的**——比如 trace 里混进了参数原文（PII 泄漏）、
     span 属性名写错（下游采集不到，但本地一切正常、不报错）
  ② **账单可复现性依赖的**——字符数必须是确定性的，否则"省了 X 字符"
     这句话第二天就复现不出来

不测"模型答得对不对"：那是模型行为，不是本模块的契约，而且本机跑一次
要几十秒。模型行为进 `examples/` 归档，不进单测。
"""

from __future__ import annotations

import pytest

from findata.contextbudget.llm import payload_chars
from findata.contextbudget.schemas import (
    ACTIVE_TOOLS,
    ALL_TOOLS,
    SILENT_TOOLS,
    tools_payload_chars,
)
from findata.contextbudget.tasks import TASKS, is_hit
from findata.contextbudget.telemetry import (
    SEMCONV,
    args_hash,
    record_model_call,
    record_tool_call,
)
from findata.contextbudget.tools import (
    REPO_ROOT,
    call_tool,
    extract_blocks,
    get_signature,
    read_file,
    search_code,
)
from findata.observability.tracing import (
    get_in_memory_exporter,
    get_tracer,
    setup_tracer,
)


@pytest.fixture(autouse=True)
def _clean_exporter():
    """每个测试开跑前清空 in-memory exporter 收集到的 span。

    **这里刻意不用 `reset_for_test()`** —— OTel 的 TracerProvider 是进程级
    单例，`set_tracer_provider` 第二次调用会被 SDK 警告并忽略。而
    `reset_for_test()` 只重置本模块的缓存引用，换不掉真实 provider；结果是
    span 继续导到**第一次**创建的 exporter，而 `get_in_memory_exporter()`
    返回的却是新建的那个 —— 两者不一致，会让测试**看起来在验证，其实什么
    都没验证**（本文件第一版就踩了这个坑：一个测试报 IndexError）。
    正确做法是 `clear()`，与 tests/test_observability.py 保持一致。
    """
    setup_tracer(service_name="test-contextbudget")
    exp = get_in_memory_exporter()
    if exp is not None:
        exp.clear()
    yield


# --------------------------------------------------------------------------
# 工具清单
# --------------------------------------------------------------------------


def test_tool_list_has_expected_split():
    """活跃 / 沉默的分野是实验设计的一部分，数量变了实验条件就变了。"""
    assert len(ACTIVE_TOOLS) == 4
    assert len(SILENT_TOOLS) == 23
    assert len(ALL_TOOLS) == 27
    names = [t["function"]["name"] for t in ALL_TOOLS]
    assert len(names) == len(set(names)), "工具名不得重复（重复会让选择歧义）"


def test_tools_payload_chars_is_deterministic():
    """字符数是账单的分母，必须确定：同一个清单两次调用得到同一个数。"""
    first = tools_payload_chars(ALL_TOOLS)
    second = tools_payload_chars(ALL_TOOLS)
    assert first == second
    assert first > 0
    # 全量清单必须显著大于活跃清单，否则"工具膨胀"这个实验没有自变量
    assert first > tools_payload_chars(ACTIVE_TOOLS) * 2


def test_silent_tools_declare_no_implementation():
    """沉默工具**只有定义**。它一旦有了实现，就不再是"用不到的定义"，
    实验的自变量就变质了。"""
    from findata.contextbudget.tools import ACTIVE_IMPL

    active_names = {t["function"]["name"] for t in ACTIVE_TOOLS}
    silent_names = {t["function"]["name"] for t in SILENT_TOOLS}
    assert set(ACTIVE_IMPL) == active_names
    assert not (silent_names & set(ACTIVE_IMPL))


# --------------------------------------------------------------------------
# 遥测：PII 与属性名
# --------------------------------------------------------------------------


def test_args_hash_does_not_leak_arguments():
    """trace 里只能留摘要。参数原文含用户数据、内部 id，记原文等于种 PII。"""
    digest = args_hash({"secret_token": "sk-abc123", "user": "张三"})
    assert "abc123" not in digest
    assert "张三" not in digest
    assert len(digest) == 16


def test_args_hash_is_order_insensitive():
    """键序不应改变摘要，否则同一参数在对账时匹配不上。"""
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_tool_span_uses_semconv_names_and_hides_arguments():
    exporter = get_in_memory_exporter()
    assert exporter is not None
    tracer = get_tracer("test")

    record_tool_call(
        tracer,
        tool_name="read_file",
        call_id="call_1",
        arguments={"path": "src/secret_plan.py"},
        result_code="ok",
        payload_chars=1234,
        duration_ms=5.5,
    )

    spans = list(exporter.get_finished_spans())
    assert len(spans) == 1
    span = spans[0]
    attrs = dict(span.attributes or {})

    assert span.name == "gen_ai.tool.call"
    assert attrs["gen_ai.tool.name"] == "read_file"
    assert attrs["gen_ai.tool.call_id"] == "call_1"
    assert attrs["gen_ai.tool.args_hash"] == args_hash({"path": "src/secret_plan.py"})
    assert attrs["gen_ai.tool.result_code"] == "ok"
    assert attrs["findata.context.tool_payload_chars"] == 1234
    # 参数值绝不能出现在任何属性里
    assert "secret_plan" not in str(attrs)
    assert "path" not in str(attrs).lower()


def test_extension_attributes_do_not_pollute_semconv_namespace():
    """本项目自己的量必须走 findata.context.*，不许塞进 gen_ai.*。

    理由是下游会把 gen_ai.* 当规范字段解析；混进去会让别人对我这套数
    产生错误的信任——以为那是标准约定保证的字段。
    """
    exporter = get_in_memory_exporter()
    assert exporter is not None
    tracer = get_tracer("test")

    record_model_call(
        tracer,
        model="qwen2.5:3b",
        prompt_tokens=100,
        completion_tokens=10,
        n_tool_calls=1,
        messages_chars=500,
        tools_chars=11559,
        turn=1,
        duration_ms=9.9,
    )
    attrs = dict(list(exporter.get_finished_spans())[0].attributes or {})

    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.request.model"] == "qwen2.5:3b"
    assert attrs["findata.context.tools_chars"] == 11559
    assert attrs["findata.context.semconv"] == SEMCONV
    # 非约定字段不得占用 gen_ai.* 前缀
    assert "gen_ai.tool_payload_chars" not in attrs
    assert not any(k.startswith("gen_ai.context.") for k in attrs)


# --------------------------------------------------------------------------
# 工具实现：路径边界与参数校验
# --------------------------------------------------------------------------


def test_read_file_blocks_path_traversal():
    """目录穿越必须挡住。这不是安全表演——它保证工具永远只读仓库内的东西，
    否则"读操作占 token 大头"这个观测就掺进了不该有的输入。"""
    result = read_file("../../../../etc/passwd")
    assert result.result_code == "not_found"
    assert "passwd" not in result.content or "not found" in result.content.lower()


def test_unknown_parameter_returns_actionable_error():
    """参数名写错时，错误里必须带上合法参数名。

    实测 qwen2.5:3b 会把 prefix 传成 "path: " 这种自造键。如果只回
    "unexpected keyword argument"，模型拿不到任何可执行信息，那一轮就白花。
    这条测试钉住"错误必须可行动"。
    """
    result = call_tool("list_files", {"path: ": "src"})
    assert result.result_code == "error"
    assert "prefix" in result.content, "错误信息必须给出合法参数名"
    assert "path: " in result.content, "要指出错在哪"


def test_silent_tool_reports_not_implemented():
    result = call_tool("git_blame", {"path": "x"})
    assert result.result_code == "not_implemented"


def test_search_and_signature_are_asymmetric():
    """这个不对称是整个实验的支点：同一个信息需求，两条路的 payload 差很多。

    如果不成立（比如 get_signature 返回的比 search_code 还大），
    "裁剪上下文能省钱"就无从证明，只能靠嘴说。
    """
    path = "src/findata/agentops/triage.py"
    big = read_file(path)
    small = get_signature(path)
    assert big.result_code == "ok" and small.result_code == "ok"
    assert small.payload_chars < big.payload_chars / 2, (
        f"signature={small.payload_chars} 应当远小于 file={big.payload_chars}"
    )


def test_search_code_returns_full_bodies():
    """搜索返回完整函数体（而非仅匹配行）——这是被观测的浪费来源之一，
    行为一旦改成"只返回行"，账单的构成就变了，必须让它显式失败。"""
    result = search_code("RULES_VERSION", max_results=1)
    assert result.result_code == "ok"
    assert "def " in result.content or "=" in result.content
    assert result.payload_chars > 100


def test_extract_blocks_splits_top_level_definitions():
    source = (
        '"""mod"""\n'
        "import os\n"
        "\n"
        "def alpha(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "class Beta:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def gamma():\n"
        "    return 2\n"
    )
    blocks = extract_blocks(source)
    names = [name for name, *_ in blocks]
    assert names == ["alpha", "Beta", "gamma"]
    _, _, _, alpha_text = blocks[0]
    assert "return x" in alpha_text
    assert "class Beta" not in alpha_text, "块之间不得串味"


def test_repo_root_points_at_project_root():
    """语料目录必须解析到项目根，否则所有路径都失效且会静默返回空。"""
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "src" / "findata").is_dir()


# --------------------------------------------------------------------------
# 任务集与弱判据
# --------------------------------------------------------------------------


def test_task_ids_are_unique_and_nonempty():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids))
    assert len(TASKS) >= 30, "任务太少，配对检验的样本量不够"
    assert all(t.must_contain for t in TASKS), "每个任务都要有判据，否则无法产出任何标签"


def test_is_hit_is_case_insensitive_and_conservative():
    task = TASKS[0]
    token = task.must_contain[0]
    assert is_hit(task, f"答案是 {token.upper()}")
    assert is_hit(task, f"答案是 {token.lower()}")
    # 空答案永远不算命中 —— 避免"模型啥都没说"被计成成功
    assert not is_hit(task, "")
    assert not is_hit(task, "完全无关的回答")


def test_payload_chars_is_deterministic():
    payload = {"a": 1, "b": [1, 2, 3], "c": "中文"}
    assert payload_chars(payload) == payload_chars(payload)
    assert payload_chars(payload) > 0


@pytest.mark.parametrize("bad", ["", "   "])
def test_read_file_rejects_blank_path(bad: str):
    assert read_file(bad).result_code in {"not_found", "error"}
