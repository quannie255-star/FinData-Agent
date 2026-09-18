"""合成集评测：归因准确率（本项目第一个**有 ground truth** 的归因评测）。

以前所有归因指标都靠人肉复盘——没有 ground truth，准确率无从谈起。
合成样本的根因是写进去的，所以能反过来问：归因器判对了吗。

跑三件事：
  1. **证伪检查**——每类注入的失败必须留下可观测信号，否则这条合成无效
     （合成数据最危险的失败就是"看起来一堆样本，其实什么都没注进去"）；
  2. **归因准确率**——判对的类别 / 总数，带混淆矩阵（错在哪两类之间
     比一个总数有用得多）；
  3. **处置分布**——能不能当训练样本，四档各多少。

    uv run python scripts/run_synth_eval.py
    uv run python scripts/run_synth_eval.py --per-class 30 --strict
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from findata.agentops.probes import run_all
from findata.agentops.schema import iter_traces, write_traces
from findata.agentops.synth import CLASSES, generate
from findata.agentops.triage import (
    CAT_ENV_TIMEOUT,
    CAT_HALLUCINATED,
    CAT_LUCKY_GUESS,
    CAT_OK,
    CAT_PARAM_ERROR,
    CAT_RETRY_LOOP,
    CAT_TOOL_MISSING,
    diagnose,
    verdict,
)

OUT_JSONL = Path("examples/synth-traces.jsonl")
OUT_TXT = Path("examples/synth-eval-report.txt")

# 每类必须留下的可观测信号，否则合成无效。
# 这一栏是**合成器的自检**，不是归因器的一部分。
_EXPECTED_SIGNAL = {
    CAT_OK: lambda p: p["abort"]["aborted"] is False and p["degrade"]["n"] == 0,
    CAT_PARAM_ERROR: lambda p: True,  # 靠下面的 error 步骤检查
    CAT_TOOL_MISSING: lambda p: True,
    CAT_ENV_TIMEOUT: lambda p: True,
    CAT_RETRY_LOOP: lambda p: bool(p["retry_storm"]["storm"]),
    CAT_HALLUCINATED: lambda p: p["ungrounded_slot"]["n"] > 0,
    CAT_LUCKY_GUESS: lambda p: p["degrade"]["n"] > 0,
}


def _has_error_step(t) -> bool:
    return any(s.status == "error" for s in t.steps)


def main() -> None:
    ap = argparse.ArgumentParser(description="合成集归因评测")
    ap.add_argument("--per-class", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--min-accuracy", type=float, default=0.90,
                    help="归因准确率门禁（ROADMAP R4.1 定的是 0.90）")
    ap.add_argument(
        "--mode", default="", choices=["", "paraphrase", "heldout"],
        help="dev 改写集 / 从不调参的 held-out 集（不填 = canonical，数字不算数）",
    )
    ap.add_argument("-o", "--out", default=str(OUT_TXT))
    ap.add_argument("--strict", action="store_true", help="不达标以非零码退出")
    args = ap.parse_args()

    n = write_traces(OUT_JSONL, generate(args.per_class, args.seed, args.mode))
    traces = list(iter_traces(OUT_JSONL))

    _label = {
        "": "常规集（canonical 措辞，循环论证，数字不算数）",
        "paraphrase": "dev 改写集（已用于调参，不能当泛化成绩）",
        "heldout": "held-out 集（从不参与调参，这才是泛化能力）",
    }
    mode = _label[args.mode]
    lines = [
        f"合成集归因评测 · {mode}",
        f"seed={args.seed}，每类 {args.per_class} 条",
        f"轨迹 {n} 条（读回 {len(traces)}）",
        "这是本项目第一个有 ground truth 的归因评测——根因是写进去的，"
        "所以「判对没判对」可以数出来。",
        "",
        "一、证伪检查：注入的失败有没有留下可观测信号",
    ]

    ok_signal = True
    per_class = {c: [t for t in traces if t.golden.get("root_cause") == c] for c in CLASSES}
    for c in CLASSES:
        bad = []
        for t in per_class[c]:
            p = run_all(t)
            if not _EXPECTED_SIGNAL[c](p):
                bad.append(t.run_id)
            if c in (CAT_PARAM_ERROR, CAT_TOOL_MISSING, CAT_ENV_TIMEOUT) and not _has_error_step(t):
                bad.append(t.run_id)
        if bad:
            ok_signal = False
        lines.append(f"  {c:<14}{len(per_class[c]):>4} 条   "
                     f"{'✓ 信号可观测' if not bad else f'✗ {len(bad)} 条没注进去'}")

    # 二、归因准确率
    hit = Counter()
    conf: dict[str, Counter] = {}
    verdicts: Counter = Counter()
    for t in traces:
        d = diagnose(t)
        truth = t.golden.get("root_cause")
        got = d.category
        hit[got == truth] += 1
        conf.setdefault(truth, Counter())[got] += 1
        verdicts[verdict(d)] += 1

    acc = hit[True] / len(traces)
    if args.mode == "":
        lines.append("")
        lines.append("  ⚠️ 常规集的 100% 是**循环论证**：规则是我写的，样本也是照着")
        lines.append("     规则造的，拿规则的原话去考规则必然满分。这个数字不能对外引用，")
        lines.append("     真正的泛化能力看 `--paraphrase` 改写集。")
    lines += [
        "",
        "二、归因准确率（判对类别 / 总数）",
        f"  {hit[True]}/{len(traces)}  {acc:6.1%}     门禁 ≥ {args.min_accuracy:.0%}",
        "",
        "  混淆矩阵（行=真实根因，列=判成什么）",
        f"  {'':<13}" + "".join(f"{c[:7]:<9}" for c in CLASSES),
    ]
    short = {c: c[:11] for c in CLASSES}
    for truth in CLASSES:
        row = conf.get(truth, Counter())
        cells = "".join(
            (f"{row[c]:>3}" if row.get(c) else "  ·") + " " * 6 for c in CLASSES
        )
        lines.append(f"  {short[truth]:<13}{cells}")

    lines += ["", "三、处置分布（能不能当训练样本）"]
    for v, k in verdicts.most_common():
        lines.append(f"  {v:<14}{k:>4}  {k / len(traces):6.1%}")

    # 错判样例：归类错比一个总数有用
    lines += ["", "四、判错的样本（前 10 条）"]
    shown = 0
    for t in traces:
        d = diagnose(t)
        if d.category != t.golden.get("root_cause"):
            lines.append(f"  {t.run_id} 真实={t.golden.get('root_cause')} 判成={d.category}")
            lines.append(f"      {d.evidence[0] if d.evidence else d.suggestion}")
            shown += 1
            if shown >= 10:
                break
    if not shown:
        lines.append("  （无）")

    passed = ok_signal and acc >= args.min_accuracy
    lines += [
        "",
        f"门禁：合成信号可观测 且 归因准确率 ≥ {args.min_accuracy:.0%} → "
        f"{'通过' if passed else '未通过'}",
    ]

    text = "\n".join(lines)
    print(text)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(f"\n已归档 → {args.out} / {OUT_JSONL}")
    if args.strict and not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
