"""2×2 对照：工具清单长度 × 模型规模，**重点是交互项**。

**为什么需要单独一个脚本**：单看"每个模型各自省了多少"回答不了 R5.2 的问题。
R5.2 问的是 —— **工具膨胀的代价是否随模型变小而变大？** 这是**交互**，
必须在同一个任务上比较"两个模型的代价差"：

```
Δ7B = ptok(all7b) − ptok(active7b)      每个任务的"膨胀代价"
Δ3B = ptok(all3b) − ptok(active3b)
交互项 = Δ7B − Δ3B
```

**为什么用符号检验而不是 t 检验**：同 `compare_audit_arms.py` 的理由 ——
token 差值长尾，t 检验会高估显著性。

**报这个脚本的输出时必须带的三句话**（打印在末尾，别只抄表）：
  ① 交互不显著 ≠ 交互为零；② 两个模型各自的读数是否显著要分开看；
  ③ 四臂是否同一语料，由账单指纹核对，不由本脚本断言。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from findata.contextbudget.corpus import manifests_agree
from findata.contextbudget.stats import load_runs, prompt_tokens, sign_test_p, tool_calls

DEFAULT_DIR = Path("examples/context-audit")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2×2 对照 + 交互项")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--big-all", default="all7b", help="大模型 × 长清单")
    parser.add_argument("--big-active", default="active7b", help="大模型 × 短清单")
    parser.add_argument("--small-all", default="all3b", help="小模型 × 长清单")
    parser.add_argument("--small-active", default="active3b", help="小模型 × 短清单")
    parser.add_argument(
        "--require-same-corpus",
        action="store_true",
        help="四个臂语料指纹缺失或不一致时以非 0 退出",
    )
    return parser.parse_args()


def _load_bill(base: Path, tag: str) -> dict[str, Any]:
    return json.loads((base / f"bill-{tag}.json").read_text(encoding="utf-8"))


def _pair_facts(
    runs_all: dict[str, dict[str, Any]], runs_active: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """一对臂（同一模型）的配对事实。"""
    shared = sorted(set(runs_all) & set(runs_active))
    diffs = {t: prompt_tokens(runs_all[t]) - prompt_tokens(runs_active[t]) for t in shared}
    n_pos = sum(1 for d in diffs.values() if d > 0)
    n_neg = sum(1 for d in diffs.values() if d < 0)
    total_all = sum(prompt_tokens(r) for r in runs_all.values())
    total_active = sum(prompt_tokens(r) for r in runs_active.values())
    calls_all = sum(tool_calls(r) for r in runs_all.values())
    calls_active = sum(tool_calls(r) for r in runs_active.values())
    return {
        "n_paired": len(shared),
        "diffs": diffs,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "p": sign_test_p(n_pos, n_neg),
        "total_all": total_all,
        "total_active": total_active,
        "drop": 1 - total_active / total_all if total_all else 0.0,
        "calls_all": calls_all,
        "calls_active": calls_active,
    }


def report_corpus(all_bills: dict[str, dict[str, Any]]) -> bool:
    manifests = [(b.get("reproducibility") or {}).get("corpus", {}) for b in all_bills.values()]
    agree = manifests_agree(manifests)
    shown = {tag: (b.get("reproducibility") or {}).get("corpus", {}).get("sha256", "?")[:16]
             for tag, b in all_bills.items()}
    print("  四臂语料指纹  : " + "  ".join(f"{t}={s}" for t, s in shown.items()))
    if agree:
        print("                  ✅ 四臂同一语料 —— 「唯一变量」已核对")
        return True
    if agree is False:
        print("                  ❌ 四臂语料**不一致** —— 交互项里混着语料差异")
        return False
    print("                  ⚠️ 指纹不完整（老归档）→ **无法核对**，须用 check_corpus_drift.py")
    return False


def main() -> int:
    args = parse_args()
    base = Path(args.dir)
    tags = {
        "big_all": args.big_all,
        "big_active": args.big_active,
        "small_all": args.small_all,
        "small_active": args.small_active,
    }

    runs = {k: load_runs(base / f"runs-{t}.jsonl") for k, t in tags.items()}
    bills = {k: _load_bill(base, t) for k, t in tags.items()}

    print("=" * 78)
    print("2×2 对照：工具清单长度 × 模型规模")
    print("=" * 78)
    corpus_ok = report_corpus(bills)
    if args.require_same_corpus and not corpus_ok:
        print("\n--require-same-corpus 已指定且核对未通过 → 退出码 2")
        return 2

    big = _pair_facts(runs["big_all"], runs["big_active"])
    small = _pair_facts(runs["small_all"], runs["small_active"])

    print(f"\n--- 每一对各自的读数（{tags['big_all']} vs {tags['big_active']} 等）---")
    for label, facts in (("大模型", big), ("小模型", small)):
        print(
            f"  {label}：{facts['total_all']:,} → {facts['total_active']:,}  "
            f"降 {facts['drop']:.1%}  正差 {facts['n_pos']} / 负差 {facts['n_neg']}  "
            f"符号检验 p = {facts['p']:.4g}"
        )
        print(
            f"          工具调用 {facts['calls_all']} → {facts['calls_active']}  "
            f"（{facts['calls_active'] - facts['calls_all']:+d}）"
        )

    # --- 交互项 ---
    shared = sorted(set(big["diffs"]) & set(small["diffs"]))
    if not shared:
        print("\n没有可配对的任务 —— 检查四个臂是否跑了同一批任务")
        return 1

    inter = {t: big["diffs"][t] - small["diffs"][t] for t in shared}
    n_pos = sum(1 for v in inter.values() if v > 0)  # 大模型的膨胀代价更大
    n_neg = sum(1 for v in inter.values() if v < 0)
    n_zero = len(shared) - n_pos - n_neg
    p_value = sign_test_p(n_pos, n_neg)

    print(f"\n--- 交互项（Δ大模型 − Δ小模型，单位 prompt_tokens，配对 {len(shared)} 个任务）---")
    print(f"  正（大模型的膨胀代价更大）: {n_pos}   负: {n_neg}   相等: {n_zero}")
    print(f"  均值 : {statistics.mean(inter.values()):+,.1f}")
    print(f"  中位数: {statistics.median(inter.values()):+,.1f}")
    print(f"  符号检验双侧 p = {p_value:.4g}")
    if p_value < 0.05:
        print("  → 交互显著：工具膨胀的 token 代价**随模型规模变化**")
    else:
        print("  → **交互不显著**：没有证据说「小模型受工具膨胀的代价更大/更小」")
        print("     （不显著 ≠ 交互为零，只说本批样本撑不起这个结论）")

    print("\n  极端 6 例（按 |交互| 降序）：")
    print(f"    {'任务':<30} {'Δ大模型':>10} {'Δ小模型':>10} {'交互':>10}")
    for task, value in sorted(inter.items(), key=lambda kv: -abs(kv[1]))[:6]:
        label = task if len(task) <= 28 else task[:27] + "…"
        print(
            f"    {label:<30} {big['diffs'][task]:>+10,} {small['diffs'][task]:>+10,} "
            f"{value:>+10,}"
        )

    print("\n--- 口径与限制 ---")
    print("  · 交互项是**同任务内**的差值再相减，任务难度被消掉两次")
    print("  · delta 本身也是**配对实测**（每臂都是同一批任务），不是估算")
    print("  · 符号检验不假设正态；token 差值长尾，t 检验会高估显著性")
    print("  · 质量侧（命中率变化）**不在本脚本里** —— 用 grade_audit_runs.py，")
    print("    且它的判据是关键词（弱），引用时必须写「关键词命中」")
    print("  · 质量侧只在本地模型口径下成立；token 侧结论可迁移（token 由上下文结构决定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
