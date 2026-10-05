"""AFAC 赛题四 M1/M2：脚手架题集上的 baseline 与检索臂作答。

付费 API 不进 CI（仓库铁律）——本脚本只在配好 FINDATA_DASHSCOPE_API_KEY
的机器上手动跑，产物归档 examples/。口径声明必须随报告走：
脚手架题集的数字不是官方评测成绩，不得对外这么声称。

--mode full     全文直入（朴素臂，官方 baseline 复刻；B 榜题跳过）
--mode retrieve BM25 条款级检索后片段作答（检索臂；A 榜题限定文档内检索，B 榜盲检）
--mode both     两臂各跑一遍，报告附对照段（只陈述数字，不主张显著性——M5 才做）
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from findata.finqa import fixture as fx
from findata.finqa.baseline import run_baseline
from findata.finqa.qwen import DecodingParams, QwenClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODES = ("full", "retrieve", "both")


def _print_run(title: str, details, report, skipped) -> None:
    print(f"\n===== {title} =====")
    for d in details:
        mark = "✓" if d["correct"] else "✗"
        print(
            f"{mark} {d['qid']} [{d['answer_format']}] gold={d['gold']} "
            f"pred={d['pred']} tokens={d['prompt_tokens']}+{d['completion_tokens']}"
        )
    if skipped:
        print(f"（跳过 B 榜题 {len(skipped)} 道：{'/'.join(skipped)}）")
    for line in report.summary_lines():
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="eval/fixtures/afac_scaffold")
    parser.add_argument("--model", default=None, help="默认取 settings.dashscope_model")
    parser.add_argument("--mode", default="full", choices=MODES)
    parser.add_argument("--k", type=int, default=6, help="检索臂 top-k 片段数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--max-doc-chars", type=int, default=0, help="文档截断上限（0=不截断）")
    parser.add_argument("--out", default=None, help="报告输出路径（txt）；同目录写同名 .json")
    args = parser.parse_args()

    root = PROJECT_ROOT / args.fixture
    questions = fx.load_questions(root)
    docs = fx.load_docs(root, max_chars=args.max_doc_chars)
    fx.check_docs(questions, docs)

    client = QwenClient(model=args.model, params=DecodingParams())
    run_modes = ("full", "retrieve") if args.mode == "both" else (args.mode,)

    runs: dict[str, dict] = {}
    for mode in run_modes:
        details, ledger, report, skipped = run_baseline(
            questions, docs, client, limit=args.limit, mode=mode, k=args.k
        )
        _print_run(f"mode={mode}", details, report, skipped)
        runs[mode] = {"details": details, "report": report, "skipped": skipped}

    if "full" in runs and "retrieve" in runs:
        f, r = runs["full"]["report"], runs["retrieve"]["report"]
        print("\n===== 两臂对照（同题不同上下文；只陈述，不主张显著性） =====")
        common = {d["qid"] for d in runs["full"]["details"]} & {
            d["qid"] for d in runs["retrieve"]["details"]
        }
        print(f"共同题目 {len(common)} 道")
        print(f"full:     acc {f.accuracy:.2%}，tokens {f.total_tokens:,}")
        print(f"retrieve: acc {r.accuracy:.2%}，tokens {r.total_tokens:,}")
        delta = f.total_tokens - r.total_tokens
        print(f"检索臂 token 差异：{delta:+,}（{'省' if delta > 0 else '多'}了 {abs(delta):,}）")

    if args.out:
        params = client.params
        header = [
            "# AFAC 赛题四 M1/M2 报告（脚手架口径）",
            "",
            f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
            f"模型：{client.model}（temperature={params.temperature}, "
            f"top_p={params.top_p}, seed={params.seed}, max_tokens={params.max_tokens}）",
            f"题集：{args.fixture}（{len(questions)} 题，自建脚手架，非官方评测集，"
            "数字不得对外声称为评测成绩）",
            f"模式：{args.mode}（k={args.k}）；文档截断：{args.max_doc_chars or '无'}",
            "",
        ]
        lines = list(header)
        for mode, run in runs.items():
            lines.append(f"## mode={mode}")
            lines.append("")
            for d in run["details"]:
                mark = "✓" if d["correct"] else "✗"
                lines.append(
                    f"- {mark} {d['qid']} [{d['answer_format']}] gold={d['gold']} "
                    f"pred={d['pred']} | {d['prompt_tokens']}+{d['completion_tokens']}"
                )
            if run["skipped"]:
                lines.append(f"- （跳过 B 榜题：{'/'.join(run['skipped'])}）")
            lines.append("")
            lines.extend(run["report"].summary_lines())
            lines.append("")
        out_path = PROJECT_ROOT / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        json_path = out_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "model": client.model,
                    "params": vars(params),
                    "fixture": args.fixture,
                    "mode": args.mode,
                    "k": args.k,
                    "runs": {
                        mode: {
                            "report": {
                                "total": run["report"].total,
                                "correct": run["report"].correct,
                                "accuracy": run["report"].accuracy,
                                "prompt_tokens": run["report"].prompt_tokens,
                                "completion_tokens": run["report"].completion_tokens,
                                "total_tokens": run["report"].total_tokens,
                                "token_score": run["report"].tscore,
                                "final_score": run["report"].final,
                            },
                            "skipped": run["skipped"],
                            "details": run["details"],
                        }
                        for mode, run in runs.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写：{out_path}（json：{json_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
