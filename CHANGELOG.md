# Changelog

本项目遵循语义化版本（SemVer）。所有显著变更记录于此。

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
