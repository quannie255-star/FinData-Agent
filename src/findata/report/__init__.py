"""巡检流水线与报告渲染。"""

from findata.report.dashboard import (
    DashboardData,
    TrendPoint,
    build_dashboard,
    render_dashboard,
)
from findata.report.inspect import (
    ROUTES,
    Alert,
    InspectionResult,
    InspectionSummary,
    Snapshot,
    benign_label,
    run_inspection,
)
from findata.report.render import render_html, render_markdown

__all__ = [
    "ROUTES",
    "Alert",
    "DashboardData",
    "InspectionResult",
    "InspectionSummary",
    "Snapshot",
    "TrendPoint",
    "benign_label",
    "build_dashboard",
    "render_dashboard",
    "render_html",
    "render_markdown",
    "run_inspection",
]
