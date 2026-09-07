"""指标字典加载器。

MetricRegistry 是「指标字典 YAML」的运行时入口。YAML 改动不需要发版,
只要 reload registry 即可。这把"加新探针"从改 py 代码 → 改配置文件。

设计选择:
- 加载用 PyYAML(项目里还没用过,但 yaml 是 stdlib 外的轻量依赖,质量监控领域必用)
- 不做 hot-reload: 业务进程启动时加载一次,改 YAML 重启服务
- 校验: 必填字段缺失、kind 不识别、probe_fn 找不到 → 启动期 fail-fast
- list() 返回 list[dict] 而非 list[dataclass]: MCP tool 直接 json 序列化

注意 kind=probe 的 metric 仍然要 ProbeContext 跑(读 stock_daily DataFrame),
kind=sql 的 metric 给 SQL 字符串 + 命名参数 $asof, 由 compiler.compile 落地为
可执行 (sql, params)。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import yaml


class MetricRegistry:
    """从 YAML 加载 metric 字典,并提供运行时查询接口。"""

    def __init__(self, metrics: list[dict[str, Any]]):
        self._metrics: list[dict[str, Any]] = []
        for m in metrics:
            normalized = self._validate(m)
            self._metrics.append(normalized)

    @classmethod
    def from_yaml(cls, path: str | Path) -> MetricRegistry:
        p = Path(path)
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "metrics" not in data:
            raise ValueError(f"metrics yaml 顶层必须含 'metrics' 列表: {p}")
        return cls(data["metrics"])

    # ─── 查询 ───

    def list(self) -> list[dict[str, Any]]:
        """返回所有 metric 元数据(纯 dict,便于 JSON 序列化)。"""
        return list(self._metrics)

    def names(self) -> list[str]:
        return [m["name"] for m in self._metrics]

    def get(self, name: str) -> dict[str, Any]:
        for m in self._metrics:
            if m["name"] == name:
                return m
        raise KeyError(f"unknown metric: {name!r}; known = {self.names()}")

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self._metrics if m["kind"] == kind]

    # ─── 反射: 把 kind=probe 的 probe_fn 拉成真函数 ───

    def resolve_probe_fn(self, name: str) -> Any:
        """返回 metric 声明的 py 函数(供 findata.dq.probes 流水线调用)。"""
        m = self.get(name)
        if m["kind"] != "probe":
            raise ValueError(f"metric {name!r} is kind={m['kind']!r}, not probe")
        module = import_module(m["probe_module"])
        return getattr(module, m["probe_fn"])

    # ─── 校验 ───

    REQUIRED_TOP = ("name", "kind", "description", "default_severity")
    VALID_KIND = ("probe", "sql")
    VALID_SEVERITY = ("P0", "P1", "P2")

    def _validate(self, m: dict[str, Any]) -> dict[str, Any]:
        for k in self.REQUIRED_TOP:
            if k not in m:
                raise ValueError(f"metric 缺少必填字段 {k!r}: {m.get('name', m)}")
        if m["kind"] not in self.VALID_KIND:
            raise ValueError(
                f"metric {m['name']!r} kind={m['kind']!r}, 必须是 {self.VALID_KIND}"
            )
        if m["default_severity"] not in self.VALID_SEVERITY:
            raise ValueError(
                f"metric {m['name']!r} default_severity={m['default_severity']!r}, "
                f"必须是 {self.VALID_SEVERITY}"
            )
        if m["kind"] == "probe":
            if "probe_module" not in m or "probe_fn" not in m:
                raise ValueError(
                    f"kind=probe 的 metric {m['name']!r} 需声明 probe_module + probe_fn"
                )
            # 反射前不强制 import(避免启动期缺依赖硬崩)
        if m["kind"] == "sql":
            if "sql_template" not in m:
                raise ValueError(f"kind=sql 的 metric {m['name']!r} 需声明 sql_template")
        return m
