"""Agent 轨迹 schema：对齐主流大模型 API 的 messages / tool_calls 形状。

刻意在形状上对齐 OpenAI 那一套，而不是自造一个更"干净"的结构：
  · JD 里的硬要求是「看懂主流大模型 API 的数据组织方式（消息结构、
    tool calls、流式返回）」，自造格式等于没练过；
  · 轨迹是要给下游训练与评测消费的，形状对齐才能直接喂，不用再写一层
    转换（每多一层转换，就多一处口径漂移）。

落盘用 **JSONL 不是 JSON**：轨迹是流式追加的，跑 1 万条不该把 1 万条都读
进内存。读写两端都用生成器，这本身就是 JD 里"大文件流式处理"的练兵场——
不是另写个 demo，而是在真实链路里就得这么干。

与 dq 层的关系：dq 层管**数据**可不可信，这里管**Agent 这一步**可不可信。
两者共用同一套归因哲学（形态相同、结论相反要能区分），但不共用代码——
dq 层不 import agent 层的铁律照旧。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"

# 步骤状态。degraded 单独一档，不并进 error：降级是护栏生效、链路仍然
# 给出了答案，把它和"跑挂了"混在一起会让归因失真。
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"


@dataclass
class Step:
    """一次工具调用。字段命名对齐 OpenAI tool_calls（name / arguments / result）。"""

    seq: int
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_OK
    error: str = ""
    ts: str = ""
    latency_ms: float = 0.0
    usage_chars: int = 0

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now().isoformat(timespec="milliseconds")

    @property
    def failed(self) -> bool:
        return self.status in (STATUS_DEGRADED, STATUS_ERROR)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = "tool_call"  # 与 messages 里的 role 体系对齐，预留给下游
        return {k: v for k, v in d.items() if v not in ({}, "", 0.0, 0)}


@dataclass
class Trace:
    """一次 Agent 运行的完整轨迹。"""

    task: str
    agent: str = "findata-chatbi"
    run_id: str = ""
    parser: str = "rule"
    started_at: str = ""
    ended_at: str = ""
    steps: list[Step] = field(default_factory=list)
    outcome: dict[str, Any] = field(default_factory=dict)
    # golden 一并落盘：轨迹本身不带标签就无法归因，只能看不能判。
    golden: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = uuid.uuid4().hex[:12]
        if not self.started_at:
            self.started_at = datetime.now().isoformat(timespec="milliseconds")

    def step(
        self,
        name: str,
        *,
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        status: str = STATUS_OK,
        error: str = "",
        usage_chars: int = 0,
    ) -> Step:
        s = Step(
            seq=len(self.steps) + 1,
            name=name,
            arguments=arguments or {},
            result=result or {},
            status=status,
            error=error,
            usage_chars=usage_chars,
        )
        self.steps.append(s)
        return s

    def finish(self, **outcome: Any) -> None:
        self.outcome = outcome
        self.ended_at = datetime.now().isoformat(timespec="milliseconds")

    # ── 派生量：给探针与归因用 ──

    @property
    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if s.failed]

    @property
    def n_degraded(self) -> int:
        return sum(s.status == STATUS_DEGRADED for s in self.steps)

    @property
    def total_chars(self) -> int:
        return sum(s.usage_chars for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "agent": self.agent,
            "parser": self.parser,
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [s.to_dict() for s in self.steps],
            "outcome": self.outcome,
            "golden": self.golden,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def write_traces(path: str | Path, traces: Iterator[Trace] | list[Trace]) -> int:
    """流式写 JSONL：一条一行，边生成边落盘，不把全量堆在内存里。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as f:
        for t in traces:
            f.write(t.to_json() + "\n")
            n += 1
    return n


def iter_traces(path: str | Path) -> Iterator[Trace]:
    """流式读 JSONL：生成器逐行吐，1GB 文件也不会把内存吃光。"""
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            steps = [
                Step(
                    seq=s["seq"],
                    name=s["name"],
                    arguments=s.get("arguments", {}),
                    result=s.get("result", {}),
                    status=s.get("status", STATUS_OK),
                    error=s.get("error", ""),
                    ts=s.get("ts", ""),
                    latency_ms=s.get("latency_ms", 0.0),
                    usage_chars=s.get("usage_chars", 0),
                )
                for s in d.get("steps", [])
            ]
            yield Trace(
                task=d.get("task", ""),
                agent=d.get("agent", ""),
                run_id=d.get("run_id", ""),
                parser=d.get("parser", "rule"),
                started_at=d.get("started_at", ""),
                ended_at=d.get("ended_at", ""),
                steps=steps,
                outcome=d.get("outcome", {}),
                golden=d.get("golden", {}),
            )
