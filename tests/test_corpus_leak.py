"""语料泄漏检查的测试。

这组测试针对的是一次**真实的、并且改变过结论的**事故：R5.0 的语料快照里
同时放着 32 道题的定义与每题的 `must_contain` 判据（`tasks.py`），
等于**把答案卡放在考场里**。实测后果：

- 一条 `search_code("五档处置")`：含答案卡 4,718 字符 / 不含 22 字符；
- 34 条真实查询里 14 条在含答案卡的语料上返回更大，共多出约 +4.5 万字符；
- 这份泄漏**偏向短清单臂**（5 个任务引用考题定义、2 个假命中，另一臂 0 个），
  于是"短清单质量不降反升"这条结论在无泄漏语料上重算后**不成立**。

所以这里钉的不是"函数返回什么"，而是四件更要紧的事：

1. **内容判据能抓到改名的副本** —— 只查路径的检查在文件被改名后就瞎了，
   而"改名"恰好是最容易发生、也最不容易被发现的形态；
2. **干净语料不许被误标** —— 一个"泄漏"标记会让人去剔数据，**误标就是误删**；
3. **"无法核对"不许退化成"干净"** —— 语料根缺失时必须给 `None`，
   否则"没查"会伪装成"查过了，没问题"；
4. **变化的轴必须由账单推导** —— `compare_audit_arms.py` 曾把
   「唯一变量是工具清单长度」写死在标题里，却被用在两臂工具数相同的
   裁剪策略对照上。这条测试就是那次的回归。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from findata.contextbudget.leakcheck import (
    EXAM_CARD_RELPATHS,
    check_exam_card,
    exam_card_in_corpus,
    scan_corpus_for_exam_card,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPARE_SCRIPT = REPO_ROOT / "scripts" / "compare_audit_arms.py"
LEAK_SCRIPT = REPO_ROOT / "scripts" / "check_corpus_leak.py"


def _load(path: Path, name: str) -> Any:
    """按路径加载 scripts/ 下的脚本（scripts 不是包，只能这样 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_corpus(tmp_path: Path) -> Path:
    """一个**不含**答案卡的小语料：形状与真实语料一致（.py/.md 会被枚举）。"""
    root = tmp_path / "clean"
    (root / "src" / "pkg").mkdir(parents=True)
    root.joinpath("src", "pkg", "mod.py").write_bytes(b"def alpha():\n    return 1\n")
    root.joinpath("README.md").write_bytes(b"# project\n")
    return root


def test_missing_root_is_none_not_false() -> None:
    """**未知 ≠ 没有。** 语料根缺失必须给 None，否则"没查"会伪装成"查过了"。"""
    assert exam_card_in_corpus("") is None
    assert exam_card_in_corpus(tmp_path_that_does_not_exist()) is None
    chk = check_exam_card("")
    assert chk.has_card is None
    assert chk.unverifiable is True
    assert "无法核对" in chk.describe()


def tmp_path_that_does_not_exist() -> str:
    return str(Path(__file__).resolve().parent / "_no_such_dir_")


def test_clean_corpus_is_not_flagged(clean_corpus: Path) -> None:
    """干净语料不许被误标 —— **误标就是误删数据**。"""
    chk = check_exam_card(clean_corpus)
    assert chk.has_card is False
    assert chk.present == []
    assert chk.content_hits == {}
    assert chk.n_files_scanned >= 2
    assert chk.n_files_skipped == 0


def test_path_criterion_sees_declared_file(tmp_path: Path) -> None:
    """路径判据：声明清单里的文件在，就报在。"""
    root = tmp_path / "withcard"
    target = root / EXAM_CARD_RELPATHS[0]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"def beta():\n    return 2\n")
    assert exam_card_in_corpus(root) is True
    chk = check_exam_card(root)
    assert EXAM_CARD_RELPATHS[0] in chk.present
    assert chk.has_card is True


def test_content_criterion_catches_renamed_copy(tmp_path: Path) -> None:
    """**这条是本文件的核心**：改名的答案卡必须被内容判据抓到。

    只查路径的检查在"文件被改名/复制到别处"后就完全瞎了，而这是最容易
    发生、也最不容易被发现的形态。这里刻意让**路径判据全灭**，
    只留内容判据能看见。
    """
    root = tmp_path / "renamed"
    (root / "notes").mkdir(parents=True)
    # 内容里有判据本体（must_contain），但文件名与任何受保护路径都不同
    root.joinpath("notes", "scratch.md").write_bytes(
        "题目与判据：# t01 ... must_contain=('原文',)\n".encode()
    )
    assert exam_card_in_corpus(root) is False, "路径判据本该看不到它（这正是它的盲区）"
    hits, scanned, skipped = scan_corpus_for_exam_card(root)
    assert "notes/scratch.md" in hits
    assert "must_contain" in hits["notes/scratch.md"]
    assert scanned == 1 and skipped == 0
    assert check_exam_card(root).has_card is True, "内容判据必须兜住路径判据的盲区"


def test_unreadable_file_is_counted_not_silently_dropped(clean_corpus: Path) -> None:
    """读不了的文件要**计数报出来**。静默丢掉会让"扫了 0 个文件"看起来像"干净"。"""
    clean_corpus.joinpath("bad.py").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    _hits, _scanned, skipped = scan_corpus_for_exam_card(clean_corpus)
    assert skipped == 1


def test_check_reports_absent_paths_so_a_claim_can_be_falsified(clean_corpus: Path) -> None:
    """"不在"要逐条列出来，不能只给一个布尔值 —— 否则这句话没有产物能证伪它。"""
    chk = check_exam_card(clean_corpus)
    assert sorted(chk.absent) == sorted(EXAM_CARD_RELPATHS)
    assert chk.present == []


# ─────────────────── 重放判据的汇总（纯函数，不碰真实语料） ───────────────────


def test_summarize_replay_counts_only_larger_queries() -> None:
    """重放汇总：只把**变大的**计入多出字符 —— 变小的不能拿来抵账。"""
    mod = _load(LEAK_SCRIPT, "_check_corpus_leak")
    rep = {
        "n_queries": 4,
        "rows": [
            {"query": "a", "target_chars": 100, "control_chars": 50,
             "delta_chars": 50, "target_leak_fingerprints": ["must_contain"]},
            {"query": "b", "target_chars": 30, "control_chars": 30,
             "delta_chars": 0, "target_leak_fingerprints": []},
            {"query": "c", "target_chars": 10, "control_chars": 90,
             "delta_chars": -80, "target_leak_fingerprints": []},
            {"query": "d", "target_chars": 70, "control_chars": 20,
             "delta_chars": 50, "target_leak_fingerprints": []},
        ],
    }
    s = mod.summarize_replay(rep)
    assert s["n_queries_returning_leak_fingerprints"] == 1
    assert s["leaked_queries"] == ["a"]
    assert s["n_queries_larger_on_target"] == 2  # a 与 d，c 变小不算
    assert s["total_extra_chars_on_target"] == 100  # 50 + 50，不减 80
    assert s["max_extra_chars_on_target"] == 50


def test_summarize_replay_without_control_root_reports_no_delta() -> None:
    """不给对照语料时不许编造差值 —— 只报绝对量。"""
    mod = _load(LEAK_SCRIPT, "_check_corpus_leak")
    rep = {"n_queries": 1, "rows": [
        {"query": "a", "target_chars": 99, "target_result_code": "ok",
         "target_leak_fingerprints": ["must_contain"]},
    ]}
    s = mod.summarize_replay(rep)
    assert "total_extra_chars_on_target" not in s
    assert s["n_queries_returning_leak_fingerprints"] == 1


def test_collect_queries_dedups_and_keeps_order(tmp_path: Path) -> None:
    """查询要**去重且保序**取真实的那些；编出来的查询不是证据。"""
    mod = _load(LEAK_SCRIPT, "_check_corpus_leak")
    trace = tmp_path / "runs-x.jsonl"
    trace.write_text(json.dumps({
        "task": "q1",
        "turns": [
            {"tool_calls": [{"name": "search_code", "arguments": {"query": "AAA"}}]},
            {"tool_calls": [
                {"name": "list_files", "arguments": {"prefix": "src"}},
                {"name": "search_code", "arguments": {"query": "BBB"}},
            ]},
        ],
    }, ensure_ascii=False) + "\n"
        + json.dumps({"task": "q2", "turns": [
            {"tool_calls": [{"name": "search_code", "arguments": {"query": "AAA"}}]},
            {"tool_calls": [{"name": "search_code", "arguments": {"query": ""}}]},
        ]}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert mod.collect_queries(trace, limit=10) == ["AAA", "BBB"]


# ────────────── 变化的轴必须由账单推导（硬编码标题那次的回归） ──────────────


def _bill(*, n_tools: int, chars: int, policy: str, code: str = "aaa") -> dict[str, Any]:
    return {
        "scope": {
            "n_tools_in_list": n_tools,
            "tools_chars_per_call": chars,
            "n_silent_tools": 0,
            "model": "qwen2.5:latest",
            "n_tasks": 32,
            "compact_policy": policy,
            "compact_caps": {},
        },
        "reproducibility": {
            "n_tools_in_list": n_tools,
            "corpus_root": "",
            "executed_code": {"sha256": code, "n_files": 101, "chars": 600_000},
        },
    }


def test_axis_is_compaction_policy_when_that_is_what_changed() -> None:
    """工具数与定义字符数**相同**、只有裁剪策略不同 ⇒ 轴必须报裁剪策略。

    这是那次事故的回归：脚本当时还在印「长清单一致更贵」，而两臂根本没有长清单短清单之分。
    """
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    axes, values = mod.describe_axis(
        _bill(n_tools=4, chars=2524, policy="none"),
        _bill(n_tools=4, chars=2524, policy="hint"),
    )
    assert axes == ["裁剪策略"], f"应只报裁剪策略，实得 {axes}"
    assert values == ["裁剪策略 A=none B=hint"]
    assert "工具清单长度" not in "".join(axes)


def test_axis_is_tool_list_when_that_is_what_changed() -> None:
    """反过来也要成立（工具数不同 ⇒ 轴是工具清单长度 + 定义字符数）。"""
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    axes, _values = mod.describe_axis(
        _bill(n_tools=27, chars=11559, policy="none"),
        _bill(n_tools=4, chars=2524, policy="none"),
    )
    assert "工具清单长度" in axes
    assert "工具定义字符数" in axes
    assert "裁剪策略" not in axes, "两臂裁剪策略相同，不该出现在变化的轴里"


def test_missing_field_reads_as_unrecorded_not_zero() -> None:
    """老账单没有 `compact_policy` ⇒ 读作「未记录」，**不许**顶成 0/空串。

    顶成 0 的后果：老账单会被判成"裁掉了" → 与"没有裁剪"的臂比出假差异。
    """
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    old = {"scope": {"n_tools_in_list": 4, "tools_chars_per_call": 2524}, "reproducibility": {}}
    assert mod._axis_value(old, "compact_policy") == "未记录"
    new = _bill(n_tools=4, chars=2524, policy="none")
    axes, _ = mod.describe_axis(old, new)
    assert "裁剪策略" in axes, "「未记录」 vs 「none」两类不同，必须报出来"


def test_code_axis_is_checked_and_can_fail() -> None:
    """代码指纹不同 ⇒ `report_code` 必须返回 False（R5.2 的 7B 批就是这样裂开的）。"""
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    a = _bill(n_tools=4, chars=2524, policy="none", code="ef9f3bcab2ab")
    b = _bill(n_tools=4, chars=2524, policy="hint", code="4b313ddbdcaf")
    assert mod.report_code("A", a, "B", b) is False
    assert mod.report_code("A", a, "B", _bill(
        n_tools=4, chars=2524, policy="hint", code="ef9f3bcab2ab")) is True


def test_code_axis_unverifiable_when_both_bills_are_old() -> None:
    """两个老账单都没有代码指纹 ⇒ **无法核对**，不是"一致"。"""
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    old = {"scope": {"n_tools_in_list": 4, "tools_chars_per_call": 2524}, "reproducibility": {}}
    assert mod.report_code("A", old, "B", old) is False


def test_exam_card_status_is_read_from_bill_corpus_root(clean_corpus: Path) -> None:
    """`compare_audit_arms` 的答案卡核对要**真的去读账单里那个语料根**（不是猜）。"""
    mod = _load(COMPARE_SCRIPT, "_compare_audit_arms")
    bill = _bill(n_tools=4, chars=2524, policy="none")
    bill["reproducibility"]["corpus_root"] = str(clean_corpus)
    assert mod.report_exam_card("A", bill) is False
    assert mod.report_exam_card("A", _bill(n_tools=4, chars=2524, policy="none")) is None
