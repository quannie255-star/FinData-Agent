"""M6 产品化：FastAPI 服务。

暴露三个能力：
  - 指标直查（无需 LLM）：POST /metric
  - 自然语言问答（LangGraph Agent）：POST /agent、POST /agent/stream(SSE)
  - 血缘溯源：GET /trace/{run_id}

设计要点：
  - DuckDB 连接单例化（模块加载时建立），查询链接 write 模式以支持 query_run 血缘落表。
  - /agent 依赖 LLM（FINDATA_LLM_API_KEY），无 key 时返回明确错误，不假装可用。
  - 所有数字都带 run_id，/trace 可追回 SQL 与参数 —— 这是「可信」叙事的对外接口。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from findata.config import settings
from findata.core.db import connect
from findata.semantic.catalog import MetricCatalog
from findata.semantic.tools import list_metrics, metric_query

# 模块级单例连接：查询需要写 query_run 血缘，故用读写模式
_CONN = connect(settings.dsn, read_only=False)
_CATALOG = MetricCatalog()


class MetricRequest(BaseModel):
    metric: str
    params: dict[str, Any] | None = None


class AgentRequest(BaseModel):
    question: str


def _build_app() -> FastAPI:
    app = FastAPI(
        title="Findata",
        version="0.6.0",
        description="可信金融数据分析 Agent：指标直查 + 自然语言问答 + 数字溯源",
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": "0.6.0"}

    @app.get("/metrics")
    def metrics() -> list[dict]:
        return list_metrics(_CATALOG)

    @app.post("/metric")
    def query_metric(req: MetricRequest) -> dict:
        try:
            result = metric_query(_CONN, req.metric, req.params or {}, _CATALOG)
        except Exception as exc:  # 语义层校验失败（未知指标/参数错）
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 结构化返回，前端可直接渲染
        payload = {
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
    def agent_answer(req: AgentRequest) -> dict:
        from findata.agent.graph import answer_question

        try:
            answer = answer_question(_CONN, req.question)
        except Exception as exc:
            # 常见根因：未配置 LLM key
            raise HTTPException(
                status_code=502,
                detail=f"Agent 调用失败（是否配置 FINDATA_LLM_API_KEY？）：{exc}",
            ) from exc
        return {"question": req.question, "answer": answer}

    @app.post("/agent/stream")
    def agent_stream(req: AgentRequest) -> StreamingResponse:
        from langchain_core.messages import HumanMessage

        from findata.agent.graph import build_agent

        agent = build_agent(_CONN)

        async def event_source():
            # 流式输出：用 langgraph 的 astream 拿 token 级增量
            try:
                initial = {
                    "messages": [HumanMessage(content=req.question)],
                    "run_ids": [],
                }
                async for chunk in agent._app.astream(initial, stream_mode="messages"):
                    # stream_mode="messages" 返回 (message_chunk, metadata) 元组
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
    def trace(run_id: str) -> dict:
        """按 run_id 回溯查询血缘（SQL + 参数 + 交叉验证结论）。"""
        row = _CONN.execute(
            "SELECT metric, params, sql, value, started_at, finished_at "
            "FROM query_run WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run_id={run_id} 未找到")

        verify = _CONN.execute(
            "SELECT status, main_value, verify_value, note, run_id "
            "FROM verify_run WHERE query_run_id = ?",
            [run_id],
        ).fetchone()
        if verify is not None:
            verification = {
                "status": verify[0],
                "main_value": verify[1],
                "verify_value": verify[2],
                "note": verify[3],
                "verify_run_id": verify[4],
            }
        else:
            verification = None

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

    return app


app = _build_app()
