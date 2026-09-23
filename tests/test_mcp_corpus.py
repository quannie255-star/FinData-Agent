"""真实 MCP 语料抓取脚本的测试。

这个脚本产出的东西会**替代**原来那 27 个手写工具定义，之后所有"工具定义吃多少
prompt token"的数字都建立在它身上。所以它错的方式都不显眼：少算几个工具、
把抓失败记成 0 个工具、指纹因为来源不同而变 —— 产物看起来都像正常输出。

因此这里钉四件事：

1. **缺名字的工具不许被编个名字补上**（那是造数据），要计数并报出来。
2. **指纹是"定义本身"的函数**：同一份定义换个抓取来源，指纹必须不变
   （否则"实验用的就是这份清单"没法核对）；但定义本身改一个字，指纹必须变。
3. **抓不到 ≠ 0 个工具**：失败单列，成功数不含失败数。
4. **指纹算法必须与账单同源**：这里用"自己再算一遍 sha256"的方式
   独立复现一遍口径，而不是断言"等于我想的那个值"。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fetch_mcp_corpus.py"


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("_fetch_mcp_corpus", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 工具定义：缺名字不许编，方言要记下来
# ---------------------------------------------------------------------------


def test_tool_without_a_name_is_dropped_not_invented(mod: Any) -> None:
    """**缺名字返回 None，不许用位置序号之类的东西补一个。**

    补一个名字 = 把"这条定义不完整"改写成"这条定义叫 xxx"，是造数据。
    调用方拿 None 去计数，"有多少条不完整"才是可观测的。
    """
    assert mod.to_openai_tool({"description": "没有名字"}) is None
    assert mod.to_openai_tool({"name": "   ", "description": "空白名"}) is None
    assert mod.to_openai_tool({"name": "ok_tool", "description": ""}) is not None


@pytest.mark.parametrize("key", ["input_schema", "inputSchema", "parameters"])
def test_all_three_schema_dialects_are_read_and_named(mod: Any, key: str) -> None:
    """三种写法在真实语料里都存在，都要认；**同时要能说出用的是哪一种**。

    记下来不是为了好看：它决定了"这份语料能不能被同一个程序批量处理"。
    读不出来就当没有 schema，也**不许**替它编一个。
    """
    raw = {
        "name": "t",
        "description": "d",
        key: {"type": "object", "properties": {"a": {"type": "string"}}},
    }
    tool = mod.to_openai_tool(raw)
    assert tool is not None
    assert tool["function"]["parameters"]["properties"] == {"a": {"type": "string"}}
    assert mod.schema_key_of(raw) == key


def test_missing_schema_becomes_empty_not_invented(mod: Any) -> None:
    """没有 schema ⇒ 空 properties，**不是**凭空给一个参数。"""
    raw = {"name": "t", "description": "d"}
    tool = mod.to_openai_tool(raw)
    assert tool is not None
    assert tool["function"]["parameters"]["properties"] == {}
    assert tool["function"]["parameters"]["required"] == []
    assert mod.schema_key_of(raw) == ""


# ---------------------------------------------------------------------------
# 指纹：出厂的工具必须是"干净的"，这样账单指纹才对得上
# ---------------------------------------------------------------------------


def test_corpus_tools_are_clean_of_provenance_fields(mod: Any) -> None:
    """**工具字典里不许有来源字段** —— 它要原样喂给 runner，而账单算
    `tools_list_sha256` 算的就是喂进去的那份。

    第一版把 `_provenance` 塞在工具里，靠"算指纹时再剥掉"来兜。
    那是在赌"每个调用方都记得剥"：漏一处，两个指纹就永远对不上，
    而**没有任何东西会报错**。改成工具本身干净、来源存在平行的 `tool_index` 里,
    这条不变量就变成结构性的，不靠记性。
    """
    tool = mod.to_openai_tool({"name": "t", "description": "d"})
    assert tool is not None
    assert set(tool) == {"type", "function"}
    assert set(tool["function"]) == {"name", "description", "parameters"}


def test_fingerprint_moves_when_a_definition_changes(mod: Any) -> None:
    """定义改一个字就要变 —— 否则"清单没变"这个断言是空的。"""
    before = mod.corpus_fingerprint([mod.to_openai_tool({"name": "t", "description": "d"})])
    after = mod.corpus_fingerprint([mod.to_openai_tool({"name": "t", "description": "d2"})])
    assert before != after


def test_fingerprint_matches_the_audit_bill_algorithm(mod: Any) -> None:
    """**两条独立路径**：这里照账单那边的算法自己再算一遍。

    `run_context_audit.build_reproducibility` 用的是
    `sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False))[:32]`。
    这里改成别的算法，两边的指纹会永远对不上，而**没有任何东西会报错**——
    所以这条显式钉住，且用"再算一遍"而不是"等于某个魔数"。
    """
    tools = [mod.to_openai_tool({"name": "t", "description": "d"})]
    expected = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    assert mod.corpus_fingerprint(tools) == expected


def test_meta_fingerprint_is_computed_from_the_tools_that_get_shipped(mod: Any) -> None:
    """`meta.tools_list_sha256` 必须是**出厂那份 `tools`** 的指纹。

    只要这两者一致，"把 corpus['tools'] 喂给 runner 后账单指纹应当等于
    corpus['meta'].tools_list_sha256" 就是可核对的 —— 语料与账单是两份
    独立落盘的东西，对不上就说明中间被换过。
    """
    corpus = mod.build_corpus(
        {
            "source": "test",
            "source_kind": mod.SOURCE_PAGE,
            "n_source_files": 1,
            "servers": [
                {
                    "id": "ok",
                    "source_kind": mod.SOURCE_PAGE,
                    "n_tools": 2,
                    "tool_schema_keys": ["input_schema", "inputSchema"],
                    "tools": [
                        mod.to_openai_tool({"name": "a", "description": "d"}),
                        mod.to_openai_tool({"name": "b", "description": "d"}),
                    ],
                }
            ],
            "failures": [],
        }
    )
    assert corpus["meta"]["tools_list_sha256"] == mod.corpus_fingerprint(corpus["tools"])
    # 来源按下标对齐，长度必须相等 —— 否则"第 i 个工具来自哪台"就是编的
    assert len(corpus["tool_index"]) == len(corpus["tools"]) == 2
    assert [i["server"] for i in corpus["tool_index"]] == ["ok", "ok"]
    assert [i["schema_key"] for i in corpus["tool_index"]] == ["input_schema", "inputSchema"]
    assert corpus["meta"]["schema_key_counts"] == {"input_schema": 1, "inputSchema": 1}


# ---------------------------------------------------------------------------
# server 级记录：不完整的定义要计数，不许静默丢
# ---------------------------------------------------------------------------


def test_declared_server_counts_incomplete_tools_instead_of_dropping_them(mod: Any) -> None:
    """两条无名字 + 一条有名字 ⇒ `n_tools == 1` 且 `n_tools_without_name == 2`。

    只报前者会把"这份 schema 有一半是坏的"藏起来；
    把坏的算进去又会虚高工具数。两个数都要有。
    """
    payload = {
        "server_info": {"name": "x", "repoUrl": "https://example.invalid"},
        "tools": [
            {"name": "good", "description": "d", "input_schema": {"type": "object"}},
            {"description": "没有名字"},
            "这是字符串不是对象",
        ],
    }
    rec = mod.normalize_declared(payload, server_id="x", provenance={"file": "x.json"})
    assert rec["n_tools"] == 1
    assert rec["n_tools_without_name"] == 2
    # camelCase 的 repoUrl 也要读出来，并标成 camel 方言
    assert rec["repo_url"] == "https://example.invalid"
    assert rec["info_dialect"] == "camel"


# ---------------------------------------------------------------------------
# 汇总：失败单列，且抓不到就整体失败
# ---------------------------------------------------------------------------


def test_build_corpus_keeps_failures_out_of_the_tool_count(mod: Any) -> None:
    """**失败不许变成"0 个工具"。** 它是未观测，不是零。"""
    collected = {
        "source": "test",
        "source_kind": mod.SOURCE_PAGE,
        "n_source_files": 2,
        "servers": [
            {
                "id": "ok",
                "source_kind": mod.SOURCE_PAGE,
                "n_tools": 1,
                "n_tools_without_name": 0,
                "tools": [mod.to_openai_tool({"name": "a", "description": "d"})],
            }
        ],
        "failures": [{"file": "bad.json", "error": "不是合法 JSON"}],
    }
    corpus = mod.build_corpus(collected)
    meta = corpus["meta"]
    assert meta["n_servers_ok"] == 1
    assert meta["n_servers_failed"] == 1
    assert meta["n_tools"] == 1
    assert meta["failures"] == [{"file": "bad.json", "error": "不是合法 JSON"}]
    # 工具来自哪台、属于哪类证据，能在平行索引里追到
    assert corpus["tool_index"][0]["server"] == "ok"
    assert corpus["tool_index"][0]["source_kind"] == mod.SOURCE_PAGE


def test_summarize_prints_the_success_rate(mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """成功率必须**打印出来**：不报成功率，"抓了 47 台"听起来就像 47 台全成了。"""
    corpus = mod.build_corpus(
        {
            "source": "test",
            "source_kind": mod.SOURCE_INTROSPECTED,
            "n_source_files": 4,
            "servers": [
                {
                    "id": "ok",
                    "source_kind": mod.SOURCE_INTROSPECTED,
                    "n_tools": 1,
                    "tools": [mod.to_openai_tool({"name": "a", "description": "d"})],
                }
            ],
            "failures": [{"command": "x", "error": "timeout"}] * 3,
        }
    )
    mod.summarize(corpus)
    out = capsys.readouterr().out
    assert "25.0%" in out  # 1 / 4
    assert "失败 3" in out


def test_collect_schemas_reads_from_cache_and_records_the_path(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """离线也能跑：**每个文件都记下走的是哪条路**。

    "抓到了"不够 —— 走缓存、走 raw、走 API 三件事的证据强度与代价都不同，
    读者要能判断这次抓取离限流还有多远。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.json").write_text(
        json.dumps({"server_info": {"name": "a"}, "tools": [{"name": "t1", "description": "d"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod, "_api_list_dir", lambda repo, subdir: [{"name": "a.json", "type": "file"}]
    )
    collected = mod.collect_schemas(
        repo="r", subdir="s", cache=cache, offline=True, limit=0
    )
    assert collected["n_source_files"] == 1
    assert len(collected["servers"]) == 1
    assert collected["servers"][0]["provenance"]["via"] == "cache"
    assert collected["servers"][0]["provenance"]["sha256"]


def test_a_file_missing_from_cache_is_a_failure_not_an_empty_server(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存里没有、又是 offline ⇒ **记成失败**，不许产出一条 `n_tools=0` 的 server。

    这一条是把"未观测"和"观测到 0"分开：前者进 failures，后者才是真的没有工具。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        mod, "_api_list_dir", lambda repo, subdir: [{"name": "ghost.json", "type": "file"}]
    )
    collected = mod.collect_schemas(repo="r", subdir="s", cache=cache, offline=True, limit=0)
    assert collected["servers"] == []
    assert len(collected["failures"]) == 1
    assert "ghost.json" in collected["failures"][0]["file"]


def test_main_exits_nonzero_when_nothing_was_collected(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**一个工具都没抓到 ⇒ 返回 1。**

    静默产出一个 `n_tools: 0` 的语料比报错危险得多：它看起来是一份正常产物，
    而用它跑出来的"工具定义只占 X% 上下文"会是个假数字。
    """
    from findata.contextbudget import tasks  # noqa: F401  （仅确保包可导入，非本用例依赖）

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mod, "_api_list_dir", lambda repo, subdir: [])
    out = tmp_path / "corpus.json"
    argv = sys.argv
    sys.argv = [
        "fetch_mcp_corpus.py",
        "--source", "schemas",
        "--cache-dir", str(cache),
        "--offline",
        "--out", str(out),
    ]
    try:
        code = mod.main()
    finally:
        sys.argv = argv
    assert code == 1
    assert "一个工具都没抓到" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["n_tools"] == 0
