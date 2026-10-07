"""提交物：把运行归档写成题面 answer.csv（两种 schema 可配置）。

题面内部有两种提交口径并存（docs/afac-track4.md §0.5 #1，如实记录在案）：

- 5 列：qid,answer,prompt_tokens,completion_tokens,total_tokens
- 8 列：qid,answer_1..answer_4,prompt_tokens,completion_tokens,total_tokens

共同约定：summary 行紧跟表头（题面原文「放在表头之后」），token 为该次
运行全过程真实台账（不接受手填——「估算的 token 进提交文件就是伪造账单」）。
2027 年以官方最新题面为准，不押注任何一种 schema。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HEADER_5 = ["qid", "answer", "prompt_tokens", "completion_tokens", "total_tokens"]
HEADER_8 = [
    "qid", "answer_1", "answer_2", "answer_3", "answer_4",
    "prompt_tokens", "completion_tokens", "total_tokens",
]


def write_answer_csv(run_json: Path, out_csv: Path, schema: str = "5col") -> int:
    """从运行归档生成 answer.csv；返回数据行数（不含表头与 summary）。"""
    if schema not in ("5col", "8col"):
        raise ValueError(f"schema 必须是 5col/8col，得到 {schema}")
    data = json.loads(run_json.read_text(encoding="utf-8"))
    runs = data["runs"]
    if len(runs) != 1:
        raise ValueError(f"运行文件含 {len(runs)} 个 mode，answer.csv 只接受单 mode")
    mode = next(iter(runs))
    details = runs[mode]["details"]
    report = runs[mode]["report"]

    header = HEADER_5 if schema == "5col" else HEADER_8
    summary = ["summary"] + [""] * (len(header) - 4) + [
        str(report["prompt_tokens"]),
        str(report["completion_tokens"]),
        str(report["total_tokens"]),
    ]
    rows = [header, summary]
    for d in details:
        tokens = [
            str(d["prompt_tokens"]), str(d["completion_tokens"]),
            str(d["prompt_tokens"] + d["completion_tokens"]),
        ]
        if schema == "5col":
            rows.append([d["qid"], d["pred"] or "", *tokens])
        else:
            # mcq/multi/tf 的字母组合放 answer_1；多答案字段留给题面
            # 8 列口径下的计算题/抽取题（本脚手架暂无该题型）
            rows.append([d["qid"], d["pred"] or "", "", "", "", *tokens])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)
    return len(details)
