"""可信日报 Agent（R1.3）测试。

核心验收：审校节点对「无徽章数字」必须拦截——这是 ROADMAP
「引用无徽章数字必须被拦截」门禁的单测形态。
"""

from __future__ import annotations

from findata.agent.llm import LLMError, MockLLMClient
from findata.agent.nodes.report import (
    generate_report_section,
    market_facts,
    resolve_citation,
    review_and_enforce,
    template_draft,
)
from findata.dq.badges import BadgeLevel, build_badges
from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.report import Snapshot, run_inspection
from tests.test_report import _clean_snapshot


def _board():
    return run_inspection(_clean_snapshot()).badges


def test_cited_number_with_valid_badge_passes():
    text, notes = review_and_enforce("收盘 1700.00 元«stock_daily.close»。", _board())
    assert "■" not in text
    assert "⟦1700.00 元⟧" in text
    assert "stock_daily.close" in text
    assert notes == []


def test_uncited_number_is_intercepted():
    """验收门禁：引用无徽章数字必须被拦截。"""
    text, notes = review_and_enforce("贵州茅台收盘 1700.00 元，豪气。", _board())
    assert "1700.00" not in text
    assert "■" in text
    assert any("无徽章引用" in n for n in notes)


def test_ghost_citation_is_intercepted():
    """引用了不存在的指标 = 无徽章，数字必须被拦。"""
    text, notes = review_and_enforce("收盘 1700.00 元«ghost.key»。", _board())
    assert "1700.00" not in text
    assert "«ghost.key»" not in text
    assert "■" in text
    assert any("ghost.key" in n for n in notes)


def test_unusable_badge_citation_is_intercepted():
    """徽章是 ✗ 不可用 = 数字不可引用，引用了也拦。"""
    f = Finding(
        probe="uniqueness",
        table="stock_daily",
        symbol="600519",
        window="2024-03-01..2024-03-05",
        metric="duplicate_keys",
        value=3.0,
        threshold=1.0,
        severity_hint=Severity.P0,
    )
    d = Diagnosis(
        finding_key=f.key,
        root_cause=RootCause.DUPLICATE_WRITE,
        severity=Severity.P0,
        confidence=0.95,
        explanation="主键重复",
    )
    board = build_badges([f], [d], observed_tables={"stock_daily": True, "valuation_daily": True})
    assert board.of("stock_daily", "close").level is BadgeLevel.UNUSABLE
    text, notes = review_and_enforce("收盘 1700.00 元«stock_daily.close»。", board)
    assert "1700.00" not in text
    assert "■" in text


def test_dates_and_integer_counts_pass_through():
    """日期与不带单位的整数计数是元数据，不拦。"""
    text, notes = review_and_enforce(
        "观察日 2026-09-15，覆盖 25 只标的，健康分口径见 docs/badge.md。", _board()
    )
    assert "■" not in text
    assert notes == []


def test_template_draft_survives_review_without_interception():
    """确定性模板按契约引用，审校应当零拦截——这是契约的自我一致。"""
    snap = _clean_snapshot()
    result = run_inspection(snap)
    draft = template_draft(market_facts(snap), result.badges)
    final, notes = review_and_enforce(draft, result.badges)
    assert "■" not in final
    assert notes == []
    assert "⟦" in final and "〔✓" in final


def test_summary_health_score_citation_resolves():
    ok, mark = resolve_citation("summary.health_score", _board())
    assert ok
    assert "健康分" in mark
    ok, _ = resolve_citation("summary.nope", _board())
    assert not ok


def test_generate_section_without_llm_uses_template():
    """无 LLM（None）走确定性模板：llm_used=False、零成本、报告完整。"""
    snap = _clean_snapshot()
    result = run_inspection(snap)
    sec = generate_report_section(result, snap, llm_client=None)
    assert not sec.llm_used
    assert sec.usage.get("calls", 0) == 0
    assert "市场解读" in sec.md
    assert "■" not in sec.md
    assert "docs/badge.md" in sec.md
    # 可视化：合成语料数据足够，应产出图表 md（表格）与 html（SVG）
    assert "归一化" in sec.md
    assert "<svg" in sec.html


def test_generate_section_with_mock_llm_and_uncited_numbers():
    """LLM 乱写不带引用的数字：审校必须拦下，且成本如实入账。"""
    snap = _clean_snapshot()
    result = run_inspection(snap)
    mock = MockLLMClient(default="今日市场大涨 3.21%，成交 999.99 亿元，情绪高涨。")
    sec = generate_report_section(result, snap, llm_client=mock)
    assert sec.llm_used
    assert sec.usage["calls"] == 1
    assert sec.usage["chars_in"] > 0 and sec.usage["chars_out"] > 0
    # 数字只能出现在审校记录的引述里，正文不得出现
    body = sec.md.split("> 可信审校")[0]
    assert "3.21" not in body and "999.99" not in body
    assert body.count("■") >= 2
    assert any("无徽章引用" in n for n in sec.review_notes)


def test_llm_error_falls_back_to_template():
    """LLM 挂了降级模板，降级写进审校记录——日报不能因为模型失败而开天窗。"""

    class _Boom:
        @staticmethod
        def complete(system: str, user: str) -> str:
            raise LLMError("演练故障")

    snap = _clean_snapshot()
    result = run_inspection(snap)
    sec = generate_report_section(result, snap, llm_client=_Boom())  # type: ignore[arg-type]
    assert not sec.llm_used
    assert "■" not in sec.md
    assert any("分析降级" in n for n in sec.review_notes)
    assert "覆盖" in sec.md


def test_snapshot_synthetic_report_agent_graph_runs(snapshot: Snapshot = None):
    """LangGraph 报告链端到端（模板路径）：状态流转完整、审校在最后。"""
    snap = _clean_snapshot()
    result = run_inspection(snap)
    app = __import__(
        "findata.agent.nodes.report", fromlist=["build_report_graph"]
    ).build_report_graph(result, snap)
    state = app.invoke({"review_notes": [], "usage": {}})
    assert state["final_md"].startswith("## 市场解读")
    assert "draft" in state and "viz_md" in state
