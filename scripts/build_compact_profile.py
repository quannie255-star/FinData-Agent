"""从**已跑过的一批 trace**里学出「哪些字段从来没被引用过」——R5.2 的证据臂输入。

    uv run python scripts/build_compact_profile.py \
        --trace examples/context-audit/runs-active7b-fz.jsonl \
        --corpus-root "$SNAP" --out examples/context-audit/compaction-profile-active7b.json

这个脚本只做一件事：把 `findata-context-economist` 已经算出来的**字段级引用判定**
按 `(工具, 字段种类)` 汇总成一张表，据此生成"下一轮该丢掉哪一类字段"的规则。

三条必须写清楚的事：

1. **这是"从 trace 反推"的那一臂的输入**，不是判据。判据仍然是
   `attribution.judge_field`（重放路线）或 `judge_record`（原生路线），
   本脚本只是给它换个分组方式。

2. **规则是"整类丢"，不是逐字段丢。** 逐字段丢会退化成"记住了这批任务的答案"
   ——同一批任务上必然赢，换一批任务就露馅。整类丢（例如
   `read_file` 的 `block` 全留、`section` 全丢）才是**可以迁移到新调用**的策略。
   代价也要说：整类丢比逐字段丢省得少。省多少由实测给，不由设计者给。

3. **`ref_rate == 0` 是"观测到 0 次引用"，不是"证明没用"。** 观测次数少
   （比如某一类只有 3 段）时，0 次引用完全可能是抽样噪声。所以表里同时给
   `n` 与 `n_ref`，**丢不丢的门槛写在产物里**（`keep_rule`），可以被别人改。

**过拟合边界（必须在报告里跟着数字一起出现）**：profile 与对照臂用的是
同一批 32 个任务、同一份语料。真实部署里 profile 应当来自**别的任务**。
所以本实验测的是「证据型裁剪能不能在不伤结果的前提下省下 token」这一定性结论，
**不是**「用旧 trace 学到的规则在新任务上也能省这么多」那一定量结论。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from findata.contextbudget import attribution
from findata.economist.analyze import analyze
from findata.economist.trace import load_bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 trace 学出字段裁剪规则")
    parser.add_argument("--trace", required=True, help="源 trace（runs-*.jsonl）")
    parser.add_argument("--corpus-root", required=True, help="重放语料根（必须是冻结快照）")
    parser.add_argument("--out", required=True, help="输出的 profile JSON")
    parser.add_argument(
        "--drop-building",
        action="store_true",
        help=(
            "同时**丢掉 `header:` / `whole:` 这两类结构性字段**。"
            "默认不丢：它们的引用率低多半是因为它们承载的是文件名/文档首行，"
            "而那正是「这个返回是哪个文件」的锚点。把它们当垃圾丢，属于"
            "把「没观测到引用」读成「没用」，本工具箱里明确禁止这个读法。"
        ),
    )
    return parser.parse_args()


@dataclass
class KindStat:
    tool: str
    kind: str
    n: int = 0
    n_ref: int = 0
    chars: int = 0
    chars_ref: int = 0

    @property
    def ref_rate(self) -> float:
        return self.n_ref / self.n if self.n else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "n_ref": self.n_ref,
            "ref_rate": round(self.ref_rate, 4),
            "chars": self.chars,
            "chars_ref": self.chars_ref,
        }


def collect(verdicts: list) -> dict[tuple[str, str], KindStat]:
    table: dict[tuple[str, str], KindStat] = {}
    for v in verdicts:
        # 状态为「不可判定」的段**不进表**：它们既不能算被引用，也不能算没被引用。
        # 混进来等于把「没证据」记成「证据表明没用」——本工具箱里最忌讳的一次错。
        if v.status == attribution.STATUS_UNVERIFIABLE:
            continue
        key = (v.tool, v.kind)
        stat = table.setdefault(key, KindStat(tool=v.tool, kind=v.kind))
        stat.n += 1
        stat.chars += v.chars
        if v.referenced:
            stat.n_ref += 1
            stat.chars_ref += v.chars
    return table


@dataclass
class CallStat:
    """一次工具调用里「被引用的段有多少个」。

    这是**上限规则**的依据：如果 32 个任务、53 次调用里，模型单次引用过的
    `list_files` 路径最多只有 4 个，那"返回 90 个路径"就是可裁的；
    而"该留几个"这件事，答案不该由设计者拍脑袋，应该由这张表给。

    **口径提醒**：分组键是 `(task, tool, turn)`。同一轮里模型并发发了两次
    同名调用时，它们会被并成一组，于是"单次引用的最大数"会被**高估**。
    高估的方向是**上限更松、裁得更少**——保守的一侧，不会放大结论。
    """

    tool: str
    n_calls: int = 0
    counts: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        ordered = sorted(self.counts)
        return {
            "n_calls": self.n_calls,
            "n_ref_p50": _pct(ordered, 0.50),
            "n_ref_p90": _pct(ordered, 0.90),
            "n_ref_max": ordered[-1] if ordered else 0,
        }


def _pct(ordered: list[int], q: float) -> int:
    if not ordered:
        return 0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def collect_calls(verdicts: list) -> dict[str, CallStat]:
    per_call: dict[tuple[str, str, int], int] = {}
    per_call_n: dict[tuple[str, str, int], int] = {}
    tool_of: dict[tuple[str, str, int], str] = {}
    for v in verdicts:
        if v.status == attribution.STATUS_UNVERIFIABLE:
            continue
        key = (v.task, v.tool, v.turn)
        tool_of[key] = v.tool
        per_call_n[key] = per_call_n.get(key, 0) + 1
        if v.referenced:
            per_call[key] = per_call.get(key, 0) + 1
    out: dict[str, CallStat] = {}
    for key, tool in tool_of.items():
        stat = out.setdefault(tool, CallStat(tool=tool))
        stat.n_calls += 1
        stat.counts.append(per_call.get(key, 0))
    return out



def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.trace)
    result = analyze(bundle, corpus_root=args.corpus_root)
    table = collect(result.verdicts)
    calls = collect_calls(result.verdicts)

    if not table:
        print("字段级判定为空 —— 没有字段就没有可反推的规则，退出非 0", file=sys.stderr)
        return 1

    # 「上限规则」：单次调用该留几个段。取**被引用个数的 p90**，
    # 不取 max（max 会被一次异常调用抬到天上，等于不裁），不取 p50（会砍掉真实使用）。
    # 每个工具至少留 1 个——留 0 个不再是"裁剪"，是"把这个工具废掉"，
    # 那是另一个实验（本工具箱里对应"合理性判断裁剪"那一臂）。
    caps = {tool: max(1, int(s.to_dict()["n_ref_p90"])) for tool, s in calls.items()}
    cap_rule = "cap[tool] = 单次调用**被引用段数**的 p90（至少 1）；超出的段折成一行提示"

    keep_rule = (
        "ref_rate > 0 即整类保留；ref_rate == 0 的整类丢"
        "（header/whole 除外，除非 --drop-building）"
        "｜⚠️ 效力：ref_rate == 0 读作「**本批 trace 观测到 0 次引用**」，"
        "**不是「证明没用」**——任务没覆盖到也会得到 0。判断依据见同目录 table 里的"
        "原始计数 n / n_ref（不是只给比例），且处置**只做整类丢不做逐条判死**、"
        "丢了仍然可逆（模型还能再调一次这个工具，存储里没有任何东西被删）"
    )
    never_referenced = [
        (tool, kind) for (tool, kind), s in table.items() if s.n_ref == 0
    ]
    structural = {"header", "whole"}
    drop = sorted(
        (tool, kind)
        for tool, kind in never_referenced
        if args.drop_building or kind not in structural
    )
    keep = sorted(k for k in table if k not in set(drop))

    savings = sum(table[k].chars for k in drop)
    total = sum(s.chars for s in table.values())

    profile = {
        "source_run": str(args.trace),
        "corpus_root": str(args.corpus_root),
        "label": bundle.label,
        "n_verdicts": len(result.verdicts),
        "n_calls_native": result.n_calls_native,
        "n_calls_replayed": result.n_calls_replayed,
        "keep_rule": keep_rule,
        "keep": [{"tool": t, "kind": k} for t, k in sorted(set(keep))],
        "drop": [{"tool": t, "kind": k} for t, k in drop],
        "cap_rule": cap_rule,
        "caps": caps,
        "per_call": {tool: s.to_dict() for tool, s in sorted(calls.items())},
        "table": [
            {"tool": t, "kind": k, **s.to_dict()}
            for (t, k), s in sorted(table.items(), key=lambda kv: -kv[1].chars)
        ],
        "savings": {
            "seg_chars_droppable": savings,
            "seg_chars_total": total,
            "fraction": round(savings / total, 4) if total else 0.0,
            "note": (
                "这个比例是**字段级字符**上的，不是 prompt token 上的："
                "真实进上下文的字符串里还夹着段之间的连接文本，"
                "而且模型可能因为上下文变短而改变行为（多调或少调工具）。"
                "报告里的 token 数一律来自实测。"
            ),
        },
        "counts": {"n_never_referenced_kinds": len(never_referenced)},
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"源 trace          : {args.trace}  （{bundle.label}）")
    print(f"字段级判定条数    : {len(result.verdicts)}")
    print(f"  原生记录路径    : {result.n_calls_native} 次调用")
    print(f"  重放路径        : {result.n_calls_replayed} 次调用")
    print("\n--- (工具, 字段种类) 引用率表 ---")
    head = (
        f"  {'tool':<16}{'kind':<10}{'n':>5}{'n_ref':>7}"
        f"{'rate':>8}{'chars':>10}{'chars_ref':>11}"
    )
    print(head)
    for (tool, kind), s in sorted(table.items(), key=lambda kv: -kv[1].chars):
        flag = "  ← 丢" if (tool, kind) in set(drop) else ""
        print(
            f"  {tool:<16}{kind:<10}{s.n:>5}{s.n_ref:>7}{s.ref_rate:>8.2%}"
            f"{s.chars:>10,}{s.chars_ref:>11,}{flag}"
        )
    print(
        f"\n可丢字段字符 {savings:,} / 字段字符合计 {total:,}"
        f"（{savings / total:.1%}，字段级口径）"
        if total
        else "\n可丢字段字符 0"
    )
    print(f"规则：{keep_rule}")
    print("\n--- 单次调用「被引用段数」分布（上限规则的依据）---")
    print(f"  {'tool':<16}{'calls':>6}{'p50':>6}{'p90':>6}{'max':>6}{'cap':>6}")
    for tool, s in sorted(calls.items()):
        d = s.to_dict()
        print(
            f"  {tool:<16}{d['n_calls']:>6}{d['n_ref_p50']:>6}{d['n_ref_p90']:>6}"
            f"{d['n_ref_max']:>6}{caps[tool]:>6}"
        )
    print(f"规则：{cap_rule}")
    print(f"落盘：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
