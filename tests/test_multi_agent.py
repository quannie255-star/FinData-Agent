"""多智能体测试：Supervisor 路由全分支、溯源 gate、验证链路、端到端对话流。

不依赖真实 LLM：分析链路用脚本化假模型（monkeypatch make_chat_model），
归因链路用 MockLLMClient / BaselineTriage。假模型的队列必须**共享同一个
list**（图里每个节点都会重新构建 chat model，拷贝会让每个节点都从头弹出
同一条消息；且 AIMessage 对象必须是每次新建的——langgraph 的 add_messages
按 id 原位更新，同一个对象会让消息序列永远不前进）。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from findata.agent import llm as llm_mod
from findata.agent import supervisor as sp
from findata.agent.nodes import verifier as verifier_mod
from findata.agent.nodes.triage import build_dq_tools, load_recent_findings
from findata.agent.supervisor import (
    build_multi_agent,
    judge_complexity,
    post_check_node,
)
from findata.core.db import connect
from findata.dq.history import recent_signals, record_signals
from findata.dq.models import Diagnosis, Finding, RootCause, Severity

# ─────────────────────────── fixture 数据 ───────────────────────────


@pytest.fixture()
def conn(tmp_path):
    """合成 fixture 仓库（与 eval/golden 数值同源）。"""
    from scripts.build_eval_fixture import build

    db = tmp_path / "ma_fixture.duckdb"
    build(str(db))
    c = connect(str(db))
    yield c
    c.close()


def _add_signals(conn):
    """预置 DQ 信号历史：1 条已抑制（停牌误报）+ 1 条真实故障（停更）。"""
    record_signals(
        conn,
        [
            (
                Finding(
                    probe="freshness", table="stock_daily", symbol="600519",
                    window="2024-01-01..2024-01-02", metric="stale_trading_days",
                    value=2, threshold=1, severity_hint=Severity.P1,
                ),
                Diagnosis(
                    finding_key="f1", root_cause=RootCause.BENIGN_SUSPENSION,
                    severity=Severity.OK, confidence=0.95,
                    explanation="窗口内全部交易日处于停牌区间",
                ),
            ),
            (
                Finding(
                    probe="completeness", table="stock_daily", symbol="000858",
                    window="2024-01-03..2024-01-03", metric="missing_rows",
                    value=1, threshold=0, severity_hint=Severity.P1,
                ),
                Diagnosis(
                    finding_key="f2", root_cause=RootCause.UPSTREAM_STALE,
                    severity=Severity.P1, confidence=0.8,
                    explanation="无停牌/新股可解释，判定为上游停更",
                ),
            ),
        ],
        asof=date(2024, 1, 4),
    )


def _add_suspension(conn):
    """给 600519 补停牌事件：让 triage_trust 重归因时有证据可依。

    归因器只认当前仓库里的证据——dq_signal 存了当时的抑制结论，但
    triage_trust 是"重新归因"，仓库里没有停牌记录就会诚实地判成故障。
    """
    conn.execute(
        "INSERT INTO corporate_event VALUES ('600519', '2024-01-01', 'suspension',"
        " '2024-01-02', '测试停牌')"
    )


class _FakeModel:
    """脚本化假模型：共享队列 + 队列耗尽后的默认回复。"""

    def __init__(self, queue: list, default: str = "默认回复"):
        self._queue = queue
        self._default = default

    def bind_tools(self, tools):  # 与 ChatOpenAI.bind_tools 同签名
        return self

    def invoke(self, messages):
        return self._queue.pop(0) if self._queue else AIMessage(content=self._default)


def _patch_llm(monkeypatch, queue: list, default: str = "默认回复") -> None:
    monkeypatch.setattr(
        llm_mod, "make_chat_model", lambda temperature=0.0: _FakeModel(queue, default)
    )


def _call_metric(cid: str, metric: str, params: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "metric_query", "args": {"metric": metric, "params": params},
             "id": cid, "type": "tool_call"}
        ],
    )


# ─────────────────────────── Supervisor 路由全分支 ───────────────────────────


@pytest.mark.parametrize(
    ("question", "want"),
    [
        # direct：单指标、单标的
        ("茅台最新收盘价是多少？", "direct"),
        ("五粮液值多少钱", "direct"),
        ("600519 收盘价", "direct"),
        ("茅台最近市盈率多少", "direct"),
        # deep：对比/聚合词
        ("对比茅台和五粮液的最新收盘价", "deep"),
        ("茅台和五粮液哪个更便宜", "deep"),
        ("这几只股票的平均涨幅是多少", "deep"),
        ("两只股票的涨幅分别是多少", "deep"),
        # deep：多标的（代码）
        ("600519 和 000858 的收盘价", "deep"),
        # deep：多指标关键词
        ("茅台最新收盘价和区间涨幅分别是多少？", "deep"),
        ("茅台的市盈率和股价", "deep"),
    ],
)
def test_judge_complexity_all_branches(question, want):
    assert judge_complexity(question) == want


@pytest.mark.parametrize(
    ("question", "want"),
    [
        ("茅台的市盈率是多少", True),
        ("600519 最新收盘价", True),  # 代码命中
        ("什么是市盈率？", False),  # 概念问题
        ("解释一下涨跌幅的含义", False),
        ("你好", False),  # 闲聊
    ],
)
def test_is_data_question(question, want):
    assert sp.is_data_question(question) == want


def test_supervisor_node_sets_mode():
    state = sp.supervisor_node({"question": "对比茅台和五粮液的收盘价"})
    assert state == {"mode": "deep"}
    state = sp.supervisor_node({"question": "茅台最新收盘价"})
    assert state == {"mode": "direct"}


# ─────────────────────────── 图结构 ───────────────────────────


def test_multi_agent_compiles(conn):
    agent = build_multi_agent(conn)
    assert agent is not None
    assert agent.app is not None


# ─────────────────────────── 端到端：direct 快路径 ───────────────────────────


def test_direct_fast_path_skips_verifier(conn, monkeypatch):
    """简单题走快路径：schema→tools→schema→gate→finalize，verifier 不参与。"""
    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        AIMessage(content="拿到结果了"),  # schema 看到结果，不再调工具
        AIMessage(content="茅台最新收盘价 121.0 元 [run_id=abc123] ✓ 已交叉验证"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")

    assert state["mode"] == "direct"
    assert state["tool_rounds"] == 1
    assert state["verify_rounds"] == 0, "快路径不该有验证轮"
    assert len(state["run_ids"]) == 1
    assert state["queries"][0]["metric"] == "close_price"
    assert state["queries"][0]["verification"] == "verified"
    assert state["messages"][-1].content.startswith("茅台最新收盘价")
    assert "verdict" not in state


def test_direct_flow_accumulates_usage(conn, monkeypatch):
    """usage 跨节点累加（对比评测的 token 成本来源）。"""
    um = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    def _ai(content, **kw):
        return AIMessage(content=content, usage_metadata=dict(um), **kw)

    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        _ai("拿到结果了"),
        _ai("121.0 元 [run_id=abc123] ✓"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")
    # 两次带 usage 的 LLM 调用（schema 看结果 + finalize）；tool_call 消息不耗 token
    assert state["usage"] == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}


def test_data_tools_node_rejects_non_whitelist(conn, monkeypatch):
    """白名单外的工具调用必须被拒绝（多智能体不允许比单 Agent 更自由）。"""

    def _forbidden_call():
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "drop_table", "args": {"table": "stock_daily"},
                 "id": "cx", "type": "tool_call"}
            ],
        )

    queue = [_forbidden_call(), AIMessage(content="好的")]
    _patch_llm(monkeypatch, queue, default="茅台收盘价 [run_id=abc123] ✓")

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")
    tool_msgs = [m for m in state["messages"] if m.type == "tool"]
    assert any("不允许调用工具" in m.content for m in tool_msgs)
    assert state["run_ids"] == [], "被拒绝的调用不得产生溯源"


# ─────────────────────────── 溯源 gate ───────────────────────────


def test_gate_retries_once_then_flags_untrusted(conn, monkeypatch):
    """查了但拿不到 run_id：gate 打回补查一次，仍失败则带警告放行（不静默）。"""
    queue = [
        _call_metric("c1", "not_exist", {}),  # 查询失败，无 run_id
        AIMessage(content="查询失败了"),  # 补查轮：schema 直接放弃
        AIMessage(content="抱歉查不到该数据"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")
    final = state["messages"][-1].content
    assert "溯源检查未通过" in final, "数据类问题无 run_id 必须显式标注"
    assert state["gate_retries"] == 2, "gate 恰好重试一次（两次经过都计数）"


def test_gate_passes_concept_question_without_run_id(conn, monkeypatch):
    """非数据类问题（闲聊/概念）不需要 run_id，post_check 不得误伤。"""
    queue = [
        AIMessage(content="你好！我是 Findata 可信问数助手。"),  # schema 直接回答
        AIMessage(content="你好！我是 Findata 可信问数助手。"),  # finalize
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("你好")
    assert "溯源检查未通过" not in state["messages"][-1].content
    assert state["tool_rounds"] == 0


def test_tool_loop_capped_in_multi_agent(conn, monkeypatch):
    """多智能体的回环上限与单 Agent 一致：MAX_TOOL_ROUNDS 后强制收尾。"""

    counter = {"n": 0}

    class _AlwaysCall(_FakeModel):
        def invoke(self, messages):
            counter["n"] += 1
            return _call_metric(f"c{counter['n']}", "close_price", {"symbol": "600519"})

    monkeypatch.setattr(llm_mod, "make_chat_model", lambda temperature=0.0: _AlwaysCall([]))
    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")
    assert state["tool_rounds"] == sp._MAX_TOOL_ROUNDS
    assert len(state["run_ids"]) == sp._MAX_TOOL_ROUNDS


# ─────────────────────────── 端到端：deep 全链路 ───────────────────────────


def test_deep_full_flow_with_verifier_and_dq_context(conn, monkeypatch):
    """复杂题：schema 两轮查两标的 → verifier 拉 dq_context → 结论 → finalize。"""
    _add_signals(conn)
    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        _call_metric("c2", "close_price", {"symbol": "000858"}),
        AIMessage(content="两个标的都查到了"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dq_context", "args": {"symbol": "600519"},
                 "id": "c3", "type": "tool_call"}
            ],
        ),
        AIMessage(content="验证结论：600519 曾有停牌误报已抑制，数据可信"),
        AIMessage(content="茅台 121.0 元，五粮液 72.0 元 [run_id=abc123] ✓"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("对比茅台和五粮液的最新收盘价")

    assert state["mode"] == "deep"
    assert state["tool_rounds"] == 2
    assert state["verify_rounds"] == 2
    assert len(state["run_ids"]) == 2
    assert len(state["queries"]) == 2
    assert len(state["dq_signals"]) == 1, "dq_context 的 JSON 信号要收进 state"
    assert state["dq_signals"][0]["root_cause"] == "benign_suspension"
    assert "停牌误报" in state["verdict"]
    assert state["messages"][-1].content.startswith("茅台")


def test_verifier_digest_updated_in_place(conn, monkeypatch):
    """验证摘要按固定 id 注入：每轮 verifier 输入里有且仅有一份，不重复堆叠。"""
    _add_signals(conn)
    seen: list[list] = []  # 记录每次 llm.invoke 的输入

    class _Spy(_FakeModel):
        def invoke(self, messages):
            seen.append(list(messages))
            return super().invoke(messages)

    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        AIMessage(content="查到了"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dq_context", "args": {"symbol": "600519"}, "id": "c2",
                 "type": "tool_call"}
            ],
        ),
        AIMessage(content="数据可信"),
        AIMessage(content="121.0 元 [run_id=abc123] ✓"),
    ]
    monkeypatch.setattr(
        llm_mod, "make_chat_model", lambda temperature=0.0: _Spy(queue)
    )

    build_multi_agent(conn).invoke("茅台最新收盘价是多少？顺便对比一下数据质量信号可信吗")
    # verifier 被调两轮（dq_context 前后），每轮输入恰好一份查询摘要
    verifier_inputs = [
        msgs for msgs in seen
        if any(getattr(m, "id", None) == verifier_mod._DIGEST_ID for m in msgs)
    ]
    assert len(verifier_inputs) == 2, "verifier 应被调用两轮"
    for msgs in verifier_inputs:
        digests = [m for m in msgs if getattr(m, "id", None) == verifier_mod._DIGEST_ID]
        assert len(digests) == 1, "每轮输入只应有一份摘要"
    # 第二轮的摘要应包含 dq_context 之后的信号计数提示
    second = [
        m.content
        for m in verifier_inputs[1]
        if getattr(m, "id", None) == verifier_mod._DIGEST_ID
    ][0]
    assert "1 条" in second


# ─────────────────────────── TriageAgent 工具 ───────────────────────────


def test_dq_context_tool_returns_json(conn):
    _add_signals(conn)
    tools = {t.name: t for t in build_dq_tools(conn, llm_client=None, auto=False)}
    out = json.loads(tools["dq_context"].invoke({"symbol": "600519"}))
    assert len(out) == 1
    assert out[0]["root_cause"] == "benign_suspension"
    assert out[0]["suppressed"] is True

    empty = json.loads(tools["dq_context"].invoke({"symbol": "300750"}))
    assert empty == []


def test_triage_trust_falls_back_to_rules_without_llm(conn):
    """无 LLM 客户端：triage_trust 落在规则归因（诚实降级，不装 LLM）。"""
    _add_signals(conn)
    _add_suspension(conn)
    tools = {t.name: t for t in build_dq_tools(conn, llm_client=None, auto=False)}
    out = tools["triage_trust"].invoke({"symbol": "000858"})
    assert "真实故障" in out and "谨慎使用" in out

    ok = tools["triage_trust"].invoke({"symbol": "600519"})
    assert "数据可信" in ok, "仓库里有停牌证据 → 重归因仍是误报抑制"

    none = tools["triage_trust"].invoke({"symbol": "300750"})
    assert "无数据质量信号" in none


def test_triage_trust_reattributes_on_current_evidence(conn):
    """仓库证据与历史结论矛盾时，triage_trust 按当前证据诚实改判。

    这是「一个像的事实比没有事实更危险」的反向应用：不给它编造证据的
    机会，没有停牌记录就不许说"停牌误报"。
    """
    _add_signals(conn)  # 存了"停牌误报已抑制"的结论，但仓库没有停牌事件
    tools = {t.name: t for t in build_dq_tools(conn, llm_client=None, auto=False)}
    out = tools["triage_trust"].invoke({"symbol": "600519"})
    assert "真实故障" in out, "无停牌证据时不得沿用历史抑制结论"


def test_triage_trust_in_graph_uses_rule_fallback(conn, monkeypatch):
    """图内 VerifierAgent 追问 triage_trust：真实 key 存在也必须走规则降级。"""
    # .env 里有真实 key；这里把 OpenAICompatClient 打成不可用，保证测试离线确定
    class _Unavailable:
        available = False

    monkeypatch.setattr(llm_mod, "OpenAICompatClient", _Unavailable)
    _add_signals(conn)
    queue = [
        _call_metric("c1", "close_price", {"symbol": "000858"}),
        AIMessage(content="查到了"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "triage_trust", "args": {"symbol": "000858"},
                 "id": "c2", "type": "tool_call"}
            ],
        ),
        AIMessage(content="验证结论：000858 存在未消解质量问题"),
        AIMessage(content="五粮液 72.0 元 [run_id=abc123] ⚠"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("五粮液最新收盘价是多少？对比一下这批数据可信吗")
    tool_texts = [m.content for m in state["messages"] if m.type == "tool"]
    assert any("谨慎使用" in t for t in tool_texts), "triage_trust 结论应进入对话"
    assert "未消解" in state["verdict"]


def test_load_recent_findings_roundtrip(conn):
    _add_signals(conn)
    findings = load_recent_findings(conn, "600519")
    assert len(findings) == 1
    f = findings[0]
    assert f.probe == "freshness"
    assert f.symbol == "600519"
    assert f.severity_hint is Severity.P1


# ─────────────────────────── DQ 信号历史 ───────────────────────────


def test_record_signals_idempotent(conn):
    pair = (
        Finding(
            probe="freshness", table="stock_daily", symbol="600519",
            window="2024-01-01..2024-01-02", metric="stale_trading_days",
            value=2, threshold=1, severity_hint=Severity.P1,
        ),
        Diagnosis(
            finding_key="f1", root_cause=RootCause.UPSTREAM_STALE,
            severity=Severity.P1, confidence=0.8, explanation="v1",
        ),
    )
    record_signals(conn, [pair], asof=date(2024, 1, 4))
    # 同 (asof, finding_key) 重跑 → 覆盖不重复
    newer = (
        pair[0],
        Diagnosis(
            finding_key="f1", root_cause=RootCause.BENIGN_SUSPENSION,
            severity=Severity.OK, confidence=0.95, explanation="v2",
        ),
    )
    record_signals(conn, [newer], asof=date(2024, 1, 4))
    rows = recent_signals(conn, symbol="600519")
    assert len(rows) == 1
    assert rows[0]["root_cause"] == "benign_suspension"
    assert rows[0]["explanation"] == "v2"


def test_recent_signals_window_and_anchor(conn):
    _add_signals(conn)  # asof=2024-01-04
    # 更早的信号：锚点取表内 max(asof)，8 天前落在 7 天窗外
    record_signals(
        conn,
        [
            (
                Finding(
                    probe="validity", table="stock_daily", symbol="600519",
                    window="2023-12-20..2023-12-20", metric="pct_chg_out_of_limit",
                    value=1, threshold=0, severity_hint=Severity.P0,
                ),
                Diagnosis(
                    finding_key="old", root_cause=RootCause.VALUE_OUT_OF_RANGE,
                    severity=Severity.P0, confidence=0.9, explanation="旧信号",
                ),
            )
        ],
        asof=date(2023, 12, 20),
    )
    assert len(recent_signals(conn)) == 2, "7 天窗内只有 2 条（锚点 2024-01-04）"
    assert len(recent_signals(conn, days=30)) == 3
    assert len(recent_signals(conn, symbol="000858")) == 1
    replayed = recent_signals(conn, asof=date(2023, 12, 20), days=7)
    assert len(replayed) == 1 and replayed[0]["probe"] == "validity", (
        "历史回放不得读到锚点之后的信号"
    )


def test_post_check_appends_warning_for_data_question():
    from langchain_core.messages import AIMessage as AM

    state = {
        "question": "茅台最新收盘价是多少？",
        "messages": [AM(content="茅台股价大约 1800 元")],  # 无 run_id
    }
    out = post_check_node(state)
    assert "溯源检查未通过" in out["messages"][0].content


def test_post_check_passthrough_with_run_id():
    from langchain_core.messages import AIMessage as AM

    state = {
        "question": "茅台最新收盘价是多少？",
        "messages": [AM(content="121.0 元 [run_id=abcd1234] ✓")],
    }
    assert post_check_node(state) == {}


def test_post_check_passthrough_for_concept_question():
    from langchain_core.messages import AIMessage as AM

    state = {
        "question": "什么是市盈率？",
        "messages": [AM(content="市盈率是股价除以每股收益的比值。")],
    }
    assert post_check_node(state) == {}


# ─────────────────────────── HumanMessage 兼容性 ───────────────────────────


def test_initial_state_shape():
    from findata.agent.nodes.state import initial_state

    s = initial_state("测试问题")
    assert s["question"] == "测试问题"
    assert s["mode"] == "direct"
    assert s["run_ids"] == [] and s["queries"] == [] and s["dq_signals"] == []
    assert s["tool_rounds"] == 0 and s["verify_rounds"] == 0 and s["gate_retries"] == 0
    assert isinstance(s["messages"], list)
    assert HumanMessage is not None  # 保持 import 有意义


def test_invoke_seeds_question_into_messages(conn, monkeypatch):
    """回归：invoke 必须把问题作为首条用户消息注入对话。

    此前漏掉这行，真实模型看到空对话只能照 system prompt 里的示例瞎猜
    （评测 7 题全答成"茅台+五粮液收盘价"）；脚本化假模型不读输入，抓不住。
    """
    seen: list[list] = []

    class _Spy(_FakeModel):
        def invoke(self, messages):
            seen.append(list(messages))
            return super().invoke(messages)

    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        AIMessage(content="收到"),
        AIMessage(content="121.0 元 [run_id=abcd1234] ✓"),
    ]
    monkeypatch.setattr(llm_mod, "make_chat_model", lambda temperature=0.0: _Spy(queue))

    build_multi_agent(conn).invoke("宁德时代最新收盘价是多少？")
    first_user = [
        m for m in seen[0] if isinstance(m, HumanMessage)
    ]
    assert first_user, "SchemaAgent 的输入里必须有用户消息"
    assert "宁德时代" in first_user[0].content, "用户消息必须携带原始问题"


# ─────────────────────────── 错误自修正循环（WrenAI 模式） ───────────────────────────


def test_query_self_correction_loop(conn, monkeypatch):
    """查询参数错误 → SchemaAgent 看到错误文本 → 修正重查 → 成功。

    对标 WrenAI 的 sql_correction 循环（dry-run 失败 → 带错误重试 → 轮数
    上限）：我们的版本是"工具返回错误文本 → schema 修正参数"，回环上限
    由 MAX_TOOL_ROUNDS 强制。失败尝试必须记录在 state 里——分步漏斗要
    统计查询执行成功率，Verifier 也要看到失败才能追问。
    """
    queue = [
        _call_metric("c1", "close_price", {}),  # 缺 symbol → 查询失败
        _call_metric("c2", "close_price", {"symbol": "600519"}),  # 修正后成功
        AIMessage(content="这次查到了"),
        AIMessage(content="121.0 元 [run_id=abcd1234] ✓"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")

    assert state["tool_rounds"] == 2
    assert len(state["queries"]) == 2
    assert "error" in state["queries"][0], "失败尝试必须记录在案"
    assert "error" not in state["queries"][1] and state["queries"][1]["run_id"]
    assert state["messages"][-1].content.startswith("121.0")


def test_usage_attributed_by_agent(conn, monkeypatch):
    """token 用量按子 Agent 归因（成本分摊到 schema/verifier/finalize）。"""
    um = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    def _ai(content):
        return AIMessage(content=content, usage_metadata=dict(um))

    queue = [
        _call_metric("c1", "close_price", {"symbol": "600519"}),
        _ai("拿到结果了"),
        _ai("121.0 元 [run_id=abcd1234] ✓"),
    ]
    _patch_llm(monkeypatch, queue)

    state = build_multi_agent(conn).invoke("茅台最新收盘价是多少？")
    aba = state["usage_by_agent"]
    assert aba["schema"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }, "只有带 usage_metadata 的回复计入（tool_call 消息不耗 token）"
    assert aba["finalize"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert aba.get("verifier", {}).get("total_tokens", 0) == 0, "快路径不该有 verifier 开销"
