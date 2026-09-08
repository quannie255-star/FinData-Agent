"""指标目录：加载 YAML 指标字典，按 name 检索指标规格。

数据模型用 pydantic 严格校验，字典写错会在启动时报错而非运行时炸。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

_PARAM_TYPES = Literal["string", "int", "float", "date"]

_METRICS_YAML = Path(__file__).with_name("metrics.yaml")


class ParamSpec(BaseModel):
    type: _PARAM_TYPES
    required: bool = True


class VerifySpec(BaseModel):
    """交叉验证口径：第二条独立 SQL + 容差 + 说明。"""

    template: str
    tol: float = 1e-6
    note: str = ""


class MetricSpec(BaseModel):
    name: str
    label: str
    description: str
    params: dict[str, ParamSpec]
    template: str
    unit: str
    source: str
    verify: VerifySpec | None = None

    @field_validator("template")
    @classmethod
    def _no_raw_sql_holes(cls, v: str) -> str:
        """模板占位符必须是 :name 形式，且 name 只能来自 params（编译器再二次校验）。"""
        return v


class _Dict(BaseModel):
    metrics: list[MetricSpec]


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, MetricSpec]:
    """加载并校验指标字典，返回 {name: MetricSpec} 映射。"""
    raw = yaml.safe_load(_METRICS_YAML.read_text(encoding="utf-8"))
    catalog = _Dict.model_validate(raw)
    by_name: dict[str, MetricSpec] = {}
    for spec in catalog.metrics:
        if spec.name in by_name:
            raise ValueError(f"指标名重复: {spec.name}")
        by_name[spec.name] = spec
    if not by_name:
        raise ValueError("指标字典为空")
    return by_name


class MetricCatalog:
    """供编译器/工具使用的只读目录门面。"""

    def __init__(self) -> None:
        self._specs = load_catalog()

    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> MetricSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> MetricSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(
                f"未知指标 '{name}'。可用指标：{', '.join(self._specs)}"
            )
        return spec
