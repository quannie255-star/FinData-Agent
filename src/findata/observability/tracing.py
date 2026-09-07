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

_INITIALIZED = False
_EXPORTER: InMemorySpanExporter | None = None
_PROVIDER: TracerProvider | None = None


def setup_tracer(
    service_name: str = "findata",
    service_version: str = "0.4.0",
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
    """测试专用：重置全局状态。生产代码不要调。

    pytest 跑在不同 test 顺序下，OTel 的全局 provider 会残留。用这个 hook
    让每个 test 从干净状态起跑。"""
    global _INITIALIZED, _EXPORTER, _PROVIDER
    _INITIALIZED = False
    _EXPORTER = None
    _PROVIDER = None
