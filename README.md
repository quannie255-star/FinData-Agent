# Findata — 可信数据分析 Agent

> 让 Agent 报出的每一个数字都可验证、可溯源。

**Findata** 是一个通用可信数据分析 Agent 框架：LLM 负责理解意图与规划，语义指标层负责确定性生成查询，验证层负责交叉核验，评测层负责持续质量门禁。当前以 **A 股金融数据** 作为首个垂直域（数据来自 akshare，存于 DuckDB）。

## 为什么做这个

通用 Text2SQL Agent 的裸准确率约 60%，生产不可用；现有开源方案（Vanna/WrenAI/DeepBI）都没有解决两个问题：

1. **数字可信性** — Agent 给出的数字无法验证、无法溯源；
2. **质量可迭代** — 改一版 prompt，系统是变好还是变坏无从知晓。

Findata 的回答是四层机制：**指标语义层**（LLM 不写裸 SQL，选择/组合预定义指标，由编译器确定生成 SQL）、**验证器**（关键数字用第二条独立路径交叉核验）、**引用绑定**（答案中每个数字关联指标定义与数据血缘 run_id）、**评测门禁**（golden set + 轨迹评估 + CI 回归）。

## 架构

```
接口层    FastAPI + SSE 流式 / CLI
─────────────────────────────────────────────
Agent 层  LangGraph：澄清 → 规划 → 执行 → 验证 → 报告
─────────────────────────────────────────────
可信层    验证器（交叉核验）· 引用绑定 · 置信标注
─────────────────────────────────────────────
语义层    指标字典(YAML) → 确定性 SQL 编译器
─────────────────────────────────────────────
工具层    MCP server：metric_query / chart / rag_search / validate
─────────────────────────────────────────────
数据层    域插件：金融(A股行情/估值/指数, akshare→DuckDB) · 可扩展
─────────────────────────────────────────────
评测层    golden set · 轨迹评估 · LLM-as-judge · CI 质量门禁
```

## 快速开始

```bash
uv sync
cp .env.example .env          # 填入 LLM API key（国内 OpenAI 兼容接口均可）

uv run python scripts/ingest_finance.py   # 采集数据到 data/warehouse.duckdb（约 2-3 分钟）
uv run pytest                             # 运行测试
```

## 路线图

- [x] M1 数据层：DuckDB 仓库 + akshare 采集（幂等/增量/血缘）
- [ ] M2 语义层：指标字典 + 确定性 SQL 编译 + MCP 工具
- [ ] M3 Agent 编排：LangGraph + SSE 流式 API
- [ ] M4 可信层：交叉验证 + 引用溯源
- [ ] M5 评测体系：golden set + 轨迹评估 + CI 门禁
- [ ] M6 产品化：Docker 一键部署 + 演示

## License

MIT
