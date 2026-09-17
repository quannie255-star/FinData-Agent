"""多智能体子 Agent 节点（M12 拆分自单 Agent 回环）。

- schema.py    SchemaAgent    理解问题 → 选指标 / 填参（原 parse 节点平移）
- query.py     QueryAgent     调语义层编译 SQL 并执行（原 tools 节点平移）
- verifier.py  VerifierAgent  独立交叉验证 + 拉取 DQ 上下文（新增）
- triage.py    TriageAgent    归因子 Agent，包成工具供 VerifierAgent 追问

每个节点只依赖 state 与注入的连接/工具，图拓扑由 supervisor.py 组装。
"""

from findata.agent.nodes.state import MultiAgentState

__all__ = ["MultiAgentState"]
