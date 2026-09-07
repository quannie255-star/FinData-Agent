# Findata — 数据质量监控 Agent

> 不只是发现数据异常，而是判断它**该不该被当回事**。

**Findata** 是一个面向 A 股金融数据的**数据质量监控 Agent**：自动巡检数据仓库，发现异常后做**根因归因**，并主动**抑制因合法业务事件（停牌、除权除息、新股上市、财报修正）产生的误报**，最终产出可行动的告警与巡检报告。

## 为什么是这个方向

做问数（Text2SQL）的开源方案已经挤满（Vanna / WrenAI / DB-GPT / DeepBI），但它们全部默认一件事：**喂进去的数据是好的**。真实生产里这个前提不成立——上游停更、字段空值率暴涨、口径漂移、多源不一致每天都在发生。

而真正拦住数据质量工具的，也不是"检测不到"，是**告警噪音**：

- 停牌 → 没有行情（合法，不该告警）
- 除权除息 → 价格跳变（合法，不该告警）
- 新股上市 → 历史长度不足（合法，不该告警）
- 上游采集失败 → 没有行情（**故障，必须告警**）

前三种和最后一种**在数据形态上完全一样**。只会检测的工具必然淹没人，而淹没有人的工具没人用。

所以本项目的核心命题是：**在形态相同、结论相反的事实之间做出区分**。这既是技术难点，也是产品的生死线。

## 架构

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

**设计原则：探测与判断分离。** 探针只回答"观测到了什么"，不回答"是不是问题"；归因层独占判断权，因此抑制逻辑可单测、可评测、可替换成 LLM。

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
mm-curation-pipeline 的 `scripts/findata_health_stage.py` 把 findata 当作外部模块 import，在清洗流水线末尾加一道"仓库级健康巡检"，并把 mm-curation 的产物摘要写进 findata 报告头部：
```
## 上下游：本次巡检由 mm-curation-pipeline 触发
- 上游输入样本数：2106
- 上游漏斗后样本数：1585
- 上游 Recall@1：0.575
- findata 巡检摘要：信号 13 / 告警 8 / 抑制 5 / 健康分 36
```

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

## 部署：HTTP API + MCP 工具 + Docker

三个 entry-points 同源共享 `findata.service` 层（避免重复实现两条相同的流水线）。

```bash
uv run findata-serve          # 演示站 + API 同进程（默认 :8080）
uv run findata-api            # 仅 FastAPI（适合 ASGI 后端场景）
uv run findata-mcp            # MCP stdio server（挂给 Claude Desktop / Cursor）
```

### HTTP 路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 演示站首页（iframe 嵌入 live dashboard） |
| `GET` | `/healthz` | 健康探针，不触发任何巡检 |
| `GET` | `/docs` | FastAPI 自动生成的 OpenAPI UI |
| `POST` | `/v1/inspect` | 跑一次巡检，返回 JSON 摘要 + 告警/抑制明细 |
| `POST` | `/v1/eval` | 跑评测；`triage=baseline/llm/both`，both 输出 Cohen's κ |
| `GET` | `/v1/dashboard` | 拉一份自包含 HTML 看板（无 CDN、无外部依赖） |
| `POST` | `/v1/render` | 单日报告：`fmt=md` 或 `fmt=html` |

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

挂载后多出三个工具： `findata_health_check` / `findata_dashboard` / `findata_validate`。
外部 Agent（Claude Desktop / Cursor / Cline）就可以把 `findata` 当作「命令 + 读 JSON」的能力用——不再需要写 sql、改 ETL、写文档，证据、回放、验证都可以从对话里直接调。

### Docker

```bash
docker compose up --build
```

`slim` 多阶段构建：`uv` 装依赖到一个完整 venv，runtime 镜像只拷 site/ 与 site-packages，最终镜像约 200MB。健康检查走 `/healthz`。

### 为什么 HTTP + MCP 都做

- HTTP 适合「人在浏览器看 / 看板嵌入 / 系统对接」
- MCP 适合「Agent 在 IDE 里调 / 自动化诊断 / 可被组合」

二者背后是**同一份** `findata.service` 调用，避免出现"UI 一份数据、Agent 一份数据"的那种自然漂移。

## 快速开始

```bash
uv sync
uv run pytest                                    # 72 个单测
uv run python scripts/run_eval.py                # 数据质量评测
uv run python scripts/run_inspection.py          # 每日巡检
uv run python scripts/build_dashboard.py         # 评测看板
uv run python scripts/ingest_finance.py          # 采真实数据（约 2-3 分钟）
uv run findata-serve                             # 演示站（:8080）
docker compose up                                # 容器化版
```

巡检默认跑合成语料而非真实仓库，是刻意的：真实仓库要先采集几分钟且依赖外网，
而让 clone 仓库的人第一条命令就看到完整产出，比"先去准备数据"重要得多。

## 路线图

- [x] M1 数据层：DuckDB 仓库 + akshare 采集（幂等 / 增量 / 血缘）
- [x] M2 质量内核：七类探针 + 规则归因器 + 三类评测指标 + 确定性故障语料
- [x] M3 产品化：巡检流水线 + 告警路由/聚合/健康分 + Markdown/HTML 报告
- [x] M4 Agent 层：LLM 归因器（Triage 协议 + 白名单校验 + 逐条降级 + 可观测统计）
- [x] M5 看板：回放 + 健康分趋势 + 朴素基线对照 + 告警历史（静态 HTML，零依赖）
- [x] **M6 部署**：FastAPI + MCP 工具 + Docker slim 镜像 + 单页演示站（HTTP/MCP 同源）

## License

MIT
