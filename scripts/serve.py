"""M6 产品化：服务入口。

- 首次启动若仓库为空，自动灌入合成 fixture，保证 demo 开箱即有数据可查；
  真实数据请先跑 `uv run python scripts/ingest_finance.py`。
- 暴露 FastAPI（findata.api.app:app）到 0.0.0.0:8000。
"""

from __future__ import annotations

import logging

from findata.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("findata.serve")


def ensure_data() -> None:
    from findata.core.db import connect

    conn = connect(settings.dsn, read_only=True)
    try:
        n = conn.execute("SELECT count(*) FROM stock_daily").fetchone()[0]
    finally:
        conn.close()

    if n == 0:
        logger.info("仓库为空，载入合成 fixture（3 只股票）以保证 demo 可用")
        from scripts.build_eval_fixture import build

        build(settings.dsn)
    else:
        logger.info("检测到已有数据（%d 行日线），直接启动", n)


def main() -> None:
    ensure_data()
    import uvicorn

    uvicorn.run(
        "findata.api.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
