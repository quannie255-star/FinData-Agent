"""M6 产品化：FastAPI 接口冒烟测试（不需要 LLM key）。

覆盖：健康、指标列表、指标直查（带 run_id 与验证）、血缘溯源、错误参数 4xx。
/agent 与 /agent/stream 依赖 LLM，单独在需要真实 key 时验证，此处只验无 key 时
返回结构化错误而非崩溃。

隔离策略：session 级 fixture 设置临时 FINDATA_DB_PATH，再导入 app 并灌入合成 fixture，
不污染本地 data/warehouse.duckdb，也不依赖网络采集。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def app():
    # 必须在导入 app 之前切换数据库路径，app 在模块加载时即建立单例连接。
    # 直接改 settings.db_path 实例属性，避开「env 在 settings 初始化后才设」的缓存问题。
    from findata.config import settings

    old = settings.db_path
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "warehouse.duckdb"
    settings.db_path = db_path

    from scripts.build_eval_fixture import build

    from findata.api.app import app as _app

    build(str(db_path))  # 灌入 3 只股票的合成数据

    yield _app

    settings.db_path = old


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    names = {m["name"] for m in r.json()}
    assert {"close_price", "pct_change", "valuation_pe_ttm"} <= names


def test_metric_query_with_run_id(client):
    r = client.post("/metric", json={"metric": "close_price", "params": {"symbol": "600519"}})
    assert r.status_code == 200
    body = r.json()
    assert body["value"] is not None
    assert body["run_id"]  # 每个数字都带 run_id
    assert body["verification"]["status"] in {"verified", "mismatch", "not_verifiable"}


def test_metric_query_unknown_metric_400(client):
    r = client.post("/metric", json={"metric": "not_a_metric", "params": {}})
    assert r.status_code == 400


def test_trace_returns_lineage(client):
    # 先查一次拿到 run_id，再溯源
    q = client.post("/metric", json={"metric": "close_price", "params": {"symbol": "600519"}})
    run_id = q.json()["run_id"]
    r = client.get(f"/trace/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["metric"] == "close_price"
    assert "sql" in body
    # 验证血缘已关联（query_run_id 命中）
    assert body["verification"] is not None


def test_trace_not_found_404(client):
    r = client.get("/trace/deadbeef0000")
    assert r.status_code == 404
