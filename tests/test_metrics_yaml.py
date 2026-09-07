"""指标字典 (metrics.yaml) + 编译器 (compiler) 测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from findata.dq import (
    MetricRegistry,
    SqlTemplate,
    compile_metric,
    compile_sql_template,
    default_ctx,
)

METRICS_YAML = Path(__file__).parent.parent / "src" / "findata" / "dq" / "metrics.yaml"


def test_registry_loads_yaml():
    r = MetricRegistry.from_yaml(METRICS_YAML)
    names = r.names()
    # 7 个 probe + 至少 3 个 sql 模板
    assert len(names) >= 10
    assert "freshness" in names
    assert "row_count" in names


def test_registry_validate_required_fields():
    """必填字段缺一就 fail-fast。"""
    import yaml as _yaml

    bad = _yaml.safe_load(
        "metrics:\n  - name: x\n  - kind: probe\n"  # 缺 description / default_severity
    )
    with pytest.raises(ValueError, match="必填字段"):
        MetricRegistry(bad["metrics"])


def test_registry_validate_kind_enum():
    """kind 必须是 probe/sql, 其它值 reject。"""
    import yaml as _yaml

    bad_yaml = (
        "metrics:\n"
        "  - name: x\n"
        "    kind: python\n"
        "    description: y\n"
        "    default_severity: P1\n"
    )
    bad = _yaml.safe_load(bad_yaml)
    with pytest.raises(ValueError, match="必须是"):
        MetricRegistry(bad["metrics"])


def test_registry_validate_probe_module_required():
    """kind=probe 必须有 probe_module。"""
    import yaml as _yaml

    bad_yaml = (
        "metrics:\n"
        "  - name: x\n"
        "    kind: probe\n"
        "    description: y\n"
        "    default_severity: P1\n"
        "    probe_fn: foo\n"  # 缺 probe_module
    )
    bad = _yaml.safe_load(bad_yaml)
    with pytest.raises(ValueError, match="probe_module"):
        MetricRegistry(bad["metrics"])


def test_registry_by_kind():
    r = MetricRegistry.from_yaml(METRICS_YAML)
    sql_metrics = r.by_kind("sql")
    probe_metrics = r.by_kind("probe")
    assert all(m["kind"] == "sql" for m in sql_metrics)
    assert all(m["kind"] == "probe" for m in probe_metrics)
    assert len(probe_metrics) >= 5
    assert len(sql_metrics) >= 3


def test_registry_resolve_probe_fn():
    """kind=probe 的 metric 反射到 findata.dq.probes.<fn>。"""
    r = MetricRegistry.from_yaml(METRICS_YAML)
    fn = r.resolve_probe_fn("freshness")
    assert callable(fn)
    assert fn.__name__ == "probe_freshness"


def test_registry_resolve_probe_fn_rejects_sql():
    r = MetricRegistry.from_yaml(METRICS_YAML)
    with pytest.raises(ValueError, match="not probe"):
        r.resolve_probe_fn("row_count")


# ── compiler 测试 ──


def test_compile_simple_template():
    tpl = compile_sql_template("SELECT * FROM t WHERE date = $asof")
    assert tpl.names == ["asof"]
    sql, params = tpl.bind({"asof": date(2024, 1, 2)})
    assert sql == "SELECT * FROM t WHERE date = ?"
    assert params == [date(2024, 1, 2)]


def test_compile_multi_param_dedup():
    """同一占位出现多次只算一个, 按首次出现顺序。"""
    tpl = compile_sql_template("SELECT * FROM t WHERE date = $asof AND close > $asof")
    assert tpl.names == ["asof"]
    sql, params = tpl.bind({"asof": date(2024, 1, 2)})
    assert sql.count("?") == 2
    assert params == [date(2024, 1, 2), date(2024, 1, 2)]


def test_compile_multi_param_order():
    tpl = compile_sql_template("SELECT * FROM t WHERE date = $asof AND symbol = $sym")
    assert tpl.names == ["asof", "sym"]
    sql, params = tpl.bind({"asof": date(2024, 1, 2), "sym": "000001"})
    assert sql == "SELECT * FROM t WHERE date = ? AND symbol = ?"
    assert params == [date(2024, 1, 2), "000001"]


def test_compile_missing_ctx_raises():
    tpl = compile_sql_template("SELECT * FROM t WHERE date = $asof")
    with pytest.raises(KeyError, match="asof"):
        tpl.bind({})


def test_compile_metric_rejects_probe():
    r = MetricRegistry.from_yaml(METRICS_YAML)
    with pytest.raises(ValueError, match="kind=sql"):
        compile_metric(r.get("freshness"), default_ctx(asof=date(2024, 1, 2)))


def test_compile_metric_returns_bind_or_template():
    r = MetricRegistry.from_yaml(METRICS_YAML)
    m = r.get("row_count")
    # 不给 ctx → 返回 SqlTemplate (defer)
    tpl = compile_metric(m)
    assert isinstance(tpl, SqlTemplate)
    # 给 ctx → 返回 (sql, params)
    sql, params = compile_metric(m, default_ctx(asof=date(2024, 1, 2)))
    assert "?" in sql
    assert params == [date(2024, 1, 2)]


def test_default_ctx_today_when_none():
    ctx = default_ctx()
    assert ctx["asof"] == date.today()


def test_default_ctx_overrides():
    ctx = default_ctx(asof=date(2024, 1, 2), foo=42)
    assert ctx["asof"] == date(2024, 1, 2)
    assert ctx["foo"] == 42
