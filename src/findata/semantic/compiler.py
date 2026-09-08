"""确定性 SQL 编译器：把「指标 + 参数」渲染成可执行的参数化 SQL。

核心安全/可信保证：
1. LLM 只产出结构化的 metric + params，绝不直接拼 SQL 字符串；
2. 渲染出的 SQL 使用 DuckDB 的参数绑定（?），值永远不内联进 SQL 文本，
   从根上杜绝 SQL 注入；
3. 参数逐个对照字典校验类型与必填，未知参数直接拒绝，防越权列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from findata.semantic.catalog import MetricCatalog, MetricSpec

# 模板中允许出现的占位符形式：:name 或 :name 后跟非标识符字符
_PLACEHOLDER = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

_DATE_STR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CompileError(ValueError):
    """语义层编译错误：指标不存在、参数缺失/越权、类型不符等。"""


@dataclass(frozen=True)
class CompiledQuery:
    """编译产物：参数化 SQL + 有序绑定值 + 指标元数据。"""

    sql: str
    params: tuple
    metric: MetricSpec


def _coerce(spec_type: str, key: str, raw) -> object:
    """把用户/LLM 传入的原始值按字典声明的类型强转，失败即报错。"""
    if spec_type == "string":
        if not isinstance(raw, str):
            raise CompileError(f"参数 '{key}' 应为字符串，实际 {type(raw).__name__}")
        return raw
    if spec_type == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise CompileError(f"参数 '{key}' 应为整数，实际 {type(raw).__name__}")
        return raw
    if spec_type == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise CompileError(f"参数 '{key}' 应为数值，实际 {type(raw).__name__}")
        return float(raw)
    if spec_type == "date":
        if isinstance(raw, date):
            return raw.isoformat()
        if isinstance(raw, str) and _DATE_STR.match(raw):
            return raw
        raise CompileError(f"参数 '{key}' 应为 YYYY-MM-DD 日期，实际 {raw!r}")
    raise CompileError(f"未知参数类型 '{spec_type}'")  # 字典写错，属代码缺陷


def _render_template(
    spec: MetricSpec,
    template: str,
    coerced: dict[str, object],
) -> CompiledQuery:
    """把已校验、已强转的参数渲染进模板，返回参数化 SQL。"""
    placeholders = _PLACEHOLDER.findall(template)
    missing = set(placeholders) - set(coerced)
    if missing:
        raise CompileError(
            f"指标 '{spec.name}' 模板引用了未绑定的占位符 {sorted(missing)}"
        )

    bindings: list[object] = []

    def _replace(m: re.Match) -> str:
        bindings.append(coerced[m.group(1)])
        return "?"

    sql = _PLACEHOLDER.sub(_replace, template)
    return CompiledQuery(sql=sql, params=tuple(bindings), metric=spec)


def compile_metric(
    catalog: MetricCatalog,
    name: str,
    params: dict | None = None,
) -> CompiledQuery:
    """把指标名 + 参数编译成参数化 SQL。

    抛 CompileError 的情形：指标不存在、缺必填参数、传入未声明参数、类型不符。
    """
    params = params or {}
    try:
        spec = catalog.require(name)
    except KeyError as exc:
        raise CompileError(str(exc)) from exc

    coerced = _validate_and_coerce(spec, params)
    return _render_template(spec, spec.template, coerced)


def compile_verify(
    catalog: MetricCatalog,
    name: str,
    params: dict | None = None,
) -> CompiledQuery | None:
    """编译指标的验证口径（若字典里定义了 verify）。

    无 verify 定义时返回 None；否则复用主路径的参数校验与类型强转，
    保证验证 SQL 与主路径用同一套参数。
    """
    params = params or {}
    try:
        spec = catalog.require(name)
    except KeyError as exc:
        raise CompileError(str(exc)) from exc

    if spec.verify is None:
        return None

    coerced = _validate_and_coerce(spec, params)
    return _render_template(spec, spec.verify.template, coerced)


def _validate_and_coerce(
    spec: MetricSpec,
    params: dict,
) -> dict[str, object]:
    """校验参数（未知/缺失/类型）并返回强转后的值映射。"""
    declared = set(spec.params)
    supplied = set(params)
    unknown = supplied - declared
    if unknown:
        raise CompileError(
            f"指标 '{spec.name}' 不接受参数 {sorted(unknown)}；可选参数：{sorted(declared)}"
        )

    missing = {k for k, p in spec.params.items() if p.required and k not in supplied}
    if missing:
        raise CompileError(f"指标 '{spec.name}' 缺少必填参数 {sorted(missing)}")

    coerced: dict[str, object] = {}
    for key, val in params.items():
        coerced[key] = _coerce(spec.params[key].type, key, val)
    return coerced
