"""xlam 适配器测试：钉住**误报防线**。

这个适配器的价值不在"能解析 JSON"，在"不该报的别报"。真实语料里脏的
形态和我们以为的不一样：同名工具重复注册、description 口头承诺默认值——
每一条都会让 naive 实现误判，而误判的方向总是"怪 agent"。
"""

from __future__ import annotations

from findata.agentops.adapters.xlam import (
    _as_list,
    _description_claims_default,
    inspect_call,
    required_params,
    to_trace,
)
from findata.agentops.schema import STATUS_ERROR, STATUS_OK
from findata.agentops.triage import CAT_SCHEMA_CONTRADICTION, diagnose, verdict

# permitivity 的 description 写着 "default is 8.854e-12" 但 schema 没标 default
# ——这是真实数据里被抓到的第一类问题，直接拿它做夹具。
_EF_FIELD = {
    "name": "calculate_electric_field",
    "description": "Calculate the electric field of a charge.",
    "parameters": {
        "charge": {"description": "the charge in coulombs", "type": "float"},
        "distance": {"description": "the distance in meters", "type": "float"},
        "permitivity": {
            "description": "the permitivity of the material (default is 8.854e-12)",
            "type": "float",
        },
    },
}


def test_required_params_means_no_default():
    assert required_params(_EF_FIELD) == ["charge", "distance", "permitivity"]


def test_description_claiming_default_is_detected():
    assert _description_claims_default(_EF_FIELD["parameters"]["permitivity"])
    assert not _description_claims_default(_EF_FIELD["parameters"]["charge"])


def test_clean_call_has_no_issues():
    call = {
        "name": "calculate_electric_field",
        "arguments": {"charge": 2, "distance": 3, "permitivity": 8.854e-12},
    }
    assert inspect_call(call, [_EF_FIELD]) == []


def test_default_is_not_passed_is_schema_contradiction_not_agent_fault():
    """没传的参数，但 description 说它有默认值 → 数据源自相矛盾。

    这一条是本项目在真实数据上最重要的一条判定：形态上和"agent 漏填必填
    参数"一模一样，结论完全相反。归错了，改进建议就是朝反方向使劲。
    """
    call = {"name": "calculate_electric_field", "arguments": {"charge": 2, "distance": 3}}
    # 去掉 permitivity 的 default 承诺后，同样没传就应该判 missing required
    strict = {
        **_EF_FIELD,
        "parameters": {
            **_EF_FIELD["parameters"],
            "permitivity": {"description": "the permitivity", "type": "float"},
        },
    }
    assert inspect_call(call, [_EF_FIELD]) == [
        "schema contradiction: permitivity 的 description 称有默认值，schema 未标 default"
    ]
    assert inspect_call(call, [strict]) == ["missing required: permitivity"]


def test_duplicate_tool_name_does_not_false_positive():
    """同名工具重复注册、schema 不同：任一份能解释调用就放行。

    真实语料里 `time_series` / `web_search` 都注册了两遍且参数不同。只取
    第一份核对会把"数据源重复注册"误判成"agent 传错参数"（实测 45 条误报）。
    """
    stock = {
        "name": "time_series",
        "description": "stock time series",
        "parameters": {"symbol": {"description": "ticker", "type": "str"}},
    }
    fx = {
        "name": "time_series",
        "description": "fx time series",
        "parameters": {
            "base": {"description": "base currency", "type": "str"},
            "symbols": {"description": "target", "type": "str"},
        },
    }
    call = {"name": "time_series", "arguments": {"base": "USD", "symbols": "XAU"}}
    assert inspect_call(call, [stock, fx]) == []
    # 反过来顺序也是干净的——不能依赖注册顺序
    assert inspect_call(call, [fx, stock]) == []
    # 两份都解释不了的，才报（报问题最少的那份）
    bad = {"name": "time_series", "arguments": {"nope": 1}}
    issues = inspect_call(bad, [stock, fx])
    assert "unknown argument: nope" in issues
    assert issues == ["missing required: symbol", "unknown argument: nope"]


def test_tool_not_found():
    call = {"name": "no_such_tool", "arguments": {}}
    assert inspect_call(call, [_EF_FIELD]) == ["tool not found: no_such_tool"]


def test_tools_and_answers_may_be_json_strings():
    """数据集里这两个字段是 JSON **字符串**，不是数组。"""
    assert _as_list('[{"name": "a"}]') == [{"name": "a"}]
    assert _as_list([{"name": "a"}]) == [{"name": "a"}]
    assert _as_list("not json") == []


def test_duplicate_call_is_flagged_separately():
    rec = {
        "query": "roll a die twice",
        "tools": [{"name": "roll", "description": "", "parameters": {}}],
        "answers": [{"name": "roll", "arguments": {}}, {"name": "roll", "arguments": {}}],
    }
    t = to_trace(rec, 0)
    assert len(t.steps) == 2
    assert t.steps[0].status == STATUS_OK
    assert t.steps[1].status == STATUS_ERROR
    assert "duplicate call" in t.steps[1].error


def test_golden_only_carries_provenance_not_labels():
    """golden 只放出处，不放结论——这批数据没有"哪条脏"的标签。"""
    rec = {"query": "q", "tools": [], "answers": [{"name": "x", "arguments": {}}]}
    g = to_trace(rec, 42).golden
    assert g["row"] == 42 and g["unlabeled"] is True
    assert "root_cause" not in g


def test_schema_contradiction_triage_and_verdict():
    """根因判对，且处置是丢弃——但丢弃不等于归责 agent。"""
    rec = {
        "query": "electric field of 2C at 3m",
        "tools": [_EF_FIELD],
        "answers": [
            {"name": "calculate_electric_field", "arguments": {"charge": 2, "distance": 3}}
        ],
    }
    t = to_trace(rec, 1)
    d = diagnose(t)
    assert d.category == CAT_SCHEMA_CONTRADICTION
    assert verdict(d) == "✗ 丢弃"
