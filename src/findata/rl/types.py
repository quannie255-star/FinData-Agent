"""RL 管线的数据契约（M14）。

参照三个前沿项目的收敛设计，各取其一：

- ``RLTask`` 的字段名对齐 **verl** 的训练数据行约定
  （``prompt``/``data_source``/``env_class``/``reward_spec.ground_truth``/
  ``extra_info``），任务侧数据零转换即可进 verl 训练；
- ``StepResult`` 对齐 **SkyRL** ``BaseTextEnvStepOutput``
  （observations / reward / done / metadata）；
- ``Step``/``Trajectory``/``Episode``/``TrajectoryGroup`` 对齐 **rLLM**
  的轨迹结构，其中 ``Step.status``（MSG/TOOL_CALL/FINISH）是日后做多轮
  loss mask 与 GRPO 组内对比的关键字段——环境观测消息要被 mask 出
  policy loss，只有 assistant 的动作 token 参与梯度。

设计取舍：消息一律用 OpenAI chat 格式的 dict，不发明新序列化。
这样轨迹既可直接喂 Agent Lightning 的事件流，也可被人直接阅读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# OpenAI chat 格式的一条消息：{"role": ..., "content": ...}（tool 角色另带 tool_call_id）
Message = dict

# 任务类型标识，同时是奖励函数的分发键（对齐 verl 的 data_source 列语义）
ATTRIBUTION = "attribution"  # 任务 A：数据质量信号归因
ASK = "ask"  # 任务 B：可信问数


class StepStatus(StrEnum):
    """一步动作的性质，供训练侧构造 loss mask。"""

    MSG = "msg"  # 纯文本回复（无工具调用）
    TOOL_CALL = "tool_call"  # 调用了环境工具
    FINISH = "finish"  # 提交终态结论（submit_attribution / 最终答案）


@dataclass(frozen=True)
class RLTask:
    """一个 RL 任务（一次 episode 的输入与标注）。

    frozen 保证任务定义在 rollout 过程中不可变——同 seed 可复现的前提
    是"环境状态可重置"，而可重置的前提是任务本身不可变。
    """

    task_id: str
    data_source: str  # ATTRIBUTION / ASK（奖励分发键）
    env_class: str  # 环境注册 id（对齐 SkyRL 的 env_class 列语义）
    prompt: tuple[Message, ...]  # 初始消息（OpenAI chat 格式）
    ground_truth: dict  # 标注/期望，只进 reward_spec，环境可见但策略不可见
    extra_info: dict = field(default_factory=dict)  # 透传元数据（seed、来源等）

    def to_verl_row(self) -> dict:
        """转 verl 训练数据行。字段名即约定，见 verl examples/data_preprocess。"""
        return {
            "data_source": self.data_source,
            "prompt": list(self.prompt),
            "env_class": self.env_class,
            "reward_spec": {"method": "rule", "ground_truth": self.ground_truth},
            "extra_info": self.extra_info,
        }


@dataclass
class StepResult:
    """环境的一步反馈（对齐 SkyRL BaseTextEnvStepOutput）。

    reward 为 None 表示本步不计分（outcome 主干：只有终态计分）；
    中间步留这个口子是为了日后引入过程奖励时不改接口。
    """

    observations: list[Message]  # 追加给策略的观测消息（tool 角色等）
    reward: float | None = None
    done: bool = False
    info: dict = field(default_factory=dict)


@dataclass
class Step:
    """轨迹中的一步：该步的策略上下文 + 策略动作 + 动作性质。"""

    chat: list[Message]  # 该步时策略看到的完整上下文快照
    action: dict  # 策略动作，如 {"action": "submit_attribution", ...}
    status: StepStatus = StepStatus.MSG


@dataclass
class Trajectory:
    """一次 agent-环境交互的完整轨迹。"""

    steps: list[Step] = field(default_factory=list)

    @property
    def n_turns(self) -> int:
        return len(self.steps)


@dataclass
class Episode:
    """一个任务的完整 rollout 结果：轨迹 + 终态奖励 + 分项明细。

    breakdown 单独存放（而非揉进 reward），是训练后归因分析的原料：
    哪些分项拖了后腿，reward 标量本身说不出来。
    """

    task: RLTask
    trajectory: Trajectory
    reward: float
    breakdown: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)  # 终态 info（backend、超时与否等）
    error: str | None = None  # rollout 异常时非 None，reward 记 0（对齐 rLLM OutcomeError）


@dataclass
class TrajectoryGroup:
    """同一任务的多个 rollout（GRPO 组内对比的分组单位）。

    GRPO 的优势估计来自组内相对奖励，因此组的成员必须是同一任务——
    这正是"环境可重置"存在的意义。
    """

    task: RLTask
    episodes: list[Episode] = field(default_factory=list)

    @property
    def rewards(self) -> list[float]:
        return [e.reward for e in self.episodes]
