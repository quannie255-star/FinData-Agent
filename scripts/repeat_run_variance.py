"""同配置两次运行的差异 = **噪声下界**。

    uv run python scripts/repeat_run_variance.py \
        --a active7b-fx --b active7b-fx-fp \
        --out examples/context-audit/noise-floor-7b-fx.json

**为什么要单独一个脚本**（2026-09-22，`docs/r5.0-acceptance.md` §3.11）：

这套 harness **没有固定解码**（没设 seed、没把 temperature 压到 0）。后果是：
"两臂有差异"这个观测里，**混着一部分"同一臂再跑一次也会有的差异"**。
在本阶段之前，**那个量从来没被测过**——于是"配对检验显著"只覆盖了任务难度这一层方差，
覆盖不了整轮轨迹的随机性。

自己踩的坑（写进这里免得再犯）：

1. **标签会说谎，`scope.model` 不会。** 我第一版拿一批文件名写着 `7b` 的产物去算这个差，
   差出来 28%，差点宣布"噪声比效应还大"。核对账单才发现**那批其实是 3B**
   （跑的时候漏了 `--model`）。所以本脚本第一步就是核对 setup。
2. **"没记录"不是"不一致"**，也不是"一致"。老批次的账单里没有 `compact_policy` 这个键，
   天真的 `a[k] != b[k]` 会把"老账单没这个字段"判成"配置不一致"，
   于是**唯一可用的历史配对被自己的闸门挡掉**。现在三分类，第三类单列、默认不放行。
3. **`AgentRun` 是 dataclass，落盘的 runs 是 dict。** 第一版照 `AgentRun` 的写法
   用属性访问（`run.total_prompt_tokens`），而那是 **property**，
   脚本一跑就 `AttributeError`。**写出来但没跑过的代码，和没写过没有区别**——
   这也是本文件要有测试的原因。

**三个读数怎么用**：

1. **噪声下界（相对值）**：同配置两次运行，每任务 prompt token 的差异中位数。
   任何**跨臂**差值小于它的，都只能写「在噪声内」，不能写"没测到伤害"。
2. **方向偏移**：如果 B 在绝大多数任务上都比 A 大（或小），那说明有一个**系统性**
   偏移源（例如某个服务状态、缓存、并发），不只是抖动——这一条比数值本身更重要。
3. **质量侧翻转数**：同配置两次运行**自己就会翻掉几个任务**的命中档。
   这个数就是"质量侧结论的分辨率下限"：小于它的档位变化，**任何方向都不该被解释**。

**本脚本不做的事**（刻意）：不合成单一"方差"数字、不做显著性检验。
n=32 的两个样本之间的差异检验，结论只会是"测不出"，那没有信息量。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from findata.contextbudget.grading import mcnemar_exact
from grade_audit_runs import grade_arm, load_runs

DEFAULT_DIR = Path("examples/context-audit")
TIERS = (
    ("hit（弱·剔泄漏）", "hit_clean"),
    ("verified_hit（弱+过程）", "verified_hit"),
    ("exact_hit（精确值，为准）", "exact"),
)


def read_bill(base: Path, tag: str) -> dict[str, Any]:
    path = base / f"bill-{tag}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def setup_of(bill: dict[str, Any]) -> dict[str, str]:
    """决定"两次运行是不是同一个配置"的五样东西。

    **臂名不算证据**——它只是文件名的后缀。这几样来自产物本身。
    """
    scope = bill.get("scope") or {}
    rep = bill.get("reproducibility") or {}
    return {
        "模型": str(scope.get("model", NL)),
        "语料": str((rep.get("corpus") or {}).get("sha256", NL)),
        "工具清单": str(rep.get("tools_list_sha256", NL)),
        "裁剪策略": str(scope.get("compact_policy", NL)),
        "工具数": str(scope.get("n_tools_in_list", NL)),
    }


def compare_setups(a: dict[str, str], b: dict[str, str]) -> tuple[list[str], list[str]]:
    """返回 `(不一致的项, 无法核对的项)` —— **三分类，不是两分类**。

      · 两边都记了且相同 → 可核对且一致
      · 两边都记了但不同 → **不一致**（这不是"同一个配置跑两次"，差值不叫噪声下界）
      · **任一边没记**   → **无法核对**（既不能说一致，也不能说不一致）

    第三类是最容易写错的，两个方向都会错：
      · 写成 `a[k] != b[k]` ⇒ 老账单里没有 `compact_policy` 这个键，就会被判成
        "配置不一致"，于是**唯一可用的历史配对被闸门挡住**（实测：
        `active3b-fx` 的账单是补丁前写的，没有这个字段）；
      · 把"没记"当成"相同" ⇒ 正是本项目栽过的那类跟头
        （当时我把"两批都叫 7b"当成了"两批都是 7B"）。
    所以"不知道"必须单列，且**默认不放行**。
    """
    mismatched: list[str] = []
    unverified: list[str] = []
    for k in a:
        if a[k] == NL or b[k] == NL:
            unverified.append(k)
        elif a[k] != b[k]:
            mismatched.append(k)
    return mismatched, unverified


NL = "未记录"  # 产物里"这个字段没有值"的唯一写法，不许用 None/空串/0 兼职


def per_task_metrics(run: dict[str, Any]) -> dict[str, Any]:
    """一个任务在一次运行里的可观测量。

    **刻意分开三类**：token（provider 实测）、字符（本项目所数）、行为（轮数/调用数）。
    把它们合成一个"效率分"是这类工具最常见的过度声称。

    ⚠️ **这里读到的是 `json.loads` 出来的 dict，不是 `AgentRun` 对象**。
    `AgentRun` 上的 `total_prompt_tokens` / `n_tool_calls` / `total_tool_payload_chars`
    都是 **property**，dict 上一个都没有。第一版照 `AgentRun` 的写法用属性访问，
    脚本一跑就 `AttributeError`——**写出来但没跑过的代码，和没写过没有区别**，
    而这条脚本承载的是"结论里有多少是噪声"这个关键判读，不能停在"看起来写完了"。
    """
    turns = run.get("turns") or []
    if not isinstance(turns, list):
        turns = []
    calls: list[dict[str, Any]] = []
    for t in turns:
        if isinstance(t, dict):
            calls.extend(c for c in (t.get("tool_calls") or []) if isinstance(c, dict))
    return {
        "task": str(run.get("task", "")),
        "prompt_tokens": sum(
            int(t.get("prompt_tokens") or 0) for t in turns if isinstance(t, dict)
        ),
        "completion_tokens": sum(
            int(t.get("completion_tokens") or 0) for t in turns if isinstance(t, dict)
        ),
        "turns": len(turns),
        "tool_calls": len(calls),
        "payload_chars": sum(int(c.get("payload_chars") or 0) for c in calls),
    }


def policy_evidence(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """从**落盘的 trace** 里看这一臂实际用了什么策略、有没有真的裁过。

    这是比对账单**独立**的第二条路径。要它是因为账单字段可能来自老版本
    （没有 `compact_policy`），而**每次工具调用都记着自己的策略**。
    两条路径对不上，说明其中一份不可信——那本身就是要报出来的结论，
    不是可以四舍五入掉的细节。
    """
    seen: dict[str, int] = {}
    n_calls = 0
    n_applied = 0
    for run in runs:
        for t in run.get("turns") or []:
            if not isinstance(t, dict):
                continue
            for c in t.get("tool_calls") or []:
                if not isinstance(c, dict):
                    continue
                n_calls += 1
                pol = str(c.get("compact_policy", NL))
                seen[pol] = seen.get(pol, 0) + 1
                if c.get("compact_applied") is True:
                    n_applied += 1
    return {"per_call_policy": seen, "n_calls": n_calls, "n_compaction_applied": n_applied}


def summarize_delta(pairs: list[tuple[float, float]], label: str) -> dict[str, Any]:
    """两个数列的逐任务差（b - a）：中位/均值/极值 + 方向计数 + 符号检验。

    `sign_p` 用双侧精确符号检验：**当"B 更大的任务数"接近对半时，p 会很大**，
    那正是"这两次运行不可区分"的意思（与"两臂等效"完全无关）。
    """
    deltas = [b - a for a, b in pairs]
    n_pos = sum(1 for d in deltas if d > 0)
    n_neg = sum(1 for d in deltas if d < 0)
    outs: dict[str, Any] = {
        "label": label,
        "n": len(deltas),
        "a_total": sum(a for a, _ in pairs),
        "b_total": sum(b for _, b in pairs),
        "delta_median": statistics.median(deltas) if deltas else 0,
        "delta_mean": statistics.fmean(deltas) if deltas else 0,
        "delta_min": min(deltas) if deltas else 0,
        "delta_max": max(deltas) if deltas else 0,
        "n_b_larger": n_pos,
        "n_b_smaller": n_neg,
        # "逐任务完全相同"要单独数：它是"这个量在两次运行间根本不动"的证据，
        # 方向计数（n_pos/n_neg）里看不见它。
        "n_identical": sum(1 for d in deltas if d == 0),
        "abs_delta_median": statistics.median([abs(d) for d in deltas]) if deltas else 0,
    }
    total_a = sum(a for a, _ in pairs)
    outs["b_over_a"] = (outs["b_total"] / total_a) if total_a else None
    if n_pos + n_neg:
        p, p1, p2 = mcnemar_exact(n_pos, n_neg)
        outs["sign_p_two_sided"] = p
        # 单侧"B 不更大"与"B 不更小"都给出来：方向是这一节最要紧的信息，不给方向
        # 就等于把"系统性偏移"写成了"随机抖动"。
        outs["sign_p_one_sided_b_not_larger"] = p1
        outs["sign_p_one_sided_b_not_smaller"] = p2
    return outs


def fmt(x: float) -> str:
    return f"{x:+,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="同配置两次运行的噪声下界")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--a", required=True, help="第一次运行（如 active7b-fx）")
    ap.add_argument("--b", required=True, help="第二次运行（如 active7b-fx-fp）")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--allow-unrecorded",
        action="store_true",
        help="允许在「两边都没记录」时强行继续（产物标 unverified，结论要打折）",
    )
    args = ap.parse_args()

    base = Path(args.dir)
    bill_a, bill_b = read_bill(base, args.a), read_bill(base, args.b)
    if not bill_a or not bill_b:
        print(f"缺账单：{args.a} 或 {args.b}", file=sys.stderr)
        return 2

    # 先把 trace 读进来：配置核对要同时看账单与落盘 trace 两条路径。
    path_a, path_b = base / f"runs-{args.a}.jsonl", base / f"runs-{args.b}.jsonl"
    if not (path_a.exists() and path_b.exists()):
        print(f"缺 runs：{path_a.name} 或 {path_b.name}", file=sys.stderr)
        return 2
    runs_a, runs_b = load_runs(path_a), load_runs(path_b)
    ev_a, ev_b = policy_evidence(runs_a), policy_evidence(runs_b)

    setup_a, setup_b = setup_of(bill_a), setup_of(bill_b)
    mismatched, unverified = compare_setups(setup_a, setup_b)

    print("=" * 92)
    print(f"同配置两次运行：A={args.a}  B={args.b}   （唯一的变化就是「又跑了一次」）")
    print("=" * 92)
    print("\n--- 配置核对（**臂名不算证据**，这几样来自账单本身）---")
    for k in setup_a:
        mark = "  ?" if k in unverified else ("  ✗" if k in mismatched else "  ✓")
        print(f"{mark} {k:<10} A={setup_a[k]}   B={setup_b[k]}")
    print("\n--- 第二条路径：落盘 trace 里每次调用自己记的策略 ---")
    for tag, ev in ((args.a, ev_a), (args.b, ev_b)):
        dist = "、".join(f"{p}×{n}" for p, n in sorted(ev["per_call_policy"].items()))
        print(
            f"  {tag:<26} 调用 {ev['n_calls']:>3} 次  策略分布：{dist or '—'}"
            f"  实际裁剪生效 {ev['n_compaction_applied']} 次"
        )
    if NL in ev_a["per_call_policy"] or NL in ev_b["per_call_policy"]:
        print(
            "  · 有臂的调用记录里**没有**策略字段（老批次落盘时还没这个字段）\n"
            "    ⇒ 这条路径**证不了**它当时的策略，只能证「记录里没有裁剪的痕迹」"
        )
    if mismatched:
        print(
            f"\n✗ 配置不一致：{mismatched}\n"
            "  这不是「同一个配置跑两次」，是两件不同的事——算出来的差不叫噪声下界。\n"
            "  请先用 compare_compaction_arms.py --require-same-setup 核对臂的选择。",
            file=sys.stderr,
        )
        return 2
    if unverified and not args.allow_unrecorded:
        print(
            f"\n✗ 这些项**无法核对**（至少一边没记录）：{unverified}\n"
            "  「记的一样」和「有一边没记」是两件事。先补上指纹再量噪声，\n"
            "  否则这个数只是「我假设它们相同」的产物"
            "（加 --allow-unrecorded 可强行继续，产物里会标 unverified_setup_fields）。\n"
            f"  提示：可用配对如 {args.a} vs {args.b} 中只要一边是老批次就会落到这里。",
            file=sys.stderr,
        )
        return 2
    if unverified:
        print(
            f"\n⚠ 配置核对**未完成**：{unverified} 无法核对，本次按 --allow-unrecorded 继续。\n"
            "  ⇒ 读数要带上这句：这是「同模型同语料同工具清单」的两次运行，\n"
            "    **但策略/代码那一层没有被产物证实**。",
            file=sys.stderr,
        )

    m_a = {r["task"]: per_task_metrics(r) for r in runs_a}
    m_b = {r["task"]: per_task_metrics(r) for r in runs_b}
    m_a.pop("", None)
    m_b.pop("", None)
    shared = sorted(set(m_a) & set(m_b))
    if not shared:
        print("两批没有共同任务", file=sys.stderr)
        return 2

    print(f"\n共同任务 {len(shared)} 个（A 侧 {len(m_a)}，B 侧 {len(m_b)}）")

    print("\n--- 1) token 侧（provider 实测）---")
    stats: dict[str, Any] = {}
    for key, label in (
        ("prompt_tokens", "prompt token"),
        ("completion_tokens", "completion token"),
        ("turns", "轮数"),
        ("tool_calls", "工具调用数"),
        ("payload_chars", "工具返回字符"),
    ):
        s = summarize_delta([(m_a[t][key], m_b[t][key]) for t in shared], label)
        stats[key] = s
        ratio = f"{s['b_over_a']:.3f}" if s["b_over_a"] else "—"
        print(
            f"  {label:<14} A={s['a_total']:>9,}  B={s['b_total']:>9,}  B/A={ratio}"
            f"   逐任务差 中位={fmt(s['delta_median'])} 均值={fmt(s['delta_mean'])}"
            f" 极值=[{fmt(s['delta_min'])},{fmt(s['delta_max'])}]"
        )
        print(
            f"  {'':<14} 方向：B 更大 {s['n_b_larger']} / B 更小 {s['n_b_smaller']}"
            f" / 相同 {s['n_identical']}"
            + (
                f"  （符号检验双侧 p={s['sign_p_two_sided']:.4g}）"
                if "sign_p_two_sided" in s
                else ""
            )
        )

    print("\n--- 2) 质量侧：同配置两次运行**自己就会翻**几个任务 ---")
    # ⚠️ 键要用**同一个**：`per_task_metrics` 取的是 runs 里的 `task`（**问题原文**），
    # 而 `grade_arm` 返回的 `Grade` 主键是 `task_id`（t01…）。第一版拿问题原文去索引
    # `task_id` 的字典，跑起来 `KeyError`。这里统一按问题原文索引，
    # 并把"不在 TASKS 里、判不了档"的任务**单列剔除**，不让分母悄悄变小。
    g_a = {g.task: g for g in grade_arm(runs_a)}
    g_b = {g.task: g for g in grade_arm(runs_b)}
    graded = [t for t in shared if t in g_a and t in g_b]
    if len(graded) != len(shared):
        print(
            f"  ⚠ 有 {len(shared) - len(graded)} 个共同任务不在 TASKS 里 ⇒ **判不了档**，"
            "已从小节 2 的分母中剔除（小节 1 的 token 仍然用全部共同任务）"
        )
    flips: dict[str, Any] = {}
    for tier, attr in TIERS:
        only_a = sorted(t for t in graded if getattr(g_a[t], attr) and not getattr(g_b[t], attr))
        only_b = sorted(t for t in graded if getattr(g_b[t], attr) and not getattr(g_a[t], attr))
        n_a = sum(1 for t in graded if getattr(g_a[t], attr))
        n_b = sum(1 for t in graded if getattr(g_b[t], attr))
        flips[tier] = {
            "a_only": [g_a[t].task_id for t in only_a],
            "b_only": [g_b[t].task_id for t in only_b],
            "a_hits": n_a,
            "b_hits": n_b,
            "n_graded": len(graded),
        }
        print(
            f"  {tier:<26} A={n_a}/{len(graded)}  B={n_b}/{len(graded)}"
            f"  翻转 {len(only_a)} + {len(only_b)}"
        )
        if only_a or only_b:
            print(f"      A 命中而 B 没命中：{flips[tier]['a_only'] or '—'}")
            print(f"      B 命中而 A 没命中：{flips[tier]['b_only'] or '—'}")

    print("\n--- 3) 读法（**这是本脚本存在的全部理由**）---")
    floor = stats["prompt_tokens"]["abs_delta_median"]
    total_floor = (
        abs(stats["prompt_tokens"]["b_total"] - stats["prompt_tokens"]["a_total"])
        / max(1, stats["prompt_tokens"]["a_total"])
    )
    exact = flips["exact_hit（精确值，为准）"]
    # ⚠️ 只报 `a_only` 会骗人：实测 7B 那对是 0+1（净 +1、k=1），
    # 单看 a_only 会打印"自己翻 0 个任务"，而其实**翻了一个**。
    # 不一致对数 k 与净方向必须一起报，否则"下界"被报小了。
    exact_k = len(exact["a_only"]) + len(exact["b_only"])
    exact_net = len(exact["b_only"]) - len(exact["a_only"])
    print(
        f"  · token 噪声下界：逐任务 |差| 中位 = **{floor:,.0f}**，"
        f"合计相对差 = **{total_floor:.1%}**"
    )
    print(
        f"  · 质量噪声下界：`exact_hit` 同配置自己翻 **{exact_k}/{len(graded)}** 个不一致对"
        f"（净 **{exact_net:+d}** 个任务）"
    )
    print("  · ⇒ **跨臂的差值小于上面这两个数时，只能写「在噪声内」**，")
    print("    既不能写「没测到伤害」，也不能写「没有影响」。")
    print("  · 本脚本**不做**显著性检验：n=32 的两个样本比差异，结论只会是「测不出」。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        import contextlib

        with contextlib.suppress(Exception):
            out.write_text("", encoding="utf-8")  # 先占位，避免半写文件被当成成品
        payload = {
            "a": args.a,
            "b": args.b,
            "setup": setup_a,
            "setup_b": setup_b,
            "unverified_setup_fields": unverified,
            "mismatched_setup_fields": mismatched,
            "trace_policy_evidence": {args.a: ev_a, args.b: ev_b},
            "n_shared": len(shared),
            "stats": stats,
            "quality_flips": flips,
            "noise_floor": {
                "prompt_tokens_abs_delta_median": floor,
                "prompt_tokens_total_relative": total_floor,
                # 不一致对数与净方向**都要**：只给单向计数会把下界报小
                # （7B 那对是 0+1，只看单向会印成"翻 0 个"）。
                "exact_hit_discordant_k": exact_k,
                "exact_hit_net": exact_net,
                "exact_hit_flips": exact_k,
            },
            "note": (
                "token 为 provider 实测；字符为本项目所数，两者不混用。"
                "这是**同配置两次运行**的差，不是两臂的差；它给出「跨臂差值多小算噪声」的下界。"
                "两个样本之间的显著性检验刻意不做（n=32 只会得到「测不出」）。"
                + (
                    f"⚠ 本次 setup 有无法核对的项 {unverified}：读的时候必须带上这句，"
                    "不能说成「配置完全相同的两次运行」。"
                    if unverified
                    else ""
                )
            ),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
