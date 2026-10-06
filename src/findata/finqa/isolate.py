"""M2.2 选项级证据隔离：每选项独立判定 + 确定性拼装。

v2.1 的失败形态是「过包含」——证据都召回齐了，但 A/C 的证据片段与 D 的
干扰条款在同一上下文里，四线并行推理时被带偏（reg_s_001 多选 D）。
本模块把判断拆到选项粒度：

- 每个选项只用**自己**召回的片段，独立输出三态判定 TRUE / FALSE / UNCERTAIN；
- **UNCERTAIN 不入选**（保守——多选无部分分，漏选错选同罪，宁可交白卷
  也不猜）；
- mcq/multi 的最终答案由代码拼装，不靠模型的格式纪律；
- 拼不出合法答案（multi 空集 / mcq 无唯一 TRUE）时，回退到合并上下文的
  单次作答（v2.1 路径）——结构上不劣于上一代。

tf 题选项只是「正确/错误」无信息量，不走隔离，直接合并上下文单次作答。
"""

from __future__ import annotations

import re

from findata.finqa.baseline import (
    MAX_CHUNK_CHARS,
    build_messages_from_chunks,
    extract_answer,
    option_queries,
)
from findata.finqa.fixture import Question
from findata.finqa.qwen import QwenClient, TokenLedger
from findata.finqa.retrieval import BM25Index, chunk_docs
from findata.finqa.scoring import ScoreReport, is_correct, normalize_answer

JUDGE_SYSTEM_PROMPT = (
    "你是严格的金融合规分析师。只能依据提供的文档片段判断，"
    "禁止使用外部知识或常识推测。"
)

_VERDICT_LINE = re.compile(r"VERDICT\s*[:：]\s*(TRUE|FALSE|UNCERTAIN)", re.IGNORECASE)


def build_judge_messages(
    question: str, letter: str, option_text: str, chunks_text: str, fmt_label: str
) -> list[dict[str, str]]:
    """判定语义是「该选项是否应选入本题答案」，不是「陈述是否为真」。

    M2.2 的教训（reg_s_009 选项 C）：陈述为真 ≠ 选项该选——「独董每年自查」
    是真话，但不在「不得担任情形」的集合里。FALSE 必须明确涵盖
    「陈述虽真实、但不属于题目所问」这一支。
    """
    user = (
        "以下是与该选项相关的文档检索片段：\n\n"
        f"{chunks_text}\n\n"
        f"【题目】{question}\n【题型】{fmt_label}\n\n"
        f"【待判定选项】选项{letter}：{option_text}\n\n"
        "【判定要求】判断该选项是否应作为本题正确答案的一部分被选入，"
        "只能依据片段：\n"
        "- 片段提供充分依据，该选项正确且符合题目所问 → VERDICT: TRUE（应选入）\n"
        "- 片段提供充分依据表明不应选入——陈述错误、与片段矛盾、"
        "或陈述虽真实但不属于题目所问的集合 → VERDICT: FALSE\n"
        "- 片段依据不足、无法判断 → VERDICT: UNCERTAIN\n"
        "先引用条文或数据给一句依据，然后另起一行严格按格式输出最终判定。"
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def extract_verdict(reply: str) -> str:
    """取最后一行 VERDICT 标记；解析不出返回 ""（按 UNCERTAIN 处理）。"""
    matches = _VERDICT_LINE.findall(reply)
    return matches[-1].upper() if matches else ""


def assemble_multi(verdicts: dict[str, str]) -> str:
    """多选拼装：TRUE 全入选，UNCERTAIN 不入选（保守，防过包含）。"""
    letters = sorted(letter for letter, v in verdicts.items() if v == "TRUE")
    return "".join(letters)


def assemble_mcq(verdicts: dict[str, str]) -> str | None:
    """单选拼装：恰好一个 TRUE 才返回字母；否则返回 None（走回退仲裁）。"""
    trues = sorted(letter for letter, v in verdicts.items() if v == "TRUE")
    return trues[0] if len(trues) == 1 else None


def run_isolate(
    questions: list[Question],
    docs: dict[str, str],
    client: QwenClient,
    limit: int = 0,
    k: int = 6,
    k_total: int = 12,
    k_option: int = 4,
) -> tuple[list[dict[str, object]], TokenLedger, ScoreReport, list[str]]:
    """选项级隔离作答；返回结构与 run_baseline 一致（skipped 恒为空）。

    k_option 是每选项召回的片段数（M2.3：12 → 4——聚焦判定不需要大片池，
    token ×3.1 的主因就是它）；k_total 只作用于回退仲裁的合并检索。
    """
    index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
    ledger = TokenLedger()
    report = ScoreReport()
    details: list[dict[str, object]] = []
    for q in questions[: limit or None]:
        verdicts: dict[str, str] = {}
        prompt_sum = completion_sum = 0
        if q.answer_format != "tf":
            # tf 的选项只是「正确/错误」，无信息量——不逐选项判定
            for letter, text in sorted(q.options.items()):
                chunks = index.retrieve_many(
                    [f"{q.question} {text}"], k_per_query=k_option, total_cap=k_option,
                    doc_ids=q.doc_ids or None,
                )
                chunks_text = (
                    "\n\n".join(c.block() for c in chunks) or "（检索未命中任何片段）"
                )
                reply, record = client.chat(
                    q.qid, f"judge:{letter}",
                    build_judge_messages(q.question, letter, text, chunks_text, q.format_label()),
                )
                ledger.add(record)
                prompt_sum += record.prompt_tokens
                completion_sum += record.completion_tokens
                verdicts[letter] = extract_verdict(reply) or "UNCERTAIN"
        if q.answer_format == "multi":
            pred = assemble_multi(verdicts)
        elif q.answer_format == "mcq":
            pred = assemble_mcq(verdicts) or ""
        else:  # tf：直接走合并上下文单次作答
            pred = ""
        fallback_reply = ""
        if not pred:
            # 回退仲裁：合并上下文单次作答（v2.1 路径），不劣于上一代
            chunks = index.retrieve_many(
                option_queries(q), k_per_query=k, total_cap=max(k_total, 12),
                doc_ids=q.doc_ids or None,
            )
            fallback_reply, record = client.chat(
                q.qid, "answer", build_messages_from_chunks(q, chunks)
            )
            ledger.add(record)
            prompt_sum += record.prompt_tokens
            completion_sum += record.completion_tokens
            pred = normalize_answer(extract_answer(fallback_reply), q.answer_format)
        ok = is_correct(pred, q.gold, q.answer_format)
        report.add(q.qid, q.domain, ok, prompt_sum, completion_sum)
        details.append(
            {
                "qid": q.qid,
                "domain": q.domain,
                "answer_format": q.answer_format,
                "gold": q.gold,
                "pred": pred,
                "correct": ok,
                "verdicts": verdicts,
                "used_fallback": bool(fallback_reply),
                "prompt_tokens": prompt_sum,
                "completion_tokens": completion_sum,
            }
        )
    return details, ledger, report, []
