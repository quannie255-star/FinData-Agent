"""语料指纹与漂移核验的测试。

这组测试针对的是一个**已经真实发生过**的事故：R5.0 的第一臂在跑的同时，
我往仓库里提交了 4 个提交，于是同一臂的 `list_files` 三次调用读到了三个不同的
目录状态，而产物里**没有任何东西**能证明这件事发生过。

所以这里钉的不是"函数返回什么"，而是三件更要紧的事：

1. **指纹对内容敏感**（改一个字节就变）、对无关事物不敏感（mtime 不动它）；
2. **工具真的会去读 `--corpus-root` 指定的地方**——否则指纹描述的是一个
   工具根本不读的目录，那这条防线是装饰品；
3. **"无法判断"不能退化成"不一致"**：老归档没有指纹，必须与"两臂语料不同"
   区分开，否则会把历史批次全判成无效。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from findata.contextbudget import tools
from findata.contextbudget.corpus import (
    corpus_manifest,
    diff_manifests,
    iter_corpus_files,
    manifests_agree,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "check_corpus_drift.py"


def _load_drift_script() -> Any:
    """按路径加载 scripts/ 下的脚本（scripts 不是包，只能这样 import）。"""
    spec = importlib.util.spec_from_file_location("_check_corpus_drift", DRIFT_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """一个小而完整的"语料快照"，形状与真实语料一致。"""
    root = tmp_path / "snap"
    (root / "pkg").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "pkg" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (root / "docs" / "n.md").write_text("# note\n", encoding="utf-8")
    # 这些**不该**进语料：后缀不符 / 在排除目录里
    (root / "pkg" / "blob.bin").write_bytes(b"\x00\x01")
    (root / ".git").mkdir()
    (root / ".git" / "trap.py").write_text("should not be seen\n", encoding="utf-8")
    return root


def test_manifest_is_deterministic(snapshot: Path) -> None:
    assert corpus_manifest(snapshot) == corpus_manifest(snapshot)


def test_manifest_ignores_files_outside_the_corpus_boundary(snapshot: Path) -> None:
    """边界（后缀 + 排除目录）必须是**唯一来源**，且真的生效。"""
    rels = {p.relative_to(snapshot).as_posix() for p in iter_corpus_files(snapshot)}
    assert rels == {"pkg/a.py", "docs/n.md"}


def test_content_change_moves_the_fingerprint(snapshot: Path) -> None:
    before = corpus_manifest(snapshot)
    (snapshot / "pkg" / "a.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    after = corpus_manifest(snapshot)
    assert before["sha256"] != after["sha256"]
    assert before["n_files"] == after["n_files"]  # 改内容不改文件数 —— 只看文件数会漏


def test_rename_without_content_change_moves_the_fingerprint(snapshot: Path) -> None:
    """只改名不改内容：文件数与字节数都不变，**只有指纹能发现**。"""
    before = corpus_manifest(snapshot)
    (snapshot / "pkg" / "a.py").rename(snapshot / "pkg" / "b.py")
    after = corpus_manifest(snapshot)
    assert (before["n_files"], before["bytes"]) == (after["n_files"], after["bytes"])
    assert before["sha256"] != after["sha256"]


def test_mtime_is_not_part_of_the_fingerprint(snapshot: Path) -> None:
    """指纹必须是**内容**的函数：checkout / 复制会改 mtime，那不该算语料变了。"""
    before = corpus_manifest(snapshot)
    import os
    import time

    stamp = time.time() - 10_000
    for path in iter_corpus_files(snapshot):
        os.utime(path, (stamp, stamp))
    assert corpus_manifest(snapshot) == before


def test_empty_corpus_is_not_the_same_as_unmeasured(tmp_path: Path) -> None:
    """空语料有一个**真实的**指纹；`manifests_agree` 只在缺失时才回 None。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    manifest = corpus_manifest(empty)
    assert manifest["n_files"] == 0
    assert manifest["sha256"]  # 非空串：算过，只是语料为空


def test_manifests_agree_distinguishes_unknown_from_disagreement() -> None:
    same = {"sha256": "aaa"}
    other = {"sha256": "bbb"}
    assert manifests_agree([same, dict(same)]) is True
    assert manifests_agree([same, other]) is False
    # **最关键的一条**：缺指纹 ≠ 语料不同。返回 None，调用方必须单独处理。
    assert manifests_agree([same, {}]) is None
    assert manifests_agree([{}]) is None


def test_diff_manifests_names_what_changed(snapshot: Path) -> None:
    """"不一样"不够用，要能说清是哪个文件。"""
    other = snapshot.parent / "other"
    other.mkdir()
    (other / "pkg").mkdir()
    (other / "docs").mkdir()
    (other / "pkg" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (other / "pkg" / "c.py").write_text("# new\n", encoding="utf-8")  # added
    (other / "docs" / "n.md").write_text("# NOTE CHANGED\n", encoding="utf-8")  # changed

    delta = diff_manifests(snapshot, other)
    assert delta["added"] == ["pkg/c.py"]
    assert delta["removed"] == []
    assert delta["changed"] == ["docs/n.md"]


def test_prefix_escape_is_treated_as_outside_the_corpus(snapshot: Path) -> None:
    """`../` 越界不抛异常，返回空表：语料边界是不变量。"""
    assert iter_corpus_files(snapshot, "../") == []


def test_tools_actually_read_the_corpus_root_we_point_them_at(snapshot: Path) -> None:
    """**这条是整套防线的关键**：指纹描述的目录，必须就是工具读的目录。

    如果 `set_corpus_root` 只影响指纹、不影响 `read_file`，那"两臂同一语料"
    这句话会**看起来**有据可查，实际上描述的是一个没人读的目录。
    """
    original = tools.get_corpus_root()
    try:
        tools.set_corpus_root(snapshot)
        assert tools.get_corpus_root() == snapshot.resolve()

        result = tools.read_file("pkg/a.py")
        assert result.result_code == "ok"
        assert "def alpha()" in result.content
        # 真实仓库里有 pyproject.toml，快照里没有 —— 证明读的确实是快照
        assert tools.read_file("pyproject.toml").result_code == "not_found"

        listing = tools.list_files("pkg")
        assert "pkg/a.py" in listing.content
        assert "blob.bin" not in listing.content
    finally:
        tools.set_corpus_root(original)


def test_drift_is_detected_from_payload_chars_only() -> None:
    """trace 级核验的判据本身：同一调用、不同字符数 ⇒ 内容变过。

    用**构造的**索引来测（不依赖真实归档）：判据要有独立的测试，
    否则它哪天静默失效，产物看起来还是"没问题"。
    """
    mod = _load_drift_script()
    index_a = {
        ("t01", "list_files", '{"prefix": "docs/"}'): 287,
        ("t02", "read_file", '{"path": "a.py"}'): 100,
    }
    index_b = {
        ("t01", "list_files", '{"prefix": "docs/"}'): 311,  # 漂移
        ("t02", "read_file", '{"path": "a.py"}'): 100,
        ("t03", "read_file", '{"path": "b.py"}'): 50,  # 仅 B 有
    }
    result = mod.cross_check(index_a, index_b)
    assert len(result["shared"]) == 2
    assert [(k[0], ca, cb) for k, ca, cb in result["drift"]] == [("t01", 287, 311)]
    assert [k[0] for k in result["only_b"]] == ["t03"]
    assert result["only_a"] == []


def test_drift_script_exits_nonzero_when_it_finds_drift(tmp_path: Path) -> None:
    """漂移必须**以退出码体现**，不然没人会读那段输出。"""
    mod = _load_drift_script()

    runs_dir = tmp_path / "arch"
    runs_dir.mkdir()
    base = {"final_answer": "x", "stopped_reason": "answered"}
    run_a = {
        **base,
        "task": "q",
        "turns": [
            {
                "prompt_tokens": 10,
                "tool_calls": [
                    {"name": "list_files", "arguments": {"prefix": "docs/"}, "payload_chars": 287}
                ],
            }
        ],
    }
    run_b = json.loads(json.dumps(run_a))
    run_b["turns"][0]["tool_calls"][0]["payload_chars"] = 311
    for tag, run in (("a", run_a), ("b", run_b)):
        (runs_dir / f"runs-{tag}.jsonl").write_text(
            json.dumps(run, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    import sys

    argv = sys.argv
    sys.argv = ["check_corpus_drift.py", "--dir", str(runs_dir), "--arm-a", "a", "--arm-b", "b"]
    try:
        with pytest.raises(SystemExit) as exc:
            sys.exit(mod.main())
    finally:
        sys.argv = argv

    # main() 返回 1 → sys.exit(1)；被 pytest.raises 捕获
    assert exc.value.code == 1
