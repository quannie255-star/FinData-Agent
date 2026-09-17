"""RL 管线端到端测试（强制 mock 模式，不依赖 LLM key、不花钱）。"""

from __future__ import annotations

import json
import sys

import pytest

from findata.config import settings

_ARTIFACTS = (
    "report.json",
    "report.md",
    "tasks_attribution.jsonl",
    "tasks_ask.jsonl",
    "agl_events.jsonl",
    "trajectories.jsonl",
)


@pytest.fixture
def forced_mock(monkeypatch):
    """屏蔽本机 .env 的 key：CI 与本地的管线行为必须一致（mock 路径）。"""
    monkeypatch.setattr(settings, "llm_api_key", "")


def test_pipeline_end_to_end_mock(tmp_path, monkeypatch, forced_mock):
    import scripts.rl_pipeline as pipe

    monkeypatch.setattr(sys, "argv", ["rl_pipeline.py", "--seeds", "1", "--out", str(tmp_path)])
    rc = pipe.main()
    assert rc == 0
    for name in _ARTIFACTS:
        assert (tmp_path / name).exists(), f"缺少产物 {name}"

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["backend"] == "mock"
    assert report["gate"]["pass"]
    # 规则基线是有内容的对照锚点；mock 无策略，rollout reward 诚实为 0
    assert report["rule_baseline"]["reward_mean"] > 0.5
    assert report["aggregate"]["reward_mean"] == 0.0
    assert report["n_tasks"]["attribution"] > 0
    assert report["n_tasks"]["ask"] == 7  # golden 全量任务行照常导出
    assert report["ask_skipped"]

    text = (tmp_path / "tasks_attribution.jsonl").read_text(encoding="utf-8").strip()
    rows = [json.loads(x) for x in text.splitlines()]
    assert rows
    for row in rows:
        assert row["reward_spec"]["method"] == "rule"
        assert "expected_cause" in row["reward_spec"]["ground_truth"]
        assert row["prompt"], "任务行必须自包含初始 prompt（verl 训练直接可用）"

    # 轨迹与 AGL 事件与归因任务数一致
    n_traj = len((tmp_path / "trajectories.jsonl").read_text(encoding="utf-8").strip().splitlines())
    n_agl = len((tmp_path / "agl_events.jsonl").read_text(encoding="utf-8").strip().splitlines())
    assert n_traj == n_agl == report["n_tasks"]["attribution"]


def test_pipeline_gate_blocks_on_failure(tmp_path, monkeypatch, forced_mock):
    import scripts.rl_pipeline as pipe

    def failing_gate(extended: bool) -> dict:
        return {
            "pass": False,
            "reasons": ["alert_precision 0.0% < 朴素基线 50.0%"],
            "alert_precision": 0.0,
            "naive_alert_precision": 0.5,
            "root_cause_accuracy": 0.0,
            "benign_suppression": 0.0,
        }

    monkeypatch.setattr(pipe, "_gate", failing_gate)
    monkeypatch.setattr(sys, "argv", ["rl_pipeline.py", "--seeds", "1", "--out", str(tmp_path)])
    rc = pipe.main()
    assert rc == 1
    # 拦截时报告仍须落盘（失败现场可回查）
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert not report["gate"]["pass"]


def test_gate_uses_real_evaluation():
    """_gate 必须真跑评测（不许造假门禁）。"""
    from scripts.rl_pipeline import SEED0, _gate

    gate = _gate(extended=False)
    assert gate["pass"]
    assert gate["alert_precision"] >= gate["naive_alert_precision"]
    assert SEED0 == 20240102
