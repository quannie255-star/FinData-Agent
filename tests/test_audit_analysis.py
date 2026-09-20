"""对照实验的统计部分必须钉住 —— 手写实现，没有 scipy 兜底。

为什么这个文件值得存在
----------------------
符号检验的 p 值是 R5.2 头条数字的组成部分（"工具膨胀的代价是否显著"）。
一个手写的组合数求和如果错了，报告会照样打印一个 4 位小数的 p 值，
**而且看起来完全正常** —— 这正是必须进单测的那一类错误：
它不会让任何东西变红，只会让结论悄悄错掉。

本文件的主体是**与二项分布精确值逐项对齐**的参数化用例，不是"跑一遍不出错"。

另一个钉住的东西：脚本里的 p 值必须**不依赖 scipy**。本项目 dev 依赖里没有
scipy，如果哪天有人改成 `from scipy.stats import binomtest`，CI 会 ImportError
—— 那属于环境差异，此处用"能不能 import 脚本本身"间接覆盖。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_audit_arms.py"


@pytest.fixture(scope="module")
def arms() -> Any:
    """按路径加载分析脚本（scripts/ 不是包，只能这样 import）。"""
    spec = importlib.util.spec_from_file_location("_compare_audit_arms", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 期望值全部写成**精确二项式**（2 × P(X ≤ min)，X ~ Bin(n, 1/2)），
# 不写小数——写成小数就等于把"我当时算出来的数"当成了事实来源。
@pytest.mark.parametrize(
    ("n_pos", "n_neg", "expected"),
    [
        (10, 0, 2 * 1 / 2**10),  # 全同向，n=10
        (9, 1, 2 * 11 / 2**10),  # C(10,0)+C(10,1)=11
        (8, 2, 2 * 56 / 2**10),  # 1+10+45=56
        (7, 2, 2 * 46 / 2**9),  # n=9：1+9+36=46
        (6, 0, 2 * 1 / 2**6),
        (5, 0, 2 * 1 / 2**5),  # 0.0625：小样本的经典临界值
        (4, 0, 2 * 1 / 2**4),  # 0.125
        (3, 3, 1.0),  # 无方向 → 截到 1，不得报"显著"
        (0, 0, 1.0),  # 没有可用配对 → 必须返回 1，不能返回 0
    ],
)
def test_sign_test_matches_exact_binomial(
    arms: Any, n_pos: int, n_neg: int, expected: float
) -> None:
    assert arms.sign_test_p(n_pos, n_neg) == pytest.approx(expected)


def test_sign_test_is_symmetric(arms: Any) -> None:
    """哪一臂在前不该影响 p —— 否则报告结论会被参数顺序左右。"""
    for a, b in [(7, 2), (5, 0), (4, 4), (9, 1)]:
        assert arms.sign_test_p(a, b) == pytest.approx(arms.sign_test_p(b, a))


def test_sign_test_never_exceeds_one(arms: Any) -> None:
    """双侧尾概率要截到 1。不截的话 (4,4) 会报出 >1 的 p 值。"""
    for a in range(0, 12):
        for b in range(0, 12):
            p = arms.sign_test_p(a, b)
            assert 0.0 < p <= 1.0, f"p({a},{b}) = {p}"


def test_sign_test_gets_more_significant_with_consistency(arms: Any) -> None:
    """同向比例固定时，样本越多越显著 —— 方向对了才值得看具体数值。"""
    p_small = arms.sign_test_p(6, 0)
    p_big = arms.sign_test_p(12, 0)
    assert p_big < p_small


def test_no_shared_tasks_is_reported_not_silently_odd(arms: Any, tmp_path: Path) -> None:
    """两臂没有可配对任务时必须**报错退出**，不能打一堆 0 然后说"无差异"。

    这是"沉默失败"的典型：0 差值分不清"真的没差别"和"根本没比上"。
    """
    import json

    (tmp_path / "runs-a.jsonl").write_text(
        json.dumps({"task": "t1", "turns": [], "final_answer": ""}) + "\n", encoding="utf-8"
    )
    (tmp_path / "runs-b.jsonl").write_text(
        json.dumps({"task": "t2", "turns": [], "final_answer": ""}) + "\n", encoding="utf-8"
    )
    for tag in ("a", "b"):
        (tmp_path / f"bill-{tag}.json").write_text(
            json.dumps(
                {
                    "scope": {"n_tasks": 1, "n_tools_in_list": 1, "tools_chars_per_call": 1},
                    "measured": {
                        "keyword_hits": 0,
                        "total_tool_calls": 0,
                        "n_answered": 0,
                        "n_errored": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
    argv = ["prog", "--dir", str(tmp_path), "--arm-a", "a", "--arm-b", "b"]
    import sys

    old = sys.argv
    sys.argv = argv
    try:
        rc = arms.main()
    finally:
        sys.argv = old
    assert rc == 1, "没有可配对任务时必须以非 0 退出"
