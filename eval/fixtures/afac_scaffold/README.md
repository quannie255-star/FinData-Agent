# AFAC 赛题四脚手架题集（开发用口径，v4）

## 这是什么

官方评测数据未开放下载（比赛已收官），本目录是**自建脚手架题集**，
用于 M1-M5 全链（harness / 评分器 / 检索 / 判定 / 记忆 / 归因 / 配对验证）
的开发测试。**它不是官方评测集，跑出来的数字不得对外声称为评测成绩。**

## 口径声明（v4，2026-10-07）

- **规模**：50 题 / 7 文档 / **五域全齐**（regulatory 24 / financial_reports 9 /
  insurance 7 / financial_contracts 5 / research 5），其中 **7 题 B 榜口径**
  （不给 doc_ids，含一道四域混合盲检 `res_s_005`）；
- **文档来源**（全部官方公开 PDF 确定性提取）：
  - `strict_csrc_035` 章程指引全文（32.8K/192 条文块）、`strict_csrc_036`
    独董办法全文（9.5K）、`strict_csrc_023` 治理准则节选；
  - `fin_rep_byd_2025` 比亚迪 2025 年报摘要（巨潮，近三年对比数据）；
  - `ins_pingan_ci_2015` 平安附加平安福重疾条款（平安官网，16 页）；
  - `fc_lhxc_cb_2026` 隆华新材可转债募集说明书摘要（巨潮，**193 页 / 163.6K
    字符——本集首份真实长文档**，第X节/一、二、锚点 + 二级切分）；
  - `res_dwzf_300059_2026q1` 东吴证券东方财富 2026 一季报点评
    （东方财富研报库，4 页，含盈利预测表）；
  - 原始 PDF 均存 `data/raw/afac/`（gitignore），生成方式见各文档头部；
- **题目来源**：`reg_s_001` 为赛题官方样例原题；其余 49 题自建，gold 均由
  文档文本严格推出（新启用的条款/数字先 grep 验证原文再出题），
  `evidence` 记录锚点；
- **难度边界**：五域全但 regulatory 占比仍高（48%）；长文档只有 1 份；
  B 榜 7 道。距离真实赛题（A 榜 100 题 / B 榜 100 题盲检 + 86 文档）
  仍有量级差距，读数不外推。

## v3 → v4 修订记录

- 新增 research / financial_contracts 两域（五域全齐）；
- 新增首份 193 页真实长文档（募集说明书全文）；
- 题量 31 → 50（+19：res 5 / fc 5 / reg 4 / ins 2 / fin 3）；
- B 榜题 6 → 7（含四域混合盲检）。

## v0 → v3 修订记录（摘要）

- v1：+独董办法（修 reg_s_001 证据欠定），5 → 11 题；
- v2：法规全文化 + 比亚迪摘要（跨域第一份财报），11 → 20 题；
- v3：+平安重疾条款（insurance 域）+ 财报二级切分，20 → 31 题。

## 文件

- `questions.json`：题目数组，字段对齐赛题题面 + `gold` + `evidence`；
- `docs/<doc_id>.md`：文档文本，首行 `# <标题>`，`## ` 分节。

## 复现

```bash
uv run python scripts/run_afac_baseline.py --mode isolate --out examples/afac-v4-isolate-k4.txt
uv run python scripts/run_afac_baseline.py --mode isolate --k-option 2 --out examples/afac-v4-isolate-k2.txt
uv run python scripts/run_afac_baseline.py --mode memory --layered --out examples/afac-v4-memory-layered.txt
```

需要 `.env` 里配置 `FINDATA_DASHSCOPE_API_KEY`（付费 API，不进 CI）。
