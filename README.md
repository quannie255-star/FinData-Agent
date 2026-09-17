# Findata — 可信数据增强模块

[![test](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)
[![release](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![镜像](https://img.shields.io/badge/ghcr.io-v2.2.0-green)

**一句话**：findata 不做 Agent 基座——它是挂到成熟 Agent 项目上的
**「可信增强模块」**：宿主负责对话与编排，findata 负责回答
**「这个数字能不能引用、凭什么」**。每个数字带四档徽章
（✓ 已核验 / ✓ 基线通过 / ⚠️ 仅借鉴 / ✗ 不可用），徽章可点开看归因证据链。

它由三部分组成：

| 组成 | 给谁用 | 一句话 |
| --- | --- | --- |
| **可信增强模块**（v2.1） | 任何 Agent 宿主 | MCP / HTTP / Python / 可嵌入徽章芯片四个接入面，引用数字前查一次 `trust_check` |
| **通用数据可信包**（v2.2） | 任何有数据的人 | `findata-trust-report <csv|xlsx|duckdb|sqlite>`——任意表出带徽章报告 |
| **A 股领域包**（v0.x–v2.0） | 金融数据团队 | 探针→归因→抑制→徽章日报全链路 + 可信问数多智能体，是"领域包"的参考实现 |

差异化不在检测（那是 Great Expectations / dbt tests 的主场），在**归因证据链**：
缺 6 行数据是漏采还是停牌、量能 5 倍是故障还是行情——需要交易日历、公司事件、
截面共动性这些领域知识才能判定。徽章的「✓ 已核验」只能由领域包给出；
通用包最高「✓ 基线通过」，两者在 `examples/domain-vs-generic-comparison.md`
的同数据集对比里差出 40 分（60 vs 100）。

---

## 快速开始

```bash
uv sync
uv run pytest                              # 351 例测试
uv run python scripts/eval_golden.py --strict   # 评测门禁（语义 golden + 真实回放 9 case）

# 通用包：任意表出可信报告（无需任何领域配置）
uv run findata-trust-report your.csv --strict

# A 股领域包：采集 → 带徽章日报一条命令
uv run python scripts/ingest_finance.py --full --events
uv run python scripts/daily_pipeline.py --skip-ingest   # 巡检+报告 Agent，出 md+html

# 增强模块：MCP server（挂给 Claude Desktop / Cursor / 任意 MCP host）
uv run findata-mcp
```

不想装环境：[`examples/`](examples/) 里有全部可复现归档（带徽章日报、对比页、
外部宿主集成 transcript），任何人 clone 后重跑结果一致。

## 一、把 findata 挂到你的 Agent 上（增强模块）

宿主只讲标准协议，零 findata 内部依赖。四个接入面共用同一份 `service` 实现：

| 接入面 | 入口 | 适合 |
| --- | --- | --- |
| **MCP 工具** | `findata-mcp`（stdio） | Claude Desktop / Cursor / Cline / 自建 LangGraph Agent |
| **HTTP API** | `GET /v1/trust` · `GET /v1/trust/board` | 只能调 REST 的 GenBI / 平台型宿主（松耦合门禁，不 fork 不焊接） |
| **Python** | `findata.service.trust_check / trust_board` | 宿主是 Python 且接受进程内依赖 |
| **徽章芯片** | `dq.badges.badge_chip_html` | 宿主渲染报告时贴在数字旁，悬停即证据 |

```jsonc
// Claude Desktop / 任意 MCP host 的配置
{ "mcpServers": { "findata": { "command": "findata-mcp" } } }
```

```bash
curl 'http://localhost:8000/v1/trust?table=stock_daily&metric=close'
# → {"badge":"verified","mark":"✓ 已核验","usable":true,
#    "evidence":[["corporate_event:suspension:600030","已核验停牌 · …"],…],
#    "health_score":95.8}
```

**宿主接入纪律**（完整版 `docs/integration.md`）：会话开始拉一次 `trust_board`；
引用任何数字前查 `trust_check`，`usable=false` 不得作为已核验引用；未知指标
错误带全部可用键，据此自愈。参照实现是 findata 自己的审校节点
（`agent/nodes/report.py::review_and_enforce`，纯规则、可全分支单测）。

**真实外部宿主已验证**：`scripts/mcp_host_demo.py` 是一个对 findata 零导入的
通用 Agent 宿主（官方 MCP SDK stdio 客户端 + 宿主自己的 DeepSeek）——真实实测
问「茅台最新收盘价能否放心引用」，经 工具发现 → `metric_query` 查数 →
`trust_check` 过门禁 → 回答逐数字带徽章与 run_id（transcript 归档
[`examples/mcp-host-integration.txt`](examples/mcp-host-integration.txt)，
线级协议测试进 CI）。

## 二、通用数据可信包

```bash
uv run findata-trust-report data.csv --schema schema.yaml -o report.md --html report.html
uv run findata-trust-report warehouse.duckdb --table stock_daily --strict
```

- 五类通用检查：完整性（空值率 vs 声明容忍）/ 唯一性（主键重复）/
  新鲜度（时间锚点停更）/ 离群值（组内 IQR + 水平迁移）/ Schema 一致；
- 声明式 schema（YAML）：列类型、可空、主键、时间锚点、分组列、容忍度；
  无声明时自动推断，但检查降级在报告里**显式可见**；
- **诚实上限**：无领域知识，最高只给「✓ 基线通过」（= 没查出毛病，
  不是证明可靠）。「✓ 已核验」只能来自领域包的归因背书——测试钉死。

两个数据集实测（fixture 入库，离线可复现）：

| 数据集 | 通用包看到了什么 |
| --- | --- |
| [UCI Beijing PM2.5](examples/generic-trust-report-air-quality.md)（真实公开数据，CC BY 4.0） | 数据停更 **4917 天** → 全列 ⚠️，健康分 50 |
| [脱敏电商订单](examples/generic-trust-report-ecommerce.md)（合成演示，明确标注） | 主键重复 → ✗ 不可用；类目内金额水平迁移 → ⚠️ |

**通用包 vs 领域包，同一数据集差多少？**
[`examples/domain-vs-generic-comparison.md`](examples/domain-vs-generic-comparison.md)：
同一份 A 股数据，通用包 60 分 / 8 列 ⚠️ / 11 条信号无法归因；
领域包 100 分 / 8 列 ✓ 已核验 / 2 段停牌缺口归因+公告证据。
「没查出毛病」和「证明可靠」的差值，就是领域包架构的存在理由。

## 三、A 股领域包：从检测到可信日报

核心命题：**停牌导致的缺行和采集失败导致的缺行，在数据形态上完全一样**。
只会检测的工具必然淹没人。链路：7 类探针 → 归因（规则引擎 + 知识层 +
截面共动）→ 四档徽章 → 报告 Agent → 带徽章日报。

**真实数据考试与修复**（这是本项目最重要的故事）：

- 2026-09-15 真实数据首次考试：7/7 告警全是合法事件（停牌/行情），抑制 0/9，
  健康分 72——复盘见 [`docs/reviews/2026-09-15-attribution.md`](docs/reviews/2026-09-15-attribution.md)；
- R1 修复（知识层种子 + 截面共动归因）后回放：**抑制 8/9，健康分 95.8**
  （[`examples/replay-badge-report.txt`](examples/replay-badge-report.txt)），
  9 个真实案例固化为回放 golden 进 CI 门禁（`eval/golden/replay.yaml`）；
- 唯一残留如实：中芯国际 2023-03 孤立行情是**个股级**事件，需公告/新闻
  等外部事实源，保留为漂移告警（P2 观察级）。

**带徽章日报**（`scripts/daily_pipeline.py` 一条命令）：巡检 → 徽章 →
报告 Agent（分析 → 可视化 → 可信审校）。审校是纯规则：报告里每个数字必须带
`«表.列»` 引用标记，无徽章/幽灵引用/✗ 徽章一律拦截。真实 LLM 实测：
710/472 字符、审校 0 拦截
（样例 [`examples/daily-badge-report-2026-09-15.md`](examples/daily-badge-report-2026-09-15.md)）。

**可信问数**（语义层 + LangGraph 多智能体）：LLM 只负责选指标填参数，SQL 由
编译器确定性生成（参数绑定，无注入面），每条查询自动双口径交叉验证，
答案强制携带 `run_id` 可回溯到 SQL 与验证结论。MCP `findata_ask` /
`findata_metric_query` 与 `POST /agent` 同源。

### 徽章口径

| 徽章 | 含义 | 证据要求 |
| --- | --- | --- |
| ✓ 已核验 | 领域知识归因背书 | 归因结论 + 证据引用（事件表/截面共动/日历） |
| ✓ 基线通过 | 仅通用检查通过，无领域知识参与 | 检查项清单 |
| ⚠️ 仅借鉴 | 有疑点，人工判断 | 疑点描述 |
| ✗ 不可用 | 归因为真实故障 | 根因 + 建议动作 |

健康分 = 徽章聚合（✓ 1.0 · ⚠️ 0.5 · ✗ 0，只对已观测指标发徽章），
唯一权威定义在 `src/findata/dq/badges.py`，口径文档 [`docs/badge.md`](docs/badge.md)。

## 评测门禁（声明与代码一致的全部底气）

```
CI: lint → pytest(351, py3.11/3.12) → e2e smoke → eval-gate → ci-gate
eval-gate = 语义 golden（5 case 数值钉死）
          + 归因质量下限（合成语料召回/精确/抑制 ≥0.9）
          + 真实回放 golden（2026-09-15 快照 9 case：根因/抑制/证据链逐条断言）
```

- 合成语料：告警精确率 **100%（朴素基线 61.5%）**——不做归因时 13 条信号
  里 5 条是"看起来像故障"的合法事件；
- 真实回放 9/9：合成语料证明**机制对**，真实回放证明**事实够**，缺一不可；
- 扩充语料（v0.9 封存）：进化后的 LLM prompt 抑制 100% vs 规则 62.5%，
  归档 [`examples/eval-prompt-evolution.txt`](examples/eval-prompt-evolution.txt)。

## 架构

```
外部宿主（Claude Desktop / LangGraph Agent / GenBI 平台 / 你的脚本）
   │  MCP stdio          │  HTTP REST            │  Python / HTML 芯片
   └─────────────────────┴───────────────────────┴──────────────┐
                                                                 │
   findata.service ── 同一份实现，四种接入面 ──┐                  │
   ├─ 增强模块: trust_check / trust_board      │                  │
   ├─ 巡检: inspect / render / dashboard       │                  │
   └─ 问数: metric_query / ask（run_id 溯源）  │                  │
                                                                 │
   ┌────────────────┬────────────────────┬────────────────────┐ │
   │ A股领域包       │ 通用包 (generic/)   │ 评测 (eval/)       │ │
   │ dq/ 探针+归因   │ 5 类通用探针        │ 合成语料+故障注入  │ │
   │ badges/ 徽章    │ 声明式 schema       │ 真实回放 golden    │ │
   │ report/ 日报    │ trust-report CLI    │ 质量下限门禁       │ │
   │ agent/ 报告Agent│                     │                    │ │
   └────────────────┴────────────────────┴────────────────────┘ │
                                │                                │
                共享数据底座 core/db.py + DuckDB 仓库             │
                domains/finance/ 采集（akshare，幂等+血缘）       │
                                                                 │
   generic/ 与 dq/badges 共享徽章聚合单一实现 ◄───────────────────┘
```

关键目录：`dq/`（探针+归因+日历+徽章）· `generic/`（通用包）·
`report/`（巡检渲染推送）· `agent/`（Supervisor 多智能体 + 报告 Agent）·
`semantic/`（指标语义层 + SQL 编译器）· `mcp/`（9 个 MCP 工具）·
`eval/`（合成语料/故障注入/门禁/真实回放）· `domains/finance/`（采集+停牌种子）。

## HTTP / MCP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/trust` · `/v1/trust/board` | **可信增强模块**：单指标徽章 / 全板徽章 |
| `POST` | `/v1/inspect` | 巡检：JSON 摘要 + 告警/抑制 + 全板徽章 |
| `POST` | `/v1/eval` · `GET /v1/dashboard` · `POST /v1/render` | 评测 / 看板 / 单日报告 |
| `GET` | `/metrics` · `POST /metric` · `POST /agent` · `GET /trace/{run_id}` | 可信问数：指标/问答/血缘回溯 |
| MCP | `findata_trust_check` `findata_trust_board` `findata_ask` `findata_metric_query` 等 9 个 | stdio，配置即用 |

三者背后是同一份 `findata.service`——UI、Agent、API 不会漂移出两套数据。
镜像分发：`v*.*.*` tag 触发多架构构建推 ghcr.io,push 前先单架构冒烟。

## 诚实边界

- **合成语料的数字不外推到真实数据**。对外表述必须标注口径；
  真实数据的结论一律以回放 golden 与 examples/ 归档为准；
- **通用包不承诺"任意数据可信"**：它只承诺通用基线检查，✓ 基线通过
  不是可靠性证明；残留在报告里的检查降级清单同样重要；
- **个股级行情归因仍是盲区**（如 2023-03 中芯国际），需要公告/新闻事实源，
  已知残留按观察级跟踪；
- **封存模块不投入**：RL 训练（待 GPU）、prompt 自进化、LLM 归因记忆
  是已验收里程碑与投递证据，测试照跑，不加新功能；
- 实盘 / 交易信号红线不变；可视化够用即可，不做花哨前端。

## 技术选型：为什么没用这些

### 监控链路没用 LangGraph，分析与报告 Agent 用了

巡检流水线是**确定性 DAG**：探针 → 归因 → 路由 → 渲染，没有运行时才能决定的
分支——套图是把确定的东西变成不确定的。问数与报告 Agent 有真实的回环
（工具调用、验证追问、审校重写），才轮到 LangGraph。

### 归因用查表，不用 RAG

归因要的不是"相似的文档"，是**精确的业务事实**：这只股票这段区间是不是停牌。
这是 `corporate_event` + SQL 的查表问题。相似度检索会返回"看起来像"的事实，
而在数据质量场景里，一个像的事实比没有事实更危险——它会让系统理直气壮地
抑制掉真告警。宁可 UNKNOWN 降级，也不编一个像的。

## 文档地图

| 文档 | 内容 |
| --- | --- |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 主线计划、验收门禁、不做清单（唯一权威来源） |
| [`docs/integration.md`](docs/integration.md) | 增强模块接入指南（宿主三条接入纪律） |
| [`docs/badge.md`](docs/badge.md) | 徽章四档与健康分口径 |
| [`docs/reviews/2026-09-15-attribution.md`](docs/reviews/2026-09-15-attribution.md) | 真实告警归因复盘（v2.0 的起点） |
| [`PITCH.md`](PITCH.md) | 简历描述 / 电梯演讲 / 高频追问（自用） |
| [`examples/`](examples/) | 全部可复现归档：日报、对比页、集成 transcript、评测输出 |

## 许可

数据来源：akshare（新浪/东财公开接口）、UCI ML Repository（CC BY 4.0）。
本项目仅供学习与研究，不构成任何投资建议；实盘/交易信号为永久红线。
