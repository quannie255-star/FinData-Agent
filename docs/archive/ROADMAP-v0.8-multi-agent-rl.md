# 开发路线图 v0.8 → v1.0：多智能体 · 自进化 · Agent RL

> 本文是下一阶段（2026-09 起）的开发路线图。README「路线图」一节记录已完成的
> 里程碑（M1–M11），本文展开未来三个版本的实操计划（M12–M14）。
>
> 写作动机：2026 秋招/社招 JD 的高频关键词已从「会用 Agent 框架」演进为
> **「多智能体协作 · Agent RL · 自进化」**（美团 LongCat「AI 自进化」、字节
> 「Agent RL/多智能体优先」、腾讯数据智能体「Text2SQL + SFT/RLHF + 评测」、
> 阿里「自进化智能体」均有明确要求）。本项目已有的一半资产恰好是后两者的地基。
>
> 原则不变：**声明与代码一致**。每个阶段的验收门禁先写在这里，做完的才算数；
> 评测报告归档 `examples/`，成本（token/延迟/GPU）如实报告，不藏在效果后面。

---

## 零、盘点：离三个关键词各差多少

| JD 关键词 | 现状 | 缺口 | 难度 |
| --- | --- | --- | --- |
| 多智能体协作 | 分析层已有 LangGraph 单 Agent 回环（`parse→tools→finalize`，`agent/graph.py`，工具白名单 + `MAX_TOOL_ROUNDS=4`） | 拆成职责单一的子 Agent + Supervisor；把 DQ 监控产物接入**运行时**背书 | 低——框架已就位，纯重构 |
| Agent 自进化 | 有确定性故障注入评测（`eval/faults.py`）+ golden 集（`eval/golden/`）+ CI 门禁（`run_eval.py --min-*`、`eval_golden.py --strict`） | 缺「优化器」：prompt 进化、经验记忆、（进阶）工作流搜索 | 中——评测集是现成的适应度函数 |
| Agent RL | 有可确定性计算的奖励信号（归因一致性 / 告警精确率 / 答案正确性 + run_id 保留率） | 缺训练管线：环境封装、GRPO、训练后回归 | 高——需要 GPU 预算，有降级路径 |

**本项目的独特优势**：评测集既是 CI 门禁，也是自进化的适应度函数，也是 RL 的
奖励函数。三个阶段复用同一套评测资产，边际成本递减——这是多数开源项目
（包括 AutoGen/LangGraph 官方示例）没有的东西。

---

## 一、Phase 1 · v0.8.0 多智能体重构（M12）

### 目标

问数分析层从单 Agent 回环重构为 Supervisor 编排的多智能体，并第一次让监控层
在运行时（而不只是共享 DuckDB 底座）为分析层背书。

### 架构

```
用户问题
  └─→ Supervisor（路由 + 复杂度判断 + 溯源强制检查）
        ├─ 简单题：直通 QueryAgent 一次调用（省 token/延迟）
        └─ 复杂题：按需编排
             ├─ SchemaAgent    选指标 / 填参（现 parse 节点 + list_metrics）
             │      ↓
             ├─ QueryAgent     语义层编译 SQL 并执行（现 tools 节点，产出带 run_id）
             │      ↓
             ├─ VerifierAgent  独立交叉验证 + 拉取 DQ 上下文
             │                 （Finding / Diagnosis / 抑制记录：
             │                  "该指标 3 天前刚被抑制过一次误报"）
             │      ↓ 追问"这批数据可信吗"
             ├─ TriageAgent    LLMTriage 包成工具（复用 dq/triage.py）
             │
             └─ 强制检查：run_id 不齐 / 验证标记缺失 → 不得出答案
```

### 拆分原则（延续 README「技术选型」的既有结论，不自相矛盾）

- 监控层**仍是确定性 DAG，不上 LangGraph**——那条取舍（PITCH Q2）不变，
  多智能体只发生在分析层
- LLM 仍不写 SQL，SQL 由语义层编译器确定性生成——职责平移，不新增自由度
- **简单题走快路径**：单指标直查不必"四人开会"。多智能体是有成本的
  （token/延迟），成本对比要进评测报告，这是本阶段的诚实义务

### 子 Agent 职责表

| Agent | 职责 | 来源 |
| --- | --- | --- |
| SchemaAgent | 理解问题 → 选指标/填参（现 parse 节点 + `list_metrics`） | 现有代码平移 |
| QueryAgent | 调语义层编译 SQL 并执行（现 tools 节点） | 现有代码平移 |
| VerifierAgent | 独立交叉验证数字/溯源 + 拉取 DQ 上下文（`Finding`/`Diagnosis`/抑制记录，如"该指标 3 天前刚被抑制过一次误报"） | 新增；复用 `semantic/verifier.py` + `dq/` 产物 |
| TriageAgent | 归因子 Agent：把 `dq/triage.py` 的 LLMTriage 包成工具，供 VerifierAgent 追问"这批数据可信吗" | 复用 `dq/triage.py` |
| Supervisor | 路由 + 复杂度判断 + 溯源强制检查 | 新增 |

### 改动清单

- `agent/graph.py` 拆为 `agent/nodes/`（各子 Agent）+ `agent/supervisor.py`
- MCP（`mcp/server.py`）新增 `findata_ask`：把整个可信问数 Agent 暴露为一个
  工具，成为外部 Agent 的"可信问数"入口——从"有 MCP"升级为"可被别的 Agent 编排"
- 评测：`eval/golden/` 扩 agent 级指标（答案正确率、run_id 保留率、平均工具
  轮数、token 成本、端到端延迟），**单 Agent vs 多智能体对比数据进 README**

### 验收门禁

- [x] golden 集 `eval_golden.py --strict` 不回归（5/5 通过）
- [x] 全部现有测试绿 + 各子 Agent 单测（Supervisor 路由表全分支覆盖）
      （190 例绿；`tests/test_multi_agent.py` 覆盖路由全分支 / gate 重试与
      放行 / verifier ⇄ dq_tools 回环 / triage 降级 / 端到端对话流）
- [x] 对比报告归档 `examples/`：正确率、成本、延迟三项都报，成本上升如实写
      （`examples/eval-agent-single-vs-multi.txt`：DeepSeek 实测，正确率
      100% → 100%，run_id 保留率 100% → 100%，token/题 2074 → 2506
      即 **+21% 如实入账**，延迟 3.56s → 3.20s 基本持平）

**实施注记（与原计划的偏差，如实记录）**：

- Supervisor 复杂度判断落为**确定性规则**而非 LLM——路由本身也要花钱，
  规则零成本、可全分支单测；判错的代价只是"简单题多走一轮验证"
- TriageAgent 以**工具形态**挂在 VerifierAgent 下（`triage_trust`），
  不是图里的独立节点——归因是"被追问才有"的深挖动作，简单题不值得
- DQ 运行时背书落地为 `dq_signal` 历史表（`service.inspect` duckdb 源
  巡检产物幂等落库）——VerifierAgent 的 `dq_context` / `triage_trust`
  都以它为数据源，也是 M13 经验记忆的地基
- 评测中发现并修复一个结构性 bug：`invoke()` 漏把用户问题注入 messages
  时，脚本化假模型测试全绿而真实模型 7 题全错——**agent 级评测必须用
  真实模型跑**，这正是把对比评测列为本阶段验收门禁的理由

### 借鉴项目

- [LangGraph 多智能体官方模式](https://langchain-ai.github.io/langgraph/)（supervisor / swarm）
- [Anthropic：How we built our multi-agent research system](https://www.anthropic.com/engineering/built-multi-agent-research-system)（orchestrator-worker 范式与"何时该多 Agent"的工程判断）
- [DB-GPT](https://github.com/eosphoros-ai/DB-GPT) / [SuperSonic](https://github.com/tencentmusic/supersonic) / [WrenAI](https://github.com/Canner/WrenAI)（同领域"语义层 + Agent"的拆分方式；**差异化 = 我们有 DQ 运行时背书，它们没有**）

---

## 二、Phase 2 · v0.9.0 自进化闭环（M13）

### 目标

让 prompt 成为被评测、被进化、被门禁管理的资产——而不是手改手调。

### 2.1 Prompt 进化（零 GPU，首选）

- 工具：[DSPy](https://github.com/stanfordnlp/dspy) + [GEPA](https://github.com/gepa-ai/gepa)
  （`dspy.GEPA`，反思式提示进化；论文口径样本效率优于 GRPO）
- 两个优化对象：
  1. **LLM 归因器 prompt**（`dq/triage.py`）：适应度 = 根因准确率 / 告警精确率
     （同 13 条语料 + 扩充语料）。已归档基线：DeepSeek 实测 85.7% / 60%，
     规则引擎 100%（κ=0.74/0.65，见 `examples/eval-llm-triage-real.txt`）。
     目标不是"超过规则引擎"，是在"规则为主、LLM 为辅"的定位上**可测量地变强**
  2. **问数链路 prompt**（parse/finalize）：适应度 = golden 集答案正确率 +
     run_id 保留率
- 进化产物走与代码**相同的 CI 门禁**（eval-gate），不过不许合入——
  "prompt 也是代码，进化也要过门禁"的工程化落地
- 每代 prompt + 分数归档 `eval/prompts/`（版本化，可回滚可对比）

### 2.2 经验记忆

- TriageAgent 跨 run 记忆：历史归因结论结构化沉淀（形态指纹 → 根因 → 合法/故障），
  相似 Finding 先召回先例再判断（参考 [Mem0](https://github.com/mem0ai/mem0) 的记忆 schema）
- **记忆写入必须过验证**：与 PITCH Q3「一个像的事实比没有事实更危险」同一原则——
  记忆污染会让系统理直气壮地抑制真告警，宁缺毋滥，低置信记忆只作提示不作判据

**2026-09 对标补充**（[TradingAgents](https://github.com/TauricResearch/TradingAgents)
源码核验：`agents/utils/memory.py` + `graph/reflection.py`，可直接抄的机制）：

- **两阶段写入**：决策时先落 pending 条目（零 LLM 成本），等结果可确认后
  再补 reflection 教训——正好匹配我们"探针告警 → 归因 → 误报确认"的时序；
  未归因的 pending 条目不参与召回
- **结构化 tag 行**：把"日期 | key | 处置 | 量化结果 | resolved 日期"编码进
  单行 tag，解析零 LLM、召回过滤 O(1)；条目用 HTML 注释分隔（防 LLM 输出
  伪造分隔符）
- **召回预算**：同 key 近 5 条全量 + 跨 key 仅 3 条一句话教训——控制上下文
  爆炸；point-in-time 过滤（回放时不得引用"事后才确认"的根因，防时间泄漏）
- **工程细节**：原子写（temp file + rename）、幂等写入、轮转上限（pending
  永不丢）
- **我们的差异化空间**（TradingAgents 没做的）：反思置信度评分 + 冲突消解
  （如"被抑制误报 N 天后复发则自动降权该条经验"）——与上面"记忆污染防护"
  原则闭环。M12 的 `dq_signal` 历史表即记忆的地基

### 2.3 进阶（可选）：工作流搜索

- [AFlow](https://github.com/geekan/MetaGPT) 式 MCTS 在评测集上搜索更优图拓扑
  （例："VerifierAgent 追问一轮值不值得 token 成本"由数据决定而非拍脑袋）

### 难例靶子（来自 PITCH Q5 的真实失败，不是假想敌）

> 成交量放大 5.4x（2023-07 券商政策行情，合法）vs 单位变更（故障）：
> 探针的 10 日均量把评测注入的 20x 稀释成 5.14x，与真实放量的 5.40x 几乎重合，
> 规则归因已证明分不开（硬塞规则会让根因准确率 100% → 85.7%，门禁挂掉）。

这是自进化的理想试金石：构造含此形态的扩充语料，看进化后的 LLM 归因能否
**分开规则分不开的形态**。成了，就是"自进化产生超越规则引擎能力"的实测证据；
不成，也是诚实的负结果归档（项目先例：撤回 5.4x 规则时就是这么做的）。

### 验收门禁

- [x] GEPA 进化前后对比报告归档 `examples/`（复用 `eval-llm-triage-real.txt` 格式）
      （`examples/eval-prompt-evolution.txt`：v1 → v1.1 → v2 三代版本、
      四列对比、进化全程与成本如实入账）
- [x] 进化产物过 eval-gate：根因准确率 / 告警精确率不低于进化前
      （v2 在 extended + base 双语料 根因/抑制/精确 全 100%，接受时与
      独立复测各验证一次；已合入代码默认，与 `eval/prompts/triage_v2.yaml`
      逐字节一致）
- [x] 记忆机制有单测 + 记忆污染防护用例（低置信记忆不得改变抑制决策）
      （`tests/test_memory.py`：低置信毒化记忆下抑制决策与 prompt 逐字节
      不变；另有两阶段写入 / 召回预算 / 时间点过滤 / 冲突消解共 14 例）

**实施注记（与原计划的偏差与新发现，如实记录）**：

- **难例靶子打赢了，且路径可解释**：扩充语料（`eval/extended.py`）把
  ROADMAP 里的真实案例（5.4x 放量 vs 单位变更被稀释成 5.14x）语料化——
  脉冲与故障同形态（×20、10 日），探针比值同分布（5.31x 落在脉冲区间
  [5.17, 7.43] 内部），阈值规则数学上无缝隙。判别力来自给
  `build_evidence` 补的两个确定性事实：量额一致性（~1 vs ~4.6）与
  同窗口共振（2 vs 0）。**规则引擎一行未改**：它不利用这两个事实是
  定位选择（利用了就重演「硬塞规则 100% → 85.7%」，探索期在 v1.1
  基线上实测复现），不是能力差距。进化产物（v2）学到的判别式
  「量额比近 1 + 同窗口共振 → benign_market_action」正是 ROADMAP
  预期的「进化产生超越规则引擎能力」的证据。
- **进化的最大敌人不是不会改，而是假信号**：v1 的「根因 100%」由
  规则兜底撑起（模型 10/16 条自造 "P3" 被拒收降级，规则答对记成系统
  答对）。适应度若不区分「模型亲判」与「降级兜底」，进化会奖励高
  拒收率。三条修正（亲判记分 / 判定率进 Pareto / 对照语料只要求零
  回归）都是从实测失败里长出来的，各有单测。
- **反思器发现了自己的契约缺陷**：五轮一致提议「severity 取白名单」，
  按真实团队用法由维护者确认固化为 v1.1 基线（版本档案，非代码改动），
  再从去噪后的基线继续进化——进化的职责是非显然的行为规则，契约
  修复走人审。
- **dspy.GEPA 落为可选路线**（`evolve` 依赖组已装、脚本已接
  `--engine dspy`），实际收敛由内置 GEPA-lite 完成：小语料 + 严格
  输出契约下，零依赖实现的提议-筛选-验收管线更可控、预算可硬顶。
- **记忆写入点在 LLM 归因发生处**（`triage_trust` 工具与评测路径），
  巡检主路径保持零 LLM 规则（`service.inspect` 不变）——比原计划的
  「service.inspect 写记忆」更诚实：规则归因是确定性的，记忆对它无增量。
- **记忆指纹带判别证据标签**（`fingerprint(finding, ctx, extra)`）：
  混淆对在 probe|metric|比值分桶上同指纹，靠 va_shift 分桶分开——
  否则先例召回会把「上次这形态是故障」错误背书给合法放量。
  依赖方向保持 dq 不 import agent：标签由调用方（LLMTriage）计算传入。

### 借鉴项目

- [AgentEvolver](https://github.com/modelscope/AgentEvolver)（自提问/自导航/**自归因**——"自归因"与本项目归因层同构，最对口的参照系）
- [GEPA](https://github.com/gepa-ai/gepa) / [DSPy](https://github.com/stanfordnlp/dspy)（本轮实测：dspy.GEPA 为可选引擎，内置 GEPA-lite 收敛；论文口径样本效率优于 GRPO）；
  [NousResearch/hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution)（2026-04 GitHub Trending：DSPy+GEPA 从真实执行痕迹进化 skills/prompt/工具描述，与本项目「评测集当适应度函数」同构）
- [AFlow/ADAS](https://github.com/XMUDeepLIT/Awesome-Self-Evolving-Agents)（进阶）；
- 记忆：[Mem0](https://github.com/mem0ai/mem0)（记忆层 schema）、
  [TradingAgents](https://github.com/TauricResearch/TradingAgents)（两阶段写入 + 结构化 tag + 召回预算，已落地）、
  [BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS)（EMNLP 2025 Oral，分层记忆 OS）、
  [NirDiamant/Agent_Memory_Techniques](https://github.com/NirDiamant/Agent_Memory_Techniques)（Mem0/Letta/Zep 实操对比）

---

## 三、Phase 3 · v1.0.0 Agent RL 管线（M14）

### 目标

跑通「环境 → 奖励 → 训练 → 评测回归」完整管线。规模可以小，闭环必须真。

### 环境与奖励（全部复用现有资产）

| 组件 | 来源 | 说明 |
| --- | --- | --- |
| RL 环境 | `eval/faults.py` 确定性故障注入器 | 可重置、可复现，比抓取真实环境的 SWE-agent 干净得多 |
| 任务 A 奖励 | 归因 vs 规则引擎/人工标注 | 一致性 + 抑制精确率；**稠密且确定性**的奖励 |
| 任务 B 奖励 | golden 集 | 数值正确 + run_id 保留 + 工具轮数惩罚 |

### 管线选型

- [Agent Lightning](https://github.com/microsoft/agent-lightning)（微软）：包住现有
  LangGraph 图**零重写**，agent 执行与模型训练解耦（LightningRL 分层信用分配）
- 训练后端：[verl](https://github.com/volcengine/verl)（字节 HybridFlow，生产级）+ GRPO
- 架构参照：[SkyRL](https://github.com/novasky-ai/skyrl)、[rLLM](https://github.com/rllm-org/rllm)（harness/sandbox/训练后端解耦）；环境封装参照 [AgentGym-RL](https://github.com/woooodyy/AgentGym-RL)

### 成本控制与降级路径

- LoRA + 7B 级模型，先跑通**窄任务**（归因决策）再谈扩展
- **无 GPU 预算时**：本阶段降级为「环境与 reward 封装 + 实验设计文档」，
  训练等有卡再做。Phase 2 的 GEPA 路线本身在样本效率上就是 GRPO 的合法替代，
  自进化叙事不受影响

### 验收门禁

> **v0.10.0 交付状态（2026-09-15）**：按降级路径完成「环境与 reward 封装 +
> 实验设计文档」，数据侧闭环全通（`scripts/rl_pipeline.py` 一条命令；
> 真实 LLM 基线归档 `examples/rl-baseline.txt`）。训练侧待 GPU。

- [x] 训练前基线与训练后对比归档（reward 曲线 + eval-gate 回归）
      —— 训练前基线已归档（`examples/rl-baseline.txt`：rollout reward /
      规则基线同尺子对照 / eval-gate 通过）；「训练后」待有卡按
      `docs/rl-experiment.md` §3.4 重跑同一命令对比
- [ ] 训练不劣化：golden 集与归因语料的全部指标不低于训练前
      （待有卡；对照流程与门禁已在管线内就位）
- [x] 可复现：固定 seed / 数据版本，一条命令重跑训练与评测
      —— `scripts/rl_pipeline.py`（SEED0 固定、fixture 确定性、57 个
      RL 单测全确定性；「训练」命令在有卡后补入同一入口）

---

## 四、诚实边界（这个阶段明确不做的）

- **不为 RL 而 RL**：规则归因器在该语料上是 100%，LLM/训练路线的定位是
  "规则分不开的形态"与"规则覆盖不到的新故障"，不是替代规则
- **监控层不上 LangGraph/多智能体**：确定性 DAG 的取舍不变
- **不做 DGM 式自我改代码**：自进化范围锁定 prompt / 记忆 / 工作流三个可控维度，
  每个维度都有门禁；「agent 改自己源码」只作面试谈资，不进生产路径
- **成本如实报告**：多智能体 token 成本、进化实验开销、GPU 用量都进评测报告

---

## 五、与投递材料的衔接

JD 关键词 → 里程碑映射（投递前逐项自查）：

| JD 关键词 | 对应里程碑 | 证据位置（做完后填） |
| --- | --- | --- |
| 多智能体系统 | M12（v0.8.0） | README 架构图 + `examples/` 对比报告 |
| Agent 自进化 / prompt 优化 | M13（v0.9.0） | `eval/prompts/` 版本史 + `examples/` 进化对比报告 |
| Agent RL / GRPO / 训练管线 | M14（v1.0.0） | 训练报告 + reward 曲线 |
| 评测系统 / 质量门禁 | **已有**（v0.7.1 eval-gate） | CI + `run_eval.py --min-*` |

每完成一个 Phase 的固定动作：CHANGELOG 记录 → README 路线图打勾 →
`examples/` 归档评测报告 → PITCH.md「三个能撑住追问的数字」增补对应数字。

---

## 六、版本节奏

| 版本 | 里程碑 | 内容 | 前置条件 |
| --- | --- | --- | --- |
| v0.8.0 | M12 ✅ | 多智能体重构 + `findata_ask` + agent 级评测 | 无（纯重构，随时可开） |
| v0.9.0 | M13 ✅ | GEPA prompt 进化 + 归因经验记忆 + 扩充语料 | M12 的 VerifierAgent 承载记忆 |
| v1.0.0 | M14 | Agent RL 管线（Agent Lightning + verl + GRPO） | GPU 预算；无卡则降级为环境封装 |
