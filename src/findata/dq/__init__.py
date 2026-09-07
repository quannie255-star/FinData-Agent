"""数据质量核心：检测、归因、分级。

分两级是为了让「发现」和「判断」可以分别演进与评测：
- probes  只负责观测信号，输出 Finding（不做判断，不知道业务背景）
- triage  负责把 Finding 归因成 Diagnosis，给出最终分级

这样探针可以独立评测（召回率），归因器可以整体替换
（规则 baseline -> LLM）并用同一套评测集比较增量价值。

M2 起的指标字典 (YAML + 确定性 SQL 编译器) 也放在这里：
- findata.dq.registry: 加载 metrics.yaml
- findata.dq.compiler: 把 sql_template 编译成 (sql, params)
"""
from findata.dq.compiler import (
    SqlTemplate,
    compile_metric,
    compile_sql_template,
    default_ctx,
)
from findata.dq.registry import MetricRegistry

__all__ = [
    "MetricRegistry",
    "SqlTemplate",
    "compile_metric",
    "compile_sql_template",
    "default_ctx",
]
