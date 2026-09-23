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
from findata.contextbudget.leakcheck import check_exam_card
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

# 这个脚本被用在不止一种对照上（工具清单长度 / 裁剪策略），所以**变化的轴必须
# 从账单里推导**。顺序即打印顺序。
_AXIS_LABELS: tuple[tuple[str, str], ...] = (
    ("n_tools_in_list", "工具清单长度"),
    ("tools_chars_per_call", "工具定义字符数"),
    ("compact_policy", "裁剪策略"),
    ("compact_caps", "裁剪上限"),
    ("model", "模型"),
    ("n_tasks", "任务数"),
    ("n_silent_tools", "沉默工具数"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="两臂对照（配对差分）")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--arm-a", default="all7b", help="A 臂（默认 all7b）")
    parser.add_argument("--arm-b", default="active7b", help="B 臂（默认 active7b）")
    parser.add_argument("--usd-per-mtok", type=float, default=EXAMPLE_USD_PER_MTOK)
    parser.add_argument(
        "--require-same-corpus",
        action="store_true",
        help="两臂语料指纹缺失或不一致时以非 0 退出（CI / 对外报数时用）",
    )
    parser.add_argument(
        "--require-same-code",
        action="store_true",
        help="两臂代码指纹缺失或不一致时以非 0 退出。**语料冻结保护不了代码** —— "
        "顺序跑批次时改仓库会让两臂跑不同代码，实测发生过。",
    )
    return parser.parse_args()


def describe_axis(bill_a: dict[str, Any], bill_b: dict[str, Any]) -> tuple[list[str], list[str]]:
    """**从两张账单里推导出真正变化的那一轴**。返回 `(变化的轴名, 两臂取值描述)`。

    为什么要推导，而不是写死一句话
    ------------------------------
    这个脚本原先在正文和诚实说明里都硬编码了「唯一变量是工具清单长度」。
    2026-09-22 发现它被用在 R5.2 的**裁剪策略**对照上 —— 那一批两臂的工具数与
    定义字符数**完全相同**（都是 4 个 / 2,524 字符），脚本却还在印
    「长清单一致更贵」。**同一段输出里自己印的证据就否掉了自己的标题。**

    这正是本项目那条铁律的形态：一句打印出来的声明，如果没有任何产物能证伪它，
    它就不是诚实说明，是口号（同 `report_corpus` 的由来）。

    没列出、也没变的东西不印 —— 免得读者以为它变了。
    """
    axes: list[str] = []
    values: list[str] = []
    for key, label in _AXIS_LABELS:
        va, vb = _axis_value(bill_a, key), _axis_value(bill_b, key)
        if va != vb:
            axes.append(label)
            values.append(f"{label} A={va} B={vb}")
    return axes, values


def _axis_value(bill: dict[str, Any], key: str) -> str:
    """取一轴的值。**缺字段一律读作「未记录」**，不许拿 0/空串顶替 —— 老账单
    没有 `compact_policy`，读成 0 会把它误判成「变了」（同 `repeat_run_variance`）。"""
    scope = (bill or {}).get("scope") or {}
    rep = (bill or {}).get("reproducibility") or {}
    if key in rep and rep.get(key) not in (None, ""):
        val = rep[key]
    elif key in scope:
        val = scope[key]
    else:
        return "未记录"
    if isinstance(val, dict):
        return json.dumps(val, sort_keys=True, ensure_ascii=False) if val else "{}"
    if val in (None, ""):
        return "未记录"
    return str(val)


def _arm_facts(bill: dict[str, Any]) -> str:
    """一行说清这一臂是什么配置。**缺的字段印「未记录」，不印 0**。"""
    scope = (bill or {}).get("scope") or {}
    rep = (bill or {}).get("reproducibility") or {}
    n_tools = scope.get("n_tools_in_list", "未记录")
    chars = scope.get("tools_chars_per_call")
    chars_txt = f"{chars:,} 字符/次" if isinstance(chars, int) else "定义未记录"
    policy = _axis_value(bill, "compact_policy")
    silent = scope.get("n_silent_tools")
    silent_txt = f"，沉默 {silent} 个" if isinstance(silent, int) else ""
    code = rep.get("executed_code", {}).get("sha256", "") if isinstance(rep, dict) else ""
    code_txt = f"，代码指纹 {code[:12]}" if code else "，代码指纹未记录"
    return f"{n_tools} 个工具，{chars_txt}，裁剪={policy}{silent_txt}{code_txt}"


def report_exam_card(arm: str, bill: dict[str, Any]) -> bool | None:
    """报这一臂的语料里**答案卡在不在**（`None` = 无法核对）。

    这条必须印在对照旁边，不能只活在另一个脚本里：`-fz` 那一批的语料里
    放着 `src/findata/contextbudget/tasks.py`（同时写着题目与 `must_contain`），
    实测有 5 个任务的答案引用了考题定义、其中 2 个本会被判「命中」——
    而这些命中**偏向短清单臂**，直接把「短清单质量不降反升」这条结论做成了假象。
    """
    root = ((bill or {}).get("reproducibility") or {}).get("corpus_root") or ""
    chk = check_exam_card(root)
    card = chk.has_card
    if card is None:
        print(f"  答案卡核对      : {arm} 无法核对（账单里没记语料根）")
        return None
    if card:
        print(f"  答案卡核对      : {arm} ⚠️ **在** —— {chk.describe()}")
    else:
        print(f"  答案卡核对      : {arm} ✅ {chk.describe()}")
    return card


def report_code(arm_a: str, bill_a: dict[str, Any], arm_b: str, bill_b: dict[str, Any]) -> bool:
    """核对**两臂跑的是不是同一份代码**。与语料核对同构，但查的是另一个轴。

    为什么要单独查这一条（2026-09-22 实测）
    --------------------------------------
    `--corpus-root` 把**语料**冻住了，但**被执行的代码在调用方工作区**。R5.2 的
    `-fp` 批就是这么裂开的（四臂顺序跑 40 分钟，我中途改了仓库）：

        active7b-fx-fp  代码指纹 ef9f3bcab2abf4  625,281 字符
        active7b-ch-fp  代码指纹 ef9f3bcab2abf4  625,281 字符
        active7b-ci-fp  代码指纹 4b313ddbdcafeb  629,723 字符  ← 变了
        active7b-cp-fp  代码指纹 4b313ddbdcafeb  629,723 字符  ← 变了

    于是 `fx ↔ ci`、`fx ↔ cp` 这两组对照**有两个变量**（裁剪策略 + 代码），
    而当时脚本还在印「唯一变量」。这就是 §2.5 那次事故的代码侧版本。
    `--corpus-root` 保护不了它 —— **只有代码指纹能**。
    """
    rep_a = ((bill_a or {}).get("reproducibility") or {})
    rep_b = ((bill_b or {}).get("reproducibility") or {})
    ca = (rep_a.get("executed_code") or {}).get("sha256", "")
    cb = (rep_b.get("executed_code") or {}).get("sha256", "")
    if not ca and not cb:
        print("  代码核对        : 两臂都没记代码指纹（老归档，补丁之前）→ **无法核对**")
        return False
    if not ca or not cb:
        print(f"  代码核对        : 只有一侧有（A={bool(ca)} B={bool(cb)}）→ **无法核对**")
        return False
    print(f"  代码核对        : A={ca[:16]}  B={cb[:16]}")
    if ca == cb:
        print("                    ✅ 两臂同一份代码 —— 代码这一轴**已核对**")
        return True
    print("                    ❌ 两臂**代码不同** —— 差值里混着代码差异，须先消除")
    print("                       （顺序跑批次时改仓库就会这样；加冻结 worktree 或先跑完再改）")
    return False


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
    axes, axis_values = describe_axis(bill_a, bill_b)
    axis_txt = "、".join(axes) if axes else "**没找到变化的轴**（两臂配置字段全同）"
    print(f"两臂对照：{axis_txt} → 代价")
    print("=" * 78)
    print(f"臂 A = {args.arm_a}（{_arm_facts(bill_a)}）")
    print(f"臂 B = {args.arm_b}（{_arm_facts(bill_b)}）")
    for val in axis_values:
        print(f"  · 变化的轴：{val}")
    print(f"可配对任务 = {len(shared)} / A={len(runs_a)}，B={len(runs_b)}")
    corpus_verified = report_corpus(args.arm_a, bill_a, args.arm_b, bill_b)
    code_verified = report_code(args.arm_a, bill_a, args.arm_b, bill_b)
    cards = [report_exam_card(args.arm_a, bill_a), report_exam_card(args.arm_b, bill_b)]
    if args.require_same_corpus and not corpus_verified:
        print("\n--require-same-corpus 已指定且核对未通过 → 退出码 2")
        return 2
    if args.require_same_code and not code_verified:
        print("\n--require-same-code 已指定且核对未通过 → 退出码 2")
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
        print(f"  → 差值方向显著（A 侧一致更贵；A/B 在「{axis_txt}」上的取值见上方）")
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
    print(f"  · 两臂相比，**账单里变化的轴是「{axis_txt}」**。这句由 `describe_axis` 逐字段")
    print(f"    比对得出（比对范围：{'、'.join(lb for _, lb in _AXIS_LABELS)}），不是写死的。")
    if corpus_verified:
        print("  · 语料指纹一致 ⇒ 工具读到的**磁盘内容**相同（**这一轴已核对**）")
    else:
        print("  ⚠️ 语料这一轴**未被核对**：指纹缺失或不一致（见上方「语料核对」）。")
        print("     工具读到的磁盘内容可能不同 —— 引用差值时必须同时说明，或用")
        print("     check_corpus_drift.py 做 trace 级核验并报出漂移规模。")
    if code_verified:
        print("  · 代码指纹一致 ⇒ 两臂**跑的是同一份代码**（**这一轴已核对**）")
    else:
        print("  ⚠️ 代码这一轴**未被核对**：指纹缺失或不一致（见上方「代码核对」）。")
        print("     `--corpus-root` 只冻语料，**冻不住被执行的代码**；顺序跑批次时改仓库")
        print("     就会让两臂跑不同代码（R5.2 的 7B 批实测就这样裂开了）。")
    if True in cards:
        print("  ⚠️ **有臂的语料里带着答案卡**（见上方「答案卡核对」）：")
        print("      `tasks.py` 同时写着 32 道题与每题的判据，模型读到它等于拿到答案卡。")
        print("      实测后果：`-fz` 批里短清单臂 5 个任务引用了考题定义（2 个是假命中），")
        print("      长清单臂 2 个、0 个假命中 ⇒ **这份泄漏是偏向短清单臂的**。")
        print("      ⇒ 质量侧读数**不能直接用**；换成无答案卡的语料重跑，")
        print("        或用 `grade_audit_runs.py` 的「判据泄漏档」剔除后再读。")
    elif None in cards:
        print("  ⚠️ 有臂**无法核对**答案卡（账单里没记语料根）—— 未知不等于没有")
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
