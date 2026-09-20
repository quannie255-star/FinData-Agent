"""两臂对照：全量工具清单 vs 只给 4 个活跃工具。

**回答的问题**：工具清单里那些从不被调用的定义，代价是多少？

**方法**：配对差分。同样 32 个任务、同一个模型、同一份 prompt，唯一变量是
工具清单的长度。于是"同一任务在两臂的 `prompt_tokens` 之差"就是工具膨胀的
代价——**实测出来的差，不是估算出来的比例**。

**为什么必须配对**：任务难度差异极大（清单类任务 2 轮就完，跨文件任务可能
8 轮）。如果只比两臂的总量均值，任务构成一变结论就翻。配对把任务差异消掉，
剩下的差值只能归因于工具清单。

**显著性**：用符号检验（sign test），纯 Python 可算，不依赖 scipy。
它只问"active 臂更省的任务占多少"，不假设差值服从正态——token 差值明显不
正态（长尾），用 t 检验会高估显著性。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from findata.contextbudget.corpus import manifests_agree
from findata.contextbudget.stats import (
    load_runs,
    mcnemar_exact_p,
    prompt_tokens,
    sign_test_p,
    tool_calls,
)

__all__ = ["load_runs", "main", "prompt_tokens", "sign_test_p", "tool_calls"]

DEFAULT_DIR = Path("examples/context-audit")

# 示例单价（美元 / 百万 input token）。**必须按实际供应商与日期核实后再引用**，
# 这里只用来给一个量级感，不构成报价。
EXAMPLE_USD_PER_MTOK = 2.50


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="两臂对照（配对差分）")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--arm-a", default="all7b", help="长清单臂（默认 all7b）")
    parser.add_argument("--arm-b", default="active7b", help="短清单臂（默认 active7b）")
    parser.add_argument("--usd-per-mtok", type=float, default=EXAMPLE_USD_PER_MTOK)
    parser.add_argument(
        "--require-same-corpus",
        action="store_true",
        help="两臂语料指纹缺失或不一致时以非 0 退出（CI / 对外报数时用）",
    )
    return parser.parse_args()


def report_corpus(arm_a: str, bill_a: dict[str, Any], arm_b: str, bill_b: dict[str, Any]) -> bool:
    """核对「唯一变量」这句话。返回**这句话是否已被验证**。

    ⚠️ 这个函数的存在本身是个教训：在这之前，脚本把
    「唯一变量是工具清单长度」**硬编码**在诚实说明里 —— 而 2026-09-20 实测发现
    那一批两臂的语料其实不同（我边跑边提交，4 处调用读到了不同目录状态）。
    一句打印出来的声明，如果没有任何产物能证伪它，那它就不是诚实说明，是口号。
    """
    rep_a = (bill_a or {}).get("reproducibility") or {}
    rep_b = (bill_b or {}).get("reproducibility") or {}
    if not rep_a and not rep_b:
        print("  语料核对        : 两臂**都没有**指纹（老归档，记录于补丁之前）")
        print("                    → 「唯一变量」这句话**无法核对**；")
        print("                      用 scripts/check_corpus_drift.py 做 trace 级交叉核验")
        return False
    if not rep_a or not rep_b:
        print(f"  语料核对        : 只有一侧有指纹（A={bool(rep_a)} B={bool(rep_b)}）→ 无法核对")
        return False

    agreed = manifests_agree([rep_a.get("corpus", {}), rep_b.get("corpus", {})])
    print(
        f"  语料核对        : A={rep_a.get('corpus', {}).get('sha256', '?')[:16]} "
        f"({rep_a.get('corpus', {}).get('n_files', '?')} 文件)  "
        f"B={rep_b.get('corpus', {}).get('sha256', '?')[:16]} "
        f"({rep_b.get('corpus', {}).get('n_files', '?')} 文件)"
    )
    if agreed:
        print("                    ✅ 两臂同一语料 —— 「唯一变量」这句话**已核对**")
        return True
    if agreed is False:
        print("                    ❌ 两臂语料**不同** —— 差值里混着语料差异，须先消除")
        return False
    print("                    ⚠️ 指纹不完整，无法判断")
    return False


def main() -> int:  # noqa: C901 — 一段顺读的报告，不拆
    args = parse_args()
    base = Path(args.dir)

    runs_a = load_runs(base / f"runs-{args.arm_a}.jsonl")
    runs_b = load_runs(base / f"runs-{args.arm_b}.jsonl")
    shared = sorted(set(runs_a) & set(runs_b))

    if not shared:
        print("没有可配对的任务 —— 检查两臂是否跑了同一批任务")
        return 1

    bill_a = json.loads((base / f"bill-{args.arm_a}.json").read_text(encoding="utf-8"))
    bill_b = json.loads((base / f"bill-{args.arm_b}.json").read_text(encoding="utf-8"))

    print("=" * 78)
    print("两臂对照：工具清单长度 → 代价")
    print("=" * 78)
    print(
        f"臂 A = {args.arm_a}（{bill_a['scope']['n_tools_in_list']} 个工具，"
        f"定义 {bill_a['scope']['tools_chars_per_call']:,} 字符/次）"
    )
    print(
        f"臂 B = {args.arm_b}（{bill_b['scope']['n_tools_in_list']} 个工具，"
        f"定义 {bill_b['scope']['tools_chars_per_call']:,} 字符/次）"
    )
    print(f"可配对任务 = {len(shared)} / A={len(runs_a)}，B={len(runs_b)}")
    corpus_verified = report_corpus(args.arm_a, bill_a, args.arm_b, bill_b)
    if args.require_same_corpus and not corpus_verified:
        print("\n--require-same-corpus 已指定且核对未通过 → 退出码 2")
        return 2

    # --- 配对差分 ---
    diffs: list[int] = []
    rows: list[tuple[str, int, int, int]] = []
    for task in shared:
        ta, tb = prompt_tokens(runs_a[task]), prompt_tokens(runs_b[task])
        diffs.append(ta - tb)
        rows.append((task, ta, tb, ta - tb))

    n_pos = sum(1 for d in diffs if d > 0)  # A 更贵
    n_neg = sum(1 for d in diffs if d < 0)
    n_zero = sum(1 for d in diffs if d == 0)
    p_value = sign_test_p(n_pos, n_neg)

    print("\n--- 配对差分（A − B，单位：prompt_tokens）---")
    print(f"  正差（A 更贵）: {n_pos}   负差（B 更贵）: {n_neg}   相等: {n_zero}")
    print(f"  均值   : {statistics.mean(diffs):+,.1f}")
    print(f"  中位数 : {statistics.median(diffs):+,.1f}")
    if len(diffs) > 1:
        print(f"  标准差 : {statistics.stdev(diffs):,.1f}")
    print(f"  最小/最大: {min(diffs):+,} / {max(diffs):+,}")
    print(f"  符号检验双侧 p = {p_value:.4g}")
    if p_value < 0.05:
        print("  → 差值方向显著（同一任务上，长清单一致更贵）")
    else:
        print("  → 差值方向不显著，不能据此下结论")

    total_a = sum(prompt_tokens(r) for r in runs_a.values())
    total_b = sum(prompt_tokens(r) for r in runs_b.values())
    if total_a:
        drop = 1 - total_b / total_a
        print(f"\n  全量 prompt_tokens：A={total_a:,}  B={total_b:,}  降幅={drop:.1%}")

    saved = total_a - total_b
    if saved > 0:
        print(
            f"  [示例折算] 若按 ${args.usd_per_mtok:.2f}/M input token 计，"
            f"这批任务省 ${saved / 1_000_000 * args.usd_per_mtok:.4f}"
            f" —— 单价须按实际供应商核实，此处仅给量级"
        )

    # --- 质量是否受损（这是全部的关键：省了钱有没有付出代价）---
    n_tasks_a = bill_a["scope"]["n_tasks"]
    n_tasks_b = bill_b["scope"]["n_tasks"]
    hits_a = bill_a["measured"]["keyword_hits"]
    hits_b = bill_b["measured"]["keyword_hits"]
    calls_a = bill_a["measured"]["total_tool_calls"]
    calls_b = bill_b["measured"]["total_tool_calls"]
    ans_a = bill_a["measured"]["n_answered"]
    ans_b = bill_b["measured"]["n_answered"]
    err_a = bill_a["measured"]["n_errored"]
    err_b = bill_b["measured"]["n_errored"]

    print("\n--- 质量侧对照（关键词命中是弱判据）---")
    print(f"  关键词命中 : A={hits_a}/{n_tasks_a}  B={hits_b}/{n_tasks_b}  差={hits_b - hits_a:+d}")
    print(f"  工具调用数 : A={calls_a}      B={calls_b}      差={calls_b - calls_a:+d}")
    print(f"  完成数     : A={ans_a}/{n_tasks_a}      B={ans_b}/{n_tasks_b}")
    # 错误数必须一起报：超时/异常是环境噪声，不是模型能力。
    # 只报对自己有利的那一臂等于筛选证据。
    print(f"  错误数     : A={err_a}      B={err_b}      （超时等环境失败，须同口径处理）")

    # 两臂任务集必须一致，否则"配对"是假的
    if n_tasks_a != n_tasks_b:
        print(f"  ⚠️ 两臂任务数不同（{n_tasks_a} vs {n_tasks_b}）—— 配对的效力下降，须在报告里说明")

    # McNemar 式配对：只在"两臂结论不同"的任务上比较
    both = only_a = only_b = neither = 0
    for task in shared:
        ha = _hit(runs_a[task], bill_a)
        hb = _hit(runs_b[task], bill_b)
        if ha and hb:
            both += 1
        elif ha and not hb:
            only_a += 1
        elif hb and not ha:
            only_b += 1
        else:
            neither += 1
    print(f"  配对明细   : 都命中={both}  仅A={only_a}  仅B={only_b}  都不中={neither}")
    if only_a + only_b:
        print(f"  McNemar 精确检验 p = {mcnemar_exact_p(only_a, only_b):.4g}")
    else:
        print("  McNemar：两臂结论没有分歧，无法检验（这是'不显著'的一种，不是'等效'）")

    print("\n--- 逐任务（按 A 的 token 降序）---")
    print(f"  {'任务':<34} {'A':>8} {'B':>8} {'差':>8}")
    for task, ta, tb, diff in sorted(rows, key=lambda r: -r[1])[:12]:
        label = task if len(task) <= 32 else task[:31] + "…"
        print(f"  {label:<34} {ta:>8,} {tb:>8,} {diff:>+8,}")

    print("\n--- 诚实说明 ---")
    if corpus_verified:
        print("  · 唯一变量是工具清单长度（**已用两臂语料指纹核对**）；模型、任务、")
        print("    prompt、max_turns 两臂一致")
    else:
        print("  ⚠️ 唯一变量**未被核对**：语料指纹缺失或不一致（见上方「语料核对」）。")
        print("     模型、任务、prompt、max_turns 两臂一致，但**工具读到的磁盘内容**")
        print("     可能不同 —— 引用差值时必须同时说明这一点，或用 check_corpus_drift.py")
        print("     做 trace 级核验并报出漂移规模。")
    print("  · 差值是实测，不是估算；token 数全部来自 provider usage")
    print("  · 符号检验不假设正态；token 差值长尾，t 检验会高估显著性")
    print("  · 关键词命中是弱判据（脚本里已注明局限），不能当准确率引用")
    print("  · 本机 7B 本地模型口径：token 侧结论可迁移（token 由上下文结构决定），")
    print("    质量侧结论只在本地模型上成立")
    print("  · 沉默工具是构造的（见 schemas.py），比例只能算实验条件下的量")
    return 0


def _hit(run: dict[str, Any], bill: dict[str, Any]) -> bool:
    """从落盘的 runs 里重建命中判定。

    bill 里只有聚合值，逐任务命中标记未落盘；这里用与 runner 相同的关键词
    判据重算，保证两处口径一致（tasks.is_hit 是唯一实现）。
    """
    from findata.contextbudget.tasks import TASKS, is_hit

    for task in TASKS:
        if task.question == run["task"]:
            return is_hit(task, run.get("final_answer", ""))
    return False


if __name__ == "__main__":
    sys.exit(main())
