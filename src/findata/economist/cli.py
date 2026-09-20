"""`findata-context-economist` 的命令行入口。

    findata-context-economist --trace examples/context-audit --tag all7b-fz \\
        --corpus-root /path/to/snapshot --usd-per-mtok 0.4

读一份（或两份）真实 trace，输出**经过验证的**上下文与工具策略改动 + 三组数字。
参数设计的三条理由：

- **`--trace` 接受目录或文件**：目录下按 `runs-*.jsonl` / `spans-*.jsonl` 发现。
  文件多于一个又没给 `--tag` 时报错，不猜——静默挑一个会让报告的输入
  变成一个没人核对过的选择。
- **`--corpus-root` 是可选的，但缺了它就没有字段级结论**：工具返回的原文
  不在 trace 里，要靠重放重建；重建必须有语料。缺语料根时报告照出，
  但 §1.2 会明写「字段级归因未观测」。
- **`--usd-per-mtok` 没有默认值**：默认单价会让报告凭空长出一个美元数字。
  不给单价就只报 token。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from findata.economist.analyze import (
    GRADER_KEYWORD,
    GRADER_NONE,
    Analysis,
    analyze,
    verify,
)
from findata.economist.report import render, render_json
from findata.economist.trace import TraceBundle, load_bundle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="findata-context-economist",
        description="读真实 agent trace，输出经核对的上下文/工具策略改动与三组数字",
    )
    parser.add_argument("--trace", required=True, help="改前 trace：目录或 .jsonl 文件")
    parser.add_argument("--tag", default="", help="trace 目录里有多个文件时用它指定")
    parser.add_argument("--after", default="", help="改后 trace（做改前/改后对照时才需要）")
    parser.add_argument("--after-tag", default="", help="改后 trace 的选择器")
    parser.add_argument(
        "--corpus-root",
        default="",
        help="重建工具返回所用的语料根。**不给就没有字段级归因**（会明写为未观测）",
    )
    parser.add_argument(
        "--grader",
        choices=(GRADER_NONE, GRADER_KEYWORD),
        default=GRADER_KEYWORD,
        help="判分器。keyword = 用内置任务集的关键词判据（弱判据）；none = 不判分",
    )
    parser.add_argument(
        "--usd-per-mtok",
        type=float,
        default=0.0,
        help="每百万 prompt token 的单价；0（默认）= 不折算金额",
    )
    parser.add_argument(
        "--tools-file",
        default="",
        help="可选：下发给模型的工具清单 JSON（形如 {\"all\": [...], \"active\": [...]}）",
    )
    parser.add_argument("--label", default="", help="报告标题里的批次名，缺省用文件名")
    parser.add_argument("--out-dir", default="examples/context-economist")
    return parser.parse_args(argv)


def load_offered_tools(path: str) -> dict[str, list[dict]]:
    """候选工具清单。**只在能被字符数核对上时才被采用**（见 analyze._identify_offered_tools）。"""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    from findata.contextbudget.schemas import ACTIVE_TOOLS, ALL_TOOLS

    return {"all": list(ALL_TOOLS), "active": list(ACTIVE_TOOLS)}


def default_system_prompt() -> str:
    """trace 里不记系统提示（它是常量，不在逐轮 messages 里），只能由调用方提供。

    拿不到就拿不到——报告会把它记成一条已声明的缺口，而不是假装没有它也没影响。
    """
    try:
        from findata.contextbudget.agent import SYSTEM_PROMPT
    except ImportError:
        return ""
    return SYSTEM_PROMPT


def _sh_arg(value: object) -> str:
    """把一个参数写成**可以直接复制粘贴执行**的形式。

    两个实测坑，都是"复现命令复现不了自己"：

    ① Windows 路径里的反斜杠在 POSIX shell 下会被当成转义符吃掉
       （`shlex.split(r'--trace C:\\\\a\\\\b')` 得到 `C:ab`）。统一换成正斜杠——
       Windows 也认正斜杠，两边都能跑。
    ② 含空格的路径不加引号会被拆成两个参数。

    判据：**复现段是给外部核对用的入口，它印出来的东西必须真的能跑**，
    而不是"看起来像一条命令"。
    """
    text = str(value).replace("\\", "/")
    if any(ch in text for ch in ' \t"\'()&;'):
        text = '"' + text.replace('"', '\\"') + '"'
    return text


def print_summary(analysis: Analysis, verification, usd_per_mtok: float) -> None:
    a = analysis
    print("=" * 74)
    print(f"findata-context-economist · {a.label}")
    print("=" * 74)
    print(
        f"任务={len(a.bundle.runs)}  工具调用={a.bundle.n_tool_calls}  "
        f"轮数={sum(r.n_turns for r in a.bundle.runs.values())}"
    )
    r = a.reconstruct
    if r.corpus_root:
        print(
            f"重建对账：{r.n_ok}/{r.n_calls} 通过（{r.coverage:.1%}）  "
            f"不一致={r.n_mismatch}  无参数={r.n_skipped_no_args}"
        )
    else:
        print("重建对账：**未提供语料根 ⇒ 字段级归因未观测**")

    c2t = a.chars_to_tokens
    if c2t.n_turns:
        print(
            f"换算基准：{c2t.ratio:.4f} token/字符（中位 {c2t.median:.4f}，"
            f"p10–p90 {c2t.p10:.4f}–{c2t.p90:.4f}，n={c2t.n_turns}）"
        )

    print("\n--- 逐工具 ---")
    for agg in sorted(a.tool_aggs.values(), key=lambda x: -x.payload_chars):
        codes = ",".join(f"{k}:{v}" for k, v in sorted(agg.codes.items()))
        print(
            f"  {agg.tool:<14} calls={agg.calls:<3} chars={agg.payload_chars:<8} "
            f"segs={agg.n_segments:<4} ref={agg.n_referenced:<4} "
            f"not_ref={agg.n_not_referenced:<4} unver={agg.n_unverifiable:<4} [{codes}]"
        )

    if a.flows:
        print("\n--- 跨工具信息流（最硬证据）---")
        for flow in sorted(a.flows.values(), key=lambda f: -f.n_token_hits)[:8]:
            print(
                f"  {flow.from_tool} → {flow.to_tool}: "
                f"命中 {flow.n_token_hits} 次，涉及 {flow.n_segments} 段，"
                f"样例 {flow.samples[:3]}"
            )

    print("\n--- 提案 ---")
    total = 0
    for p in a.proposals:
        total += p.saving_chars
        print(f"  [{p.strength.split('（')[0]}] {p.lever} · {p.target}")
        print(f"      省 {p.saving_chars:,} 字符 ≈ {int(p.saving_chars * c2t.ratio):,} tokens")
    if a.proposals:
        tokens = int(total * c2t.ratio)
        print(f"  合计（**不可简单相加**，返回侧提案有重叠）：{total:,} 字符 ≈ {tokens:,} tokens")
        if usd_per_mtok > 0:
            print(f"  折算：${tokens * usd_per_mtok / 1e6:.4f}（单价 ${usd_per_mtok}/M）")
        else:
            print("  折算：未提供单价 ⇒ 金额不折算")
    else:
        print("  （未生成提案）")

    print("\n--- 三组数字的状态 ---")
    if verification and verification.n_paired:
        v = verification
        delta = v.after_tokens - v.before_tokens
        pct = delta / v.before_tokens if v.before_tokens else 0.0
        print(f"  省 token ：**实测** {v.before_tokens:,} → {v.after_tokens:,} "
              f"({delta:+,}，{pct:+.1%})，配对 {v.n_paired} 个任务")
        print("             （对照见报告 §7；估算与实测的差额也记在那里）")
    else:
        print("  省 token ：未实测（未提供改后一臂）+ 换算上界见上")
    if usd_per_mtok > 0:
        print("  折算成本 ：已折算（继承换算偏差）")
    else:
        print("  折算成本 ：未折算（缺单价）")
    if verification and verification.n_graded:
        v = verification
        print(
            f"  成功率   ：命中 {v.before_hits} → {v.after_hits} / {v.n_graded}，"
            f"McNemar p={v.mcnemar_p:.4f}"
        )
    else:
        print("  成功率   ：未测得（缺改后一臂或判分器）")
    print()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    bundle = load_bundle(args.trace, tag=args.tag, label=args.label)
    after_bundle: TraceBundle | None = None
    if args.after:
        after_bundle = load_bundle(
            args.after, tag=args.after_tag, label=f"{bundle.label}→改后"
        )

    analysis = analyze(
        bundle,
        corpus_root=args.corpus_root or None,
        system_prompt=default_system_prompt(),
        offered=load_offered_tools(args.tools_file),
    )

    verification = None
    if after_bundle is not None:
        verification = verify(bundle, after_bundle, grader=args.grader)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = analysis.label.replace("/", "_").replace("\\", "_")
    report_path = out_dir / f"report-{slug}.md"
    json_path = out_dir / f"economist-{slug}.json"

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # **`--label` 必须写进复现命令**：输出文件名（`slug`）和报告标题都由 label 决定。
    # 少了它，照抄这条命令会写出**另一组文件名** —— 实测踩到：跑完得到
    # `report-examples_context-audit.md`，而报告自己叫 `report-frozen7b-all-vs-active.md`。
    # **一条不能复现自己的复现命令，等于没有复现段。** `--tools-file` 同理：
    # 不记下来，"下发了哪些工具"可能重建得不一样 ⇒ 定义侧提案会跟着变。
    reproduce = (
        f"findata-context-economist --trace {_sh_arg(args.trace)}"
        + (f" --tag {_sh_arg(args.tag)}" if args.tag else "")
        + f" --label {_sh_arg(analysis.label)}"
        + (f" --after {_sh_arg(args.after)}" if args.after else "")
        + (f" --after-tag {_sh_arg(args.after_tag)}" if args.after_tag else "")
        + (f" --corpus-root {_sh_arg(args.corpus_root)}" if args.corpus_root else "")
        + f" --grader {args.grader}"
        + (f" --tools-file {_sh_arg(args.tools_file)}" if args.tools_file else "")
        + (f" --usd-per-mtok {args.usd_per_mtok}" if args.usd_per_mtok else "")
        + f" --out-dir {_sh_arg(args.out_dir)}"
    )

    report_path.write_text(
        render(
            analysis,
            verification=verification,
            usd_per_mtok=args.usd_per_mtok,
            reproduce_cmd=reproduce,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            render_json(analysis, verification=verification, usd_per_mtok=args.usd_per_mtok),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print_summary(analysis, verification, args.usd_per_mtok)
    print(f"落盘：{report_path}")
    print(f"      {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
