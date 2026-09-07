"""归因器协议。

为什么值得单独抽一个 Protocol：评测 runner 只依赖这个接口，
于是"规则归因器 vs LLM 归因器"可以用同一份语料、同一把尺子量，
换实现不需要动评测代码——增量是多少，是跑出来的，不是讲出来的。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from findata.dq.models import Diagnosis, Finding
from findata.dq.probes import ProbeContext


@runtime_checkable
class Triage(Protocol):
    """把观测信号判成归因结论。"""

    def diagnose_all(self, findings: list[Finding], ctx: ProbeContext) -> list[Diagnosis]: ...


__all__ = ["Triage"]
