"""演示站一键启动：单进程同时 serve 静态站与 API。

端口：`PORT` 环境变量（默认 8080）。
- `GET /`              → `site/index.html`
- `GET /healthz`       → 健康探针
- `GET /v1/*`          → API
- `GET /<staticfile>`  → `site/` 下的静态资源

设计：演示站与 API 共用同一 FastAPI app，**不**用 mount 隔离——避免
mount 把 `/v1` 前缀再叠一层（mount 会让内部 app 的 `/v1/inspect`
变成 `/v1/v1/inspect`）。演示站路由只占根路径 + /assets，API 占 /v1/*，
二者天然不冲突。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from findata.api.app import app as api_app
from findata.api.app import healthz as api_healthz

PORT = int(os.environ.get("PORT", "8080"))


def _resolve_site_dir() -> Path:
    """定位 site/ 目录，容错 editable / wheel / frozen 多种 install 方式。

    解析顺序：
    1. `FINDATA_SITE_DIR` 环境变量（部署时显式覆盖，最稳）
    2. `__file__` 的 4 级上溯（`src/findata/cli/serve.py` 的 repo root）
    3. 从 `__file__` 向上 walk，找首个含 `index.html` 的 site/ 目录
    4. 都失败时退回第 2 步的猜测路径（请求时 read_text 会 500，比静默好）
    """
    env = os.environ.get("FINDATA_SITE_DIR")
    if env:
        return Path(env).resolve()

    here = Path(__file__).resolve()
    primary = here.parents[3] / "site"
    if (primary / "index.html").exists():
        return primary

    for ancestor in here.parents:
        cand = ancestor / "site"
        if (cand / "index.html").exists():
            return cand

    return primary


SITE_DIR = _resolve_site_dir()

# 拿 API 全部路由 + 演示站路由拼成单一进程
app = api_app

# 演示站静态资源（CSS/JS/SVG）放在 /assets 下
app.mount("/assets", StaticFiles(directory=SITE_DIR / "assets"), name="assets")


# 也提供 /v1/healthz 别名，避免两个 help 文档互相打架
@app.get("/v1/healthz", include_in_schema=False)
def healthz_alias():  # type: ignore[no-untyped-def]
    """同 api_app 的 /healthz，/v1 前缀版本，给对接按 prefix 分流的服务用。"""
    return api_healthz()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse((SITE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> PlainTextResponse:
    # 简单占位：站内不提供 favicon，避免 404 噪声
    return PlainTextResponse("", status_code=204)


# 演示用 dashboard 嵌入页：iframe 拉 /v1/dashboard
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(SITE_DIR / "dashboard.html")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "findata.cli.serve:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
