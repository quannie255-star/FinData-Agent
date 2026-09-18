"""沙箱执行：把工具调用丢进**子进程**跑，采集真实的环境失败信号。

为什么必须真跑
--------------
不做沙箱，「环境下失败」只能靠猜关键词。而猜是不成立的：held-out 集上
`env_timeout` 判对 0/20——真实报错写的是 "deadline exceeded"、
"TimeoutExpired"、"context deadline"，跟关键词表里的 "timeout" 对不上。
**没见过真实信号，就没有资格说会归因环境失败。**

沙箱给的正是真实信号：真起一个进程、真让它跑到超时被杀、真拿到
`subprocess.TimeoutExpired` 的原文。归因器在这个原文上判对，才算数。

边界（必须说清，不能吹）
------------------------
- **这是进程级隔离，不是容器级安全边界。** 它防的是「一个工具把主进程
  拖死」，不防恶意代码。要防恶意代码得用 Docker/gVisor——那需要容器
  运行时，本机拉不到镜像，做不到就不假装做了。
- **内存限制只在 Unix 生效**（`resource.setrlimit`）。Windows 没有这个
  系统调用，代码会明确标 `memory_limit_applied=False`，不静默降级。
- **超时是受控注入的**（我们故意跑一个死循环、故意给 0.5s）。这是为了
  验证归因器认识真实报错文本；它不是"生产里自然发生的超时"，别拿它
  去证明"我们的系统经常超时"。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field

# Windows 没有 resource 模块，内存限制做不到就不做，但**要标出来**
MEMORY_LIMIT_SUPPORTED = sys.platform != "win32"

# 子进程里先跑这段：能设内存上限就设，设不了就继续（不让它变成崩溃）
_MEM_PRELUDE = """
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, ({mb} * 1024 * 1024, {mb} * 1024 * 1024))
    print("__MEM_LIMIT_APPLIED__", flush=True)
except Exception:
    print("__MEM_LIMIT_UNAVAILABLE__", flush=True)
"""


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    duration_ms: float = 0.0
    oom_killed: bool = False  # 只在能设内存上限时才有意义
    memory_limit_applied: bool = False
    # 归因器要的是**原文**。摘要会丢信息，原文才能复核。
    raw_error: str = ""
    signals: list[str] = field(default_factory=list)


def _decode(raw: bytes | str | None) -> str:
    """TimeoutExpired 给的 stdout/stderr 有时是 bytes 有时是 str，统一成 str。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def run_in_sandbox(
    code: str,
    *,
    timeout: float = 2.0,
    memory_mb: int | None = None,
    python: str | None = None,
) -> SandboxResult:
    """在子进程里跑一段 Python，返回**真实**的执行信号。

    `code` 是要执行的代码（工具实现被序列化进去）。不传函数对象是因为
    跨进程只能传字节——这也是为什么工具实现必须是可序列化的纯文本。
    """
    body = code
    if memory_mb and MEMORY_LIMIT_SUPPORTED:
        body = _MEM_PRELUDE.format(mb=memory_mb) + body

    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            [python or sys.executable, "-c", body],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        dur = (time.perf_counter() - t0) * 1000
        out, err, code_rc = p.stdout, p.stderr, p.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        dur = (time.perf_counter() - t0) * 1000
        out = _decode(e.stdout)
        err = _decode(e.stderr)
        code_rc = -1
        timed_out = True

    applied = "__MEM_LIMIT_APPLIED__" in out
    out = out.replace("__MEM_LIMIT_APPLIED__", "").replace("__MEM_LIMIT_UNAVAILABLE__", "")
    # OOM 在 Linux 上通常是 137（SIGKILL）；Windows 上做不到，就别猜
    oom = (not timed_out) and code_rc in (137, -9) and applied

    sig: list[str] = []
    if timed_out:
        sig.append("timeout")
    elif oom:
        sig.append("oom")
    elif code_rc != 0:
        sig.append("nonzero_exit")

    raw = ""
    if timed_out:
        raw = f"TimeoutExpired: command timed out after {timeout}s"
    elif err.strip():
        # 只取最后一行：Python traceback 头几行是文件路径，对归因没用
        raw = err.strip().splitlines()[-1]
    elif code_rc != 0:
        raw = f"exit code {code_rc}"

    return SandboxResult(
        ok=(code_rc == 0 and not timed_out),
        stdout=out.strip(),
        stderr=err.strip(),
        exit_code=code_rc,
        timed_out=timed_out,
        duration_ms=round(dur, 1),
        oom_killed=oom,
        memory_limit_applied=applied,
        raw_error=raw,
        signals=sig,
    )


def sandbox_step_name(result: SandboxResult) -> str:
    """给结果一个可归类的名字——**只描述观测到的信号，不做判断**。

    判断在 `triage.py`。这里要是顺手判了，探针与判断的边界就破了。
    """
    if result.timed_out:
        return "sandbox_timeout"
    if result.oom_killed:
        return "sandbox_oom"
    if result.exit_code != 0:
        return "sandbox_nonzero_exit"
    return "sandbox_ok"
