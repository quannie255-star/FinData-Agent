# Launch Kit（发布材料，复制即用）

> 配合 `docs/distribution.md` 使用。launch 五项清单里"提交 MCP 目录登记 /
> 更新仓库描述"两项的现成材料都在这里。**所有对外发布动作由维护者执行**，
> 本文件只准备材料。

## 一、GitHub 仓库（复制即用）

**Description**（GitHub 仓库一句话，≤350 字符）：

```text
可信数据增强模块：挂到任何 Agent 上的四档可信徽章（✓ 已核验/✓ 基线通过/⚠️ 仅借鉴/✗ 不可用），MCP/HTTP trust 接口 + 任意数据表的带徽章报告（trust-report）+ A 股领域包（探针-归因-抑制-可信日报）。真实数据考试抑制 0/9 → 8/9，回放 golden 进 CI。
```

**Topics**：`data-quality` `data-agent` `mcp` `mcp-server` `langgraph` `duckdb`
`data-trust` `attribution` `chinese-stock` `evaluation`

## 二、MCP Registry 登记（草稿）

提交前按 registry 当时的 schema 校验字段（schema 会演进，以下为内容草稿，
字段名以 https://github.com/modelcontextprotocol/registry 的最新要求为准）：

```json
{
  "name": "io.github.quannie255-star/findata",
  "title": "Findata Trust Module",
  "description": "Trust enhancement module for AI agents: before citing any
    number, query a four-level trust badge (verified / baseline-pass /
    caution / unusable) with a full evidence chain (corporate events,
    cross-sectional co-movement, trading calendar). 9 MCP tools incl.
    findata_trust_check / findata_trust_board / findata_metric_query
    (deterministic SQL + cross-verification + run_id lineage). Also
    exposes an HTTP API for REST-only hosts.",
  "server": { "command": "findata-mcp", "transport": "stdio" },
  "repository": "https://github.com/quannie255-star/FinData-Agent",
  "keywords": ["data-quality", "trust", "finance", "attribution", "evaluation"]
}
```

登记前自查：`findata-mcp` 在干净环境 `uv tool install` 或 `uvx` 可拉起
（entry-point 已在 pyproject；安装路径写进 registry 的 packages 字段时以
实际发布的 PyPI/GH 包形态为准）。

## 三、发布公告草稿

**中文（V2EX / 即刻 / 朋友圈，二选一裁剪）**：

```text
做了一个开源项目 Findata，定位是「可信数据增强模块」：现在的 Agent 和
问数平台都默认"喂进去的数据是好的"，这个模块负责回答「这个数字能不能
引用、凭什么」——每个数字带四档徽章（已核验/基线通过/仅借鉴/不可用），
点开有归因证据链（停牌/除权/截面共动这些业务事实）。

几个实测：
· 真实 A 股数据考试：7 条告警全是误报（抑制 0/9）→ 修复 → 抑制 8/9，
  9 个案例固化成回放 golden 进 CI
· 同一份数据，通用基线包 60 分 vs 领域包 100 分——差值就是领域知识的价值
· 外部 Agent 经标准 MCP 协议零依赖接入：查数前先过 trust_check 门禁

接入方式：MCP server（Claude Desktop/Cursor 配置即用）/ HTTP /v1/trust /
Python。也带一个通用包 findata-trust-report，任意 CSV/DuckDB 表出带
徽章报告。
GitHub: <链接>
```

**英文（X / Reddit r/mcp 等）**：

```text
I built Findata — a "trust enhancement module" for AI agents. Before your
agent cites a number, it queries a 4-level trust badge (verified /
baseline-pass / caution / unusable) with a full evidence chain: were those
6 missing rows a suspension or a pipeline failure? (On real A-share data,
my first run had 7/7 false alerts — the fix is now a replay golden in CI.)

MCP server (works with Claude Desktop/Cursor) + HTTP API + a generic
`trust-report` CLI that badges any CSV/DuckDB table. Same dataset scores
60 with the generic pack vs 100 with the domain pack — that gap is the
whole product thesis.
GitHub: <link>
```

**配图建议**（按说服力排序）：
1. `examples/domain-vs-generic-comparison.md` 渲染图——同一数据 60 vs 100；
2. `examples/mcp-host-integration.txt` 的最终回答段——逐数字带徽章；
3. `examples/daily-badge-report-2026-09-15.md` 的指标可信度表。

## 四、发布步骤清单（按序）

1. `git tag v2.2.0 && git push origin v2.2.0` → release workflow 出镜像，
   验证 ghcr 拉取 + `/healthz` 冒烟（流水线自带）；
2. GitHub 仓库 Settings 填上面 Description + Topics；
3. 按 `docs/distribution.md` 回填**计时起点**（launch 五项全满足之日）；
4. 提交 MCP registry 登记（上方草稿，先按最新 schema 校验）；
5. 择机发公告（草稿在上方；建议附对比页配图）；
6. 30 天内只做分发不做功能，测量方式见 distribution.md 第二节。
