"""运行数据质量评测并打印报告。

用法：
    uv run python scripts/run_eval.py
    uv run python scripts/run_eval.py --triage both        # 规则 vs LLM 同台对比
    uv run python scripts/run_eval.py --json reports/eval.json
    uv run python scripts/run_eval.py --min-recall 0.8 --min-precision 0.5   # CI 门禁
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from findata.agent import LLMTriage, OpenAICompatClient
from findata.eval.runner import run_evaluation


def _print(title: str, report) -> None:
    print(title)
    print("=" * 46)
    width = max(len(k) for k, _ in report.as_rows())
    for key, value in report.as_rows():
        print(f"{key.ljust(width)}  {value}")
    if report.misses:
        print("\n漏检的故障：")
        for item in report.misses:
            print(f"  - {item}")
    if report.false_positives:
        print("\n未匹配到任何标注的告警（噪音）：")
        for item in report.false_positives[:20]:
            print(f"  - {item}")


def main() -> int:
    parser = argparse.ArgumentParser(description="运行数据质量 Agent 评测")
    parser.add_argument("--json", dest="json_path", default=None, help="报告 JSON 输出路径")
    parser.add_argument("--min-recall", type=float, default=0.0, help="故障召回率下限")
    parser.add_argument("--min-precision", type=float, default=0.0, help="告警精确率下限")
    parser.add_argument("--min-suppression", type=float, default=0.0, help="误报抑制率下限")
    parser.add_argument("--seed", type=int, default=20240102, help="故障注入随机种子")
    parser.add_argument(
        "--triage",
        choices=("baseline", "llm", "both"),
        default="baseline",
        help="归因器：baseline=规则；llm=大模型（需 FINDATA_LLM_API_KEY）；both=对比",
    )
    args = parser.parse_args()

    result = run_evaluation(fault_seed=args.seed)
    report = result.report

    if args.triage == "baseline":
        _print("Findata 数据质量评测 · 规则归因器", report)
    else:
        client = OpenAICompatClient()
        if not client.available:
            _print("Findata 数据质量评测 · 规则归因器", report)
            print(
                "\n[跳过 LLM 对比] 未配置 FINDATA_LLM_API_KEY。\n"
                "配置后重跑 --triage both 即可得到两列对比，"
                "归因器接口与评测流程无需改动。"
            )
            return _gate(report, args)
        llm = LLMTriage(client)
        # 用 second_triage 路径：同一份 findings 跑两遍归因，
        # 在 runner 里直接算两套 diagnoses 的 Cohen's κ。
        # 之所以不在脚本里再调一次 run_evaluation：避免重复跑探针
        # 与重新注入故障（两次结果会因为时钟等非确定性微差而对不齐 κ）。
        both_result = run_evaluation(
            fault_seed=args.seed, second_triage=llm
        )
        llm_report = _llm_report_from(both_result)
        _print("Findata 数据质量评测 · 规则 vs LLM", report)
        print()
        _print("LLM 归因器", llm_report)
        print(
            f"\n模型实际判定 {llm.stats.from_llm}/{llm.stats.total} 条"
            f"（降级 {llm.stats.fallback}，拒收非法输出 {llm.stats.rejected}）"
        )
        _print_delta(report, llm_report)
        _print_agreement(both_result.triage_agreement)
        if args.json_path:
            report = llm_report

    return _gate(report, args)


def _llm_report_from(both_result):
    """从 second_triage 路径里抽出"只看 LLM 归因"的那一份报告。"""
    from findata.eval.metrics import evaluate

    llm_dx = both_result.llm_diagnoses
    llm_report = evaluate(
        both_result.findings, llm_dx, both_result.faults, both_result.benigns
    )
    # baseline 报告里的朴素基线用 baseline 的 findings 口径算
    base_report = both_result.report
    llm_report.naive_alert_precision = base_report.naive_alert_precision
    return llm_report


def _print_agreement(agreement: dict | None) -> None:
    """打印归因器一致性 κ（借自 mm-curation 的 curation_eval.cohen_kappa）。

    完全一致 = 1.0；与随机抽签一致 = 0.0；< 0 = 比随机还差。
    这是"两套归因器是否在做同一件事"的统计量，比逐条 diff 更稳。
    """
    if not agreement:
        return
    k_cause = agreement["kappa_root_cause"]
    k_sup = agreement["kappa_suppress"]
    n = agreement["n_compared"]
    src = agreement["source"]
    print(
        f"\n两套归因器一致性（{src}，n={n}）\n"
        + "-" * 46
    )

    def fmt(x):
        return "n/a" if x is None else f"{x:+.4f}"

    print(f"  根因标签 κ       {fmt(k_cause)}")
    print(f"  抑制决策 κ       {fmt(k_sup)}")
    if k_cause is not None and k_cause < 0:
        print("  ⚠  κ<0：两套归因器结论比随机还分歧，需要调查")


def _print_delta(base, other) -> None:
    """并排给出两列关键指标与差值——增量是多少，必须一眼可见。"""
    rows = [
        ("故障召回率", base.fault_recall, other.fault_recall),
        ("根因准确率", base.root_cause_accuracy, other.root_cause_accuracy),
        ("误报抑制率", base.benign_suppression, other.benign_suppression),
        ("告警精确率", base.alert_precision, other.alert_precision),
    ]
    print("\n规则 → LLM 增量")
    print("-" * 46)
    for name, a, b in rows:
        delta = b - a
        sign = "+" if delta > 0 else ("" if delta == 0 else "")
        print(f"{name.ljust(12)}  {a:6.1%} → {b:6.1%}   {sign}{delta:.1%}")


def _gate(report, args) -> int:
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n报告已写入 {out}")

    failures = []
    if report.fault_recall < args.min_recall:
        failures.append(
            f"故障召回率 {report.fault_recall:.1%} 低于下限 {args.min_recall:.1%}"
        )
    if report.alert_precision < args.min_precision:
        failures.append(
            f"告警精确率 {report.alert_precision:.1%} 低于下限 {args.min_precision:.1%}"
        )
    if report.benign_suppression < args.min_suppression:
        failures.append(
            f"误报抑制率 {report.benign_suppression:.1%} 低于下限 {args.min_suppression:.1%}"
        )

    if failures:
        print("\n门禁未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
