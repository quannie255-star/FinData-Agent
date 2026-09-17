"""findata MCP 工具测试：直接调工具函数（不跨 stdio，依赖 SDK 已断言 wire 层）。"""

from __future__ import annotations

from findata.mcp.server import (
    findata_dashboard,
    findata_health_check,
    findata_trust_board,
    findata_trust_check,
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


# ─────────────────────── findata_metric_query（语义层可信查询） ───────────────────────


def _fixture_db(tmp_path, monkeypatch):
    """建合成 fixture 仓库并把 settings 指过去，返回路径。"""
    from scripts.build_eval_fixture import build

    from findata.config import settings

    db = tmp_path / "mcp_fixture.duckdb"
    build(str(db))
    monkeypatch.setattr(settings, "db_path", db)
    return db


def test_metric_query_returns_verified_result(tmp_path, monkeypatch):
    from findata.mcp.server import findata_metric_query

    _fixture_db(tmp_path, monkeypatch)
    payload = findata_metric_query("close_price", {"symbol": "600519"})
    assert "error" not in payload
    assert payload["value"] == 121.0
    assert payload["unit"] == "元"
    assert payload["run_id"]
    # 招牌能力必须真的在：交叉验证结论随结果返回
    assert payload["verification"]["status"] == "verified"


def test_metric_query_unknown_metric_returns_error(tmp_path, monkeypatch):
    from findata.mcp.server import findata_metric_query

    _fixture_db(tmp_path, monkeypatch)
    payload = findata_metric_query("no_such_metric", {"symbol": "600519"})
    assert "error" in payload


def test_metric_query_missing_warehouse(tmp_path, monkeypatch):
    from findata.config import settings
    from findata.mcp.server import findata_metric_query

    monkeypatch.setattr(settings, "db_path", tmp_path / "missing.duckdb")
    payload = findata_metric_query("close_price", {"symbol": "600519"})
    assert "error" in payload
    assert "不存在" in payload["error"]


def test_execute_metric_duckdb_source_connects(tmp_path, monkeypatch):
    """execute_metric(source=duckdb) 必须真的连上真实仓库，而不是 NotImplemented。"""
    import pytest

    from findata.config import settings
    from findata.mcp.server import _connect_for_execute

    _fixture_db(tmp_path, monkeypatch)
    conn = _connect_for_execute("duckdb", seed=0)
    try:
        n = conn.execute("SELECT count(*) FROM stock_daily").fetchone()[0]
        assert n > 0
    finally:
        conn.close()

    monkeypatch.setattr(settings, "db_path", tmp_path / "missing.duckdb")
    with pytest.raises(ValueError, match="不存在"):
        _connect_for_execute("duckdb", seed=0)


# ─────────────────────── findata_ask（可信问数多智能体，M12） ───────────────────────


def _fake_state(question: str) -> dict:
    from langchain_core.messages import AIMessage

    return {
        "messages": [AIMessage(content="茅台最新收盘价 121.0 元 [run_id=abcd1234] ✓")],
        "question": question,
        "mode": "direct",
        "run_ids": ["abcd1234"],
        "tool_rounds": 1,
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    }


def test_ask_returns_answer_with_lineage(tmp_path, monkeypatch):
    from findata.mcp.server import findata_ask

    _fixture_db(tmp_path, monkeypatch)
    import findata.agent.supervisor as sup

    monkeypatch.setattr(
        sup, "invoke_state", lambda conn, q: _fake_state(q)
    )
    payload = findata_ask(question="茅台最新收盘价是多少？")
    assert "error" not in payload
    assert payload["answer"].startswith("茅台最新收盘价")
    assert payload["mode"] == "direct"
    assert payload["run_ids"] == ["abcd1234"]
    assert payload["usage"]["total_tokens"] == 120


def test_ask_llm_failure_is_actionable(tmp_path, monkeypatch):
    """LLM 未配置时返回可行动错误（指明环境变量），而不是裸异常。"""
    from findata.mcp.server import findata_ask

    _fixture_db(tmp_path, monkeypatch)
    import findata.agent.supervisor as sup

    def _boom(conn, q):
        raise RuntimeError("分析链路 LLM 不可用：请配置 FINDATA_LLM_API_KEY")

    monkeypatch.setattr(sup, "invoke_state", _boom)
    payload = findata_ask(question="茅台最新收盘价是多少？")
    assert "FINDATA_LLM_API_KEY" in payload["error"]
    assert "hint" in payload


def test_ask_missing_warehouse(tmp_path, monkeypatch):
    from findata.config import settings
    from findata.mcp.server import findata_ask

    monkeypatch.setattr(settings, "db_path", tmp_path / "missing.duckdb")
    payload = findata_ask(question="茅台最新收盘价是多少？")
    assert "error" in payload and "不存在" in payload["error"]


# ─────────────────────── findata_trust_check / trust_board（增强模块） ───────────────────────


def test_trust_check_returns_usable_badge():
    payload = findata_trust_check("stock_daily", "close", source="synthetic", seed=20240102)
    assert "error" not in payload
    assert payload["badge"] in ("verified", "baseline", "caution", "unusable")
    assert isinstance(payload["usable"], bool)
    assert payload["summary"]
    assert payload["health_score"]


def test_trust_check_unknown_metric_returns_available_keys():
    payload = findata_trust_check("stock_daily", "ghost", source="synthetic")
    assert "error" in payload
    assert "stock_daily.close" in payload["error"]


def test_trust_check_rejects_unknown_source():
    payload = findata_trust_check("stock_daily", "close", source="oracle")
    assert "error" in payload


def test_trust_board_returns_all_badges():
    payload = findata_trust_board(source="synthetic", seed=20240102)
    assert len(payload["badges"]) == 12
    assert payload["counts"]["unusable"] + payload["counts"]["caution"] >= 1  # 注入语料必触发
    usable = [b for b in payload["badges"] if b["usable"]]
    assert all(b["badge"] in ("verified", "baseline") for b in usable)
