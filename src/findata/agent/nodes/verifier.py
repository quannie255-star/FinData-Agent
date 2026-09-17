"""VerifierAgent：独立交叉验证 + 拉取 DQ 上下文（M12 新增角色）。

「独立」的含义：它不信任 QueryAgent 的结论。查询摘要以结构化事实的形式
注入（数字 + 验证标记），Verifier 的职责是判断这些验证标记是否足以支撑
答案，并在需要时补两件事——

1. dq_context：拉取该标的近 7 天的 DQ 信号（运行时背书的入口）
2. triage_trust：向 TriageAgent 追问「这批数据可信吗」

与 SchemaAgent 同一条铁律：验证员也不得编造或修改数字，结论只能引用
工具返回的事实。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from findata.agent.llm import make_analysis_llm
from findata.agent.nodes.state import MultiAgentState
from findata.agent.tools import usage_of

# 注入给 Verifier 的查询摘要消息用固定 id：Verifier ⇄ dq_tools 回环时
# add_messages 按 id 原位更新，不会每轮追加一份重复摘要
_DIGEST_ID = "findata-queries-digest"

_SYSTEM = (
    "你是 Findata 可信问数系统里的 VerifierAgent（独立验证员）。\n\n"
    "你会看到 QueryAgent 已执行的查询结果与交叉验证标记。你的职责：\n"
    "1. 检查每个数字的验证状态（✓ verified / ⚠ mismatch / ○ not_verifiable）"
    "是否足以支撑回答用户的问题。\n"
    "2. 需要数据质量背书时，调用 dq_context 查该标的近期信号；"
    "对信号有疑虑时，调用 triage_trust 追问这批数据是否可信。\n"
    "3. 绝不修改或编造数字，结论只能引用工具返回的事实。\n"
    "4. 信息足够后，输出一段验证结论（不再调用工具）：数据是否可信、"
    "引用了哪些背书、对最终答案有什么提示。简洁，不超过三句话。"
)


def _queries_digest(state: MultiAgentState) -> HumanMessage:
    """把 QueryAgent 的结构化查询摘要渲染成验证员可见的事实清单。"""
    queries = state.get("queries") or []
    if not queries:
        content = "【已执行查询】本轮没有成功的 metric_query（可能是非数据类问题）。"
    else:
        lines = []
        for i, q in enumerate(queries, 1):
            if "error" in q:
                lines.append(f"{i}. {q.get('metric')} {q.get('params')} → 查询失败：{q['error']}")
                continue
            lines.append(
                f"{i}. {q['metric']} {q['params']} → {q['value']} {q['unit']} "
                f"[run_id={q['run_id']}] 验证：{q.get('verification')}"
            )
        content = "【已执行查询（数字不可修改）】\n" + "\n".join(lines)
    signals = state.get("dq_signals") or []
    if signals:
        content += f"\n【已拉取的 DQ 信号】{len(signals)} 条"
    return HumanMessage(content=content, id=_DIGEST_ID)


def make_verifier_node(dq_tools: list):
    """VerifierAgent 节点工厂。dq_tools = [dq_context, triage_trust]。"""

    def verifier_node(state: MultiAgentState) -> dict:
        llm = make_analysis_llm().bind_tools(dq_tools)
        messages = [("system", _SYSTEM), _queries_digest(state), *state["messages"]]
        response: AIMessage = llm.invoke(messages)

        update: dict = {
            "messages": [response],
            "verify_rounds": state.get("verify_rounds", 0) + 1,
            "usage": usage_of(response),
            "usage_by_agent": {"verifier": usage_of(response)},
        }
        # 无 tool_calls 的回复即验证结论；带 tool_calls 的中间轮不定结论
        if not response.tool_calls:
            update["verdict"] = response.content
        return update

    return verifier_node
