"""RL 导出测试：verl 行约定、AGL 事件格式、JSONL 往返、parquet 可选路径。"""

from __future__ import annotations

import json

from findata.rl.export import (
    episode_to_agl_record,
    task_to_row,
    write_agl_events,
    write_tasks_jsonl,
    write_trajectories,
)
from findata.rl.types import Episode, RLTask, Trajectory


def _task() -> RLTask:
    return RLTask(
        task_id="attr-1-000",
        data_source="attribution",
        env_class="attribution",
        prompt=({"role": "user", "content": "请归因"},),
        ground_truth={"expected_cause": "upstream_stale", "expected_suppressed": False},
        extra_info={"seed": 1},
    )


def _episode(task: RLTask, reward: float = 0.9, error: str | None = None) -> Episode:
    return Episode(task=task, trajectory=Trajectory(), reward=reward, breakdown={}, error=error)


class TestVerlRow:
    def test_required_fields(self):
        row = task_to_row(_task())
        # verl 训练数据行约定：字段名即契约（examples/data_preprocess/gsm8k.py 模式）
        assert row["data_source"] == "attribution"
        assert row["prompt"] == [{"role": "user", "content": "请归因"}]
        assert row["env_class"] == "attribution"
        assert row["reward_spec"]["method"] == "rule"
        assert row["reward_spec"]["ground_truth"]["expected_cause"] == "upstream_stale"
        assert row["extra_info"]["seed"] == 1


class TestJsonlWriters:
    def test_tasks_jsonl_roundtrip(self, tmp_path):
        path = tmp_path / "tasks.jsonl"
        n = write_tasks_jsonl([_task(), _task()], path)
        assert n == 2
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        doc = json.loads(lines[0])
        assert doc["data_source"] == "attribution"

    def test_agl_events_format(self, tmp_path):
        path = tmp_path / "agl.jsonl"
        eps = [_episode(_task(), reward=0.9), _episode(_task(), reward=0.0, error="boom")]
        n = write_agl_events(eps, path)
        assert n == 2
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").strip().splitlines()]
        ok, bad = rows
        assert ok["events"][0]["event_type"] == "reward"
        assert ok["events"][0]["data"]["value"] == 0.9
        assert ok["status"] == "completed"
        assert bad["status"] == "error" and bad["error"] == "boom"

    def test_trajectories_jsonl(self, tmp_path):
        path = tmp_path / "traj.jsonl"
        n = write_trajectories([_episode(_task())], path)
        assert n == 1
        doc = json.loads(path.read_text(encoding="utf-8").strip())
        assert doc["task_id"] == "attr-1-000"
        assert "steps" in doc and "final_messages" in doc


class TestParquet:
    def test_missing_pyarrow_gives_actionable_error(self, tmp_path, monkeypatch):
        """pyarrow 缺席时报错必须可行动（软依赖哲学）。"""
        import builtins

        import pytest

        from findata.rl import export

        real_import = builtins.__import__

        def no_pyarrow(name, *args, **kwargs):
            if name.startswith("pyarrow"):
                raise ImportError("No module named 'pyarrow'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pyarrow)
        with pytest.raises(RuntimeError, match="uv sync --group rl"):
            export.write_tasks_parquet([_task()], tmp_path / "t.parquet")

    def test_parquet_if_available(self, tmp_path):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            return  # 环境无 pyarrow：主路径已由 JSONL 测试覆盖
        from findata.rl.export import write_tasks_parquet

        n = write_tasks_parquet([_task()], tmp_path / "t.parquet")
        assert n == 1
        assert (tmp_path / "t.parquet").exists()


class TestAglRecord:
    def test_record_shape(self):
        rec = episode_to_agl_record(_episode(_task(), reward=0.5))
        assert rec["rollout_id"] == "attr-1-000"
        assert rec["metrics"]["reward"] == 0.5
        assert rec["events"][-1]["event_type"] == "reward"
