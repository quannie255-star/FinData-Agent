"""个人文档库：自有文档（PDF/Word/TXT/MD）→ 检索 → 自由问答。

与评测线（fixture + 选项判定）的关系：共用检索层（BM25F + 分块）与模型
客户端（token 台账），答题头换成**自由作答**——个人场景没有选项可判，
答案是自由文本，但纪律不变：

- 只许依据文档作答，文档没有就明说「未提及」（不编造）；
- 答案必须带【文档·Section】出处引用，引用经在场匹配校验（M4 归因规则）；
- token 台账覆盖全部调用。

用户数据一律放 ``findata_userdata/``（gitignore）：documents/ 存原件，
converted/ 存转换后的 markdown（`## ` 分节锚点供检索层切块）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from findata.finqa.attribution import extract_citations, match_citations
from findata.finqa.baseline import MAX_CHUNK_CHARS
from findata.finqa.qwen import QwenClient
from findata.finqa.retrieval import BM25Index, chunk_docs

SAFE_NAME = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff.]+")
_WS = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n\s*\n+")
_ANSWER = re.compile(r"ANSWER\s*[:：]\s*(.+)")


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    cleaned = SAFE_NAME.sub("_", stem).strip("._") or "document"
    return cleaned[:80]


# ── 转换 ──


def convert_pdf(pdf_path: Path, doc_name: str) -> str:
    doc = pymupdf.open(pdf_path)
    parts = [f"# {doc_name}", ""]
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if text:
            parts += ["", f"## Page {i}", "", text]
    return "\n".join(parts) + "\n"


def convert_docx(docx_path: Path, doc_name: str) -> str:
    import docx  # python-docx

    document = docx.Document(str(docx_path))
    parts = [f"# {doc_name}", ""]
    section = 0
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if (para.style.name or "").startswith("Heading"):
            section += 1
            parts += ["", f"## Heading-{section} {text[:60]}"]
        else:
            parts.append(text)
    return "\n".join(parts) + "\n"


def convert_textlike(text_path: Path, doc_name: str) -> str:
    text = text_path.read_text(encoding="utf-8", errors="replace").strip()
    if "## " not in text:
        # 无分节锚点的纯文本：按 2400 字符插 Section 锚点（供检索层分块）
        parts = [text[i : i + 2400] for i in range(0, len(text), 2400)]
        text = "\n\n".join(
            f"## Section {i}\n\n{part}" for i, part in enumerate(parts, start=1)
        )
    return f"# {doc_name}\n\n{text}\n"


def convert_file(path: Path, docs_dir: Path, converted_dir: Path) -> Path:
    """原件已在 docs_dir 下；转换为 converted_dir/<stem>.md 并返回。"""
    doc_name = safe_stem(path.name)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        md = convert_pdf(path, doc_name)
    elif suffix == ".docx":
        md = convert_docx(path, doc_name)
    elif suffix in (".txt", ".md"):
        md = convert_textlike(path, doc_name)
    else:
        raise ValueError(f"不支持的格式：{suffix}（支持 pdf/docx/txt/md）")
    converted_dir.mkdir(parents=True, exist_ok=True)
    out = converted_dir / f"{doc_name}.md"
    out.write_text(md, encoding="utf-8")
    return out


# ── 自由问答 ──

FREE_QA_SYSTEM_PROMPT = (
    "你是严谨的文档问答助手。只能依据提供的文档片段回答，"
    "禁止使用外部知识或常识推测。"
)


def build_free_qa_messages(question: str, chunks_text: str) -> list[dict[str, str]]:
    user = (
        "以下是用户文档中与问题相关的检索片段：\n\n"
        f"{chunks_text}\n\n"
        f"【问题】{question}\n\n"
        "【作答要求】\n"
        "- 只依据片段内容回答，引用出处用【文档编号·Section】标注；\n"
        "- 片段不足以回答时，明确写「提供的文档片段中没有提及」，不许推测；\n"
        "- 回答简洁，不超过 300 字。"
    )
    return [
        {"role": "system", "content": FREE_QA_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def ask_free(
    question: str,
    docs: dict[str, str],
    client: QwenClient,
    k_total: int = 12,
) -> dict:
    """自由问答：检索 → 单次作答 → 引用归因 + token 台账。"""
    index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
    chunks = index.retrieve_many([question], k_per_query=6, total_cap=k_total)
    chunks_text = "\n\n".join(c.block() for c in chunks) or "（文档库中没有命中内容）"
    reply, record = client.chat(
        "free_qa", "free_answer", build_free_qa_messages(question, chunks_text)
    )
    units = [(c.doc_id, c.header, len(c.text)) for c in chunks]
    used = match_citations(extract_citations(reply), [(d, h) for d, h, _ in units])
    answer = reply.strip()
    m = _ANSWER.search(answer)
    if m:  # 模型若按 ANSWER: 格式收尾则取正文
        answer = (answer[: m.start()]).strip() or answer
    return {
        "question": question,
        "answer": answer[:2000],
        "citations": [
            {"doc_id": doc_id, "section": header}
            for i, (doc_id, header, _chars) in enumerate(units)
            if i in used
        ],
        "retrieved_sections": len(units),
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "total_tokens": record.total_tokens,
    }
