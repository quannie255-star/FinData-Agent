# AGENTS.md — AI 会话交接说明

> 给下一个 AI 会话（或新同事）的启动文件。读完这一页 + `docs/ROADMAP.md`
> 就应该能直接开工，不需要考古聊天记录。

## 当前主赛道（2026-09-18 起，先读这段）

**Agent 训练数据的质检与过滤。** 一句话：findata 原来回答「这个数字能不能
引用」，现在也回答**「这条轨迹能不能拿去训练，以及它为什么会这样」**。

为什么切过来：10 份目标 JD 里 JD9 几乎逐条对口，且缺口真实——
Langfuse / LangSmith 做的是「记录 + 打分 + 改进 agent」，而 JD9 要的是
「从海量轨迹里筛出能喂训练的正负样本」。目标不同，不是同一件事的竞争。
完整论证与里程碑见 `docs/ROADMAP.md` 顶部重定向节与 Phase R4。

核心命题没变，还是「形态相同、结论相反」：
- 数据域：停牌缺行 vs 上游漏采
- **轨迹域：表面成功但路径错误（蒙对）vs 真成功；表面失败但归因于环境
  （沙箱超时）vs 真能力不足**

新代码一律进 `src/findata/agentops/`；A 股降为**领域包 1**（保留，作为
「这套归因不是只会一件事」的证据），金融侧封存模块继续不动。

**已达成闭环（真实数据，2026-09-18）**：`scripts/run_trust_filter.py` 一条
命令跑完「拉公开 tool-calling 语料 → 归一化 → 探针 + 归因 → 过滤出可训练
样本」。语料是 `Salesforce/xlam-function-calling-60k`（ModelScope，96MB，
ijson 流式），**没有一条是我们自己编的**——自造样本证明不了自己的规则。
20000 条样本 / 31691 次调用，检出 354 条（1.77%），其中 349 条根因是
`schema_contradiction`：参数没传，但工具 description 写着"default is
8.854e-12"而 schema 没标 default——**形态上和"agent 漏填必填参数"一样，
结论完全相反，错的是数据源**。第一版就是把它全判成了 agent 的能力问题。
归档见 `examples/trust-filter-report.txt`（含「诚实说明」一节）。

## 这个项目是什么（三句话，v2.2 定位，仍成立）

1. **Findata 是可信数据基础设施**：接入数据源，产出数据分析报告，
   报告上**每个数字带可信徽章**（✓ 已核验 / ✓ 基线通过 / ⚠️ 仅借鉴 /
   ✗ 不可用），徽章可点开看归因证据链。
2. 差异化在**归因**而非检测：缺 6 行数据是漏采还是停牌，需要领域知识
   （交易日历、公司事件、截面共动性）才能判定——这是 Great Expectations
   类工具没有的东西。
3. A 股是第一个领域包（已建成），架构是**通用协议 + 领域包**，
   后续可扩展任意数据域。

## 当前状态（截至 2026-09-17）

- 监控层（探针→归因→抑制→报告）、问数层（多智能体）与可信日报（R1）已验收
  （v2.0.0，2026-09-16）：知识层上线、逐指标徽章层（`dq/badges.py` +
  `docs/badge.md`）、真实回放 golden（9/9，抑制 0/9 → 8/9，健康分
  72 → 95.8，`examples/replay-badge-report.txt`）、报告 Agent（无徽章
  数字强制拦截）。
- **定位重定向（2026-09-17，v2.1.0）**：findata 不做通用 Agent 基座，
  改做**挂到成熟 Agent 项目上的「可信增强模块」**。四个接入面已落地并
  验收：MCP `findata_trust_check`/`findata_trust_board`、HTTP `GET /v1/trust`
  与 `/v1/trust/board`、Python `service.trust_check`、可嵌入徽章芯片；
  集成契约见 `docs/integration.md`。**真实外部宿主集成演示已通过**
  （`scripts/mcp_host_demo.py`：纯 MCP 协议的通用宿主 + 宿主自己的
  DeepSeek，零 findata 导入，实测带徽章引用，归档
  `examples/mcp-host-integration.txt`）。对平台型宿主（WrenAI 等）
  只做 HTTP 松耦合门禁，不 fork 不焊接。
- **R3 通用包已落地（v2.2.0，2026-09-17）**：`src/findata/generic/`
  （5 类通用探针 + 通用归因器，**永不出 ✓ 已核验**，测试钉死）+
  `findata-trust-report` CLI + 两个数据集 demo（UCI PM2.5 真实数据 /
  脱敏电商合成）+ 对比页（`examples/domain-vs-generic-comparison.md`：
  同一数据集通用包 60 分 vs 领域包 100 分——「没查出毛病」≠「证明可靠」）。
- **R3 分发段完成（v2.2.0，2026-09-17）**：README/PITCH 已按增强模块
  定位重写（README 三组成结构，数字全部对应 examples/ 归档）；分发止损
  计划落 `docs/distribution.md`——**开发阶段到此收官**，进入 30 天止损
  观察窗：唯一指标 = 可验证的陌生宿主集成（模块内零遥测）。
- **下一步**（唯一权威来源 `docs/ROADMAP.md` 与 `docs/distribution.md`）：
  tag `v2.2.0` 发布 → 回填计时起点 → 30 天后按决策门走 A（加领域包）
  或 B（停线复盘）。开发铁律继续有效；改动抑制/徽章逻辑前先看归因复盘
  与回放 golden。
- 2026-09-15 真实数据首次考试：7/7 告警全是合法事件（停牌/行情），
  抑制 0/9，根因是知识层（corporate_event）从未上线——完整复盘与修复
  方案在 `docs/reviews/2026-09-15-attribution.md`，R1 即其落地。
  唯一残留：688981 2023-03 孤立事件（个股级行情需新闻事实源，P2 观察级）。

## 必读文档（按顺序）

1. `docs/ROADMAP.md` —— 主线计划、验收门禁、不做清单
2. `docs/reviews/2026-09-15-attribution.md` —— 真实告警归因复盘（v2.0 的起点）
3. `PITCH.md` —— 项目叙事与"能撑住追问的数字"（改数字必须对应 examples/ 归档）
4. `README.md` —— 架构与已完成里程碑（v2.0 定位重写在 R3，读时注意其口径是 v0.9 时代的）

## 开发铁律（违反任何一条的 PR 不许合入）

- **评测先行**：新能力先写验收门禁进 ROADMAP，做完打勾；评测不过不许合入
- **声明与代码一致**：文档里说的数字必须来自 `examples/` 可复现归档，不凭记忆写
- **探测与判断分离**：`dq/probes.py` 只观测，判断全在 triage 层（可整体替换为 LLM）
- **dq 层不 import agent 层**；LLM 不写 SQL（SQL 由 `semantic/` 编译器确定性生成）
- **成本如实**：多智能体/LLM 调用的 token 与延迟进评测报告，不藏
- **中文注释**，风格对齐现有代码；注释说约束，不说"这行做了什么"
- ruff line-length 100；`eval/prompts/` 的 prompt 模板与代码内模板逐字节一致

## 封存模块（不删、不改、不加功能）

| 模块 | 状态 |
| --- | --- |
| `eval/evolution.py` · `scripts/evolve_prompt.py` · `eval/prompts/` | v0.9 GEPA 进化（已验收） |
| `dq/memory.py` · `agent/llm_triage.py` · `eval/extended.py` | v0.9 记忆/LLM归因/扩充语料（已验收） |

它们是已验收里程碑与投递材料证据（PITCH 数字直接引用其归档），
测试与 CI 照常跑；新功能一律不进这些模块。

## 常用命令

```bash
uv run pytest                                  # 全量测试（CI 同款）
uv run ruff check .                            # lint

# ── 主赛道（Agent 轨迹质检）──
uv run python scripts/run_trust_filter.py --limit 20000  # **主闭环**：真实语料→质检→过滤
uv run python scripts/run_agent_trace.py       # 本机 Ollama 真实轨迹采集（48 条）
uv run python scripts/run_trustbench.py --strict  # TrustBench：SQL 正确但答案不该引用
uv run python scripts/eval_golden.py --strict  # golden 集严格门禁（含真实回放 9 case）
uv run python scripts/daily_pipeline.py --skip-ingest  # 只巡检出带徽章日报（调试）
uv run python scripts/daily_pipeline.py        # 采集→事件→巡检→推送→归档（生产）
uv run python scripts/seed_events.py           # 停牌历史种子入库（幂等）
uv run python scripts/build_replay_fixture.py  # 真实回放 fixture 固化（只在真实仓库机器跑）
uv run findata-trust-report data.csv --schema schema.yaml --strict  # 通用包：任意表出带徽章报告
uv run python scripts/compare_domain_vs_generic.py  # 通用包 vs 领域包对比页（需真实仓库）
uv run python scripts/attribute_alerts.py      # 真实告警归因复盘复现
uv run python scripts/ingest_finance.py --full --events  # 全量+公司事件采集
uv run python scripts/run_trustbench.py --strict  # TrustBench：SQL 正确但答案不该引用
uv run python scripts/run_parser_eval.py          # 解析层评测（规则，离线）
uv run python scripts/run_parser_eval.py --llm    # + 本机 Ollama（默认 qwen2.5:3b）
uv run python scripts/chatbi_demo.py --llm --show-cost "..."  # ChatBI 自然语言问答
```

## 目录地图（只列关键路径）

```
src/findata/
  dq/          探针+归因+日历+徽章（v2.0 引擎核心；badges.py 逐指标徽章+健康分）
  report/      巡检→路由→渲染→推送（徽章渲染、知识层状态行）
  agent/       Supervisor + nodes 多智能体（nodes/report.py 报告 Agent：分析/viz/审校）
  semantic/    指标语义层 + SQL 编译器
  core/ domains/finance/  DuckDB 仓库 + akshare 采集（--events/--events-only；seeds.py 停牌种子）
  generic/     通用数据可信包（v2.2：任意 csv/excel/duckdb/sqlite → 带徽章报告）
  mcp/         MCP 工具（v2.1 已挂 trust_check/trust_board，增强模块接口）
  eval/  → ../eval/        合成语料/故障注入/门禁（fixtures, faults, runner, golden, replay）
data/warehouse.duckdb      真实仓库（25 标的，2022-01~今，gitignore）
eval/fixtures/replay_20260915/  真实回放 fixture（入库！由 build_replay_fixture.py 固化）
reports/daily/             日报产物（gitignore，每次重新生成）
docs/reviews/              真实事件复盘（入库！与 reports/ 相反）
docs/badge.md              徽章与健康分口径（唯一权威定义在 dq/badges.py）
docs/integration.md        增强模块接入指南（外部宿主的三条接入纪律）
```

## 已知坑（前人踩过的，别再踩）

- `corporate_event` 曾是空表（事件采集从未跑过）→ 全部停牌抑制规则失效，
  真实告警 7/7 误报。R1.0 已修复（种子 + 事件采集常态化）；改抑制逻辑前
  仍先看归因复盘，真实回放 golden 会拦住行为漂移
- **合成语料上的指标不能外推到真实数据**（61.5%→100% 是合成口径）；
  对外表述必须标注口径
- 交易日历必须用仓库 `trading_calendar` 表（硬编码节假日只有 2024-25，
  春节全市场休市在数据里无观测，推不出来）
- duckdb 保留字：`"table"` 要加引号；日期列在 duckdb→pandas 路径是
  datetime64，与合成路径的 date 对象比较要先统一（见 `dq/triage.py::_has_event`）
- git 分支：主开发在 `master`；远端 `main` 是无共同祖先的游离分支，勿动勿删
- `reports/`、`logs/`、`data/*.duckdb` 均为 gitignore 的再生产物；
  **需要入库的分析结论放 `docs/`**

## 会话开工惯例

1. 读本文件 + ROADMAP，确认要做的 Phase 与验收门禁
2. 先写/改验收对应的测试，再实现
3. 完成后：`uv run pytest && uv run ruff check .` 全绿 →
   ROADMAP 打勾 → CHANGELOG 追加 → 有评测数字则归档 `examples/`
