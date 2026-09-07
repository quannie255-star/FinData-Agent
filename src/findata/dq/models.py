"""数据质量核心数据契约。

设计要点（面试可讲的三个决定）：

1. **Finding 与 Diagnosis 分离**。Finding 是探针看到的客观信号，不含判断；
   Diagnosis 才是带业务背景的结论。探针因此可以被单独评测（能不能发现问题），
   归因器可以被整体替换（规则 → LLM）而不动探针。

2. **BENIGN_* 是一等公民**。停牌、除权、新股上市、非交易日都会触发异常信号，
   但它们不是故障。把它们建模成根因标签而非"过滤掉"，是因为
   「识别合法异常」本身就是这个 Agent 的核心能力——告警疲劳才是行业真痛点，
   能不能抑制误报直接决定产品有没有人用。

3. **证据字段强制非空**。每个 Finding 都必须能回答"凭什么这么说"，
   这是后续引用绑定与人工复核的基础。没有证据的告警等于没有告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """告警级别。OK 表示已确认为合法业务事件，告警被主动抑制。"""

    P0 = "P0"  # 阻断：数据不可用，下游应停止消费
    P1 = "P1"  # 重要：影响部分分析结论，需人工确认
    P2 = "P2"  # 提示：可观测但可接受，记录即可
    OK = "OK"  # 抑制：合法业务事件，不告警


class RootCause(StrEnum):
    """根因标签。前缀 BENIGN_ 表示合法业务事件，不应告警。"""

    # ---- 真实故障 ----
    UPSTREAM_OUTAGE = "upstream_outage"  # 上游采集失败（血缘 run 失败/中断）
    UPSTREAM_STALE = "upstream_stale"  # 上游停更，超过预期新鲜度
    MISSING_ROWS = "missing_rows"  # 交易日内应到未到的行缺失
    DUPLICATE_WRITE = "duplicate_write"  # 重复写入导致主键重复
    VALUE_OUT_OF_RANGE = "value_out_of_range"  # 值域越界（负价、涨幅超限等）
    DISTRIBUTION_DRIFT = "distribution_drift"  # 分布漂移（量级/波动率突变）
    SCHEMA_NULL_SWEEP = "schema_null_sweep"  # 某列整段变 NULL（接口字段失效）
    UNIT_SHIFT = "unit_shift"  # 单位跳变（元 → 万元）

    # ---- 合法业务事件（应被抑制）----
    BENIGN_SUSPENSION = "benign_suspension"  # 停牌：合法缺失，非故障
    BENIGN_CORPORATE_ACTION = "benign_corporate_action"  # 除权除息：合法价格跳变
    BENIGN_NEW_LISTING = "benign_new_listing"  # 新股：历史长度天然不足
    BENIGN_NON_TRADING_DAY = "benign_non_trading_day"  # 非交易日：本就不该有数据

    UNKNOWN = "unknown"  # 归因失败，兜底为最保守级别

    @property
    def is_benign(self) -> bool:
        return self.name.startswith("BENIGN_")


# 允许归因器输出的根因。UNKNOWN 不在其中：
# 说"不知道"没有信息量，那种情况应当降级给规则归因器，而不是当作结论。
DIAGNOSABLE_CAUSES: tuple[str, ...] = tuple(
    c.value for c in RootCause if c is not RootCause.UNKNOWN
)


@dataclass(frozen=True)
class Finding:
    """探针观测到的一个可疑信号。只描述现象，不做判断。"""

    probe: str  # 探针名，如 freshness / completeness
    table: str
    symbol: str
    window: str  # 受影响范围描述，如 "2024-03-01..2024-03-05"
    metric: str  # 观测指标名，如 stale_trading_days
    value: float  # 观测值
    threshold: float  # 触发阈值
    severity_hint: Severity  # 探针建议级别，未经归因
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """稳定标识，用于 Finding 与 Diagnosis、标注之间的对齐。"""
        return f"{self.probe}|{self.table}|{self.symbol}|{self.window}"


@dataclass(frozen=True)
class Diagnosis:
    """归因结论：根因、最终级别、置信度与自然语言解释。"""

    finding_key: str
    root_cause: RootCause
    severity: Severity
    confidence: float  # 0~1
    explanation: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def suppressed(self) -> bool:
        """该信号被判定为合法事件、告警已抑制。"""
        return self.root_cause.is_benign or self.severity is Severity.OK
