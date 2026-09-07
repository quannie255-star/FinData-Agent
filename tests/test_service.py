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
