"""M14 RL 管线一条命令：环境 → 奖励 → 轨迹采集 → 训练数据导出 → 回归门禁。

对应 ROADMAP M14 验收门禁的「可复现：固定 seed / 数据版本，一条命令重跑」。
无 GPU 降级口径下，本命令覆盖数据侧完整闭环；权重更新（GRPO）待有卡，
步骤见 docs/rl-experiment.md。

用法：
    uv run python scripts/rl_pipeline.py                          # mock 模式（无 key）
    uv run python scripts/rl_pipeline.py --seeds 3 --extended     # 多 seed + 扩充语料
    uv run python scripts/rl_pipeline.py --archive examples/rl-baseline.txt

行为：
- 检测到 FINDATA_LLM_API_KEY 时用真实 LLM 跑归因 rollout 与问数 rollout
  （backend=openai_compat）；否则 mock 跑通管线（backend=mock，reward 曲线
  只证明管线在动，不证明策略能力——mock 无推理，如实标注）
- 无论 backend，规则引擎基线（BaselineTriage 同尺子计分）总是产出，
  作为「训练前基线」的对照锚点
- 产物（out 目录）：report.json / report.md / tasks_attribution.jsonl /
  tasks_ask.jsonl / agl_events.jsonl / trajectories.jsonl
- 回归门禁：归因语料上 alert_precision 不得低于朴素基线（归因层价值
  仍在的最低断言），attribution episode 不允许出现 error；不满足退出码 1

退出码：0 全过；1 gate 拦截；2 环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLDEN_PATH = ROOT / "eval" / "golden" / "agent.yaml"
SEED0 = 20240102  # 与 run_evaluation 缺省 seed 同源，保证可复现

# mock 模式的确定性动作：一直查证据直到超时。不假装 mock 有判断力——
# 它的作用是验证管线与产线一致性，reward≈0 正是"无策略"的诚实读数。
_MOCK_DEFAULT = '{"action": "query_evidence", "arguments": {}}'


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Findata RL 管线（M14）一条命令")
    p.add_argument("--seeds", type=int, default=3, help="故障注入 seed 数量（SEED0 起连续）")
    p.add_argument(
        "--extended", action="store_true", help="叠加扩充语料（合法放量 vs 单位变更混淆对）"
    )
    p.add_argument("--max-turns", type=int, default=6, help="归因 episode 最大轮数")
    p.add_argument("--out", default=None, help="产物目录（默认 eval/rl_runs/<时间戳>）")
    p.add_argument("--archive", default=None, help="报告归档路径（如 examples/rl-baseline.txt）")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from findata.config import settings
    from findata.dq.models import RootCause
    from findata.dq.triage import BaselineTriage
    from findata.eval.runner import run_evaluation
    from findata.rl.env import build_ask_tasks, build_attribution_tasks
    from findata.rl.reward import attribution_reward
    from findata.rl.rollout import aggregate, rollout_episode

    has_key = bool(settings.llm_api_key)
    backend = "openai_compat" if has_key else "mock"
    default_out = ROOT / "eval" / "rl_runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else default_out
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"backend={backend}  seeds={args.seeds}  extended={args.extended}  out={out_dir}")

    # ── 1. 归因任务（任务 A）：确定性故障注入 → 带标注语料 ──
    episodes = []
    attr_task_count = 0
    rule_rewards: list[float] = []
    attr_tasks = []
    rules = BaselineTriage()
    for s in range(args.seeds):
        result = run_evaluation(fault_seed=SEED0 + s, extended=args.extended)
        pairs = build_attribution_tasks(result, seed=SEED0 + s, max_turns=args.max_turns)
        for task, env in pairs:
            # 规则基线：与 rollout 同一把奖励尺子，度量规则引擎的 reward 水位
            dx = rules.diagnose(env.item.finding, result.ctx)
            r, _ = attribution_reward(
                dx,
                RootCause(task.ground_truth["expected_cause"]),
                bool(task.ground_truth["expected_suppressed"]),
            )
            rule_rewards.append(r)
        policy = _make_policy(has_key, args.max_turns)
        for task, env in pairs:
            episodes.append(rollout_episode(task, env, policy))
        attr_tasks.extend(t for t, _ in pairs)
        attr_task_count += len(pairs)
    log(f"任务 A：{attr_task_count} 个归因 episode 已完成")

    # ── 2. 问数任务（任务 B）：golden 集（需要真实 LLM 与仓库）──
    ask_skipped: str | None = None
    ask_tasks = []
    cases = _load_golden()
    if has_key:
        runner, conn = _make_ask_runner(out_dir)
        try:
            ask_pairs = build_ask_tasks(cases, runner)
            for task, env in ask_pairs:
                episodes.append(rollout_episode(task, env, None))
            ask_tasks = [t for t, _ in ask_pairs]
        finally:
            conn.close()
        log(f"任务 B：{len(ask_tasks)} 个问数 episode 已完成")
    else:
        ask_skipped = (
            "未配置 FINDATA_LLM_API_KEY：任务 B rollout 跳过（任务行仍导出，"
            "见 tasks_ask.jsonl）。问数策略是 LangGraph 图，离不开真实 LLM。"
        )
        ask_tasks = [t for t, _ in build_ask_tasks(cases, None)]
        log(f"任务 B：跳过 rollout（{len(ask_tasks)} 个任务行仍导出）")

    # ── 3. 聚合与导出 ──
    from findata.rl.export import write_agl_events, write_tasks_jsonl, write_trajectories

    agg = aggregate(episodes)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backend": backend,
        "seeds": [SEED0 + s for s in range(args.seeds)],
        "extended": args.extended,
        "max_turns": args.max_turns,
        "n_tasks": {"attribution": attr_task_count, "ask": len(ask_tasks)},
        "ask_skipped": ask_skipped,
        "aggregate": agg,
        "rule_baseline": {
            "n": len(rule_rewards),
            "reward_mean": sum(rule_rewards) / len(rule_rewards) if rule_rewards else 0.0,
            "note": "BaselineTriage 用同一把奖励尺子计分——训练前基线的对照锚点",
        },
    }

    n1 = write_tasks_jsonl(attr_tasks, out_dir / "tasks_attribution.jsonl")
    n2 = write_tasks_jsonl(ask_tasks, out_dir / "tasks_ask.jsonl")
    n3 = write_agl_events(episodes, out_dir / "agl_events.jsonl")
    n4 = write_trajectories(episodes, out_dir / "trajectories.jsonl")
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(f"导出：tasks_attribution={n1} tasks_ask={n2} agl_events={n3} trajectories={n4}")

    # ── 4. 回归门禁 ──
    gate = _gate(args.extended)
    report["gate"] = gate
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    text = render_markdown(report, gate)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    if args.archive:
        Path(args.archive).parent.mkdir(parents=True, exist_ok=True)
        Path(args.archive).write_text(text, encoding="utf-8")
        log(f"已归档 {args.archive}")

    if not gate["pass"]:
        print(text)
        log(f"门禁拦截：{gate['reasons']}")
        return 1
    log("门禁通过")
    return 0


def _make_policy(has_key: bool, max_turns: int):
    from findata.agent.llm import MockLLMClient, OpenAICompatClient
    from findata.rl.policy import TriageAgentPolicy

    client = OpenAICompatClient() if has_key else MockLLMClient(default=_MOCK_DEFAULT)
    return TriageAgentPolicy(client, max_turns=max_turns)


def _load_golden() -> list[dict]:
    import yaml

    return yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]


def _make_ask_runner(out_dir: Path):
    """问数 runner：临时 fixture 仓库 + supervisor 图。golden 数值与 fixture 同源。"""
    from scripts.build_eval_fixture import build as build_fixture

    from findata.core.db import connect
    from findata.rl.policy import make_ask_runner

    db = out_dir / "ask_fixture.duckdb"
    build_fixture(str(db))
    conn = connect(str(db))
    return make_ask_runner(conn), conn


def _gate(extended: bool) -> dict:
    """回归门禁：归因层价值仍在（精确率不低于朴素基线）且 rollout 无 error。"""
    from findata.eval.runner import run_evaluation

    result = run_evaluation(fault_seed=SEED0, extended=extended)
    rep = result.report
    reasons: list[str] = []
    if rep.alert_precision < rep.naive_alert_precision:
        reasons.append(
            f"alert_precision {rep.alert_precision:.1%} < 朴素基线 {rep.naive_alert_precision:.1%}"
        )
    return {
        "pass": not reasons,
        "reasons": reasons,
        "alert_precision": rep.alert_precision,
        "naive_alert_precision": rep.naive_alert_precision,
        "root_cause_accuracy": rep.root_cause_accuracy,
        "benign_suppression": rep.benign_suppression,
    }


def render_markdown(report: dict, gate: dict) -> str:
    """报告文本（examples/ 归档风格）。"""
    agg = report["aggregate"]
    lines = [
        "Findata RL 管线基线（M14 · 无 GPU 降级：环境与奖励封装）",
        "=" * 58,
        f"生成时间: {report['generated_at']}  ·  backend: {report['backend']}"
        f"  ·  seeds: {report['seeds'][0]}..{report['seeds'][-1]}"
        f"  ·  extended: {report['extended']}",
        "",
        f"任务 A（归因，多轮窄 agent）   n={agg['by_data_source'].get('attribution', {}).get('n', 0)}",  # noqa: E501
        f"任务 B（问数，golden 集）      n={agg['by_data_source'].get('ask', {}).get('n', 0)}"
        + ("（已跳过：未配置 LLM key）" if report.get("ask_skipped") else ""),  # noqa: E501
        "",
        f"规则基线 reward（同尺子）      {report['rule_baseline']['reward_mean']:.3f}"
        f"  (n={report['rule_baseline']['n']})",
        f"rollout reward 均值            {agg['reward_mean']:.3f}",
        f"rollout reward 分布            min={agg['reward_min']:.3f} max={agg['reward_max']:.3f}",
        f"error episodes                 {agg['n_errors']}",
    ]
    for source, g in agg.get("by_data_source", {}).items():
        bd = g.get("breakdown_mean", {})
        if source == "attribution":
            lines += [
                "",
                "任务 A 分项（均值）",
                f"  root_cause={bd.get('root_cause', 0):.3f}  suppress={bd.get('suppress', 0):.3f}"
                f"  format={bd.get('format', 0):.3f}  提交率={bd.get('submitted', 0):.0%}",
            ]
        else:
            lines += [
                "",
                "任务 B 分项（均值）",
                f"  values={bd.get('values', 0):.3f}  run_id_kept={bd.get('run_id_kept', 0):.3f}"
                f"  format={bd.get('format', 0):.3f}  tool_rounds={bd.get('tool_rounds', 0):.1f}",
            ]
    if report.get("ask_skipped"):
        lines += ["", f"注：{report['ask_skipped']}"]
    lines += [
        "",
        "回归门禁",
        f"  alert_precision   {gate['alert_precision']:.1%}（朴素基线 {gate['naive_alert_precision']:.1%}）",  # noqa: E501
        f"  root_cause        {gate['root_cause_accuracy']:.1%}",
        f"  benign_suppress   {gate['benign_suppression']:.1%}",
        f"  结果              {'通过' if gate['pass'] else '拦截: ' + '; '.join(gate['reasons'])}",
        "",
        "无 GPU 降级边界：本报告覆盖「环境 → 奖励 → 轨迹 → 训练数据」数据侧闭环；",
        "权重更新（Agent Lightning + verl + GRPO）待有卡执行，见 docs/rl-experiment.md。",
    ]
    return "\n".join(lines) + "\n"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
