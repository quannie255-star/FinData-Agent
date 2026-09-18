# 开发路线图 v3.0 · Agent 训练数据的质检与过滤（2026-09-18 起）

> ## 主线重定向（2026-09-18）——新方向只看这一节与「四·Phase R4」
>
> 对照 10 份目标实习 JD（`docs/jd-coverage.md`）后决定：**主赛道切到
> Agent 训练数据的质检与过滤**。R1–R3 全部转为已验收历史，不删不改。
>
> **一句话**：findata 原来回答「这个数字能不能引用」，现在也回答
> **「这条轨迹能不能拿去训练，以及它为什么会这样」**。
>
> ### 为什么是这条
>
> 1. **JD 命中密度最高**：10 份里 JD9 几乎逐条对口（数据合成、沙箱、
>    评测管线工程化、**评测归因分析**、Tool Calling、MCP、Vibe Coding），
>    JD8/3/1 大面积重叠（MCP、自进化、自动化评测、JSONL/tool_calls）。
> 2. **缺口是真的，不是我编的**：Langfuse / LangSmith / Arize / Braintrust
>    做的是 tracing + 打分 + 人工标注闭环，目标是**改进 agent**；
>    独立对比文（Inference.net, 2026-06）原话——「两者都擅长存储与检查
>    单条 trace，但都不告诉你**几千次运行里究竟是什么一直在出错**」。
>    而 JD9 要的是**生产训练数据**：从海量轨迹里筛出可用的正/负样本。
>    目标不同，不是同一件事的竞争。
> 3. **与现有肌肉 100% 同构，不浪费一分资产**：核心命题还是
>    「形态相同、结论相反」——
>    - 停牌缺行 vs 上游漏采（数据域）
>    - **表面成功但路径错误（蒙对）vs 真成功**（轨迹域）
>    - **表面失败但归因于环境（沙箱超时/工具不存在）vs 真能力不足**（轨迹域）
>
>    归因引擎、徽章、评测门禁全部原地复用，只是换了个域。
>
> ### 明确不做什么
>
> - **不跟 Langfuse/LangSmith 抢 tracing 与看板**——那是红海，且不是 JD 要的
> - 不做 agent 框架、不做编排、不做前端
> - RAG / Spark / A/B 实验：继续不做（`docs/jd-coverage.md` 第 5 节，
>   不因为 JD 里出现过就改口）
>
> ### 领域包视角（架构不变）
>
> | 领域包 | 状态 | 作用 |
> | --- | --- | --- |
> | `domains/finance/` A 股 | ✅ 已建成 | 归因引擎在**数据域**的证据；降级为领域包 1 |
> | `agentops/` Agent 轨迹 | 🚧 新建 | 归因引擎在**轨迹域**的落地，主赛道 |
>
> A 股没被放弃，它是「这套归因不是只会一件事」的证据。

---

# 以下为 v2.0–v2.2 历史（已验收，保留不删）

# 开发路线图 v2.0 · 可信数据引擎 与 可信报告 Agent（2026-09-16 起）

> **定位升级**：v0.7–v0.10 完成了"检测—归因—抑制"监控层与多智能体问数层
> （多智能体 ✅ / 自进化 ✅ / RL 环境封装 ✅·训练待卡；旧路线图归档于
> `docs/archive/ROADMAP-v0.8-multi-agent-rl.md`）。v2.0 起主线转向 PITCH Q6
> 预告的第二条主线的泛化——**"消费时可信"**：
>
> **可信数据引擎**（通用协议 + 领域包：接入数据源，对每张表、每个指标给出
> 带证据链的可信判定）× **可信报告 Agent**（多智能体生成当日数据分析报告，
> 报告上每个数字带可信徽章：哪些可直接引用、哪些只能借鉴、哪些不可用）。
>
> A 股是第一个领域包（知识层已建成），不是这个产品的边界。
>
> 原则不变：**声明与代码一致**。每个阶段验收门禁先写在这里，做完的才算数；
> 成本（token/延迟）如实入账；评测不过不许合入。

---

## 零、定位与护城河（先读这段，防止做偏）

**一句话**：接入数据源，产出经分析可视化的报告，报告上每个数字带
"可信/仅借鉴/不可用"徽章，徽章可点开看归因证据。

- **护城河不是统计检查，是归因证据链**。完整性/唯一性/新鲜度/离群值这些
  通用检查是 Great Expectations / dbt tests / Soda 的主场，我们只把它们
  作为"通用基线包"；差异化在领域包的归因知识：缺 6 行是漏采还是 2022-01
  中信证券配股停牌——这决定徽章是 ✗ 还是 ✓（真实案例见
  `docs/reviews/2026-09-15-attribution.md`）
- **徽章四档，每档可点开看证据**：
  | 徽章 | 含义 | 证据要求 |
  | --- | --- | --- |
  | ✓ 已核验 | 领域知识归因背书 | 归因结论 + 证据引用（公告/事件表/截面共动） |
  | ✓ 基线通过 | 仅通用检查通过，无领域知识参与 | 检查项清单 |
  | ⚠️ 仅借鉴 | 有疑点，人工判断 | 疑点描述（如"近 30 日空值率 0→40%"） |
  | ✗ 不可用 | 归因为真实故障 | 根因 + 建议动作 |
- **多智能体是手段不是卖点**。报告生成有真实分工（采集→分析→可视化→
  可信审校），用现成 Supervisor + nodes 架构；对外叙事的主角是徽章
  与证据链。单 vs 多的 token 账沿用 `eval-agent-single-vs-multi` 的纪律如实归档
- **"任意数据"在 v1 不做承诺**。协议通用（任何数据可跑通用包），智能在
  领域包（首发只有 A 股包）。演示泛化的方式是对比页：同一数据集
  仅通用包 vs +领域包，徽章含金量的差值就是产品本身

---

## 一、资产盘点（新方向视角：复用 / 保留 / 封存）

| 模块 | 状态 | 在 v2.0 中的角色 |
| --- | --- | --- |
| `dq/`（probes · triage · calendar · compiler） | ✅ 已验收 | **引擎核心**，直接复用；R1 加逐指标映射 |
| `report/`（inspect · render · notify） | ✅ 已验收 | 日报骨架；R1 加徽章层 |
| `agent/`（supervisor · nodes · llm · tools） | ✅ v0.8 验收 | 报告 Agent 编排底座；R1 扩 viz/审校节点 |
| `core/db` · `domains/finance/ingest` | ✅ 已验收 | A 股领域包的数据接入（含 `--events` 事件采集路径，**从未跑过**） |
| `eval/`（fixtures · faults · runner · golden） | ✅ 已验收 | CI 门禁照旧；新增徽章层评测 |
| `semantic/` · `mcp/server.py` | ✅ 已验收 | 保留；R3 给 MCP 挂 `trust_check` 工具 |
| `eval/extended.py` · `dq/memory.py` · `agent/llm_triage.py` · `eval/evolution.py` | ✅ v0.9 验收 | **封存保留**：已验收里程碑 + 投递材料证据，不删不改不发展 |
| ~~`rl/` · `scripts/rl_pipeline.py`~~ | 🗑 **已删除（v3.0）** | RL 环境封装无 GPU 训练，且 10 份目标 JD 无一条要 RL；代码保留在 git 历史 |
| `scripts/attribute_alerts.py` · `docs/reviews/` | ✅ | v2.0 的奠基案例：7/7 真实误报考试与归因 |

**清理决策（2026-09-16）**：不删任何已验收代码——它们有测试、有 CI、
有 PITCH 数字背书，删除等于烧掉简历资产。"冗余"以封存标注解决：
新任务严禁往封存模块里加功能。真正清除的是位置错误的产物：
旧路线图归档至 `docs/archive/`，归因复盘从 gitignore 的 `reports/`
迁入 `docs/reviews/`（纳入版本库），归因脚本从临时目录迁入 `scripts/`。

---

## 二、Phase R1 · v2.0.0 可信日报（第 1–2 周）

**交付物：自己每天真读的那份 A 股日报，每个数字带徽章。**

### R1.0 知识层上线（前置，来自归因复盘 P0a）✅ 2026-09-16

归因复盘已证明：`corporate_event` 0 行 → 停牌抑制规则在生产是死代码 →
7/7 真实告警全是误报。徽章的"✓ 已核验"依赖这层知识，必须最先修。

- `daily_pipeline.py` 常态化带事件采集（`--events`），失败不阻塞行情巡检
- 历史种子：4 笔已核实停牌写入 `corporate_event`（kind=suspension，
  公告链接入 detail；来源见归因复盘）
- 日报新增"知识层状态"行（事件表行数/最新事件日期）——抑制能力降级必须可见
- 验收：
  - [x] 重跑 2026-09-15 巡检，7 条真实告警 ≥4 条转抑制
        （漂移 3 条由 R1.1 截面共动规则处理，可后置）
        ——实测：种子落地后 4 条 missing_rows 全部转 `benign_suspension`（7 告警 → 3）
  - [x] 新增测试：corporate_event 为空时日报出现降级提示
        （`tests/test_report.py::test_empty_knowledge_layer_shows_degradation`；
        种子契约另见 `tests/test_seeds.py`）

### R1.2 逐指标徽章层 ✅ 2026-09-16

- `findings → (table, metric) → 徽章` 映射：探针按表/列产出，
  归因给出根因与证据，映射层聚合成每个指标一个徽章
- 新增根因 `BENIGN_MARKET_EVENT`：drift 归因先查截面共动性
  （同窗全市场放量分布 + `index_daily` 指数量能），共动显著判合法
  ——可抑制 2024-09 等真实行情误报（见归因复盘 P0b）
- 真实回放 golden：7 条真实告警用 `Snapshot.as_of()` 固化为评测用例
  进 CI 门禁（P0c）——合成语料证明机制对，真实回放证明事实够
- 验收：
  - [x] 日报（md/html）每个数字带徽章，徽章可展开证据链
        （12 项列徽章 + `<details>` 证据链；报告 Agent 解读逐数字带徽章，
        样例 `examples/daily-badge-report-2026-09-15.md`）
  - [x] 健康分由徽章聚合得出，口径写入文档
        （`docs/badge.md` + `dq/badges.py`；真实回放健康分 72.0 → 95.8）
  - [x] 真实回放 golden 进 `eval_golden.py --strict`，CI 挂红
        （`eval/golden/replay.yaml` 9/9 通过：4×停牌 + 4×市场行情抑制，
        唯一残留 2023-03 中芯国际孤立事件按复盘保留为已知误报）

### R1.3 报告 Agent（多智能体）✅ 2026-09-16

- 复用 Supervisor 编排，扩展节点：分析（指标解读）→ 可视化（图表
  选择与生成）→ 可信审校（核对每个引用数字的徽章，无徽章不得引用）
- 简单任务直通快路径（沿用 v0.8 结论）；单 vs 多 token/延迟如实归档
- 验收：
  - [x] `daily_pipeline.py` 一条命令出带徽章的 md+html 报告并推送
        （实测：真实 LLM 分析 710/472 字符、审校 0 拦截、SVG 图表；
        无 key/LLM 失败自动降级确定性模板，管线不因 Agent 失败中断）
  - [x] 审校节点单测：引用无徽章数字必须被拦截
        （`tests/test_report_agent.py`：裸数字/幽灵引用/✗ 徽章引用三条拦截路径
        + 日期与整数计数放行 + 模板零拦截自洽）
  - [x] 全部测试绿 + ruff 无告警（331 例绿，ruff clean）

---

## 三、Phase R2 · v2.1.0 可信增强模块（2026-09-17 维护者决策：主线重定向）

> **定位修订**：从头做通用 Agent 基座是重造轮子，findata 的正确位置是
> **挂到成熟 Agent 项目上的"可信增强模块"**——宿主负责对话与编排，
> findata 负责回答「这个数字能不能引用、凭什么」。原 R2 的通用包
> （trust-report / 任意数据接入）**延后**到 R3 之后；原 R3 的引擎 API
> 提前为本阶段。判断依据：对话编排已是充分竞争的成熟能力，而
> 归因证据链 + 无徽章不引用闸门 + 评测门禁方法论是 findata 独有的
> 增量；接入面做成标准协议（MCP/HTTP）后，宿主换成谁都不影响增量价值。
> 拒绝的路线：fork/焊接进某个具体平台（如 WrenAI）——那会把独有
> 增量绑死在别人的发版节奏上；对平台型宿主用 HTTP 面做松耦合门禁即可。

**交付物：外部 Agent 引用任何数字前，都有一条标准协议可以查到
「这个数字带什么徽章、证据是什么」。**

- MCP 工具：`findata_trust_check(table, metric)` + `findata_trust_board()`
  （挂在现有 `findata-mcp` stdio 服务上，任何 MCP host 零配置接入）
- HTTP API：`GET /v1/trust` + `GET /v1/trust/board`（给只能调 REST 的
  GenBI/平台型宿主做松耦合门禁）
- 可嵌入徽章芯片：`badge_chip_html`（内联样式，宿主报告数字旁直接贴）
- 集成契约文档：宿主 Agent 的三条接入纪律（会话拉全板 / 引用前检查 /
  未知键自愈），参照实现是 findata 自己的审校节点
- 验收：
  - [x] MCP + HTTP + Python 三个接入面同一份 service 实现，响应同构
        （实测归档 `examples/trust-module-integration.txt`）
  - [x] 真实仓库实测：close=✓ 已核验（停牌证据链）、volume=⚠️ 仅借鉴、
        健康分 95.8，未知指标错误带可用键
  - [x] 测试覆盖三面（MCP 4 例 / API 3 例 / 芯片转义 1 例）
  - [x] 宿主集成演示：一个真实的外部 Agent（非 findata 自己的）通过
        MCP 消费 trust_check 并在回答中带徽章引用
        （`scripts/mcp_host_demo.py` = 只讲标准 MCP 协议的通用宿主——
        官方 MCP Python SDK stdio 客户端 + 宿主自己的 DeepSeek 配置，
        零 findata 导入；真实 LLM 实测：metric_query → trust_check →
        回答逐数字带验证状态/徽章/run_id，transcript 归档
        `examples/mcp-host-integration.txt`，线级协议测试
        `tests/test_mcp_host.py` 进 CI）

---

## 四、Phase R3 · v2.2.0 通用包与分发（原 R2 内容并入）✅ 2026-09-17

- **通用基线检查包**（原 R2 主体，延后至此）：完整性/唯一性/新鲜度/
  离群值/Schema 一致，声明式 schema 描述，`findata trust-report <csv|db|table>`；
  两个公开数据集 demo + 对比页（仅通用包 vs +领域包 的徽章差异）。
  含金量自检继续有效：若通用包报告连自己都觉得没有引用价值，
  说明领域包战略需要修正。
- 验收：
  - [x] 非金融数据集端到端出带徽章报告
        （`src/findata/generic/`：5 类通用探针 + 通用归因器 + 徽章聚合
        复用 `dq/badges` 单一实现；`findata-trust-report` CLI；
        demo① = UCI Beijing PM2.5 真实公开数据（新鲜度拦截 4917 天停更，
        `examples/generic-trust-report-air-quality.md`）；
        demo② = 脱敏电商订单合成数据（主键重复 ✗ + 组内金额水平迁移 ⚠️，
        `examples/generic-trust-report-ecommerce.md`））
  - [x] 对比页：同一数据集 仅通用包 vs +A 股领域包 的徽章差异
        （`scripts/compare_domain_vs_generic.py` → `examples/domain-vs-generic-comparison.md`：
        同一 stock_daily 2025 切片，通用包 60 分 / 8 列 ⚠️ / 11 信号无法归因
        vs 领域包 100 分 / 8 列 ✓ 已核验 / 2 信号停牌归因——
        「没查出毛病」与「证明可靠」的差值即产品本身，含金量自检通过）
  - [x] 诚实边界测试钉死：通用包永不出 BENIGN_*/✓ 已核验
        （`tests/test_generic.py::test_generic_never_verifies`）；
        过程中发现并修复徽章聚合的 P0 优先级 bug（⚠️ 曾可掩盖 ✗）
- [x] README/PITCH 按增强模块定位重写（README 687 → 230 行，三组成结构：
      增强模块/通用包/A股领域包；PITCH 简历描述、电梯演讲、Q1/Q2/Q4/Q5
      全部对齐 v2.2 口径——旧"撤回改动"故事补上了 v2.0 的后续：换证据源
      而不是换阈值）
- [x] 分发止损启动：计划落 `docs/distribution.md`（launch 定义 / 唯一指标 =
      可验证的陌生宿主集成 / 无遥测的人肉测量方式 / 30 天决策门 A 加领域包
      B 停线复盘 / 窗口期克制清单）。计时起点待 tag v2.2.0 后回填

---

## 四·续、Phase R4 · v3.0 Agent 训练数据的质检与过滤（当前主线）

**交付物：`findata-trust-filter` —— 丢进去一批 Agent 轨迹，吐出"哪些能
喂训练、哪些不能、为什么"，每一条判定带证据链。**

### R4.1 轨迹质检层（探针 + 正负样本判定）✅ 已落地

复用 `dq/badges.py` 的徽章实现（单一实现，不另起炉灶），落到轨迹上：

| 判定 | 含义 | 证据要求 |
| --- | --- | --- |
| ✓ 可用正样本 | 路径正确且结果正确 | 关键步骤清单 + 无降级/无重试风暴 |
| ⚠️ 需人工看 | 结果对但路径可疑（蒙对嫌疑） | 可疑步骤定位（如重试 3 次才成功） |
| ✗ 丢弃 | 根因为 agent 能力问题 | 根因分类 + 证据（原文/日志片段） |
| ⊘ 不算失败 | 根因为环境（沙箱超时/工具缺失） | 环境证据——**别把它当负样本，会浪费数据** |

探针 5 类（形态信号，不含判断）：`degrade` / `abort` / `retry_storm` /
`ungrounded_slot`（槽位在问句里无出处）/ `silent_mismatch`。

- 验收门禁（先写后做，逐条核对）：
  - [x] 48 条真实轨迹跑质检，逐条判定可复核（归档 `examples/agent-trace-report.txt`）
  - [x] loud / silent 两档严重度判定单测钉死（`tests/test_agentops.py`）
  - [x] **误报防线单测钉死**：`ungrounded_slot` 三道防线、`retry_storm`
        改为判「同名**且同参数」——第一版在真实语料上误报 53 条
  - [ ] **归因准确率 ≥ 90%** —— **未达成，刻意保留红灯**（见下）

> **关于那个未打勾的门禁（必须写清楚，不许偷偷划掉）**
> 早先用自造合成集测出 100%，换成从未参与调参的 held-out 集后掉到
> **63.6%**（`tool_missing` 0/20、`env_timeout` 0/20）。这 36 个百分点
> 就是"自己造的样本证明不了自己的规则"的量化代价：规则怎么写的，样本
> 就怎么长。因此 R4.2 的合成管线**整体作废**，改走真实数据路线，而**真实
> 数据没有根因标签，准确率无从计算**——所以这个门禁不是"过了"，是
> **在真实数据口径下无法测**。留着它当素材：面试时能讲清为什么测不了，
> 比报一个 100% 可信得多。

### R4.2 ~~合成管线~~ → **已作废，改走真实数据**

原计划"自己造根因已知的失败样本来测归因器"。**放弃，理由三条**：

1. **循环论证**：规则怎么写的，样本就怎么长。合成集 100% → held-out 63.6%
   已经把这个代价量化了，继续调只会让数字更好看、结论更不可信。
2. **用户明确要求**：真实数据优先，不要模拟或自造。
3. **真实数据已经够用**：`Salesforce/xlam-function-calling-60k` 是公开的
   tool-calling 语料，形状就是轨迹，规模 6 万条，不需要自己造。

`src/findata/agentops/synth.py` 与 `scripts/run_synth_eval.py` 已删除
（代码仍在 git 历史）。**无标签是生产环境的常态**，质检必须能在没有
golden 的前提下工作——`ungrounded_slot` 探针就是为这个设计的。

### R4.3 闭环工程化 ✅ 已落地（真实数据）

JD9 第 3 条原话：「将『数据合成 → 沙箱运行 → 自动评测 → 质量过滤』的
闭环流程进行脚本集成与 Pipeline 工具化」。

- [x] 一条命令跑完：`scripts/run_trust_filter.py --limit 20000`
- [x] 语料来自网上真实公开数据集（`adapters/xlam.py`，ModelScope +
      ijson 流式读 96MB），**零自造样本**
- [x] 结果：20000 条 / 31691 次调用，检出 354 条（1.77%），
      根因 `schema_contradiction` 349 + `step_error` 5
- [x] 过滤前后样本量如实入账，归档 `examples/trust-filter-report.txt`
      （含「诚实说明」：脏率不代表生产、0 命中的类别不算已验证）
- [x] **产物落盘**：`agentops/export.py` 按四档处置分文件流式写 JSONL
      （OpenAI `messages` + `tool_calls` 形状）+ `manifest.json`。
      判定挂在样本的 `findata` 字段一起走，不另存对照表——分开存会漂移，
      而这种错在训练跑完之前不会有任何报错

**最锋利的一条发现**：349 条"必填参数没传"经核查**全都不是 agent 漏填**，
而是工具 schema 的 description 写着"default is 8.854e-12"、schema 里却没
标 default。形态与"agent 能力问题"完全一致，结论相反。第一版就是把它
全判成了 `param_error`（改进建议：换更大的模型）——方向完全错。

### R4.4 沙箱执行环境 ✅ 已落地（**进程级，不是容器级**）

`src/findata/agentops/sandbox.py` + `scripts/run_sandbox_probe.py`。
真起子进程、真跑到阈值、真被杀，归因器拿到的是 `subprocess.TimeoutExpired`
的**原文**，不是在字符串里写个 "timeout"。

- [x] 三类真实信号 → 归因 → 处置（归档 `examples/sandbox-signals.txt`）：
      `add` → ok / ✓ 正样本；`divide` → param_error / ✗ 丢弃；
      `spin` → env_timeout / ⊘ 不算失败
- [x] 单测钉住「超时轨迹不算负样本」与「参数写错该丢」——**同一类失败，
      结论相反**，这条之前只是写在文档里的主张
- [x] Windows 上 `resource` 不存在 → 内存限制**标 `memory_limit_applied=False`**，
      不静默降级

**边界写在代码与报告里，不吹**：
- 这是**进程级隔离，不是安全沙箱**，防不住恶意代码。要那个得用容器，
  本机拉不到 `registry-1.docker.io`，做不到就不假装做了
- **超时是我们主动设阈值触发的**，不代表生产环境超时的频率
- **gRPC 那套 "deadline exceeded" 仍然识别不了**（归因器只认含 timeout
  的文本）。这是已知局限，不因为跑通了这一个就宣称覆盖

**踩过的坑**：第一版给所有工具统一设 0.5s，结果 `divide` 明明立刻抛异常
却被记成 timeout——**子进程冷启动就要 ~0.4s**。阈值必须留够启动余量。

### R4.5 材料改写（收尾）🚧 进行中

README / PITCH / AGENTS 主线切到 v3.0；A 股降为领域包 1。
**铁律**：PITCH 里的每个数字必须对应 `examples/` 可复现归档，不凭记忆写。

### R4.6 清理（2026-09-18 已完成）

用户指令「把该删掉的删一删」。删除依据只有一条：**不服务主赛道就删**。

| 删了什么 | 为什么 |
| --- | --- |
| `legacy/`、`findata-agent/` | 早期原型与本地笔记，从未入库 |
| `src/findata/rl/` · `scripts/rl_pipeline.py` · `docs/rl-experiment.md` · `tests/test_rl_*.py` | RL 环境封装无 GPU 训练，且 10 份 JD **无一条要 RL** |
| `src/findata/agentops/synth.py` · `scripts/run_synth_eval.py` | 自造样本，与"真实数据优先"直接冲突，且是循环论证源头 |
| `eval/rl_runs/` · `examples/rl-baseline.txt` · `examples/synth-*` | 上述模块的归档产物 |

代码全在 git 历史里，随时可 `git show` 找回——**删不是烧，是不再维护**。

---

## 五、诚实边界（本阶段明确不做）

- **不过度承诺"任意数据可信"**：v1 只承诺通用包基线 + 领域包核验，
  徽章必须如实区分"✓ 已核验"与"✓ 基线通过"
- **可信分只对已观测事实归因**，不做预测性声明（"这数据以后会坏"不归我们说）
- **可视化够用即可**：图表服务于报告可读性，不做花哨前端（沿用旧边界）
- **实盘 / 交易信号红线不变**
- **封存模块不投入**：RL 训练等有卡；eval-gate 独立产品线封存
  （其判官校准思想在 R1.3 审校节点中以"无徽章不引用"的形式落地）

---

## 六、与 mm-curation-pipeline 的协同

架构模式同构、处理对象不同，**协同设计 schema，不合并代码库**：

| 模式 | mm-curation | 本项目 v2.0 |
| --- | --- | --- |
| 通用引擎 + 领域配置 | 域画像（threshold 画像自校准） | 领域包（A股包首发） |
| 检查器注册表 + 误杀审计 | operators + threshold_scan + dropped.jsonl | probes + triage + 归因复盘 |
| 判定 + 门禁 | 域判官 + eval 门禁 | 徽章 + eval-gate |
| 运维飞轮 | stats.jsonl 每日巡检 | daily pipeline + 真实回放 golden |

已有桥接：mm-curation 的 `scripts/findata_health_stage.py` 直接 import
findata 做仓库级健康巡检。R2 定领域包 schema 时与 mm-curation 的域画像
schema 协同设计（一份跨项目规范，两边各自实现），采样级清洗 → 表级可信
→ 报告消费的三段生态叙事因此更完整。

---

## 七、版本节奏与投递衔接

| 版本 | 内容 | 验收核心 | 前置 |
| --- | --- | --- | --- |
| v2.0.0 | 知识层上线 + 徽章层 + 报告 Agent | 真实日报每数字带徽章；7 条真实告警抑制 ≥6；CI 绿 | 无 |
| v2.1.0 | **可信增强模块**（2026-09-17 重定向）：MCP trust_check / HTTP /v1/trust / 徽章芯片 + 集成契约 | 三接入面同源实测 + 真实宿主集成演示 | R1 |
| v2.2.0 | 通用包（原 R2）+ 分发与止损 | 非金融数据端到端报告 + 对比页；陌生宿主调用 | R2 |

| JD 关键词 | 对应里程碑 | 证据位置 |
| --- | --- | --- |
| 数据智能体 / Data Agent | R1–R2 | 带徽章的可信报告 + examples 归档 |
| 多智能体协作 | R1.3（复用 v0.8） | 单 vs 多对比报告 |
| 数据质量 / 可信 AI | R1–R3 全程 | 归因复盘 + 徽章证据链 |
| 评测系统 / 质量门禁 | 已有 + 真实回放 golden | CI + eval-gate |
| LLM 归因 / 记忆 / 自进化 | v0.9（封存保留） | `examples/eval-prompt-evolution.txt` 等 |

每完成一个 Phase 的固定动作：CHANGELOG 记录 → 本文档打勾 →
`examples/` 归档评测报告 → PITCH.md 增补对应数字。
