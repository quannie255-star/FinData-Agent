"""赛题四口径的评分：答案标准化 + Accuracy / TokenScore / FinalScore。

口径来源（docs/afac-track4.md §0.3，全部可核对）：

- 单选题、判断题以**首个**有效答案字母为准；
- 多选题字母去重并按字母序排列后与标准答案**完全匹配**，无部分分；
- ``TokenScore = max(0, min(1, (Budget − Total) / Budget))``，Budget 固定 5,000,000；
- ``FinalScore = 100 × Accuracy × (0.7 + 0.3 × TokenScore)``。

本模块纯函数、零 IO —— 数学必须可单测，不许混进调用层。
"""

from __future__ import annotations

from dataclasses import dataclass, field

TOKEN_BUDGET = 5_000_000
VALID_LETTERS = frozenset("ABCD")


def normalize_answer(raw: str, answer_format: str) -> str:
    """把模型输出 / 标准答案标准化成可比形式。

    规则即题面的平台标准化规则：过滤出 A–D 字母；mcq/tf 取首个；multi 去重 + 排序。
    无有效字母返回 ""（按题面计错，不抛异常——评分器要能处理任意垃圾输出）。
    """
    letters = [ch for ch in raw.upper() if ch in VALID_LETTERS]
    if not letters:
        return ""
    if answer_format == "multi":
        return "".join(sorted(set(letters)))
    return letters[0]


def is_correct(pred: str, gold: str, answer_format: str) -> bool:
    """逐题判分。pred/gold 都先标准化——两侧用同一把尺子，避免口径漂移。"""
    return normalize_answer(pred, answer_format) == normalize_answer(gold, answer_format) != ""


def token_score(total_tokens: int, budget: int = TOKEN_BUDGET) -> float:
    """Token 效率分：超预算归 0，不用满预算线性到 1，两端截断。"""
    if budget <= 0:
        raise ValueError("budget 必须为正")
    return max(0.0, min(1.0, (budget - total_tokens) / budget))


def final_score(accuracy: float, tscore: float) -> float:
    """官网综合分。准确率主导（0.7 权重），token 效率最多再抬 0.3。"""
    if not 0.0 <= accuracy <= 1.0:
        raise ValueError("accuracy 必须在 [0,1]")
    if not 0.0 <= tscore <= 1.0:
        raise ValueError("tscore 必须在 [0,1]")
    return 100.0 * accuracy * (0.7 + 0.3 * tscore)


@dataclass
class ScoreReport:
    """一次运行的完整读数。per_domain 用于定位弱域（题面平台支持分项统计）。"""

    total: int = 0
    correct: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    per_q: list[dict[str, object]] = field(default_factory=list)
    per_domain: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def tscore(self) -> float:
        return token_score(self.total_tokens)

    @property
    def final(self) -> float:
        return final_score(self.accuracy, self.tscore)

    def add(self, qid: str, domain: str, ok: bool, prompt: int, completion: int) -> None:
        self.total += 1
        self.correct += int(ok)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.per_q.append(
            {"qid": qid, "domain": domain, "correct": ok, "prompt_tokens": prompt,
             "completion_tokens": completion}
        )
        slot = self.per_domain.setdefault(
            domain, {"total": 0, "correct": 0, "total_tokens": 0}
        )
        slot["total"] += 1
        slot["correct"] += int(ok)
        slot["total_tokens"] += prompt + completion

    def summary_lines(self) -> list[str]:
        """报告末尾的汇总段（成本如实的落点：token 与分数一起报）。"""
        lines = [
            f"题目数 {self.total}，正确 {self.correct}，Accuracy {self.accuracy:.4f}",
            f"prompt_tokens {self.prompt_tokens}，completion_tokens {self.completion_tokens}，"
            f"total_tokens {self.total_tokens}",
            f"TokenScore {self.tscore:.4f}（预算 {TOKEN_BUDGET:,}）",
            f"FinalScore {self.final:.4f} = 100 × {self.accuracy:.4f} ×"
            f" (0.7 + 0.3 × {self.tscore:.4f})",
        ]
        if self.per_domain:
            lines.append("分域：")
            for domain, slot in sorted(self.per_domain.items()):
                acc = slot["correct"] / slot["total"] if slot["total"] else 0.0
                lines.append(
                    f"  {domain}: {slot['correct']}/{slot['total']}（{acc:.2%}），"
                    f"tokens {slot['total_tokens']:,}"
                )
        return lines
