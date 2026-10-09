"""FinanceBench 检索层校准：金证据覆盖率 recall@k（确定性，无 API 调用）。

外部权威基准（Patronus AI FinanceBench，150 题开源子集）校准主线的检索层：
对每道题在**其所属文档内**检索 top-k 片段，度量标准答案所在的金证据段落
被覆盖的比例（词级覆盖率 ≥ 阈值记命中）。检索是纯 BM25F、逐配置确定——
同样的输入永远得到同样的数字。

诚实边界（随报告输出）：
- 只评检索层，不评作答（FinanceBench 是自由短答案题，主线答题头是选项判定）；
- 题目限定在单文档内检索（doc_name 已知，等价 A 榜口径），不测跨文档盲检；
- 语料为英文 10-K/10-Q：字段加权的短标签通道要求 CJK，对本基准不生效——
  这是被测数据的真实边界，报告里注明。

用法：
    uv run python scripts/eval_financebench_recall.py \
        --data data/raw/financebench/data/financebench_open_source.jsonl \
        --docs eval/fixtures/financebench/docs \
        --out examples/financebench-recall/report.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from findata.finqa.baseline import MAX_CHUNK_CHARS
from findata.finqa.retrieval import BM25Index, chunk_docs

_COVER_THRESHOLD = 0.8
_WORD = re.compile(r"[a-z0-9]+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


_NUM = re.compile(r"\d+(?:\.\d+)?")


def _num_tokens(text: str) -> set[str]:
    """数字 token：剥 $ % 与千分位逗号后提取（$1,577.00 与 1577.00 归并）。"""
    return set(_NUM.findall(text.replace(",", "").replace("$", "").replace("%", "")))


def answer_in_context(answer: str, retrieved_text: str) -> bool:
    """答案值是否入上下文：按数字 token 判定（格式无关）。"""
    nums = _num_tokens(answer)
    if not nums:
        return normalize(answer) in normalize(retrieved_text)
    ctx = _num_tokens(retrieved_text)
    return bool(nums & ctx)


def coverage(evidence_text: str, retrieved_text: str) -> float:
    """金证据的词级覆盖率：|evidence 词集 ∩ 检索文本词集| / |evidence 词集|。

    词集（不是序列）口径：10-K 的表格文本经 PDF 提取后列序不稳定，
    序列匹配会漏判；词集覆盖 0.8 作为「这段证据被找回了」的操作定义。
    """
    ev = set(_WORD.findall(evidence_text.lower()))
    if not ev:
        return 0.0
    got = set(_WORD.findall(retrieved_text.lower()))
    return len(ev & got) / len(ev)


def question_evidence(q: dict) -> str:
    """FinanceBench 的证据是嵌套列表 ``evidence: [{evidence_text: ...}]``。"""
    return "\n".join(
        e.get("evidence_text", "") for e in q.get("evidence", []) if e.get("evidence_text")
    )


def evaluate(
    questions: list[dict],
    docs: dict[str, str],
    k_values: list[int] = (4, 8, 16),
    threshold: float = _COVER_THRESHOLD,
) -> dict:
    """对每题在其所属文档内检索，报 recall@k（证据覆盖）与 answer@k（答案入上下文）。

    两个指标分开报：证据段覆盖对长证据不公平（2000+ 字符的段落天然需要多个
    chunk 才能覆盖到 0.8）；**answer@k 才是问答场景的操作性指标**——答案值
    是否进了上下文（作答头只需要答案值在场，不需要整段证据）。
    """
    index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
    per_q: list[dict] = []
    hits = {k: 0 for k in k_values}
    ans_hits = {k: 0 for k in k_values}
    evaluated = 0
    skipped_no_doc = 0
    skipped_no_evidence = 0
    skipped_no_answer = 0
    for q in questions:
        doc_name = q["doc_name"]
        evidence = question_evidence(q)
        if not evidence:
            skipped_no_evidence += 1
            continue
        if doc_name not in docs:
            skipped_no_doc += 1
            continue
        pool = [i for i, c in enumerate(index.chunks) if c.doc_id == doc_name]
        if not pool:
            skipped_no_doc += 1
            continue
        scores = index._scores(q["question"])  # noqa: SLF001 — 同包评测脚本的确定性入口
        ranked = sorted(pool, key=lambda i: scores[i], reverse=True)
        record = {
            "financebench_id": q["financebench_id"],
            "doc_name": doc_name,
            "coverages": {},
        }
        evaluated += 1
        answer = q.get("answer", "")
        if not answer:
            skipped_no_answer += 1
        for k in k_values:
            concat = "\n".join(index.chunks[i].text for i in ranked[:k])
            cov = coverage(evidence, concat)
            record["coverages"][k] = round(cov, 4)
            if cov >= threshold:
                hits[k] += 1
            if answer and answer_in_context(answer, concat):
                ans_hits[k] += 1
        per_q.append(record)
    k_hit_rates = {
        k: {"hits": hits[k], "evaluated": evaluated,
            "rate": round(hits[k] / evaluated, 4) if evaluated else 0.0}
        for k in k_values
    }
    k_answer_rates = {
        k: {"hits": ans_hits[k], "evaluated": evaluated,
            "rate": round(ans_hits[k] / evaluated, 4) if evaluated else 0.0}
        for k in k_values
    }
    return {
        "threshold": threshold,
        "evaluated": evaluated,
        "skipped_no_doc": skipped_no_doc,
        "skipped_no_evidence": skipped_no_evidence,
        "skipped_no_answer": skipped_no_answer,
        "recall_at_k": k_hit_rates,
        "answer_at_k": k_answer_rates,
        "per_question": per_q,
    }


def summary_lines(result: dict, source: str) -> list[str]:
    lines = [
        "# FinanceBench 检索层校准"
        f"（证据覆盖 ≥ {result['threshold']} 记 recall；answer@k = 答案值入上下文）",
        "",
        f"语料来源：{source}",
        f"评测题数：{result['evaluated']}（无文档文本跳过 {result['skipped_no_doc']}，"
        f"无证据字段跳过 {result['skipped_no_evidence']}，"
        f"无答案字段跳过 {result['skipped_no_answer']}）",
        "",
        "| k | recall（证据覆盖） | answer@k（答案入上下文） |",
        "| ---: | ---: | ---: |",
    ]
    for k in result["recall_at_k"]:
        r = result["recall_at_k"][k]["rate"]
        a = result["answer_at_k"][k]["rate"]
        lines.append(f"| {k} | {r:.2%} | {a:.2%} |")
    lines += [
        "",
        "诚实边界：只评检索层（英文 10-K/10-Q，字段加权短标签通道要求 CJK 不生效）；",
        "单文档内检索（doc 已知，A 榜口径），不测跨文档盲检；纯 BM25F 确定性可复现。",
    ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--docs", required=True, help="提取好的文档 markdown 目录")
    parser.add_argument("--out", required=True)
    parser.add_argument("--source", default="FinanceBench open-source subset")
    args = parser.parse_args()
    questions = [
        json.loads(line)
        for line in Path(args.data).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    docs = {}
    for path in sorted(Path(args.docs).glob("*.md")):
        docs[path.stem] = path.read_text(encoding="utf-8")
    result = evaluate(questions, docs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = out.with_suffix(".md")
    report.write_text(
        "\n".join(summary_lines(result, args.source)) + "\n", encoding="utf-8"
    )
    for line in summary_lines(result, args.source)[4:10]:
        print(line)
    print(f"已写：{out} 与 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
