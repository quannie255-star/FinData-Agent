"""训练侧数据导出（M14）：任务行 + 轨迹/奖励事件。

两个出口对应有卡后的两条训练路线（见 docs/rl-experiment.md）：

- **verl 直连**：``write_tasks_jsonl`` 产出的行即 verl 训练数据约定
  （``prompt``/``data_source``/``env_class``/``reward_spec``/``extra_info``），
  有 pyarrow 时可一键转 parquet；奖励函数用
  ``findata.rl.reward.compute_reward``（签名与 verl 的
  custom_reward_function 完全一致）挂载即可。
- **Agent Lightning**：``write_agl_events`` 产出 AGL v1.0 的事件流格式
  （rollout + 终态 reward 事件 ``{"event_type": "reward", "data": {"value": x}}``），
  用于对照与回放；真实训练时 AGL 网关自动录制模型调用，不需要手工喂轨迹。

JSONL 是主格式（零依赖、任何干净环境可复现），parquet 是可选项——
不把 pyarrow 强塞进主依赖，与 langchain 的软依赖处理同一哲学。
"""

from __future__ import annotations

import json
from pathlib import Path

from findata.rl.types import Episode, RLTask


def task_to_row(task: RLTask) -> dict:
    return task.to_verl_row()


def write_tasks_jsonl(tasks: list[RLTask], path: str | Path) -> int:
    """任务集 → verl 约定的 JSONL。返回行数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task.to_verl_row(), ensure_ascii=False) + "\n")
    return len(tasks)


def write_tasks_parquet(tasks: list[RLTask], path: str | Path) -> int:
    """任务集 → parquet（verl 的 data.train_files 直接可用的格式）。

    pyarrow 是可选依赖（``uv sync --group rl``）；缺它时报错必须可行动。
    """
    rows = [task.to_verl_row() for task in tasks]
    return _rows_to_parquet(rows, path)


def jsonl_to_parquet(src: str | Path, dst: str | Path) -> int:
    """tasks JSONL → parquet。有卡机器上 verl data.train_files 的最后一跳。"""
    rows = [
        json.loads(line)
        for line in Path(src).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return _rows_to_parquet(rows, dst)


def _rows_to_parquet(rows: list[dict], path: str | Path) -> int:
    if not rows:
        return 0
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "写出 parquet 需要 pyarrow。安装：uv sync --group rl "
            f"（或改用 write_tasks_jsonl——verl 同样接受 JSONL 转换）。原始错误：{exc}"
        ) from exc

    table = pa.table({k: [row.get(k) for row in rows] for k in rows[0]})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return len(rows)


def episode_to_agl_record(episode: Episode) -> dict:
    """Episode → Agent Lightning v1.0 风格的一条 rollout 记录。

    事件格式对齐 AGL 的 reward 事件（``event_type``/``data.value``）；
    轨迹以消息摘要形式附带（messages 数），完整轨迹在本地 JSONL 里，
    AGL 正式训练时由网关自行录制 token 级数据。
    """
    task = episode.task
    return {
        "rollout_id": f"{task.task_id}",
        "input": {"data_source": task.data_source, "env_class": task.env_class},
        "metrics": {"reward": episode.reward, "n_turns": episode.trajectory.n_turns},
        "events": [{"event_type": "reward", "data": {"value": episode.reward}}],
        "status": ("error" if episode.error else "completed"),
        "error": episode.error,
    }


def write_agl_events(episodes: list[Episode], path: str | Path) -> int:
    """Episode 列表 → AGL 事件 JSONL。返回行数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for episode in episodes:
            f.write(json.dumps(episode_to_agl_record(episode), ensure_ascii=False) + "\n")
    return len(episodes)


def write_trajectories(episodes: list[Episode], path: str | Path) -> int:
    """完整轨迹（含每步 chat 快照与动作）→ JSONL。训练后归因分析的原料。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for episode in episodes:
            f.write(
                json.dumps(
                    {
                        "task_id": episode.task.task_id,
                        "data_source": episode.task.data_source,
                        "reward": episode.reward,
                        "breakdown": episode.breakdown,
                        "error": episode.error,
                        "steps": [
                            {
                                "status": s.status.value,
                                "action": s.action,
                                "context_messages": len(s.chat),
                            }
                            for s in episode.trajectory.steps
                        ],
                        "final_messages": episode_snapshot(episode),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(episodes)


def episode_snapshot(episode: Episode) -> list[dict]:
    """episode 的最终消息序列（从最后一步的上下文 + 动作重建）。"""
    traj = episode.trajectory
    if not traj.steps:
        return []
    last = traj.steps[-1]
    messages = [dict(m) for m in last.chat]
    if last.action:
        messages.append(
            {"role": "assistant", "content": json.dumps(last.action, ensure_ascii=False)}
        )
    return messages
