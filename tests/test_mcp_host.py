"""外部 MCP 宿主接入的线级测试（R2 验收）。

与 test_mcp.py 的进程内直调不同，这里用 MCP 标准 **stdio 客户端**拉起
真实的 `findata-mcp` 子进程，走完整 JSON-RPC 握手 → 工具发现 → 工具调用
——证明任何讲标准协议的外部宿主都能配置级接入，零 findata 内部依赖。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent

# 工具发现的总数与关键工具名：宿主"运行时发现"的验收锚点
_EXPECTED_TOOLS = {
    "findata_health_check",
    "findata_dashboard",
    "findata_validate",
    "findata_list_metrics",
    "findata_metric_query",
    "findata_ask",
    "findata_execute_metric",
    "findata_trust_check",
    "findata_trust_board",
}


def _params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "findata.mcp.server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def _parse(res) -> dict:
    texts = [getattr(c, "text", "") for c in (res.content or [])]
    return json.loads("\n".join(t for t in texts if t))


async def _run_session() -> tuple[list[str], dict, dict]:
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            board = _parse(
                await session.call_tool("findata_trust_board", {"source": "synthetic"})
            )
            miss = _parse(
                await session.call_tool(
                    "findata_trust_check",
                    {"table": "stock_daily", "metric": "ghost", "source": "synthetic"},
                )
            )
    return tools, board, miss


def test_external_host_wire_integration():
    """线级接入全流程：握手 → 发现 9 工具 → trust_board / trust_check 可用。"""
    tools, board, miss = asyncio.run(asyncio.wait_for(_run_session(), timeout=120))
    assert set(tools) == _EXPECTED_TOOLS, f"工具发现不符：{sorted(tools)}"
    # trust_board：全板徽章经协议栈往返后结构完整
    assert len(board["badges"]) == 12
    assert board["counts"]["verified"] + board["counts"]["baseline"] >= 1
    assert {"key", "mark", "usable", "evidence"} <= set(board["badges"][0])
    # 未知指标：错误契约跨协议保持——带可用键，宿主可自愈
    assert "error" in miss
    assert "stock_daily.close" in miss["error"]
