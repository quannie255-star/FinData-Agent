"""RL 管线（M14）：把「环境 → 奖励 → 轨迹采集 → 训练数据导出」做成闭环数据侧。

模块边界（对齐 SkyRL/rLLM 的 harness/sandbox/训练后端解耦）：
- `types`   : 任务/轨迹/episode 契约（verl 行约定 + OpenAI 消息格式）
- `env`     : 两个确定性环境（归因多轮窄 agent / golden 问数）+ 注册表 + 任务构建
- `reward`  : Rubric 式奖励（纯函数，与环境和策略完全解耦）
- `policy`  : 归因多轮策略（自研轻量循环，不上 LangGraph）+ 问数 runner
- `rollout` : 轨迹采集循环（slime 式函数，无框架仪式）
- `export`  : verl 训练数据 JSONL/parquet + Agent Lightning 事件 JSONL

无 GPU 时的边界如实声明：本包覆盖「环境与 reward 封装 + 数据侧闭环」，
权重更新（GRPO 训练）待有卡后按 docs/rl-experiment.md 执行。
"""

from findata.rl.env import (
    AskEnv,
    AttributionEnv,
    build_ask_tasks,
    build_attribution_tasks,
    make,
    register,
)
from findata.rl.reward import compute_reward
from findata.rl.types import (
    Episode,
    RLTask,
    Step,
    StepResult,
    StepStatus,
    Trajectory,
    TrajectoryGroup,
)

__all__ = [
    "AskEnv",
    "AttributionEnv",
    "Episode",
    "RLTask",
    "Step",
    "StepResult",
    "StepStatus",
    "Trajectory",
    "TrajectoryGroup",
    "build_ask_tasks",
    "build_attribution_tasks",
    "compute_reward",
    "make",
    "register",
]
