# Changelog

本项目遵循语义化版本（SemVer）。所有显著变更记录于此。

## [Unreleased]

### v3.0 · 主线切到「Agent 训练数据质检」+ 真实数据闭环 + 清理（2026-09-18）

主赛道由「可信数据增强模块」切到 **Agent 训练数据的质检与过滤**（依据
`docs/jd-coverage.md`：10 份目标 JD 里 C 类 Agent/LLM 岗占 5 席，而
「Agent 轨迹 / Tool Calling 失败归因」此前是零）。核心命题不变，仍是
「形态相同、结论相反」，只是从数据域换到轨迹域。

- **真实数据闭环（主证据）** `scripts/run_trust_filter.py` +
  `src/findata/agentops/adapters/xlam.py`
  - 语料 `Salesforce/xlam-function-calling-60k`（ModelScope，96MB），
    **零自造样本**；`ijson` 流式读取（整份 `json.load` 会撑爆开发环境）
  - 20000 条 / 31691 次调用 → 检出 354 条（1.77%）；根因
    `schema_contradiction` 349 + `step_error` 5
  - **349 条「必填参数没传」经核查全不是 agent 漏填**，而是工具 description
    写着「default is 8.854e-12」、schema 却没标 default。第一版全判成
    `param_error`（建议"换更大的模型"）——方向完全反
  - 新增根因 `CAT_SCHEMA_CONTRADICTION`，**排在硬错误分类之前**
- **误报防线**（两处，都是真实数据打脸后修的）
  - `probe_retry_storm`：判据由「同名」改为「同名**且同参数**」，阈值 3→2。
    同一工具带不同参数并行调用是常态，按名字计数在 xlam 上误报 53 条
  - `inspect_call`：同名工具在注册表里重复注册且 schema 不同（该数据集
    确实有 `time_series` / `web_search` 各两份），改为「任一份 schema 能
    解释这次调用就放行」，否则误报 45 条 `unknown argument`
- **合成管线作废**：`agentops/synth.py` 与 `scripts/run_synth_eval.py` 删除。
  合成集 100% → 从未参与调参的 held-out 集 63.6%，证明自造样本测不出
  泛化；且真实数据无根因标签，准确率无从计算——**R4.1 的「归因准确率 ≥90%」
  门禁刻意保留红灯**，不偷偷划掉
- **清理**（依据：不服务主赛道就删；代码全在 git 历史，可 `git show` 找回）
  - `legacy/`、`findata-agent/`：早期原型与本地笔记
  - `src/findata/rl/` + `scripts/rl_pipeline.py` + `docs/rl-experiment.md`
    + `tests/test_rl_*.py`：无 GPU 训练，且 10 份 JD 无一条要 RL
  - `eval/rl_runs/`、`examples/rl-baseline.txt`、`examples/synth-*`：上述产物
- **产物落盘**（补上一个"声明与代码不一致"的洞）
  `src/findata/agentops/export.py`：此前脚本只打印统计，但 README 写着
  「吐出哪些能喂训练」——声明与代码不一致，违反本项目铁律。现按四档处置
  分文件流式写 JSONL（`train/review/discard/not_failure`）+ `manifest.json`，
  OpenAI `messages` + `tool_calls` 形状（`arguments` 是 **JSON 字符串**，
  写成对象会让下游整批加载失败）。判定挂在样本的 `findata` 字段一起走，
  不另存对照表。新增 `tests/test_agentops_export.py`（5 例）
- **测试 389 → 341**（删掉的是 RL 与合成相关），新增
  `tests/test_xlam_adapter.py`（11 例）+ `test_agentops_export.py`（5 例）；
  全量 + ruff 全绿

### R4.4 沙箱执行环境（进程级，2026-09-18）

`src/findata/agentops/sandbox.py` + `scripts/run_sandbox_probe.py` +
`tests/test_sandbox.py`（7 例）。真起子进程、真跑到阈值被杀，归因器拿到的
是 `subprocess.TimeoutExpired` 的**原文**——不是在字符串里写个 "timeout"。

- 三类真实信号 → 归因 → 处置：`add` → ok / ✓ 正样本；
  `divide` → `param_error` / ✗ 丢弃；`spin` → `env_timeout` / ⊘ 不算失败。
  **同一类"跑失败"，结论相反**——这条之前只是写在文档里的主张
- 此前 `env_timeout` 在 held-out 上判对 0/20，因为真实报错写的是
  "deadline exceeded" 这类文本。沙箱给的是真信号，不是关键词
- 踩坑：第一版给所有工具统一设 0.5s，`divide` 明明立刻抛异常却被记成
  timeout——**子进程冷启动就要 ~0.4s**，阈值必须留启动余量

**边界如实写，不吹**：进程级隔离**不是安全沙箱**，防不住恶意代码
（容器级本机做不了，拉不到 `registry-1.docker.io`）；超时是主动设阈值
触发的，不代表生产环境超时频率；gRPC 的 `deadline exceeded` **仍然
识别不了**，是已知局限。

### ChatBI 增强 · 解析层评测与护栏加固（2026-09-17）

接 Ollama 实跑后新增解析层评测，并加固了两条路径共有的槽位护栏。

- **解析层评测** `scripts/run_parser_eval.py` + `eval/golden/parser_variants.yaml`
  - 只拿 TrustBench 的 17 题比「规则 vs LLM」是**自己送分**：那些问句是脚本
    模板生成的，规则解析的正则就是照模板写的。补 6 道长尾问法（intent 同义
    表述「表现如何」「单日成交均值」、中文数字日期、量词「号」）+ 2 道哨兵，
    让对比真正有区分度；
  - 多口径出数，不把兜底算成模型能力：slot 槽位匹配 / **模型独立答对**
    （未降级且全对）/ 降级次数 / 字符成本 / **badge 下游徽章与 golden 一致**。
    最后一条才是决定性的——解析错一格不一定要命，改了徽章才是真事故；
  - 门禁只卡 main 组（规则满分 + 两路径徽章与 golden 一致）；variant 组是
    诊断，不预设谁该赢。
- **护栏加固**：`parse_with_llm` 新增 symbol 缺失与槽位不完整两道降级
  （price 缺 as_of、区间缺 start/end）；`engine.answer` 在拼 SQL 前拦住
  不完整的槽——此前规则解析遇「9 月 4 号」会退化成整月区间、带着
  `as_of=None` 拼出 `DATE 'None'`，报 duckdb ConversionException 这种天书。
- **实测结论**（qwen2.5:3b / 7b，本机 RTX 4060）：
  - main 组规则和 LLM 都 100%，但 LLM 降级 4–6 次（全是「只有年月」的题
    ——模型不会把月份展开成整月区间，这是项目约定不是常识，靠降级接住）；
  - variant 组规则 2/6（33.3%）、LLM 5/6（83.3%）——**长尾问法上 LLM 确实赢**；
  - 3B 与 7B 最终准确率打平，7B 只是少降级 2 次：**兜底的是护栏，不是更大的模型**。

### 主线重定向 v3.0：Agent 训练数据的质检与过滤

对照 10 份 JD 后选定主赛道。核心命题仍是「形态相同、结论相反」，换到轨迹域：
**轨迹的成功/失败是形态，能不能当训练样本是结论**（蒙对的不能当正样本、
环境导致的失败不该当负样本）。不跟 Langfuse/LangSmith 抢 tracing 与看板——
它们服务"改进 agent"，JD9 要的是"生产训练数据"。
A 股降级为领域包 1，保留作"归因不是只会一件事"的证据。

### Agent 轨迹段开工（JD 覆盖矩阵之后）

10 份目标 JD 分析落 `docs/jd-coverage.md`：三条赛道里 findata 原本两头不靠，
结论是**不重构、补一段**——补「Agent 轨迹的可信与归因」。

- **`src/findata/agentops/`**（新子包，不动任何封存模块）
  - `schema.py`：Trace / Step，**形状对齐 OpenAI tool_calls**（不自造格式——
    JD1 的硬要求就是"看懂主流大模型 API 的数据组织方式"）；JSONL 落盘 +
    生成器流式读写，1GB 文件也不吃内存；
  - `triage.py`：轨迹归因器。切的这一刀不是"失败没失败"，而是
    **loud vs silent**——loud（中断/降级）是系统知道自己不知道，安全；
    silent（链路跑完、数字出来了但口径错了或前提是编的）才危险。
- `scripts/run_agent_trace.py`：采集真实轨迹 + 出归因报告。**没有一句合成
  数据**——ChatBI 自己就是 Agent，24 题 × 2 解析器 = 48 条真实轨迹。
- **实测**：静默错 1 / 吵着失败 9 / 通顺 38。规则解析静默 0（全部以中断
  收场，loud），LLM 静默 1（编造日期）。

### 归因器抓出的三个真 bug（共同成因：没证据就下结论）

1. 模型输出 `"2025-08-1"` 个位日期，严格 `fromisoformat` 拒绝 → 误降级。
   **模型其实答对了，是我们太严**；容忍个位（无歧义 ≠ 猜测），降级 7→4。
   若不修，归因会建议"换更大模型"——方向完全错。
2. `_has_day` 用「日」字判断，「日均成交量」被当成"问句给了具体某一天"，
   把 month_expansion 错归成 missing_slots。改用 `\d\s*[日号]`。
3. **中断的轨迹被标成静默错**——一条数字都没给出来，怎么会是"静默答错"？
   判反会把"安全地拒绝"误报成"危险地答错"。判定顺序改为
   abort → mismatch → degrade。

### 新增应拒绝题 pv-07「贵州茅台 2025 年 9 月的收盘价是多少？」

模型把"9 月"编成 2025-09-04，**槽位完整、所有校验放行**，给出一个看起来
完全正常的数字。**完整性校验挡不住幻觉**：槽位确实完整，只是值是编的。
这是"停牌 vs 漏采"在 Agent 层的同构问题，也是下一步最值得做的方向。


### Phase R3 · 通用包（通用协议 + 领域包架构的"零领域知识基线包"）

交付物：**`findata trust-report <csv|db|table>`——任意数据表出带徽章的
可信报告**。徽章语义与金融域完全一致（同一聚合实现），但通用包永远
给不出「✓ 已核验」——无领域知识是它的定义性约束，测试钉死。

- **通用引擎**（`src/findata/generic/`）：
  - `schema.py` 声明式 schema（列类型/可空/主键/时间锚点/分组列/容忍度
    YAML 化）；无声明时 dtype 推断降级，且降级在报告里显式可见；
  - `probes.py` 五类通用探针：完整性（空值率 vs 声明容忍）、唯一性
    （主键重复）、新鲜度（时间锚点 vs 观察日）、离群值（组内 IQR +
    近期/历史水平迁移）、Schema 一致（缺失列/未声明列/类型漂移）。
    探测与判断分离的铁律照旧：探针只观测，判断全在 `triage.py`；
  - `triage.py` 通用归因器：**没有任何路由通向 BENIGN_***——通用包
    抑制率为 0 是特性不是缺陷，它没有资格宣称"这是合法业务事件"；
  - `engine.py` 编排 + 报告（md/html，含"通用包上限声明"与检查覆盖
    降级清单）；报告里 ✓ 基线通过的语义 = 没查出毛病，不是证明可靠。
- **CLI**：`findata-trust-report`（csv/xlsx/duckdb/sqlite；`--strict`
  门禁模式：有 ✗ 即退出 1）。
- **公开数据集 demo**（`scripts/build_generic_fixtures.py`，构建时拉取、
  fixture 入库）：
  - 真实公开数据：UCI Beijing PM2.5（CC BY 4.0，2013 切片）——
    报告把 4917 天的停更拦成 ⚠️（`examples/generic-trust-report-air-quality.md`）；
  - 确定性合成（明确标注非真实数据）：脱敏电商订单——注入的主键重复
    打成 ✗、组内金额水平迁移打成 ⚠️
    （`examples/generic-trust-report-ecommerce.md`）。
- **对比页**（R3 验收②，`examples/domain-vs-generic-comparison.md`）：
  同一 stock_daily 2025 切片喂两台引擎——通用包 60 分 / 8 列 ⚠️ /
  11 信号无法归因；领域包 100 分 / 8 列 ✓ 已核验 / 2 信号停牌归因。
  含金量自检通过：「没查出毛病」与「证明可靠」的差值就是领域包的存在理由。
- **顺手修出一个真 bug**：抽公共聚合函数时发现「最坏级别」选择写反
  （max 按"是否 P0"打分，非 P0 反而胜出）——P0 故障混排时会被 ⚠️ 掩盖。
  单一实现的价值即时兑现：一处修复，两台引擎同时受益
  （回归测试 `test_p0_dominates_mixed_findings`）。
- 测试：新增 `tests/test_generic.py` 11 例（探针/归因/诚实边界/IO/CLI），
  全量 351 例绿。
- **README/PITCH 按增强模块定位重写**：README 687 → 230 行，以"增强模块 /
  通用包 / A股领域包"三组成重排，全部数字对应 examples/ 归档；PITCH 简历
  描述、电梯演讲与 Q1/Q2/Q4/Q5 对齐 v2.2 口径（撤回改动的故事补上 v2.0
  的后续：截面共动 = 换证据源，不是换阈值）。
- **分发止损启动**：`docs/distribution.md`——launch 定义、唯一指标
  （可验证的陌生宿主集成，模块内零遥测）、人肉测量方式、30 天决策门
  （A 加领域包 / B 停线复盘）与窗口期克制清单。

## [2.1.0] - 2026-09-17

### Phase R2 · 可信增强模块（主线重定向：不做通用 Agent 基座）

**维护者决策（2026-09-17）**：从头做通用 Agent 基座是重造轮子。findata
重新定位为**挂到成熟 Agent 项目上的"可信增强模块"**——宿主负责对话
与编排，findata 负责回答「这个数字能不能引用、凭什么」。接入面做成
标准协议（MCP/HTTP），宿主换成谁都不影响增量价值；对平台型宿主
（如 WrenAI）用 HTTP 面做松耦合门禁，**不 fork、不焊接、不绑发版节奏**。
原 R2 通用包（trust-report/任意数据）延后并入 R3，详见 ROADMAP 修订。

- **MCP**：新增 `findata_trust_check(table, metric)`（四档徽章 + 证据链
  + `usable` 布尔）与 `findata_trust_board()`（会话开始拉全板）——挂在
  现有 stdio 服务上，任何 MCP host 零配置接入；未知指标错误带全部
  可用键，宿主可自愈
- **HTTP API**：`GET /v1/trust`（单指标，未知 404 带可用键）+
  `GET /v1/trust/board`——给只能调 REST 的 GenBI/平台型宿主
- **可嵌入徽章芯片**：`dq/badges.py::badge_chip_html`——内联样式
  `<span>`，宿主渲染报告时贴在数字旁，悬停即证据摘要，不依赖宿主
  样式表，summary 注入已转义（有单测）
- **集成契约**：`docs/integration.md`——宿主三条接入纪律（会话拉全板 /
  引用前检查 / 未知键自愈），参照实现是 findata 自己的审校节点
  （`agent/nodes/report.py::review_and_enforce`）
- **实测归档** `examples/trust-module-integration.txt`：真实仓库上
  close=✓ 已核验（4 笔停牌证据链）、volume=⚠️ 仅借鉴（2023-03 孤立
  行情残留疑点）、健康分 95.8；HTTP 与 MCP 响应逐字节一致
  （一份 service、两份协议，测试钉死）
- **真实外部宿主集成演示**（R2 验收收官）：`scripts/mcp_host_demo.py`
  是一个对 findata 内部**零依赖**的通用 Agent 宿主——只讲标准 MCP
  协议（官方 SDK stdio 客户端，进程隔离 JSON-RPC），运行时发现 9 个
  工具，用宿主自己的 LLM（DeepSeek）驱动工具环。真实实测：提问
  「茅台最新收盘价能否放心引用」→ metric_query 查数 → trust_check
  过门禁 → 回答逐数字带验证状态/✓ 徽章/run_id，且 LLM 正确识别出
  停牌证据属于其他标的而非茅台。transcript 归档
  `examples/mcp-host-integration.txt`；线级协议测试
  `tests/test_mcp_host.py`（握手/发现/错误契约跨协议保持）进 CI
- 测试：新增 MCP 4 例 + API 3 例 + 芯片 1 例 + 线级宿主 1 例，
  全量 340 例绿

## [2.0.0] - 2026-09-16

### Phase R1 · 可信日报（v2.0 主线第一阶段）

交付物：**自己每天真读的那份 A 股日报，每个数字带徽章**。起点是
2026-09-15 真实告警考试（7/7 全部误报、抑制 0/9，复盘见
`docs/reviews/2026-09-15-attribution.md`），R1.0/R1.2/R1.3 即其
P0a+P0d / P0b+P0c / 报告 Agent 的落地。真实数据修复后实测：
**信号 9 → 告警 1，抑制 8/9，健康分 72.0 → 95.8**（回放归档
`examples/replay-badge-report.txt`，带徽章日报样例
`examples/daily-badge-report-2026-09-15.md`）。

- **R1.0 知识层上线**（P0a+P0d）：4 笔公告级核实的历史停牌种子
  入库（`domains/finance/seeds.py`，来源链接入 detail，akshare 拿不到
  历史停牌区间，知识层的历史事实只能人工种子）；`daily_pipeline.py`
  事件采集常态化默认开、独立子进程、**失败不阻塞行情巡检**（新增
  `ingest_finance.py --events-only`）；日报新增知识层状态行
  （`corporate_event` 行数/最新事件日期），事件表为空时显式降级提示
  ——抑制能力降级必须对值班的人可见，不再无声出现。
- **R1.2 逐指标徽章层**（P0b+P0c）：
  - 新增根因 `BENIGN_MARKET_EVENT`：drift 归因先查截面共动——
    全市场 ≥ 半数标的放量 ≥2x、或指数量能 ≥1.5x、或同板块 ≥2 只同伴
    ≥2x（行业→板块映射入 triage，股票池里 600030 是唯一券商，
    只看 industry 同列板块共动永远无法命中）。阈值按真实窗口实测
    标定（924：24-25/25 ≥2x + 指数 2.2-2.8x；724：大金融 3 只同伴；
    2023-03 中芯国际孤立事件不命中）。漂移归因第一次有了"数据故障"
    之外的出口，且判据全部来自仓库自证，不需要外部数据。
  - `dq/badges.py` 徽章层：findings → (table, metric) → 四档徽章
    （✓ 已核验 / ✓ 基线通过 / ⚠️ 仅借鉴 / ✗ 不可用），行级信号覆盖全列、
    列级信号精确落列、聚合取最坏、未观测不发徽章。**健康分改由徽章
    聚合得出**（score = 100 × Σw/N，w: 1.0/1.0/0.5/0.0；告警惩罚仍用于
    排序但不再决定健康分），口径写入 `docs/badge.md`。v1.x 的
    「100−惩罚」分数与新政不可直接对比。
  - 日报 md/html 每个指标带徽章，证据链 `<details>` 可展开。
  - **真实回放 golden 进 CI 门禁**（P0c）：`scripts/build_replay_fixture.py`
    把 2026-09-15 真实仓库只读固化为 `eval/fixtures/replay_20260915/`
    （知识层由种子确定性重建，杜绝仓库漂移污染评测）；9 个 case 逐条
    断言根因/抑制/证据链 + 汇总钉死，进 `eval_golden.py --strict`
    （CI eval-gate 自动继承）。合成语料证明机制对，真实回放证明事实够。
- **R1.3 报告 Agent**：`agent/nodes/report.py` 分析 → 可视化 → 可信审校
  线性三节点（复用 Supervisor + nodes 编排模式）。分析节点有 LLM 用
  LLM、无 key/失败降级确定性模板（日报无人值守，不能因模型开天窗）；
  可视化节点确定性选图（近 30 日涨幅 top5 收盘归一化 SVG，零图表库）；
  **可信审校是纯规则**：每个数字必须带 «表.列» 引用标记，无徽章/幽灵
  引用/✗ 徽章一律拦截（v0.9 eval-gate 判官校准思想的落地）。
  `daily_pipeline.py` 一条命令出带徽章 md+html 并推送；LLM 成本
  （调用数/字符/延迟）如实写入报告脚注。实测：真实 LLM 分析
  710/472 字符、审校 0 拦截、1.6s。
- **测试**：新增 `tests/test_seeds.py`（3）、`test_badges.py`（7）、
  `test_replay_golden.py`（5）、`test_report_agent.py`（11）+
  共动归因/知识层状态用例，全量 331 例绿（R1 前 298 例）；
  合成语料门禁无回归（基础语料 100%，扩充语料抑制维持 5/8——
  v0.9 归档数字可复现，脉冲标的跨板块故规则不误伤）。
- **已知残留**（如实）：688981 2023-03 AI 行情为孤立个股事件，
  截面判据不命中，保留为漂移告警（P2 观察级，待新闻事实源，
  见复盘 P0b）。

## [0.10.0] - 2026-09-15

### M14 · Agent RL 管线（v0.10.0，无 GPU 降级：数据侧闭环）

跑通「环境 → 奖励 → 轨迹采集 → 训练数据导出 → 评测回归」数据侧完整
闭环。**权重更新（GRPO 训练）待 GPU，执行手册 `docs/rl-experiment.md`
已就绪**——按 ROADMAP 降级路径交付：环境与 reward 封装 + 实验设计文档。
v1.0.0 留给真训练完成。设计借鉴 2026-09 前沿开源（调研结论见
`docs/rl-experiment.md` 第 5 节）：环境接口取 SkyRL、数据行约定取
verl、轨迹契约取 rLLM、奖励解耦取 verifiers（Rubric 模式）、rollout
循环取 slime、接入协议取 Agent Lightning v1.0。

- **RL 环境**（`rl/env.py`）：`AttributionEnv` 把一次批式归因调用展开为
  **多轮窄 agent**——episode = 一个数据质量信号，四个工具（查证据 /
  召回记忆 / 同窗口对照 / 提交结论），提交过白名单校验（对齐
  LLMTriage 不信任防线：非法根因/UNKNOWN/空解释拒收可重试），
  `max_turns` 超时强制终止。遵守 ROADMAP「监控层不上 LangGraph」约束，
  循环自研。`AskEnv` 对应任务 B：LangGraph 多智能体图本身即策略
  （agent 零改动），环境只注入问题、回收 answer/run_ids/usage。
  同 item+ctx 观测序列逐字节一致（可重置性有单测钉死）。
- **确定性奖励**（`rl/reward.py`）：归因 = 根因对 1.0（主干）+ 抑制
  决策对 0.3 + 格式 0.1 − 超时惩罚（封顶）；问数 = 数值正确 0.6 +
  run_id 保留 0.2 + 格式 0.2 − 超轮惩罚。稠密、确定、归一化 [0,1]，
  主干放任务本质防 dense 分量被 hack。`compute_reward` 签名与 verl
  custom_reward_function 逐参数一致，有卡直接挂载。任务 B 判分与
  M12 评测共用同一把尺子（`eval/agent_judge.py`，从 scripts 下沉，
  不允许评测与奖励漂移出两套实现）。
- **任务集与轨迹**（`rl/types.py` + `rl/rollout.py`）：`RLTask` 字段
  对齐 verl 训练行约定（prompt/data_source/env_class/reward_spec/
  extra_info），任务行自包含（训练侧零环境依赖）；标注对齐复用
  `evolution.build_labeled_items`（与 M13 进化器同一份语料，不许两套
  口径）。轨迹契约（Step/Trajectory/Episode/TrajectoryGroup）对齐
  rLLM，Step.status（MSG/TOOL_CALL/FINISH）为日后 loss mask 预留。
- **数据侧管线一条命令**（`scripts/rl_pipeline.py`）：故障注入（N seed
  连续、固定 SEED0）→ 任务构建 → rollout（mock / 真实 LLM 自动探测）
  → reward 聚合（含规则基线同尺子对照锚点）→ 导出 verl JSONL +
  Agent Lightning 事件 JSONL + 完整轨迹 → eval-gate 回归门禁
  （alert_precision 不得低于朴素基线）。mock 模式零依赖跑通 CI；
  真实模式策略基线已归档 `examples/rl-baseline.txt`（31 归因 episode
  reward 均值 0.936、7 问数 episode 全对、门禁通过，DeepSeek 实测）。
- **每日运维管线落地**（`scripts/daily_pipeline.py` + `report/notify.py`）：
  采集 → 巡检 → 告警推送 → 报告归档一条命令，已注册 Windows 任务计划
  （`FindataDaily`，交易日 18:30 无人值守）。推送支持企业微信/钉钉/飞书/
  Server酱四种 webhook，推送失败绝不打断巡检；无 P0/P1 告警的日子保持
  安静。项目由此从「评测驱动的演示」进入「运维驱动的系统」。
- **诚实读数**：mock 模式 reward=0 是「无策略」的真实反映（mock 只会
  查证据不会判断），不粉饰；规则基线 reward ≈0.8-0.95 是「训练必须
  打赢的对手」的如实标定。

## [0.9.0] - 2026-09-15

### M13 · 自进化闭环（v0.9.0）

让 prompt 成为被评测、被进化、被门禁管理的资产——而不是手改手调。
三个验收门禁全部通过，实测证据：**进化后的归因 prompt 在扩充语料上
根因 100% + 抑制 100%（规则引擎抑制率 62.5%），分开了规则分不开的形态**
（对比报告 `examples/eval-prompt-evolution.txt`，真实 DeepSeek）。

- **扩充语料难例靶子**（`eval/extended.py` + `run_eval.py --corpus extended`）：
  在 7 类故障之外再造「合法放量脉冲」（3 标的同时 ×20、量额同涨同换手、
  10 个交易日），与 drift 故障（仅放大 volume）构成探针比值同分布的
  混淆对——drift 故障实测比值 5.31x 落在脉冲区间 [5.17, 7.43] 内部，
  任何阈值规则数学上无缝隙可插（`test_extended_corpus.py` 把这条
  性质钉成回归断言）。`build_evidence` 为 drift 类信号补两个确定性
  判别事实：`volume_amount_ratio_shift`（量额一致性，~1 vs ~4.6）与
  `same_window_peers`（市场共振，2 vs 0）——规则与 LLM 同源可见，
  但规则引擎一行未改，它的已知局限被如实测量（扩充语料上抑制率 5/8）。
- **Prompt 版本化与进化引擎**（`agent/prompts.py` + `eval/evolution.py`
  + `scripts/evolve_prompt.py`）：每代 prompt 是 YAML 档案（模板 + 分数 +
  出处，`eval/prompts/`，可回滚可对比）；进化 = 反射式提议（GEPA-lite，
  零依赖内置实现；`--engine dspy` 走 dspy.GEPA 可选路线）→ 训练集筛选 →
  全量 Pareto 接受 → 不回归门禁。适应度与 CI 门禁同一把尺子
  （复用 `run_evaluation` + `metrics.evaluate`），不是 LLM 自己给自己打分。
  dspy/gepa 进 `evolve` 可选依赖组，核心依赖不受影响。
- **实验迭代出的三条进化设计修正**（各有单测，是最值钱的副产品）：
  适应度只给「模型亲判且完全正确」的条目记分（否则拒收降级被规则
  兜底救回会记成成功）；Pareto 接受含模型判定率（规则兜底会掩盖模型
  真实变强）；严格改进只要求在目标语料上、对照语料只要求零回归。
- **进化实测**（v1 手写 → v1.1 契约修复 → v2 进化产物，版本档案落盘）：
  v1 的「根因 100%」是规则兜底假象（10/16 条因模型自造 "P3" 级别被
  拒收降级）；反射器五轮一致诊断出该契约缺陷，维护者确认为 v1.1；
  从 v1.1 出发第 2 轮收敛出 v2——双语料根因/抑制/精确全 100%、
  判定率 100%、零拒收，独立复测一致，eval-gate 通过后合入代码默认
  （与 `triage_v2.yaml` 逐字节一致，CI 校验）。v1 在扩充语料上抑制率
  62.5% → v2 100%（+37.5 个百分点）。全程约 170 次 LLM 调用（约 75 万
  字符往返），如实入账。
- **归因经验记忆**（`dq/memory.py` + `triage_memory` 表）：LLM 归因结论
  跨 run 结构化沉淀——两阶段写入（归因即落 pending，同指纹复现且结论
  一致才 confirm；pending 不参与召回）；召回预算（同指纹 5 条 + 跨指纹
  3 条一句话教训）；时间点过滤（`created_asof <= asof`，回放不泄漏未来）；
  冲突消解（对立标签出现则权重减半，跌破 0.5 不再召回）。**污染防护**：
  低置信（<0.6）记忆完全不进 prompt——单测断言毒化记忆下抑制决策与
  prompt 逐字节不变（M13 验收门禁 #3）。接线：`LLMTriage` 支持
  system/user 模板覆盖与 memory 注入；`triage_trust` 工具在 LLM 路径
  自动挂记忆（best-effort，打不开即退化为无记忆）。
- **难例结论如实**：ROADMAP 难例靶子问「进化后的 LLM 能否分开规则
  分不开的形态」——本轮答案是肯定的，但路径不神秘：判别力来自
  语料构造时注入证据的两个确定性事实（量额一致性 + 市场共振），
  进化的贡献是把「怎么用这些事实」固化成可执行、过了门禁的 prompt。
  规则引擎没有利用这些事实（一行未改），这是定位差异，不是能力差距
  ——规则加了同样证据同样能判对，但会重演 ROADMAP 预警过的
  「硬塞规则 100% → 85.7%」（v1.1 基线在探索期实测复现了这一点）。

## [0.8.0] - 2026-09-14

### M12 · 多智能体重构（v0.8.0）

问数分析层从单 Agent 回环重构为 Supervisor 编排的多智能体，并第一次让
监控层在**运行时**（而不只是共享 DuckDB 底座）为分析层背书。

- **Supervisor + 四个子 Agent**（`agent/supervisor.py` + `agent/nodes/`）：
  - SchemaAgent（选指标/填参，原 parse 节点平移）与 QueryAgent（语义层
    确定性执行，原 tools 节点平移）与单 Agent 共享同一套工具
    （`agent/tools.py`），对比评测不混入工具变量；
  - VerifierAgent（新增）：独立交叉验证 + 运行时拉取 DQ 上下文
    （`dq_context` 工具查 `dq_signal` 历史表）；
  - TriageAgent（复用 `dq/triage.py`）：LLM 归因包成 `triage_trust` 工具，
    供 VerifierAgent 追问"这批数据可信吗"；无 key 自动落规则归因。
  - 复杂度判断用**确定性规则**（零 LLM 成本、可全分支单测）：单指标问题
    走 direct 快路径，对比/聚合/多标的走 deep 全链路（含 VerifierAgent）。
- **溯源强制检查两道闸**：gate 节点拦"查了但没拿到 run_id"（打回补查一次）；
  post_check 终检"数据类问题的答案里见不到 run_id"（追加显式警告，不静默）。
- **DQ 信号历史**：`dq_signal` 表 + `dq/history.py`；`service.inspect`
  （duckdb 源）巡检产物幂等落库（best-effort，失败不打挂巡检；synthetic
  源不落库，防污染）。这是 M13 归因经验记忆的地基。
- **MCP 新增 `findata_ask`**：整个可信问数多智能体暴露为一个工具——
  从"有 MCP"升级为"可被外部 Agent 编排"。
- **API `/agent` 默认走多智能体**（`FINDATA_AGENT_ARCH=single` 可切回单
  Agent 对照组）；单 Agent（`agent/graph.py`）保留为对比基线，不删除。
- **agent 级评测**：`eval/golden/agent.yaml`（7 题：5 direct + 2 deep）+
  `scripts/eval_agent.py`（答案正确率 / run_id 保留率 / 工具轮数 /
  token 成本 / 端到端延迟，单 vs 多同题同库对比，判分器纯函数可单测）。
- **对比报告归档 `examples/eval-agent-single-vs-multi.txt`**（DeepSeek 实测，
  合成 fixture）：正确率 100% → 100%，run_id 保留率 100% → 100%，
  token/题 2074 → 2506（**+21%，多智能体的成本如实入账**），延迟
  3.56s → 3.20s（基本持平）。
- 修复巡检真实仓库路径的 `datetime64[us]` 与 `datetime.date` 比较崩溃
  （`dq/triage.py::_has_event`，LLM 归因证据打包必经之路）。
- 修复 `recent_signals` 历史回放缺上界（显式 asof 时会读到"未来"信号）。

### 测试

- 新增 `tests/test_multi_agent.py`（38 例：Supervisor 路由全分支、溯源
  gate 重试/放行、verifier ⇄ dq_tools 回环、triage 降级、端到端对话流）+
  `tests/test_eval_agent.py`（判分器）+ MCP `findata_ask` / API 架构切换
  用例。全量 190 例绿；`eval_golden.py --strict` 5/5 不回归。

### 对标调研落地（2026-09-14，LangGraph 官方 / Anthropic / DB-GPT / SuperSonic / WrenAI / TradingAgents / ai-hedge-fund 源码级调研）

- **分步成功率漏斗**进评测报告：路由正确率 / 查询执行成功率 / 溯源通过率
  （Anthropic 复合失败率经验：失败逐环节复利放大，端到端数字定位不了哪环在漏）；
  查询失败尝试也进 state（Verifier 可追问 + 漏斗可统计）
- **按 Agent 成本归因**（`usage_by_agent`）：实测 schema 1795 / verifier 335 /
  finalize 493 token/题——verifier 全链路只占 13%，是 effort 分档的数据依据
- **子 Agent prompt 显式硬上限**（Anthropic：不给硬规则模型会过度工程化）
- 对标结论进 README「对标开源实现」小节：workflow 优先 / LLM 不写 SQL 压低
  复合失败率（0.7% vs 14.5%）均有出处；**数据质量运行时背书确认为开源空白**
  （最近邻 OpenMetadata MCP 只是被动元数据工具）；ROADMAP M13 经验记忆
  用 TradingAgents 两阶段记忆机制（pending → 反思、tag 行、point-in-time
  防时间泄漏、原子写）细化

## [0.7.1] - 2026-09-09

### 修复（审计收敛：让声明与代码一致）

- **评测可复现性**：删除对兄弟仓库 `mm-curation-pipeline/curation-eval` 的
  环境依赖（`eval/_interop.py` 整个移除）。Cohen's κ 收敛为本地实现
  （`eval/runner.py::cohen_kappa`，教科书语义、单元测试钉死）。
  此前在干净环境 clone 后会有 2 个测试必挂。
- **Agent 图三处实质修复**（`agent/graph.py`）：
  1. parse 节点补上 `bind_tools` —— 此前真实模型发不出 tool_calls，图结构性
     到不了工具节点（旧测试用会发 tool_calls 的桩掩盖了这一点）；
  2. 工具执行后回环到 parse，LLM 可多轮补查（上限 `MAX_TOOL_ROUNDS=4`，
     防失控循环）—— README 宣称的"多轮回环"自此成真；
  3. `run_ids` 提取从"按括号切"改为正则截取，不再把验证标记混进 id。
  无 API key 时报错带可行动指引（指明 `FINDATA_LLM_API_KEY`）。
- **两处"假满分"**（对"可信"叙事最危险的失败模式）：
  1. `service.evaluate` 此前把 LLM 写死为静默 Mock，API/MCP 展示的 κ 恒为
     1.0 空洞值 —— 现配置了 `FINDATA_LLM_API_KEY` 即走真实模型，且结果通过
     `llm_backend` 字段（`mock` / `openai_compat`）如实暴露参与对比的 backend；
  2. API/MCP 巡检 `source=duckdb` 此前静默连空内存库返回 100 分 —— 现默认
     连生产仓库，仓库缺失直接 `FileNotFoundError`（报错好过假满分）。
- **MCP 补齐招牌能力**：新增 `findata_metric_query` 工具（语义层可信查询，
  带交叉验证 + run_id 溯源）；`findata_execute_metric(source=duckdb)` 接通
  真实仓库（此前 `NotImplementedError`）。README 中"execute_metric 走语义层"
  的错误声明一并更正：两者职责不同，诊断用途无交叉验证。
- **数据安全**：`scripts/build_eval_fixture.py` 拒绝生产仓库路径 ——
  fixture 第一步会清空行情表，此前误指路径已实际毁掉一次 warehouse（已
  全量重采恢复）。
- **schema 漂移**：`stock_universe.list_date` 补入 MIGRATIONS，老库自动补列
  （此前旧库再采集必崩）；评测 fixture 同步该列（修复 `test_eval_runs_on_fixture`）。
- `findata-api` 入口点指向 `main()` 函数（此前指向 ASGI 对象，一跑就 TypeError）；
  SSE 路由改用公开的 `agent.app` 属性（不再摸私有 `_app`）；
  `dq/protocol.py` 的 `Triage` Protocol 接线为 `run_evaluation` 的参数类型。

### CI

- **eval-gate job 真正落地**（`test.yml`）：合成 fixture 上跑 golden set
  （`eval_golden.py --strict`）+ 归因层质量下限（`run_eval.py --min-*`），
  并纳入 ci-gate 聚合。0.5.0 曾声称"CI 新增 eval-gate job"，当时实际只有
  一行注释 —— 本次补齐，声明自此为真。

### 文档

- README 的 MCP 工具清单更正为 6 个并区分两类工具职责；
  site 演示站文案同步（探针数 / 工具数）。
- 归档真实 LLM 对比评测（`examples/eval-llm-triage-real.txt`）：DeepSeek 实测
  根因准确率 85.7%、误报抑制 60%，低于规则引擎的 100% —— κ=0.74/0.65。
  评测体系的价值正在于此：在该语料上"规则为主、LLM 为辅"是有数据依据的
  设计决策，而不是拍脑袋。

## [0.7.0] - 2026-09-08

### 合并

- 数据质量监控（dq/）与可信问数分析（semantic/）合并为统一项目 v0.7.0：
  共享 `core/db.py` 数据底座（ingest_run / query_run / verify_run 血缘表），
  监控域与问数域各自独立成层，`service.py` 统一封装供 API 与 MCP 复用。

## [0.6.0] - 2026-09-08

### 新增（M6 产品化落地）

- FastAPI 服务 `src/findata/api/app.py`：REST + SSE 流式接口
  （`/health`、`/metrics`、`/metric`、`/agent`、`/agent/stream`、`/trace/{run_id}`）。
  所有数字带 `run_id`，`/trace` 可回追 SQL、参数与交叉验证结论。
- 服务入口 `scripts/serve.py`：首启若仓库为空自动灌入 3 只股票演示数据，保证开箱即用。
- `Dockerfile` + `docker-compose.yml`：一键构建并起服务，`docker compose up --build` 后访问
  `http://localhost:8000/docs` 即可交互；数据卷持久化避免重建丢数。
- 新增依赖 `fastapi`、`uvicorn[standard]`（开发依赖 `httpx` 用于接口测试）。

### 测试

- 新增 `tests/test_api.py`：接口冒烟（健康/指标列表/直查带 run_id/溯源/错误 4xx），6 项用例。
- 全项目 42 项用例通过。

## [0.5.0] - 2026-09-08

### 新增（M5 评测门禁落地）

- Golden set `eval/golden.yaml`：已知输入 + 钉死数值 + 验证状态，作为确定性回归基准。
- 评测脚本 `scripts/eval_golden.py`：`--strict` 下任何数值偏差即非零退出；默认跑合成 fixture。
- CI 合成 fixture `scripts/build_eval_fixture.py`：无网络环境也能确定性复现评测。
- CI 新增 `eval-gate` job：build fixture → 跑 golden set，偏差挡在 merge 前。
- 测试 `tests/test_eval.py`：golden 结构校验 + fixture 自动构建。

### 说明

- golden set 绑定合成 fixture（非真实行情），保证 CI 确定性；真实仓库数值随时间漂移，不在 golden 范围。

## [0.4.0] - 2026-09-08

### 新增（M3 Agent 编排落地）

- LangGraph 编排 `src/findata/agent/graph.py`：`parse → 条件路由 → tools → finalize`。
  LLM 只负责「选指标 + 填参数」，SQL 由语义层确定性生成；工具白名单只允许
  `metric_query` / `list_metrics`，LLM 无法越界调用或写裸 SQL。
- LLM 客户端 `src/findata/agent/llm.py`：OpenAI 兼容接口封装（DeepSeek/Qwen/GLM）。
- 对话入口 `scripts/agent_demo.py`：`uv run python scripts/agent_demo.py "茅台多少钱"`。
- 最终答案强制携带 `run_id` 溯源，`run_ids` 在 state 中收集。
- 新增依赖 `langgraph`、`langchain-openai`。

### 测试

- 新增 `tests/test_agent.py`：工具绑定、白名单、run_id 返回、图编译，共 6 项用例。
  全项目 23 项用例通过。

## [0.4.1] - 2026-09-08

### 新增（M4 可信层落地）

- 交叉验证器 `src/findata/semantic/verifier.py`：每个指标可在字典声明第二条独立验证口径，
  主/副路径各算一次并比对；一致标 `verified`，不一致标 `mismatch` 并注明差异，无口径标 `not_verifiable`。
- 验证血缘表 `verify_run`：新增 `query_run_id` 关联列，便于一次查询回追验证结论。
  老库通过 `ALTER TABLE` 迁移兼容。
- `executor.py` 在每次查询后自动触发验证，`QueryResult.verification` 携带结论；
  `format_result` 在答案后附加验证标记。
- 指标字典 `metrics.yaml`：三个指标各加独立验证口径（排序取最新 vs MAX(date) 等值匹配、区间拆分连乘、
  静态/滚动市盈同向性）。

### 测试

- 新增 `tests/test_verifier.py`：覆盖 verified / mismatch / not_verifiable / 防注入 / 血缘写入，8 项用例。

## [0.3.0] - 2026-09-08

### 新增（M2 语义层最小闭环落地）

- 指标字典 `src/findata/semantic/metrics.yaml`：3 个预定义指标
  `close_price` / `pct_change` / `valuation_pe_ttm`，每个含 SQL 模板、参数 schema、单位、数据源。
- 确定性 SQL 编译器 `compiler.py`：LLM 只产出「指标 + 参数」，SQL 按模板确定性渲染，
  全程参数绑定（`?`），从根上杜绝 SQL 注入；未知参数/类型不符/缺必填均被拒绝。
- 查询执行 `executor.py`：新增 `query_run` 血缘表，每次查询落一条带 `run_id` 的记录，
  数字可追溯到 SQL 与参数。
- MCP 工具 `tools.py`：`metric_query`（指标+参数 → 数字+run_id）、`list_metrics`。
- 可跑演示 `scripts/query_demo.py`：`uv run python scripts/query_demo.py close_price --symbol 600519`。
- 新增 `pyyaml` 依赖。

### 测试

- 新增 `tests/test_semantic.py`：覆盖指标目录、编译器防注入、参数校验、执行与溯源。

## [0.2.0] - 2026-09

### 新增（M1 数据层）

- DuckDB 仓库 + akshare 采集（幂等 upsert / 增量 / 血缘 `ingest_run`）。
- 股票池 25 只 + 3 个指数，日线 / 估值 / 指数三张表。
