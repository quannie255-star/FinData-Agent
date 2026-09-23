# 这不是 demo：Agent 上下文/工具预算这件事，业务上已经烂到什么程度

> 写于 2026-09-22。目的只有一个：**用外部证据证明本项目的题目是业务问题，不是技术选题**。
> 全部结论都带出处；凡是"我实测"与"别人报的"分开标注；凡是红海我都点名承认。
> 不允许出现"业内普遍认为"这类无出处的话。

---

## 0. 先说结论（三句）

1. **它的题目已经是一个在招的岗位**，不是我的偏好：Adobe 在招 **AI Context Engineer**，
   薪酬区间 **$114,100–$215,950/年**，职责里写的就是本项目在做的事（见 §2.1）。
2. **它对应一条已经在炸的成本科目**：Gartner 预测到 **2027 年底 >40% 的 Agent 项目被取消**，
   原因里成本是首因之一；有人实测**单个 MCP server 就能让每次调用多背 150K–300K token**
   （见 §1.1、§1.5）。
3. **市场把"该怎么验证"写成了散文，但没人把它做成工具**（§4）。
   本项目唯一可能的差异化就在这里：**不是更会省，而是能证明省了没伤结果**。

---

## 1. 烂摊子的五个面

### 1.1 成本面：每一次调用都在为一个用不到的工具清单付钱

| 外部数字 | 出处 |
| --- | --- |
| 一个 30+ 工具的 SaaS MCP server，**每次调用**就往上下文里塞 **150K–300K** token 的定义 | MuleSoft 官方博客（Agent Control Plane · Cost Management） |
| 270+ 工具的业务系统 ≈ **17,500 token**；接 3 个这种服务 = 143,000 / 200,000 上下文（**71% 的窗口**） | dev.to 实测文（MCP Security in Practice 作者） |
| 741 个工具 ≈ **127,000 token**，**100,000 请求/天** 就是"数十亿 token 花在固定开销上" | Prosodica（AI Engineer 播客） |
| 1,000 请求/天 + 重度 MCP 开销 ≈ **$170/天 ≈ $5,100/月**，**只花在加载 schema** | 同上 dev.to 实测文 |
| 真实账单结构（157,670 次真实 Claude Code 调用）：**cache reads 48% + cache writes 39%**，可见输出只占 **12.1%** | brainy.ink 的仪器化统计 |
| 输入:输出 ≈ **100:1**（Manus 官方 2025 复盘把 cache 命中率列为"最决定 agent 经济性的指标"） | Manus 复盘（经 Mantissa AI 转述） |
| 同类代码 agent 会话，**"从不压缩"比"50–100k 就总结"贵 13–15×**（1,000 轮：$37 vs $550） | frankencoder，34 万条真实消息 / 600+ 会话 |

**一句话**：agent 的钱不是花在"模型写了多少"，是花在**每一轮都把同一份前缀重发一次**。
工具定义在这份前缀里，而且它是**最没有信息量、却最稳定重发**的那部分。

### 1.2 质量面：工具一多，选错是**静默**发生的

| 外部数字 | 出处 |
| --- | --- |
| 10 个工具 78% → 100 个约 40% → **741 个 13.6%**（选对工具的概率） | Prosodica（同上） |
| 单工具 95–96% → 5 工具 85–91% → **20+ 工具 65–78%**；**参数错占失败 60–75%** | 2026 BFCL 衍生benchmark 汇总 |
| LangChain ReAct（2025-02-10）：同一任务从 1 个域扩到 7 个域，**GPT-4o 日历排程准确率跌到 2%** | StackOne 引用 LangChain 官方 benchmark |
| 150 工具压测：**OpenAI API 硬上限 128 个工具**，两个模型在 150 工具上**直接跑不完** | dev.to「We Gave LLMs 150 Tools」 |
| 生产经验：越 10–15 个"活跃工具"阈值后，可靠性"一个季度内掉进 80 几分" | 企业 MCP 架构实践总结 |

**关键机制（这条决定了产品形态）**：失败不报错。文字描述是
「*the agent does not report confusion: it calls something plausible and adjacent, the run
completes, and the wrong system gets written to*」——**跑完了，写错了系统，没人报警**。
这解释了一个反常现象：**成本事故会被发票发现，质量事故只能在业务侧被发现**（或者永远发现不了）。

### 1.3 供给面：工具数量的增速**比任何人的治理能力快**

- 英国 AI Security Institute 联合 Bank of England：**177,436 个 agent 工具**（2024-11 → 2026-02）。
  一年前公开数量约 **5,000**；月下载量 **80,000 → 14,000,000**。
- 同批数据的另一面（arXiv 2603.23802）：软件开发类工具占 **67%**，占 MCP server 下载的 **90%**；
  更值得注意的是 **"action 类工具"的占比从 27% 涨到 65%** —— 也就是说
  **工具正在从"读"变成"写"**（改文件、发邮件、下单）。
- 中国供给侧同样在爆发：截至 2026-07-31，**至少 20 个大众消费品牌**
  （滴滴、瑞幸、高德、飞猪、麦当劳…）上线了自家 skill/MCP/CLI，"一场围绕
  **被 AI 找到**的商业基建赛"。

**推论**：工具池的增长是**别人推着**的（每家 vendor 都要被 agent 找到），而 `+1 个 server`
的代价落在**你的**发票上。**没有人会因为帮你省 token 而少发一个 server。**

### 1.4 责任面：这些 server **不是你的，改不了**（这条否掉一整类解法）

MuleSoft 官方原话：

> "Most of these servers aren't yours to fix. Snowflake, GitHub, Atlassian, Notion —
> you consume them, you don't control them. **The standard advice — trim tool
> descriptions, implement lazy loading — assumes you have access to the server code.
> You don't.**"

这条和本项目 `MEMORY.md` 里我自己记下的**第二个方法论错误**完全对上：我曾把
"改第三方 MCP server 的 schema"当成一条提案杠杆，而它的**可执行性**是零。
现在的处置判据是：**能不能给出一个对方能执行的动作**。
→ 这个判据不是我自己想的，是市场里的最大玩家独立说出来的同一句话。

### 1.5 后果面：项目会被砍，而且**成本是首因之一**

- Gartner（2025-06-25）：**>40% 的 agentic AI 项目将在 2027 年底前被取消**，
  原因是"**escalating costs**, unclear business value or inadequate risk controls"。
  同一条里还有两个数字很好用来校准预期：agentic AI 厂商里"**只有约 130 家是真的**"
  （其余是 agent washing）；**15% 的日常工作决策会在 2028 年由 agent 自主做出**。
- CIO.com 的同题报道里，BlackLine 的 CTO 说得更具体：
  「*costs are volatile and difficult to forecast. Token-based pricing fluctuates with
  behavior, not capacity*」——财务按"容量"预算，而 agent 按"行为"计费，
  **董事会开始问：为什么 AI 成本像无上限的运营支出而不是有边界的投资**。

**一句话**：这件事的失败形态不是"技术做不到"，是**"讲不清 ROI 然后被砍"**。
所以能活下来的产物，交付物里必须有**能进汇报材料的数字**。

---

## 2. 谁在为此付钱、付多少

### 2.1 岗位证据：这是一个**已经存在的职位**（最硬的"业务味道"）

| 岗位 | 关键职责原文 | 出处 |
| --- | --- | --- |
| **Adobe · AI Context Engineer**（$114,100–$215,950/年） | "Develop memory mechanisms… including summarization, pruning, and **context compression** strategies"；"**Own optimization of cost, latency, and reliability**—balancing token limits, response time, and accuracy for production workloads"；"Establish and document best practices for context engineering, **contributing to standards adopted across the enterprise**" | Adobe 官方招聘 |
| **滴滴 · AI Agent 工程师（研发效能）** | "**Harness 工程能力建设**：设计和实现 AI Agent 的 Harness，构建**标准化评测体系**，支持 Agent 的**自动评估、回归测试和持续优化**"；"RAG 与**上下文工程**建设…优化上下文注入策略"；加分项"**成本优化经验（Token 优化 / 推理优化）**" | 滴滴招聘官网 |
| **彩讯科技 · 智能体算法工程师（Context Engineering / Agent Runtime）** 15–25k 广州 | "设计并实现 **Context Builder** 核心模块，完成多源上下文的接入、筛选、排序、**裁剪**、压缩、摘要和组装"；"通过**上下文预算管理**、优先级策略和动态压缩机制，提升长任务场景下的上下文质量、稳定性与 **token 使用效率**" | 猎聘 |
| 某通（鱼泡直聘） | "**性能与成本优化**：深入理解模型计费与上下文窗口的关系…优化 token 使用，**平衡性能与成本**"；"建立科学的评估体系，通过 **A/B 测试**、人工评估和自动化指标…" | 鱼泡直聘 |

> **这三条 JD 合起来就是本项目的方法论**：Context Builder（裁剪）+ Harness/评测体系
> （验证）+ 成本优化（报数）。也就是说 —— **本项目不是给一个不存在的岗位准备的，
> 它是对一个已在招岗位的核心职责做了一次最小可运行实现。**

### 2.2 预算证据：钱有多少、什么时候值得上工具

| 数字 | 出处 |
| --- | --- |
| 银行客服类 agent：**单 agent 工作流 $20,000–$30,000**，**多 agent 团队 $100,000–$200,000** | McKinsey QuantumBlack（2026-08） |
| 单任务成本 $0.50–$8.00；**治理差可达 $20+**（重试 + 上下文膨胀） | 成本治理综述 |
| 上了网关级预算控制的企业常见口径：**两个季度内总 agent 支出降 30–60%**（不降任务量） | 同上（**厂商案例，需打折看**） |
| 自建网关要 **0.5–2 个平台工程师**维护；商业治理平台**年费几万到几十万美元** | 同上 |
| **月 token 支出超过约 $10,000–$20,000，做正经预算框架才回本**；低于此，"日志代理 + 表格周检"是合理解 | 同上 |
| 评测基建本身占 agent 项目**建设期成本的 15–25%**；500 条 golden case 打标 **$5,000–$20,000** | 企业评测实践 |
| 自动路由的 shadow eval：143 个判定轮次 **$1.55**，匹配或优于现模型 **88.1%** | LiteLLM 官方，shadow evaluation |

> 最后两条很关键：**验证本身是有成本的，而且已经被明码标价**。
> 一个"能证明裁剪不掉质量"的工具，卖点不是省 token，是**把上面那笔 15–25% 的评测开销降到可忽略**。

### 2.3 采购证据：云厂商已经把这一层做成商品

- **腾讯云 AI 网关**：明确列"**成本不可控**、治理困难"为核心问题，卖点是协议适配 + 智能路由 + 成本优化策略。
- **阿里云计算巢私有化 MCP 市场**：用 **Higress 网关**做网络控制 + 限流，主打"成本透明"。
- **AWS Bedrock AgentCore Gateway / Cloudflare 参考架构 / AEGIS Framework**：都在描述同一件事——
  **在 agent 与 MCP server 之间放一个审计与策略点**。

**这说明市场不需要被教育**（接受度已经解决），也说明 **"造一个网关"这条路上已经没有位置了**（见 §3）。

---

## 3. 市场**已经**解决了什么（红海清单，必须承认，不许当成我的差异化）

| 已经商品化的能力 | 代表实现 | 我的结论 |
| --- | --- | --- |
| **工具过滤机制**（allow/deny list 代理） | `please-mcp`、`lightweight-github-mcp`（100+ → 10–20）、`ubie-oss/mcp-proxy`、`mcp-controller`、`mcp-shield` | **不做**。机制层免费且已有 5+ 实现 |
| **"哪些工具从没被调用"** | **Langfuse**（2025-12：区分 *Available Tools* / *Tool Calls*，营销话术就是"找出有工具可用但 0 调用的 agent"）；**Latitude**（Tools 面板直接有 `Unused` 徽章 + "how many tools went unused"） | **不做**。这正是我 R5.0 最强的那个读数（23/27 从未被调用），**别人已经做成面板了** |
| **按检索注入 top-k 工具** | RAG-MCP（arXiv 2505.03275：prompt 2133.84 → 1084 token，选择准确率 13.62% → 43.13%）；Anthropic **Tool Search**（GA 2026-02，官方称保留 85% 上下文）；Cloudflare **Code Mode**（1.17M token → ~1,000）；MCP 协议级 Tool Search | **不做**（且 RAG-MCP 自己承认 searcher 在 >100 个 MCP 时精度下降） |
| **成本归因面板** | **MuleSoft** Cost Management（按 server 拆到 11.7M token / 5.9% 占比；并已给出 **"oversized tool responses" 的 207.5K 节省**建议） | **不做面板**。但注意：**它们已经在动返回侧了**，这条赛道也在变挤 |
| **模型路由 / 影子评测** | LiteLLM Auto-Router 的 shadow eval；Jev/TypeSafe 的决策模型 | **不做路由**（已是事实标准） |

> **必须承认的结论**：如果本项目的卖点是"找出没用的工具"或"少发工具定义"，
> 那它在 2026 年**没有位置**——这五类都有现成实现，其中两类是头部可观测性厂商的付费功能。
>
> **剩下没被占的格子只有两个**，两个都窄：
> ① **粒度**：现有工具全是**工具级**（用了没有/调了几次），**没有一个是字段级**
>    （这个返回里的 12 个块，模型用了哪 1 个）。
> ② **证据闭环**：所有现存产物都在回答"能省多少"，**没有一个把"省了之后结果变差了吗"做成可核对的门禁**。

---

## 4. 剩下没人做的：不是「省」，是「**证明省了不掉质量**」

这一节是这个项目唯一站得住的立足点。三条引文，全部是**方法论写得对、但没有工具**：

1. **llmcfo（成本研究站）**：「*Take 100 real conversations, apply your compaction strategy,
   and re-run with an LLM-as-judge to score response quality changes. Sample 20 for human
   review. If less than 5% show degradation, the strategy is safe.*」
   → 方法完全正确（配对重跑 + 质量复评），**但交付物是一句建议**。

2. **EgoistAI「Context Compaction Regression Testing」**：
   「*Do not grade a summary by how readable it sounds. **Grade it by whether future actions
   remain correct.***」「*Run the old and new compactors on the same **frozen corpus**…
   Set **hard zero-tolerance gates**… **Canary** the new version… keep the previous state
   for **rollback**.*」
   「*A compactor is not merely a summarizer. It is a **migration function** for an agent's
   operational state. **If you would not deploy a database migration without tests, do not
   deploy this one on vibes.***」
   → **这就是本项目的论点，由市场用更强的话说了一遍。** 而它是一篇文章，不是一个工具。

3. **ai-cost-estimator「summary drift」**：「*Savings count only after recovery and quality
   costs are included*」「*Compare with an **uncompressed replay or a checkpointed control***」
   「*Stop or revise compaction when a **quality threshold is crossed** even if token graphs
   look attractive*」「*A healthy average can **hide severe drift** in rare high-consequence work*」
   → 又一次：**口径写对了，工具没有**。

**再往前一步看学术侧**：目前最接近的公开测量是
**arXiv 2601.07190（Focus，主动上下文压缩）**——

| | Focus 论文 | 本项目 |
| --- | --- | --- |
| 样本 | **N=5**（SWE-bench Lite 子集） | **n=32**（配对，逐题可对账） |
| 模型 | Claude Haiku 4.5（闭源付费） | 本机 7B / 3B |
| 结论 | −22.7% token（14.9M → 11.5M），准确率 **3/5 = 3/5 不变** | 见 §5 |
| 判据 | 单一准确率 | 四档判据 + **分辨力下限** + 反例 |

> 也就是说：**在这个问题上，"能给数字的公开工作"目前还是 n=5 的量级。**
> 这不是因为我更聪明，是因为**验证这件事很贵、很土、没人愿意做**——
> 而本项目从第一天起就在做这道题（`docs/r5.0-acceptance.md` 的每一节都是"先证明前提成立，再报数"）。

---

## 5. 本项目已经拿到的读数（对外可报的那几个）

**定义侧（R5.0，冻结语料 + 配对，详见 `docs/r5.0-acceptance.md` §3.5.4）**

| 量 | 27 个工具定义 | 4 个 | 差 |
| --- | ---: | ---: | ---: |
| prompt tokens（实测，provider usage） | 224,051 | 100,843 | **−55.0%**，符号检验 **p=2.56e-06** |
| 其中 23 个工具**一次都没被调用** | — | — | 每轮固定吃 **2,232 token** |

**返回侧（R5.2，同一批 32 任务 / 同一份冻结语料 / 同一个模型，只换"该留什么"）**

| 臂（判断依据） | 省掉的返回字符 | prompt tokens | 工具调用 | `exact_hit`（精确值，为准） |
| --- | ---: | ---: | ---: | ---: |
| 不裁（基线） | 0% | 106,843 | 56 | **7/32** |
| **保头部 500 字符**（合理性判断：只看"有多长"） | **88.7%** | 79,303（**−25.8%**） | 64 | **7/32（不变）** |
| **整段换成一行提示**（协议仿真，去掉内容本身） | **95.9%** | 90,663（−15.1%） | 81 | **1/32** |

配对检验（同 32 题，McNemar 精确）：
- 保头部 vs 基线：`exact_hit` 不一致对 **k=6**（下限 0.03125 < 0.05 ⇒ **本对照有分辨力**），
  结果 **p=1.0** ⇒ **"没测到伤害"，不是"测不出"**。
- 一行提示 vs 基线：`exact_hit` **7 → 1**，**p=0.0078（显著变差）**。

**这条对照真正给出的产品结论（也是最有业务价值的一句）**：

> **"少给内容"和"不给内容"不是程度差异，是两种东西。**
> 去掉 88.7% 的内容，质量一个点没掉；去掉 95.9%，质量显著崩，而且
> **模型多调 45% 的工具、总 token 反而没省多少**（−15.1% 对 −25.8%）。
> 换句话说：**"内容换摘要"这条路把一个不稳的杠杆，换成了一个更糟的杠杆**——
> 省得更多、花得更多、结果更差。

---

## 6. 产品化的最小形态（复用已有资产，只写一层适配）

### 6.1 已有的（不需要重写）

| 已有资产 | 它已经是产品的一部分 |
| --- | --- |
| `findata-context-economist`（v0.1，提交 `b2832ed`） | **核心引擎**：读真实 trace → 字段级归因 → 提案 → 配对验证 → 报告（省 token / 折算成本 / 成功率三组数字，**没有验证就不给数**） |
| `build_compact_profile.py` + `compaction.py` | 把 trace 变成**可执行策略**（工具上限 / 保留规则） |
| `grade_audit_runs.py` + `compare_compaction_arms.py` | **门禁**：四档判据、McNemar、分辨力下限、语料指纹核对 |
| `build_audit_corpus.py` | **冻结与去泄漏**的纪律（含"答案卡不许和考卷放同一个考场"） |

### 6.2 唯一真正要写的：输入适配层

现在的输入是**本项目的 trace**（自采 OTel GenAI 约定）。要给别人用，只需要能读：

1. **OTel GenAI 语义约定的导出**（Langfuse / Phoenix / 自建 collector 都能导）
2. 或者**最土的**：`messages` 流水（每轮 `tool_definitions` + `tool_calls` + `tool_results`）
   ——**只要能拿到"给了哪些工具、调了哪些、返回了什么"，本引擎就能跑**。

### 6.3 交付物的形态（三件，全是文件，不是平台）

```
① policy.json      → 能直接被现成网关吃掉的 allow/deny 与上限
                     （对齐 ubie-oss/mcp-proxy 的 _extensions.tools.allow、mcp-shield 的 allowed_tools）
② report.md/.json  → 配对验证报告：token −X%（实测）、四档质量、分辨力下限、反例清单
③ baseline.json    → 基线指纹（语料 + 策略 + 模型 + 判据版本）→ 每次改动可核对、可回滚
```

**关键定位**：**不造网关，给网关产出策略**。它和 §3 那些东西是上下游，不是竞品——
它们负责"执行过滤"，本项目负责"决定过滤什么、并证明过滤没坏事"。

### 6.4 明确不做（防过度设计）

- ❌ 不做 MCP 代理 / 网关（5+ 开源 + 云厂商）
- ❌ 不做 trace 面板 / 可观测性（Langfuse / Latitude，红海）
- ❌ 不做模型路由（LiteLLM Auto-Router / Jev 已是事实标准）
- ❌ 不做向量检索式工具推荐（RAG-MCP / Anthropic Tool Search 已覆盖）
- ❌ 不做训练 / 微调（无 GPU 事实，且与"能证明"这条优势无关）

---

## 7. 诚实边界（三条，**不许省**，省了就变成过度声称）

1. **token 只是成本的一小块。** McKinsey QuantumBlack 2026-08 的银行案例里，
   token 占可变运行成本的 **20–25%**，**人工复核占 70–75%**。
   ⇒ 本项目的杠杆**天花板就是那 20–25%**；能撬动剩下 70% 的路径是"降低异常率"，
   而本项目的质量判据恰好是那条路径的**入口**（能证明裁剪没让结果变差，才敢谈降异常）。
   **不许把本项目说成"能省 55% 的账单"——正确说法是"能省 55% 的 prompt token"**。

2. **机制层是红海，我的差异化只剩两条（粒度 + 验证），都很窄。**
   而且 MuleSoft 已经开始做返回侧（"oversized tool responses"），
   Anthropic / Cloudflare 在协议层直接压 schema —— **稀缺的不是"裁剪的点子"，
   是"能证明裁剪没伤结果的闭环"。** 这句话在 R5.2 的竞品分析里已经写过一次，现在有外部证据背书了。

3. **本机模型口径。** 上面 R5.0 / R5.2 的质量侧结论只在本机 7B / 3B 上成立；
   token 侧结论可迁移（token 数由上下文结构决定）。**报数时必须带这句。**

---

## 附录：证据出处

**成本 / 账单**
- MuleSoft, *Cut AI Agent Token Costs With MCP Cost Management*：blogs.mulesoft.com/news/mcp-cost-management/
- dev.to, *MCP Servers Eat 55,000 Tokens Before Your Agent Says a Word — I Measured the Real Cost*：dev.to/kenimo49/…
- brainy.ink, *AI Agent Token Costs: What You Actually Pay For*（157,670 次真实调用）：brainy.ink/paper/ai-agent-token-costs
- frankencoder, *Keeping your AI coding costs low with Anthropic*（34 万条消息 / 600+ 会话）：frankencoder.com/blog/context-economics.html
- Augment Code, *AI Coding Cost Analysis: Where Token Spend Really Goes in an Agent Loop*：augmentcode.com/guides/…
- Mantissa AI（转述 Manus 2025 复盘：输入:输出 ≈ 100:1）：mantissaai.com/blog/context-engineering-long-running-agents

**工具膨胀 / 选择准确率**
- BigGo Finance 转述 Prosodica（Shaikh & Rastogi，AI Engineer 播客）：10/100/741 工具 → 78%/40%/13.6%
- StackOne, *Why AI Agent Accuracy Drops as Tool Count Grows*：stackone.com/blog/agent-tool-count-ceiling
- presenc.ai, *AI Agent Tool-Calling Accuracy Benchmarks 2026*（BFCL 衍生）：presenc.ai/research/…
- dev.to, *We Gave LLMs 150 Tools: Here's What Broke*：dev.to/craigtracey/…
- getunblocked.com, *MCP Tool Overload: A Measured Guide*：getunblocked.com/blog/mcp-tool-overload

**供给面 / 生态**
- arXiv 2603.23802, *How are AI agents used? Evidence from 177,000 MCP tools*
- MCP Tool Overload 综述（177,436 工具 / 80k → 14M 月下载，引自 UK AISI + Bank of England）
- 新华财经《智能体"干活"三件套》（2026-07-31）：至少 20 个消费品牌上线 skill/MCP/CLI

**背书 / 后果**
- Gartner 新闻稿（2025-06-25）：*Over 40% of Agentic AI Projects Will Be Canceled by End of 2027*
- CIO.com, *Why most agentic AI projects stall before they scale*
- meshiq 转述 McKinsey QuantumBlack（2026-08）：银行 agent $20k–30k / $100k–200k；token 占 20–25%

**验证方法论（就是本项目的立足点）**
- llmcfo, *Context compaction is a cost lever*：llmcfo.com/research/context-compaction-cost
- EgoistAI, *Context Compaction Regression Testing for Long-Running AI Agents*：egoistai.com/articles/…
- ai-cost-estimator, *Agent Context Compaction Cost: Measure Summary Drift Before Tokens Saved*
- tianpan.co《上下文压缩改变了你的模型真正看到的内容》（逐字精简 > 部分压缩；结构化账本只追加）
- arXiv 2601.07190, *Active Context Compression（Focus）*：−22.7% token，准确率 3/5 不变，N=5

**已商品化的机制层（§3 红海清单）**
- OSS 代理：`please-mcp`、`lightweight-github-mcp`、`ubie-oss/mcp-proxy`、`mcp-controller`、`mcp-shield`
- 可观测性：Langfuse 工具调用过滤（2025-12-22 changelog）、Latitude Tools 面板（Unused 徽章）
- 检索式选择：RAG-MCP（arXiv 2505.03275）、Anthropic Tool Search、Cloudflare Code Mode、MCP Tool Search
- 影子评测：LiteLLM Auto-Router shadow evaluations

**岗位与采购**
- Adobe, *AI Context Engineer*（$114,100–$215,950）：bebee.com/us/jobs/ai-context-engineer-adobe-…
- 滴滴出行, *AI Agent 工程师（研发效能）*：talent.didiglobal.com/social/p/61510
- 彩讯科技, *智能体算法工程师（Context Engineering / Agent Runtime）* 15–25k：liepin.com/job/1982035731.shtml
- 腾讯云 *AI 网关*；阿里云 *计算巢私有化 MCP 市场*（Higress）
