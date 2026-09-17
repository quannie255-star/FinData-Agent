"""TriageAgent：归因子 Agent，包成工具供 VerifierAgent 追问。

定位（不是新增一个自由发挥的 LLM 角色）：归因走 dq 层既有实现——
规则为主（BaselineTriage）、LLM 为辅（LLMTriage，逐条降级、白名单校验）。
本模块只做两件事：

1. dq_context   查询某标的最近的 DQ 信号（dq_signal 表，含被抑制的误报）
2. triage_trust 追问「这批数据可信吗」：把最近信号喂给归因器，渲染出
   可信结论——"3 天前刚被抑制过一次误报"这类运行时背书从这里出

无 LLM key 时 triage_trust 自动落在规则归因上（诚实降级，不装 LLM），
CI 与本地无网环境照样可测。
"""

from __future__ import annotations

import json

import duckdb
from langchain_core.tools import tool

from findata.dq.history import DEFAULT_LOOKBACK_DAYS, recent_signals, signal_to_row
from findata.dq.models import Finding, Severity
from findata.dq.triage import BaselineTriage

# 工具白名单（验证链路）
DQ_TOOLS = frozenset({"dq_context", "triage_trust"})


def load_recent_findings(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[Finding]:
    """从 dq_signal 历史表还原 Finding 对象（归因器的输入契约）。"""
    rows = recent_signals(conn, symbol=symbol, days=days)
    return [
        Finding(
            probe=r["probe"],
            table=r["table_name"],
            symbol=r["symbol"],
            window=r["window"],
            metric=r["metric"],
            value=float(r["value"]),
            threshold=float(r["threshold"]),
            severity_hint=Severity(r["severity_hint"]),
        )
        for r in rows
    ]


def _make_triage(llm_client=None, auto: bool = False, memory=None):
    """选择归因器。

    - llm_client 显式传入 → 用它（LLMTriage，内含规则降级）
    - llm_client=None 且 auto=True → 自动探测（配置了 key 就用 LLM）
    - llm_client=None 且 auto=False → 纯规则（测试与离线环境的确定性路径）
    - memory（TriageMemory）只在 LLM 路径生效：先例注入 + 结论回流。
      规则归因是确定性的，记忆对它没有增量，不挂。
    """
    if llm_client is None and auto:
        from findata.agent.llm import OpenAICompatClient

        client = OpenAICompatClient()
        if client.available:
            llm_client = client
    if llm_client is None:
        return BaselineTriage()
    from findata.agent.llm_triage import LLMTriage

    return LLMTriage(client=llm_client, memory=memory)


def _probe_context(conn):
    """用仓库本身构造 ProbeContext（归因器需要的证据：停牌/上市日/血缘）。"""
    from findata.report.inspect import Snapshot

    return Snapshot.from_duckdb(conn).to_probe_context()


def build_dq_tools(conn: duckdb.DuckDBPyConnection, llm_client=None, auto: bool = True) -> list:
    """构造验证链路工具集合（VerifierAgent 绑定 + dq_tools 节点执行共用）。

    auto=True（默认，生产路径）：未显式给 llm_client 时按配置自动探测；
    测试传 llm_client=None + auto=False 即锁定规则归因，离线可复现。
    """

    @tool("dq_context")
    def dq_context(symbol: str, days: int = DEFAULT_LOOKBACK_DAYS) -> str:
        """查询某标的最近 N 天的数据质量信号，包括已告警与已抑制（误报）的记录。

        symbol 是 6 位股票代码，如 600519。days 为回看天数（1-30，默认 7）。
        返回 JSON 数组：每条含 asof / probe / root_cause / severity /
        suppressed / explanation。为空数组表示近期无任何数据质量信号。
        """
        rows = recent_signals(conn, symbol=symbol, days=max(1, min(int(days), 30)))
        return json.dumps([signal_to_row(r) for r in rows], ensure_ascii=False, indent=1)

    @tool("triage_trust")
    def triage_trust(symbol: str) -> str:
        """追问"这批数据可信吗"：对指定标的最近的数据质量信号跑根因归因
        （规则为主、LLM 为辅），返回这批数据是否可信的结论。

        symbol 是 6 位股票代码。返回按信号的逐条归因结论：
        benign_* / 已抑制 = 数据正常，告警是误报；真实故障根因 = 数据不可信。
        """
        findings = load_recent_findings(conn, symbol)
        if not findings:
            return f"{symbol} 近 {DEFAULT_LOOKBACK_DAYS} 天无数据质量信号，未见已知异常。"
        ctx = _probe_context(conn)
        memory = None
        if llm_client is not None or auto:
            # M13：LLM 归因路径挂经验记忆——已确认先例注入 prompt，结论回流。
            # 记忆挂在与仓库同库的 triage_memory 表；打不开就退化为无记忆
            # （best-effort，与 dq_signal 落库同一取舍：增强不能打挂主职责）。
            try:
                from findata.dq.memory import TriageMemory

                memory = TriageMemory(conn)
            except Exception:
                memory = None
        triager = _make_triage(llm_client, auto=auto, memory=memory)
        diagnoses = triager.diagnose_all(findings, ctx)
        lines = []
        for d in diagnoses:
            tag = (
                "已抑制（合法业务事件，数据正常）"
                if d.suppressed
                else f"真实故障（{d.severity.value}）"
            )
            lines.append(
                f"[{d.root_cause.value}] {tag} 置信度 {d.confidence:.0%}：{d.explanation}"
            )
        n_bad = sum(1 for d in diagnoses if not d.suppressed)
        verdict = (
            f"结论：{symbol} 近期数据存在 {n_bad} 个未消解的质量问题，谨慎使用。"
            if n_bad
            else f"结论：{symbol} 近期信号均为合法业务事件（已抑制），数据可信。"
        )
        return "\n".join(lines) + "\n" + verdict

    return [dq_context, triage_trust]
