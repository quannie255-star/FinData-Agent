"""SchemaAgent：理解问题 → 选指标 + 填参。

职责平移自单 Agent 的 parse 节点（M12 拆分），行为契约不变：
- LLM 只负责「选指标 + 填参数」，绝不写 SQL
- 数据类问题必须调用工具，不得凭空编数字
- 与数据无关的问题（问候/概念解释）可直接回答
"""

from __future__ import annotations

from findata.agent.llm import make_analysis_llm
from findata.agent.nodes.state import MultiAgentState
from findata.agent.tools import usage_of

_SYSTEM = (
    "你是 Findata 可信问数系统里的 SchemaAgent。用户**已经**提出了完整的数据"
    "问题（对话里的用户消息），你的唯一职责是把它映射成语义层指标查询并拿到"
    "数据——不是与用户对话。\n\n"
    "规则：\n"
    "1. 直接调用 metric_query 查询能回答问题的指标。股票名称用常识映射成"
    " 6 位代码（如 茅台=600519、五粮液=000858、宁德时代=300750）；"
    "日期区间按用户给定的填写。\n"
    "2. 需要多个指标或多个标的时，在一轮里并行发起多个 metric_query。\n"
    "3. 只有确实无法判断该用哪个指标时才先调 list_metrics；看到指标列表后"
    "**必须立即**发起 metric_query，不要停下来向用户复述列表或追问。\n"
    "4. 绝不向用户追问、绝不要求补充信息——问题里的信息就是全部输入。\n"
    "5. 查询结果足够回答时，用一句话复述结果并原样保留 run_id，不再调用工具。\n"
    "6. 与数据无关的问题（问候、解释概念）直接回答，无需工具。\n\n"
    "硬性上限（超限会被系统强制收尾，不要试探）：\n"
    "- 全程最多 4 轮工具调用；查询失败时修正参数重查即可，不要更换目标。\n"
    "- 一轮内最多并行 4 个 metric_query；宁可少查，不要发散。"
)


def make_schema_node(tools: list):
    """SchemaAgent 节点工厂。tools 必须绑定，否则真实模型发不出 tool_calls。"""

    def schema_node(state: MultiAgentState) -> dict:
        llm = make_analysis_llm().bind_tools(tools)
        messages = [("system", _SYSTEM), *state["messages"]]
        response = llm.invoke(messages)
        return {
            "messages": [response],
            "usage": usage_of(response),
            "usage_by_agent": {"schema": usage_of(response)},
        }

    return schema_node
