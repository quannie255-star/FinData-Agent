"""Agent 级判分器：答案特征判定 + run_id 溯源保留率（M14 从 scripts/eval_agent.py 下沉）。

下沉的理由与 agent/tools.py 同源：判分逻辑是 M12 评测与 M14 RL 奖励共用的
「同一把尺子」，脚本里留一份实现、rl 里再写一份，迟早漂移。
脚本层（scripts/eval_agent.py）改为 import 本模块，行为不变。
"""

from __future__ import annotations

import re

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MARK_RE = re.compile(r"[✓⚠○]")
_RUN_ID_RE = re.compile(r"run_id=([0-9a-f]+)")


def extract_numbers(text: str) -> list[float]:
    """从答案文本里抽出全部数字 token（按 token 匹配，"121.0" 不会误中 21）。"""
    return [float(m.replace(",", "")) for m in _NUM_RE.findall(text)]


def judge_answer(answer: str, expect: dict, tol: float) -> tuple[bool, list[str]]:
    """按期望特征判单个答案。返回 (是否通过, 失败原因列表)。"""
    reasons: list[str] = []
    numbers = extract_numbers(answer)

    for want in expect.get("values", []):
        if not any(abs(n - float(want)) <= tol for n in numbers):
            reasons.append(f"答案未包含期望数值 {want}（tol={tol}）")

    unit = expect.get("unit")
    if unit and unit not in answer:
        reasons.append(f"答案未携带单位 {unit}")

    if expect.get("verification_mark") and not _MARK_RE.search(answer):
        reasons.append("答案未转述交叉验证标记（✓/⚠/○）")

    if expect.get("want_run_id") and not _RUN_ID_RE.search(answer):
        reasons.append("答案未携带 run_id 溯源")

    return (not reasons, reasons)


def judge_run_ids(answer: str, produced: list[str]) -> float:
    """run_id 保留率：答案里出现且可回追到查询产出的 run_id / 查询产出的全部。"""
    if not produced:
        return 0.0
    kept = set(_RUN_ID_RE.findall(answer)) & set(produced)
    return len(kept) / len(set(produced))
