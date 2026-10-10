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
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from findata.config import settings
from findata.finqa import fixture as fx
from findata.finqa.embedding import EmbeddingUnavailable, build_embedder
from findata.finqa.isolate import run_isolate
from findata.finqa.personal import ask_free, convert_file, embed_model_for, safe_stem
from findata.finqa.qwen import QwenClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "eval" / "fixtures" / "afac_scaffold"
STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNS_DIR = PROJECT_ROOT / "examples"

# 个人文档库（用户私有数据，gitignore）：documents/ 原件，converted/ 转换后 markdown
USERDATA_DIR = PROJECT_ROOT / "findata_userdata"
USER_DOCS_DIR = USERDATA_DIR / "documents"
USER_CONVERTED_DIR = USERDATA_DIR / "converted"
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024

_RUN_NAME = re.compile(r"^[A-Za-z0-9._-]+\.json$")
_DOC_ID = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,80}$")


def build_client() -> QwenClient:
    """独立函数便于测试替换（真实调用要 key；付费 API 不进 CI）。"""
    return QwenClient()


def build_embedder_or_none(docs: dict[str, str]):
    """混合检索开关（FINDATA_FINQA_HYBRID_RETRIEVAL，默认关）。

    产品口径（docs/next-cycle-roadmap.md §3.1.1）：embedding 只进个人版
    自由问答；后端不可用返回 None（ask_free 走 BM25F 并在响应里如实标注），
    不让检索基建的缺失挡住问答本身。
    """
    if not settings.finqa_hybrid_retrieval:
        return None
    try:
        return build_embedder(
            backend=settings.finqa_embed_backend,
            model=embed_model_for(docs, settings.finqa_embed_model),
            cache_dir=settings.finqa_embed_cache_dir,
        )
    except EmbeddingUnavailable:
        return None


app = FastAPI(title="finqa demo", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> PlainTextResponse:
    # 站内不提供 favicon，返回 204 避免访问日志被 404 刷屏（同 findata.cli.serve 的占位做法）
    return PlainTextResponse("", status_code=204)


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


# ── 个人文档库（自由问答；用户私有数据，findata_userdata/）──


def _load_user_docs() -> dict[str, str]:
    return {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(USER_CONVERTED_DIR.glob("*.md"))
    }


@app.get("/api/library")
def list_library() -> list[dict]:
    USER_CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(USER_CONVERTED_DIR.glob("*.md")):
        original = USER_DOCS_DIR / (path.stem + _original_suffix(path.stem))
        out.append(
            {
                "doc_id": path.stem,
                "chars": path.stat().st_size,
                "has_original": original.exists(),
            }
        )
    return out


def _original_suffix(stem: str) -> str:
    """找回原件扩展名（同一 stem 可能多格式，取先命中者）。"""
    for suffix in (".pdf", ".docx", ".txt", ".md"):
        if (USER_DOCS_DIR / (stem + suffix)).exists():
            return suffix
    return ""


@app.post("/api/library/upload")
def upload(
    file: Annotated[UploadFile, File()] = None,
    mask_pii: str = Form("false"),
) -> dict:
    """上传文档；``mask_pii=true`` 时对转换后的检索文本做 PII 脱敏（原件不动）。"""
    if file is None:
        raise HTTPException(400, "缺少文件")

    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    stem = safe_stem(file.filename)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".pdf", ".docx", ".txt", ".md"):
        raise HTTPException(400, f"不支持的格式：{suffix}（支持 pdf/docx/txt/md）")
    content = file.file.read()
    if len(content) > UPLOAD_LIMIT_BYTES:
        raise HTTPException(413, "文件超过 50MB 上限")
    USER_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    original = USER_DOCS_DIR / f"{stem}{suffix}"
    original.write_bytes(content)
    try:
        converted = convert_file(
            original,
            USER_DOCS_DIR,
            USER_CONVERTED_DIR,
            mask_pii=mask_pii.strip().lower() in ("1", "true", "yes", "on"),
        )
    except ValueError as exc:
        original.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    return {
        "doc_id": converted.stem,
        "chars": converted.stat().st_size,
        "status": "converted",
    }


@app.delete("/api/library/{doc_id}")
def delete_doc(doc_id: str) -> dict:
    if not _DOC_ID.match(doc_id):
        raise HTTPException(400, "非法文档编号")
    removed = []
    converted = USER_CONVERTED_DIR / f"{doc_id}.md"
    if converted.exists():
        converted.unlink()
        removed.append("converted")
    original = USER_DOCS_DIR / (doc_id + _original_suffix(doc_id))
    if original.exists():
        original.unlink()
        removed.append("original")
    if not removed:
        raise HTTPException(404, f"文档不存在：{doc_id}")
    return {"doc_id": doc_id, "removed": removed}


@app.post("/api/ask-free")
def ask_free_endpoint(payload: dict) -> dict:
    """自由问答：对个人文档库检索并作答（真实 Qwen 调用，约 10-30 秒）。"""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    if len(question) > 2000:
        raise HTTPException(400, "问题过长（上限 2000 字符）")
    docs = _load_user_docs()
    if not docs:
        raise HTTPException(400, "文档库为空：先在「我的文档」上传文件")
    try:
        client = build_client()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return ask_free(question, docs, client, embedder=build_embedder_or_none(docs))


def main() -> int:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
