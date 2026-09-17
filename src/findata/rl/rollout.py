"""轨迹采集（M14）：slime 式的函数循环，无框架仪式。

一个 rollout 就是「policy.act ↔ env.step」的 while 循环加上终态计分。
奖励不在这里发明——按 data_source 分发到 reward 模块的纯函数，
环境终态 info 提供结构化输入（提交的 Diagnosis / 答案与 run_ids）。

TrajectoryGroup 是 GRPO 的分组单位：同一任务的多次 rollout 的优势
来自组内相对奖励，所以 group 的语义约束是「同任务、可复现环境、
不同采样」——同 seed 的确定性环境保证前两条，采样差异来自
policy（temperature>0 的真实 LLM 或不同的 Mock 队列）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from findata.rl.reward import ask_reward, attribution_reward
from findata.rl.types import (
    ASK,
    ATTRIBUTION,
    Episode,
    RLTask,
    Step,
    StepStatus,
    Trajectory,
    TrajectoryGroup,
)


@dataclass
class _Actor:
    """统一的策略接口：ask 任务用空动作占位（图在 env.step 内部整体执行）。"""

    fn: Callable[[list[dict]], dict] | None

    def act(self, messages: list[dict]) -> dict:
        return self.fn(messages) if self.fn else {}


def rollout_episode(task: RLTask, env, policy) -> Episode:
    """跑一个 episode：多轮交互 + 终态计分。

    策略异常/环境异常都兜成 reward=0 的 error episode（对齐 rLLM 的
    OutcomeError 处理：一个坏样本不该打断整批 rollout，但要如实留痕）。
    """
    actor = _Actor(None) if (task.data_source == ASK and policy is None) else policy
    messages = env.reset(_task_desc(task))
    traj = Trajectory()
    info: dict = {}
    error: str | None = None
    hard_cap = getattr(env, "max_turns", 8) + 2  # 环境超时兜底之外的最后防线

    try:
        for _ in range(hard_cap):
            chat_snapshot = [dict(m) for m in messages]
            action = actor.act(messages)
            if action:
                messages.append(
                    {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}
                )
            result = env.step(action)
            traj.steps.append(Step(chat=chat_snapshot, action=action, status=_status(action, task)))
            messages.extend(result.observations)
            info = result.info
            if result.done:
                if task.data_source == ASK and info.get("answer") is not None:
                    # 终态答案写入轨迹：它是 assistant 的最后一条消息
                    messages.append({"role": "assistant", "content": info["answer"]})
                break
        else:
            error = f"hard cap {hard_cap} reached（环境未按约定终止）"
    except Exception as exc:  # noqa: BLE001 — 兜底转 error episode
        error = f"{type(exc).__name__}: {exc}"

    if error is not None:
        return Episode(task=task, trajectory=traj, reward=0.0, info=info, error=error)

    reward, breakdown = _score(task, info)
    return Episode(task=task, trajectory=traj, reward=reward, breakdown=breakdown, info=info)


def rollout_group(
    task: RLTask,
    env_factory: Callable[[], object],
    policy_factory: Callable[[], object],
    n: int,
) -> TrajectoryGroup:
    """同一任务跑 n 次，产出 GRPO 组。环境/策略用工厂构造，避免状态串场。"""
    group = TrajectoryGroup(task=task)
    for _ in range(n):
        group.episodes.append(rollout_episode(task, env_factory(), policy_factory()))
    return group


def _task_desc(task: RLTask) -> dict:
    """env.reset 的入参。AttributionEnv 忽略它；AskEnv 需要问题文本。"""
    question = ""
    for m in task.prompt:
        if m.get("role") == "user":
            question = str(m.get("content", ""))
            break
    return {"question": question}


def _status(action: dict, task: RLTask) -> StepStatus:
    if task.data_source == ASK:
        return StepStatus.FINISH
    name = action.get("action")
    if name == "submit_attribution":
        return StepStatus.FINISH
    if name in {"query_evidence", "recall_memory", "peer_comparison"}:
        return StepStatus.TOOL_CALL
    return StepStatus.MSG


def _score(task: RLTask, info: dict) -> tuple[float, dict]:
    """终态计分：按 data_source 分发到 reward 纯函数（环境 info → 奖励输入）。"""
    if task.data_source == ATTRIBUTION:
        return attribution_reward(
            info.get("submitted"),
            _require_cause(task.ground_truth),
            bool(task.ground_truth["expected_suppressed"]),
            extra_turns=int(info.get("extra_turns", 0)),
        )
    return ask_reward(
        str(info.get("answer", "")),
        task.ground_truth,
        run_ids=list(info.get("run_ids") or []),
        tool_rounds=int(info.get("tool_rounds", 0)),
    )


def _require_cause(ground_truth: dict):
    from findata.dq.models import RootCause

    return RootCause(ground_truth["expected_cause"])


def aggregate(episodes: list[Episode]) -> dict:
    """聚合 reward 分布与分项均值（reward 曲线归档与训练后对比的原料）。"""
    n = len(episodes)
    if n == 0:
        return {"n": 0}
    rewards = [e.reward for e in episodes]
    out: dict = {
        "n": n,
        "reward_mean": sum(rewards) / n,
        "reward_min": min(rewards),
        "reward_max": max(rewards),
        "n_errors": sum(1 for e in episodes if e.error),
    }
    by_source: dict[str, list[Episode]] = {}
    for e in episodes:
        by_source.setdefault(e.task.data_source, []).append(e)
    out["by_data_source"] = {
        source: _agg_group(group) for source, group in by_source.items()
    }
    return out


def _agg_group(episodes: list[Episode]) -> dict:
    rewards = [e.reward for e in episodes]
    out: dict = {
        "n": len(episodes),
        "reward_mean": sum(rewards) / len(rewards),
    }
    keys: set[str] = set()
    for e in episodes:
        for k, v in e.breakdown.items():
            # bool 参与（True→1.0）：提交率这类布尔分项本身就是有意义的均值
            if isinstance(v, (int, float)):
                keys.add(k)
    out["breakdown_mean"] = {
        k: sum(float(e.breakdown.get(k, 0.0)) for e in episodes) / len(episodes)
        for k in sorted(keys)
    }
    return out
