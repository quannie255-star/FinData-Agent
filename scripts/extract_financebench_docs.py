"""FinanceBench PDF → 文档 markdown（按页分节，供检索层切块）。

对每份 PDF 用 PyMuPDF 确定性提取，每页一节 `## Page N`——法规文档的「条款号」
锚点在 10-K 里不存在，页号是这类文档最稳定的分节锚点；超长页由检索层
二级切分兜底。输出到 eval/fixtures/financebench/docs/<doc_name>.md。

用法（在含 pdfs/ 的 FinanceBench 仓库内）：
    uv run python scripts/extract_financebench_docs.py \
        --pdfs-dir data/raw/financebench/pdfs \
        --out-dir eval/fixtures/financebench/docs
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf


def extract_pdf(pdf_path: Path, doc_name: str) -> str:
    doc = pymupdf.open(pdf_path)
    parts = [
        f"# {doc_name}",
        "",
        f"> 文档编号 {doc_name}。来源：FinanceBench 官方仓库 PDF（SEC 10-K/10-Q/"
        f"Earnings，{len(doc)} 页），PyMuPDF 确定性提取（按页分节）。"
        "外部基准校准语料。",
    ]
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if text:
            parts += ["", f"## Page {i}", "", text]
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdfs-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for pdf_path in sorted(Path(args.pdfs_dir).glob("*.pdf")):
        md = extract_pdf(pdf_path, pdf_path.stem)
        out = out_dir / f"{pdf_path.stem}.md"
        out.write_text(md, encoding="utf-8")
        print(f"{out.name}：{len(md):,} 字符，{md.count(chr(10) + '## Page')} 页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
