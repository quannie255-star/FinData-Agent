"""多智能体共享状态契约。

通道设计的两个决定：
- run_ids / queries / dq_signals 用累加 reducer——多轮工具执行的溯源必须
  全部保留（单 Agent 版的 plain channel 只留最后一轮，是个已知小缺陷，
  多智能体版不继承它）。
- usage 用 merge_usage reducer——对比评测要算 token 成本，必须跨节点累计。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from findata.agent.tools import merge_usage


def merge_usage_by_agent(a: dict, b: dict) -> dict:
    """LangGraph reducer：按子 Agent 名分别累计 token 用量。

    Anthropic 复合失败率经验的对偶要求——多智能体的成本也要能归因到
    每个 Agent（「谁花的」和「总共多少」同样重要），否则 effort 分档
    （哪类问题值得走 deep）就只能拍脑袋。
    """
    out = dict(a)
    for name, usage in b.items():
        out[name] = merge_usage(out.get(name, {}), usage)
    return out


class MultiAgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    # 原始问题（post_check 判断"数据类问题"时用，不随消息流改变）
    question: str
    # Supervisor 路由结果：direct（快路径）/ deep（多智能体全链路）
    mode: str
    # 溯源：所有 metric_query 产出的 run_id（累加）
    run_ids: Annotated[list[str], operator.add]
    # 已执行查询的结构化摘要（metric/params/value/verification，累加；
    # 失败的尝试也在——带 error 键，Verifier 需要看到失败才能追问）
    queries: Annotated[list[dict], operator.add]
    # VerifierAgent 拉到的 DQ 信号（累加）
    dq_signals: Annotated[list[dict], operator.add]
    # 轮数上限计数
    tool_rounds: int
    verify_rounds: int
    gate_retries: int
    # VerifierAgent 的验证结论文本
    verdict: str
    # 跨节点累计 token 用量（总量）
    usage: Annotated[dict, merge_usage]
    # 按子 Agent 归因的 token 用量（schema / verifier / finalize）
    usage_by_agent: Annotated[dict, merge_usage_by_agent]


def initial_state(question: str) -> MultiAgentState:
    """图的入口状态。显式初始化全部累加通道，不依赖框架的类型默认值。"""
    return MultiAgentState(
        messages=[],
        question=question,
        mode="direct",
        run_ids=[],
        queries=[],
        dq_signals=[],
        tool_rounds=0,
        verify_rounds=0,
        gate_retries=0,
        usage={},
        usage_by_agent={},
    )
