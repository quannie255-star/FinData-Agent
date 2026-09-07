# findata 演示镜像（slim）：单容器同时跑 API（uvicorn）+ 演示站静态页。
#
# 多阶段：builder 只负责解析并装好**依赖**，runtime 只拷 site-packages + 源码。
# 最终镜像里没有任何 build toolchain 残留的 site-packages 污染。

# ─── builder ───
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# uv 比 pip 快 10×，且严格走 uv.lock（可复现）
RUN pip install uv==0.7.3

# akshare 依赖链里的 native wheels（lxml / curl-cffi / akracer 等）在 slim
# 上需要 system headers。这一层装完 100MB+，但命中缓存后只走 deps 下载。
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

# 只装**依赖**，不装 project 本身（--no-install-project）。
#
# 为什么不在容器里装 project：slim 容器内跑 PEP 517 build 需要现场拉 build
# backend（hatchling → editables，或 setuptools isolated build），多轮 release
# 都卡在这一步（exit 1 / exit 2）。而 CI runner 上 `uv build` 是好的 —— 所以
# 这里干脆不装，改用「源码 + PYTHONPATH」运行（见 runtime 阶段）。
# 代码里 `findata.__version__` 有 importlib.metadata 的 fallback，不依赖安装。
RUN uv sync --no-dev --no-install-project

# ─── runtime ───
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONPATH=/app/src

# 只拷 site-packages，**不拷整个 .venv**：venv 的 bin/python 是指向 builder
# 绝对路径的符号链接，跨 stage 拷过来可能失效。直接灌进系统 site-packages，
# 解释器仍用 runtime 镜像自带的 python，零符号链接风险。
COPY --from=builder /build/.venv/lib/python3*/site-packages/ /usr/local/lib/python3.11/site-packages/

COPY src /app/src
COPY site /app/site

WORKDIR /app

# build-time 冒烟：依赖齐不齐、能不能 import、site/ 在不在，当场炸比上线炸好
RUN python -c "import findata, findata.api.app, findata.cli.serve; \
               print('import ok:', findata.__file__, findata.__version__)" \
 && python -c "import pathlib; p=pathlib.Path('/app/site/index.html'); \
               assert p.exists(), 'site/index.html missing'"

# 健康检查直接走 /healthz
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

# 8080 暴露给 K8s / LB；docker-compose 也用同一端口
EXPOSE 8080

# uvicorn 跑 API；演示站静态文件由 findata.cli.serve 同进程挂载
CMD ["python", "-m", "findata.cli.serve"]
