"""findata MCP Server（stdio transport）。

为什么 stdio：M6 的目标是「Demo 上线 + 可被外部 Agent 调」。
stdio 让 Claude Desktop / Cursor / Cline 这类现成的客户端零配置接入——
把 `findata-mcp` 当成「执行命令 + 读 JSON」的能力，比 SSE 简单一个量级。

工具集
------
- `findata_health_check` : 跑一次巡检，返回 JSON 摘要
- `findata_dashboard`    : 拼一份自包含 HTML 看板
- `findata_validate`     : 跑评测，返回 5 项指标；可选 κ 一致性

调用 `findata-mcp` 启动。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from findata.service import build_dashboard_html, eval_to_json, inspect, result_to_json

mcp = MCPServer(
    name="findata",
    instructions=(
        "findata: 数据质量监控 Agent。三个工具——"
        "健康检查返回告警/抑制摘要；"
        "返回自包含 HTML 看板；"
        "validate 跑评测并可选输出 baseline vs LLM 归因的 Cohen's κ。"
    ),
)


# ─────────────────────────── 工具 ───────────────────────────


@mcp.tool(
    name="findata_health_check",
    title="Findata Health Check",
    description=(
        "对 findata 仓库跑一次巡检：按设定探针扫描数据，返回待处理告警与已抑制"
        "信号的 JSON 摘要。source=synthetic 时不需要真实数据库。"
        "asof 缺省取最近交易日，可显式给 YYYY-MM-DD 回放任意历史时点。"
    ),
)
def findata_health_check(
    source: str = "synthetic",
    asof: str | None = None,
    seed: int = 20240102,
) -> dict[str, Any]:
    """Run a one-shot inspection, returning the JSON summary plus alert/suppressed lists.

    Args:
        source: `synthetic` or `duckdb`. `synthetic` uses deterministic fixtures (good
            for demos and CI); `duckdb` reads the populated warehouse.
        asof: Optional observation day in YYYY-MM-DD for historical replay.
        seed: Synthetic-fault injection seed (only honored when source=synthetic).

    Returns:
        A JSON-shaped dict with `asof`, `summary`, `alerts`, `suppressed`. Schema
        matches `GET /v1/inspect` so API & MCP callers can share parsers.
    """
    target = _parse_date(asof)
    result = inspect(source, target, seed)
    return result_to_json(result)


@mcp.tool(
    name="findata_dashboard",
    title="Findata Dashboard",
    description=(
        "拼一份自包含的 HTML 看板（无 CDN、无外部依赖），可直接落盘或回贴给前端。"
        "可选附带评测得分。"
    ),
)
def findata_dashboard(
    source: str = "synthetic",
    asof: str | None = None,
    days: int = 20,
    with_eval: bool = False,
    seed: int = 20240102,
) -> dict[str, Any]:
    """Render the self-contained dashboard HTML.

    Args:
        source: `synthetic` or `duckdb`.
        asof: Optional YYYY-MM-DD; defaults to the latest day in the snapshot.
        days: Length of the trend window (1-180).
        with_eval: If true, attach synthetic eval report to the dashboard.
        seed: Synthetic-fault seed.

    Returns:
        `{"html", "length", "summary"}` — `html` is ready-to-write, `summary` is
        the JSON snapshot of the current observation point.
    """
    target = _parse_date(asof)
    html, current = build_dashboard_html(source, target, days, with_eval, seed)
    return {
        "html": html,
        "length": len(html),
        "summary": result_to_json(current),
    }


@mcp.tool(
    name="findata_validate",
    title="Findata Validate",
    description=(
        "跑 findata 评测：合成基线 + 注入故障 + 跑流水线 + 比对标注。"
        "输出 5 项指标（故障召回/根因准确/分级准确/误报抑制/告警精确率）"
        "及朴素基线对照。triage=both 时附加 baseline vs LLM 归因器的 Cohen's κ。"
    ),
)
def findata_validate(
    seed: int = 20240102,
    triage: str = "baseline",
) -> dict[str, Any]:
    """Run the eval harness end-to-end.

    Args:
        seed: Fault-injection seed (controls what is injected; same seed yields the
            same faults, so eval results are bit-stable across runs).
        triage: `baseline` (rules only), `llm` (LLM only), or `both` (run both and
            report Cohen's κ for root-cause labels and suppress decisions).

    Returns:
        JSON with `metrics.rows` (list of (label, value) tuples), signal/injection
        counts, and (when triage=both) `triage_agreement` (κ + sample size + source).
    """
    if triage not in ("baseline", "llm", "both"):
        return {"error": f"unknown triage: {triage!r}"}
    # 延迟导入，避免在工具列举时被强制拉起 LLM 客户端
    from findata.service import evaluate as _evaluate

    return eval_to_json(_evaluate(seed=seed, triage=triage))  # type: ignore[arg-type]


# ─────────────────────────── 入口 ───────────────────────────


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s)


async def _run_stdio() -> None:
    await mcp.run_stdio_async()


def main() -> None:
    """findata-mcp console_scripts 入口：纯 stdio 运行。"""
    parser = argparse.ArgumentParser(prog="findata-mcp", add_help=True)
    # 目前没有 flag，但预留一份 placeholder，方便后续加 --log-level 之类
    parser.parse_args()
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
