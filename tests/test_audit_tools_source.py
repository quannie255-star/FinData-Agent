"""`run_context_audit --tools-from`：真实语料接进实验台的那条缝。

为什么值得单独钉住
------------------
在它之前，"实验用的工具清单是哪一份"**只有文件路径**这一条线索：
手写的 27 个和抓来的 230 个都会算出一个 `tools_list_sha256`，
但两个数之间没有任何可以互相核对的东西。于是"这次跑的是真实 server 语料"
只能靠我说，不能靠产物证明。

这个接口补的正是那一环：账单里同时记下**语料自报的指纹**与**本次重算的指纹**，
两者不一致就说明语料在落盘之后被动过、或者指纹算法变过。

这里钉三件事：
1. 工具清单**干净**（没有来源字段）⇒ 重算指纹才有可能等于语料自报的那个；
2. 身份信息里的成功/失败台数**原样带过来**（不许只报成功的）；
3. **语料被改过能被发现** —— 这条是这整条缝的存在理由。
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
SCRIPT = REPO_ROOT / "scripts" / "run_context_audit.py"


@pytest.fixture(scope="module")
def mod() -> Any:
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("_run_context_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corpus(path: Path, *, tools: list[dict[str, Any]] | None = None) -> Path:
    """造一份最小语料，**meta 里的指纹按同一算法真算一遍**（不写死一个数）。"""
    tools = tools if tools is not None else [
        {"type": "function", "function": {"name": "t1", "description": "d", "parameters": {}}}
    ]
    fingerprint = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "source": "test",
                    "source_kind": "page_declared",
                    "n_servers_ok": 47,
                    "n_servers_failed": 4,
                    "tools_list_sha256": fingerprint,
                },
                "tools": tools,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_loader_returns_tools_and_identity(mod: Any, tmp_path: Path) -> None:
    """工具清单 + 身份信息一起回来，**失败台数也要带过来**。

    只报成功台数会让人以为"47 台全成了"；语料里明明写着 4 台失败。
    """
    path = _corpus(tmp_path / "c.json")
    tools, info = mod.load_tools_from_corpus(str(path))
    assert len(tools) == 1
    assert info["kind"] == "mcp_corpus"
    assert info["n_servers_ok"] == 47
    assert info["n_servers_failed"] == 4
    assert info["source_kind"] == "page_declared"
    assert info["corpus_tools_list_sha256"]


def test_recomputed_fingerprint_equals_the_corpus_one(mod: Any, tmp_path: Path) -> None:
    """**这是这条缝的全部意义**：账单侧重算的指纹必须等于语料自报的那个。

    算法与 `run_context_audit.build_reproducibility` 里那份一致，
    所以"账单指纹 == 语料指纹"是一次真正的核对，而不是两个各自独立的数。
    """
    path = _corpus(tmp_path / "c.json")
    tools, info = mod.load_tools_from_corpus(str(path))
    recomputed = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    assert recomputed == info["corpus_tools_list_sha256"]


def test_a_tampered_corpus_is_detectable(mod: Any, tmp_path: Path) -> None:
    """把工具改一个字、meta 指纹不动 ⇒ 重算值必须不再等于自报值。

    这条在证明：这个接口**有能力发现**"语料落盘之后被换过"。
    如果一个检查在任何情况下都通不过/都通过，它就不是检查。
    """
    path = _corpus(tmp_path / "c.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tools"][0]["function"]["description"] = "偷偷改过的描述"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    tools, info = mod.load_tools_from_corpus(str(path))
    recomputed = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    assert recomputed != info["corpus_tools_list_sha256"]


def test_empty_corpus_yields_no_tools_so_the_caller_can_refuse(
    mod: Any, tmp_path: Path
) -> None:
    """空清单要**返回空表**（而不是抛错，也不是编一个默认清单）。

    调用方拿到空表就返回 2 —— 用空清单跑出来的"没调用任何工具"是一批
    看起来正常的假读数，比报错危险得多。
    """
    path = _corpus(tmp_path / "c.json", tools=[])
    tools, info = mod.load_tools_from_corpus(str(path))
    assert tools == []
    assert info["kind"] == "mcp_corpus"


def test_the_shipped_real_corpus_is_still_self_consistent(mod: Any) -> None:
    """**仓库里那份真实语料**的 meta 指纹，必须与它的 tools 对得上。

    这条会在别人改了语料却忘了重算指纹时红 —— 那时所有引用那次实验的报告
    都建立在一份对不上的语料上。语料不存在时跳过（不在 CI 里造 300KB 文件）。
    """
    path = REPO_ROOT / "examples" / "mcp-corpus" / "schemas-github.json"
    if not path.exists():
        pytest.skip("真实语料不在本机（未抓取），跳过")
    tools, info = mod.load_tools_from_corpus(str(path))
    recomputed = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    assert recomputed == info["corpus_tools_list_sha256"]
    assert tools, "真实语料里不该一个工具都没有"
