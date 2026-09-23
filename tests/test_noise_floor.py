"""噪声下界脚本的测试 —— 三条用例对应三个**已经真实发生**的错。

`scripts/repeat_run_variance.py` 承载的是"R5.2 那些质量侧结论里有多少是噪声"这个判读。
它自己要是错的，读出来的就是一个**看起来很正常**的数（"同配置差 0.1%"），
而那个数会被直接写进验收报告。所以它必须进单测，且测试要钉住下面三件事：

1. **核对项不许来自臂名。** 判"两次运行是不是同一个配置"必须读账单字段。
   反面教材：我有一批文件名写着 `7b`、实际是 3B 的产物，差点据此宣布
   "噪声大到盖过效应"（见 §3.10）。
2. **"没记录"不是"不一致"，也不是"一致"。** 老批次的账单没有 `compact_policy`
   这个键；把它判成"配置不一致"会把**唯一可用的历史配对**挡在闸门外。
3. **落盘的 runs 是 dict，不是 `AgentRun`。** 第一版照 dataclass 的 property 写属性访问，
   以及拿"问题原文"去索引以 `task_id` 为键的判档结果 —— 两处都会在真数据上直接崩，
   而这两处**只有真跑一次才会暴露**。下面的 `test_..._does_not_crash` 就是为此存在的。

测试里刻意用**真 task 的问题原文**（从 `TASKS` 取），再加一条不在 `TASKS` 里的
假任务：后者的作用是证明"判不了档的任务被单列剔除、不许悄悄缩小分母"。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from findata.contextbudget.tasks import TASKS

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "repeat_run_variance.py"


@pytest.fixture(scope="module")
def mod() -> Any:
    """按路径加载脚本（`scripts/` 不是包）。

    脚本里 `from grade_audit_runs import ...` 是**兄弟脚本的直接导入**，
    所以必须先把 `scripts/` 放上 `sys.path` —— 这也正是"脚本能不能被 import"
    这一条本身有测试价值的原因。
    """
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("_repeat_run_variance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 夹具：一个"能判档"的最小批次
# ---------------------------------------------------------------------------


def _bill(
    *,
    model: str = "qwen2.5:3b",
    corpus: str = "corpus-sha",
    tools_list: str = "tools-sha",
    policy: str | None = "none",
    n_tools: int = 4,
) -> dict[str, Any]:
    """账单。`policy=None` 表示**这个键不存在**（老批次），与 `policy="none"` 不同。"""
    scope: dict[str, Any] = {"model": model, "n_tools_in_list": n_tools}
    if policy is not None:
        scope["compact_policy"] = policy
    return {
        "scope": scope,
        "reproducibility": {
            "corpus": {"sha256": corpus},
            "tools_list_sha256": tools_list,
        },
    }


def _run(
    question: str,
    *,
    prompt_tokens: int,
    completion_tokens: int = 5,
    payload_chars: int = 100,
    answer: str = "一个答复",
) -> dict[str, Any]:
    return {
        "task": question,
        "final_answer": answer,
        "stopped_reason": "answered",
        "error": "",
        "turns": [
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tool_calls": [
                    {
                        "name": "list_files",
                        "arguments": {"prefix": ""},
                        "result_code": "ok",
                        "payload_chars": payload_chars,
                    }
                ],
            }
        ],
    }


def _write_pair(
    tmp_path: Path,
    *,
    a: str = "arm-a",
    b: str = "arm-b",
    bill_a: dict[str, Any] | None = None,
    bill_b: dict[str, Any] | None = None,
    runs_a: list[dict[str, Any]],
    runs_b: list[dict[str, Any]],
) -> None:
    (tmp_path / f"bill-{a}.json").write_text(
        json.dumps(bill_a if bill_a is not None else _bill(), ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / f"bill-{b}.json").write_text(
        json.dumps(bill_b if bill_b is not None else _bill(), ensure_ascii=False),
        encoding="utf-8",
    )
    for tag, runs in ((a, runs_a), (b, runs_b)):
        (tmp_path / f"runs-{tag}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs),
            encoding="utf-8",
        )


def _run_main(
    mod: Any, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """跑一次 `main()`，**同时**取回 stdout 与 stderr。

    必须一起返回：`readouterr()` 会**清空**缓冲区，先读一次 out 再读 err
    只会得到空串（第一版就是这么写错的 —— 断言拿到 `''`，
    看起来像"脚本没打印"，其实是"我把水管接走了"）。
    """
    old = sys.argv
    sys.argv = ["repeat_run_variance.py", *argv]
    try:
        code = mod.main()
    finally:
        sys.argv = old
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# 1) 核对项来自产物，不来自臂名
# ---------------------------------------------------------------------------


def test_setup_of_reads_the_bill_fields(mod: Any) -> None:
    """五样都要真的从账单里取到；取不到才是"未记录"。"""
    s = mod.setup_of(_bill())
    assert s == {
        "模型": "qwen2.5:3b",
        "语料": "corpus-sha",
        "工具清单": "tools-sha",
        "裁剪策略": "none",
        "工具数": "4",
    }


def test_setup_of_marks_absent_keys_as_unrecorded(mod: Any) -> None:
    """**缺键**（老批次）与**值恰好为空**必须区分开，不能被 `.get(x, "")` 糊成一个。"""
    s = mod.setup_of(_bill(policy=None))
    assert s["裁剪策略"] == mod.NL
    assert s["模型"] != mod.NL


def test_compare_setups_is_three_way_not_two_way(mod: Any) -> None:
    """三分类的边界：一致 / 不一致 / **无法核对**。

    第三类是本文件最要紧的一条。天真的 `a[k] != b[k]` 会把
    "老账单没有这个字段" 判成 "配置不一致"，于是唯一可用的历史配对
    （`active3b-fx` vs `active3b-fx-fp`，前者的账单是补丁前写的）被闸门挡掉；
    反过来把"没记"当成"相同"，就是把"两批都叫 7b"当成"两批都是 7B"。
    """
    same = {"模型": "m", "裁剪策略": "none"}
    assert mod.compare_setups(same, dict(same)) == ([], [])
    assert mod.compare_setups(same, {**same, "模型": "other"}) == (["模型"], [])
    # 一边缺 → 无法核对（不是不一致！）
    assert mod.compare_setups(same, {**same, "裁剪策略": mod.NL}) == ([], ["裁剪策略"])
    # 两边都缺 → 也是无法核对，不是"一致"
    both_missing = {"模型": "m", "裁剪策略": mod.NL}
    assert mod.compare_setups(both_missing, dict(both_missing)) == ([], ["裁剪策略"])


# ---------------------------------------------------------------------------
# 2) 闸门：不一致就非 0 退出；"无法核对"默认也不放行
# ---------------------------------------------------------------------------


def test_gate_rejects_a_pair_that_is_not_the_same_setup(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """模型不同 ⇒ 这不是"同一个配置跑两次"，算出来的差不叫噪声下界。"""
    _write_pair(
        tmp_path,
        bill_b=_bill(model="qwen2.5:latest"),
        runs_a=[_run(TASKS[0].question, prompt_tokens=10)],
        runs_b=[_run(TASKS[0].question, prompt_tokens=10)],
    )
    code, out, err = _run_main(
        mod, ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b"], capsys
    )
    assert code == 2
    assert "配置不一致" in err
    assert "模型" in out


def test_gate_requires_the_flag_when_a_field_cannot_be_verified(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**"没记录"默认不放行**：强行继续时要把未核对项写进产物，读数才能带上这句话。"""
    runs = [_run(TASKS[0].question, prompt_tokens=10)]
    _write_pair(tmp_path, bill_a=_bill(policy=None), runs_a=runs, runs_b=runs)
    argv = ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b"]
    out_file = tmp_path / "noise.json"

    code, _, err = _run_main(mod, argv, capsys)
    assert code == 2
    assert "无法核对" in err

    code, _, _ = _run_main(mod, [*argv, "--allow-unrecorded", "--out", str(out_file)], capsys)
    assert code == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["unverified_setup_fields"] == ["裁剪策略"]
    assert payload["mismatched_setup_fields"] == []
    # 产物自己要把这句写在 note 里，而不是只活在终端上
    assert "无法核对" in payload["note"]


def test_gate_warns_loudly_when_the_two_runs_share_nothing(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """共同任务 0 个时非 0 退出：算不出下界，不能静默给一个 0。"""
    _write_pair(
        tmp_path,
        runs_a=[_run(TASKS[0].question, prompt_tokens=10)],
        runs_b=[_run(TASKS[1].question, prompt_tokens=20)],
    )
    code, _, _ = _run_main(mod, ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b"], capsys)
    assert code == 2


# ---------------------------------------------------------------------------
# 3) 数据形状：只有真跑一次才会暴露的两处
# ---------------------------------------------------------------------------


def test_main_runs_end_to_end_on_raw_dicts(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**这是 AttributeError 的回归用例。**

    第一版 `per_task_metrics` 照 `AgentRun` 的 dataclass 写法用属性访问
    （`run.total_prompt_tokens`），可 `load_runs` 给的是 `json.loads` 出来的 dict，
    那些名字在 dict 上根本不存在。这个 bug **写的时候看不出来**，
    只有把脚本真跑一次才会炸 —— 所以这里用最小真数据把它跑通。

    顺带钉住第二处：判档结果按 `task_id` 建键、而 `shared` 按**问题原文**建键，
    第一版混用会 `KeyError`。下面**故意**放一条不在 `TASKS` 里的假任务，
    用来证明"判不了档的任务被剔除并报数"，而不是让分母悄悄变小。
    """
    question = TASKS[0].question
    fake = "（这条问题不在 TASKS 里，判不了档）"
    runs_a = [
        _run(question, prompt_tokens=10, payload_chars=100),
        _run(fake, prompt_tokens=999, payload_chars=7),
    ]
    runs_b = [
        _run(question, prompt_tokens=12, payload_chars=150),
        _run(fake, prompt_tokens=999, payload_chars=7),
    ]
    _write_pair(tmp_path, runs_a=runs_a, runs_b=runs_b)
    out_file = tmp_path / "noise.json"

    code, out, _ = _run_main(
        mod, ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b", "--out", str(out_file)],
        capsys,
    )
    assert code == 0, "脚本必须在落盘 runs 的 dict 形状上跑通"

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    # token 侧用**全部**共同任务（含假任务）—— 它是 token 层面的量，不依赖判档
    assert payload["n_shared"] == 2
    assert payload["stats"]["prompt_tokens"]["a_total"] == 1009
    assert payload["stats"]["prompt_tokens"]["b_total"] == 1011
    assert payload["stats"]["payload_chars"]["a_total"] == 107
    # 质量侧只判得了 1 个任务，分母必须如实反映这一点
    assert payload["quality_flips"]["exact_hit（精确值，为准）"]["n_graded"] == 1
    assert "判不了档" in out or "不在 TASKS" in out


def test_noise_floor_payload_carries_both_numbers(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """产物里必须**同时**有 token 下界与质量侧翻转数 —— 只给一个就会被读成"只有一种噪声"。"""
    question = TASKS[0].question
    _write_pair(
        tmp_path,
        runs_a=[_run(question, prompt_tokens=10)],
        runs_b=[_run(question, prompt_tokens=30)],
    )
    out_file = tmp_path / "noise.json"
    code, _, _ = _run_main(
        mod, ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b", "--out", str(out_file)],
        capsys,
    )
    assert code == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    floor = payload["noise_floor"]
    assert floor["prompt_tokens_abs_delta_median"] == 20
    assert floor["prompt_tokens_total_relative"] == pytest.approx(2.0)
    assert "exact_hit_flips" in floor
    # 读数怎么用必须写在产物里，不能只活在脚本的 print 里
    assert "在噪声内" in payload["note"] or "下界" in payload["note"]


def test_identical_runs_produce_a_zero_floor_not_a_missing_one(
    mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """两次运行完全一样 ⇒ 下界是 **0**，不是 `None`/缺字段。

    "没测到差异" 和 "没测" 是两件事：前者是一个（很强）的观测，
    后者什么都不支持。产物里必须是 0。
    """
    question = TASKS[0].question
    runs = [_run(question, prompt_tokens=10, payload_chars=100)]
    _write_pair(tmp_path, runs_a=runs, runs_b=runs)
    out_file = tmp_path / "noise.json"
    code, _, _ = _run_main(
        mod, ["--dir", str(tmp_path), "--a", "arm-a", "--b", "arm-b", "--out", str(out_file)],
        capsys,
    )
    assert code == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["noise_floor"]["prompt_tokens_abs_delta_median"] == 0
    assert payload["noise_floor"]["prompt_tokens_total_relative"] == 0
    assert payload["stats"]["prompt_tokens"]["n_identical"] == 1


def test_policy_evidence_is_read_from_the_trace_not_the_bill(mod: Any) -> None:
    """第二条独立路径：每次调用自己记着策略。

    它证不了老批次的策略（那批落盘时没有这个字段），但"记录里没有裁剪痕迹"
    这一点本身是可报的观测 —— 而且它与账单是两条独立的路，
    两条对不上时说明其中一份不可信。
    """
    runs = [_run(TASKS[0].question, prompt_tokens=10)]
    ev = mod.policy_evidence(runs)
    assert ev["per_call_policy"] == {mod.NL: 1}  # 夹具里没写策略字段
    assert ev["n_compaction_applied"] == 0

    runs[0]["turns"][0]["tool_calls"][0].update(
        {"compact_policy": "cap", "compacted_chars": 30, "compact_applied": True}
    )
    ev = mod.policy_evidence(runs)
    assert ev["per_call_policy"] == {"cap": 1}
    assert ev["n_compaction_applied"] == 1
