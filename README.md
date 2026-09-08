# Findata — 可信数据分析 Agent

> 让 Agent 报出的每一个数字都可验证、可溯源。

**Findata** 是一个可信数据分析 Agent 框架：LLM 负责理解意图与规划，语义指标层负责确定性生成查询，验证层负责交叉核验，评测层负责持续质量门禁。当前以 **A 股金融数据** 作为首个垂直域（数据来自 akshare，存于 DuckDB）。

## 为什么做这个

通用 Text2SQL Agent 的裸准确率约 60%，生产不可用；现有开源方案（Vanna/WrenAI/DeepBI）都没有解决两个问题：

1. **数字可信性** — Agent 给出的数字无法验证、无法溯源；
2. **质量可迭代** — 改一版 prompt，系统是变好还是变坏无从知晓。

Findata 的回答是四层机制：**指标语义层**（LLM 不写裸 SQL，选择/组合预定义指标，由编译器确定生成 SQL）、**验证器**（关键数字用第二条独立路径交叉核验）、**引用绑定**（答案中每个数字关联指标定义与数据血缘 run_id）、**评测门禁**（golden set + CI 回归）。

## 架构

```
接口层    FastAPI(REST + SSE 流式) / CLI
─────────────────────────────────────────────
Agent 层  LangGraph：解析 → 选指标 → 调工具 → 报告(带溯源)
─────────────────────────────────────────────
可信层    验证器(交叉核验) · 引用绑定 · 置信标注
─────────────────────────────────────────────
语义层    指标字典(YAML) → 确定性 SQL 编译器(防注入)
─────────────────────────────────────────────
工具层    metric_query / list_metrics（白名单，LLM 不可越界）
─────────────────────────────────────────────
数据层    域插件：金融(A股行情/估值/指数, akshare→DuckDB) · 可扩展
─────────────────────────────────────────────
评测层    golden set · 数值门禁 · CI 质量门禁
```

## 快速开始

```bash
uv sync
cp .env.example .env          # 填入 LLM API key（国内 OpenAI 兼容接口均可）

uv run python scripts/ingest_finance.py   # 采集数据到 data/warehouse.duckdb（约 2-3 分钟）
uv run pytest                             # 运行单元测试 + 评测结构校验
uv run python scripts/eval_golden.py      # 跑 golden set 评测门禁
```

### 语义层查询演示（M2）

```bash
uv run python scripts/query_demo.py --list                          # 列出所有指标
uv run python scripts/query_demo.py close_price --symbol 600519     # 最新收盘价
uv run python scripts/query_demo.py pct_change \
    --symbol 600519 --start 2024-01-01 --end 2024-12-31             # 区间涨跌幅
uv run python scripts/query_demo.py valuation_pe_ttm --symbol 600519
```

每个答案都带 `run_id`，可追溯到 `query_run` 表的 SQL 与参数 —— 这是"数字可信"的最小闭环。

### 交叉验证演示（M4）

每个指标可在字典里声明第二条独立验证口径。查询时自动跑双路径，结果一致标 `✓ 已交叉验证`，
不一致标 `⚠ 交叉验证不一致` 并注明两路径差异 —— 这是"可信"叙事的差异化卖点。

```bash
uv run python scripts/query_demo.py close_price --symbol 600519
# 输出含：✓ 已交叉验证（独立口径一致）[run_id=...]
```

### Agent 对话（M3）

```bash
uv run python scripts/agent_demo.py "贵州茅台最新收盘价是多少"
uv run python scripts/agent_demo.py "600519 今年涨了多少"
```

Agent 由 LangGraph 编排：LLM 只负责「选指标 + 填参数」，SQL 由语义层确定性生成，
数字经 `metric_query` 返回时自带 `run_id`，最终答案强制携带溯源与验证标记。

### 评测门禁（M5）

`eval/golden.yaml` 是一组「已知输入 + 钉死数值 + 验证状态」的回归集。CI 每次构建都会
跑它，任何数值漂移都会被挡在 merge 之前 —— 直接回应"数字是自己造的靶子"的质疑。

```bash
uv run python scripts/eval_golden.py --strict     # 任何偏差即非零退出
```

### HTTP 服务 / 产品化（M6）

```bash
# 方式一：直接起
uv run python scripts/serve.py
#   首次启动若仓库为空会自动灌入 3 只股票的演示数据
#   打开 http://localhost:8000/docs 交互

# 方式二：Docker 一键起
docker compose up --build
#   打开 http://localhost:8000/docs
```

接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health`        | 健康检查 |
| GET  | `/metrics`       | 列出全部可查指标 |
| POST | `/metric`        | 直查指标 `{metric, params}`，返回数值 + run_id + 验证 |
| POST | `/agent`         | 自然语言问答 `{question}`，返回带溯源答案（需 LLM key） |
| POST | `/agent/stream`  | 同上，SSE 流式返回 token |
| GET  | `/trace/{run_id}`| 按 run_id 回溯查询血缘（SQL + 参数 + 交叉验证结论） |

示例：

```bash
curl -X POST http://localhost:8000/metric \
  -H "Content-Type: application/json" \
  -d '{"metric":"close_price","params":{"symbol":"600519"}}'

curl http://localhost:8000/trace/<上一步返回的 run_id>
```

## 路线图（已全部落地）

- [x] M1 数据层：DuckDB 仓库 + akshare 采集（幂等/增量/血缘）
- [x] M2 语义层：指标字典(YAML) + 确定性 SQL 编译器(防注入) + `metric_query` 工具 + 溯源（3 个指标最小闭环）
- [x] M3 Agent 编排：LangGraph（解析 → 选指标 → 调工具 → 报告带溯源），工具白名单
- [x] M4 可信层：交叉验证器（双路径口径）+ 验证血缘表 + 答案标记
- [x] M5 评测体系：golden set 数值门禁 + CI 回归 + 合成 fixture
- [x] M6 产品化：FastAPI(REST/SSE) + Docker/Docker Compose 一键部署

## License

MIT
