# Findata — 一个项目，两套互补能力

[![test](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml/badge.svg)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)
[![release](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml/badge.svg)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![镜像](https://img.shields.io/badge/ghcr.io-v0.7.0-green)

> 一句话：**把"A 股数据到底可不可信"和"问它一个问题"放进同一个仓库——前者保证后者答得出来、而且答得对。**

Findata 是一个面向 A 股金融数据的智能体，包含**两套互补能力**，共享同一个 DuckDB 数据底座：

| 能力 | 子包 | 解决什么问题 |
| --- | --- | --- |
| **数据质量监控** | `dq/` `eval/` `report/` `observability/` | 数据仓库巡检：发现异常 → 根因归因 → 抑制合法业务事件造成的误报 → 产出可行动告警与评测门禁 |
| **可信问数分析** | `semantic/` `agent/` | 自然语言问数：LLM 选指标＋填参 → 确定性 SQL 编译 → 自动交叉验证 → 每个数字带 `run_id` 溯源 |

## 为什么是"两套互补"而不是"两个项目"

做问数（Text2SQL）的开源方案已经挤满（Vanna / WrenAI / DB-GPT / DeepBI），但它们全部默认一件事：**喂进去的数据是好的**。真实生产里这个前提不成立——上游停更、字段空值率暴涨、口径漂移、多源不一致每天都在发生。

而真正拦住数据质量工具的，也不是"检测不到"，是**告警噪音**：

- 停牌 → 没有行情（合法，不该告警）
- 除权除息 → 价格跳变（合法，不该告警）
- 新股上市 → 历史长度不足（合法，不该告警）
- 上游采集失败 → 没有行情（**故障，必须告警**）

前三种和最后一种**在数据形态上完全一样**。只会检测的工具必然淹没人，而淹没有人的工具没人用。

所以本项目把两件常被拆成两个仓库的事，刻意放在**同一个仓库的同一个数据底座上**：

- **监控层**负责"数据可不可信"——它巡检仓库、归因、抑制误报，让仓库里的数字值得被问。
- **分析层**负责"问它一个问题"——它在已经被监控保证过质量的仓库上，用确定性 SQL 编译＋交叉验证，保证每个答案都可溯源、可复核。

两层共享 M1 的 DuckDB 仓库与采集链路（`core/db.py` / `domains/finance/ingest.py`），互不重复实现数据接入；监控层产出的 `corporate_event` / `trading_calendar` 同时也是分析层可信度的底气来源。一个负责"数据干净"，一个负责"问得准"，**监控给分析背书，分析让监控有用**——这就是"互补"而非"并列"的含义。

---

# 能力一：数据质量监控（dq / eval / report / observability）

**Findata 监控层**的核心命题是：在形态相同、结论相反的事实之间做出区分。这既是技术难点，也是产品的生死线。

## 为什么是这个方向

（详见上文"为什么是两套互补"。）监控层的设计原则：**探测与判断分离**——探针只回答"观测到了什么"，不回答"是不是问题"；归因层独占判断权，因此抑制逻辑可单测、可评测、可替换成 LLM。

## 架构（监控层）

```
产品层    巡检报告(Markdown/HTML) · 告警推送 · 评测看板
─────────────────────────────────────────────────
归因层    Triage 协议（可替换实现）
          ├─ 规则归因器：确定性，CI 门禁，作为对比基线
          └─ LLM 归因器：白名单校验 + 逐条降级 + 全批兜底
─────────────────────────────────────────────────
探测层    新鲜度 · 完整性 · 唯一性 · 值域 · 漂移 · 空值扫荡 · 历史深度
─────────────────────────────────────────────────
知识层    交易日历 · 公司事件表 · 数据血缘(ingest_run) · 上市信息
─────────────────────────────────────────────────
数据层    DuckDB 仓库 + akshare 采集（幂等 upsert / 增量 / run_id 血缘）
─────────────────────────────────────────────────
评测层    确定性故障注入语料 · 三类指标 · CI 质量门禁
```

## 上下游生态

findata 不是孤岛。三个项目用**同一套"质量协议"串联**——采样级清洗、仓库级巡检、训练级指标，各管一段，互不重叠：

```
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ mm-curation-pipeline │    │   FinData-Agent      │    │   MLMetrics-TSDB     │
│ 样本级数据质量        │ →  │ 仓库级数据质量        │ →  │ 训练指标质量          │
│  (curation-eval)     │    │   (findata)          │    │   (mlmetrics)        │
│ 算子 / 漏斗 / 域判官 │    │ 探针 / 归因 / 告警   │    │ WAL / 时序 / 异常检测 │
└──────────────────────┘    └──────────────────────┘    └──────────────────────┘
```

**真实借用**：
- `cohen_kappa` 借自 mm-curation-pipeline 的 `curation-eval` 包——LLM 归因器 vs 规则归因器的一致性指标（修正随机一致），跑 `--triage both` 时输出
- `pr_from_drops` 的语义等价于 findata 的告警精确率——`drop=你认为是问题的信号`，`dirty=真实故障标注`。同一语义，findata 写在评测内核里更直接
- `Triage` 协议与 `LLMTriage` 框架（白名单校验 + 逐条降级 + 全批兜底）可以被任何"判定"类项目复用

**真实边界（不接的）**：
- `Contaminator` 借不到——它是样本级接口（sample.id + image/text），findata 的故障是表级（symbol + date + column），硬接要写厚适配层。详见 `src/findata/eval/_interop.py`
- `Operator` / `BatchOperator` 同上——单样本签名 vs 全表上下文签名

**真实联动**：
mm-curation-pipeline 的 `scripts/findata_health_stage.py` 把 findata 当作外部模块 import，在清洗流水线末尾加一道"仓库级健康巡检"，并把 mm-curation 的产物摘要写进 findata 报告头部。

## 核心契约

```python
Finding   # 探针产出：观测事实（指标值、阈值、证据字典），不含判断
Diagnosis # 归因产出：根因标签 + 严重度 + 是否抑制 + 人类可读解释
```

根因标签：`UPSTREAM_STALE`(上游停更) · `UPSTREAM_MISSING` · `SCHEMA_DRIFT`(单位/口径漂移) ·
`UPSTREAM_DUP` · `SOURCE_DIVERGENCE`(多源不一致) / 合法事件：`SUSPENSION` · `EX_DIVIDEND` · `NEW_LISTING` · `CORPORATE_ACTION`

## 评测（eval 先行）

语料是**确定性合成 + 故障注入**的，不依赖实时接口——否则 CI 门禁会随机失败。每份语料自带标注：注入的真实故障（期望被召回并正确归因）和内置的合法事件（期望被抑制）。

```bash
uv run python scripts/run_eval.py --seed 20240102
```

| 指标 | 含义 | 当前 |
|---|---|---|
| 故障召回率 | 注入的故障是否都被发现 | 100% (7/7) |
| 根因准确率 | 根因标签是否与标注一致 | 100% (7/7) |
| 分级准确率 | P0/P1/P2 定档是否正确 | 100% (7/7) |
| 误报抑制率 | 合法事件是否被正确静音 | 100% (5/5) |
| **告警精确率** | 收到的告警里有多少是真问题 | **100%**（朴素基线 61.5%） |
| 干净数据误报 | 无故障语料上的告警数 | **0**（5 个信号全部抑制，健康分 100） |

最后一行是项目最想证明的数字：**不做归因、见到异常就告警，精确率只有 61.5%；加上归因层后是 100%。** 归因不是锦上添花，是产品能否被使用的分水岭。

倒数第二行同样重要：**在健康数据上必须一声不响。** 一个会在干净数据上乱叫的工具，它所有的告警都会失去可信度。

评测同时充当回归门禁：`--seed` 可切换语料，多种子下指标稳定，任何回归都会被立刻发现。

## LLM 归因器

规则覆盖不到的长尾交给模型，但**前提是它不能让系统变差**：

```bash
uv run python scripts/run_eval.py --triage both   # 规则 vs 模型，同台用同一把尺子量
```

`LLMTriage` 与 `BaselineTriage` 实现同一个 `Triage` 协议，吃同一份证据（`build_evidence`），
因此两者的差异只可能来自**推理**，不会来自"看到的东西不一样"。

三条硬性约束：

1. **白名单校验** — 根因与级别只能取受控枚举值，模型编造的标签直接拒收
2. **逐条降级** — 某条不合法就只降级那一条，其余合法结论不连坐
3. **全批兜底** — 无 key / 超时 / 限流 / 返回非 JSON，整批退回规则结论，巡检照常出报告

第 3 条已固化为测试：**模型完全不可用时，评测指标必须与纯规则逐项相等，不得更低。**
一个会让系统变糟的 LLM 改动，本来就不该上线。

归因过程可观测：`TriageStats` 记录模型实际判定了多少条、降级多少条、拒收多少条非法输出——
"接了大模型"不等于"大模型在干活"，这个数字能区分两者。

## 每日巡检

```bash
uv run python scripts/run_inspection.py                     # 零数据可跑（合成语料演示）
uv run python scripts/run_inspection.py --source warehouse  # 巡检真实 DuckDB 仓库
uv run python scripts/run_inspection.py --format html --open
```

产物在 `reports/`：Markdown 给值班的人看，HTML 给演示和归档。

流水线在归因之后还多做三步，它们决定这是"产品"还是"检测脚本"：

- **告警路由** — P0 推送值班群并阻断下游，P1 进当日工单，P2 并入周报
- **告警聚合** — 同一标的同一根因只占一行（上游停更会同时触发新鲜度和完整性两个探针，不合并就是一次故障刷两行）
- **健康分** — P0 扣 12 / P1 扣 4 / P2 扣 1，一眼看出仓库今天能不能用

## 评测看板

```bash
uv run python scripts/build_dashboard.py --days 30 --with-eval
# 产出 reports/dashboard-YYYY-MM-DD.html（开盖即用、零依赖、零 CDN）
```

一个静态 HTML 看板，三栏布局：

- **健康分趋势** — 内联 SVG 折线图，N 天回放，颜色编码 P0/P1/P2 红线
- **系统自评** — 把"告警精确率 100% / 朴素基线 61.5%"写成一句人话、附朴素基线对照口径，避免被当成自吹自擂
- **告警历史 / 根因分布 / 评测指标** — tab 切换，离线双击即可打开

为什么做静态 HTML 而不是 FastAPI：clone 仓库的人不应该为了看个演示先起一个服务——所有数据都已经在文件里，HTML 一开就能用。事故复盘时也是，邮件附件发出去就行。

---

# 能力二：可信问数分析（semantic / agent）

> 监控层回答"数据可不可信"，分析层回答"那问它一个问题"——但它**绝不**像普通 Text2SQL 那样让 LLM 直接写 SQL。

**核心设计**：LLM 只负责"选指标 + 填参数"，SQL 由**语义层确定性编译器**渲染（`:name` 占位符 → 参数绑定），每次查询自动跑第二条**独立口径**做交叉验证，所有数字都带 `run_id` 落 `query_run` / `verify_run` 血缘表。`/trace/{run_id}` 可回追 SQL、参数与验证结论。

为什么这么做：Text2SQL 的最大风险不是"算错"，是"它编了一个像那么回事的数字"。findata 的分析层把"生成 SQL"这项最不可控的事拿走，换成"从受控指标字典里选一个 + 编译器填参"——SQL 永远来自人类审核过的模板，LLM 的发挥空间被锁死在"选哪个、填什么值"上。

## 语义层（semantic）

指标字典 `src/findata/semantic/metrics.yaml` 定义每个指标的 SQL 模板、参数 schema、单位与验证口径：

| 指标 | 含义 | 单位 | 交叉验证 |
| --- | --- | --- | --- |
| `close_price` | 最新收盘价（前复权） | 元 | 排序取最新 vs `MAX(date)` 等值匹配 |
| `pct_change` | 区间累计涨跌幅 | % | 首尾比值 vs 区间切两半独立相乘 |
| `valuation_pe_ttm` | 最新 PE-TTM | 倍 | 排序取最新 vs `MAX(date)` 等值匹配 |

三个文件构成确定性链路：

- `catalog.py` — 加载并校验 YAML 指标字典（写错在启动时即报错）
- `compiler.py` — 把 `:name` 模板 + 参数确定性渲染成 `?` 绑定 SQL，编译期只允许来自 `params` 的键
- `executor.py` — 执行主路径、写 `query_run`、调用验证器
- `verifier.py` — 用第二条独立口径重算，产出 `VERIFIED` / `MISMATCH` / `NOT_VERIFIABLE`，写 `verify_run`

验证结论在答案里强制保留标记：`✓ 已交叉验证` / `⚠ 交叉验证不一致` / `○ 未交叉验证`。

## 问数 Agent（agent，LangGraph）

`agent/graph.py` 是一个**有回环**的对话状态机——这正是监控层那条"确定性 DAG 不需要 LangGraph"判据的反面例子：问数需要"先理解意图 → 可能要查数 → 工具回填 → 组织答案"，存在运行时才能决定的分支与多轮工具调用，所以是 LangGraph 的正当用例。

```
用户问题
  → parse（LLM 理解意图，决定直接答 or 需要查数）
  → 条件路由：
        - 数据问题 → 调用 metric_query / list_metrics 工具
        - 非数据问题 → 直接 finalize
  → 工具结果回填
  → finalize（LLM 组织答案，数字必须带 run_id 溯源）
```

护栏：
- **工具白名单** `ALLOWED_TOOLS = {metric_query, list_metrics}`——Agent 只能调这两个语义层工具，绝不能直接碰 SQL 或表
- **system prompt 硬约束**——数据类问题必须调工具，不得凭记忆编数字
- **run_id 强制携带**——最终答案里每个数字后保留方括号 `run_id`，供上层溯源

## 分析链的"可信"对外接口

| 端点 | 说明 |
| --- | --- |
| `GET /metrics` | 列出语义层所有可用指标 |
| `POST /metric` | 直查指标：确定性 SQL 编译＋参数绑定＋交叉验证，返回带 `run_id` 的结果 |
| `POST /agent` | 自然语言问答（LangGraph Agent）：选指标 → 填参 → 确定性查询 → 验证 |
| `POST /agent/stream` | 问答的 SSE 流式版本 |
| `GET /trace/{run_id}` | 按 `run_id` 回溯查询血缘（SQL + 参数 + 交叉验证结论） |
| `GET /health` | 分析链路存活探针（与监控 `/healthz` 并存，路径不冲突） |

---

# 共享数据底座（M1）

两套能力都建在同一个 DuckDB 仓库上，数据接入只实现一次：

- `core/db.py` — 连接与建表。`SCHEMA_SQL` 建监控层全部表（`ingest_run` / `stock_universe` / `trading_calendar` / `corporate_event` / `stock_daily` / `index_daily` / `valuation_daily`），`MIGRATIONS` 追加分析层血缘表（`query_run` / `verify_run`）。`connect()` 先跑 schema 再跑迁移。
- `domains/finance/ingest.py` — akshare 采集：日线、指数、估值、交易日历、公司事件，幂等 upsert / 增量 / `run_id` 血缘。
- `config.py` — `pydantic-settings` 配置（DSN、LLM 接入点等）。

`stock_daily` 等表的列注释显式声明单位（价格=元、成交量=股、估值=倍），语义层引用时不再猜单位——这是监控层与分析层共用同一份"单位真相"的代价，也是分析答案里单位永远正确的原因。

# 统一架构

```
                        ┌─────────────────────────────────────────────┐
   外部入口             │   HTTP API (api/app.py)   ·   MCP (mcp/server.py) │
                        │   demo 站 (cli/serve.py)                        │
                        └───────────────┬─────────────────────────────────┘
                                        │
            ┌───────────────────────────┴───────────────────────────┐
            │  能力一：数据质量监控              │  能力二：可信问数分析        │
            │  dq/ 探测+归因                    │  agent/ LangGraph 编排       │
            │  eval/ 故障注入+门禁              │  semantic/ 指标字典+编译器   │
            │  report/ 报告+看板+告警路由       │            +交叉验证+血缘    │
            │  observability/ OTel 埋点         │                            │
            └───────────────────────────┬───────────────────────────┘
                                        │
                        ┌───────────────┴───────────────────────────┐
                        │       共享服务层  service.py                │
                        └───────────────┬───────────────────────────┘
                                        │
                        ┌───────────────┴───────────────────────────┐
                        │  共享数据底座  core/db.py + DuckDB 仓库      │
                        │  domains/finance/ingest.py (akshare)        │
                        └─────────────────────────────────────────────┘
```

# 部署：HTTP API + MCP 工具 + Docker

三个 entry-points 同源共享 `findata.service` 层（避免重复实现两条相同的流水线）。

```bash
uv run findata-serve          # 演示站 + API 同进程（默认 :8080）
uv run findata-api            # 仅 FastAPI（适合 ASGI 后端场景）
uv run findata-mcp            # MCP stdio server（挂给 Claude Desktop / Cursor）
```

### HTTP 路由（两套能力并存）

| 方法 | 路径 | 能力 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/` | — | 演示站首页（iframe 嵌入 live dashboard） |
| `GET` | `/healthz` | 监控 | 健康探针，不触发任何巡检 |
| `GET` | `/docs` | — | FastAPI 自动生成的 OpenAPI UI |
| `POST` | `/v1/inspect` | 监控 | 跑一次巡检，返回 JSON 摘要 + 告警/抑制明细 |
| `POST` | `/v1/eval` | 监控 | 跑评测；`triage=baseline/llm/both`，both 输出 Cohen's κ |
| `GET` | `/v1/dashboard` | 监控 | 拉一份自包含 HTML 看板（无 CDN、无外部依赖） |
| `POST` | `/v1/render` | 监控 | 单日报告：`fmt=md` 或 `fmt=html` |
| `GET` | `/v1/traces` | 监控 | 最近 OpenTelemetry span（按 `end_time desc`） |
| `GET` | `/health` | 分析 | 分析链路存活探针 |
| `GET` | `/metrics` | 分析 | 列出语义层所有可用指标 |
| `POST` | `/metric` | 分析 | 直查指标：确定性 SQL 编译＋交叉验证，带 `run_id` |
| `POST` | `/agent` | 分析 | 自然语言问答（LangGraph Agent） |
| `POST` | `/agent/stream` | 分析 | 问答的 SSE 流式版本 |
| `GET` | `/trace/{run_id}` | 分析 | 按 `run_id` 回溯查询血缘 |

### MCP 工具（stdio transport）

```json
{
  "mcpServers": {
    "findata": {
      "command": "findata-mcp",
      "args": []
    }
  }
}
```

挂载后多出五个工具：
`findata_health_check` / `findata_dashboard` / `findata_validate` /
`findata_list_metrics` / `findata_execute_metric`（基于指标字典 YAML 的
确定性 SQL 执行，外部 Agent 可以直接给 Claude Desktop / Cursor 拉指标层；
`findata_execute_metric` 走的就是语义层 `metric_query`，绝不接受裸 SQL）。

### Docker

```bash
docker compose up --build
```

`slim` 多阶段构建：`uv sync --no-dev --no-install-project` 只装**依赖**，runtime 只拷
site-packages + `src/` + `site/`，最终镜像约 200MB。健康检查走 `/healthz`。

> **为什么不在容器里 `pip install .`**：slim 内跑 PEP 517 build 要现场拉 build
> backend（hatchling → `editables`，或 setuptools isolated build），release 曾连挂 5 轮。
> 改成源码 + `PYTHONPATH=/app/src` 直跑后链路一次通——`findata.__version__` 有
> `_FALLBACK_VERSION` 兜底，不依赖 installed metadata。

### 为什么 HTTP + MCP 都做

- HTTP 适合「人在浏览器看 / 看板嵌入 / 系统对接」
- MCP 适合「Agent 在 IDE 里调 / 自动化诊断 / 可被组合」

二者背后是**同一份** `findata.service` 调用，避免出现"UI 一份数据、Agent 一份数据"的那种自然漂移。

# CI 与镜像分发

```bash
# 本地一键验全
uv run ruff check src tests scripts
uv run pytest
uv run python scripts/ci_assert_inspect.py < /tmp/inspect.json   # 模拟 CI smoke
```

GitHub Actions 三关 + 聚合门禁：

| Job | 干什么 | 抓的故障 |
| --- | --- | --- |
| `lint` | ruff check src + tests + scripts | 风格 / 已弃用 API / 隐含 bug |
| `test` | pytest (py 3.11 & 3.12 matrix) | 行为回归 |
| `smoke` | `uvicorn findata.cli.serve:app` 真实起 8080，curl `/`、`/healthz`、`/v1/inspect`、`/v1/dashboard`，断言响应字段 | "import 顺序错"、"entry-point 在打包后找不到模块"、"运行时字段不一致"——unit test 拼不出来的故障 |
| `ci-gate` | 等上面三关都 success 才放行 | 防止 reviewer 看到 4 条绿里混着 1 条红 |

镜像分发（`release.yml`）：**仅 `v*.*.*` tag 触发 docker buildx 多平台构建**
（linux/amd64 + linux/arm64），推到 `ghcr.io/quannie255-star/findata-agent`。
RC tag（如 `v0.5.0-rc1`）不会污染 `:latest`，只有 stable tag 推 `:latest`。

**push 前先冒烟**：multi-arch 构建只能 push、不能 `docker run`，所以流水线拆两步 ——
先单架构 amd64 `--load` 构建并起容器，curl `/healthz` 与 `/` 通过后才 multi-arch push。
坏镜像不会进 registry。

# 可观测：OpenTelemetry

`findata.observability` 子包默认装一个 `InMemorySpanExporter`（演示站 5KB 起步，
不需要 OTLP collector）。`service.inspect` / `render_report` / `build_dashboard` /
`evaluate` 四个入口都埋了根 span，关键 attribute：

- `findata.source` / `findata.asof` / `findata.seed`
- `findata.health_score` / `findata.n_findings` / `findata.n_alerts` / `findata.n_suppressed` / `findata.grade`

`evaluate(both)` 子 span `service.evaluate.both_triage` 会写 `findata.cohen_kappa`，
LLM 归因器 vs 规则归因器的一致性指标从此有量化追溯。

`GET /v1/traces?limit=50` 按 `end_time desc` 返最近 span——线上告警可以一键跳到
生成它的 service 调用栈。后续接 Tempo / Jaeger / Honeycomb 只换 provider，业务代码不动。

# 快速开始

```bash
uv sync
uv run pytest                                    # 单测（监控 + 分析两套）
uv run python scripts/run_eval.py                # 数据质量评测
uv run python scripts/run_inspection.py          # 每日巡检（合成语料，无需外网）
uv run python scripts/build_dashboard.py         # 评测看板
uv run python scripts/ingest_finance.py --events # 采真实数据（含交易日历与公司事件）
uv run python scripts/run_inspection.py --source warehouse   # 真实仓库巡检
uv run python scripts/query_demo.py              # 分析层：直查指标 + 交叉验证示例
uv run python scripts/agent_demo.py              # 分析层：自然语言问答示例
uv run findata-serve                             # 演示站（:8080）
docker compose up                                # 容器化版
```

巡检默认跑合成语料而非真实仓库，是刻意的：真实仓库要先采集几分钟且依赖外网，
而让 clone 仓库的人第一条命令就看到完整产出，比"先去准备数据"重要得多。

# 产出样例

不想先装环境的话，[`examples/`](examples/) 里有跑好的快照——巡检报告（Markdown / HTML）、
评测看板、评测门禁输出，浏览器直接打开就能看，且**任何人 clone 后重跑结果逐字一致**。

[`PITCH.md`](PITCH.md) 是另一份东西：**给要介绍这个项目的人准备的**——简历可直接粘贴的
项目描述、30 秒电梯演讲、以及几个高频追问的应答（包括这个项目的边界和一处我撤回了的实现）。

巡检一瞥（合成语料，观察日 2024-06-28）：

```text
健康分 36.0（严重）· 信号 13 → 告警 8 · 抑制 5（降噪 38%）
  [P0] 600030 upstream_stale      — 最新数据落后 4 个交易日，且无停牌/新股可解释，判定为上游停更
  [P0] 000858 unit_shift          — 量级突变至 0.000118 倍，判定为单位或口径变更
  [P1] 600519 schema_null_sweep   — 列 amount 近期空值率 73%，远高于历史，判定为上游字段下线
```

评测门禁（规则归因器 · 确定性故障注入语料）：

| 指标 | 结果 |
| --- | --- |
| 故障召回率 | 100.0% (7/7) |
| 根因准确率 | 100.0% (7/7) |
| 误报抑制率 | 100.0% (5/5) |
| 告警精确率 | 100.0% (7/7) —— **朴素基线 61.5%** |

最后一行是重点：**同一批 13 条信号，不做归因、全部告警时精确率只有 61.5%。**
这 38.5 个百分点的差就是归因层存在的全部理由，也是这个项目的门槛——检测不难，
难的是判断该不该报。

# 真实数据上跑过吗

跑过。[`examples/inspection-warehouse-2026-09-07.md`](examples/inspection-warehouse-2026-09-07.md)
是真实 akshare 数据（25 只标的 / 3,396 行，观察日 2026-09-07）的巡检结果。

**第一版暴露了误报，已修**：健康分 84、4 条告警，其中 3 条 `missing_rows` 全部落在
2022 年春节窗口。硬编码节假日表只覆盖 2024-2025，而春节是**全市场同时休市**——
那些日子根本没有观测，"数据里出现过就是交易日"永远推不出来。
接进真实交易日历（`trading_calendar` 表，akshare `tool_trade_date_hist_sina`，
1990 至今 8797 天）后，信号 57 → 3、告警 4 → 2、健康分 84 → 92。
剩下的 2 条经核查都是真信号：600030 缺的 6 个交易日，同窗口其他标的有数据，
是回补漏采而非集体休市。

**还有两条没修，也不打算假装修好**：

- **真实数据上抑制率是 0**。`corporate_event` 现在有 553 条真实除权除息记录
  （覆盖 24 只，1995–2026），但告警窗口内一条都没落上；停牌接口只给"此刻谁停着"
  的快照、没有历史起止日。所以**合成语料上 100% 的精确率是设计出来的对照，
  归因层的抑制能力在真实数据上尚未被触发**——缺的不是算法，是停牌历史这个事实来源
- **成交量漂移分不清"单位变更"和"市场放量"**。另一条告警是 600030 放量 5.40 倍，
  核查后同期价格上涨 14.8%，是 2023 年 7 月券商政策行情，合法。试过加归因规则，
  失败了：探针算的"10 日均量/前 30 日均量"把评测注入的 20x 稀释成 5.14x，
  和真实放量的 5.40x 数值重合；硬塞规则会让根因准确率从 100% 掉到 85.7%、
  评测门禁挂掉。**为修一条告警牺牲门禁是负收益，所以撤回并留作已知局限**

愿意把这两条写进 README，是因为**知道边界比假装没边界重要**。被问"真实数据上跑过吗"
时，能说清为什么误报过、为什么抑制是 0、试过什么没成功，比报一个"100%"可信得多。

# 技术选型：为什么没用这些 / 为什么用了这些

面试常问"用了什么"，但**知道不用什么、以及为什么不用**，更能说明架构判断力。

### 监控层没用 LangGraph，分析层用了

核心流水线是**确定性 DAG**：探针 → 归因 → 路由 → 渲染。没有运行时才能决定的分支，
没有回环，没有"走一步看一步"的工具调用链。

LangGraph 解决的是"下一步取决于上一步结果"的编排问题——多轮工具调用、条件跳转、
人工介入、状态回溯。监控层一条直线跑到底，套一层图就是把确定的东西变成不确定的，
还多一个状态机要测、要解释、要在出问题的时候排查。

而**分析层的问数 Agent 正好相反**：它需要先理解意图、可能要查数、工具回填后再组织答案，
存在运行时决定的分支与多轮工具调用——这是有回环的对话状态机，**此时上 LangGraph 是正当的**。
同一个项目里，"确定性 DAG 不上图、有状态的 Agent 上图"是同一个判据的正反两面，而不是双标。

有一处确实像图：LLM 归因超时 → 单条降级 → 整批兜底。目前用 `try/except` +
协议兜底实现，是**一层的**回退，画成图反而把三行控制流复杂化。

### 没用 RAG / 向量库

归因要的不是"相似的文档"，是**精确的业务事实**：这只股票这段区间是不是停牌、
哪天除权、什么时候上市。这些是查表问题，`corporate_event` + SQL 直接命中。

更要紧的是，相似度检索会返回"看起来像"的事实，而在数据质量场景里，**一个像的
事实比没有事实更危险**——它会让系统理直气壮地抑制掉一条真告警。项目的设计取向
是宁可说"不知道"（`UNKNOWN` 降级给规则归因器），也不能编一个像的。

### 语义层没让 LLM 直接写 SQL

这是分析层对"可信"的底线。Text2SQL 的风险不在算错，在编数字。findata 把 SQL 生成
锁进人类审核过的指标字典模板，LLM 只能"选指标 + 填参数"，编译器完成确定性渲染与
参数绑定。配合每次查询的第二条独立口径交叉验证，答案的可信度来自"算法不同 + 口径独立"，
而不是"模型说它是对的"。

### 用了 DuckDB 而不是数仓 / 时序库

单机分析场景，列存 + 单文件，零运维。真正的收益是**让 clone 仓库的人第一条命令
就能跑出完整结果**——需要先起一套集群的项目，绝大多数人根本不会跑第二遍。

生产上换成 ClickHouse 只需换 `domains/finance` 的查询层：探针和归因器吃的是
DataFrame，不关心底下是谁供的数。

# 目录地图

```
src/findata/
├── core/            # 共享：db.py(DuckDB+血缘) · lineage.py
├── domains/finance/ # 共享：ingest.py (akshare 采集，幂等/增量)
├── config.py        # pydantic-settings 配置
├── dq/              # 能力一：探测 probes · 归因 triage · 契约 models · 协议 protocol
├── eval/            # 能力一：故障注入 faults · fixture · metrics · runner · _interop
├── report/          # 能力一：inspect 流水线 · render 报告 · dashboard 看板
├── observability/   # 能力一：OTel 埋点
├── semantic/        # 能力二：catalog(指标字典) · compiler(确定性SQL) · executor · verifier(交叉验证) · tools · metrics.yaml
├── agent/           # 能力二：graph(LangGraph 问数) · llm(LLM 客户端 + make_chat_model)
├── service.py       # 共享服务层：监控与分析都经过它
├── api/app.py       # 共享 HTTP 入口：监控路由 + 分析路由并存
├── mcp/server.py    # 共享 MCP 入口：5 个工具
└── cli/serve.py     # 演示站

tests/               # 两套能力的单测（test_dq/ test_eval/ test_semantic/ test_agent/ test_verifier/ test_golden_eval/ ...）
scripts/             # run_inspection · run_eval · build_dashboard · ingest_finance · query_demo · agent_demo · serve · eval_golden ...
eval/golden/         # 分析层 golden 评测集（确定性 pin 值，CI 门禁）
```

# 路线图

- [x] **M1 数据层**：DuckDB 仓库 + akshare 采集（幂等 / 增量 / 血缘）— *两套能力共享底座*
- [x] M2 质量内核：七类探针 + 规则归因器 + 三类评测指标 + 确定性故障语料
- [x] M3 产品化：巡检流水线 + 告警路由/聚合/健康分 + Markdown/HTML 报告
- [x] M4 Agent 层：LLM 归因器（Triage 协议 + 白名单校验 + 逐条降级 + 可观测统计）
- [x] M5 看板：回放 + 健康分趋势 + 朴素基线对照 + 告警历史（静态 HTML，零依赖）
- [x] M6 部署：FastAPI + MCP 工具 + Docker slim 镜像 + 单页演示站（HTTP/MCP 同源）
- [x] M7 CI：ruff + pytest(py 3.11/3.12) + 真实 serve smoke + ci-gate 聚合，ghcr.io 多平台镜像分发（仅 stable tag 推 `:latest`）
- [x] M8 可观测：`findata.observability` OTel 默认 InMemory exporter，service 层埋点 + `GET /v1/traces` 端点
- [x] M2-bis 指标字典：metric 抽到 YAML，`SqlTemplate` 确定性编译器（`$name` → `?`，同名占位按次数复制参数），新增 MCP `findata_list_metrics` / `findata_execute_metric`
- [x] M9 业务事实表 schema：`stock_universe.list_date` + `corporate_event(suspension/ex_rights/listing)`，归因层依赖的生产 schema 落地
- [x] M9-bis 上市日回填：`backfill_list_date` 用 `stock_daily` 首个交易日回填 `list_date`（幂等 + 补采到更早历史时自我修正）
- [x] M10 发布链路：镜像不在容器内 build project（slim 内 PEP 517 build 需现场拉 build backend，是连挂 5 轮的根因），release 拆两步「单架构起容器冒烟 → 通过才 multi-arch push」，坏镜像不进 registry
- [x] M11 真实交易日历 + 公司事件采集：`trading_calendar` 表（8797 天）+ `TradingCalendar.from_trading_days` 查表模式 + `ingest_corporate_events`（除权除息 553 条 + 停牌快照）。真实数据上信号 57 → 3、告警 4 → 2、健康分 84 → 92
- [x] **M2–M6 可信问数分析（v0.7.0 合并）**：`semantic/`（指标字典 + 确定性 SQL 编译器 + 交叉验证 + `query_run`/`verify_run` 血缘）+ `agent/graph.py`（LangGraph 问数 Agent，工具白名单 + run_id 溯源）+ 分析 HTTP 路由（`/metric` `/agent` `/agent/stream` `/trace/{run_id}`）+ golden 评测集（`eval/golden/`）

# License

MIT
