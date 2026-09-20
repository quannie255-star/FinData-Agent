"""可观测层测试：OTel 装好之后,业务调用必留下 span 痕迹。

OTel 的 TracerProvider 是真正的进程级单例——`set_tracer_provider` 第二次
调用会被 SDK 警告并忽略。所以这一组测试共享同一个 provider 与 exporter,
并按需 `clear()` exporter 历史。

`reset_for_test()` 现在**只清空 span 缓冲**、不再假装"换一个干净 provider"
（那个声明当年与实现不符，见 `tracing.reset_for_test` 的 docstring）。
它可以放心用来清场，与直接 `clear()` 等价；本文件仍显式调用 `clear()`，
是为了让「清的是什么」在测试里一眼可见。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from findata.observability import get_in_memory_exporter, get_tracer
from findata.observability.tracing import reset_for_test, setup_tracer


@pytest.fixture(autouse=True)
def _clean_exporter():
    """每个测试开跑前清空 in-memory exporter 收集到的 span。"""
    setup_tracer()  # 幂等
    exp = get_in_memory_exporter()
    if exp is not None:
        exp.clear()
    yield
    # 测完不重置,留给下一个测试


def test_setup_tracer_is_idempotent():
    """重复调 setup_tracer() 不会触发 OTel 的 'override' 警告或重置。"""
    import findata.observability.tracing as tracing_mod

    p1 = setup_tracer()
    p2 = setup_tracer()  # no-op
    assert p1 is p2
    # 跟踪 provider 仍是首次创建的同一个
    assert tracing_mod._PROVIDER is p1


def test_inspect_records_health_score_on_span():
    """service.inspect 应该把核心健康指标塞进 span attribute。"""
    import findata.service as svc  # noqa: E402

    result = svc.inspect("synthetic", seed=20240102)
    spans = get_in_memory_exporter().get_finished_spans()
    assert any(s.name == "service.inspect" for s in spans), [s.name for s in spans]
    span = next(s for s in spans if s.name == "service.inspect")
    # 关键 attribute 都要在,值与 InspectionResult 摘要一致
    assert span.attributes["findata.source"] == "synthetic"
    assert span.attributes["findata.health_score"] == result.summary.health_score
    assert span.attributes["findata.n_alerts"] == result.summary.n_alerts
    assert span.attributes["findata.n_findings"] == result.summary.n_findings
    assert span.attributes["findata.grade"] == result.summary.grade()


def test_evaluate_both_records_kappa_subspan():
    """triage=both 跑完会有 service.evaluate.both_triage 子 span,含 κ。"""
    import findata.service as svc  # noqa: E402

    result = svc.evaluate(seed=20240102, triage="both")
    spans = get_in_memory_exporter().get_finished_spans()
    sub = next((s for s in spans if s.name == "service.evaluate.both_triage"), None)
    assert sub is not None, [s.name for s in spans]
    # κ 已写进 attribute
    assert "findata.llm_kappa_root_cause" in sub.attributes
    assert 0.0 <= sub.attributes["findata.llm_kappa_root_cause"] <= 1.0
    # 业务结果应带 triage_agreement
    assert result.triage_agreement is not None


def test_spans_land_in_the_exporter_that_is_read_back():
    """钉住那条**曾经被违反过**的不变量：
    span 的落点必须等于 `get_in_memory_exporter()` 的返回值。

    历史事故：`reset_for_test()` 清掉模块缓存后，`setup_tracer()` 会重建一个
    provider + exporter，但 OTel 的全局 provider 是单例、第二次
    `set_tracer_provider` 被 SDK 忽略 —— 真 span 继续进**旧** exporter，
    而 `get_in_memory_exporter()` 返回**新** exporter。测试于是"看起来在验证，
    其实什么都没验证"。这条用例就是防止那个状态回来。
    """
    from opentelemetry import trace as otel_trace

    import findata.observability.tracing as tracing_mod

    reset_for_test()  # 只清缓冲，不得换 provider
    before = get_in_memory_exporter()
    setup_tracer()  # 幂等：应当是 no-op
    after = get_in_memory_exporter()

    assert before is after, "setup_tracer() 换掉了 exporter —— provider 单例被破坏了"
    assert otel_trace.get_tracer_provider() is tracing_mod._PROVIDER, (
        "全局 provider 与模块记录的 provider 不是同一个 —— "
        "span 会导进我们读不到的地方"
    )

    with get_tracer("findata.test").start_as_current_span("test.after_reset"):
        pass
    names = [s.name for s in after.get_finished_spans()]
    assert "test.after_reset" in names, (
        "刚记录的 span 没出现在 get_in_memory_exporter() 里 —— "
        "落点与读取点已经分叉，后续所有基于 span 的断言都不可信"
    )
    reset_for_test()
    assert after.get_finished_spans() == (), "reset_for_test() 应当清空缓冲"


def test_tracer_returns_otel_tracer():
    """get_tracer() 必须返回 OTel 的 Tracer(才能 start_as_current_span)。"""
    from opentelemetry.trace import Tracer

    tracer = get_tracer("findata.test")
    assert isinstance(tracer, Tracer)
    with tracer.start_as_current_span("test.span") as s:
        s.set_attribute("k", "v")
    spans = get_in_memory_exporter().get_finished_spans()
    assert any(s.name == "test.span" for s in spans)


def test_v1_traces_endpoint_returns_spans_after_inspect():
    """/v1/traces 能在跑过 inspect 之后吐出 span 列表。"""
    from findata.api.app import app  # noqa: E402

    client = TestClient(app)
    # 先跑一次业务调用
    r1 = client.post("/v1/inspect", json={"source": "synthetic", "seed": 20240102})
    assert r1.status_code == 200
    # 再拿 trace
    r2 = client.get("/v1/traces?limit=10")
    assert r2.status_code == 200
    body = r2.json()
    assert body["enabled"] is True
    assert body["n_returned"] >= 1
    names = [s["name"] for s in body["spans"]]
    assert "service.inspect" in names
    # duration_ms 必是非负数
    for s in body["spans"]:
        assert s["duration_ms"] >= 0


def test_v1_traces_does_not_crash_when_called_early():
    """OTel 已装(其他测试触发了)也能拿 span,不应崩溃。"""
    from findata.api.app import app  # noqa: E402

    client = TestClient(app)
    r = client.get("/v1/traces")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert "spans" in body
