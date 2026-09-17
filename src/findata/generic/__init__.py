"""generic：通用数据可信包（v2.2.0）。

通用协议 + 领域包架构中的「零领域知识基线包」：给任意 CSV / Excel /
DuckDB / SQLite 表出带徽章的可信报告。徽章语义与金融域完全一致
（dq/badges 单一实现），但**永远给不出 ✓ 已核验**——那是领域包的能力。
"""

from findata.generic.engine import (
    GenericReport,
    render_html,
    render_markdown,
    run_generic_inspection,
)
from findata.generic.schema import ColumnSpec, TableSpec, from_yaml, infer

__all__ = [
    "ColumnSpec",
    "GenericReport",
    "TableSpec",
    "from_yaml",
    "infer",
    "render_html",
    "render_markdown",
    "run_generic_inspection",
]
