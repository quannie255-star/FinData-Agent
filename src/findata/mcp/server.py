"""findata MCP Server（stdio transport）。

为什么 stdio：M6 的目标是「Demo 上线 + 可被外部 Agent 调」。
stdio 让 Claude Desktop / Cursor / Cline 这类现成的客户端零配置接入——
把 `findata-mcp` 当成「执行命令 + 读 JSON」的能力，比 SSE 简单一个量级。

工具集
------
- `findata_health_check`  : 跑一次巡检，返回 JSON 摘要
- `findata_dashboard`     : 拼一份自包含 HTML 看板
- `findata_validate`      : 跑评测，返回 5 项指标；可选 κ 一致性
- `findata_list_metrics`  : 列出语义层指标字典元数据
- `findata_metric_query`  : 语义层可信查询——确定性编译 + 交叉验证 + run_id 溯源（招牌能力）
- `findata_execute_metric`: 跑 dq 诊断字典里的 SQL-kind metric（运维诊断用途，
                            无交叉验证；可信查询请走 findata_metric_query）

调用 `findata-mcp` 启动。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer

from findata.config import settings
from findata.dq import MetricRegistry, compile_metric, default_ctx
from findata.service import build_dashboard_html, eval_to_json, inspect, result_to_json

# 默认指标字典路径: 与 findata 包一起发布; 也可被 FINDATA_METRICS_YAML 环境变量覆盖
_DEFAULT_METRICS_YAML = Path(__file__).parent.parent / "dq" / "metrics.yaml"
_METRICS_REGISTRY: MetricRegistry | None = None


def _registry() -> MetricRegistry:
    """进程级单例 registry。YAML 改动需重启服务, 与"快速查一下指标元数据" 的
    调用频次配得上 —— 不做 hot-reload."""
    global _METRICS_REGISTRY
    if _METRICS_REGISTRY is None:
        import os as _os
        path_str = _os.environ.get("FINDATA_METRICS_YAML", str(_DEFAULT_METRICS_YAML))
        _METRICS_REGISTRY = MetricRegistry.from_yaml(Path(path_str))
    return _METRICS_REGISTRY


mcp = MCPServer(
    name="findata",
    instructions=(
        "findata: 数据质量监控 + 可信问数 Agent。六个工具——"
        "health_check 返回告警/抑制摘要；"
        "dashboard 返回自包含 HTML 看板；"
        "validate 跑评测并可选输出 baseline vs LLM 归因的 Cohen's κ；"
        "list_metrics 列出语义层指标字典；"
        "metric_query 是可信问数入口（确定性编译 + 交叉验证 + run_id 溯源，"
        "回答数字类问题请优先用它）；"
        "execute_metric 跑 dq 诊断字典的 SQL 指标（运维诊断用，无交叉验证）。"
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


@mcp.tool(
    name="findata_metric_query",
    title="Findata Metric Query",
    description=(
        "语义层可信查询：给定指标名与参数，编译器确定性生成 SQL（LLM/调用方"
        "绝不接触裸 SQL），执行后自动用第二口径交叉验证，返回数值 + 单位 + "
        "run_id 溯源 + 验证结论（verified/mismatch/not_verifiable）。"
        "回答数字类问题请优先使用本工具而非 execute_metric。"
        "指标元数据用 findata_list_metrics 查看。"
    ),
)
def findata_metric_query(
    metric: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a trusted semantic-layer metric query with cross-verification.

    Args:
        metric: Registered metric name, e.g. close_price / pct_change /
            valuation_pe_ttm.
        params: Parameter dict, e.g. {"symbol": "600519"} or
            {"symbol": "600519", "start": "2024-01-01", "end": "2024-12-31"}.

    Returns:
        Dict with value / unit / as_of / run_id / verification. Every number
        can be traced back via its run_id (see the query_run / verify_run
        lineage tables).
    """
    from findata.core.db import connect
    from findata.semantic.catalog import MetricCatalog
    from findata.semantic.tools import metric_query as _metric_query

    path = Path(settings.dsn)
    if not path.exists():
        return {
            "error": f"数据仓库不存在：{path}。请先运行 scripts/ingest_finance.py。",
            "metric": metric,
        }
    conn = connect(settings.dsn, read_only=False)
    try:
        r = _metric_query(conn, metric, params or {}, MetricCatalog())
    except Exception as exc:  # 语义层校验失败（未知指标/参数非法）
        return {"error": str(exc), "metric": metric, "params": params}
    finally:
        conn.close()
    out: dict[str, Any] = {
        "metric": r.metric,
        "label": r.label,
        "value": r.value,
        "unit": r.unit,
        "as_of": r.as_of,
        "source": r.source,
        "run_id": r.run_id,
        "params": r.params,
    }
    if r.verification is not None:
        out["verification"] = {
            "status": r.verification.status.value,
            "note": r.verification.note,
            "verify_run_id": r.verification.verify_run_id,
        }
    return out


@mcp.tool(
    name="findata_list_metrics",
    title="Findata List Metrics",
    description=(
        "列出指标字典 (findata/dq/metrics.yaml) 里所有 metric 的元数据: "
        "name / kind / description / default_severity / threshold。 "
        "返回纯 JSON 数组, 外部 Agent 拿到后可以选择具体 metric 跑 execute_metric。"
        "环境变量 FINDATA_METRICS_YAML 可覆盖默认 yaml 路径。"
    ),
)
def findata_list_metrics() -> dict[str, Any]:
    """Enumerate every metric defined in the metric dictionary."""
    reg = _registry()
    return {"count": len(reg.list()), "metrics": reg.list()}


@mcp.tool(
    name="findata_execute_metric",
    title="Findata Execute Metric",
    description=(
        "确定性跑一个 dq 诊断字典里的 SQL-kind metric: 把指标字典里的 "
        "sql_template 编译成 (sql, params), 拿数据源里读出来的 duckdb 视图直接执行, "
        "返回结果行。注意：这是运维诊断用途，无交叉验证与溯源；"
        "面向用户的数字类查询请走 findata_metric_query（语义层，带验证+溯源）。"
        "只支持 kind=sql 的 metric; kind=probe 的请走 findata_health_check。"
        "source=synthetic 时, 会在内存里建一张合成的 stock_daily 视图再跑。"
    ),
)
def findata_execute_metric(
    name: str,
    source: str = "synthetic",
    asof: str | None = None,
    seed: int = 20240102,
) -> dict[str, Any]:
    """Compile a metric's SQL template, run it, and return the rows."""
    reg = _registry()
    try:
        metric = reg.get(name)
    except KeyError as exc:
        return {"error": str(exc)}
    if metric["kind"] != "sql":
        return {
            "error": f"metric {name!r} is kind={metric['kind']!r}, not sql; "
            "use health_check instead"
        }
    target = _parse_date(asof)
    sql, params = compile_metric(metric, default_ctx(asof=target or date.today()))
    try:
        conn = _connect_for_execute(source, seed)
    except (NotImplementedError, ValueError) as exc:
        return {"error": str(exc), "name": name, "kind": "sql"}
    try:
        result = conn.execute(sql, params).fetchall()
        cols = [d[0] for d in conn.description]
        rows = [dict(zip(cols, row, strict=False)) for row in result]
    finally:
        if source == "duckdb":
            conn.close()
    return {
        "name": name,
        "kind": "sql",
        "sql": sql.strip(),
        "params": [str(p) for p in params],
        "rows": rows,
        "threshold": metric.get("threshold", {}),
    }


def _connect_for_execute(source: str, seed: int) -> duckdb.DuckDBPyConnection:
    """根据 source 选数据视图。synthetic 走 Snapshot.from_synthetic 把 DataFrame 注册成视图。"""
    conn = duckdb.connect(":memory:")
    if source == "synthetic":
        from findata.report.inspect import Snapshot

        snap = Snapshot.from_synthetic(seed=seed)
        conn.register("stock_daily", snap.stock_daily)
        if not snap.valuation_daily.empty:
            conn.register("valuation_daily", snap.valuation_daily)
    elif source == "duckdb":
        conn.close()
        path = Path(settings.dsn)
        if not path.exists():
            raise ValueError(
                f"数据仓库不存在：{path}。请先运行 scripts/ingest_finance.py，"
                "或改用 source='synthetic'。"
            )
        conn = duckdb.connect(str(path), read_only=True)
    else:
        raise ValueError(f"unknown source: {source!r}")
    return conn


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
