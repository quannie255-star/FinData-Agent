# Findata — 金融长文本智能问答系统（检索 · 判定 · 记忆 · 归因 · 验证全链）

[![test](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/test.yml)
[![release](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)](https://github.com/quannie255-star/FinData-Agent/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![tests](https://img.shields.io/badge/tests-701-brightgreen)]()

> **定位（2026-10-07）**：面向金融科技 / 大模型应用岗的**求职作品**。问题场景
> 与评测口径借用 **AFAC2026 赛题四**（金融长文本 Agent 的动态记忆压缩与高效
> 问答，出题方复旦大学）的公开题面——该比赛已于 2026-08-26 收官，本仓库
> **无任何参赛成绩或官方关联**。差异化不在「做了个问答系统」，在**每一步
> 优化都有失败证据、根因分析和配对验证**（见下方迭代链）。

**一句话**：从证监会法规到 193 页债券募集说明书，长金融文档问答 = 检索给证据、
判定做裁决、记忆管成本；本系统四层全链自研（701 例测试），并在自建五域评测集上
把「准确率 - token」曲线的每个拐点都归因到了具体机制。

## 当前读数（自建五域评测集：50 题 / 7 文档 / 737K 字符官方原文，qwen-plus）

| 臂 | 准确率 | token | 说明 |
| --- | --- | ---: | --- |
| 全文直入（官方 baseline 口径的本地复刻） | 90% | 188K* | *31 题口径 |
| BM25 逐选项检索 | 95% | 62K | 31 题口径 |
| 选项级证据隔离（**当前最强**） | **98% → 隐含 100%**† | 453K | †43 A 榜 + 7 B 榜（两阶段检索复测）|
| 滚动摘要记忆（朴素压缩基线） | 77.4 ± 3.2%（n=3） | 216K | 压缩的代价基线 |
| 分层记忆（数值/条款逐字 + 叙述压缩） | 96% | 612K | 恢复 19pp，验证「不可逆信息不动」判据 |

配对验证配套：k_option 阶梯（4/3/2 同题配对，−28% token 与 −1 题的分辨力
边界）、噪声下界（长链架构放大非确定性的实证）、evidence_retrieval 导出
（B 榜审核格式）。全部归档于 [`examples/`](examples/)，复现命令见
[`eval/fixtures/afac_scaffold/README.md`](eval/fixtures/afac_scaffold/README.md)。

## 错题形态迭代链——本项目的核心资产

每一代修好上一代、暴露下一代，全部有归档读数与回归测试：

```
漏证据（召回不足）      → 逐选项检索（官方推荐流程背书）
过包含（跨选项干扰）    → 选项级证据隔离 + 三态判定
集合边界丢失（真≠该选） → 判定语义改「应选入本题答案」
表格召回（散文词频压制表格标签）→ BM25F 双通道字段加权
前言污染（来源块挤占证据）      → 前言块出索引 + 生成器修正
B 榜挤占（长文档挤掉目标域）    → 两阶段检索（文档级粗筛 top-1）
```

三次「走错又折返」同样留档：token 注入式加权被 BM25 长度归一化反噬（改双索引
线性组合）；复审批量翻错（改单向升级 UNCERTAIN→TRUE）；验收测试与生产配置
分叉导致假通过（修测试对齐二级切分）。

## 架构（`src/findata/finqa/`，701 例测试全绿）

```
题面/评分     scoring.py    题面公式逐字实现（多选无部分分、TokenScore 截断）
检索          retrieval.py  条款级分块 + 二级切分 + BM25F 双通道 + 两阶段 + 限定检索
判定          isolate.py    选项级证据隔离：三态判定 + 确定性拼装 + 单向复审 + 回退仲裁
记忆          memory.py     朴素滚动摘要 / 分层压缩（逐字层 + 叙述层）双臂
归因          attribution.py 引用观测：片段级/记忆行级使用率（防幻觉引用虚增）
提交          submit.py     answer.csv 双 schema（题面两种口径并存）
```

硬约束在代码里：token 台账覆盖全部调用（接口不返回 usage 即抛错，禁止估算）；
付费 API 不进 CI；逐配置确定解码（temperature=0 + seed 固定）。

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

### 5 分钟用你自己的数据试（离线，无 API key）

```bash
git clone https://github.com/quannie255-star/FinData-Agent.git && cd FinData-Agent
uv sync

# 用任何一份你手头的 CSV（订单表 / 传感器数据 / 财务导出都行）：
uv run findata-trust-report your_data.csv -o report.md --html report.html
```

没有 schema 声明也能跑（通用包自动推断，检查降级会在报告里显式列出）；
报告给出每列徽章（✓ 基线通过 / ⚠️ 仅借鉴 / ✗ 不可用）与健康分。
想看「加了领域知识会多出什么」，开
[`examples/domain-vs-generic-comparison.md`](examples/domain-vs-generic-comparison.md)
对比同一份数据的两种读数。

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
CI: lint → pytest(701, py3.11/3.12) → e2e smoke → eval-gate → ci-gate
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
