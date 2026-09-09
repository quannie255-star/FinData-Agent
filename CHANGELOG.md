# Changelog

本项目遵循语义化版本（SemVer）。所有显著变更记录于此。

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
