"""findata service 层单测：装快照、跑巡检、看板装配、序列化。"""

from __future__ import annotations

import json
from datetime import date

from findata.report.inspect import Snapshot
from findata.service import (
    build_dashboard_html,
    render_report,
    result_to_json,
)
from findata.service import (
    inspect as svc_inspect,
)


def _synthetic(asof: date | None = None) -> Snapshot:
    return Snapshot.from_synthetic(seed=20240102)


def test_inspect_synthetic_returns_summary_with_alerts():
    result = svc_inspect("synthetic")
    assert result.summary.n_symbols > 0
    # 我们的合成语料注入固定种子下，有告警也有抑制
    assert result.summary.n_findings > 0
    assert result.summary.health_score < 100


def test_inspect_with_asof_replays_history():
    end = _synthetic().asof
    mid = date.fromisoformat("2024-06-15")
    later = svc_inspect("synthetic", asof=end)
    earlier = svc_inspect("synthetic", asof=mid)
    assert later.summary.asof > earlier.summary.asof
    # 早期发现数应当不多于晚期（数据累积），允许相等（早段可能都是基线正常）
    assert earlier.summary.n_rows <= later.summary.n_rows


def test_render_report_md_contains_summary():
    body, media = render_report("synthetic", None, "md", seed=20240102)
    assert media.startswith("text/markdown")
    assert "观察日" in body
    assert "健康分" in body


def test_render_report_html_is_well_formed():
    body, media = render_report("synthetic", None, "html", seed=20240102)
    assert media.startswith("text/html")
    assert body.lstrip().startswith("<!DOCTYPE html>") or body.lstrip().startswith("<html")


def test_build_dashboard_html_returns_self_contained_page():
    html, current = build_dashboard_html("synthetic", None, days=5, with_eval=False, seed=20240102)
    assert "数据质量看板" in html
    assert current.summary.n_rows > 0
    # meta 注入是 API 层职责——service 层只产出纯净 HTML
    assert 'name="findata-summary"' not in html


def test_result_to_json_is_jsonable_roundtrip():
    result = svc_inspect("synthetic")
    payload = result_to_json(result)
    text = json.dumps(payload, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["asof"] == result.asof.isoformat()
    assert "summary" in parsed
    assert "alerts" in parsed
    assert "suppressed" in parsed


# ─────────────────────── duckdb 源：必须接真实仓库或显式报错 ───────────────────────


def _build_tiny_warehouse(db_path):
    """最小真实仓库：1 只股票 2 个交易日，足以让巡检读到行。"""
    import pandas as pd

    from findata.core.db import connect, upsert_dataframe

    conn = connect(str(db_path))
    daily = pd.DataFrame(
        {
            "symbol": ["600519"] * 2,
            "date": [pd.to_datetime("2024-01-02").date(), pd.to_datetime("2024-01-03").date()],
            "open": [100.0, 110.0],
            "high": [101.0, 111.0],
            "low": [99.0, 109.0],
            "close": [100.0, 110.0],
            "volume": [1e6, 1e6],
            "amount": [1e8, 1.1e8],
            "pct_chg": [0.0, 10.0],
            "turnover": [1.0, 1.0],
        }
    )
    upsert_dataframe(conn, "stock_daily", daily, ["symbol", "date"])
    universe = pd.DataFrame(
        {
            "symbol": ["600519"],
            "name": ["贵州茅台"],
            "industry": ["白酒"],
            "listed_board": ["main"],
            "list_date": [pd.to_datetime("2001-08-27").date()],
        }
    )
    upsert_dataframe(conn, "stock_universe", universe, ["symbol"])
    conn.close()


def test_inspect_duckdb_missing_warehouse_raises(tmp_path):
    """仓库缺失必须显式报错——巡检空库给 100 分是"可信"项目最不该有的失败模式。"""
    import pytest

    with pytest.raises(FileNotFoundError, match="ingest_finance"):
        svc_inspect("duckdb", db_path=str(tmp_path / "nope.duckdb"))


def test_inspect_duckdb_real_warehouse(tmp_path):
    db = tmp_path / "real.duckdb"
    _build_tiny_warehouse(db)
    result = svc_inspect("duckdb", db_path=str(db))
    assert result.summary.n_rows > 0
    assert result.summary.n_symbols >= 1


def test_eval_reports_llm_backend(monkeypatch):
    """无 key 时 llm_backend 必须如实标 mock——κ 是"规则 vs 静默桩"，不是"规则 vs 真模型"。"""
    from findata.config import settings
    from findata.service import eval_to_json, evaluate

    monkeypatch.setattr(settings, "llm_api_key", "")
    result = evaluate(seed=20240102, triage="both")
    assert result.llm_backend == "mock"
    payload = eval_to_json(result)
    assert payload["llm_backend"] == "mock"
    assert "triage_agreement" in payload
