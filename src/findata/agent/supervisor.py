"""Supervisor：路由 + 复杂度判断 + 溯源强制检查 + 多智能体图组装。

三个设计决定（都不是默认选项，是有意为之）：

1. 复杂度判断用**确定性规则**而不是 LLM——路由本身也要花钱，规则零成本、
   可全分支单测；判错的代价只是"简单题多走一轮验证"，方向上安全。
2. TriageAgent 以工具形态挂在 VerifierAgent 下（ROADMAP 职责表：
   "包成工具供 VerifierAgent 追问"），不是图里的独立节点——归因是
   "被追问才有"的深挖动作，简单题不值得为它花一轮。
3. 溯源强制检查是两道闸：gate 节点拦"查了但没拿到 run_id"（打回补查一次），
   post_check 拦"数据类问题的答案里见不到 run_id"（追加显式警告标记）。
   绕不过去，但也不静默丢弃——要么补齐溯源，要么明确告知未通过检查。

拓扑：
    START → supervisor ─┬─ schema ⇄ data_tools ─┐
                        │                        ├─ gate ─┬ deep → verifier ⇄ dq_tools → finalize
                        └────────────────────────┘        └ direct → finalize
    finalize → post_check → END
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import duckdb
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from findata.agent.llm import make_analysis_llm
from findata.agent.nodes.query import make_data_tools_node
from findata.agent.nodes.schema import make_schema_node
from findata.agent.nodes.state import MultiAgentState, initial_state
from findata.agent.nodes.triage import DQ_TOOLS, build_dq_tools
from findata.agent.nodes.verifier import make_verifier_node
from findata.agent.tools import RUN_ID_RE, build_data_tools, usage_of

# 与单 Agent 相同的回环上限：多智能体不允许比单 Agent 更失控
_MAX_TOOL_ROUNDS = 4
# 验证回环上限：dq_context + triage_trust 各一次就够，3 轮是宽松上限
_MAX_VERIFY_ROUNDS = 3

# ─────────────────────────── 路由规则 ───────────────────────────

# 指标关键词 → 指标名（规则路由的事实来源：语义层指标字典的口语同义词）
_METRIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "close_price": ("收盘价", "收盘", "股价", "价格", "多少钱", "价位"),
    "pct_change": ("涨跌幅", "涨幅", "跌幅", "涨了", "跌了", "变化率", "区间"),
    "valuation_pe_ttm": ("市盈率", "估值", "pe_ttm"),
}

# deep 信号词：对比 / 聚合 / 多目标。注意避开"最新""最近"这类单指标高频词
_DEEP_PATTERNS = (
    "对比", "比较", "相比", "哪个", "哪些", "谁更", "排名", "平均",
    "之和", "总和", "合计", "分别", "以及", "还有", "和它", "跟他",
)

# 6 位股票代码
_CODE_RE = re.compile(r"\b[0-9]{6}\b")

# 概念类问题的标志：问定义/原因而不是问数
_CONCEPT_MARKERS = ("什么是", "解释", "介绍", "含义", "什么意思", "为什么", "怎么理解")

METRIC_KEYWORD_MAP = _METRIC_KEYWORDS


def judge_complexity(question: str) -> Literal["direct", "deep"]:
    """规则复杂度判断：命中 deep 信号 → 多智能体全链路，否则单指标快路径。"""
    text = question.lower()
    if any(p in question for p in _DEEP_PATTERNS):
        return "deep"
    if len(_CODE_RE.findall(question)) >= 2:
        return "deep"
    hits = sum(1 for kws in _METRIC_KEYWORDS.values() if any(k in text for k in kws))
    if hits >= 2:
        return "deep"
    return "direct"


def is_data_question(question: str) -> bool:
    """是否数据类问题（post_check 用）：问数而不是问概念。启发式，如实标注。"""
    if any(m in question for m in _CONCEPT_MARKERS):
        return False
    if _CODE_RE.search(question):
        return True
    text = question.lower()
    return any(k in text for kws in _METRIC_KEYWORDS.values() for k in kws)


# ─────────────────────────── 图节点 ───────────────────────────


def supervisor_node(state: MultiAgentState) -> dict:
    """路由节点：零 LLM 成本，只定模式。"""
    return {"mode": judge_complexity(state["question"])}


def gate_node(state: MultiAgentState) -> dict:
    """溯源闸第一道（计数）：查过工具但没拿到 run_id → 记一次重试。"""
    if _traceability_missing(state):
        return {"gate_retries": state.get("gate_retries", 0) + 1}
    return {}


def _traceability_missing(state: MultiAgentState) -> bool:
    """查过数据工具却没有任何 run_id = 溯源链断裂。"""
    return state.get("tool_rounds", 0) > 0 and not state.get("run_ids")


def make_finalize_node():
    """最终答复发稿人：组织答案，强制 run_id 溯源、验证标记与 DQ 背书转述。"""

    def finalize_node(state: MultiAgentState) -> dict:
        llm = make_analysis_llm()
        sys = (
            "你是 Findata 可信数据分析 Agent。请根据以上对话与验证结论，"
            "用简洁中文回答用户。\n"
            "要求：\n"
            "1. 直接给出数字结论，不要复述过程。\n"
            "2. 每个数字后必须保留方括号里的 run_id（如 [run_id=abc123]），不得省略。\n"
            "3. 查询结果的交叉验证标记（✓ 已交叉验证 / ⚠ 不一致 / ○ 未验证）"
            "必须原样转述，不得省略或美化。\n"
            "4. 若验证员（VerifierAgent）给出了数据质量结论或背书"
            "（如「近期无告警」「信号已抑制」），用一句话如实转述，不得夸大。\n"
            "5. 若查询失败或无数据，如实说明，不要编造。"
        )
        messages = [("system", sys), *state["messages"]]
        response = llm.invoke(messages)
        return {
            "messages": [response],
            "usage": usage_of(response),
            "usage_by_agent": {"finalize": usage_of(response)},
        }

    return finalize_node


def post_check_node(state: MultiAgentState) -> dict:
    """溯源闸第二道（放行前终检）：数据类问题的答案里必须见得到 run_id。

    见不到时不静默丢弃答案——追加显式警告标记，让调用方一眼看到
    「这个答案没过溯源检查」，而不是被无标注的裸数字骗过去。
    """
    final = state["messages"][-1]
    text = getattr(final, "content", "") or ""
    if isinstance(text, list):  # 多模态 content 块，取文本部分
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if is_data_question(state.get("question", "")) and not RUN_ID_RE.search(text):
        return {
            "messages": [
                AIMessage(
                    content=text + "\n\n⚠ 溯源检查未通过：本答案未携带 run_id，"
                    "数字未经查询链路背书，请勿直接采信。"
                )
            ]
        }
    return {}


def make_dq_tools_node(dq_tools: list):
    """dq 工具执行节点：白名单执行 + 把 dq_context 的 JSON 信号收进 state。"""
    registry = {t.name: t for t in dq_tools}

    def dq_tools_node(state: MultiAgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        tool_messages: list[ToolMessage] = []
        signals: list[dict] = []
        for call in last.tool_calls:
            name = call["name"]
            if name not in DQ_TOOLS:
                tool_messages.append(
                    ToolMessage(
                        content=f"错误：不允许调用工具 {name}",
                        tool_call_id=call["id"],
                    )
                )
                continue
            content = registry[name].invoke(call["args"])
            if name == "dq_context":
                try:
                    rows = json.loads(content)
                    if isinstance(rows, list):
                        signals.extend(rows)
                except ValueError:
                    pass
            tool_messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
        return {
            "messages": tool_messages,
            "dq_signals": signals,
        }

    return dq_tools_node


# ─────────────────────────── 路由函数 ───────────────────────────


def _has_tool_calls(state: MultiAgentState) -> bool:
    last = state["messages"][-1]
    return isinstance(last, AIMessage) and bool(last.tool_calls)


def after_schema(state: MultiAgentState) -> Literal["data_tools", "gate"]:
    return "data_tools" if _has_tool_calls(state) else "gate"


def after_data_tools(state: MultiAgentState) -> Literal["schema", "gate"]:
    """数据工具后的回环：没到轮数上限就回 SchemaAgent 看结果再决定。

    与单 Agent 的 _after_tools 同语义：执行后无条件回 schema（由 LLM 看
    结果决定继续查还是收尾），轮数上限在这里强制。
    """
    if state.get("tool_rounds", 0) < _MAX_TOOL_ROUNDS:
        return "schema"
    return "gate"


def after_gate(state: MultiAgentState) -> Literal["schema", "verifier", "finalize"]:
    # gate_node 先自增、路由函数后判断：retries==1 是首次经过（打回补查），
    # retries==2 是补查后仍失败（带警告放行，不无限回环）
    if _traceability_missing(state) and state.get("gate_retries", 0) <= 1:
        return "schema"
    if state.get("mode") == "deep":
        return "verifier"
    return "finalize"


def after_verifier(state: MultiAgentState) -> Literal["dq_tools", "finalize"]:
    return "dq_tools" if _has_tool_calls(state) else "finalize"


def after_dq_tools(state: MultiAgentState) -> Literal["verifier", "finalize"]:
    """dq 工具后的回环：没到验证轮数上限就回 VerifierAgent 看证据再决定。"""
    if state.get("verify_rounds", 0) < _MAX_VERIFY_ROUNDS:
        return "verifier"
    return "finalize"


# ─────────────────────────── 图组装 ───────────────────────────


def build_multi_agent(conn: duckdb.DuckDBPyConnection):
    """构建 Supervisor 编排的多智能体图（已注入数据库连接）。

    子 Agent 与工具：
    - SchemaAgent  : 绑定 metric_query / list_metrics（与单 Agent 完全同源）
    - QueryAgent   : 数据工具执行节点（白名单 + 轮数上限 + run_id 溯源）
    - VerifierAgent: 绑定 dq_context / triage_trust，独立验证 + DQ 运行时背书
    - TriageAgent  : 以工具形态挂在 VerifierAgent 下（见 nodes/triage.py）
    """
    data_tools = build_data_tools(conn)
    dq_tools = build_dq_tools(conn)

    graph = StateGraph(MultiAgentState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("schema", make_schema_node(data_tools))
    graph.add_node("data_tools", make_data_tools_node(conn))
    graph.add_node("gate", gate_node)
    graph.add_node("verifier", make_verifier_node(dq_tools))
    graph.add_node("dq_tools", make_dq_tools_node(dq_tools))
    graph.add_node("finalize", make_finalize_node())
    graph.add_node("post_check", post_check_node)

    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "schema")
    graph.add_conditional_edges(
        "schema", after_schema, {"data_tools": "data_tools", "gate": "gate"}
    )
    graph.add_conditional_edges(
        "data_tools", after_data_tools, {"schema": "schema", "gate": "gate"}
    )
    graph.add_conditional_edges(
        "gate",
        after_gate,
        {"schema": "schema", "verifier": "verifier", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "verifier", after_verifier, {"dq_tools": "dq_tools", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "dq_tools", after_dq_tools, {"verifier": "verifier", "finalize": "finalize"}
    )
    graph.add_edge("finalize", "post_check")
    graph.add_edge("post_check", END)

    app = graph.compile()

    class _MultiAgent:
        def __init__(self):
            self._app = app

        @property
        def app(self):
            """编译后的图，供 API 层做 astream 流式输出。"""
            return self._app

        def invoke(self, question: str) -> dict:
            state = initial_state(question)
            # 问题必须作为首条用户消息进对话——SchemaAgent 的全部输入就是它。
            # （回归教训：漏掉这行时真实模型看到空对话，只能照 system prompt
            # 里的示例股票瞎猜；脚本化假模型不读输入，测试抓不住。）
            state["messages"] = [HumanMessage(content=question)]
            return self._app.invoke(state)

    return _MultiAgent()


def answer_question(conn: duckdb.DuckDBPyConnection, question: str) -> str:
    """多智能体一次性问答：返回最终答案文本。"""
    result = invoke_state(conn, question)
    final = result["messages"][-1]
    return final.content if hasattr(final, "content") else str(final)


def invoke_state(conn: duckdb.DuckDBPyConnection, question: str) -> dict[str, Any]:
    """问答并返回完整 state（评测脚本与上层取 run_ids / usage / verdict 用）。"""
    return build_multi_agent(conn).invoke(question)
