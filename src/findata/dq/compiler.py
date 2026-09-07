"""确定性 SQL 编译器: 把指标字典里的 sql_template 编译成 (sql, params)。

「确定性」意思是: 给定 metric 定义 + 上下文, 输出 100% 可重现的 (sql, params),
不需要 LLM。这与"用户问自然语言 → LLM 翻译 → SQL"是相反的路径 —— 后者灵活
但每跑一次结果可能不一样, 前者严格但所有变化都可以 git diff。

模板语法:
  sql_template: "SELECT count(*) FROM stock_daily WHERE date = $asof"
  ctx:          {"asof": date(2024, 1, 2)}
  编译输出:
  sql:          "SELECT count(*) FROM stock_daily WHERE date = ?"
  params:       [date(2024, 1, 2)]

只支持 `$<identifier>` 占位(单字符前缀 + 字母数字下划线),不引入 jinja
(项目里没有 jinja 依赖,且 90% 模板只有 1-2 个占位,简单语法足够)。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# 匹配 $name 形式, name 必是字母开头, 后接字母/数字/下划线
_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class SqlTemplate:
    """编译后的可执行 SQL + 命名参数列表。

    `names` 是去重后按首次出现顺序排列的唯一参数名;
    `params_repeat` 是每个 name 的出现次数 (>= 1)。
    同一占位出现 N 次的 SQL 模板, bind 时要传 N 次同名值给 duckdb `?` 占位。
    """

    __slots__ = ("sql", "names", "params_repeat")

    def __init__(self, sql: str, names: list[str], params_repeat: list[int]):
        self.sql = sql
        self.names = names
        self.params_repeat = params_repeat

    def bind(self, ctx: dict[str, Any]) -> tuple[str, list[Any]]:
        """把命名占位替换为 ? 并按 names 顺序取 ctx 值, 返回 (sql, params)。
        同一 name 出现 N 次, params 列表里就有 N 个该值, 与 SQL 中 `?` 数量一致。"""
        params: list[Any] = []
        for name, repeat in zip(self.names, self.params_repeat, strict=True):
            if name not in ctx:
                raise KeyError(
                    f"SQL 模板占位 ${name} 没有在 ctx 里提供; "
                    f"ctx keys = {list(ctx)}"
                )
            params.extend([ctx[name]] * repeat)
        return self.sql, params

    def __repr__(self) -> str:
        return (
            f"SqlTemplate(sql={self.sql!r}, names={self.names!r}, "
            f"params_repeat={self.params_repeat!r})"
        )


def compile_sql_template(template: str) -> SqlTemplate:
    """把含 $name 占位的 SQL 模板编译成 SqlTemplate, 调用 bind(ctx) 取 (sql, params)。

    同一 $name 多次出现时, names 里只记一次 (首次), params_repeat 记出现次数。
    """
    seen: dict[str, int] = {}  # name -> 出现次数
    order: list[str] = []  # 首次出现顺序

    def _repl(m: re.Match[str]) -> str:
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 1:
            order.append(name)
        return "?"

    sql = _PARAM_RE.sub(_repl, template)
    params_repeat = [seen[n] for n in order]
    return SqlTemplate(sql=sql, names=order, params_repeat=params_repeat)


def compile_metric(
    metric: dict[str, Any], ctx: dict[str, Any] | None = None
) -> tuple[str, list[Any]] | SqlTemplate:
    """对一条 metric 字典做编译。

    返回:
        - (sql, params) 当 ctx 给了
        - SqlTemplate   当 ctx 没给(供 defer 到执行期 bind)
    """
    if metric.get("kind") != "sql":
        raise ValueError(f"metric {metric.get('name', '?')!r} 不是 kind=sql, 不能 compile")
    template = compile_sql_template(metric["sql_template"])
    if ctx is None:
        return template
    return template.bind(ctx)


# ── helpers ────────────────────────────────────────────────────────────


def default_ctx(asof: date | None = None, **extra: Any) -> dict[str, Any]:
    """生成 SQL metric 编译期的默认 ctx。
    现在只需要 $asof, 后续可加 $source / $symbol / $window 等。"""
    ctx: dict[str, Any] = {"asof": asof or date.today()}
    ctx.update(extra)
    return ctx
