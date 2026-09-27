"""每日管线的两条「高可用」契约：步骤不会挂死、落盘不会留半截文件。

为什么值得单独写测试：这条管线是无人值守的（Windows 计划任务 / cron，18:30），
没有人在旁边看。它有两种坏法不会自己出声：

1. **挂死**——子进程卡在网络请求上，进程还在、日志不写、退出码永远不来，
   第二天早上你只看到「昨天没跑」。所以每一步都要有硬超时，且超时后必须
   真的把子进程掐掉（不是标记一下继续等）。
2. **半截文件**——报告按日期命名，写一半被中断后，第二天重跑之前没人会发现，
   而那期间推送/页面读到的是残缺内容。所以落盘必须原子。

导入方式与 tests/test_corpus_leak.py 一致（scripts/ 不是包，用 spec_from_file_location
直接加载文件），避免依赖 sys.path 的隐式行为。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_pipeline.py"


def _load() -> ModuleType:
    """按文件路径加载被测模块（模块级只做路径拼接，不会真的跑管线）。"""
    spec = importlib.util.spec_from_file_location("_daily_pipeline_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """日志目录改到 tmp_path —— 测试绝不能往真 logs/daily/ 里写东西。"""
    mod = _load()
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path)
    return mod


def test_hung_step_is_killed_and_returns_124(pipeline: ModuleType, tmp_path: Path) -> None:
    """卡住的子进程必须被掐断、返回 124，且超时事实要落进日志。"""
    t0 = time.perf_counter()
    code, tail = pipeline._run_step(
        [sys.executable, "-c", "import time; time.sleep(60)"], "hung", timeout=2
    )
    elapsed = time.perf_counter() - t0

    assert code == pipeline.TIMEOUT_EXIT == 124
    # 关键：证明是「被掐断」而不是「等它跑完」——60s 的睡眠里只允许花掉个位数秒。
    assert elapsed < 30, f"没有真的终止子进程：耗时 {elapsed:.1f}s"
    assert "timeout" in tail  # 这条注记由我们写，子进程只会 sleep，不会自己印

    logs = list(tmp_path.glob("hung-*.log"))
    assert len(logs) == 1
    assert "timeout" in logs[0].read_text(encoding="utf-8")


def test_step_timeout_is_finite_and_bounded(pipeline: ModuleType) -> None:
    """默认超时必须是有限正整数，且没有大到等于「没设」。

    上限取 6 小时：这是无人值守场景下的常识边界——比这更长，等于放弃当天调度窗口。
    """
    assert isinstance(pipeline.STEP_TIMEOUT_SECONDS, int)
    assert 0 < pipeline.STEP_TIMEOUT_SECONDS <= 6 * 3600


def test_write_text_atomic_replaces_and_leaves_no_residue(
    pipeline: ModuleType, tmp_path: Path
) -> None:
    """原子写：目标文件内容完整，临时文件不残留，父目录按需创建。"""
    target = tmp_path / "nested" / "2026-09-24.md"
    pipeline._write_text_atomic(target, "第一版\n")

    assert target.read_text(encoding="utf-8") == "第一版\n"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

    # 覆盖路径同样干净（旧内容不留尾巴）
    pipeline._write_text_atomic(target, "第二版\n")
    assert target.read_text(encoding="utf-8") == "第二版\n"
    assert list(target.parent.glob(".*tmp*")) == []


def test_successful_step_still_returns_real_exit_code(pipeline: ModuleType, tmp_path: Path) -> None:
    """加了超时之后，正常步骤的退出码仍是子进程真实退出码（没被兜底成 0）。"""
    code, _ = pipeline._run_step(
        [sys.executable, "-c", "import sys; sys.exit(3)"], "ok", timeout=30
    )
    assert code == 3
    assert list(tmp_path.glob("ok-*.log"))
