"""官方法规 PDF → 脚手架文档 markdown（确定性转换，可复现）。

为什么是脚本而不是手工节选：M1 的教训——手工整理时凭记忆补条文会写错
（第八十二条第二项就错过一次）。本脚本确定性切分，脚手架文档可以随时
从官方 PDF 重新生成；生成后的文档仍需人工抽查锚点条文（脚本不做语义判断）。

用法：
    uv run python scripts/build_afac_docs.py \
        --pdf data/raw/afac/zhangcheng_2023.pdf \
        --doc-id strict_csrc_035 --title 上市公司章程指引（2023年修订）\
        --out eval/fixtures/afac_scaffold/docs/strict_csrc_035.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz

ARTICLE = re.compile(r"^(第[一二三四五六七八九十百千]+条)\s?")
CHAPTER = re.compile(r"^第[一二三四五六七八九十百千]+[章节]\s*\S")
PAGE_NO = re.compile(r"^—\s*\d+\s*—$")


def extract(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    lines: list[str] = []
    for page in doc:
        for raw in page.get_text().splitlines():
            line = raw.strip()
            if not line or PAGE_NO.match(line):
                continue
            lines.append(line)
    return "\n".join(lines)


def split_articles(text: str) -> list[tuple[str, list[str]]]:
    """按「第X条」行首切分；条款号作块标题，行内剩余文字并入正文首行。

    章节标题行（第X章/节）并入下一条的正文——条款号是检索锚点，必须留在
    `## ` 标题里，不能被剥掉。
    """
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = ARTICLE.match(line)
        if m:
            rest = line[m.end() :].strip()
            blocks.append((m.group(1), [rest] if rest else []))
        else:
            blocks[-1][1].append(line)
    # 章节标题行留在其所在位置（通常是上一条末尾）——是上下文不是锚点
    result: list[tuple[str, list[str]]] = []
    for header, body in blocks:
        result.append((header, list(body)))
    return result


def to_markdown(doc_id: str, title: str, pdf_path: Path, pages: int) -> str:
    text = extract(pdf_path)
    blocks = split_articles(text)
    parts = [
        f"# {title}",
        "",
        f"> 文档编号 {doc_id}。来源：官方 PDF（{pdf_path.name}，{pages} 页），"
        "由 scripts/build_afac_docs.py 确定性生成（条文切分、去页码、断行重组），"
        "锚点条文已人工抽查。开发用脚手架文档，条文以官方发布为准。",
    ]
    preamble = "\n".join(blocks[0][1]).strip()
    if preamble:
        # 发布信息/审议通过行是元数据不是条文：进顶部 blockquote（前言块），
        # 检索层按 is_preamble 排除——写成 ## 节会被字段加权顶进召回（M2.4 教训）
        parts += ["", f"> {preamble}"]
    for header, body in blocks[1:]:
        body_text = "".join(body).strip()  # 中文条文：行间无空格重组
        if not body_text:
            continue
        parts += ["", f"## {header}", "", body_text]
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    pdf_path = Path(args.pdf)
    doc = fitz.open(pdf_path)
    md = to_markdown(args.doc_id, args.title, pdf_path, len(doc))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    n_articles = md.count("\n## ")
    print(f"{out}：{len(md):,} 字符，{n_articles} 个条文块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
