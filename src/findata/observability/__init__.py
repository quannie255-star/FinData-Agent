"""findata 内部的可观测层：把 OpenTelemetry 装到 service 层。

设计原则：
- 默认 InMemorySpanExporter（不依赖任何后端），CI 与离线环境零成本启动
- setup_tracer() 幂等，重复调用不会注册多个 provider
- 业务代码只 import get_tracer() 与 get_in_memory_exporter()，
  OTel SDK 的具体细节（Resource / Sampler / BatchSpanProcessor）封在这里
- /v1/traces 端点直接读 InMemorySpanExporter，不在业务代码里散 span

为什么不做"OTLP 推送到 collector"那一层：
- 部署时还早，先把"看得到 span"立起来
- 后续要接 Tempo / Jaeger 时，只换 provider 注册逻辑，业务代码 0 改
"""
from findata.observability.tracing import (
    get_in_memory_exporter,
    get_tracer,
    setup_tracer,
)

__all__ = ["setup_tracer", "get_tracer", "get_in_memory_exporter"]
