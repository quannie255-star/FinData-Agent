"""M5 证据导出：把运行归档转成题面 B 榜审核要求的 evidence_retrieval 格式。

题面格式（docs/afac-track4.md §0 引用）：

    {"qid": ..., "answer": ..., "evidence_retrieval": [
        {"doc_id": ..., "quoted_clause": ..., "reasoning": ...}]}

原料来自运行 json 的 attributions（每题每次调用喂了什么、用了哪些）与
replies（判定依据原文）。quoted_clause 由 fixture 重新确定性分块后按
(doc_id, header) 回查——被 (i/n) 切分的同名块取首个，口径在输出里注明。

用法：
    uv run python scripts/export_evidence.py examples/afac-m4-isolate-attribution.json \
        --out evidence.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from findata.finqa.baseline import MAX_CHUNK_CHARS
from findata.finqa.retrieval import chunk_docs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUOTE_LIMIT = 90
REASONING_LIMIT = 220


def build_evidence(run_path: Path, fixture_root: Path) -> list[dict]:
    data = json.loads(run_path.read_text(encoding="utf-8"))
    runs = data["runs"]
    mode = next(iter(runs))
    from findata.finqa import fixture as fx

    docs = fx.load_docs(fixture_root)
    chunk_text: dict[tuple[str, str], str] = {}
    for c in chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS):
        chunk_text.setdefault((c.doc_id, c.header), c.text)

    out: list[dict] = []
    for d in runs[mode]["details"]:
        evidence = []
        for attr in d.get("attributions", []):
            reply = d.get("replies", {}).get(attr["purpose"], "")
            reasoning = reply[:REASONING_LIMIT].replace("\n", " ")
            for idx in attr["used_idx"]:
                doc_id, header, _chars = attr["fed"][idx]
                text = chunk_text.get((doc_id, header), "")
                quote = f"{header}：{text[:QUOTE_LIMIT]}" if text else header
                evidence.append(
                    {
                        "doc_id": doc_id,
                        "quoted_clause": quote + ("…" if len(text) > QUOTE_LIMIT else ""),
                        "reasoning": reasoning,
                    }
                )
        out.append({"qid": d["qid"], "answer": d["pred"], "evidence_retrieval": evidence})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json")
    parser.add_argument("--fixture", default="eval/fixtures/afac_scaffold")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    evidence = build_evidence(PROJECT_ROOT / args.run_json, PROJECT_ROOT / args.fixture)
    out_path = PROJECT_ROOT / args.out
    out_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    n_with_ev = sum(1 for e in evidence if e["evidence_retrieval"])
    print(f"{out_path}：{len(evidence)} 题，{n_with_ev} 题带证据"
          f"（quoted_clause 按 (doc_id, header) 回查首个同名块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
