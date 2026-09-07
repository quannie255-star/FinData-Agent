"""findata MCP 工具测试：直接调工具函数（不跨 stdio，依赖 SDK 已断言 wire 层）。"""

from __future__ import annotations

from findata.mcp.server import (
    findata_dashboard,
    findata_health_check,
    findata_validate,
)


def test_health_check_returns_summary():
    payload = findata_health_check(source="synthetic", seed=20240102)
    assert payload["summary"]["health_score"] < 100
    assert isinstance(payload["alerts"], list)
    assert isinstance(payload["suppressed"], list)


def test_dashboard_returns_html_and_summary():
    payload = findata_dashboard(source="synthetic", days=5, with_eval=False, seed=20240102)
    assert payload["length"] == len(payload["html"])
    assert "数据质量看板" in payload["html"]
    assert payload["summary"]["summary"]["n_rows"] > 0


def test_validate_baseline_full_metrics():
    payload = findata_validate(seed=20240102, triage="baseline")
    rows = dict(payload["metrics"]["rows"])
    assert "100.0%" in rows["故障召回率"]
    assert "100.0%" in rows["根因准确率"]
    assert "triage_agreement" not in payload


def test_validate_both_includes_kappa():
    payload = findata_validate(seed=20240102, triage="both")
    a = payload["triage_agreement"]
    assert -1.0 <= a["kappa_root_cause"] <= 1.0
    assert a["n_compared"] > 0


def test_validate_rejects_unknown_triage():
    payload = findata_validate(seed=20240102, triage="magic")
    assert "error" in payload
