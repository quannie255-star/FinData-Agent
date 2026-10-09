# Findata 架构与实现评审文档（自包含，供外部 AI/工程师评审）

> 写给不了解本仓库的评审者：本文自包含——架构、核心模块的真实实现逻辑（代码摘录）、
> 评测方法与读数、已知短板、想请你们评审的问题，全部在本文内。
> 请重点看「六、请评审的问题」一节；对实现逻辑的任何一节都可以直接挑战。

---

## 一、项目是什么

**金融长文本智能问答系统**：用户上传自己的文档（PDF/Word/TXT/MD，典型如合同、
法规、年报、保险条款），系统检索证据后回答自由问题，答案带出处引用。

问题场景与评测口径借用 AFAC2026 赛题四（金融长文本 Agent 的动态记忆压缩与
高效问答，出题方复旦大学）的公开题面；该比赛已收官，本项目**以求职（实习）
为目的，比赛只是借用的场景**，无参赛成绩或官方关联。

**硬约束**（沿用赛题规则，也符合项目哲学）：

1. 推理只许用 Qwen 系列 API（阿里云百炼/魔搭），禁止修改模型参数；
2. **禁止任何 embedding 模型参与检索**——检索只能是规则型（关键词/BM25/
   结构化索引）；
3. 预处理阶段（PDF 解析、结构化）允许非 Qwen 工具（如 MinerU）。

## 二、现状与读数

- 两套评测：**自建五域评测集**（法规/财报/保险/合同/研报，50 题、7 份官方
  原文文档约 74 万字符，含 193 页募集说明书与 7 道 B 榜盲检题）+
  **FinanceBench 外部基准校准**（Patronus AI，金融 QA 产业界标准，99 题）。
- 自建集当前读数（qwen-plus，temperature=0 + 固定 seed，逐配置确定）：

| 配置 | 准确率 | token | 说明 |
| --- | --- | ---: | --- |
| 全文直入（朴素基线） | 90% | 188K | 31 题期 |
| 逐选项检索 | 95% | 62.5K | 同上 |
| 选项级证据隔离（当前默认） | 98%（49/50） | 453K | 50 题期 |
| + B 榜两阶段检索 | **7/7 → 隐含 50/50** | — | B 榜 7 题复测 |
| 滚动摘要记忆（朴素压缩） | 77.4±3.2%（n=3） | 216K | 压缩代价基线 |
| 分层记忆（数值/条款逐字+叙述压缩） | 96% | 612K | 恢复 19pp |

- **FinanceBench 外部校准（诚实读数：英文域弱）**：99 题，
  证据覆盖 recall@8 = 8.1%，answer@8 = 33.3%（答案值入上下文）。
  中文条款域 50/50 的能力**不迁移**到英文 10-K 表格域；失败原因已定位：
  英文复数词干（已修）、答案数字格式失配（已修评测）、EDGAR HTML 剥标签
  把表格压平（待做）、纯 query 信号（待做）。
- 质量基建：701 例测试全绿；token 台账覆盖全部调用（接口无 usage 即抛错）；
  噪声下界 n=3 实证（链式架构 ±3.2pp，隔离架构逐配置确定）。

## 三、系统架构

```
┌─ Web 层 service.py（FastAPI，单机 http://127.0.0.1:8100，无鉴权仅本机）──┐
│  /api/questions /api/answer/{qid}   评测演示（考官模式：gold 不出后端）  │
│  /api/library (upload/list/delete)  个人文档库（findata_userdata/，gitignore）│
│  /api/ask-free                      自由问答（对用户自己的文档）          │
│  /api/runs                          运行归档浏览                          │
└──────────────┬───────────────────────────────────────────────┘
               │
┌─ 检索层 retrieval.py ──┐
│ chunk_docs：按 ## 标题切块（法条=条款号锚点），超长块二级切分      │
│   （1200 字符/块，行级不切断，尾部两行重叠）；无分节文档整文保留     │
│ tokenize：CJK 二元组 + 条款号整 token + 西文词复数归一（ies→y 等） │
│ BM25Index：双通道 BM25F——正文打分 + 字段打分（节标题+短标签行）    │
│   线性组合 score = body + 2.0×field；前言/来源块不进索引            │
│ retrieve：单查询 top-k；doc_ids 给定时限定检索（A 榜口径）          │
│ retrieve_many：多查询轮转合并去重（逐选项，防第一查询占满名额）；   │
│   docs_top=1 两阶段：B 榜先文档级粗筛（top-3 片段分聚合）再片段级   │
└──────────────┬────────┘
               │
┌─ 判定层 isolate.py ────┐
│ 每个选项：只检索自己相关的片段（题干+该选项为查询）→ 独立三态判定   │
│   TRUE / FALSE / UNCERTAIN（判定语义：该选项是否应选入本题答案，     │
│   FALSE 明确涵盖「陈述虽真但不属于题目所问」）                       │
│ UNCERTAIN → 复审一次（证据加倍），单向升级（只许 UNCERTAIN→TRUE）   │
│ mcq：恰好一个 TRUE 才选；multi：全部 TRUE 入选，UNCERTAIN 不入选    │
│ 拼不出合法答案 → 回退仲裁：合并上下文单次作答（结构不劣于上一代）    │
│ tf 题选项无信息量，不隔离，直接单次作答                             │
└──────────────┬────────┘
               │
┌─ 记忆层 memory.py ─────┐
│ 朴素臂：证据池分批读入 → 滚动摘要 ≤400 字 → 判定只看记忆            │
│ 分层臂（layered）：法条标题/数字密度≥4% 的片段逐字保留进判定上下文， │
│   只对叙述层滚动压缩                                                │
└──────────────┬────────┘
               │
┌─ 归因层 attribution.py ┐
│ 从回复抽取引用（【doc·条款】/裸条款号/编号节），只在场匹配（防幻觉   │
│ 引用虚增使用率）；产出使用率、按位次曲线、浪费字符占比               │
└──────────────┬────────┘
               │
┌─ 评分 scoring.py ──────┐
│ 题面公式逐字实现：单选/判断取首字母；多选去重排序后完全匹配（无部分  │
│ 分）；TokenScore = max(0,min(1,(5M−Total)/5M))；                    │
│ FinalScore = 100×Acc×(0.7+0.3×TokenScore)                           │
└────────────────────────┘

个人问答头 personal.py（自由问答，无选项）：检索（同检索层）→ 单次自由
作答（只依据片段、引用【doc·Section】、不足明说「未提及」不推测）→ 引用
经在场匹配校验（幻觉引用不计数）。
外部校准 fetch_edgar_docs.py + eval_financebench_recall.py：SEC EDGAR
独立数据通路（公司名→CIK 自动匹配、申报历史分页合并、form 代码映射、金
证据覆盖率自检环、限速+磁盘缓存）。
```

## 四、核心模块实现逻辑（真实代码摘录 + 设计理由）

### 4.1 检索层：BM25F 双通道 + 两阶段

```python
def tokenize(text: str) -> list[str]:
    tokens: list[str] = _ARTICLE_TOKEN.findall(text)      # 条款号整 token（锚点）
    tokens.extend(_normalize_latin(m.group(0)) for m in _LATIN.finditer(text))
    chars = [c for c in text if _CJK.match(c)]
    tokens.extend(a + b for a, b in zip(chars, chars[1:], strict=False))  # CJK 二元组
    return tokens

def _field_tokens(chunk: Chunk) -> list[str]:
    """字段通道：节标题 + 块内短标签行（表格指标名/年份行）。"""
    tokens = tokenize(chunk.header)
    for line in chunk.text.splitlines():
        stripped = line.strip()
        if _SHORT_LABEL.match(stripped) and _CJK.search(stripped):
            tokens.extend(tokenize(stripped))
    return tokens

class BM25Index:
    def __init__(self, chunks):
        indexed = [c for c in chunks if not c.is_preamble]
        covered = {c.doc_id for c in indexed}
        indexed += [c for c in chunks if c.is_preamble and c.doc_id not in covered]
        self.chunks = indexed
        self._body_bm25 = BM25Okapi([tokenize(c.text) for c in indexed])
        self._field_bm25 = BM25Okapi([_field_tokens(c) for c in indexed])

    def _scores(self, query):
        toks = tokenize(query)
        body = self._body_bm25.get_scores(toks)
        field = self._field_bm25.get_scores(toks)
        return [b + FIELD_WEIGHT * f for b, f in zip(body, field, strict=True)]
```

**设计理由与踩坑记录**：

- **为什么 BM25F 双通道而不是 token 注入加权**：先把标签行 token 重复注入
  正文语料，结果表格块排名**不升反降**——BM25 长度归一化惩罚变长的多标签
  块。改成两个独立 BM25 线性组合后表格块进 top-4。
- **为什么前言块出索引但无分节文档保留**：文档来源说明块的标题（如「上市
  公司治理准则」）与 query 高频词重合，字段加权后被顶到 #1-#4 挤掉真证据；
  但研报等整文无 `##` 分节的文档，全文就是唯一「前言块」，排除会导致该文档
  检索池为空（真踩过：research 域 4/5 错，判定拿到「检索未命中」）。
- **已知局限**：字段加权的短标签通道要求 CJK，对英文 10-K 不生效——这是
  FinanceBench 校准读数低的原因之一（见 §三）。

### 4.2 两阶段检索（B 榜盲检）

```python
def doc_scores(self, query: str) -> dict[str, float]:
    by_doc: dict[str, list[float]] = {}
    for chunk, score in zip(self.chunks, self._scores(query), strict=True):
        by_doc.setdefault(chunk.doc_id, []).append(score)
    return {d: sum(sorted(ss, reverse=True)[:3]) for d, ss in by_doc.items()}

# retrieve_many(..., docs_top=1)：B 榜（无 doc_ids）每查询先取文档级 top-1，
# 再在该文档内做片段检索；A 榜 doc_ids 显式给定时忽略粗筛。
```

**理由**：50 题唯一错题（B 榜营收题）的病根是 193 页合同文档在全局片段池
挤占目标域表格块。`docs_top=3` 会把合同文档也放进二级池（实测仍挤占），
`docs_top=1` 配合**逐选项并集**保住多文档覆盖（三道跨域 B 题关键条款离线
验证全命中）。

### 4.3 判定层：选项级证据隔离

```python
for letter, text in sorted(q.options.items()):
    chunks = index.retrieve_many([f"{q.question} {text}"],
                                 k_per_query=k_option, total_cap=k_option, ...)
    reply, record = client.chat(q.qid, f"judge:{letter}",
                                build_judge_messages(q.question, letter, text,
                                                     chunks_text, q.format_label()))
    verdict = extract_verdict(reply) or "UNCERTAIN"
    if verdict == "UNCERTAIN":
        wider = index.retrieve_many(..., k_per_query=k_option * 2, ...)
        reply2, record2 = client.chat(q.qid, f"judge:{letter}:retry", ...)
        if extract_verdict(reply2).upper() == "TRUE":
            verdict = "TRUE"          # 单向升级：只许 UNCERTAIN→TRUE
    verdicts[letter] = verdict

def assemble_multi(verdicts):   # 多选答案由代码确定性拼装
    return "".join(sorted(letter for letter, v in verdicts.items() if v == "TRUE"))

def assemble_mcq(verdicts):     # 恰好一个 TRUE 才返回；否则回退仲裁
    trues = sorted(letter for letter, v in verdicts.items() if v == "TRUE")
    return trues[0] if len(trues) == 1 else None
```

判定 prompt 关键句（判定语义）：

```
判断该选项是否应作为本题正确答案的一部分被选入：
- 依据充分且符合题目所问 → TRUE（应选入）
- 依据充分表明不应选入——陈述错误、与片段矛盾、或陈述虽真实
  但不属于题目所问的集合 → FALSE
- 片段依据不足 → UNCERTAIN（不入选，保守防过包含）
```

**踩坑记录（三次失败形态的演进，每次都有归档读数）**：

1. 单查询合并检索 → 跨域题漏选（第二域证据进不了上下文）→ 逐选项检索；
2. 逐选项后上下文变大 → 选项间干扰（「过包含」，多选了 D）→ 选项级隔离；
3. 隔离后判定语义是「陈述真假」→「独董每年自查」这种**真话但不在题干集合**
   的选项被误选 → 语义改为「应选入本题答案」。
4. 复审曾允许判 FALSE：更宽证据引入干扰把对题判错 → 改单向升级。

### 4.4 自由问答头（个人工具形态）

```python
def ask_free(question, docs, client, k_total=12):
    index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
    chunks = index.retrieve_many([question], k_per_query=6, total_cap=k_total)
    reply, record = client.chat(qid, "free_answer",
                                build_free_qa_messages(question, chunks_text))
    units = [(c.doc_id, c.header, len(c.text)) for c in chunks]
    used = match_citations(extract_citations(reply), [(d, h) for d, h, _ in units])
    return {"answer": ..., "citations": [...], "total_tokens": ...}
```

纪律：只依据检索片段、引用经在场匹配校验（幻觉引用不计数）、片段不足
明说「未提及」不推测。真实端到端验收：上传中文服务合同，问违约金与期限，
两个事实全对、引用正确、276 token。

### 4.5 工程纪律（全部有测试钉死）

- token 台账覆盖全部调用；接口不返回 usage 直接抛错（禁止估算——「估算的
  token 进提交文件就是伪造账单」）；
- 付费 API 不进 CI（所有测试离线/假客户端）；
- 逐配置确定解码（temperature=0，seed 固定）——隔离臂两次运行完全一致；
- 评测先行：每个策略改动先写验收测试（含失败用例翻正条件）再碰 API。

## 五、评测方法学

- **配对口径**：同题/同语料/同解码，唯一变量是被测维度；比较工具固化
  「不一致对为 0 ≠ 等价」（只说未观察到差异）；
- **噪声下界**：链式架构（摘要→判定）n=3 实测 ±3.2pp；隔离架构逐配置
  确定。一切质量结论带区间或注明 n；
- **分辨力自觉**：50 题下 ±1 题差异明确说「低于分辨力」（k=2 能否切默认
  就因此悬而未决）；
- **外部校准**：FinanceBench（产业界标准）+ DocMath-Eval（ACL 2024，gated
  待授权）。基准只用于校准报告，不拿来调参。

## 六、请评审的问题（按想知道的程度排序）

1. **检索层架构**：BM25F 双通道（权重 2.0 是拍的）+ 两阶段（docs_top=1）
   这个组合，在「禁 embedding、纯规则检索」的约束下，有没有我们没想到的
   更稳健形态？特别是：英文 10-K 场景（FinanceBench 校准读数很差）里，
   不用神经模型还能怎么显著提召回？
2. **判定层**：选项级隔离（每选项独立判定）在准确率上有效但 token ×2~3。
   有没有更省的等效方案（例如一次上下文内结构化输出四选项判定 + 自洽性
   校验）？我们担心单次结构化输出会退回「选项间干扰」老问题——值得再试吗？
3. **评分对齐**：FinalScore = 100×Acc×(0.7+0.3×TokenScore)，TokenScore
   在 5M 预算内线性。我们算出「Acc≈80% 区间，掉 1% 准确率需要省约 83 万
   token 才回本」——这个边际分析框架对吗？
4. **分层压缩的定位**：分层压缩把记忆臂从 77% 拉回 96% 但 token 仍比
   「不压缩挑相关片段」高。我们的结论是「分层的价值场景在超大规模文档池」
   ——同意吗？还是应该现在就探索跨题记忆复用？
5. **工程健壮性**：单机 FastAPI + 原生 JS 单页（无构建链、无鉴权、仅本机），
   作为个人工具够了；如果要做到「可以给同事用」，最小必要的加固清单是什么？
6. **评测方法学**：自建 50 题的分辨力上限已经卡住调参结论（±1 题不可分辨）。
   扩到多大题量才够支撑「k=2 切默认」这类决策？有没有比「扩题量」更便宜的
   提分辨力手段（分层抽样/交叉验证）？
7. **Anything else**：任何你觉得「这帮人没想到」的盲区，直说。

## 七、已知短板（主动交代，免得你们从这些开始）

- 英文 10-K 域检索弱（FinanceBench answer@8 = 33%），改进路线已列未做；
- 评测集自建（50 题），官方赛题数据已下线不可得；±1 题差异不可分辨；
- 单模型口径（qwen-plus），未做云端大模型对标；
- 外部真实用户 0（求职作品，非运营产品）；
- Web 层无鉴权，仅本机使用；
- 曾三次「走错又折返」（长度归一化反噬/复审干扰/测试配置分叉），均已留档
  ——但这也说明检索调参对本团队有摸索成本，欢迎更系统的方法建议。
