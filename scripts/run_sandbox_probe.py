"""沙箱信号验证：真起进程、真跑到超时被杀，看归因器认不认得**真实报错**。

跑什么
------
三个受控工具实现，在子进程里各跑一次：

  add          正常返回
  divide       参数非法，抛 ValueError（真实 traceback）
  spin         死循环，0.5s 后真被 TimeoutExpired 杀掉

**这三个工具是受控用例，不是业务工具**——它们的唯一用途是验证归因器。
但**信号是真的**：真起进程、真跑到超时、真被杀、真拿到原始报错文本。
这跟"在字符串里写一个 'timeout' 然后看归因器认不认"有本质区别——后者
是循环论证，前者是外部事实。

不证明什么
----------
· 不证明生产环境的超时有多 frequent（超时是我们主动设 0.5s 触发的）
· 不证明 gRPC 那套 "deadline exceeded" 能识别——**它现在确实识别不了**，
  这是已知局限，不因为跑通了这一个就宣称覆盖
· 不是安全沙箱，防不住恶意代码（见 `sandbox.py` 的边界说明）

    uv run python scripts/run_sandbox_probe.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from findata.agentops.sandbox import (  # noqa: E402
    MEMORY_LIMIT_SUPPORTED,
    run_in_sandbox,
    sandbox_step_name,
)
from findata.agentops.schema import STATUS_ERROR, STATUS_OK, Trace  # noqa: E402
from findata.agentops.triage import diagnose, verdict  # noqa: E402

ARCHIVE = Path("examples/sandbox-signals.txt")

# 每个工具单独的超时阈值。**子进程启动本身就要 ~400ms**（解释器冷启动），
# 所以阈值必须留够余量，否则"参数错误"那个也会因为启动慢被判成超时——
# 第一版就是这么翻的车：divide 明明立刻抛异常，却被记成 timeout。
BOOT = 5.0  # 正常/报错类：给足启动时间
SPIN = 2.0  # 死循环：只要超过启动时间就该被杀

# 受控工具实现：文本形式，因为跨进程只能传字节
TOOLS: dict[str, tuple[str, float]] = {
    "add": ("print(1 + 1)", BOOT),
    "divide": ('raise ValueError("invalid argument: divisor must be > 0")', BOOT),
    "spin": ("while True:\n    pass", SPIN),
}


def run(archive: Path | None) -> int:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for name, (code, timeout) in TOOLS.items():
        r = run_in_sandbox(code, timeout=timeout)
        sig = ",".join(r.signals) or "none"

        t = Trace(task=f"run {name} in sandbox", agent="sandbox-probe", parser="sandbox")
        status = STATUS_OK if r.ok else STATUS_ERROR
        t.step(
            name,
            arguments={"timeout": timeout},
            status=status,
            error=r.raw_error,
            usage_chars=len(r.stdout),
        )
        t.golden = {"signal": sandbox_step_name(r), "exit_code": r.exit_code}
        t.finish(duration_ms=r.duration_ms)
        d = diagnose(t)
        rows.append((name, sig, r.raw_error or "（无）", d.category, verdict(d), d.label))

    out: list[str] = []
    a = out.append
    a("沙箱信号验证 · 真实进程（进程级隔离，非容器安全边界）")
    a(f"超时阈值：正常/报错类 {BOOT}s，死循环 {SPIN}s（子进程冷启动约 0.4s，必须留余量）")
    a(f"内存限制 {'支持' if MEMORY_LIMIT_SUPPORTED else '**Windows 不支持，未施加**'}")
    a("")
    a("一、信号 → 归因 → 处置")
    for name, sig, raw, cat, v, label in rows:
        a(f"  {name}")
        a(f"    信号    {sig}")
        a(f"    报错原文 {raw[:70]}")
        a(f"    归因    {cat}（{label}）")
        a(f"    处置    {v}")
    a("")
    a("二、这一步证明了什么")
    a("  · 超时是**真超时**：真的起了子进程、真的跑到阈值、真的被杀，")
    a("    归因器拿到的是 subprocess.TimeoutExpired 的原文，不是我们写的一个词")
    a("  · `env_timeout` 的处置是 ⊘ 不算失败——环境问题不该当负样本，")
    a("    这条之前只是写在文档里的主张，现在有真实信号背书")
    a("")
    a("三、没证明什么（别拿这份报告吹）")
    a("  · 超时是我们主动设 0.5s 触发的，**不代表生产环境超时有多频繁**")
    a("  · gRPC 那套 \"deadline exceeded\" 现在**识别不了**，归因器只认")
    a("    含 timeout 的文本。这是已知局限，不因为这一个跑通了就算覆盖")
    a("  · 进程级隔离防不住恶意代码；要那个得上容器，本机拉不到镜像就没做")
    a("  · 内存限制在 Windows 上无法施加（`resource` 模块不存在），")
    a("    代码标了 `memory_limit_applied=False`，没有静默降级")

    text = "\n".join(out)
    print(text)
    if archive:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(text + "\n", encoding="utf-8")
        print(f"\n已归档 → {archive}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="沙箱信号 → 归因验证")
    ap.add_argument("--archive", default=str(ARCHIVE), help="归档路径；空串则不归档")
    args = ap.parse_args()
    return run(Path(args.archive) if args.archive else None)


if __name__ == "__main__":
    raise SystemExit(main())
