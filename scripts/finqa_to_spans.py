"""把 finqa 运行归档转换成 context-economist 的双层 trace 格式。

用途：让 v4.0 的 context-economist 反过来审计 finqa 主线自己的 token 成本——
旧工具审新系统。输出 economist 目录约定的一对文件：

- ``runs-<tag>.jsonl``（增强层）：每题一条 run（逐轮 token、最终答案），
  报告的逐任务读数从这里来；
- ``spans-<tag>.jsonl``（标准层）：每题一条 `gen_ai.chat` span（OTel GenAI
  语义），保证「任何 agent 按 convention 打点就能被分析」的通用路径可用。

诚实边界：finqa 归档只有逐题汇总（无逐轮上下文字符、无工具返回原文），
messages_chars/tools_chars 以 0 写入——economist 报告会据此声明
「换算基准未观测」「字段级归因未观测」，这是被转换数据的真实边界，不是缺陷。

用法：
    uv run python scripts/finqa_to_spans.py examples/afac-v4-fix-isolate-k4.json \
        --out-dir examples/finqa-cost-audit --tag finqa-k4
    findata-context-economist --trace examples/finqa-cost-audit --tag finqa-k4 --grader none
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def run_to_trace(run_data: dict) -> tuple[list[dict], list[dict]]:
    """运行归档 → (runs 记录, spans 记录)。每题各一条。"""
    runs = run_data.get("runs")
    if not isinstance(runs, dict) or not runs:
        raise ValueError("运行归档必须含 runs 字典（单 mode 文件）")
    run_records: list[dict] = []
    span_records: list[dict] = []
    model = str(run_data.get("model", ""))
    for mode, run in runs.items():
        for detail in run.get("details", []):
            qid = str(detail.get("qid", ""))
            prompt = int(detail.get("prompt_tokens", 0))
            completion = int(detail.get("completion_tokens", 0))
            run_records.append(
                {
                    "task": qid,
                    "final_answer": str(detail.get("pred", "")),
                    "stopped_reason": "done",
                    "error": "",
                    "turns": [
                        {
                            "turn": 1,
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "messages_chars": 0,  # 归档未记录，报告将如实声明未观测
                            "tools_chars": 0,
                            "duration_ms": 0.0,
                            "tool_calls": [],
                        }
                    ],
                }
            )
            span_records.append(
                {
                    "name": "gen_ai.chat",
                    "task": qid,
                    "attributes": {
                        "gen_ai.request.model": model,
                        "gen_ai.usage.input_tokens": prompt,
                        "gen_ai.usage.output_tokens": completion,
                        "findata.context.turn": 1,
                        "findata.finqa.mode": mode,
                        "findata.finqa.correct": bool(detail.get("correct")),
                    },
                }
            )
    return run_records, span_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--tag", required=True, help="输出文件 tag（runs-<tag>/spans-<tag>）")
    args = parser.parse_args()
    data = json.loads(Path(args.run_json).read_text(encoding="utf-8"))
    run_records, span_records = run_to_trace(data)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, records in (("runs", run_records), ("spans", span_records)):
        path = out_dir / f"{kind}-{args.tag}.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        print(f"{path}：{len(records)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
