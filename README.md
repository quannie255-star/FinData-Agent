# Findata — 金融长文本 Agent 的动态记忆压缩与高效问答

[![test](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)
[![release](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)

> **定位（2026-10-04 定稿）**：按 **AFAC2026 挑战组赛题四**（金融长文本 Agent
> 的动态记忆压缩与高效问答挑战，出题方复旦大学）的公开题面口径构建。
> ⚠️ AFAC2026 已于 2026-08-26 收官，本仓库**无任何参赛成绩或官方关联**——
> 对齐的是问题口径，目标是 AFAC2027（预计 2027 年 5-6 月报名）带成型系统下场。
> 开发计划唯一权威：[`docs/afac-track4.md`](docs/afac-track4.md)；
> 方向依据与 3 个月方向锁见 [`docs/final-scope.md`](docs/final-scope.md) §6。

**一句话**：agent 每轮往上下文/记忆里塞什么、塞多少，没人管、也**没人能证明
管了没坏事**。本项目读真实 trace，用**引用观测**反推哪些内容实际没人用，
产出**经过配对验证的**压缩策略（`policy.json`）与验证报告。

> **它和"上下文优化工具"的区别**：那些工具负责**执行过滤**（少发定义、检索
> 注入、截断返回），早已商品化；本项目负责**决定过滤什么、并证明过滤没伤到
> 结果**。不造网关，给网关/Agent 产出策略。

## 已实测的读数（同批任务 / 同一份冻结语料 / 同一份代码，唯一变量是被测的那一维）

| 改什么 | 省多少（实测 token） | 质量侧（配对） | 结论 |
| --- | ---: | --- | --- |
| 工具定义 27 → 4 个 | **−53.4%**（符号检验 p=2.46e-07，30 正/2 负） | 干净语料上 k=2 < 6，**本对照无分辨力** | 只主张 token 侧 |
| 工具返回裁掉 **95.9%** | −15.1% | `exact_hit` **7/32→1/32**（7B）、**6/32→0/32**（3B），两次 p=0.03125 | **两个模型上都显著变差** |

**主动撤回过的声称**（撤回记录比结论值钱）：

- 「裁 88.7% 不掉质量」→ 7B 没测到伤害但 3B 方向不利（6/32→2/32）⇒ 只能说**未证实**；
- 「短清单质量不降反升（7/32→13/32）」→ 出在**含答案卡的语料**上，泄漏偏向
  短清单臂；干净语料重算后该对照**没有分辨力**（k=2 < 6）⇒ 撤回，只报 token 侧；
- 噪声下界从未测过（harness 未固定解码）⇒ 定死顺序：**先量噪声、再固定解码、
  最后才报质量结论**，不许颠倒。

→ 站得住的那条结论本身很有信息量：「**少给内容**」和「**不给内容**」不是
程度差异，是两种东西。全套口径与复现见
[`docs/r5.0-acceptance.md`](docs/r5.0-acceptance.md) §3.8–§3.11、产物在
[`examples/context-economist/`](examples/context-economist/)。

## 赛题四对齐：资产映射与差距

| 赛题考查点 | 仓库既有资产 |
| --- | --- |
| 动态记忆压缩 / 上下文优化 | v4.0 `contextbudget`/`context-economist`：字段级引用归因、四档裁剪、`policy.json` |
| 证明压缩不伤结果 | 配对验证闭环、分辨力下限 `2^(1-k)`、噪声下界、语料/代码指纹（**差异化主打**） |
| 事实核验 | 报告 Agent 审校（无徽章数字强制拦截）+ `run_id` 溯源 + 双口径交叉验证 |
| 多智能体基建 | LangGraph Supervisor + nodes |
| 金融领域感 | A 股领域包、事件知识层（表格 → 文本部分迁移） |

要新建的（按依赖序）：官方统一 API harness → 五类金融文本评测集固化为
golden → **跨轮记忆流转模块**（v4.0 只做过单轮上下文预算，这是真正的新模块）。
里程碑 M0–M5 见 [`docs/afac-track4.md`](docs/afac-track4.md) §4
（评测先行：先把评测接成门禁，再写策略）。

---

# 第二作品：A 股金融数据可信引擎（零维护运行，2026-10-04 起）

> 完整介绍与归档见下文；当前状态：每日管线**照跑**（运行证据不断），
> bug 修、功能不加。它是金融数据岗求职的独立作品，也是本项目「领域知识
> 归因」能力的原始出处。

**核心命题**：停牌导致的缺行和采集失败导致的缺行，在数据形态上完全一样；
放量 5.4× 的合法行情和单位写错的故障，探针读数几乎重合。引擎在探针之上加
**归因层**（交易日历 + 公司事件知识层 + 截面共动性，查表可复现），判定每个
异常该不该报，产出**每个数字带四档可信徽章**的日报
（✓ 已核验 / ✓ 基线通过 / ⚠️ 仅借鉴 / ✗ 不可用）。

**真实运行史**（固化入库，[`examples/daily-pipeline-run-history.md`](examples/daily-pipeline-run-history.md)）：

```
共 17 次启动，16 次走到巡检（akshare，25 标的，2022-01 ~ 今）：
(告警 7 / 抑制 0 / 健康分 72) × 5 → (告警 1 / 抑制 8 / 健康分 96) × 11
```

2026-09-15 真实数据首次考试 7/7 告警全是合法事件（知识层从未上线，
`corporate_event` 0 行）→ 复盘
[`docs/reviews/2026-09-15-attribution.md`](docs/reviews/2026-09-15-attribution.md)
→ 按复盘修复（停牌种子 + 截面共动，一行外部数据没加）→ 真实仓库快照回放
**抑制 8/9、健康分 95.8**（[`examples/replay-badge-report.txt`](examples/replay-badge-report.txt)），
9 个真实案例固化为回放 golden 进 CI。

**通用包 vs 领域包**（同一份 A 股数据，
[`examples/domain-vs-generic-comparison.md`](examples/domain-vs-generic-comparison.md)）：
通用包 60 分 / 11 信号无法归因；领域包 100 分 / 停牌核验。「没查出毛病」和
「证明可靠」的差值，就是归因层的存在理由。徽章口径
[`docs/badge.md`](docs/badge.md)，唯一定义在 `src/findata/dq/badges.py`。

```bash
uv run python scripts/daily_pipeline.py --skip-ingest  # 离线巡检 → 带徽章日报
uv run findata-trust-report data.csv --schema schema.yaml --strict  # 任意表出带徽章报告
```

**接入面**（宿主零 findata 内部依赖）：MCP `findata_trust_check`/`findata_trust_board`
（stdio）、HTTP `GET /v1/trust`、Python `service.trust_check`、可嵌入徽章芯片。
真实外部宿主已验证（纯 MCP 协议 + 宿主自己的 DeepSeek，transcript 归档
[`examples/mcp-host-integration.txt`](examples/mcp-host-integration.txt)）。

---

# 换域证据一（v3.0）：Agent 训练数据的质检与过滤

丢进去一批 Agent 轨迹，吐出「哪些能喂训练、哪些不能、为什么」，每条判定带
证据链。核心是归因：**必填参数没传**，可能是 agent 漏填（能力问题），也可能
是工具 schema 自相矛盾（数据源的错）——形态一模一样，结论完全相反。

```bash
uv run python scripts/run_trust_filter.py    # 全量 60000 条，约 16 秒
```

全量 60000 样本 / 100011 次调用（`Salesforce/xlam-function-calling-60k`，
无一条自造），命中 1351 条（2.25%，说命中不说错误），其中 1286 条根因是
`schema_contradiction`——第一版全判成"agent 漏填"误杀 349 条，改判后归零。
归档全文（含「诚实说明」节）：
[`examples/trust-filter-report.txt`](examples/trust-filter-report.txt)。

## 评测门禁（声明与代码一致的全部底气）

```
CI: lint → pytest(673, py3.11/3.12) → e2e smoke → eval-gate → ci-gate
eval-gate = 语义 golden（5 case 数值钉死）
          + 归因质量下限（合成语料召回/精确/抑制 ≥0.9）
          + 真实回放 golden（2026-09-15 快照 9 case：根因/抑制/证据链逐条断言）
```

合成语料告警精确率 **100%（朴素基线 61.5%）**；真实回放 9/9——合成语料证明
**机制对**，真实回放证明**事实够**，缺一不可。

## 技术选型：为什么没用这些

**监控链路没用 LangGraph，分析与报告 Agent 用了**：巡检是确定性 DAG，套图是把
确定的东西变成不确定的；问数与报告 Agent 有真实回环（工具调用、验证追问、
审校重写），才轮到 LangGraph。

**归因用查表，不用 RAG**：归因要的不是"相似的文档"，是**精确的业务事实**。
相似度检索会返回"看起来像"的事实——一个像的事实比没有事实更危险，它会让
系统理直气壮地抑制掉真告警。宁可 UNKNOWN 降级，也不编一个像的。

## 诚实边界

- **与 AFAC 的关系**：只按公开题面口径对齐，**无参赛成绩、无官方关联**，
  对外不得暗示 otherwise；
- **质量侧读数有撤回记录**（见上「主动撤回过的声称」），凡小于噪声下界的
  只能写"在噪声内"；成功率侧结论只在本地 7B/3B 口径成立，token 侧可迁移；
- **合成语料的数字不外推到真实数据**，对外表述必须标注口径；
- **个股级行情归因仍是盲区**（如 2023-03 中芯国际），需公告/新闻事实源；
- 封存模块（RL 训练、prompt 自进化、LLM 归因记忆）测试照跑，不加新功能；
- 实盘 / 交易信号红线不变。

## 文档地图

| 文档 | 内容 |
| --- | --- |
| [`docs/afac-track4.md`](docs/afac-track4.md) | **当前主线唯一权威开发计划**（里程碑/停止清单/parking-lot） |
| [`docs/final-scope.md`](docs/final-scope.md) | 收敛与修订史（§6 = 赛题四对齐决定 + 3 个月方向锁） |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 历代主线论证（背景） |
| [`docs/r5.0-acceptance.md`](docs/r5.0-acceptance.md) | v4.0 实验全套口径（配对验证/噪声下界/分辨力） |
| [`docs/business-case.md`](docs/business-case.md) | 需求侧依据与诚实边界 |
| [`docs/integration.md`](docs/integration.md) | 可信增强模块接入指南 |
| [`docs/badge.md`](docs/badge.md) | 徽章四档与健康分口径 |
| [`docs/reviews/2026-09-15-attribution.md`](docs/reviews/2026-09-15-attribution.md) | 真实告警归因复盘 |
| [`PITCH.md`](PITCH.md) | 简历描述 / 电梯演讲 / 高频追问（自用，双轨） |
| [`docs/interview-qa.md`](docs/interview-qa.md) | 拷问逐条作答（自用） |
| [`examples/`](examples/) | 全部可复现归档 |

## 许可

数据来源：akshare（新浪/东财公开接口）、UCI ML Repository（CC BY 4.0）、
ModelScope 公开数据集。本项目仅供学习与研究，不构成任何投资建议；
实盘/交易信号为永久红线。
