"""Agent 轨迹子包：把 Agent 的每一步工具调用落成可归因的轨迹数据。

为什么要有这个包（对应 JD 覆盖矩阵第 4 节）：
  findata 原本只会评「数据」可不可信，不会评「Agent 这一步」可不可信。
  而 Agent 岗要的正是后者——tool calling 为什么失败、失败在哪一步、
  能不能自动归因。

与 dq 层的关系：共用同一套归因哲学（形态相同、结论相反要能区分），
但不共用代码。dq 层不 import agent 层的铁律照旧，这里也一样——
本包不 import dq 层，判定所需的事实由调用方喂进来。
"""

from findata.agentops.schema import (
    SCHEMA_VERSION,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    Step,
    Trace,
    iter_traces,
    write_traces,
)

__all__ = [
    "SCHEMA_VERSION",
    "STATUS_DEGRADED",
    "STATUS_ERROR",
    "STATUS_OK",
    "Step",
    "Trace",
    "iter_traces",
    "write_traces",
]
