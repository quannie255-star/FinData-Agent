"""findata：数据质量监控 Agent。

模块边界：
- `dq`        : 探针/归因/契约    （最纯粹的可测试内核）
- `eval`      : 评测（合成基线、故障注入、指标）
- `agent`     : LLM 归因器
- `report`    : 巡检流水线与报告/看板
- `service`   : 高层 API 复用层（HTTP API + MCP Server 都用）
- `api`       : FastAPI HTTP 入口
- `mcp`       : MCP Server 入口（stdio transport）
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("findata")
    except PackageNotFoundError:  # editable install 边界情况
        __version__ = "0.4.0"
except Exception:  # noqa: BLE001
    __version__ = "0.4.0"

__all__ = ["__version__"]
