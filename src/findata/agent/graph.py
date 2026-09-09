"""M3 Agent 编排：LangGraph 图，把「自然语言问题」变成「带溯源的可信答案」。

流程（有回环的对话状态机）：
    用户问题
      → parse（LLM 理解意图，绑定工具后产出 tool_calls 或直接作答）
      → 条件路由：
            - 带工具调用 → tools 节点执行 → 回到 parse 再判断
              （LLM 看到工具结果后可以继续补查，最多 MAX_TOOL_ROUNDS 轮）
            - 无工具调用 → finalize 组织最终答案
      → finalize（数字必须带 run_id 溯源与交叉验证标记）

设计原则（面试考点）：
- LLM 只负责「选指标 + 填参数」，绝不写 SQL；SQL 由语义层编译器确定性生成。
- 工具白名单 + 轮数上限：回环不能变成失控循环。
- 每个数字经 metric_query 返回时自带 run_id，最终答案强制携带，保证可溯源。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, TypedDict

import duckdb
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from findata.agent.llm import make_chat_model
from findata.semantic.catalog import MetricCatalog
from findata.semantic.executor import format_result
from findata.semantic.tools import list_metrics, metric_query

# 工具节点允许的工具名（白名单，防止 LLM 越界调用）
ALLOWED_TOOLS = {"metric_query", "list_metrics"}
# 工具调用回环上限：防止 LLM 陷入"查了再查"的失控循环
MAX_TOOL_ROUNDS = 4

_RUN_ID_RE = re.compile(r"run_id=([0-9a-f]+)")


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    # 最终答案里出现的所有 run_id，供上层收集溯源
    run_ids: list[str]
    # 已执行的工具轮数（plain channel：每轮 tools 节点 +1）
    tool_rounds: int


def _make_llm():
    """构建分析链路模型；失败时给出可行动的报错，而不是裸的 SDK 异常。"""
    try:
        return make_chat_model()
    except Exception as exc:
        raise RuntimeError(
            f"分析链路 LLM 不可用：{exc}。请配置 FINDATA_LLM_API_KEY"
            "（及可选 FINDATA_LLM_BASE_URL / FINDATA_LLM_MODEL）"
        ) from exc


def _build_tools(conn: duckdb.DuckDBPyConnection) -> list:
    """构造绑定当前 DuckDB 连接的工具集合。"""

    catalog = MetricCatalog()

    @tool("metric_query")
    def metric_query_tool(metric: str, params: dict[str, Any] | None = None) -> str:
        """查询一个预定义金融指标，返回数值与 run_id 溯源。

        metric 必须是已注册的指标名（close_price / pct_change / valuation_pe_ttm）。
        params 是参数字典，如 {"symbol": "600519"} 或
        {"symbol": "600519", "start": "2024-01-01", "end": "2024-12-31"}。
        返回的 run_id 必须原样写进最终答案，用于数据溯源。
        """
        try:
            result = metric_query(conn, metric, params or {}, catalog)
        except Exception as exc:  # 语义层校验失败 → 返回可读错误，供 LLM 修正重试
            return f"查询失败：{exc}"
        return format_result(result)

    @tool("list_metrics")
    def list_metrics_tool() -> str:
        """列出所有可查询的指标及其含义，用于不确定该查哪个指标时。"""
        items = list_metrics(catalog)
        lines = [f"{m['name']}: {m['label']} — {m['description']}" for m in items]
        return "\n".join(lines)

    return [metric_query_tool, list_metrics_tool]


def _make_parse_node(tools: list):
    """parse 节点工厂：LLM 必须绑定工具，否则真实模型发不出 tool_calls，
    图会结构性退化成"永远直接作答"。"""

    def _parse_node(state: AgentState) -> dict:
        """LLM 理解意图：判断直接回答还是调用工具查数。

        通过 system prompt 约束 LLM：数据类问题必须调用工具，不得凭空编数字。
        """
        llm = _make_llm().bind_tools(tools)
        sys = (
            "你是 Findata 可信数据分析 Agent。你的职责是回答金融数据问题，"
            "但**绝不凭空编造任何数字**。"
            "\n\n规则：\n"
            "1. 涉及股票价格、涨跌幅、估值等数据问题，必须调用工具查询，"
            "不要靠记忆或猜测回答。\n"
            "2. 工具结果已足够回答时，直接给出文本答案（不再调用工具），"
            "并把工具返回的 run_id 原样保留在答案里用于数据溯源。\n"
            "3. 若还需补充数据（换参数、查别的指标），可再次调用工具。\n"
            "4. 若问题与数据无关（如问候、解释概念），可直接回答，无需工具。"
        )
        messages = [("system", sys), *state["messages"]]
        response = llm.invoke(messages)
        return {"messages": [response]}

    return _parse_node


def _should_call_tools(state: AgentState) -> Literal["tools", "finalize"]:
    """路由：最后一条 AI 消息若带了 tool_calls 则进入工具节点，否则直接收尾。"""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finalize"


def _make_tools_node(conn: duckdb.DuckDBPyConnection):
    """构造 tools 节点：闭包捕获 conn，避免 conn 进 state schema。"""
    tools = {t.name: t for t in _build_tools(conn)}

    def _tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]

        tool_messages: list[ToolMessage] = []
        run_ids: list[str] = []
        for call in last.tool_calls:
            name = call["name"]
            if name not in ALLOWED_TOOLS:
                tool_messages.append(
                    ToolMessage(
                        content=f"错误：不允许调用工具 {name}",
                        tool_call_id=call["id"],
                    )
                )
                continue
            content = tools[name].invoke(call["args"])
            # 从结果里抽取 run_id 收集溯源（结果文本在 run_id 后还有验证标记，
            # 必须用正则精确截取 16 进制 id，不能按括号切）
            run_ids.extend(_RUN_ID_RE.findall(content))
            tool_messages.append(
                ToolMessage(content=content, tool_call_id=call["id"])
            )

        return {
            "messages": tool_messages,
            "run_ids": run_ids,
            "tool_rounds": state.get("tool_rounds", 0) + 1,
        }

    return _tools_node


def _after_tools(state: AgentState) -> Literal["parse", "finalize"]:
    """工具执行后的回环路由：没到轮数上限就回 parse 让 LLM 看结果再决定。"""
    if state.get("tool_rounds", 0) >= MAX_TOOL_ROUNDS:
        return "finalize"
    return "parse"


def _finalize_node(state: AgentState) -> dict:
    """LLM 组织最终答案，要求携带 run_id 溯源。"""
    llm = _make_llm()
    sys = (
        "你是 Findata 可信数据分析 Agent。请根据工具查询结果，用简洁中文回答用户。\n"
        "要求：\n"
        "1. 直接给出数字结论，不要复述过程。\n"
        "2. 每个数字后必须保留方括号里的 run_id（如 [run_id=abc123]），不得省略。\n"
        "3. 若查询结果带有交叉验证标记（✓ 已交叉验证 / ⚠ 不一致 / ○ 未验证），"
        "必须在答案里原样转述该标记，不得省略或美化。\n"
        "4. 若查询失败或无数据，如实说明，不要编造。"
    )
    messages = [("system", sys), *state["messages"]]
    response = llm.invoke(messages)
    return {"messages": [response]}


def build_agent(conn: duckdb.DuckDBPyConnection):
    """构建编译后的 LangGraph Agent（已注入数据库连接）。

    拓扑：START → parse →(有 tool_calls)→ tools →(未达上限)→ parse 回环
                        └─(无 tool_calls)→ finalize → END
    tools 达到 MAX_TOOL_ROUNDS 后强制收尾，回环不会失控。
    """
    tools = _build_tools(conn)
    graph = StateGraph(AgentState)

    graph.add_node("parse", _make_parse_node(tools))
    graph.add_node("tools", _make_tools_node(conn))
    graph.add_node("finalize", _finalize_node)

    graph.add_edge(START, "parse")
    graph.add_conditional_edges(
        "parse",
        _should_call_tools,
        {"tools": "tools", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "tools",
        _after_tools,
        {"parse": "parse", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    app = graph.compile()

    class _Agent:
        def __init__(self):
            self._app = app

        @property
        def app(self):
            """编译后的图，供 API 层做 astream 流式输出。"""
            return self._app

        def invoke(self, question: str) -> dict:
            initial: AgentState = {
                "messages": [HumanMessage(content=question)],
                "run_ids": [],
                "tool_rounds": 0,
            }
            return self._app.invoke(initial)

    return _Agent()


def answer_question(conn: duckdb.DuckDBPyConnection, question: str) -> str:
    """一次性问答：返回最终答案文本。"""
    agent = build_agent(conn)
    result = agent.invoke(question)
    messages = result["messages"]
    final = messages[-1]
    return final.content if hasattr(final, "content") else str(final)
