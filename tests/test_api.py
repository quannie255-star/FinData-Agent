"""findata HTTP API 测试。用 FastAPI 官方 TestClient（httpx 后端）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from findata.api.app import app

client = TestClient(app)


def test_healthz_is_200_with_version():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body and body["version"]


def test_inspect_synthetic_returns_json_alerts():
    r = client.post("/v1/inspect", json={"source": "synthetic", "seed": 20240102})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asof"]
    assert body["summary"]["health_score"] < 100  # 注入语料必触发
    assert isinstance(body["alerts"], list)
    assert isinstance(body["suppressed"], list)


def test_inspect_rejects_unknown_source():
    r = client.post("/v1/inspect", json={"source": "weather", "seed": 1})
    # Pydantic Literal validation 拒绝 → 422
    assert r.status_code in (400, 422)


def test_eval_baseline_reports_five_metrics():
    r = client.post("/v1/eval", json={"seed": 20240102, "triage": "baseline"})
    assert r.status_code == 200, r.text
    body = r.json()
    rows = dict(body["metrics"]["rows"])
    # 顶层评测指标都得在
    for key in (
        "故障召回率",
        "根因准确率",
        "分级准确率",
        "误报抑制率",
        "告警精确率",
    ):
        assert key in rows, key
    # 同一 seed 必得 100%
    assert "100.0%" in rows["故障召回率"]
    # no triage_agreement when not 'both'
    assert "triage_agreement" not in body


def test_eval_both_includes_kappa():
    r = client.post("/v1/eval", json={"seed": 20240102, "triage": "both"})
    assert r.status_code == 200, r.text
    body = r.json()
    a = body["triage_agreement"]
    assert "kappa_root_cause" in a
    assert "kappa_suppress" in a
    assert a["n_compared"] > 0
    # Cohen's κ 必落入 [-1, 1]
    assert -1.0 <= a["kappa_root_cause"] <= 1.0


def test_render_markdown_returns_text():
    r = client.post("/v1/render", json={"source": "synthetic", "fmt": "md", "seed": 20240102})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "观察日" in r.text


def test_dashboard_returns_html_with_meta():
    r = client.get("/v1/dashboard", params={"source": "synthetic", "days": 5})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "数据质量看板" in r.text
    assert 'name="findata-summary"' in r.text
    # meta 里 JSON 可解
    import re
    m = re.search(r"content='(\{.*?\})'", r.text, re.S)
    assert m is not None
    parsed = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    assert "asof" in parsed


# ─────────────────────── 可信问数 /agent（M12 多智能体） ───────────────────────


def test_agent_endpoint_defaults_to_multi_arch(monkeypatch):
    """/agent 默认走多智能体；supervisor.answer_question 被调用且结果透传。"""
    import findata.agent.supervisor as sup

    called = {}

    def _fake(conn, question):
        called["arch"] = "multi"
        return "121.0 元 [run_id=abcd1234] ✓"

    monkeypatch.setattr(sup, "answer_question", _fake)
    r = client.post("/agent", json={"question": "茅台最新收盘价是多少？"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"].startswith("121.0")
    assert called["arch"] == "multi"


def test_agent_endpoint_can_switch_to_single(monkeypatch):
    """FINDATA_AGENT_ARCH=single 切回单 Agent 基线（对比评测的对照组入口）。"""
    import findata.agent.graph as g

    called = {}

    def _fake(conn, question):
        called["arch"] = "single"
        return "single arch answer"

    monkeypatch.setattr(g, "answer_question", _fake)
    monkeypatch.setenv("FINDATA_AGENT_ARCH", "single")
    r = client.post("/agent", json={"question": "问题"})
    assert r.status_code == 200, r.text
    assert called["arch"] == "single"


def test_agent_arch_helper_default():
    from findata.api.app import _agent_arch

    assert _agent_arch() in ("multi", "single")


# ─────────────────────── /v1/trust（增强模块接入面） ───────────────────────


def test_trust_check_returns_badge_for_known_metric():
    r = client.get(
        "/v1/trust", params={"table": "stock_daily", "metric": "close", "source": "synthetic"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == "stock_daily"
    assert body["metric"] == "close"
    assert body["badge"] in ("verified", "baseline", "caution", "unusable")
    assert isinstance(body["usable"], bool)
    assert "health_score" in body and "evidence" in body


def test_trust_check_unknown_metric_is_404_with_known_keys():
    r = client.get(
        "/v1/trust", params={"table": "stock_daily", "metric": "ghost", "source": "synthetic"}
    )
    assert r.status_code == 404
    assert "stock_daily.close" in r.json()["error"]  # 错误信息带可用键，调用方可自愈


def test_trust_board_returns_full_badge_board():
    r = client.get("/v1/trust/board", params={"source": "synthetic"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["badges"]) == 12  # stock_daily 8 列 + valuation_daily 4 列
    assert set(body["counts"]) == {"verified", "baseline", "caution", "unusable"}
    keys = {b["key"] for b in body["badges"]}
    assert "stock_daily.close" in keys and "valuation_daily.total_mv" in keys
