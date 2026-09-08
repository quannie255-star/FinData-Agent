"""findata：面向 A 股金融数据的智能体，提供两套互补能力。

模块边界：
- `dq`        : 数据质量监控 —— 探针/归因/契约（最纯粹的可测试内核）
- `eval`      : 评测（合成基线、故障注入、指标）
- `agent`     : LLM 归因器 + 可信问数分析 Agent（LangGraph）
- `report`    : 巡检流水线与报告/看板
- `semantic`  : 可信问数分析 —— 指标字典/确定性 SQL 编译器/交叉验证（共享 M1 底座）
- `service`   : 高层 API 复用层（HTTP API + MCP Server 都用）
- `api`       : FastAPI HTTP 入口（监控路由 + 分析路由并存）
- `mcp`       : MCP Server 入口（stdio transport）
"""

from __future__ import annotations

# 单一版本源：`pyproject.toml` 的 version。镜像里不走 pip install（源码 +
# PYTHONPATH 直跑），拿不到 installed metadata，所以这里必须留一致的兜底值。
_FALLBACK_VERSION = "0.7.0"

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("findata")
    except PackageNotFoundError:  # 未安装（源码 + PYTHONPATH 直跑，如 docker 镜像内）
        __version__ = _FALLBACK_VERSION
except Exception:  # noqa: BLE001
    __version__ = _FALLBACK_VERSION

__all__ = ["__version__"]
