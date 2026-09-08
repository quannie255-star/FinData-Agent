# Findata —— 可信金融数据分析 Agent 镜像
# 多阶段构建：先装依赖，再跑服务。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 基础工具 + uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

# 依赖层（缓存友好）：先复制锁文件与源码树，再 sync
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY scripts ./scripts
COPY eval ./eval
RUN uv sync --no-dev --frozen

# 运行层
COPY . .

EXPOSE 8000

# 首次启动若仓库为空会自动灌 fixture；真实数据见 README
CMD ["uv", "run", "python", "scripts/serve.py"]
