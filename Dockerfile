# findata 演示镜像（slim）：单容器同时跑 API（uvicorn）+ 演示站静态页。
#
# 多阶段构建：第一阶段在 builder 镜像里装包，第二阶段只拷 site/ 与 venv site-packages。
# 体积压到 ~200MB，拉得快。

# ─── builder ───
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# uv 比 pip 快 10×，更可控；发布镜像用一份完整 venv
RUN pip install uv==0.7.3

# akshare 依赖的 native wheels（py-mini-racer / lxml / curl-cffi）在 slim 上
# 需要 system headers 才能装。build 一次 100MB+ 但 cache 命中后只下 deps。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先拷 manifest，让依赖层能命中缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

# 再装项目本身
COPY src ./src
RUN uv sync --no-dev

# ─── runtime ───
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST=0.0.0.0

# 演示站 + venv 一并拷过来
COPY --from=builder /build/.venv /app/.venv
COPY site /app/site
COPY src /app/src

# 健康检查直接走 /healthz
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{__import__(\"os\").environ.get(\"PORT\",8080)}/healthz',timeout=2).status==200 else 1)"

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

# 8080 暴露给 K8s / LB；docker-compose 也用同一端口
EXPOSE 8080

# uvicorn 跑 API；演示站静态文件由 findata.cli.serve 同进程挂载
CMD ["python", "-m", "findata.cli.serve"]
