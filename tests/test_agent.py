"""Agent 编排层测试：图结构、工具绑定、路由（不依赖真实 LLM）。"""

from __future__ import annotations

import pandas as pd
import pytest

from findata.agent import graph as g
from findata.core.db import connect, upsert_dataframe


@pytest.fixture()
def conn(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    daily = pd.DataFrame(
        {
            "symbol": ["600519"],
            "date": [pd.to_datetime("2024-01-04").date()],
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [121.0],
            "volume": 1000.0,
            "amount": 1e6,
            "pct_chg": 0.0,
            "turnover": 0.0,
        }
    )
    upsert_dataframe(conn, "stock_daily", daily, ["symbol", "date"])
    yield conn
    conn.close()


def test_tools_whitelist_only_two(conn):
    tools = g._build_tools(conn)
    names = {t.name for t in tools}
    assert names == {"metric_query", "list_metrics"}


def test_metric_query_tool_returns_run_id(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["metric_query"].invoke({"metric": "close_price", "params": {"symbol": "600519"}})
    assert "121.0" in out
    assert "run_id=" in out


def test_list_metrics_tool(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["list_metrics"].invoke({})
    assert "close_price" in out
    assert "pct_change" in out
    assert "valuation_pe_ttm" in out


def test_invalid_metric_tool_returns_error_text(conn):
    tools = {t.name: t for t in g._build_tools(conn)}
    out = tools["metric_query"].invoke({"metric": "not_exist", "params": {}})
    assert "查询失败" in out or "未知指标" in out


def test_graph_compiles(conn):
    agent = g.build_agent(conn)
    assert agent is not None
    assert agent._app is not None


def test_allowed_tools_constant():
    assert g.ALLOWED_TOOLS == {"metric_query", "list_metrics"}


# ─────────────────────── 回环 / 兜底 / 溯源提取（脚本化假 LLM） ───────────────────────


class _FakeModel:
    """脚本化假模型：bind_tools 后按队列返回预置 AIMessage。

    用于验证图的运行时行为（此前只测过图结构，对话流零覆盖）。
    注意：必须共享同一个 replies 列表而不是拷贝——图里每个节点都会重新
    构建 chat model，拷贝会让每个节点都从头弹出同一条消息（同一个
    AIMessage 对象 id 相同，langgraph 的 add_messages 按 id 原位更新，
    消息序列永远不前进）。
    """

    def __init__(self, replies):
        self._replies = replies

    def bind_tools(self, tools):  # 与 ChatOpenAI.bind_tools 同签名
        return self

    def invoke(self, messages):
        return self._replies.pop(0)


def _tool_call_ai(call_id: str):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "metric_query",
                "args": {"metric": "close_price", "params": {"symbol": "600519"}},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_agent_multi_round_flow_with_fake_llm(conn, monkeypatch):
    """parse→tools→parse（看到结果不再调工具）→finalize 的完整对话流。"""
    from langchain_core.messages import AIMessage

    replies = [
        _tool_call_ai("call_1"),
        AIMessage(content="已拿到结果"),  # 第二轮 parse：无 tool_calls → finalize
        AIMessage(content="最终答案"),
    ]
    monkeypatch.setattr(g, "make_chat_model", lambda temperature=0.0: _FakeModel(replies))
    agent = g.build_agent(conn)
    result = agent.invoke("茅台最新收盘价？")

    assert result["tool_rounds"] == 1
    # run_ids 必须来自真实工具结果且是干净的 16 进制 id（回归：此前按括号切，
    # 会把验证标记一起带进 run_ids）
    assert result["run_ids"], "工具真实执行了，run_ids 不应为空"
    import re

    assert all(re.fullmatch(r"[0-9a-f]+", r) for r in result["run_ids"])
    for _m in result["messages"]:
        print("MSG:", type(_m).__name__, "|", repr(_m.content)[:50])
    assert result["messages"][-1].content == "最终答案"


def test_agent_tool_loop_is_capped(conn, monkeypatch):
    """LLM 陷入'查了再查'时，回环必须在 MAX_TOOL_ROUNDS 轮后被强制掐断。"""
    counter = {"n": 0}

    class _AlwaysCall(_FakeModel):
        def invoke(self, messages):
            counter["n"] += 1
            return _tool_call_ai(f"call_{counter['n']}")

    monkeypatch.setattr(g, "make_chat_model", lambda temperature=0.0: _AlwaysCall([]))
    agent = g.build_agent(conn)
    result = agent.invoke("反复查询")
    assert result["tool_rounds"] == g.MAX_TOOL_ROUNDS


def test_agent_missing_key_gives_actionable_error(conn, monkeypatch):
    """LLM 未配置时报错必须可行动（指明环境变量），而不是裸 SDK 异常。"""

    def _boom(temperature=0.0):
        raise RuntimeError("Missing credentials")

    monkeypatch.setattr(g, "make_chat_model", _boom)
    agent = g.build_agent(conn)
    with pytest.raises(RuntimeError, match="FINDATA_LLM_API_KEY"):
        agent.invoke("问题")
