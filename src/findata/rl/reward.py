"""RL 奖励函数（M14）。Rubric 模式：奖励是独立纯函数，与环境、策略完全解耦。

参照 verifiers（Prime Intellect）的 Rubric 设计——环境只管动力学，奖励是
一组在终态评估的加权函数。这样奖励可以单独单测、单独调权，换环境或换
策略都不用动这层。

两个任务的奖励都满足 ROADMAP 对 M14 的要求：**稠密且确定性**——
同样的提交永远得同样的分，不依赖模型裁判（那会引入奖励噪声）。

权重设计的一条原则（来自 Agentic RL 的实践共识）：**主干放任务本质**
（根因判对 / 数值正确），辅助项权重小，防止 dense 分量被 hack
（比如为了拿格式分只输出格式不管对错）。

总分归一化到 [0, 1]：加权得分减超轮惩罚后除以满分。
GRPO 只需要组内相对优势，归一化不改变排序，但让 reward 曲线可读
（0.9 vs 0.6 这种），人能直接看出训练在不在涨。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from findata.dq.models import Diagnosis, RootCause, Severity
from findata.eval.agent_judge import _MARK_RE, extract_numbers, judge_run_ids
from findata.rl.types import ASK, ATTRIBUTION


@dataclass(frozen=True)
class RewardWeights:
    """任务 A（归因）的奖励权重。"""

    root_cause: float = 1.0  # 主干：根因判对
    suppress: float = 0.3  # 抑制决策对（benign 判定正确）——告警精确率的 RL 形态
    format: float = 0.1  # 提交格式合法（受控 JSON + 白名单）
    per_extra_turn: float = 0.1  # 每超一轮的惩罚
    extra_turn_cap: float = 0.5  # 惩罚封顶：超轮该罚，不该无限罚

    @property
    def max_total(self) -> float:
        return self.root_cause + self.suppress + self.format


ATTRIBUTION_WEIGHTS = RewardWeights()


@dataclass(frozen=True)
class AskWeights:
    """任务 B（问数）的奖励权重。对应 golden 集 + 工具轮数惩罚。"""

    correct: float = 0.6  # 主干：数值正确
    run_id: float = 0.2  # run_id 溯源保留率
    format: float = 0.2  # 单位 + 交叉验证标记
    per_extra_round: float = 0.1  # 超出轮数预算后每轮惩罚
    extra_round_cap: float = 0.3
    round_budget: int = 4  # 轮数预算，对齐 MAX_TOOL_ROUNDS

    @property
    def max_total(self) -> float:
        return self.correct + self.run_id + self.format


ASK_WEIGHTS = AskWeights()


def attribution_reward(
    submitted: Diagnosis | None,
    expected_cause: RootCause,
    expected_suppressed: bool,
    extra_turns: int = 0,
    weights: RewardWeights = ATTRIBUTION_WEIGHTS,
) -> tuple[float, dict]:
    """任务 A：归因提交 vs 标注。submitted=None（超时未提交）得 0 减超轮罚。

    返回 (归一化总分, 分项明细)。明细里保留归一化前的原始加权分，
    训练后归因分析靠它定位"是根因错了还是抑制错了"。
    """
    if submitted is None:
        raw = 0.0
        penalty = min(extra_turns * weights.per_extra_turn, weights.extra_turn_cap)
        breakdown = {
            "root_cause": 0.0,
            "suppress": 0.0,
            "format": 0.0,
            "extra_turns": extra_turns,
            "penalty": penalty,
            "submitted": False,
        }
        return _normalize(raw, penalty, 0.0, weights.max_total), breakdown

    cause_ok = submitted.root_cause is expected_cause
    suppress_ok = submitted.suppressed == expected_suppressed
    raw = (
        weights.root_cause * cause_ok
        + weights.suppress * suppress_ok
        + weights.format * 1.0  # 能构造出合法 Diagnosis 即格式合格
    )
    penalty = min(extra_turns * weights.per_extra_turn, weights.extra_turn_cap)
    breakdown = {
        "root_cause": float(cause_ok),
        "suppress": float(suppress_ok),
        "format": 1.0,
        "extra_turns": extra_turns,
        "penalty": penalty,
        "submitted": True,
        "predicted_cause": submitted.root_cause.value,
    }
    return _normalize(raw, penalty, 0.0, weights.max_total), breakdown


def ask_reward(
    answer: str,
    expect: dict,
    run_ids: list[str] | None = None,
    tool_rounds: int = 0,
    weights: AskWeights = ASK_WEIGHTS,
) -> tuple[float, dict]:
    """任务 B：golden 集判定。数值正确 + run_id 保留 + 格式，超轮惩罚。

    判分逻辑来自 findata.eval.agent_judge（与 M12 agent 级评测同一把尺子，
    不允许评测与奖励漂移出两套实现）。
    """
    tol = float(expect.get("tol", 1e-3))

    numbers_wanted = expect.get("values", [])
    got = extract_numbers(answer)
    values_ok = all(any(abs(n - float(w)) <= tol for n in got) for w in numbers_wanted)

    kept = judge_run_ids(answer, run_ids or []) if expect.get("want_run_id") else 1.0
    format_ok = _ask_format_ok(answer, expect)

    raw = weights.correct * values_ok + weights.run_id * kept + weights.format * format_ok
    over = max(0, tool_rounds - weights.round_budget)
    penalty = min(over * weights.per_extra_round, weights.extra_round_cap)
    breakdown = {
        "values": float(values_ok),
        "run_id_kept": kept,
        "format": float(format_ok),
        "tool_rounds": tool_rounds,
        "penalty": penalty,
    }
    return _normalize(raw, penalty, 0.0, weights.max_total), breakdown


def _ask_format_ok(answer: str, expect: dict) -> bool:
    """格式项：单位在场 + 要求时带交叉验证标记。"""
    unit = expect.get("unit")
    unit_ok = (not unit) or (unit in answer)
    mark_ok = (not expect.get("verification_mark")) or bool(_MARK_RE.search(answer))
    return unit_ok and mark_ok


def _normalize(raw: float, penalty: float, floor: float, max_total: float) -> float:
    """归一化到 [floor, 1]：超轮惩罚把总分往下压，最低不为负——
    惩罚表达"浪费"，不值得负奖励（负奖励会诱导策略学会摆烂之外的怪行为）。
    """
    if max_total <= 0:
        return 0.0
    score = (raw - penalty) / max_total
    return max(floor, min(1.0, score))


def compute_reward(
    data_source: str,
    solution_str: str,
    ground_truth: dict,
    extra_info: dict | None = None,
) -> float:
    """verl custom_reward_function 兼容签名（data_source 分发）。

    有卡接 verl 时，把这个函数所在文件配到
    ``+custom_reward_function.path=findata/rl/reward.py
    custom_reward_function.name=compute_reward`` 即可，无需适配层。

    - attribution：solution_str = 提交 JSON（submit_attribution 的参数）
    - ask：solution_str = 最终答案文本；extra_info 需带 run_ids/tool_rounds
    """
    extra_info = extra_info or {}
    if data_source == ATTRIBUTION:
        submitted = _parse_submission(solution_str, ground_truth)
        return attribution_reward(
            submitted,
            RootCause(ground_truth["expected_cause"]),
            bool(ground_truth["expected_suppressed"]),
            extra_turns=int(extra_info.get("extra_turns", 0)),
        )[0]
    if data_source == ASK:
        return ask_reward(
            solution_str,
            ground_truth,
            run_ids=extra_info.get("run_ids"),
            tool_rounds=int(extra_info.get("tool_rounds", 0)),
        )[0]
    raise ValueError(f"未知 data_source：{data_source}（支持 {ATTRIBUTION}/{ASK}）")


def _parse_submission(solution_str: str, ground_truth: dict) -> Diagnosis | None:
    """把提交文本解析成 Diagnosis；解析失败返回 None（得 0 分，不抛异常）。

    奖励函数不抛异常是有意的：训练循环里一个坏样本不该打断整个 batch。
    """
    text = solution_str.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json", "", 1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    try:
        cause = RootCause(str(doc.get("root_cause", "")).strip())
        severity = Severity(str(doc.get("severity", "P1")).strip())
        confidence = float(doc.get("confidence", 0.5))
        explanation = str(doc.get("explanation", "")).strip()
    except (ValueError, TypeError):
        return None
    if not explanation:
        return None
    return Diagnosis(
        finding_key=str(ground_truth.get("finding_key", "")),
        root_cause=cause,
        severity=severity,
        confidence=max(0.0, min(1.0, confidence)),
        explanation=explanation,
        evidence_refs=("rl_submit",),
    )
