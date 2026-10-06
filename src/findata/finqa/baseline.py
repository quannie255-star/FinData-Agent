"""M1 无压缩 baseline 与 M2 检索臂：同一 run_baseline，两种上下文构造。

- full：全文直入（A 榜口径，官方 baseline 的本地复刻）——朴素臂；
- retrieve：BM25 条款级检索后片段直入（M2）——检索臂，零模型调用检索，
  只有作答花 token。

两臂共用同一作答 prompt 与解码参数，唯一变量是进上下文的内容——
这是后续 M5 配对验证的实验形态（对照数字只陈述，不主张显著性）。
"""

from __future__ import annotations

import re

from findata.finqa.fixture import Question, doc_title
from findata.finqa.qwen import QwenClient, TokenLedger
from findata.finqa.retrieval import BM25Index, Chunk, chunk_docs
from findata.finqa.scoring import ScoreReport, is_correct, normalize_answer

# 超长块二级切分阈值：报表整节可达数千字符，不切会吃掉整个判定上下文
MAX_CHUNK_CHARS = 1200

SYSTEM_PROMPT = (
    "你是严格的金融合规分析师。只能依据提供的文档内容作答，"
    "禁止使用外部知识或常识推测；文档没有依据时也要在文档内找最接近的条款判断。"
)

_ANSWER_LINE = re.compile(r"ANSWER\s*[:：]\s*([A-Da-d]+)")


def extract_answer(reply: str) -> str:
    """从模型回复里取最后一行 ANSWER 标记；没有则返回空串（按题面计错）。

    取最后一个是刻意的：允许模型先推理再收敛，防止它在开头试探性提到的
    字母抢答。规范化的活交给 scoring.normalize_answer，这里只做抽取。
    """
    matches = _ANSWER_LINE.findall(reply)
    return matches[-1] if matches else ""


def build_messages(q: Question, docs: dict[str, str]) -> list[dict[str, str]]:
    """A 榜口径：doc_ids 给定，按序全文拼接（无压缩、无检索）。"""
    if not q.doc_ids:
        raise ValueError(
            f"{q.qid}: baseline 为 A 榜口径，缺 doc_ids（B 榜题需先过 M2 检索层）"
        )
    doc_blocks = []
    for i, doc_id in enumerate(q.doc_ids, start=1):
        text = docs[doc_id]
        doc_blocks.append(f"【文档{i}】{doc_id} 《{doc_title(text)}》\n{text}")
    options_block = "\n".join(f"{k}. {v}" for k, v in sorted(q.options.items()))
    if q.answer_format == "multi":
        answer_rule = "多选题：选出所有正确选项，按字母顺序连续书写，如 ANSWER: ACD"
    elif q.answer_format == "tf":
        answer_rule = "判断题：只输出一个选项字母（A 或 B，含义以选项内容为准）"
    else:
        answer_rule = "单选题：只输出一个选项字母"
    user = (
        "\n\n".join(doc_blocks)
        + f"\n\n【问题】{q.question}\n【选项】\n{options_block}\n【题型】{q.format_label()}\n"
        f"【作答要求】先用一两句话给出依据（引用条文编号），"
        f"然后另起一行严格按格式输出最终答案：ANSWER: <大写字母>。{answer_rule}。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def query_text(q: Question) -> str:
    """检索 query = 题干 + 选项（选项承载关键术语，如「资产负债率」「三分之二」）。"""
    options = " ".join(q.options.values())
    return f"{q.question} {options}"


def option_queries(q: Question) -> list[str]:
    """逐选项查询组（M2.1）：题干 + 每个选项各一查，每个选项的证据诉求独立召回。

    tf 题的选项只是「正确/错误」，无信息量——退回单查询（题干+选项全文）。
    """
    if q.answer_format == "tf":
        return [query_text(q)]
    return [f"{q.question} {text}" for text in q.options.values()]


def build_messages_from_chunks(q: Question, chunks: list[Chunk]) -> list[dict[str, str]]:
    """检索臂的上下文：BM25 召回的条款片段，块头带 doc_id 与条款号。"""
    options_block = "\n".join(f"{k}. {v}" for k, v in sorted(q.options.items()))
    if q.answer_format == "multi":
        answer_rule = "多选题：选出所有正确选项，按字母顺序连续书写，如 ANSWER: ACD"
    elif q.answer_format == "tf":
        answer_rule = "判断题：只输出一个选项字母（A 或 B，含义以选项内容为准）"
    else:
        answer_rule = "单选题：只输出一个选项字母"
    doc_blocks = [c.block() for c in chunks] or ["（检索未命中任何片段）"]
    user = (
        "以下是与问题相关的文档检索片段（可能不完整，仍只能依据片段内容作答）：\n\n"
        + "\n\n".join(doc_blocks)
        + f"\n\n【问题】{q.question}\n【选项】\n{options_block}\n【题型】{q.format_label()}\n"
        f"【作答要求】先用一两句话给出依据（引用条文编号），"
        f"然后另起一行严格按格式输出最终答案：ANSWER: <大写字母>。{answer_rule}。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def run_baseline(
    questions: list[Question],
    docs: dict[str, str],
    client: QwenClient,
    limit: int = 0,
    mode: str = "full",
    k: int = 6,
    k_total: int = 12,
) -> tuple[list[dict[str, object]], TokenLedger, ScoreReport, list[str]]:
    """逐题作答；返回逐题明细 / 台账 / 评分报告 / 被跳过的 qid 列表。

    full 模式只处理给了 doc_ids 的题（B 榜题检索臂专属，跳过并记录）；
    retrieve 模式处理全部题——A 榜题在给定文档内检索，B 榜题全局盲检。
    检索为逐选项多查询合并（M2.1）：k 为每查询取多少、k_total 为合并上限。
    """
    if mode not in ("full", "retrieve"):
        raise ValueError(f"mode 必须是 full/retrieve，得到 {mode}")
    selected = questions[: limit or None]
    index = (
        BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
        if mode == "retrieve"
        else None
    )
    skipped: list[str] = []
    ledger = TokenLedger()
    report = ScoreReport()
    details: list[dict[str, object]] = []
    for q in selected:
        if mode == "full":
            if not q.doc_ids:
                skipped.append(q.qid)
                continue
            messages = build_messages(q, docs)
        else:
            assert index is not None
            chunks = index.retrieve_many(
                option_queries(q), k_per_query=k, total_cap=k_total,
                doc_ids=q.doc_ids or None,
            )
            messages = build_messages_from_chunks(q, chunks)
        reply, record = client.chat(q.qid, "answer", messages)
        ledger.add(record)
        pred = normalize_answer(extract_answer(reply), q.answer_format)
        ok = is_correct(pred, q.gold, q.answer_format)
        report.add(q.qid, q.domain, ok, record.prompt_tokens, record.completion_tokens)
        details.append(
            {
                "qid": q.qid,
                "domain": q.domain,
                "answer_format": q.answer_format,
                "gold": q.gold,
                "pred": pred,
                "correct": ok,
                "reply": reply,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
            }
        )
    return details, ledger, report, skipped
