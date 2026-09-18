# FinData-Agent 项目长期笔记

## v3.0 主赛道（2026-09-18 定调，**以此为准**）

**Agent 训练数据的质检与过滤。** 丢一批 Agent 轨迹进去，吐出「哪些能喂
训练、哪些不能、为什么」，每条判定带证据链。一句话命令：
`uv run python scripts/run_trust_filter.py --limit 20000`。

- 依据：`docs/jd-coverage.md`——10 份目标 JD 里 C 类（Agent/LLM）占 5 席，
  而「Agent 轨迹 / Tool Calling 失败归因」此前为零
- 核心命题不变：形态相同、结论相反。数据域（停牌 vs 漏采）→ 轨迹域
  （schema 自相矛盾 vs agent 漏填；沙箱超时 vs 能力不足；蒙对 vs 真成功）
- 语料**必须真实**：`Salesforce/xlam-function-calling-60k`（ModelScope，
  ijson 流式）。自造样本是循环论证（合成集 100% → held-out 63.6%）
- A 股降为领域包 1；`generic/`（任意表出徽章报告）因对应「数据质量 4/10」
  关键词保留
- 已删：legacy/、findata-agent/、findata/rl/、agentops/synth.py（git 历史可找回）

## 项目定位（2026-09-06 定调）

面向实习求职的 AI Agent 项目。核心诉求：**体现业务思考与架构能力，不是炫技 demo**。

### 最终目标与基座策略（2026-09-17 用户澄清）

用户的长期目标不是 findata 本身，而是**对话式 BI / 自动报告 Agent（WrenAI 型）
或数据清洗预处理 Agent（ai-data-science-team 型）**。findata 的「数据可信」是
**切入点与差异化**，最终要挂到通用基座上（接现成 or 自写）。这与 v2.1 已定的
「增强模块、不做基座」一致——不是转向，是要补基座的可见性。

外部事实（2026-09-17 核实）：
- **WrenAI**：已重构为「GenBI engine + open context layer」，Apache-2.0，
  16.1k stars，22+ 数据源；提供 MCP server / Agent SDK（LangChain·Pydantic AI）/
  `npx skills add` 三种集成路径。其 Context Layer 五层 = 结构·语义·业务·运营·
  行为，**没有「数据现在健不健康」这一层** → findata 的精确缺口位
- **ai-data-science-team**（business-science，MIT，4.6k stars，pre-0.1.0 Beta）：
  LangGraph 多智能体 + Streamlit Pipeline Studio，主打 lineage 与代码可复现；
  Data Cleaning Agent 会自动 fillna / 处理异常值 → findata 可作**清洗前可信
  门禁**（防止把停牌这类合法缺失清洗成假数据）

判断：基座只做**演示载体**不做主线（补完 WrenAI 需数月且必被比下去）；主角永远
是增强包。接宿主属于**分发动作**，与 30 天窗口期「不加新功能」不冲突。

### 主线：查询时数据可信（Query-Time Data Trust）

数据分析 Agent 价值链分三段，前两段已是红海：
1. 口径语义对齐 — WrenAI / SuperSonic / DB-GPT（成熟）
2. SQL 生成与执行 — Vanna / Dataherald / XiYan（卷准确率）
3. **数据健康核验 — 空白**。Monte Carlo / Bigeye 只做独立监控，不与查询链路联动

错位切入：把「数据健康核验」接进查询链路。语义层保证口径一致，健康层保证数据本身可用，
两者交集才是「这个数字能不能拿去汇报」。

### 产品化目标

不是「问数机器人」，而是**分析结论的质检网关**。
- 目标用户：数据分析师 / 业务分析师（不是炫给老板看）
- 核心指标：cost per trusted answer、拒答率、核验拦截率（不是 accuracy）
- 关键行为：**证据不足时拒答，而不是猜**

### 垂直域：A 股金融（继续沿用，不换）

理由不是"有现成数据"，而是金融数据天然自带丰富的数据质量场景，与主线高度同构：
- 停牌 → 合法缺失（不是脏数据，但会被误判）
- 除权除息 → 复权口径变化（前复权/后复权数字不同）
- 财报修正 → 历史数据被改写
- 新股上市 → 历史长度不足
- 新浪 / 东财口径差异 → 交叉核验天然有第二条独立路径

### 架构分层

接入层（FastAPI+SSE / 溯源 UI / CLI）
→ 编排层（LangGraph 四角色：Planner / Analyst / Auditor / Reporter）
→ **可信层（差异化核心）：语义编译器 / 交叉核验 / 数据健康 / 引用绑定**
→ 工具层（MCP Server：metric_query / health_check / validate）
→ 数据层（DuckDB / 域插件 / 血缘 run_id）
横切：评测门禁（golden set + LLM-as-judge + CI）+ Tracing（OpenTelemetry）

### 里程碑（已重置方向）

用户把方向从"查询时数据可信"改为**纯数据质量监控 Agent**，并要求 eval 先行。

- [x] M1 数据层：DuckDB + akshare 采集（幂等/增量/血缘）
- [x] **M2 质量内核**：7 类探针 + 规则归因器 + 3 类评测指标 + 确定性故障语料
  + **M2 增量**：指标字典 YAML（11 个 metric 元数据：7 probe + 4 SQL-based
  (row_count / null_rate / distinct_symbol / freshness_sql)）+ 确定性 SQL
  编译器（$name 占位 → ? + params 列表，同名多次按次数复制）+ MCP 工具层
  (`findata_list_metrics` / `findata_execute_metric` 2 个新 tool)
- [x] M3 产品化：巡检流水线 + 告警路由/聚合/健康分 + Markdown/HTML 报告
- [x] M4 Agent 层：LLM 归因器（Triage 协议 + 白名单校验 + 逐条降级 + 可观测统计）
- [x] M5 看板：静态 HTML 看板（回放 + 趋势 + 朴素基线对照 + tab 切换）
- [x] **M6 部署**：`findata.service` 高层服务层 + FastAPI（5 路由）+ MCP stdio（5 工具：health_check/dashboard/validate/list_metrics/execute_metric）
  + docker-compose 一键 + 单页演示站（同源：HTTP/MCP 共用 service 函数避免漂移）
- [x] **M7 CI**：GitHub Actions（lint + pytest 3.11/3.12 + 真实 serve e2e smoke + ci-gate 聚合）
  + ghcr.io 仅 tag 推镜像（multi-platform amd64/arm64，metadata-action 自动算 tags，
  RC 不污染 :latest） + `.dockerignore` 防 .venv/build cache/credentials 进镜像
  + 推送到 `github.com/quannie255-star/FinData-Agent.git`（master 分支）
- [x] **M8 可观测层**：`findata.observability` 子包 + OTel 默认 InMemorySpanExporter
  + service 层 4 入口埋点（inspect/render_report/build_dashboard/evaluate）写关键
  attribute（health_score / n_alerts / κ） + `GET /v1/traces` 端点按 end_time desc
  返最近 span。不引入 OTLP collector，演示站 5KB 起得动；后续接 Tempo/Jaeger 只
  换 provider。
- [x] **M10 发布链路收尾（v0.5.0，2026-09-07 全绿）**：镜像**不在容器内 build
  project**（slim 内 PEP 517 build 要现场拉 editables/setuptools，曾连挂 5 轮）→
  改为 `uv sync --no-dev --no-install-project` 装依赖 + 只拷 site-packages +
  `/app/src` + `PYTHONPATH` 直跑；release 拆两步「单架构 --load 起容器冒烟 →
  通过才 multi-arch push」，坏镜像不进 registry。`ghcr.io/quannie255-star/
  findata-agent:0.5.0|0.5|latest`（amd64+arm64，匿名可拉）。

**项目方向定调（2026-09-07 18:00）**
GLM 那边描述的 M2-M6 是「查询时可信 Agent」（指标字典 YAML + LangGraph 编排
+ 引用溯源 + 置信标注 + golden set 轨迹评估）。与本机已实现的「纯数据质量
监控」路径不冲突——用户拍板"统一走我方向"。M2 描述被我重新解读为
"7 个现有探针 + 4 个 SQL-based 新指标 + YAML 元数据 + 确定性 SQL 编译器 +
MCP 工具层"（指标字典/编译器/工具层 M2 三件套 + 已存在的探针/归因/告警/报告）。
M3 起的 GLM 路径产物（LangGraph / SSE / 引用溯源 / 置信）我未对应实施——
如果未来要做"查询时可信"叠加，按 GLM 表的描述顺次在 findata.agent/
findata.trust/ 添新子包。

**当前主线一句话**：在形态相同、结论相反的事实之间（停牌 vs 上游停更）做出区分。
归因层是产品生死线（不做归因，精确率只有 61.5%；做了就 100%）。

**已知边界（真实数据路径，2026-09-07 实测）**
- CI 只跑合成语料 → 真实路径 bug 进不了门禁。改数据层/探针后**必须真跑一次
  `run_inspection.py --source warehouse`**（已因此抓到 calendar 的
  Timestamp/date 类型混用 crash，合成语料下永不触发）
- 硬编码节假日只覆盖 2024-2025 → 2022 春节被误报 missing_rows。春节所有标的
  同时休市，`with_observed` 推不出来，需接 `ak.tool_trade_date_hist_sina`
- **`corporate_event` 真实路径是空表**（有 schema + 消费方，**无采集代码**）→
  停牌/除权抑制在真实数据上没有数据源，报告"抑制 0"。合成语料的抑制率是设计
  出来的对照。下一步用 `ak.stock_history_dividend` 先接除权除息

**ready for JD 投递的可验证锚点**
- 评测门禁从口头承诺 → CI 自动化（test.yml 三关 + ci-gate 聚合）
- 服务外部化：HTTP API（5 路由）+ MCP stdio（3 工具）双路径，同一份 service 函数（杜绝 UI/Agent 漂移）
- 镜像可分发：ghcr.io/<owner>/findata:tag，多架构，仅 stable tag 推 :latest
- 演示站 5KB 一键起：开 `/` 即见 live dashboard iframe + 健康分实时数
- 协作可视化：三项目联动（B1 Cohen's κ + B2 findata_health_stage + C 三 README 互链）落地

## 目标 JD 技能覆盖（2026 实习岗调研）

必写项：Python / FastAPI / LangGraph / RAG+向量库 / Function Calling / Prompt / Git / Docker
2026 新增高频：**MCP 协议**、A2A、Agent Skills、上下文与记忆、多智能体
拉开差距项（多数候选人没有）：**评测 Eval**、**可观测 Tracing**、CI 门禁
反复强调的软性项：产品闭环思维、业务理解、"不局限于被动接收需求"

→ 因此评测与可观测不能省，这是面试时能甩开 90% 候选人的地方。

## 项目铁律补充（2026-09-18，R4.6 自查后固化）

**处置档位是五档不是四档**（`agentops/triage.py`）：
`✓ 正样本 / ⚠️ 需人工 / ✗ 丢弃 / ⊘ 不算失败 / 🔧 待修 schema`。
第五档的意义：**根因在数据源，处置就作用在数据源上**——修 18 个工具的
18 个参数 schema 后重跑，349 条样本全回来，一条都不用丢。

**处置选择的判据（重要，别简化成"可疑就丢"）**：
不只看"有多可疑"，还看 **①能不能给出可执行动作 ②队列长度**。
5 条 → 人工队列是队列；349 条 → 人工逐条看等于没有队列，必须结构性修复。
同一条规矩，两个方向都成立。

**措辞纪律（写代码写文档都算）**：
- 「命中率」不是「发现率」；**命中数不等于错误数**
- 「未命中」不是「干净」；**未命中 ≠ 确认正确**（无执行反馈时"正确"观测不到）
- 变量名也是措辞（`n_bad` → `n_hit`）
- 口径写进产物本身（`manifest.note`），别只活在文档里

**文档一致性门禁**（`tests/test_docs_consistency.py`，3 例）：
① 各文档对当前测试数只能有一个值；② **必须等于 `pytest --collect-only`
的真实收集数**。第 ② 条不能省——文档可以一起写错（实测 354 这个错数在
6 处共存一个月）。`docs/interview-qa.md` **刻意不在扫描名单里**（它要引用
历史错数字讲漂移史），代价是它自己的数不受门禁保护。

**可复现三元组**（`manifest.reproducibility`）：`code_commit` +
`rules_version`（改判定逻辑必须动 `triage.RULES_VERSION`）+ `data` 指纹
（sha256 前 16 位 + 字节数）。公开数据集会被作者修订，没有数据指纹就复现不了。

**权威文档分工**：`docs/interview-qa.md` = 拷问作答（五个红旗 + 不自证清单）；
`docs/ROADMAP.md` = 主线计划；`docs/jd-coverage.md` = JD 差距与洞；
`AGENTS.md` = 会话交接 + 封存模块表。**动主线逻辑前先看 interview-qa**，
里面记着"凭想象设口径"是怎么造成误杀 349 条的。

**面试时最该准备的三个"承认"**（比报好数字有用）：
① 人工一致性 κ **没做**（只有机器 vs 机器的 κ=+0.7417，n=13，不能替代）；
② 超时归因**只有一个信号**，且**刻意不修**（本批 0 命中＝未验证，
此时改判据就是重犯"凭想象设口径"）；
③ held-out 63.6% vs 门禁 ≥0.9 的**红灯不划掉**——"能讲清为什么测不了，
比报一个 100% 可信得多"。

---

## 第二轮拷问补的三条铁律（2026-09-18 晚）

**① 证据强度要分层：「值缺失」与「值放错位置」差一个量级。**
`permitivity` 描述称默认值 8.854e-12、自己没 `default`，而该值**标在 int 型
`charge`/`distance` 上**（真空介电常数不可能是电荷量的默认值）。
→ 不是"漏标"（可用疏忽解释），是"错位"（疏忽解释不通）。
**分层直接决定处置动作**：漏了→补上；放错了→移动。统一成"补 default"
会让数据源方两边都标，改得更错。全量这类只有 2 个参数位
（`calculate_electric_field.permitivity` / `mean_confidence_interval.confidence`）。

**② 两种解释都成立时，选可逆的动作，别去打解释之争。**
`required` 与 description 冲突谁优先？→ **不需要赢**：两种读法下处置都落在
"改 schema"上，都不是丢样本。判据 = **丢样本不可逆、改 schema 可逆**。
配套算术：1 处 schema 缺陷影响 258 条样本，修 1 处消掉 258 条歧义；
丢 258 条样本什么都解决不了。
推论：**处置的风险等级应由证据强度决定，不该按类别一刀切**
（"参数错→丢弃"在"除零"这类多成因上 overshoot，应降级为交人工）。

**③ 报数之前先问：这个"冲突"是不是数据源的表示层约定。**
类型/默认值冲突探针全量 2162 命中，查完发现该数据源 **89.1% 的 `default`
本就是字符串**（`type: int` + `default: "10"`）——是方言不是矛盾。
**探针当场删除**。判据：命中被一两个重复形态主导 → 先抽 3 条读原文。
**一个从不删规则的质检系统，指标只可能虚高。**

**口径变更**：主闭环**默认全量 6 万条**（`--limit 0`，16 秒）——
抽样比例与全量不是一回事，而手工核证都在全量上做。
当前：60000 条 / 100011 次调用 / 命中 1351（2.25%）/ A 65 / B 1286 /
21 个工具 21 个参数 / 364 例测试。

**已识别的最大可扩展性缺陷**：**3605 个工具名里 296 个（8.2%）有多份互不
相同的定义**（`search` 77 种；`time_series` 一个名字对应 4 个不同 API）。
容错策略"任一份 schema 能解释就放行"（不加会误报 45 条）在百万级会
**系统性少报**。缺陷清单的 `(tool, param)` key 在同名多版本下有歧义
（本批核过 0/21，**是运气不是实现保证**）。

**权威文档分工更新**：`docs/interview-qa-round2.md` = 第二轮清单作答
（**当前口径以此为准**，含"规则脚本 vs 质检系统"四条分界线 + 自我暴露 6 处）；
`docs/interview-qa.md` 保留原文与历史数字（记的是那次自查的真实状态）。
两份拷问文档都**刻意不在**文档一致性门禁的扫描名单里。
