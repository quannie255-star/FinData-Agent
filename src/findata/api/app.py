"""findata HTTP API：把 service 层能力以 REST 形式暴露。

设计原则：
- 路由只负责参数解析、错误状态码、序列化；所有业务逻辑在 `findata.service`。
- 永远返回 JSON / 纯文本，绝不直接序列化 dataclass（Pydantic 模型兜底）。
- 健康检查独立于任何业务调用，永远 200 除非进程死了。
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from findata import __version__
from findata.service import (
    build_dashboard_html,
    eval_to_json,
    inspect,
    render_report,
    result_to_json,
)

Source = Literal["synthetic", "duckdb"]
TriageKind = Literal["baseline", "llm", "both"]
ReportFmt = Literal["md", "html"]


app = FastAPI(
    title="findata",
    version=__version__,
    description="数据质量监控 Agent：异常探测 + 根因归因 + 误报抑制 + 评测门禁",
)

# 演示站嵌入用：CORS 全开。M6 是演示而非生产，省一道。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─────────────────────────── 响应模型 ───────────────────────────


class InspectRequest(BaseModel):
    source: Source = "synthetic"
    asof: _date | None = Field(default=None, description="回放的观察日（YYYY-MM-DD）")
    seed: int = Field(default=20240102, ge=1)


class EvalRequest(BaseModel):
    seed: int = Field(default=20240102, ge=1)
    triage: TriageKind = "baseline"


class RenderRequest(BaseModel):
    source: Source = "synthetic"
    asof: _date | None = None
    fmt: ReportFmt = "md"
    seed: int = Field(default=20240102, ge=1)


# ─────────────────────────── 路由 ───────────────────────────


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """轻量存活探针；不触发任何巡检，避免被 LB 当业务流量算进成本。"""
    return {"ok": True, "version": __version__, "now": datetime.now().isoformat(timespec="seconds")}


@app.post("/v1/inspect")
def post_inspect(req: InspectRequest) -> dict[str, Any]:
    """跑一次巡检，返回 JSON 摘要 + 告警/抑制明细。"""
    try:
        result = inspect(req.source, req.asof, req.seed)
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，反回 4xx 业务错
        raise HTTPException(status_code=400, detail=f"inspect failed: {exc}") from exc
    return result_to_json(result)


@app.post("/v1/eval")
def post_eval(req: EvalRequest) -> dict[str, Any]:
    """跑评测。triage=both 时同时跑 baseline + llm，并输出 Cohen's κ。"""
    try:
        result = evaluate_safe(req.seed, req.triage)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"eval failed: {exc}") from exc
    return eval_to_json(result)


@app.post("/v1/render")
def post_render(req: RenderRequest) -> PlainTextResponse:
    """单日报告：md 或 html。"""
    try:
        body, media = render_report(req.source, req.asof, req.fmt, req.seed)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"render failed: {exc}") from exc
    return PlainTextResponse(content=body, media_type=media)


@app.get("/v1/dashboard")
def get_dashboard(
    source: Source = Query("synthetic"),  # noqa: B008
    asof: _date | None = Query(None),  # noqa: B008
    days: int = Query(20, ge=1, le=180),  # noqa: B008
    with_eval: bool = Query(False),  # noqa: B008
    seed: int = Query(20240102, ge=1),  # noqa: B008
) -> PlainTextResponse:
    """组装单页看板 HTML（无 CDN、无外部依赖）。"""
    try:
        html, current = build_dashboard_html(source, asof, days, with_eval, seed)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"dashboard failed: {exc}") from exc
    # 把 current 摘要注入 <head>，方便演示站用 JS 取出做侧边栏
    injected = _inject_meta(html, result_to_json(current))
    return PlainTextResponse(content=injected, media_type="text/html; charset=utf-8")


# ─────────────────────────── 内部 ───────────────────────────


def evaluate_safe(seed: int, triage: TriageKind):
    """service.evaluate 的薄层，把 import 隔离在这里以便单测 monkey-patch。"""
    from findata.service import evaluate as _evaluate

    return _evaluate(seed=seed, triage=triage)


def _inject_meta(html: str, meta: dict[str, Any]) -> str:
    """把 current 结果的 JSON 摘要注入 <head>，便于前端消无声读取。"""
    import json

    payload = json.dumps(meta, ensure_ascii=False)
    tag = f'<meta name="findata-summary" content=\'{payload}\' />'
    # 防御：只插入一次，避免重复调用打膨胀
    if 'name="findata-summary"' in html:
        return html
    return html.replace("</head>", f"  {tag}\n</head>", 1)


# 兼容旧调用者：JSON 错误返回
@app.exception_handler(HTTPException)
def http_exc_handler(_, exc: HTTPException):  # type: ignore[no-untyped-def]
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
