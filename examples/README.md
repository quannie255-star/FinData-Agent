# 产出样例

`scripts/` 下三个脚本的真实输出快照。语料与观察日固定（2024-06-28），**任何人 clone 后跑出来逐字一致**——不需要外网、不需要先采集数据。

| 文件 | 怎么来的 | 看什么 |
| --- | --- | --- |
| `inspection-report.md` | `uv run python scripts/run_inspection.py` | 巡检报告全貌：健康分、告警清单、被抑制的合法事件 |
| `inspection-report.html` | 同上（HTML 版） | 浏览器直接打开，带样式的同一份报告 |
| `dashboard.html` | `uv run python scripts/build_dashboard.py` | 20 个交易日健康分趋势 + 回放 + 朴素基线对照 |
| `eval-rule-triage.txt` | `uv run python scripts/run_eval.py` | 评测门禁输出：召回 / 精确率 / 抑制率 + 不做归因的朴素基线 |
| `inspection-warehouse-2026-09-07.md` | 先 `ingest_finance.py --symbols ...` 再 `run_inspection.py --source warehouse` | **真实 akshare 数据**上的巡检：25 只标的 / 3,396 行 |
| `eval-llm-triage-real.txt` | `run_eval.py --triage both`（配真实 key） | 规则 vs LLM 归因对比：κ=0.74/0.65，LLM 在该语料上低于规则 |
| `eval-agent-single-vs-multi.txt` | `scripts/eval_agent.py --arch both`（配真实 key） | **单 vs 多智能体**同题同库对比：正确率持平 100%，token +21% 如实入账（v0.8.0） |
| `eval-prompt-evolution.txt` | `scripts/evolve_prompt.py`（配真实 key） | **prompt 进化三代对比（v0.9.0）**：进化产物在扩充语料（合法放量 vs 单位变更混淆对）上根因/抑制/精确全 100%，规则抑制率 62.5%；prompt 版本档案 `eval/prompts/` 可回滚 |
| ~~`rl-baseline.txt`~~ | 🗑 v3.0 已删 | RL 管线不服务任何目标 JD，模块与归档一并移除（代码仍可在 git 历史找回） |

### v3.0（Agent 轨迹质检 —— 当前主线）

| 文件 | 怎么来的 | 看什么 |
| --- | --- | --- |
| `trust-filter-report.txt` | `scripts/run_trust_filter.py --limit 20000` | **真实数据闭环（主证据）**：Salesforce xlam-60k 真实 tool-calling 语料 20000 条 / 31691 次调用，检出 354 条（1.77%），根因 `schema_contradiction` 349 + `step_error` 5 |
| `agent-trace-report.txt` | `scripts/run_agent_trace.py` | 本机 Ollama 真实轨迹 48 条的探针+归因报告 |
| `agent-traces.jsonl` | 同上（落盘产物） | 轨迹 JSONL 格式样例（形状对齐 OpenAI `tool_calls`） |
| `parser-eval-report.txt` | `scripts/run_parser_eval.py` | 问句解析层评测：主集 + 长尾变体集（含应拒绝题） |
| `parser-eval-qwen3b.txt` | 同上（换 qwen2.5:3b） | 换模型后同一套评测的对照 |
| `trustbench-report.txt` | `scripts/run_trustbench.py` | 可信层基准对照 |

### v2.0 – v2.2（可信日报 / 增强模块 / 通用包）

| 文件 | 怎么来的 | 看什么 |
| --- | --- | --- |
| `replay-badge-report.txt` | `findata/eval/replay.py`（fixture：`eval/fixtures/replay_20260915/`） | **真实回放 golden（v2.0 R1）**：2026-09-15 真实考试抑制 0/9 → 修复后 8/9，健康分 72 → 95.8；9 case 进 `eval_golden.py --strict` |
| `daily-badge-report-2026-09-15.md` | `scripts/daily_pipeline.py --skip-ingest` | **带徽章日报**：知识层状态行、12 项逐列徽章+证据链、报告 Agent 解读（真实 LLM，710/472 字符，审校 0 拦截） |
| `trust-module-integration.txt` | 三个接入面实测脚本 | **增强模块接口归档（v2.1）**：MCP `trust_check`/`trust_board` 与 HTTP `/v1/trust` 响应逐字节一致；close=✓ 已核验、volume=⚠️ 仅借鉴 |
| `mcp-host-integration.txt` | `scripts/mcp_host_demo.py`（真实 key） | **真实外部宿主集成**：对 findata 零导入的通用宿主，经标准 MCP 协议发现 9 工具 → 查数 → 过 trust 门禁 → 带徽章引用 |
| `generic-trust-report-air-quality.md/.html` | `findata-trust-report`（UCI Beijing PM2.5，真实公开数据 CC BY 4.0） | **通用包（v2.2）**：真实数据停更 4917 天被新鲜度拦成 ⚠️，健康分 50 |
| `generic-trust-report-ecommerce.md/.html` | `findata-trust-report`（脱敏电商合成 fixture，seed 可复现） | 通用包：主键重复 → ✗ 不可用；组内金额水平迁移 → ⚠️ 仅借鉴 |
| `domain-vs-generic-comparison.md` | `scripts/compare_domain_vs_generic.py`（需真实仓库） | **对比页（含金量自检）**：同一 stock_daily 2025 切片，通用包 60 分/8 列 ⚠️/11 信号无法归因 vs 领域包 100 分/8 列 ✓ 已核验/停牌归因 |

## 最该看的一行

`eval-rule-triage.txt` 里：

```
告警精确率     100.0%  (7/7)
  其中朴素基线  61.5%（不做归因、全部告警）
```

同一批信号，不做归因时精确率只有 61.5%。这 38.5 个百分点的差，就是归因层存在的全部理由——
也是这个项目真正的门槛所在：检测不难，难的是判断该不该报。

## 另一个该看的地方

巡检报告里 13 条信号、最后只留 8 条告警，被抑制的 5 条是停牌 / 除权除息 / 新股这类
**合法业务事件**。它们和真实故障在形态上完全一致——`upstream_stale` 那条的解释写着
"无停牌/新股可解释，判定为上游停更"，前半个分句才是难点：要先证明它**不是**那几种合法情况。

## 真实数据上跑出来的三个边界（诚实记录）

`inspection-warehouse-2026-09-07.md` 是真实 akshare 数据（非合成语料）的巡检结果：
25 只标的 / 3,396 行，**健康分 92，信号 3 → 告警 2，抑制 0**。

### 已修：春节被报成"缺失行情"（3 条误报）

第一版真实巡检是健康分 84、4 条告警，其中 **3 条 `missing_rows` 全部落在 2022 年春节**
（2022-01-31..2022-02-04）。根因：硬编码节假日表只覆盖 2024-2025。

`with_observed` 能补"数据里出现过的日子"，但**春节是全市场同时休市**——那些日子
根本没有观测，从数据里永远推不出来。节假日是查表问题，不是推断问题。
接入 `trading_calendar` 表（akshare `tool_trade_date_hist_sina`，1990 至今 8797 天）后：

| | 接入前 | 接入后 |
| --- | --- | --- |
| 健康分 | 84.0 | **92.0** |
| 信号 | 57 | **3** |
| 告警 | 4（3 条春节误报） | **2** |

消掉的是 54 条"非交易日缺行"噪声。剩下 2 条经核查**都是真信号**：600030 在
2022-01-19..2022-01-26 缺 6 个交易日，而同窗口 600519 / 000858 都有 6 天数据——
个别缺失而非集体休市，判"回补漏采"成立。

### 已修（v2.0 R1）：抑制率在真实数据上是 0

以下为 v0.9 时代的历史记录，问题已在 v2.0 解决——4 笔公告级核实的历史停牌
种子入库（`domains/finance/seeds.py`）+ 真实回放 golden 进 CI，抑制 0/9 → 8/9
（见上文 `replay-badge-report.txt` 与 `docs/reviews/2026-09-15-attribution.md`）。

<details><summary>历史记录（v0.9 口径，保留作证据）</summary>

`corporate_event` 现在有 553 条真实除权除息记录（覆盖 24 只，1995–2026），
但**告警窗口内一条事件都没落上**，所以抑制仍是 0。停牌接口
（`stock_zh_a_stop_em`）只返回"此刻谁停着"的快照、没有历史起止日，
对历史回溯帮助有限。

结论要说清楚：**合成语料里 100% 的精确率是设计出来的对照，真实数据上归因层的
抑制能力尚未被触发**。缺的不是算法，是停牌历史这个事实来源。

</details>

### 已修（v2.0 R1）：成交量漂移分不清"单位变更"和"市场放量"

以下为 v0.9 时代的历史记录。v2.0 的正解不是换更好的阈值，是**换证据源**：
漂移归因先查截面共动（同窗全市场放量分布 + 指数量能 + 板块同伴，全部仓库自证），
上文的 `600030 2023-07` 行情窗口现被归因为 `benign_market_event` 并抑制；
合成语料门禁零回归。唯一残留：中芯国际 2023-03 的**个股级**孤立行情
（截面判据不命中），待公告/新闻事实源，按 P2 观察级跟踪。

<details><summary>历史记录（v0.9 口径，保留作证据）</summary>

另一条告警是 600030 在 2023-07-25..2023-08-07 成交量放大 5.40 倍。核查后这是
**合法市场行为**——同期价格上涨 14.8%（2023 年 7 月"活跃资本市场"政策行情，券商放量）。

试过加归因规则区分，失败了，过程值得记：探针算的是"10 日均量 / 前 30 日均量"，
把评测语料注入的 20x 稀释成 **5.14x**，和真实放量的 **5.40x 数值上几乎重合**；
价格判据同样无效（合成语料的收盘价本就是随机漫步，10 日涨跌 10% 很常见）。
硬塞规则会让根因准确率从 100% 掉到 85.7%、评测门禁挂掉——
**为了修一条告警牺牲门禁是负收益，所以撤回，留作已知局限**。

</details>

**v0.9.0 更新（合成语料侧已破）**：M13 扩充语料把这对形态复现为「合法放量脉冲 vs
单位变更故障」的混淆对（探针比值区间重叠，阈值规则数学上无缝隙），并给证据层
补了两个确定性事实：**量额一致性**（单位变更只放大 volume，量/额比值跳 ~20 倍；
合法放量量额同涨，比值不变）与**市场共振**（同窗口多标的同时放量）。
进化出的 prompt（`eval/prompts/triage_v2.yaml`）凭这两条事实在扩充语料上做到
根因/抑制/精确全 100%（规则引擎抑制率 62.5%），见 `eval-prompt-evolution.txt`。
**真实数据侧的结论不变**：600030 那条 2023-07 的放量要坐实「合法」，仍需
外部事实（公告/行情背景）——合成语料证明的是判别方法可行，不是真实数据已接入。

---

把边界写在这里，是因为**知道自己的边界比假装没有边界重要**。被问"真实数据上跑过吗"，
能说清为什么会误报、为什么抑制是 0、试过什么没成功，比报一个"100%"可信得多。
