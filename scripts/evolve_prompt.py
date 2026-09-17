"""运行 M13 prompt 进化：反思式提示进化 + eval-gate 把关。

用法：
    # 内置引擎（零额外依赖，预算可控，推荐先跑）
    uv run python scripts/evolve_prompt.py --engine builtin --gate

    # DSPy GEPA 引擎（需 uv sync --group evolve；候选走同一套验收管线）
    uv run python scripts/evolve_prompt.py --engine dspy --gate

    # 从既有版本文件出发继续进化
    uv run python scripts/evolve_prompt.py --prompt-file eval/prompts/triage_v1.yaml

约定：
- 适应度与门禁同一把尺子：run_evaluation（extended + base 两份语料）+
  metrics.evaluate。进化产物任一语料任一指标回归即 exit 1，不许合入。
- 预算硬顶 --max-llm-calls：到顶即停，开销（调用数/字符量）如实入档。
- 被门禁接受的候选落 eval/prompts/triage_vN.yaml（分数+出处，可回滚）；
  未接受的只在 stdout 报告里留痕，不落版本目录。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# 默认现任：代码里内置的 v1（与 eval/prompts/triage_v1.yaml 应当一致）
from findata.agent import llm_triage as _lt
from findata.agent.llm import OpenAICompatClient
from findata.agent.prompts import PromptVersion, load_prompt, save_prompt
from findata.eval.evolution import (
    Budget,
    CountingClient,
    EvolutionTrace,
    assemble_system,
    build_labeled_items,
    evaluate_everywhere,
    evolve_builtin,
    gate_report,
    no_regression,
    pareto_accepts,
    split_system,
    split_train_held,
)
from findata.eval.runner import run_evaluation

DEFAULT_VERSION = "v1"
_REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = _REPO / "eval" / "prompts"


def _default_incumbent() -> PromptVersion:
    return PromptVersion(
        version=DEFAULT_VERSION,
        target="triage",
        system=_lt._SYSTEM,
        user=_lt._USER,
        metrics={},
        provenance={"engine": "hand"},
        notes="代码内置基线",
    )


def _next_version(out_dir: Path, target: str = "triage") -> str:
    existing = sorted(out_dir.glob(f"{target}_v*.yaml"))
    if not existing:
        return "v1"
    nums = []
    for p in existing:
        token = p.stem[len(target) + 1 :].split("_")[0]  # "v1.1" / "v2_gepa"
        if token.startswith("v"):
            try:
                nums.append(float(token[1:]))
            except ValueError:
                continue
    return f"v{max(nums, default=0.0) + 1:g}"


def main() -> int:
    parser = argparse.ArgumentParser(description="M13 归因 prompt 进化")
    parser.add_argument("--engine", choices=("builtin", "dspy"), default="builtin")
    parser.add_argument("--rounds", type=int, default=2, help="内置引擎进化轮数")
    parser.add_argument("--candidates", type=int, default=2, help="每轮候选数 k")
    parser.add_argument("--max-llm-calls", type=int, default=80, help="LLM 调用预算硬顶")
    parser.add_argument("--seed", type=int, default=20240102, help="故障注入种子")
    parser.add_argument(
        "--prompt-file", default=None, help="现任 prompt 版本文件（默认用代码内置 v1）"
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="版本归档目录")
    parser.add_argument("--version-tag", default=None, help="接受版本的版本号（默认自动递增）")
    parser.add_argument("--gate", action="store_true", help="进化后跑不回归门禁，未过 exit 1")
    parser.add_argument("--save", action="store_true", help="接受的候选落版本文件")
    args = parser.parse_args()

    inner = OpenAICompatClient()
    if not inner.available:
        print("未配置 FINDATA_LLM_API_KEY，进化需要真实模型参与提议与评估。")
        return 2
    # 评估走温度 0（适应度确定性），提议走温度 0.7（候选多样性），共享预算
    budget = Budget(max_calls=args.max_llm_calls)
    client = CountingClient(inner, budget)
    propose_client = CountingClient(OpenAICompatClient(temperature=0.7), budget)

    incumbent = load_prompt(args.prompt_file) if args.prompt_file else _default_incumbent()
    system_head, system_tail = split_system(incumbent.system)
    print(
        f"现任版本 {incumbent.version}（engine={incumbent.provenance.get('engine', '?')}），"
        f"引擎 {args.engine}，预算 {args.max_llm_calls} 次调用"
    )

    if args.engine == "dspy":
        trace = _evolve_dspy(client, budget, incumbent, system_tail, args)
        if trace is None:
            return 3
    else:
        trace = evolve_builtin(
            client,
            propose_client,
            system_head,
            system_tail,
            incumbent.user,
            fault_seed=args.seed,
            rounds=args.rounds,
            k_per_round=args.candidates,
        )

    _print_trace(trace)
    if trace.accepted_reports is None:
        print("\n结果：无候选被接受，现任保留。")
        return 0

    passed, failures = gate_report(trace.accepted_reports, trace.baseline_reports)
    if args.gate:
        if passed:
            print("\n门禁通过：进化产物在 extended + base 全量不回归。")
        else:
            print("\n门禁未通过（不许合入）：")
            for f in failures:
                print(f"  - {f}")

    if args.save:
        pv = PromptVersion(
            version=args.version_tag or _next_version(Path(args.out_dir)),
            target="triage",
            system=assemble_system(trace.accepted_head, system_tail),
            user=incumbent.user,
            metrics={
                "extended_root_cause_accuracy": trace.accepted_reports.extended.root_cause_accuracy,
                "extended_benign_suppression": trace.accepted_reports.extended.benign_suppression,
                "extended_alert_precision": trace.accepted_reports.extended.alert_precision,
                "extended_llm_coverage": trace.accepted_reports.extended_llm_coverage,
                "base_root_cause_accuracy": trace.accepted_reports.base.root_cause_accuracy,
                "base_benign_suppression": trace.accepted_reports.base.benign_suppression,
                "base_alert_precision": trace.accepted_reports.base.alert_precision,
            },
            provenance={
                "engine": args.engine,
                "model": inner.model,
                "fault_seed": args.seed,
                "llm_calls": trace.llm_calls,
                "llm_chars": trace.llm_chars,
                "gate_passed": passed,
                "date": date.today().isoformat(),
                "parent_version": incumbent.version,
            },
            notes=f"GEPA 式反思进化产物；改动要点：{trace.accepted_rationale}",
        )
        path = save_prompt(args.out_dir, pv)
        print(f"\n已归档 {path}")
        return 0 if passed or not args.gate else 1
    return (0 if passed else 1) if args.gate else 0


def _evolve_dspy(
    client: CountingClient, budget: Budget, incumbent: PromptVersion, system_tail: str, args
) -> EvolutionTrace | None:
    """DSPy GEPA 引擎：提议交给 dspy.GEPA，验收仍走本仓库的评测与门禁。

    任何 dspy 侧失败（未安装 / API 漂移 / 预算）都如实报告并返回 None，
    由调用方决定是否回落内置引擎——不静默降级。
    """
    try:
        import dspy
    except ImportError as e:
        print(f"dspy 未安装（uv sync --group evolve 后重试）：{e}")
        return None

    from findata.core.config import settings

    # ---- 语料（每条：证据 JSON → 根因/级别）----
    probe = run_evaluation(fault_seed=args.seed, extended=True)
    items = build_labeled_items(probe)
    train_items, held_items = split_train_held(items, probe)
    assert probe.ctx is not None
    from findata.agent.llm_triage import build_evidence

    def to_example(it):
        return {
            "evidence": build_evidence(it.finding, probe.ctx),
            "root_cause": it.expected_cause.value,
            "suppressed": it.expected_suppressed,
        }

    trainset = [to_example(it) for it in train_items]
    valset = [to_example(it) for it in held_items] or trainset

    lm_kwargs = dict(
        api_base=settings.llm_base_url.rstrip("/") + "/v1",
        api_key=client.inner.api_key,
        model_type="chat",
    )
    lm = dspy.LM(f"openai/{client.inner.model}", **lm_kwargs)
    reflect_lm = dspy.LM(f"openai/{client.inner.model}", **lm_kwargs)
    dspy.configure(lm=lm)

    class TriageSignature(dspy.Signature):
        """（instructions 由 GEPA 进化）判定一个数据质量信号的根因。"""

        evidence: str = dspy.InputField(desc="信号证据 JSON")
        root_cause: str = dspy.OutputField(desc="根因，取白名单内的值")
        suppressed: bool = dspy.OutputField(desc="是否为合法事件应抑制")

    class TriageProgram(dspy.Module):
        def __init__(self, instructions: str):
            super().__init__()
            self.predict = dspy.Predict(TriageSignature)
            self.predict.signature = self.predict.signature.with_instructions(instructions)

        def forward(self, evidence: str):
            return self.predict(evidence=evidence)

    def metric(example, pred, trace=None) -> float:
        ok_cause = str(pred.root_cause).strip() == example["root_cause"]
        ok_sup = bool(pred.suppressed) == bool(example["suppressed"])
        return (1.0 if ok_cause else 0.0) + (1.0 if ok_sup else 0.0)

    head, _tail = split_system(incumbent.system)
    optimizer = dspy.GEPA(
        metric=metric,
        auto="light",
        reflection_lm=reflect_lm,
        track_stats=True,
        warn_on_score_mismatch=False,
    )
    print(f"dspy.GEPA 编译中（train={len(trainset)} val={len(valset)}）……")
    try:
        ds_train = [dspy.Example(**ex).with_inputs("evidence") for ex in trainset]
        ds_val = [dspy.Example(**ex).with_inputs("evidence") for ex in valset]
        TriageProgram(head)  # 初始化一次：签名指令注入是否可行，早失败早暴露
        evolved = optimizer.compile(trainset=ds_train, valset=ds_val)
    except Exception as e:  # noqa: BLE001
        print(f"dspy.GEPA 编译失败：{e}")
        return None

    evolved_head = str(evolved.predict.signature.instructions)
    trace = EvolutionTrace()
    trace.baseline_reports = evaluate_everywhere(
        incumbent.system, incumbent.user, args.seed, client
    )
    cand_reports = evaluate_everywhere(
        assemble_system(evolved_head, system_tail), incumbent.user, args.seed, client
    )
    accepted = pareto_accepts(
        cand_reports.extended,
        trace.baseline_reports.extended,
        challenger_coverage=cand_reports.extended_llm_coverage,
        incumbent_coverage=trace.baseline_reports.extended_llm_coverage,
    ) and no_regression(cand_reports.base, trace.baseline_reports.base)
    trace.rounds.append(
        {"engine": "dspy.GEPA", "accepted": accepted, "head_chars": len(evolved_head)}
    )
    if accepted:
        trace.accepted_head = evolved_head
        trace.accepted_rationale = "dspy.GEPA 反思进化产物"
        trace.accepted_reports = cand_reports
    trace.llm_calls = budget.calls
    trace.llm_chars = budget.chars
    return trace


def _print_report_row(name: str, base: float, other: float) -> None:
    delta = other - base
    sign = "+" if delta > 0 else ""
    print(f"  {name.ljust(14)} {base:6.1%} → {other:6.1%}   {sign}{delta:.1%}")


def _print_trace(trace: EvolutionTrace) -> None:
    print(f"\nLLM 开销：{trace.llm_calls} 次调用，约 {trace.llm_chars} 字符往返")
    if trace.stopped_reason:
        print(f"停止原因：{trace.stopped_reason}")
    for rec in trace.rounds:
        phase = rec.get("phase", f"轮 {rec.get('round')}")
        head_line = f"[{phase}]"
        if "train_score" in rec:
            head_line += f" 训练集得分 {rec['train_score']:.1%}（n={rec.get('train_size', '?')}）"
        print(head_line)
        for c in rec.get("candidates", []):
            print(f"  候选：{c['train_score']:.1%} —— {c['rationale']}")
        if "accepted" in rec:
            print(f"  接受：{'是' if rec['accepted'] else '否'}")
        for f in rec.get("pareto_failures", []) or []:
            print(f"  门禁退化项：{f}")
    if trace.accepted_reports is not None and trace.baseline_reports is not None:
        for corpus in ("extended", "base"):
            b = getattr(trace.baseline_reports, corpus)
            e = getattr(trace.accepted_reports, corpus)
            print(f"\n{corpus} 语料（进化前 → 进化后）")
            _print_report_row("根因准确率", b.root_cause_accuracy, e.root_cause_accuracy)
            _print_report_row("误报抑制率", b.benign_suppression, e.benign_suppression)
            _print_report_row("告警精确率", b.alert_precision, e.alert_precision)


if __name__ == "__main__":
    sys.exit(main())
