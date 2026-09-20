"""字段级引用归因的测试。

这组用例的重点不是"函数能跑"，而是**钉住三条会让数字虚高的陷阱**：

1. 值在工具调用前就已在上下文里 → 出现也不算引用（否则引用率系统性虚高）
2. 大字段命中一个值就整段计入 → 必须靠 `min_hits` 或切段来控制
3. "没观测到"不等于"无用" → 三档分开计数，**不许合成一个"利用率"**

第 3 条尤其重要：一个合成的利用率数字会让"本批任务没覆盖"悄悄变成
"这个字段没用"，而那正是 R5.0 验收记录 §4.1 与 ROADMAP 五·2 明令禁止的写法。
"""

from __future__ import annotations

import pytest

from findata.contextbudget.attribution import (
    STATUS_NOT_REFERENCED,
    STATUS_REFERENCED,
    STATUS_UNVERIFIABLE,
    Attribution,
    FieldValue,
    attribute,
    is_distinctive,
    judge_field,
    value_tokens,
)

# --------------------------------------------------------------------------
# 抽取与区分度
# --------------------------------------------------------------------------


def test_value_tokens_extracts_paths_and_numbers():
    tokens = value_tokens("src/findata/agentops/triage.py 行 1284 共 21 个参数")
    assert "src/findata/agentops/triage.py" in tokens
    assert "1284" in tokens
    assert "21" in tokens


def test_short_and_common_values_are_not_distinctive():
    """短值/停用词没有区分度——拿它们的出现当引用证据是自欺。"""
    assert not is_distinctive("ok")
    assert not is_distinctive("src")
    assert not is_distinctive("3")  # 短纯数字，任何文本都会撞上
    assert not is_distinctive("read_file")
    assert is_distinctive("triage.py")
    assert is_distinctive("20240102")  # 长数字有区分度


# --------------------------------------------------------------------------
# 三档判定
# --------------------------------------------------------------------------


def test_unique_value_echoed_afterwards_is_referenced():
    fv = FieldValue("symbol", "RULES_VERSION")
    v = judge_field(fv, before="用户问：怎么改判定逻辑？", after="判定逻辑由 RULES_VERSION 控制。")
    assert v.status == STATUS_REFERENCED
    assert v.n_matched >= 1


def test_value_already_in_context_is_unverifiable_not_referenced():
    """**本文件最重要的一条。**

    值出现在回答里 ≠ 模型是从工具返回里读到的。若该值此前就在上下文里
    （用户提过、上一轮给过），那次出现**不构成引用证据**。
    不查这一条的话，引用率会因为共享词汇而系统性虚高。
    """
    fv = FieldValue("path", "src/findata/agentops/triage.py")
    v = judge_field(
        fv,
        before="请看一下 src/findata/agentops/triage.py 这个文件",
        after="src/findata/agentops/triage.py 里的 RULES_VERSION 需要改。",
    )
    assert v.status == STATUS_UNVERIFIABLE, "此前已知的值不得计为引用"
    assert "已在上下文里" in v.reason


def test_value_never_echoed_is_not_referenced_but_not_useless():
    fv = FieldValue("sha256", "9f2c1ab7e4d05c83")
    v = judge_field(fv, before="问：改判定逻辑要动什么？", after="需要修改 RULES_VERSION。")
    assert v.status == STATUS_NOT_REFERENCED
    # 措辞必须带上"不等于无用"，否则这个数字会被读成"该字段没用"
    assert "不等于该字段无用" in v.reason


def test_field_name_alone_is_not_a_reference():
    """模型说得出字段名，不等于用了它的值。这条钉住"不按名字放行"。"""
    fv = FieldValue("n_lines", "128456")
    v = judge_field(fv, before="", after="返回里有个 n_lines 字段，我没用它。")
    assert v.status == STATUS_NOT_REFERENCED, "字段名出现不得计为值被引用"


def test_all_values_preexisting_gives_unverifiable():
    fv = FieldValue("a", "alpha_beta")
    v = judge_field(fv, before="alpha_beta 是什么？", after="alpha_beta 就是那个东西。")
    assert v.status == STATUS_UNVERIFIABLE


# --------------------------------------------------------------------------
# 大字段的虚高风险（钉住这个限制本身，而不是假装它不存在）
# --------------------------------------------------------------------------


def test_large_field_with_one_hit_is_referenced_at_default_threshold():
    """**记录这个已知的粗粒度行为。**

    大字段里只要命中一个值，默认阈值下整段就算"被引用"——它可能只用了其中
    3 行，却让 15k 字符全部挂上"被使用"的标签。所以调用方对大字段必须
    抬 `min_hits` 或先切段。这条用例把这个行为**显式写下来**，
    免得数字虚高时没人知道是怎么来的。
    """
    big = " ".join(f"symbol_{i:03d}" for i in range(200))
    fv = FieldValue("content", big)
    v_default = judge_field(fv, before="", after="我只看 symbol_007 这一段。")
    a = Attribution([v_default])
    assert v_default.status == STATUS_REFERENCED
    assert a.n_referenced == 1
    # 命中 1 / 可判 200 —— 证据厚度一眼可见，不用猜
    assert v_default.n_matched == 1
    assert v_default.n_candidates >= 200


def test_min_hits_suppresses_thin_evidence():
    big = " ".join(f"symbol_{i:03d}" for i in range(200))
    v = judge_field(FieldValue("content", big), before="", after="我只看 symbol_007。", min_hits=5)
    assert v.status == STATUS_NOT_REFERENCED
    assert "低于本次阈值" in v.reason
    assert v.n_matched == 1, "命中数要如实保留，别因为没过阈值就抹掉"


# --------------------------------------------------------------------------
# 汇总：不许合成"利用率"
# --------------------------------------------------------------------------


def test_summary_keeps_three_buckets_and_does_not_invent_a_utilization_rate():
    fields = [
        FieldValue("used", "uniquefield_alpha"),  # 会被引用
        FieldValue("unused", "uniquefield_beta"),  # 不会
        FieldValue("tiny", "ok"),  # 不可判
    ]
    att = attribute(fields, before="", after="答案是 uniquefield_alpha 这个。")
    d = att.to_dict()
    assert (d["n_referenced"], d["n_not_referenced"], d["n_unverifiable"]) == (1, 1, 1)
    # 不许出现 "utilization" / "rate" / "ratio" 之类的合成键
    flat = " ".join(d.keys()).lower()
    assert "utiliz" not in flat and "rate" not in flat and "ratio" not in flat
    assert "不合成利用率" in d["note"]
    # 每个 verdict 都要带证据厚度
    for item in d["verdicts"]:
        assert "n_matched" in item and "n_candidates" in item


def test_unverifiable_is_excluded_from_both_sides():
    """不可判的字段**不计入分子也不计入分母**——否则就是编数。"""
    att = attribute([FieldValue("tiny", "ok")], before="", after="ok")
    assert (att.n_referenced, att.n_not_referenced, att.n_unverifiable) == (0, 0, 1)


@pytest.mark.parametrize("status", [STATUS_REFERENCED, STATUS_NOT_REFERENCED, STATUS_UNVERIFIABLE])
def test_every_verdict_carries_a_reason(status: str):
    """判定必须带原因：一个没有原因的 status 在报告里无法被质疑。"""
    fields = {
        STATUS_REFERENCED: FieldValue("f", "uniquefield_alpha"),
        STATUS_NOT_REFERENCED: FieldValue("f", "uniquefield_beta"),
        STATUS_UNVERIFIABLE: FieldValue("f", "ok"),
    }
    after = "uniquefield_alpha" if status == STATUS_REFERENCED else "无关内容"
    att = attribute([fields[status]], before="", after=after)
    v = att.verdicts[0]
    assert v.status == status
    assert v.reason, "每个判定都必须说明依据"
