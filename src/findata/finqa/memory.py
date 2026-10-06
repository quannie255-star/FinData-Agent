"""M3 记忆/压缩朴素臂：滚动摘要工作记忆（标准做法，当一切压缩策略的基线）。

流程（每题）：

1. 一个大证据池（逐选项轮转检索合并，k_pool 个片段）；
2. 分批读入（batch 个/轮），每轮把「已有事实记忆 + 新片段」滚动压成
   ≤max_facts 字的事实清单——保留条款号、精确数值与单位、阈值、结论关系，
   丢弃冗余与无关；
3. **选项判定只看记忆不看原文**（判定语义沿用 isolate 的「应选入答案」）；
4. 拼不出合法答案回退为「记忆 + 合并作答」单次调用。

对照口径：isolate 臂判定看原文片段、memory 臂看压缩记忆——两臂同题同解码，
差异即「压缩的准确性代价 + token 曲线」（docs/afac-track4.md M3 验收）。

与 §3.5 轮子表的偏离（如实记录）：未用 LangChain memory 组件——它是为
聊天历史设计的（消息拼接/load/save 管道），与本场景「事实清单式工作记忆」
不贴合；滚动摘要本体是约百行的标准编排模式，不是算法轮子。
"""

from __future__ import annotations

import re

from findata.finqa.attribution import extract_citations, match_citations
from findata.finqa.baseline import (
    MAX_CHUNK_CHARS,
    extract_answer,
    option_queries,
)
from findata.finqa.fixture import Question
from findata.finqa.isolate import (
    assemble_mcq,
    assemble_multi,
    build_judge_messages,
    extract_verdict,
)
from findata.finqa.qwen import QwenClient, TokenLedger
from findata.finqa.retrieval import BM25Index, chunk_docs
from findata.finqa.scoring import ScoreReport, is_correct, normalize_answer

MEMORIZE_SYSTEM_PROMPT = (
    "你是金融文档事实提取器。把已有记忆与新片段合并成紧凑的事实清单，"
    "只保留与回答给定问题可能相关的事实。"
)


def summarize_messages(
    question: str, old_facts: str, new_blocks: list[str], max_facts: int
) -> list[dict[str, str]]:
    old = old_facts or "（空）"
    user = (
        f"【问题】{question}\n\n"
        f"【已有事实记忆】\n{old}\n\n"
        "【新读取片段】\n"
        + "\n\n".join(new_blocks)
        + f"\n\n【要求】输出合并后的新事实清单，不超过 {max_facts} 字：\n"
        "- 每行一条事实，行首保留【文档编号·条款号/节号】出处前缀；\n"
        "- **精确保留**数值与单位、日期、比例与阈值（如 803,964,958,000.00 元、"
        "百分之七十、90 日），数字不得改写或省略；\n"
        "- 保留结论性关系（谁必须做什么、何种情形属于何种集合）；\n"
        "- 丢弃与问题无关及重复的内容；只输出清单本身，不要解释。"
    )
    return [
        {"role": "system", "content": MEMORIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_answer_from_memory(q: Question, facts: str) -> list[dict[str, str]]:
    """tf 题与回退仲裁：以记忆为唯一上下文作答。"""
    options_block = "\n".join(f"{k}. {v}" for k, v in sorted(q.options.items()))
    user = (
        "以下是围绕问题整理的事实记忆（来自文档检索片段的压缩）：\n\n"
        f"{facts}\n\n"
        f"【问题】{q.question}\n【选项】\n{options_block}\n【题型】{q.format_label()}\n"
        "【作答要求】只能依据记忆作答，先给一句依据，然后另起一行严格按格式输出："
        "ANSWER: <大写字母>。"
    )
    return [
        {"role": "system", "content": "你是严格的金融合规分析师，只能依据提供的内容作答。"},
        {"role": "user", "content": user},
    ]


_FACT_PREFIX = re.compile(r"^【([^【】]+)】")


def _facts_units(facts: str) -> tuple[list[tuple[str, str, int]], int]:
    """把记忆清单切成可归因单元：带【doc·条款号】前缀的行。

    无前缀行计为不可归因字符（不进使用率分母——摘要 prompt 已约束加前缀，
    无前缀说明压缩环节违规，单独暴露它）。
    """
    units: list[tuple[str, str, int]] = []
    unattributable = 0
    for line in facts.splitlines():
        m = _FACT_PREFIX.match(line.strip())
        if m and "·" in m.group(1):
            doc, _, header = m.group(1).partition("·")
            units.append((doc.strip(), header.strip(), len(line)))
        else:
            unattributable += len(line)
    return units, unattributable


def _observe_facts(
    attributions: list, replies: dict, units: list, purpose: str, reply: str
) -> None:
    """M4 记忆行级引用观测：判定/作答回复引用了哪些事实行。"""
    used = match_citations(
        extract_citations(reply), [(d, h) for d, h, _ in units]
    )
    attributions.append({"purpose": purpose, "fed": units, "used_idx": sorted(used)})
    replies[purpose] = reply[:1200]


def run_memoryqa(
    questions: list[Question],
    docs: dict[str, str],
    client: QwenClient,
    limit: int = 0,
    k_pool: int = 10,
    batch: int = 4,
    max_facts: int = 400,
) -> tuple[list[dict[str, object]], TokenLedger, ScoreReport, list[str]]:
    """记忆臂作答；返回结构与 run_baseline/run_isolate 一致（skipped 恒为空）。"""
    index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
    ledger = TokenLedger()
    report = ScoreReport()
    details: list[dict[str, object]] = []
    for q in questions[: limit or None]:
        pool = index.retrieve_many(
            option_queries(q), k_per_query=6, total_cap=k_pool,
            doc_ids=q.doc_ids or None,
        )
        pool_chars = sum(len(c.text) for c in pool)
        facts = ""
        rounds = 0
        for i in range(0, len(pool), batch):
            blocks = [c.block() for c in pool[i : i + batch]]
            reply, record = client.chat(
                q.qid, f"memorize:{rounds + 1}",
                summarize_messages(q.question, facts, blocks, max_facts),
            )
            ledger.add(record)
            facts = reply.strip()
            rounds += 1
        units, unattributable_chars = _facts_units(facts)
        attributions: list[dict[str, object]] = []
        replies: dict[str, str] = {}
        verdicts: dict[str, str] = {}
        if q.answer_format != "tf":
            for letter, text in sorted(q.options.items()):
                reply, record = client.chat(
                    q.qid, f"judge:{letter}",
                    build_judge_messages(q.question, letter, text, facts, q.format_label()),
                )
                ledger.add(record)
                _observe_facts(attributions, replies, units, f"judge:{letter}", reply)
                verdicts[letter] = extract_verdict(reply) or "UNCERTAIN"
        if q.answer_format == "multi":
            pred = assemble_multi(verdicts)
        elif q.answer_format == "mcq":
            pred = assemble_mcq(verdicts) or ""
        else:
            pred = ""
        used_fallback = False
        if not pred:
            used_fallback = True
            reply, record = client.chat(
                q.qid, "answer", build_answer_from_memory(q, facts)
            )
            ledger.add(record)
            _observe_facts(attributions, replies, units, "answer", reply)
            pred = normalize_answer(extract_answer(reply), q.answer_format)
        prompt_sum = sum(r.prompt_tokens for r in ledger.records if r.qid == q.qid)
        completion_sum = sum(r.completion_tokens for r in ledger.records if r.qid == q.qid)
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
                "used_fallback": used_fallback,
                "memory_rounds": rounds,
                "pool_chars": pool_chars,
                "memory_chars": len(facts),
                "compression_ratio": round(pool_chars / max(len(facts), 1), 2),
                "unattributable_chars": unattributable_chars,
                "facts": facts[:1500],
                "attributions": attributions,
                "replies": replies,
                "prompt_tokens": prompt_sum,
                "completion_tokens": completion_sum,
            }
        )
    return details, ledger, report, []
