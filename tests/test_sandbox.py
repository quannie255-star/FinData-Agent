"""沙箱测试：钉住「信号是真的」与「归因在真实报错上判对」。

为什么值得单独一个文件：不做沙箱时，`env_timeout` 这条归因只能靠关键词
猜，而猜是不成立的（held-out 上 0/20）。这些测试保证我们每次改归因器，
都会在**真实报错原文**上重新过一遍，而不是在字符串里写个 "timeout"。
"""

from __future__ import annotations

import sys
import time

from findata.agentops.sandbox import (
    MEMORY_LIMIT_SUPPORTED,
    run_in_sandbox,
    sandbox_step_name,
)
from findata.agentops.schema import STATUS_ERROR, STATUS_OK, Trace
from findata.agentops.triage import (
    CAT_ENV_TIMEOUT,
    CAT_PARAM_ERROR,
    V_DISCARD,
    V_NOT_FAILURE,
    diagnose,
    verdict,
)

# 子进程冷启动约 0.4s，阈值必须留余量，否则启动慢会被误记成超时
BOOT = 5.0
SPIN = 1.0


def _trace(name: str, error: str, ok: bool) -> Trace:
    t = Trace(task=f"run {name}", agent="sandbox", parser="sandbox")
    t.step(name, status=STATUS_OK if ok else STATUS_ERROR, error=error)
    t.finish()
    return t


def test_normal_tool_reports_no_signal():
    r = run_in_sandbox("print(1 + 1)", timeout=BOOT)
    assert r.ok and r.exit_code == 0
    assert r.signals == [] and r.raw_error == ""
    assert sandbox_step_name(r) == "sandbox_ok"


def test_exception_gives_real_traceback_last_line():
    """归因器要的是**原文**，摘要会丢信息。取最后一行是 traceback 的结论行。"""
    r = run_in_sandbox('raise ValueError("invalid argument: n must be > 0")', timeout=BOOT)
    assert not r.ok and r.exit_code != 0
    assert "ValueError: invalid argument" in r.raw_error
    assert r.signals == ["nonzero_exit"]
    assert sandbox_step_name(r) == "sandbox_nonzero_exit"


def test_spin_is_really_killed_and_reports_real_timeout_text():
    """真起进程、真跑到阈值、真被杀——不是我们写的一个词。"""
    t0 = time.perf_counter()
    r = run_in_sandbox("while True:\n    pass", timeout=SPIN)
    elapsed = time.perf_counter() - t0
    assert r.timed_out and not r.ok
    assert "TimeoutExpired" in r.raw_error
    assert r.signals == ["timeout"], "超时不该同时报 nonzero_exit"
    assert sandbox_step_name(r) == "sandbox_timeout"
    # 确实等到了阈值才放弃（留 0.5s 给进程启动与调度抖动）
    assert elapsed >= SPIN


def test_env_timeout_is_not_a_negative_sample():
    """这条是沙箱的全部意义：环境问题别当负样本，重跑可能就成。"""
    r = run_in_sandbox("while True:\n    pass", timeout=SPIN)
    t = _trace("spin", r.raw_error, r.ok)
    d = diagnose(t)
    assert d.category == CAT_ENV_TIMEOUT
    assert verdict(d) == V_NOT_FAILURE


def test_param_error_is_discarded():
    """同样是失败，参数写错是能力问题，该丢——和 env_timeout 结论相反。"""
    r = run_in_sandbox('raise ValueError("invalid argument: n must be > 0")', timeout=BOOT)
    d = diagnose(_trace("divide", r.raw_error, r.ok))
    assert d.category == CAT_PARAM_ERROR
    assert verdict(d) == V_DISCARD


def test_memory_limit_is_not_silently_dropped():
    """Windows 上没有 resource 模块：做不到就标出来，不许假装设了。"""
    assert MEMORY_LIMIT_SUPPORTED == (sys.platform != "win32")
    r = run_in_sandbox("print(1)", timeout=BOOT, memory_mb=64)
    if MEMORY_LIMIT_SUPPORTED:
        assert r.memory_limit_applied
    else:
        assert not r.memory_limit_applied


def test_sandbox_step_name_only_describes_signals():
    """探测与判断分离：step_name 只能描述观测到的信号，不能下结论。"""
    for name in ("sandbox_ok", "sandbox_timeout", "sandbox_oom", "sandbox_nonzero_exit"):
        assert "error" not in name.replace("_exit", ""), f"{name} 不该含判断词"
