# 分工交接（2026-09-18）

## 一句话

**我做技术侧（题集 / 代码 / 评测），你做外部侧（目标岗位 / 人脉 / 账号）。**
两条线互不阻塞，你产出文件给我，我负责归档与回填文档。

分工的依据不是"谁有空"，而是**谁能做**：
- 题集、代码、评测——我能跑、能验证，你审结论即可；
- 目标 JD、陌生宿主、GitHub 发布——要么是我不知道的信息，要么**我做了也不算数**。

---

## 我做（不需要你的账号，做完直接归档）

| # | 事项 | 验收标准 |
| --- | --- | --- |
| 1 | 长尾问法题集 6 → 24 题 | `run_parser_eval.py` 两组都能跑；24 题的 slot / badge 数字进归档 |
| 2 | 除权除息事实源接入（第二个"形态相同、结论相反"的场景） | 至少 3 道除权除息题进题集，门禁能区分"价格跌了"与"除权了" |
| 3 | 收到 JD 后出**关键词覆盖矩阵** | 每个 JD 关键词标：已覆盖 / 有但没写进材料 / 缺 |
| 4 | 归档你给的集成证据，回填 `docs/distribution.md` 计时起点 | 30 天窗口有明确的 T0 日期 |

---

## 你做（我做不了，或我做了不算数）

### ① 目标实习 JD → `docs/jd-targets.md`

**为什么必须你来**：我不知道你投哪几家。而"面向实习"这个定位如果没有具体 JD 对齐，
我做的所有东西都是在猜面试官想听什么。

格式随意，越原文越好（我要的是关键词，不是你的总结）：

```markdown
# 目标岗位

## 1. <公司> · <岗位>
链接/来源：
原文摘录（技术栈那段整段贴）：
> ...
> ...

## 2. ...
```

3–5 家就够。有内推截止日期或笔试时间也标上，我按优先级排补齐顺序。

### ② 陌生宿主集成证据 → `examples/external-integration-<代号>.md`

**这条是 30 天窗口的唯一指标，我自证不算数**——"陌生"的定义就是不是我。
你自己跑第二遍也算（换台机器 / 换个接入面），但最好找一个没看过这个项目的人。

给他这一条命令就够了（不需要 clone 仓库、不需要 Python 环境之外的东西）：

```bash
pipx run --spec git+https://github.com/quannie255-star/FinData-Agent.git findata-mcp
# 或：docker run -p 8000:8000 ghcr.io/quannie255-star/findata-agent:latest
```

> **已知的第一道坎（别提前提醒他，让他自己撞）**
> `findata_trust_check` / `findata_trust_board` 默认 `source="duckdb"`，新环境没有
> 仓库，会报 `ValueError: 数据仓库不存在：... 或改用 source='synthetic'`。
> 报错信息本身是可行动的——**观察他能不能自己看懂并改过来**，那正是最值钱的证据。
> 我刻意没把默认值改成 `synthetic`：那样陌生人会拿到合成数据还以为是真数据，
> 比报错危险得多。要不要改，等你拿到 2–3 份记录再一起定。

记录这些，越原始越好（**卡住的地方比成功更有价值**）：

```markdown
# 外部宿主集成 · <代号>

- 谁：<昵称 / 匿名>
- 日期：YYYY-MM-DD
- 环境：<OS> / Python <版本> / 是否用 Docker
- 接入面：MCP / HTTP / Python
- 有没有看过本项目文档：看过 README / 完全没看过 / 我只口述了一句
- 从零到跑通花了多久：
- 卡住的地方（原文报错照贴）：
- 原始输出（照贴，别加工）：
```

### ③ 发布收尾（需要 GitHub 账号）

三步，做完把日期告诉我，我回填计时起点。

1. 仓库 <https://github.com/quannie255-star/FinData-Agent> → ⚙ Settings
   - **Description**：
     ```
     可信数据增强模块：给任意 ChatBI / 数据 Agent 加一道「这个数字能不能引用」的门禁。MCP / HTTP / Python 三种接入面，输出带徽章与归因证据链。
     ```
   - **Topics**（逗号分隔，直接贴）：
     ```
     data-quality, data-trust, chatbi, text-to-sql, mcp, llm-agent, agent-observability, duckdb, data-observability, llm-eval
     ```
2. MCP Registry 登记（<https://registry.modelcontextprotocol.io>）——需要账号，可能等审核
3. 把完成日期发我

---

## 汇总方式

文件放上面指定的路径，我下次开工直接读。
**不要**帮我"整理"或"总结"——原始输出才有证据价值，加工过的我看不出卡在哪。
