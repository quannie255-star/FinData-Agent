# AFAC 赛题四脚手架题集（开发用口径，v1）

## 这是什么

官方评测数据未开放下载（比赛已收官），本目录是**自建脚手架题集**，
用于 M1（harness + 评分器 + baseline）与 M2（检索层 + B 榜盲检）的开发测试。
**它不是官方评测集，跑出来的数字不得对外声称为评测成绩。**

## 口径声明（v1，2026-10-05）

- **规模**：11 题（7 mcq/multi + 2 tf + 2 multi 含在 multi 内；题型分布
  mcq 4 / multi 4 / tf 2... 以 questions.json 为准）、3 份文档、单一领域
  （regulatory）。其中 2 题为 **B 榜口径**（`reg_s_010/011`，不给 doc_ids，
  走检索臂全局盲检）；v0→v1 的变化见下「v1 修订」；
- **文档来源**（全部为官方原文确定性提取 + 逐字校对，仅去页码断行）：
  - `strict_csrc_035`：证监会《上市公司章程指引》（2023 修订）官方 PDF
    （`data/raw/afac/zhangcheng_2023.pdf`，72 页）节选；
  - `strict_csrc_023`：证监会《上市公司治理准则》节选，第四十一条为赛题
    题面逐字引用的原文；
  - `strict_csrc_036`：证监会令第220号《上市公司独立董事管理办法》官方 PDF
    （`data/raw/afac/dudong_banfa_220.pdf`，18 页）节选。该办法已有 2025
    修正版（令第227号），本节选以 220 号原文为准；
- **题目来源**：`reg_s_001` 是赛题官方样例原题（题面公开，标准答案 AC）；
  其余 10 题为自建，答案均由节选条文严格推出，`evidence` 记录锚点条款；
- **已知出入（如实记录）**：① 官方样例选项 D 引用的「第77条授权」在 2023
  版官方原文对不上（该版七十七条为会议记录条款），不影响答案；② 官方样例
  的 doc 集只有两份文档，选项 C 的独立性依据（「最近12个月法律服务」）
  在治理准则 41 条里只是弹性标准——**v1 把 `strict_csrc_036`（独董办法
  第六条（六）（七）有确定性负面清单）加进了该题 doc_ids**，使 C 选项
  在脚手架口径下证据完备。这是对官方样例 doc 集的扩充，README 记录在案；
- **难度边界**：文档规模小、题目锚点集中，两臂（full/retrieve）在此集上
  均 11/11 与 9/9 全对——**说明管线正确，不说明策略优劣**；token 差异
  （共同 9 题 full 14,435 vs retrieve 13,260）在文档只有几千字符时被压扁，
  真实赛题规模（86 份长文档）下检索臂的优势才会显出来。

## v1 修订记录（v0 → v1）

- 新增 `strict_csrc_036`，`reg_s_001` doc_ids 扩至三文档（修 M1 发现的
  「证据欠定」：双模型同错 C，定性为文档过薄而非模型失误）；
- 题量 5 → 11，新增 `reg_s_006~011`（锚定独董办法第五/六/七/八/二十条）；
- 新增 2 道 B 榜口径题（无 doc_ids），供 M2 检索臂盲检。

## 文件

- `questions.json`：题目数组，字段对齐赛题题面 + `gold` + `evidence`；
- `docs/<doc_id>.md`：文档文本，首行 `# <标题>`，`## 第X条` 分块（检索层
  的条款级 chunk 以此为界）。

## 复现

```bash
# 两臂对照（全文直入 vs BM25 检索；含 B 榜盲检）
uv run python scripts/run_afac_baseline.py --mode both \
    --out examples/afac-scaffold-v1-full-vs-retrieve.txt
```

需要 `.env` 里配置 `FINDATA_DASHSCOPE_API_KEY`（付费 API，不进 CI）。
归档读数：`examples/afac-scaffold-v1-full-vs-retrieve.txt`。
