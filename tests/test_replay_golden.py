"""真实回放 golden（R1.2 / 复盘 P0c）的 pytest 入口。

与 scripts/eval_golden.py 的回放阶段同一实现（findata/eval/replay.py），
CI 在 eval-gate job 里跑后者，本地开发跑这里——两处共用一把尺子。
fixture（eval/fixtures/replay_20260915/）已入库，测试离线可跑。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from findata.eval import replay


def test_replay_fixture_exists():
    assert replay.FIXTURE_DIR.exists(), (
        f"回放 fixture 缺失：{replay.FIXTURE_DIR}（应随仓库入库，"
        "重建见 scripts/build_replay_fixture.py）"
    )
    for t in replay._TABLES:
        assert (replay.FIXTURE_DIR / f"{t}.parquet").exists()


def test_replay_snapshot_has_knowledge_layer():
    """知识层必须由种子确定性重建：4 笔停牌，一条不少。"""
    snap = replay.load_replay_snapshot()
    assert len(snap.corporate_event) == 4
    assert (snap.corporate_event["kind"] == "suspension").all()


def test_replay_golden_all_cases_pass():
    """2026-09-15 的 7 条真实告警（9 信号）按复盘结论逐条固化重放。"""
    _snap, result, cases = replay.run_replay()
    bad = [c for c in cases if not c.ok]
    for c in bad:
        print(f"✗ {c.name}: {c.detail}")
    assert not bad, f"真实回放 {len(bad)}/{len(cases)} 条失败（巡检行为漂移）"
    assert len(cases) >= 9
    # 复盘的核心命题：7 条真实告警全部不再裸奔（实际 8/9 信号被抑制）
    assert result.summary.n_suppressed >= 4
    assert result.summary.n_alerts <= 3


def test_replay_no_future_leakage():
    """as_of 截断生效：回放日之后的种子事件不得参与抑制。"""
    snap = replay.load_replay_snapshot().as_of(date(2025, 7, 1))
    # 中芯国际（2025-09）与神华（2025-08）的停牌种子必须被截掉
    dates = set(pd.to_datetime(snap.corporate_event["date"]).dt.date)
    assert date(2025, 9, 1) not in dates
    assert date(2025, 8, 4) not in dates
    assert date(2022, 1, 19) in dates


def test_strict_runner_exit_code():
    """门禁入口在 fixture 齐备时必须返回 0（失败即 CI 挂红）。"""
    assert replay.run_strict() == 0
