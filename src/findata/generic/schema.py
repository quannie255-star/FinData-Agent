"""通用数据表的声明式 schema 描述。

通用包的输入契约：一张 DataFrame + 一份 TableSpec（哪些列、什么类型、
主键是什么、新鲜度看哪列、可容忍多少空值）。声明式 schema 不是形式主义——
「这列不许有空」「这组列唯一」本身就是一种用户声明的**期望模型**，
通用检查的全部能力边界都来自这份期望；没声明的部分（比如"每个交易日
该有哪些行"）通用包就看不见，这正是它与领域包的含金量差距。

没有 schema 时可 infer() 从数据推断（dtype → kind），推断的 schema
没有主键、没有新鲜度锚点，唯一性/新鲜度检查会显式降级并在报告里注明
——降级必须可见，不允许静默跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml

ColumnKind = Literal["numeric", "categorical", "datetime", "text", "id"]

_KINDS: tuple[str, ...] = ("numeric", "categorical", "datetime", "text", "id")


@dataclass(frozen=True)
class ColumnSpec:
    """一列的声明。null_tolerance 是空值率的容忍上限（超过即⚠️疑点）。"""

    name: str
    kind: ColumnKind
    nullable: bool = True
    null_tolerance: float = 0.05
    description: str = ""


@dataclass(frozen=True)
class TableSpec:
    """一张表的声明。primary_key 为空 = 未声明主键（唯一性检查降级可见）。"""

    table: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...] = ()
    timestamp_column: str | None = None  # 新鲜度锚点列
    freshness_days: float = 30.0  # 数据最新一行允许落后观察日的自然日数
    outlier_tolerance: float = 0.05  # 列内 IQR 离群率容忍上限
    group_column: str | None = None  # 离群值在同组内计算（如按 symbol/城市 分组）
    asof: Any | None = None  # 观察时点（ISO 日期）；缺省 = 运行日

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> ColumnSpec | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


def from_yaml(path: str | Path) -> TableSpec:
    """从 YAML 装载声明式 schema。结构错误直接抛 ValueError（带字段定位）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "table" not in raw or "columns" not in raw:
        raise ValueError("schema YAML 必须包含 table 与 columns 字段")
    cols: list[ColumnSpec] = []
    for name, c in raw["columns"].items():
        c = c or {}
        kind = c.get("kind", _infer_kind_from_dtype_hint(c.get("dtype", "")))
        if kind not in _KINDS:
            raise ValueError(f"列 {name} 的 kind={kind!r} 非法，可选：{_KINDS}")
        cols.append(
            ColumnSpec(
                name=str(name),
                kind=kind,
                nullable=bool(c.get("nullable", True)),
                null_tolerance=float(c.get("null_tolerance", 0.05)),
                description=str(c.get("description", "")),
            )
        )
    pk = raw.get("primary_key") or []
    pk = tuple(pk) if isinstance(pk, list) else (str(pk),)
    declared = {c.name for c in cols}
    for key_col in pk:
        if key_col not in declared:
            raise ValueError(f"primary_key 列 {key_col!r} 未在 columns 中声明")
    ts = raw.get("timestamp_column")
    if ts is not None and ts not in declared:
        raise ValueError(f"timestamp_column {ts!r} 未在 columns 中声明")
    return TableSpec(
        table=str(raw["table"]),
        columns=tuple(cols),
        primary_key=pk,
        timestamp_column=ts,
        freshness_days=float(raw.get("freshness_days", 30.0)),
        outlier_tolerance=float(raw.get("outlier_tolerance", 0.05)),
        group_column=raw.get("group_column"),
        asof=raw.get("asof"),
    )


def infer(table: str, df: pd.DataFrame) -> TableSpec:
    """无声明时的降级路径：dtype 推断 kind。无主键、无时间锚点——
    唯一性/新鲜度检查会显式降级。推断结果写进报告，供用户补声明。"""
    cols = [
        ColumnSpec(
            name=str(c),
            kind=_infer_kind_from_series(df[c]),
            nullable=bool(df[c].isna().any()),
        )
        for c in df.columns
    ]
    # 推断时间锚点：唯一的 datetime 列才敢用（多列时不敢猜）
    dt_cols = [c.name for c in cols if c.kind == "datetime"]
    ts = dt_cols[0] if len(dt_cols) == 1 else None
    return TableSpec(table=table, columns=tuple(cols), timestamp_column=ts)


def _infer_kind_from_series(s: pd.Series) -> ColumnKind:
    if pd.api.types.is_numeric_dtype(s):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    # object 列抽样探测：能转数值算 numeric，能转日期算 datetime
    sample = s.dropna().head(50)
    if sample.empty:
        return "text"
    parsed = pd.to_datetime(sample, errors="coerce")
    if parsed.notna().mean() > 0.9 and sample.astype(str).str.len().min() >= 8:
        return "datetime"
    converted = pd.to_numeric(sample, errors="coerce")
    if converted.notna().mean() > 0.9:
        return "numeric"
    if sample.nunique() <= max(20, len(sample) // 10):
        return "categorical"
    return "text"


def _infer_kind_from_dtype_hint(hint: str) -> ColumnKind:
    hint = (hint or "").lower()
    if hint in _KINDS:
        return hint  # type: ignore[return-value]
    if hint in ("int", "float", "number", "double"):
        return "numeric"
    if hint in ("date", "datetime", "timestamp"):
        return "datetime"
    return "text"
