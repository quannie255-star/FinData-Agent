"""语义指标层：指标字典 + 确定性 SQL 编译器。

这是 Findata 区别于裸 Text2SQL 的核心：
LLM 不直接写 SQL，而是从预定义指标字典中**选择指标 + 填参数**，
SQL 由编译器按模板**确定性**渲染。这样每个数字都能被审计、被溯源。
"""

from findata.semantic.catalog import MetricCatalog, MetricSpec
from findata.semantic.compiler import CompileError, compile_metric

__all__ = ["MetricCatalog", "MetricSpec", "CompileError", "compile_metric"]
