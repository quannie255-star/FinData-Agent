"""钉死 `contextbudget.grading` 的**口径**（不是功能）。

沿用本项目的测试纪律：**测试钉死口径、不钉功能**。
所以这里每个用例断言的是"这条判据在什么情况下**不许**给出结论"，
而不是"它能判对多少"——后者取决于模型，不取决于我。

为什么这些"不许"值得单独立测试：每一条都对应一次真实的**误报**风险，
而误报的代价是把错的数字报出去。见 `docs/r5.0-acceptance.md` §4.5。
"""

from __future__ import annotations

import pytest

from findata.contextbudget.grading import (
    STRONG,
    has_exam_leak,
    judge,
    mcnemar_exact,
)
from findata.contextbudget.tasks import TASKS, Task

BY_ID = {t.id: t for t in TASKS}


def _t(task_id: str) -> Task:
    return BY_ID[task_id]


# --------------------------------------------------------------------------
# 覆盖完整性：判据不许有"沉默的空白"
# --------------------------------------------------------------------------


def test_every_task_has_a_strong_spec() -> None:
    """每题都必须有强判据定义。

    这一条防的是一个很容易犯的错：`judge` 对**没有** StrongSpec 的题返回
    `strong=False`（见下一条用例），所以漏写一题不会报错，只会静默地
    把所有答案判成"没答对"。没有这条测试，漏写和"模型全错"长得一模一样。
    """
    assert set(STRONG) == {t.id for t in TASKS}, (
        f"缺判据: {sorted({t.id for t in TASKS} - set(STRONG))}；"
        f"多判据: {sorted(set(STRONG) - {t.id for t in TASKS})}"
    )


def test_no_task_has_an_empty_alias_group() -> None:
    """别名组不能是空的，否则那一组永远命中不了（= 该题永远过不了）。"""
    for task_id, spec in STRONG.items():
        assert spec.groups, f"{task_id} 没有任何别名组"
        for group in spec.groups:
            assert group, f"{task_id} 有一个空的别名组"
            assert all(alt.strip() for alt in group), f"{task_id} 有空白别名"


# --------------------------------------------------------------------------
# 四条"不许通过"的路径
# --------------------------------------------------------------------------


def test_undefined_spec_cannot_pass() -> None:
    """没有强判据定义的题**不许**默认通过。

    否则"我没写判据"会被读成"模型答对了"——这是把工作量变成成绩。
    """
    ghost = Task(id="tZZ", question="?", must_contain=("x",), tags=("single",))
    res = judge(ghost, "随便答一句含有 x 的话", n_tool_calls=3, stopped_reason="answered")
    assert res.strong is False
    assert res.failed_by == "missing_fact"
    assert res.missing == ("<未定义强判据>",)


def test_zero_tool_calls_cannot_pass() -> None:
    """一次工具都没调 ⇒ 结构性失败（答对也是蒙的）。

    这道题（t10）的正确答案在仓库里；模型不查仓库却"答对了"，
    只能说明它记住了先验，而不是这次任务完成了。
    """
    res = judge(
        _t("t10"),
        "应走「🔧 待修 schema」档：这是数据源的错，不要丢样本",
        n_tool_calls=0,
        stopped_reason="answered",
    )
    assert res.strong is False
    assert res.failed_by == "no_lookup"


@pytest.mark.parametrize("stopped", ["max_turns", "error", "timeout", ""])
def test_abnormal_stop_cannot_pass(stopped: str) -> None:
    """没正常收尾就不算完成 —— 哪怕答案文本看起来对。"""
    res = judge(_t("t26"), "项目名 findata，版本 2.2.0", n_tool_calls=2, stopped_reason=stopped)
    assert res.strong is False
    assert res.failed_by == "no_lookup"


def test_empty_answer_cannot_pass() -> None:
    res = judge(_t("t26"), "   \n  ", n_tool_calls=2, stopped_reason="answered")
    assert res.strong is False
    assert res.failed_by == "empty"


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_normalise_ignores_whitespace_and_case() -> None:
    """折行/缩进/大小写不该影响判断——模型输出的折行是常态。"""
    res = judge(
        _t("t01"),
        "agentops 下有:\n  - adapters/xlam.py\n  - TRIAGE.PY",
        n_tool_calls=2,
        stopped_reason="answered",
    )
    assert res.strong is True
    assert res.failed_by == ""


def test_one_alias_in_a_group_suffices() -> None:
    """组内任一写法命中即算该组命中（别名组的意义就在这）。

    t09 的五组各给两个写法（符号 / 名称），这里只用**名称**这一路。
    """
    answer = "分五档：正样本 / 丢弃 / 需人工 / 不算失败 / 待修"
    res = judge(_t("t09"), answer, n_tool_calls=2, stopped_reason="answered")
    assert res.strong is True
    assert res.failed_by == ""


def test_partial_facts_are_reported_by_group() -> None:
    """缺哪一组要能指名道姓——报告里要写清"缺什么"，不能只说"没过"。"""
    res = judge(_t("t11"), "这个字段的 default 放错了位置", n_tool_calls=2,
                stopped_reason="answered")
    assert res.strong is False
    assert res.missing == ("错位",)


# --------------------------------------------------------------------------
# 判据泄漏（**这是本轮最重要的口径**）
# --------------------------------------------------------------------------


def test_exam_leak_vetoes_a_perfect_answer() -> None:
    """引用了考题定义的答案，即使**事实全中**也不许通过。

    背景：审计语料的根目录就是本仓库快照，而
    `src/findata/contextbudget/tasks.py` 里写着全部题目**和**每题的
    `must_contain`。模型读它 = 拿到答案卡。
    实测 active7b 臂 t23 就是这么过的：整段在讲 tasks.py（跑题），
    却引用了 `Task("t23", "…推导…", ("原文",), …)`。
    """
    quoted = '看这段：Task("t11", "缺陷种类?", ("错位",), tags=("cross",)) —— 即 default 错位'
    res = judge(_t("t11"), quoted, n_tool_calls=2, stopped_reason="answered")
    assert res.strong is False, "命中的是判据本身，不构成证据"
    assert res.failed_by == "leaked"
    assert res.leaked is True


def test_leak_is_checked_before_other_failures() -> None:
    """泄漏要优先报出来，否则"语料里放着答案卡"会被伪装成"模型没答对"。"""
    res = judge(_t("t01"), 'must_contain: ("xlam.py",)', n_tool_calls=0,
                stopped_reason="max_turns")
    assert res.failed_by == "leaked"


@pytest.mark.parametrize(
    "answer",
    [
        "agentops 下有 adapters/xlam.py、export.py、triage.py",
        "这个字段的 default 错位了，应该移过去而不是两边都补",
        "项目名 findata，版本 2.2.0",
    ],
)
def test_leak_fingerprint_does_not_accuse_normal_answers(answer: str) -> None:
    """反方向也要钉：**不许冤枉正常答案**。

    一个"泄漏"标记会直接把答案剔出统计 ⇒ 误标就是误删数据。
    所以指纹取窄（只有引用定义文件才会出现的形状）。
    """
    assert has_exam_leak(answer) is False


# --------------------------------------------------------------------------
# 已知的误判路径：**写出来，不藏**
# --------------------------------------------------------------------------


def test_known_false_positive_path_is_documented_not_hidden() -> None:
    """本判据**会**被跑题/拒答的答案蹭中，这条测试把它钉在案上。

    反例来自真实 trace：t22 的答案是拒答句
    「没有找到与 ungrounded_slot 探针相关的**防误报**措施的代码」，
    它命中了别名组里的宽泛词"防误报"。
    t22 另有"symbol"一组把它拦住了，但**没有任何结构性保证**别的题也拦得住。

    如果谁给别名组加了更好的判别力、这条测试变红，那是好事：
    届时应该**一起改**这里的断言和 `grading.py` 顶部 docstring 的
    "两条误判路径"，而不是删掉这条测试。
    """
    refusal = "在 agentops 的 probes.py 中没有找到与 ungrounded_slot 探针相关的防误报措施。"
    spec = STRONG["t22"]
    blob = refusal.lower()
    assert any(alt in blob for alt in spec.groups[1]), (
        "反例已失效：若 t22 的别名组不再被这句拒答命中，请更新本用例与 docstring"
    )


# --------------------------------------------------------------------------
# McNemar 的**分辨力下限**：比"不显著"强得多的那句话
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("only_a", "only_b", "expected_two", "expected_min"),
    [
        (0, 0, 1.0, 1.0),  # 无分歧
        (0, 1, 1.0, 1.0),  # k=1 ⇒ 最小可能 p 就是 1.0，**永远不可能显著**
        (0, 4, 0.125, 0.125),
        (0, 5, 0.0625, 0.0625),
        (0, 6, 0.03125, 0.03125),  # k=6 才第一次可能显著
    ],
)
def test_resolution_limit(only_a: int, only_b: int, expected_two: float,
                          expected_min: float) -> None:
    two, _one, min_p = mcnemar_exact(only_a, only_b)
    assert two == pytest.approx(expected_two)
    assert min_p == pytest.approx(expected_min)


def test_below_six_discordant_pairs_can_never_be_significant() -> None:
    """口径：**k<6 时任何配对结果都不可能显著**，与两臂实际差多少无关。

    这是"报不显著时必须一起报的分辨力"的算术依据。没有它，
    读者会把"p=0.125"读成"差不多"，而真相是"这个对照没有能力分辨"。
    """
    for k in range(0, 6):
        for only_a in range(k + 1):
            two, _one, _minp = mcnemar_exact(only_a, k - only_a)
            assert two >= 0.05, f"k={k} only_a={only_a} 竟然显著了"


def test_six_discordant_pairs_can_be_significant() -> None:
    """反方向：k=6 且全同向时，双侧 p 才第一次低于 0.05。"""
    two, _one, _minp = mcnemar_exact(0, 6)
    assert two < 0.05
