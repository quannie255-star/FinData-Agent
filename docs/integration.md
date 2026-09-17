# findata 作为可信增强模块 · 接入指南（v2.1）

> 定位（2026-09-17 维护者决策）：findata **不做通用 Agent 基座**，
> 而是作为「可信数据增强模块」挂载到成熟的 Agent 项目上。
> 宿主负责对话与编排，findata 负责「这个数字能不能引用、凭什么」。
> 实测证据：`examples/trust-module-integration.txt`；
> 徽章语义唯一权威定义：`dq/badges.py` 与 `docs/badge.md`。

## 一、为什么是增强模块而不是基座

对话编排、工具调用、多智能体规划已经是充分竞争的成熟能力（各家的
Agent 框架、GenBI 平台、临床 Agent 都在做），重造没有增量。findata
手里别处没有的东西是：

1. **归因证据链**：缺 6 行是停牌还是漏采、量能 5x 是行情还是故障——
   领域知识归因，输出四档徽章；
2. **无徽章不引用的强制闸**：宿主 Agent 引用任何数字前必须过
   `trust_check`，`usable=false` 不得作为已核验引用；
3. **评测门禁方法论**：合成语料证明机制对、真实回放 golden 证明事实够
   （`eval/golden/replay.yaml` 9/9 进 CI）。

这些能力与宿主是**正交**的：宿主越成熟，findata 的增强价值越纯粹。

## 二、三种接入面（同一份 service 实现，不许两套口径）

| 接入面 | 适合的宿主 | 入口 |
| --- | --- | --- |
| **MCP 工具** | 任何 MCP host（Claude Desktop / Cursor / Cline / 自建 LangGraph Agent 等） | `findata-mcp`（stdio），工具 `findata_trust_check` / `findata_trust_board` |
| **HTTP API** | 只能出网调 REST 的服务（GenBI 平台、低代码平台、其他团队的后端） | `GET /v1/trust?table=&metric=` · `GET /v1/trust/board` |
| **Python 进程内** | 宿主是 Python 且接受直接依赖 | `from findata.service import trust_check, trust_board` |

另附**徽章芯片组件**（`dq/badges.py::badge_chip_html`）：内联样式的
HTML `<span>`，宿主渲染报告时贴在数字旁边，悬停即证据摘要，不依赖
宿主样式表。

### MCP 接入（推荐起点，零协议开发）

宿主的 MCP 配置里加一个 server（以 Claude Desktop 为例）：

```json
{
  "mcpServers": {
    "findata": {
      "command": "uv",
      "args": ["run", "findata-mcp"],
      "cwd": "/path/to/FinData-Agent"
    }
  }
}
```

### HTTP 接入（GenBI 平台/外部服务）

```bash
# 单指标：引用前检查
curl 'http://localhost:8000/v1/trust?table=stock_daily&metric=close&source=duckdb'
# → {"badge":"verified","mark":"✓ 已核验","usable":true,"evidence":[...],"health_score":95.8}

# 全板：会话开始拉一次
curl 'http://localhost:8000/v1/trust/board'
```

### Python 进程内

```python
from findata.service import trust_check, trust_board

board = trust_board(source="duckdb")          # 会话开始：拉全板
badge = trust_check("stock_daily", "close")   # 引用前：单点检查
if not badge["usable"]:
    ...  # ⚠️ 仅借鉴：降级表述；✗ 不可用：拒绝引用并说明根因
```

## 三、集成契约（宿主 Agent 必须遵守的三条）

1. **会话开始**调 `trust_board` 拉一次全板（12 项指标徽章 + 健康分），
   之后批量引用本地对照，不必逐个查；
2. **引用任何数字前**查 `trust_check`：
   - `usable=true`（✓ 已核验 / ✓ 基线通过）→ 可引用，徽章随答案返回给读者；
   - `usable=false` 且 badge=caution（⚠️ 仅借鉴）→ 只能带疑点表述引用，
     疑点描述（`summary` 字段）必须一起给出；
   - `usable=false` 且 badge=unusable（✗ 不可用）→ 拒绝引用，转述根因
     与建议动作（`evidence` 字段）；
3. **未知指标自愈**：`trust_check` 对未知键返回 error + 全部可用键，
   宿主应据此修正引用键而不是放弃检查。

参照实现（规则版审校，可直接抄）：`agent/nodes/report.py::review_and_enforce`
——findata 自己的报告 Agent 就是这样消费徽章板的，宿主照搬即可。

## 四、与具体基座的对接说明

- **MCP host 类**（Claude Desktop、Cursor、自建 LangGraph/LangChain Agent）：
  零开发，配置即用；`findata_ask` / `findata_metric_query` 顺带提供
  可信问数能力（带 run_id 溯源）。
- **GenBI / 问数平台类**（如 WrenAI）：平台侧不需要感知 findata 内部——
  在平台产出答案的引用环节调 `/v1/trust` 做门禁即可（HTTP 面）。
  findata 不 fork 平台代码、不依赖平台发版；平台升级与 findata 升级解耦。
- **医疗 / 其他领域 Agent**：接入契约是领域无关的——宿主先部署自己的
  领域包（探针 + 归因知识，参照 `domains/finance/` 的结构），徽章协议
  与接入面原样复用。领域包怎么做，见 `docs/ROADMAP.md` 的通用包规划。

## 五、诚实边界

- `trust_check` 的回答与巡检同源（每次调用跑一遍巡检，真实仓库约 3s）：
  高频引用请用 `trust_board` 会话级缓存，不要逐数字轮询；
- ✓ 基线通过 ≠ ✓ 已核验：前者只是通用检查没查出毛病，后者有领域事实
  背书，宿主对两者的表述必须区分（`mark` 字段直接可用）；
- 健康分口径（`docs/badge.md`）：徽章聚合，✓=1.0 / ⚠️=0.5 / ✗=0，
  只对已观测指标发徽章；
- findata 不做预测性声明（"这数据以后会坏"不归我们说）。
