"""对照分析用的统计与读盘工具。

**为什么不依赖 scipy**：符号检验与 McNemar 精确检验都是**二项分布尾概率**，
纯 Python 用 `math.comb` 就能算，且这样 CI 不会因为环境缺 scipy 而假绿。
（这条有代价：n 很大时会慢。本项目的 n 是 30 量级，够用。）

**为什么符号检验而不是 t 检验**：token 差值明显长尾（见 `docs/r5.0-acceptance.md`
§3.4 的标准差/极值），t 检验会**高估**显著性。符号检验只问"哪一边更贵"的次数，
不假设任何分布。

**关于读盘函数为什么也在这里**：它们只服务于对照分析，且必须与
`scripts/compare_audit_arms.py` 的计价口径**完全一致**——放在一起是为了让
"两处口径漂移"这件事没有机会发生。口径本身（`prompt_tokens` = 逐轮求和）写在
函数上，不写在调用方。
"""

from __future__ import annotations

import json
from math import comb
from pathlib import Path
from typing import Any

__all__ = [
    "call_payload_index",
    "load_runs",
    "mcnemar_exact_p",
    "prompt_tokens",
    "sign_test_p",
    "tool_calls",
]


def _binom_two_sided(k: int, n: int) -> float:
    """双侧二项尾概率（p=0.5）：`2 * P(X <= k)`，上限截到 1。

    用 `sum(comb(n, i))` 逐项加，不用浮点近似——n≤64 时整数运算精确，
    且与手算/文档里的表达式能逐位对齐（测试里就是这么钉的）。
    """
    if n <= 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def sign_test_p(n_pos: int, n_neg: int) -> float:
    """配对差值的双侧符号检验。

    `n_pos` / `n_neg` 是**非零**差值的两侧计数，相等（差为 0）的样本按惯例剔除，
    不由本函数处理——调用方剔除后再传进来，这样"剔了多少"在调用方可见。
    """
    return _binom_two_sided(min(n_pos, n_neg), n_pos + n_neg)


def mcnemar_exact_p(only_a: int, only_b: int) -> float:
    """McNemar 精确检验：只在"两臂结论不一致"的样本上比较。

    与 `sign_test_p` 是同一个二项尾概率，但**语义不同**（这里是配对二分类的
    不一致对），所以名字分开——用同一个名字会让读者以为可以互相引用。
    """
    return _binom_two_sided(min(only_a, only_b), only_a + only_b)


def load_runs(path: Path) -> dict[str, dict[str, Any]]:
    """按任务文本索引落盘的 runs。同一任务文本在两臂里是同一个 key。"""
    runs: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        run = json.loads(line)
        runs[run["task"]] = run
    return runs


def prompt_tokens(run: dict[str, Any]) -> int:
    """一次任务的 prompt token = **逐轮求和**（每轮都要重发全部上下文）。

    这不是"某一次调用的长度"——口径写在这里，避免调用方各自解释。
    """
    return sum(t["prompt_tokens"] for t in run["turns"])


def tool_calls(run: dict[str, Any]) -> int:
    return sum(len(t["tool_calls"]) for t in run["turns"])


def call_payload_index(runs: dict[str, dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    """`(任务, 工具, 参数JSON) → payload_chars` 的索引。

    **用途是语料漂移取证**：`payload_chars` 是确定性量，同一个调用组合若在两臂
    得到不同的字符数，只可能是工具读到的磁盘内容变了。索引只保留首次出现的值：
    同一组合在同臂内重复出现且值不同，本身就是另一个要单独看的异常。
    """
    index: dict[tuple[str, str, str], int] = {}
    for task, run in runs.items():
        for turn in run.get("turns", []):
            for call in turn.get("tool_calls", []):
                key = (
                    task,
                    str(call.get("name", "")),
                    json.dumps(call.get("arguments", {}), sort_keys=True, ensure_ascii=False),
                )
                index.setdefault(key, int(call.get("payload_chars", 0)))
    return index
