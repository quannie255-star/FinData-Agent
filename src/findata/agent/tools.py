"""子 Agent 共享的语义层工具集。

M12 从 graph.py 平移而来：单 Agent（graph.py，对比基线）与多智能体
（supervisor.py）必须绑定**完全相同**的工具集——否则单 vs 多的对比评测
里混入了"工具不一样"这个变量，数据就不干净了。

铁律不变：LLM 只选指标 + 填参，绝不写 SQL；每次 metric_query 自带
run_id 溯源与交叉验证标记，工具层负责把 run_id 抽出来给上层收集。

实现约束：@tool 包装（给 LLM 绑定用）与 QueryAgent 节点的直接执行
**必须走同一个 run_metric_query / run_list_metrics**，不允许出现两条
会漂移的执行路径。
"""

from __future__ import annotations

import re
from typing import Any

import duckdb
from langchain_core.tools import tool

from findata.semantic.catalog import MetricCatalog
from findata.semantic.executor import format_result
from findata.semantic.tools import list_metrics, metric_query

# 工具白名单（数据链路）：防止 LLM 越界调用
DATA_TOOLS = frozenset({"metric_query", "list_metrics"})

# 从工具结果文本里精确截取 16 进制 run_id（结果在 run_id 后还有验证标记，
# 不能按括号切）
RUN_ID_RE = re.compile(r"run_id=([0-9a-f]+)")


def run_metric_query(
    conn: duckdb.DuckDBPyConnection,
    metric: str,
    params: dict[str, Any] | None = None,
) -> tuple[str, dict]:
    """执行一次可信查询，返回 (给 LLM 看的文本, 结构化摘要)。

    结构化摘要供 QueryAgent 节点收集溯源（run_id / 验证状态）、给
    VerifierAgent 做独立核验、给评测脚本算 agent 级指标——文本是给模型的，
    摘要是给程序的，两者出自同一次执行。
    """
    try:
        r = metric_query(conn, metric, params or {}, MetricCatalog())
    except Exception as exc:  # 语义层校验失败 → 返回可读错误，供 LLM 修正重试
        return f"查询失败：{exc}", {
            "error": str(exc),
            "metric": metric,
            "params": params or {},
        }
    summary = {
        "metric": r.metric,
        "label": r.label,
        "value": r.value,
        "unit": r.unit,
        "as_of": r.as_of,
        "run_id": r.run_id,
        "params": r.params,
        "verification": r.verification.status.value if r.verification else None,
    }
    return format_result(r), summary


def run_list_metrics() -> str:
    """列出全部指标（给 LLM 看的一行一条文本）。"""
    items = list_metrics(MetricCatalog())
    return "\n".join(f"{m['name']}: {m['label']} — {m['description']}" for m in items)


def build_data_tools(conn: duckdb.DuckDBPyConnection) -> list:
    """构造绑定当前 DuckDB 连接的数据工具集合（SchemaAgent / QueryAgent 用）。"""

    @tool("metric_query")
    def metric_query_tool(metric: str, params: dict[str, Any] | None = None) -> str:
        """查询一个预定义金融指标，返回数值与 run_id 溯源。

        metric 必须是已注册的指标名（close_price / pct_change / valuation_pe_ttm）。
        params 是参数字典，如 {"symbol": "600519"} 或
        {"symbol": "600519", "start": "2024-01-01", "end": "2024-12-31"}。
        返回的 run_id 必须原样写进最终答案，用于数据溯源。
        """
        text, _ = run_metric_query(conn, metric, params)
        return text

    @tool("list_metrics")
    def list_metrics_tool() -> str:
        """列出所有可查询的指标及其含义，用于不确定该查哪个指标时。"""
        return run_list_metrics()

    return [metric_query_tool, list_metrics_tool]


def usage_of(message: Any) -> dict[str, int]:
    """从一条 AI 消息里提取 token 用量；桩模型没有 usage_metadata 时返回空。"""
    um = getattr(message, "usage_metadata", None)
    if not um:
        return {}
    inp = int(um.get("input_tokens", 0) or 0)
    out = int(um.get("output_tokens", 0) or 0)
    total = int(um.get("total_tokens", 0) or 0) or inp + out
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": total}


def merge_usage(a: dict, b: dict) -> dict:
    """LangGraph reducer：跨节点累计 token 用量（各 key 相加）。"""
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}
