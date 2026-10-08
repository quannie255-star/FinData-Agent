"""finqa Web 服务：FastAPI 后端 + 单页前端（无构建链，静态托管）。

设计原则（对齐 findata.api.app）：
- 路由只做参数解析与序列化，业务逻辑全部复用 finqa 各模块；
- **gold 不出后端**：/api/questions 与 /api/answer 的响应都剥离标准答案——
  演示页是「考官模式」，系统作答、人来看依据，不是刷题页；
- /api/answer 是真实 API 调用（慢、花钱）：文档与 UI 双重提示，不做任何
  mock 冒充实时；
- 运行归档只读，文件名白名单校验（防路径穿越）。

启动：`findata-finqa-app`（pyproject 注册）或
`uv run uvicorn findata.finqa.service:app --port 8100`。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from findata.finqa import fixture as fx
from findata.finqa.isolate import run_isolate
from findata.finqa.qwen import QwenClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"
STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNS_DIR = PROJECT_ROOT / "examples"

_RUN_NAME = re.compile(r"^[A-Za-z0-9._-]+\.json$")


def build_client() -> QwenClient:
    """独立函数便于测试替换（真实调用要 key；付费 API 不进 CI）。"""
    return QwenClient()


app = FastAPI(title="finqa demo", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@lru_cache(maxsize=1)
def _context() -> tuple[list, dict[str, str]]:
    questions = fx.load_questions(FIXTURE_ROOT)
    docs = fx.load_docs(FIXTURE_ROOT)
    fx.check_docs(questions, docs)
    return questions, docs


def _strip(q) -> dict:
    """gold/evidence 不出后端（考官模式）。"""
    return {
        "qid": q.qid,
        "domain": q.domain,
        "split": q.split,
        "question": q.question,
        "options": q.options,
        "answer_format": q.answer_format,
        "type": q.type,
        "has_doc_ids": bool(q.doc_ids),
    }


@app.get("/api/meta")
def meta() -> dict:
    questions, docs = _context()
    by_domain: dict[str, int] = {}
    for q in questions:
        by_domain[q.domain] = by_domain.get(q.domain, 0) + 1
    return {
        "questions": len(questions),
        "docs": len(docs),
        "by_domain": by_domain,
        "b_split": sum(1 for q in questions if not q.doc_ids),
    }


@app.get("/api/questions")
def list_questions(split: str | None = None) -> list[dict]:
    questions, _ = _context()
    if split:
        questions = [q for q in questions if q.split == split]
    return [_strip(q) for q in questions]


@app.post("/api/answer/{qid}")
def answer(qid: str) -> dict:
    """对该题跑一遍选项级隔离管线（真实 Qwen 调用，约 1-2 分钟）。"""
    questions, docs = _context()
    q = next((x for x in questions if x.qid == qid), None)
    if q is None:
        raise HTTPException(404, f"题目不存在：{qid}")
    try:
        client = build_client()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    details, ledger, _report, _skipped = run_isolate([q], docs, client)
    d = details[0]
    evidence = []
    for attr in d.get("attributions", []):
        if attr["purpose"].startswith("judge:") and ":retry" not in attr["purpose"]:
            for idx in attr["used_idx"]:
                doc_id, header, _ = attr["fed"][idx]
                evidence.append({"doc_id": doc_id, "clause": header})
    return {
        "qid": d["qid"],
        "answer": d["pred"],
        "verdicts": d["verdicts"],
        "evidence": evidence,
        "used_fallback": d["used_fallback"],
        "tokens": dict(zip(
            ("prompt", "completion", "total"),
            ledger.by_qid(qid),
            strict=True,
        )),
        "model": ledger.records[-1].model if ledger.records else "",
    }


@app.get("/api/runs")
def list_runs() -> list[dict]:
    """列出 examples/ 下的 AFAC 运行归档（只读）。"""
    runs = []
    for path in sorted(RUNS_DIR.glob("afac*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            runs_iter = (
                data["runs"].items()
                if isinstance(data, dict) and "runs" in data
                else iter(())
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        for mode, run in runs_iter:
            report = run.get("report", {})
            runs.append(
                {
                    "file": path.name,
                    "mode": mode,
                    "accuracy": report.get("accuracy"),
                    "total": report.get("total"),
                    "correct": report.get("correct"),
                    "total_tokens": report.get("total_tokens"),
                }
            )
    return runs


@app.get("/api/runs/{name}")
def run_detail(name: str) -> dict:
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "非法文件名")
    path = RUNS_DIR / name
    if not path.exists():
        raise HTTPException(404, f"归档不存在：{name}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
