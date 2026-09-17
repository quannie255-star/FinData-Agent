"""告警推送测试：渠道分派、payload 形状、失败不打断、推送阈值。"""

from __future__ import annotations

from datetime import date

import pytest
import requests

from findata.config import settings
from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.report.inspect import Alert, InspectionResult, InspectionSummary
from findata.report.notify import notify_text, render_alert_message


def _finding(symbol: str = "600519") -> Finding:
    return Finding(
        probe="freshness",
        table="stock_daily",
        symbol=symbol,
        window="2024-03-01..2024-03-05",
        metric="stale_trading_days",
        value=3,
        threshold=1,
        severity_hint=Severity.P0,
    )


def _alert(cause: RootCause, severity: Severity, symbol: str = "600519") -> Alert:
    f = _finding(symbol)
    d = Diagnosis(
        finding_key=f.key,
        root_cause=cause,
        severity=severity,
        confidence=0.9,
        explanation="测试归因解释",
    )
    return Alert(finding=f, diagnosis=d, route="人工判断", action="复核")


def _result(alerts: list[Alert]) -> InspectionResult:
    summary = InspectionSummary(asof=date(2024, 3, 5), n_alerts=len(alerts), health_score=76.0)
    return InspectionResult(asof=date(2024, 3, 5), alerts=alerts, suppressed=[], summary=summary)


@pytest.fixture
def no_notify(monkeypatch):
    """强制无推送配置的干净基线（屏蔽本机 .env）。"""
    monkeypatch.setattr(settings, "notify_channel", "")
    monkeypatch.setattr(settings, "notify_webhook_url", "")


class TestRenderAlertMessage:
    def test_healthy_day_is_silent(self):
        assert render_alert_message(_result([])) == ""

    def test_p2_alone_is_silent(self):
        """P2 不值得打断人：只有 P2 时保持安静。"""
        msg = render_alert_message(_result([_alert(RootCause.UPSTREAM_STALE, Severity.P2)]))
        assert msg == ""

    def test_p0_renders_with_explanation(self):
        msg = render_alert_message(_result([_alert(RootCause.UPSTREAM_STALE, Severity.P0)]))
        assert "[P0]" in msg
        assert "600519" in msg and "upstream_stale" in msg

    def test_p1_grouped_into_summary_line(self):
        alerts = [
            _alert(RootCause.MISSING_ROWS, Severity.P1, "600519"),
            _alert(RootCause.MISSING_ROWS, Severity.P1, "000858"),
        ]
        msg = render_alert_message(_result(alerts))
        assert "[P1] 2 条" in msg


class TestNotifyText:
    def test_unconfigured_returns_notice_never_raises(self, no_notify):
        results = notify_text("标题", "内容")
        assert len(results) == 1 and "未配置" in results[0]

    def test_wecom_payload_filled(self, monkeypatch, no_notify):
        monkeypatch.setattr(settings, "notify_channel", "wecom")
        monkeypatch.setattr(settings, "notify_webhook_url", "https://example.invalid/hook")
        captured = {}

        class FakeResp:
            status_code = 200

        def fake_post(url, timeout=None, **kw):
            captured["url"], captured["json"] = url, kw.get("json")
            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        results = notify_text("巡检告警", "第一行\n第二行")
        assert results == ["wecom: OK"]
        assert captured["json"]["msgtype"] == "text"
        assert "巡检告警" in captured["json"]["text"]["content"]
        assert "第一行" in captured["json"]["text"]["content"]

    def test_serverchan_uses_form_data(self, monkeypatch, no_notify):
        monkeypatch.setattr(settings, "notify_channel", "serverchan")
        monkeypatch.setattr(settings, "notify_webhook_url", "https://sctapi.ftqq.com/KEY.send")
        captured = {}

        class FakeResp:
            status_code = 200

        def fake_post(url, timeout=None, **kw):
            captured.update(kw)
            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        notify_text("标题", "内容")
        assert "json" not in captured and "data" in captured
        assert captured["data"]["title"] == "标题"

    def test_network_failure_is_contained(self, monkeypatch, no_notify):
        monkeypatch.setattr(settings, "notify_channel", "wecom")
        monkeypatch.setattr(settings, "notify_webhook_url", "https://example.invalid/hook")

        def boom(*a, **kw):
            raise requests.ConnectionError("断网")

        monkeypatch.setattr(requests, "post", boom)
        results = notify_text("标题", "内容")
        assert len(results) == 1
        assert "推送失败" in results[0] and "不受影响" in results[0]

    def test_unknown_channel_reports(self, monkeypatch, no_notify):
        monkeypatch.setattr(settings, "notify_channel", "sms")
        monkeypatch.setattr(settings, "notify_webhook_url", "https://example.invalid")
        results = notify_text("标题", "内容")
        assert "未知推送渠道" in results[0]
