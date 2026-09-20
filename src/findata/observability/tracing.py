"""OTel tracer 引导：把全局 TracerProvider / InMemorySpanExporter 装一次。

OTel 的全局状态（TracerProvider、SpanProcessors）是单例，但 SDK 没有内置
"已经装好了" 的标记，所以这里手动维护一个 _initialized 标志做幂等。
这样多次 import + setup 不会触发 OTel 的 "Overriding of current TracerProvider
is not allowed" 警告。
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from findata import __version__ as _FINDATA_VERSION

_INITIALIZED = False
_EXPORTER: InMemorySpanExporter | None = None
_PROVIDER: TracerProvider | None = None


def setup_tracer(
    service_name: str = "findata",
    service_version: str = _FINDATA_VERSION,
    *,
    in_memory: bool = True,
    simple_processor: bool = True,
) -> TracerProvider:
    """初始化全局 TracerProvider。

    参数:
        service_name: 写入 OTel resource attribute service.name
        service_version: 写入 OTel resource attribute service.version
        in_memory: True（默认）= 同时挂 InMemorySpanExporter 让 /v1/traces 能读
        simple_processor: True = SimpleSpanProcessor（span 立刻 flush，方便测试 /
            演示站立刻看见；False = BatchSpanProcessor 用于高 QPS 生产场景）

    重复调用是 no-op，返回首次创建的 provider。
    """
    global _INITIALIZED, _EXPORTER, _PROVIDER
    if _INITIALIZED:
        return _PROVIDER  # type: ignore[return-value]

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    provider = TracerProvider(resource=resource)

    if in_memory:
        exporter = InMemorySpanExporter()
        processor: Any = (
            SimpleSpanProcessor(exporter) if simple_processor else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
        _EXPORTER = exporter

    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    _INITIALIZED = True
    return provider


def get_tracer(name: str = "findata.service") -> Any:
    """拿到业务用的 tracer。要求 setup_tracer() 已经调过。"""
    return trace.get_tracer(name)


def get_in_memory_exporter() -> InMemorySpanExporter | None:
    """拿 InMemorySpanExporter；未启用时返回 None（不抛错，方便上层判断）。"""
    return _EXPORTER


def reset_for_test() -> None:
    """测试专用：**清空已收集的 span**。生产代码不要调。

    ⚠️ 曾经这个函数名叫 reset，docstring 写"让每个 test 从干净状态起跑"，
    **但它做不到那件事**——这是一处实打实的「声明与代码不一致」，记在这里
    免得后人重新踩：

    - OTel 的 TracerProvider 是**进程级单例**，`trace.set_tracer_provider()`
      第二次调用会被 SDK 警告 `Overriding of current TracerProvider is not
      allowed` 并**忽略**。
    - 所以即使本函数把 `_INITIALIZED` 清掉、让 `setup_tracer()` 重新走一遍
      建 provider 的流程，真正的全局 provider **还是第一次那个**，span 仍旧
      导进**第一次**创建的 exporter。
    - 而 `get_in_memory_exporter()` 此时返回的是**新建的那个** exporter ——
      于是「span 落在哪个 exporter」和「测试读的是哪个 exporter」对不上，
      测试会**看起来在验证，其实什么都没验证**（当时的表现是一个
      IndexError，真凶是不一致，不是空列表那么简单）。

    现在这个函数只做它能做的那一件事：清空**当前** exporter 的缓冲，
    并且**刻意不去动** `_INITIALIZED` / `_PROVIDER` —— 保持 provider 单例
    不变，才能保证「span 的去向」与 `get_in_memory_exporter()` 始终是同一个。

    等价于 `get_in_memory_exporter().clear()`；留一个命名入口只是为了表达
    「这是测试用的重置动作」。回归测试见
    `tests/test_observability.py::test_spans_land_in_the_exporter_that_is_read_back`。
    """
    if _EXPORTER is not None:
        _EXPORTER.clear()
