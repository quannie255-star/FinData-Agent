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
