"""findata HTTP API：把 service 层能力以 REST 形式暴露。

设计原则：
- 路由只负责参数解析、错误状态码、序列化；所有业务逻辑在 `findata.service`。
- 永远返回 JSON / 纯文本，绝不直接序列化 dataclass（Pydantic 模型兜底）。
- 健康检查独立于任何业务调用，永远 200 除非进程死了。
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from findata import __version__
from findata.config import settings
from findata.core.db import connect
from findata.observability import get_in_memory_exporter
from findata.semantic.catalog import MetricCatalog
from findata.semantic.tools import list_metrics, metric_query
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


@app.get("/v1/traces")
def get_traces(
    limit: int = Query(50, ge=1, le=500),  # noqa: B008
) -> dict[str, Any]:
    """最近 N 条 span 的快照。

    OTel 的 span 走 InMemorySpanExporter 收集；演示站 dashboard 可以从
    这里拿 trace 数据渲染「刚才发生了哪些调用 + 每个调用耗时多少」。
    生产里通常配 OTLP 把 span 推给 Jaeger / Tempo，但 5KB 演示站不引入
    那一层。
    """
    exporter = get_in_memory_exporter()
    if exporter is None:
        return {"enabled": False, "spans": []}
    spans = exporter.get_finished_spans()
    # 最新的在前面；O(N) 切片,演示量级不优化
    spans = sorted(spans, key=lambda s: s.end_time, reverse=True)[:limit]
    return {
        "enabled": True,
        "n_total": len(exporter.get_finished_spans()),
        "n_returned": len(spans),
        "spans": [
            {
                "name": s.name,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "duration_ms": int((s.end_time - s.start_time) / 1_000_000),
                "attributes": _safe_attrs(s.attributes),
                "status": s.status.status_code.name,
            }
            for s in spans
        ],
    }


# 兼容旧调用者：JSON 错误返回
@app.exception_handler(HTTPException)
def http_exc_handler(_, exc: HTTPException):  # type: ignore[no-untyped-def]
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _safe_attrs(attrs: Any) -> dict[str, Any]:
    """OTel attribute 可能是非 JSON 原生类型,这里只保留可序列化的部分。"""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


# ─────────────────────────── 可信问数 / 分析 路由（semantic + agent） ───────────────────────────
# 与上方监控路由并存：两套能力共享 M1 数据底座，但查询血缘独立落 query_run / verify_run。
# 设计要点：LLM 只选指标 + 填参，SQL 由语义层确定性渲染；每次查询自动交叉验证；
# 所有数字都带 run_id，/trace 可回追 SQL 与验证结论（"可信"叙事的对外接口）。

_CONN = None
_CATALOG = None


def _get_conn():
    """懒加载读写连接（查询需要写 query_run 血缘，故读写模式）。"""
    global _CONN, _CATALOG
    if _CONN is None:
        _CONN = connect(settings.dsn, read_only=False)
        _CATALOG = MetricCatalog()
    return _CONN, _CATALOG


class MetricRequest(BaseModel):
    metric: str
    params: dict[str, Any] | None = None


class AgentRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> dict[str, Any]:
    """分析链路存活探针（与监控 /healthz 并存，路径不冲突）。"""
    return {"status": "ok", "version": __version__}


@app.get("/metrics")
def metrics() -> list[dict[str, Any]]:
    """列出语义层所有可用指标。"""
    _, catalog = _get_conn()
    return list_metrics(catalog)


@app.post("/metric")
def query_metric(req: MetricRequest) -> dict[str, Any]:
    """直查指标：确定性 SQL 编译 + 参数绑定 + 交叉验证，返回带 run_id 的结果。"""
    conn, catalog = _get_conn()
    try:
        result = metric_query(conn, req.metric, req.params or {}, catalog)
    except Exception as exc:  # 语义层校验失败（未知指标/参数错）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload: dict[str, Any] = {
        "metric": result.metric,
        "label": result.label,
        "value": result.value,
        "unit": result.unit,
        "as_of": result.as_of,
        "source": result.source,
        "run_id": result.run_id,
        "params": result.params,
        "extra": result.extra,
    }
    if result.verification is not None:
        payload["verification"] = {
            "status": result.verification.status.value,
            "note": result.verification.note,
            "verify_run_id": result.verification.verify_run_id,
        }
    return payload


@app.post("/agent")
def agent_answer(req: AgentRequest) -> dict[str, Any]:
    """自然语言问答（LangGraph Agent）：选指标 → 填参 → 确定性查询 → 验证。"""
    from findata.agent.graph import answer_question

    conn, _ = _get_conn()
    try:
        answer = answer_question(conn, req.question)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Agent 调用失败（是否配置 FINDATA_LLM_API_KEY？）：{exc}",
        ) from exc
    return {"question": req.question, "answer": answer}


@app.post("/agent/stream")
def agent_stream(req: AgentRequest) -> StreamingResponse:
    """问答的 SSE 流式版本。"""
    from langchain_core.messages import HumanMessage

    from findata.agent.graph import build_agent

    conn, _ = _get_conn()

    async def event_source():
        try:
            agent = build_agent(conn)
            initial = {"messages": [HumanMessage(content=req.question)], "run_ids": []}
            async for chunk in agent.app.astream(initial, stream_mode="messages"):
                msg, _ = chunk
                if getattr(msg, "content", None):
                    text = msg.content
                    if isinstance(text, list):
                        text = "".join(
                            part.get("text", "") for part in text if isinstance(part, dict)
                        )
                    if text:
                        yield f"data: {json.dumps({'token': text}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/trace/{run_id}")
def trace(run_id: str) -> dict[str, Any]:
    """按 run_id 回溯查询血缘（SQL + 参数 + 交叉验证结论）。"""
    conn, _ = _get_conn()
    row = conn.execute(
        "SELECT metric, params, sql, value, started_at, finished_at "
        "FROM query_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run_id={run_id} 未找到")
    verify = conn.execute(
        "SELECT status, main_value, verify_value, note, run_id "
        "FROM verify_run WHERE query_run_id = ?",
        [run_id],
    ).fetchone()
    verification = None
    if verify is not None:
        verification = {
            "status": verify[0],
            "main_value": verify[1],
            "verify_value": verify[2],
            "note": verify[3],
            "verify_run_id": verify[4],
        }
    return {
        "run_id": run_id,
        "metric": row[0],
        "params": json.loads(row[1]) if row[1] else None,
        "sql": row[2],
        "value": row[3],
        "started_at": str(row[4]) if row[4] else None,
        "finished_at": str(row[5]) if row[5] else None,
        "verification": verification,
    }


def main() -> None:
    """findata-api console_scripts 入口。

    入口必须是函数而不能是 ASGI app 对象——console_scripts 直接调用目标，
    指向 app 对象会变成"调用 FastAPI 实例"而 TypeError。
    """
    import os

    import uvicorn

    uvicorn.run(
        "findata.api.app:app",
        host=os.environ.get("FINDATA_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("FINDATA_API_PORT", "8000")),
    )
