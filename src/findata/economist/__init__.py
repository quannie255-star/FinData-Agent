"""`findata-context-economist`：从真实 agent trace 推出可执行的上下文/工具策略改动。

ROADMAP 交付物的五步流水线：

    真实 trace（OTel gen_ai.* span，可选 runs 增强层）
      ↓ 观测   trace.py       每次工具调用返回多大；工具级/段级读数
      ↓ 判定   analyze.py     哪些段被真正引用（分档，且必须先通过重建对账）
      ↓ 提议   analyze.py     裁撤定义 / 分层加载 / 结果复用 —— 每条可逆且带代价
      ↓ 验证   analyze.py     同一批任务的改前/改后两臂配对对照
      ↓ 报告   report.py      实测 / 换算 / 未测得 分开写，不合成单一百分比

对外只有两个函数：`analyze()` 与 `render()`；命令行见 `cli.py`。
"""

from findata.economist.analyze import Analysis, Proposal, Verification, analyze, verify
from findata.economist.report import render, render_json
from findata.economist.trace import TraceBundle, load_bundle

__all__ = [
    "Analysis",
    "Proposal",
    "TraceBundle",
    "Verification",
    "analyze",
    "load_bundle",
    "render",
    "render_json",
    "verify",
]
