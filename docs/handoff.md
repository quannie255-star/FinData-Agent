# 维护者任务清单（外部验证与发布）

> 面向维护者：列出需要**外部资源**（他人环境、GitHub 账号）才能完成的事项，
> 以及它们在主线里的权重。仓库内的技术工作（题集 / 代码 / 评测）不在本文，
> 见 `README.md` 与 `docs/afac-track4.md`。

## 一、外部宿主集成证据 → `examples/external-integration-<代号>.md`

**这条作者自证不算数**——"陌生"的定义就是作者以外的人。换台机器 / 换个接入面
也算，最好是没看过本项目的人。

- 定位：主线为「金融长文本 QA 记忆压缩」，本项是**加分项**（不是唯一指标）。
- 命令：

```bash
pipx run --spec git+https://github.com/quannie255-star/FinData-Agent.git findata-mcp
# 或：docker run -p 8000:8000 ghcr.io/quannie255-star/findata-agent:latest
```

> **已知的第一道坎（别提前提醒，让对方自己撞）**
> `findata_trust_check` / `findata_trust_board` 默认 `source="duckdb"`，新环境没有
> 仓库，会报 `ValueError: 数据仓库不存在：... 或改用 source='synthetic'`。
> 报错信息本身是可行动的——**观察对方能否自己看懂并改过来**，那正是最值钱的证据。
> 默认值刻意没改成 `synthetic`：那样陌生人会拿到合成数据还以为是真数据，
> 比报错危险得多。

记录模板（越原始越好，**卡住的地方比成功更有价值**）：

```markdown
# 外部宿主集成 · <代号>

- 谁：<昵称 / 匿名>
- 日期：YYYY-MM-DD
- 环境：<OS> / Python <版本> / 是否用 Docker
- 接入面：MCP / HTTP / Python
- 有没有看过本项目文档：看过 README / 完全没看过 / 只口述了一句
- 从零到跑通花了多久：
- 卡住的地方（原文报错照贴）：
- 原始输出（照贴，别加工）：
```

## 二、发布收尾（需要 GitHub 账号）

1. 仓库 <https://github.com/quannie255-star/FinData-Agent> → ⚙ Settings
   - **Description**：
     ```
     金融长文本智能问答系统：检索·判定·记忆·归因四层全链（BM25F 逐选项检索 + 选项级证据隔离 + 分层记忆压缩 + 引用观测归因），自建五域评测集（法规/财报/保险/合同/研报，50 题含 193 页募集说明书），791 例测试。场景口径对齐 AFAC2026 赛题四公开题面，无参赛成绩/官方关联。
     ```
   - **Topics**（逗号分隔，直接贴）：
     ```
     finance, fintech, question-answering, long-context, memory-compression, retrieval, bm25, llm-eval, qwen, python, data-quality, mcp
     ```
   ⚠️ `topics` 走 `PATCH /repos/{owner}/{repo}` 会被**静默忽略**（返回 200 但不变），
   必须用专用端点 `PUT /repos/{owner}/{repo}/topics`（body `{"names": [...]}`）；
   description 用 PATCH 正常生效。
2. MCP Registry 登记（<https://registry.modelcontextprotocol.io>）——需要账号，
   可能等审核。草稿如下（提交前按 registry 最新 schema 校验字段名）：

   ```json
   {
     "name": "io.github.quannie255-star/findata",
     "title": "Findata Trust Module",
     "description": "Trust enhancement module for AI agents: before citing any number, query a four-level trust badge (verified / baseline-pass / caution / unusable) with a full evidence chain (corporate events, cross-sectional co-movement, trading calendar). 9 MCP tools incl. findata_trust_check / findata_trust_board / findata_metric_query (deterministic SQL + cross-verification + run_id lineage). Also exposes an HTTP API for REST-only hosts.",
     "server": { "command": "findata-mcp", "transport": "stdio" },
     "repository": "https://github.com/quannie255-star/FinData-Agent",
     "keywords": ["data-quality", "trust", "finance", "attribution", "evaluation"]
   }
   ```

   登记前自查：`findata-mcp` 在干净环境 `uv tool install` 或 `uvx` 可拉起
   （entry-point 已在 pyproject）。
3. 把完成日期回填到 `docs/distribution.md` 的计时起点。

---

## 汇总方式

文件放上面指定路径，下次开工直接读。
**不要**帮忙"整理"或"总结"——原始输出才有证据价值，加工过看不出卡在哪。
