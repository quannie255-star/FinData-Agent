"""QueryAgent：执行语义层查询。

职责平移自单 Agent 的 tools 节点（M12 拆分）。执行走 agent.tools 的
run_metric_query / run_list_metrics——与 LLM 绑定的 @tool 是同一条路径，
不会出现"绑定的一套、执行的一套"的漂移。

在单 Agent 版之上的一处增强：除 run_id 文本抽取外，同时收集结构化查询
摘要（metric / params / value / verification），供 VerifierAgent 独立核验
与 agent 级评测使用。
"""

from __future__ import annotations

import duckdb
from langchain_core.messages import AIMessage, ToolMessage

from findata.agent.nodes.state import MultiAgentState
from findata.agent.tools import DATA_TOOLS, RUN_ID_RE, run_list_metrics, run_metric_query


def make_data_tools_node(conn: duckdb.DuckDBPyConnection):
    """数据工具执行节点工厂（闭包捕获 conn，避免 conn 进 state schema）。"""

    def data_tools_node(state: MultiAgentState) -> dict:
        last: AIMessage = state["messages"][-1]

        tool_messages: list[ToolMessage] = []
        run_ids: list[str] = []
        queries: list[dict] = []
        for call in last.tool_calls:
            name = call["name"]
            if name not in DATA_TOOLS:
                tool_messages.append(
                    ToolMessage(
                        content=f"错误：不允许调用工具 {name}",
                        tool_call_id=call["id"],
                    )
                )
                continue
            if name == "metric_query":
                args = call["args"]
                text, summary = run_metric_query(conn, args.get("metric", ""), args.get("params"))
                if "run_id" in summary:
                    run_ids.append(summary["run_id"])
                # 失败的尝试也进 queries：Verifier 需要看到失败才能追问修正，
                # 评测的分步漏斗（查询执行成功率）也需要真实的尝试总数
                queries.append(summary)
            else:  # list_metrics
                text = run_list_metrics()
            tool_messages.append(ToolMessage(content=text, tool_call_id=call["id"]))

        return {
            "messages": tool_messages,
            "run_ids": run_ids,
            "queries": queries,
            "tool_rounds": state.get("tool_rounds", 0) + 1,
        }

    return data_tools_node


def extract_run_ids(text: str) -> list[str]:
    """从任意文本里抽取 run_id（评测与后检查共用）。"""
    return RUN_ID_RE.findall(text)
