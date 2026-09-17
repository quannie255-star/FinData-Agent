# M14 实验设计：Agent RL 管线（有卡后的训练执行手册）

> 状态：**数据侧闭环已完成，权重更新待 GPU**。
> 本文是有卡时的执行手册：数据、奖励、环境、评测回归全部就绪，
> 训练只差「把策略换成可更新的权重」这一步。

## 0. 降级口径（诚实边界）

ROADMAP 对 M14 的定义是「环境 → 奖励 → 训练 → 评测回归」完整闭环，
并预留了降级路径：**无 GPU 预算时降级为「环境与 reward 封装 +
实验设计文档」**。当前交付即该降级口径的完整形态：

| 环节 | 状态 | 说明 |
| --- | --- | --- |
| 环境 | ✅ 完成 | `findata/rl/env.py`：归因多轮窄 agent + golden 问数，确定性可重置 |
| 奖励 | ✅ 完成 | `findata/rl/reward.py`：稠密、确定、归一化 [0,1]，verl 签名兼容 |
| 轨迹采集 | ✅ 完成 | `findata/rl/rollout.py`：episode / GRPO 组 / 聚合 |
| 训练数据导出 | ✅ 完成 | `findata/rl/export.py`：verl 行 JSONL/parquet + Agent Lightning 事件 |
| 基线归档 | ✅ 完成 | `scripts/rl_pipeline.py` 一条命令，报告归档 `examples/rl-baseline.txt` |
| **权重更新（GRPO）** | ⏳ 待 GPU | 本文第 4 节步骤 |

## 1. 环境与奖励（已就绪的资产盘点）

### 1.1 任务 A：数据质量信号归因（窄任务先行，ROADMAP 指定）

- **环境**：`AttributionEnv`（`findata/rl/env.py`）
  - episode = 一个数据质量信号；观测 = 信号概要 + 四个工具
    （`query_evidence` / `recall_memory` / `peer_comparison` / `submit_attribution`）
  - 状态机：查证据 → 提交结论（白名单校验，非法拒收可重试）；
    `max_turns` 超时强制终止（按未提交计分）
  - 可重置性：同 `LabeledItem` + `ProbeContext` → 观测序列逐字节一致
    （`test_rl_env.py::test_reset_is_deterministic_same_seed` 钉死）
- **任务集**：`build_attribution_tasks` —— `run_evaluation` 产物经
  `evolution.build_labeled_items` 对齐标注（**与 M13 进化器同一份语料**，
  故障优先），prompt 在构建时冻结进任务行，训练侧零环境依赖
- **奖励**：`attribution_reward`
  - 主干：根因判对（权重 1.0）；辅助：抑制决策对（0.3）+ 格式合法（0.1）
  - 超时惩罚封顶 0.5；总分归一化 [0,1]
  - 主干放「任务本质」是刻意的：dense 辅助分量权重小，防 reward hacking
    （比如为拿格式分只输出格式不管对错）

### 1.2 任务 B：可信问数（golden 集）

- **环境**：`AskEnv` —— LangGraph 多智能体图本身即策略（agent 零改动，
  Agent Lightning 的接入哲学），环境只注入问题、回收 answer/run_ids/usage
- **任务集**：`eval/golden/agent.yaml` 的 7 个 case（2 道 deep）
- **奖励**：`ask_reward` —— 数值正确（0.6）+ run_id 保留率（0.2）+
  格式（0.2），工具轮数超预算惩罚（封顶 0.3）；
  判分与 M12 agent 级评测共用 `findata/eval/agent_judge.py`（同一把尺子）

### 1.3 为什么这套奖励适合 RLVR

奖励**稠密且确定性**：同样的提交永远得同样的分，不依赖模型裁判。
这属于 verifiable reward 的理想形态——不需要训练过程奖励模型（PRM），
直接规则计算。唯一要防的是 dense 分量被 hack，已通过权重结构控制。

## 2. 现有基线（训练前对照物的归档口径）

```bash
# mock 模式（无 key，CI 安全）：验证管线一致性
uv run python scripts/rl_pipeline.py --seeds 3 --extended

# 真实模式（检测到 FINDATA_LLM_API_KEY 自动启用）：策略基线归档
uv run python scripts/rl_pipeline.py --seeds 3 --extended \
    --archive examples/rl-baseline.txt
```

报告含三层读数（见 `examples/rl-baseline.txt`）：

1. **规则基线 reward**：`BaselineTriage` 用同一把奖励尺子计分——
   规则引擎的 reward 水位是「训练必须打赢的对手」（该语料上根因
   接近全对，reward ≈ 0.8-0.95）
2. **rollout reward**：当前 LLM 策略（temperature=0）在环境里的分布
3. **回归门禁**：`alert_precision ≥ naive_alert_precision`（归因层
   价值仍在的最低断言），error episode 必须为 0

**训练后的对比基准就是第 1、2 条**：训练后的策略 reward 曲线不得低于
训练前归档，且 golden 集与归因语料全指标不低于训练前（ROADMAP 门禁 #2）。

## 3. 训练路线：Agent Lightning + verl + GRPO

### 3.1 架构（Agent Lightning v1.0，2026-08 重构后）

```
┌─ GPU 机器 ──────────────────────────────┐
│  Trainer (verl + vLLM, GRPO)            │
│    ↑ 训练样本（token ids + logprobs）     │
│  API Gateway (agl-server :8181)         │
│    ↑ 模型调用自动录制（OpenAI 兼容代理）   │
│  Rollout Controller (agl-controller)    │
└────────────┬────────────────────────────┘
             │ rollout 级代理 URL / 事件上报
┌────────────┴────────────────────────────┐
│  Agent 机器（本项目，无需 GPU）           │
│  - LangGraph 多智能体（任务 B）零改动，    │
│    base_url 指向 AGL 网关即可             │
│  - AttributionEnv + TriageAgentPolicy    │
│    （任务 A），LLM 客户端指向 AGL 网关     │
│  - 终态 POST reward → AGL_EVENT_URL      │
└─────────────────────────────────────────┘
```

关键事实（调研结论，2026-09）：
- Agent Lightning v1.0（`agentlightning` 1.0.1，Python ≥ 3.12）对
  LangGraph **零改动**接入：把 LLM 的 base_url 换成 rollout 级代理
  URL，每次模型调用自动录制为训练数据（prompt/response token ids +
  logprobs）；episode 结束向 `AGL_EVENT_URL` POST
  `{"event_type": "reward", "data": {"value": x}}`
- RL 后端唯一支持 verl（≥0.5，推荐 0.8.0 + vLLM 0.20.2），
  **必须 GPU**（最低单卡 A100 起）；不支持原生 Windows（需 WSL）
- 多轮合并是自动的：trajectory 聚合模式下，后一次调用 prompt 精确
  延续前一次时自动合并为一行，工具观测 token 被 mask 出 policy loss

### 3.2 接线点（代码里已预留）

| 接线点 | 位置 | 有卡时动作 |
| --- | --- | --- |
| LLM base_url 注入 | `findata/agent/llm.py::OpenAICompatClient`（任务 A）与 `make_chat_model`（任务 B） | base_url 改为 `AGL_OPENAI_BASE_URL` 环境变量注入的 rollout 级代理 URL |
| reward 上报 | `findata/rl/rollout.py::rollout_episode` 拿到 `ep.reward` 后 | 有卡版在此处（或其包装脚本）向 `AGL_EVENT_URL` POST reward 事件；事件格式已在 `export.episode_to_agl_record` 落地 |
| 奖励函数 | `findata/rl/reward.py::compute_reward` | verl 路线直接挂载：`+custom_reward_function.path=... custom_reward_function.name=compute_reward`（签名逐参数一致） |
| 训练数据 | `eval/rl_runs/<ts>/tasks_*.jsonl` | verl 路线：`uv sync --group rl` 后 `write_tasks_parquet` 一行转 parquet |

### 3.3 推荐训练配置（第一轮实验）

- **模型**：Qwen2.5-7B-Instruct（或同档开源 7B），LoRA（r=16, alpha=32），
  ROADMAP 指定的「LoRA + 7B 级模型，先跑窄任务」
- **算法**：GRPO（`algorithm.adv_estimator=grpo`），组大小 8，
  temperature 1.0 采样、temperature 0 评测
- **训练任务**：只训任务 A（归因决策）。任务 B 是 LangGraph 多智能体，
  credit assignment 跨四个子 Agent，第一轮不碰——这也符合 ROADMAP
  「先跑通窄任务再谈扩展」
- **训练/验证切分**：`evolution.split_train_held` 同款思路——
  留出部分 seed 与全部 benign 形态做验证，防止「背答案」
- **参考实现**：Agent Lightning v0.x 的 `examples/spider/`
  （LitAgent 包 LangGraph 的官方范式，v1.0 下逻辑相同、接线更薄）

### 3.4 有卡后的执行步骤（逐步）

```bash
# 0. GPU 机器准备（WSL2 / Linux，CUDA 12.9+，≥1×A100 或 4090 级）
git clone https://github.com/microsoft/agent-lightning && cd agent-lightning
uv sync && bash scripts/setup_verl.sh 0.8.0 cu130   # verl + vLLM 栈

# 1. 数据准备（本项目机器上执行，产物拷到 GPU 机器）
uv sync --group rl
uv run python scripts/rl_pipeline.py --seeds 8 --extended \
    --archive examples/rl-baseline.txt               # 训练前基线（门禁 #1 前半）
#    → eval/rl_runs/<ts>/tasks_attribution.jsonl + report.json 留档
uv run python -c "
from findata.rl.export import jsonl_to_parquet
jsonl_to_parquet('eval/rl_runs/<ts>/tasks_attribution.jsonl', 'train.parquet')
"

# 2. 训练入口（GPU 机器）：LitAgent 包装 TriageAgentPolicy 的多轮循环，
#    rollout() 返回 attribution_reward —— 参考 agent-lightning examples/spider 范式
agl-server &                     # 网关（录模型调用）
agl-controller &                 # 编排 rollout
python train_triage_agent.py     # agl.Trainer(n_runners=N, algorithm=agl.VERL(cfg))

# 3. 训练后回归（本项目机器）
#    - 用训练后的 checkpoint 起 vLLM 服务，FINDATA_LLM_BASE_URL 指向它
#    - 重跑 rl_pipeline（同 seed 同数据版本）
uv run python scripts/rl_pipeline.py --seeds 8 --extended \
    --archive examples/rl-post-training.txt
#    - eval-gate 全量回归（门禁 #2）
uv run python scripts/run_eval.py --corpus both --triage llm

# 4. 门禁 #3：固定 seed / 数据版本，一条命令重跑
#    rl_pipeline 本身即该命令；seed 序列 = SEED0 + range(n) 固定在脚本里
```

### 3.5 风险与对策

| 风险 | 对策 |
| --- | --- |
| 规则引擎在该语料上已近满分，RL 无提升空间 | 这正是 ROADMAP「诚实边界」预判的。RL 策略的目标形态是**规则分不开的形态**（M13 扩充语料）与**新故障类型**；扩充语料已内置（`--extended`），必要时按 `eval/extended.py` 的模式扩混淆对 |
| GRPO 组内奖励无差异（全对/全错）→ 梯度为零 | 提高 temperature 增大组内多样性；混淆对语料保证组内有分歧 |
| reward hacking（格式分/抑制分被钻空） | 权重结构已控制（主干 1.0 : 辅助 0.3/0.1）；`breakdown` 分项入档，训练后可逐项审计 |
| 多智能体任务 B 训练不稳定 | 第一轮不训任务 B（见 3.3）；其数据行与奖励已就绪，留作后续 |

## 4. 验收门禁映射（ROADMAP 三条 → 当前状态）

| 门禁 | 状态 | 证据 |
| --- | --- | --- |
| 训练前基线归档（reward 曲线 + eval-gate 回归） | ✅（降级口径） | `examples/rl-baseline.txt` + `eval/rl_runs/<ts>/report.json`；eval-gate 在管线内强制执行 |
| 训练不劣化（golden 集与归因语料全指标不低于训练前） | ⏳ 待有卡 | 3.4 步骤 3 的对照流程；基线已归档可比 |
| 可复现（固定 seed / 数据版本，一条命令） | ✅ | `scripts/rl_pipeline.py`（seed 序列固定、fixture 确定性、57 个 RL 单测全确定性） |

## 5. 借鉴来源（2026-09 调研）

- **Agent Lightning**（microsoft/agent-lightning，v1.0.1）：agent 执行与
  训练解耦的网关架构；LangGraph 零改动接入；reward 事件协议
- **verl**（volcengine/verl）：训练数据行约定（prompt/data_source/
  reward_spec/extra_info）；custom_reward_function 四参数签名
- **SkyRL**（novaSky-ai/SkyRL）：BaseTextEnv 的 reset/step/close 环境接口
  与 gymnasium 式注册表；env_class 列按行路由环境
- **rLLM**（rllm-org/rllm）：Task/Step/Trajectory/Episode/TrajectoryGroup
  轨迹契约；Step.status 做 loss mask 的字段设计
- **verifiers**（PrimeIntellect-ai/verifiers）：Rubric 模式——奖励为独立
  纯函数，在终态加权评分，与环境和策略完全解耦
- **slime**（THUDM/slime）：函数式 rollout 循环（无框架仪式）
- 过程/结果奖励的取舍：RLVR 确定性奖励为主干 + 小权重 dense 辅助
  （NeurIPS 2025 Process vs Outcome 等实践共识）
