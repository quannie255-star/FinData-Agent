# FinanceBench 检索层三臂消融（v3：BM25F / 向量 / 混合，2026-10-10）

> L2 深化立项（`docs/next-cycle-roadmap.md` §3.1.1）的验收归档。
> 目的：定位「纯词法失配」这一已识别瓶颈能否被向量臂/融合解除，并给出下一杠杆。
> 复现：见文末命令。原始产物：本目录 `bm25/` `vector/` `hybrid/`（各 report.json + report.md）。

## 1. 三臂读数（FinanceBench open-source，99 题，单文档内检索）

| 检索臂 | recall@4 | recall@8 | recall@16 | answer@4 | answer@8 | answer@16 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25`（纯 BM25F，赛题口径） | 11.11% | 16.16% | 25.25% | 31.31% | 37.37% | 37.37% |
| `vector`（纯向量，bge-small-en-v1.5） | **27.27%** | **41.41%** | **55.56%** | 34.34% | 37.37% | **41.41%** |
| `hybrid`（BM25F + 向量，等权 RRF k=60） | 14.14% | 21.21% | 37.37% | **35.35%** | 37.37% | 39.39% |

（recall = 金证据词级覆盖 ≥ 0.8 记命中；answer@k = 答案值是否进 top-k 上下文。口径与实现见
`scripts/eval_financebench_recall.py`。）

## 2. 可引用的四条结论（含负面）

1. **向量臂是真正的杠杆，不是融合**：recall@8 纯 BM25F 16.16% → 纯向量 **41.41%**（+25.25pp），
   recall@16 25.25% → **55.56%**（+30.31pp）。这直接印证了 v2 的「纯词法失配」判断——
   题面与 10-K 行文词汇不同，字符/语义级匹配能救回的正是词法匹配丢掉的。
2. **等权 RRF 融合反而弱于纯向量臂**（recall@8 21.21% vs 41.41%，recall@4 14.14% vs 27.27%）。
   诚实解释：RRF 只用名次，当两臂强弱悬殊（BM25F recall@8 仅 16%）时，等权融合会把一半名次位
   让给劣质臂，稀释了向量臂的召回。**结论：融合要见效需加权（或向量主导），"零调参等权 RRF"
   在本语料上不是正解。**
3. **answer@8 三臂相同（均 37.37%）**：k=8 的拼接上下文（8×2400 字符）已大到答案数字常在里面，
   「答案入上下文」这一操作性指标在 k=8 处对检索臂不敏感。差异体现在 recall（证据段覆盖）与
   answer@16（向量臂 41.41% 最高）。**说明单看 answer@8 会漏掉检索质量差异，必须两个指标并报。**
4. **§3.1.1 门禁目标未达**：目标 answer@8 ≥ 50%（基线 37.37%）——三臂最好也只到 37.37%
   （k=8）/ 41.41%（k=16）。**未达即记录，不带病调参**。下一杠杆按 §2.2 的顺序是加权融合或
   「向量召回 + BM25 重排」两阶段，而不是继续调 RRF 权重刷单个数字。

## 3. 确定性与缓存（可核对）

- **bm25 臂与 v2 归档逐位一致**（本目录 `bm25/report.json` 与 `examples/financebench-recall-v2/report.json`
  的三项 recall / 三项 answer 全部相同）——纯 BM25F 逐配置确定性，无随机性来源。
- **hybrid 臂 `embed_calls = 0`**：向量臂的嵌入全部命中内容寻址缓存（`data/embed-cache/`，gitignore），
  证明缓存生效、同配置两次运行不重复嵌入。vector 臂 `embed_calls = 8982`（本机首次补嵌）。
- 向量后端本机 ONNX 推理（fastembed），**文本不出本机**，`embed_usage_prompt_tokens = 0`。

## 4. 诚实边界

- **只评检索层，不评作答**：FinanceBench 是自由短答案题，主线答题头是选项判定，二者不可直接换算。
- **单文档内检索**（`doc_name` 已知，等价 A 榜口径），**不测跨文档盲检**（B 榜口径未测）。
- **产品口径不是赛题口径**：向量臂参与检索**违反 AFAC 赛题四正式答题纪律**（禁 embedding）——
  本节结果仅用于**产品模式**（个人文档库问答），对外引用凡涉及赛题纪律必须注明此差异
  （`docs/next-cycle-roadmap.md` §3.1.1 / §5 决策点 1）。赛题口径评测线（isolate/baseline/submit）
  **不 import 向量模块**，仍为纯 BM25F。
- **向量输入截断至模型上限**（bge 系 512 token）——向量臂只见块前部，BM25F 仍覆盖全文。

## 5. 复现命令

```bash
# 前置：uv sync --extra embed（本机 ONNX 向量推理）
D=data/raw/financebench/data/financebench_open_source.jsonl
DOCS=eval/fixtures/financebench/docs

uv run python scripts/eval_financebench_recall.py --data $D --docs $DOCS \
    --retriever bm25   --out examples/financebench-recall-v3/bm25/report.json
uv run python scripts/eval_financebench_recall.py --data $D --docs $DOCS \
    --retriever vector --out examples/financebench-recall-v3/vector/report.json
uv run python scripts/eval_financebench_recall.py --data $D --docs $DOCS \
    --retriever hybrid --out examples/financebench-recall-v3/hybrid/report.json
```

（纯 BM25F 臂无外部依赖；向量/混合臂需 `--extra embed`，首次运行嵌入全语料约需数十分钟，
之后走缓存秒级。）
