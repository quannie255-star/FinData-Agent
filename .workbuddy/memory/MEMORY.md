# FinData-Agent 项目长期笔记

## 项目定位（2026-09-06 定调）

面向实习求职的 AI Agent 项目。核心诉求：**体现业务思考与架构能力，不是炫技 demo**。

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
