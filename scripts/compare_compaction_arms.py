"""R5.2 的结果表：**同一批任务、同一份语料、同一个模型**，只换裁剪策略。

    uv run python scripts/compare_compaction_arms.py \
        --base active7b-fx --arms active7b-ch active7b-ci active7b-cp \
        --out examples/context-audit/compaction-arms.json

这张表要回答的是一个**代价**问题，不是"谁更省"的问题：

    把工具返回裁掉 X%，模型的能力掉多少？

所以每一行都同时给两侧：**省了多少 token（实测）** 与 **质量档掉了几个（配对）**。
只报省、不报质量，是这类工具最常见的过度声称；只报质量、不报省，则没有意义。

三条口径纪律（都是本项目踩过的坑，写进代码而不是只写进文档）：

1. **token 侧只认实测**：`prompt_tokens` 来自 provider usage。字符数是本项目数的，
   用来解释"省在哪"，**不用来换算 token**。
2. **配对检验必须报分辨力下限**：不一致对数 k < 6 时最小可能 p 就是 0.0625+
   ——此时"不显著"的正确读法是"本对照分辨不出"，**不是"两臂等效"**。
   （见 `contextbudget.grading.mcnemar_exact`）
3. **基线必须与被比臂同语料、同工具清单**：脚本会读各臂账单里的语料指纹与
   工具数，不一致就打印硬警告——"唯一变量"这句话要有产物能证伪它。
"""

from __future__ import annotations

import argparse
import json
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


def arm_row(base: Path, tag: str) -> dict[str, Any]:
    """一个臂的**账单侧**数字（实测）与**质量侧**档位。"""
    runs = load_runs(base / f"runs-{tag}.jsonl")
    grades = grade_arm(runs)
    bill = read_bill(base, tag)
    m = bill.get("measured") or {}
    scope = bill.get("scope") or {}
    rep = bill.get("reproducibility") or {}
    n = len(runs)
    row: dict[str, Any] = {
        "arm": tag,
        "n_tasks": n,
        # **模型必须进对照表**（2026-09-22 差点因此得出假结论）：我曾把一批
        # `--model` 漏掉的运行（默认落到 3B）当成"同配置的第二次运行"去算波动，
        # 数字差得很像"噪声很大"。核对时才发现两批不是同一个模型。
        # 教训与 `--require-same-code` 同源：**"唯一变量是 X" 要有产物能证伪它**，
        # 而模型是这套实验里最容易被忘记核对的那一维（它在账单 scope 里，不在臂名里）。
        "model": scope.get("model", "未记录"),
        "compact_policy": scope.get("compact_policy", "未记录"),
        "compact_caps": scope.get("compact_caps") or {},
        "n_tools_in_list": scope.get("n_tools_in_list"),
        "corpus_sha256": (rep.get("corpus") or {}).get("sha256", ""),
        # 真正被执行的那份源码（2026-09-22 补）。**语料根是快照、代码根是工作区**，
        # 所以「同批各臂同代码」这句话必须由它来证伪，而不是靠 mtime。
        "code_sha256": (rep.get("executed_code") or {}).get("sha256", ""),
        "code_commit": rep.get("executed_code_commit", ""),
        "code_dirty": rep.get("executed_code_dirty"),
        "prompt_tokens": m.get("prompt_tokens"),
        "completion_tokens": m.get("completion_tokens"),
        "turns": m.get("total_turns"),
        "tool_calls": m.get("total_tool_calls"),
        "payload_chars": m.get("tool_payload_chars"),
        # 旧批次的账单里没有这一项（裁剪功能上线前跑的）。**不许静默当 0**：
        # 退化成"等于返回字符"（=没裁）是对的，但来源必须写在旁边。
        "compacted_chars": m.get("tool_compacted_chars", m.get("tool_payload_chars")),
        "compacted_chars_source": (
            "实测" if "tool_compacted_chars" in m else "未记录（按『等于返回字符』处理）"
        ),
        "saved_chars": m.get("tool_saved_chars", 0),
        "n_answered": m.get("n_answered"),
    }
    for _label, attr in TIERS:
        row[attr] = sum(1 for g in grades if getattr(g, attr))
    row["_grades"] = {g.task_id: g for g in grades}
    return row


def pair(base_row: dict[str, Any], arm_row_: dict[str, Any], label: str) -> dict[str, Any]:
    a, b = base_row["_grades"], arm_row_["_grades"]
    shared = sorted(set(a) & set(b))
    out: dict[str, Any] = {"n_shared": len(shared)}
    for tier, attr in TIERS:
        only_a = sum(1 for t in shared if getattr(a[t], attr) and not getattr(b[t], attr))
        only_b = sum(1 for t in shared if getattr(b[t], attr) and not getattr(a[t], attr))
        both = sum(1 for t in shared if getattr(a[t], attr) and getattr(b[t], attr))
        k = only_a + only_b
        rec: dict[str, Any] = {
            "both": both,
            "base_only": only_a,
            "arm_only": only_b,
            "discordant_k": k,
            "min_possible_p": 2.0 ** (1 - k) if k else 1.0,
        }
        if k:
            p, p1, _ = mcnemar_exact(only_a, only_b)
            rec["p_two_sided"] = p
            rec["p_one_sided_arm_not_worse"] = p1
        out[tier] = rec
    return out


def fmt_saving(row: dict[str, Any]) -> str:
    p, saved = row.get("payload_chars"), row.get("saved_chars")
    if not p or saved is None:
        return "未记录"
    return f"{saved / p:.1%}"


def main() -> int:
    ap = argparse.ArgumentParser(description="R5.2：裁剪策略的代价对照")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--base", default="active7b-fx", help="基线臂（不裁剪）")
    ap.add_argument("--arms", nargs="*", required=True, help="被比的裁剪臂")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--require-same-setup",
        "--require-same-code",
        action="store_true",
        dest="require_same_setup",
        help=(
            "各臂的**代码指纹 / 模型 / 语料 / 工具清单**只要有一项不一致就非 0 退出。"
            "与 `--require-same-corpus` 同一个道理：「唯一变量是裁剪策略」这句话"
            "要能被证伪，不能只打印一句声明。"
            "（`--require-same-code` 是旧名，行为已扩到整个 setup）"
        ),
    )
    args = ap.parse_args()

    base = Path(args.dir)
    base_row = arm_row(base, args.base)

    print("=" * 100)
    print(
        "R5.2 返回内容裁剪：同一批 32 个任务 / 同一份语料 / 同一个模型，"
        "只换「该留什么」的判断依据"
    )
    print("=" * 100)

    corpora = {base_row.get("corpus_sha256")}
    toolsets = {base_row.get("n_tools_in_list")}
    codes = {base_row.get("code_sha256")}
    models = {base_row.get("model")}
    rows: list[dict[str, Any]] = []
    for tag in args.arms:
        row = arm_row(base, tag)
        rows.append(row)
        corpora.add(row.get("corpus_sha256"))
        toolsets.add(row.get("n_tools_in_list"))
        codes.add(row.get("code_sha256"))
        models.add(row.get("model"))

    print("\n--- 成本侧（token 是实测；字符是本地数的，两者不混用）---")
    head = (
        f"  {'臂':<16}{'裁剪':<7}{'ptok':>8}{'轮数':>6}{'调用':>6}"
        f"{'返回字符':>10}{'进上下文':>10}{'省':>8}"
    )
    print(head)
    for row in [base_row, *rows]:
        print(
            f"  {row['arm']:<16}{row['compact_policy']:<7}"
            f"{str(row['prompt_tokens']):>8}{str(row['turns']):>6}{str(row['tool_calls']):>6}"
            f"{str(row['payload_chars']):>10}{str(row['compacted_chars']):>10}"
            f"{fmt_saving(row):>8}"
        )
    if len(corpora) > 1 or len(toolsets) > 1:
        print("\n  ⚠ **语料或工具清单不一致** —— 差分会把这些差也算进裁剪里，读数不可比")
        print(f"     语料指纹 {len(corpora)} 种 / 工具数 {sorted(x for x in toolsets if x)}")
    if len(codes) > 1:
        print("\n  ⚠ **被执行的代码不是同一份** —— 两臂的差里混进了代码改动")
        print(f"     代码指纹：{sorted(x or '未记录' for x in codes)}")
    elif "" in codes:
        print("\n  · 代码指纹**未记录**（这批臂跑在补丁之前）⇒「同代码」只剩 mtime 这种弱证据")
    if len(models) > 1:
        print("\n  ⚠ **模型不是同一个** —— 这不是对照实验，是跨模型比较")
        print(f"     模型：{sorted(x or '未记录' for x in models)}")
        print("     要么分开比，要么把模型也当成自变量并写进结论口径")

    print("\n--- 复现身份（各臂账单里的指纹，原件在 bill-*.json）---")
    for row in [base_row, *rows]:
        print(
            f"  {row['arm']:<22} 模型={row['model']:<18}"
            f"语料={row['corpus_sha256'] or '未记录':<34}"
            f"代码={row['code_sha256'] or '未记录'}"
            f"{'（脏）' if row['code_dirty'] else ''}"
        )

    print("\n--- 质量侧（分档，不合成单一「准确率」）---")
    tiers_head = f"  {'臂':<16}" + "".join(
        f"{label.split('（')[0]:>14}" for label, _ in TIERS
    )
    print(tiers_head)
    for row in [base_row, *rows]:
        print(
            f"  {row['arm']:<16}"
            + "".join(f"{str(row[attr]) + '/32':>14}" for _, attr in TIERS)
        )

    print("\n--- 配对对照（基线 vs 每个裁剪臂）---")
    paired: dict[str, Any] = {}
    for row in rows:
        pr = pair(base_row, row, row["arm"])
        paired[row["arm"]] = pr
        print(f"\n  【{row['arm']}】策略={row['compact_policy']}  省字符={fmt_saving(row)}")
        for tier, _attr in TIERS:
            rec = pr[tier]
            k = rec["discordant_k"]
            line = (
                f"    {tier:<24} 都中={rec['both']:<3} 基线独有={rec['base_only']:<3}"
                f" 裁剪臂独有={rec['arm_only']:<3} k={k}"
            )
            print(line)
            if not k:
                print("      · 两臂无分歧 → 无法检验（**不是等效**）")
                continue
            print(
                f"      · McNemar 双侧 p={rec['p_two_sided']:.4g}"
                f"  单侧(裁剪臂不差) p={rec['p_one_sided_arm_not_worse']:.4g}"
            )
            if rec["min_possible_p"] >= 0.05:
                print(
                    f"      · ⚠ 分辨力下限 {rec['min_possible_p']:.4g} ≥ 0.05："
                    "k 太小，本对照**测不出**差异（不是'等效'）"
                )

    print("\n--- 诚实说明 ---")
    print("  · `hint` 是竞品协议的**仿真**（内容整体换成一行提示），不是那个产品本体")
    print("  · `cap` 的上限学自**另一批 trace**（active7b-fz）——")
    print("    那批 trace 跑在**另一个语料快照**上（跨语料，反而排除了自泄漏），")
    print("    但**与本次测量是同一批 32 道题**：换一批题还省不省，本实验不含")
    print("  · 省的字符按 [换算] 大约值多少 token，本表**不给**：字符→token 的比值")
    print("    随语言/代码占比变化，只报实测的 prompt_tokens 差")
    print("  · 质量侧只在本机模型口径下成立（7B: qwen2.5:latest / 3B: qwen2.5:3b，逐臂见上表）")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base": {k: v for k, v in base_row.items() if not k.startswith("_")},
            "arms": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
            "paired": paired,
            "note": (
                "token 为实测（provider usage）；字符数为本项目所数，两者不混用。"
                "配对检验报 McNemar 精确 p 与分辨力下限（k<6 时该对照测不出差异）。"
            ),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {out}")
    if args.require_same_setup and (
        len(codes) > 1 or len(models) > 1 or len(corpora) > 1 or len(toolsets) > 1
    ):
        print(
            f"\n✗ 各臂的 setup 不一致（--require-same-setup）："
            f"代码 {len(codes)} 种 / 模型 {len(models)} 种 / 语料 {len(corpora)} 种 / "
            f"工具清单 {len(toolsets)} 种",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
