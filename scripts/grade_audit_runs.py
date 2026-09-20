"""事后重判落盘的 runs：用**分档判据**替代单一的关键词命中。

为什么需要它
------------
`run_context_audit.py` 只用 `tasks.is_hit`（关键词命中）当质量标签。跑全量时
看到它太弱：7B 的输出是长篇中文散文，答对了也可能没用那个词；反过来，
**一次工具都没调却"答对"**的（拿先验蒙的）它判不出来。

但这一步**不需要重跑**：`runs-<tag>.jsonl` 存了 `final_answer` 原文，
所以更强的判据可以**事后**套在同一批答案上，两臂可比性不受影响。

判据分三档，**分开报，不给单一"准确率"**
--------------------------------------
沿用 v3.0 的处置纪律：不要把一个混合体报成一个数。

1. `hit`           —— 关键词命中（弱判据，沿用 `tasks.is_hit`，保持可比）
2. `process_ok`    —— 过程判据：**确实查了仓库**且正常收尾
                      （`turns > 0` 且至少一次工具调用，且不是 error/timeout）
3. `verified_hit`  —— `hit` AND `process_ok`。**这才是能对外说的那个数**

另外单列一个**可疑档**：`hit` 但 `not process_ok` —— 关键词对了但没查仓库。
它既不是"答对"也不是"答错"，是"**可能蒙对**"。v3.0 的教训是别把它塞进
任一档充数；这里单独计数并点名任务号。

诚实说明
--------
- `process_ok` 是**过程**判据，不是"答对"。查了仓库也可能答错。
- 本脚本产出的 `verified_hit` **仍然建立在关键词判据之上**，只是把明显没查
  证据的那些剔掉了。要真正提升质量侧结论，仍需按 `docs/r5.0-acceptance.md`
  §4.5 的方向把任务判据改成"精确值命中"。
- 本机 7B 模型：质量侧结论只在本地口径下成立。

用法
----
    uv run python scripts/grade_audit_runs.py                    # 两臂都判
    uv run python scripts/grade_audit_runs.py --arms all7b       # 只判一臂
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from findata.contextbudget.tasks import TASKS, is_hit  # noqa: E402

DEFAULT_DIR = Path("examples/context-audit")
# 正常收尾。其余（error / max_turns / …）都算过程不成立。
STOPPED_OK = "answered"

# 工具调用没落好的结果码。用于单独统计"这一臂的工具调用质量"。
BAD_CODES = frozenset({"error", "not_found", "not_implemented"})


@dataclass(frozen=True)
class Grade:
    task_id: str
    task: str
    hit: bool
    process_ok: bool
    n_calls: int
    stopped_reason: str

    @property
    def verified_hit(self) -> bool:
        return self.hit and self.process_ok

    @property
    def suspicious_hit(self) -> bool:
        """关键词对了但没查仓库 —— 可能蒙对。单独一档，不并入任何一边。"""
        return self.hit and not self.process_ok


def load_runs(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _turns(run: dict[str, Any]) -> list[dict[str, Any]]:
    turns = run.get("turns") or []
    return turns if isinstance(turns, list) else []


def n_tool_calls(run: dict[str, Any]) -> int:
    return sum(len(t.get("tool_calls") or []) for t in _turns(run))


def tool_result_codes(run: dict[str, Any]) -> list[str]:
    """工具调用结果码。runner 把每个 tool_call 的 result_code 记在 turn 里。"""
    codes: list[str] = []
    for t in _turns(run):
        for call in t.get("tool_calls") or []:
            code = call.get("result_code")
            if code:
                codes.append(str(code))
    return codes


def parse_errors(run: dict[str, Any]) -> list[str]:
    """模型产出**语法就不对**的工具调用（`parse_error` 非空）。

    这一类必须与 `result_code=error` 分开数：后者是工具执行后报错
    （可能是参数名写错、路径不存在），前者是**模型连 JSON 都没吐对**。
    混在一起会把"模型不会用工具"和"工具没找到东西"算成同一件事。
    """
    out: list[str] = []
    for t in _turns(run):
        for call in t.get("tool_calls") or []:
            pe = call.get("parse_error")
            if pe:
                out.append(str(pe))
    return out


def grade_arm(runs: list[dict[str, Any]]) -> list[Grade]:
    by_question = {t.question: t for t in TASKS}
    grades: list[Grade] = []
    for run in runs:
        task = by_question.get(run.get("task", ""))
        if task is None:
            continue
        answer = run.get("final_answer") or ""
        calls = n_tool_calls(run)
        stopped = str(run.get("stopped_reason") or "")
        # 过程判据：查了仓库 + 正常收尾。
        # 「一次工具都没调」在"回答关于本仓库的问题"这类任务上是**结构性**失败：
        # 没有证据来源，答对也是蒙的。
        process_ok = calls > 0 and stopped == STOPPED_OK
        grades.append(
            Grade(
                task_id=task.id,
                task=task.question,
                hit=bool(is_hit(task, answer)),
                process_ok=process_ok,
                n_calls=calls,
                stopped_reason=stopped,
            )
        )
    return grades


def summarize(tag: str, runs: list[dict[str, Any]], grades: list[Grade]) -> dict[str, Any]:
    n = len(grades)
    hits = [g for g in grades if g.hit]
    verified = [g for g in grades if g.verified_hit]
    suspicious = [g for g in grades if g.suspicious_hit]
    no_call = [g for g in grades if g.n_calls == 0]
    codes: list[str] = []
    parse_bad: list[str] = []
    for run in runs:
        codes.extend(tool_result_codes(run))
        parse_bad.extend(parse_errors(run))
    bad = [c for c in codes if c in BAD_CODES]
    n_process_ok = sum(1 for g in grades if g.process_ok)

    print(f"\n=== 臂 {tag}  n={n} ===")
    print(f"  ① hit（关键词命中，弱）       : {len(hits):>3} / {n}  ({len(hits) / n:.1%})")
    print(f"  ② process_ok（查了仓库+正常收尾）: {n_process_ok:>3} / {n}")
    print(
        f"  ③ verified_hit（①且②，可对外说）: {len(verified):>3} / {n}"
        f"  ({len(verified) / n:.1%})"
    )
    print(
        f"  ⚠ 可疑档（hit 但没查仓库）    : {len(suspicious):>3}"
        + (f"  任务 {[g.task_id for g in suspicious]}" if suspicious else "")
    )
    print(
        f"  · 零工具调用                  : {len(no_call):>3}"
        f"  任务 {[g.task_id for g in no_call]}"
    )
    print(
        f"  · 工具调用结果码              : 共 {len(codes)} 次，"
        f"其中异常码 {len(bad)} 次" + (f" {sorted(set(bad))}" if bad else "")
    )
    print(
        f"  · 工具调用**解析失败**        : {len(parse_bad)} 次"
        "（模型没吐对 JSON/参数，与工具报错不是一回事）"
    )
    return {
        "tag": tag,
        "n": n,
        "hit": len(hits),
        "process_ok": n_process_ok,
        "verified_hit": len(verified),
        "suspicious_hit": len(suspicious),
        "zero_tool_calls": len(no_call),
        "tool_calls_total": len(codes),
        "tool_codes_bad": len(bad),
        "tool_parse_errors": len(parse_bad),
        "suspicious_task_ids": [g.task_id for g in suspicious],
        "zero_call_task_ids": [g.task_id for g in no_call],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="事后重判落盘的 runs（分档判据）")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--arms", nargs="*", default=["all7b", "active7b"])
    ap.add_argument("--out", default=None, help="汇总落盘路径（.json）")
    args = ap.parse_args()

    base = Path(args.dir)
    print("=" * 78)
    print("事后重判：分档判据（不重跑，只重判落盘的 final_answer）")
    print("=" * 78)

    summaries: list[dict[str, Any]] = []
    graded: dict[str, dict[str, Grade]] = {}
    for tag in args.arms:
        path = base / f"runs-{tag}.jsonl"
        if not path.exists():
            print(f"\n[跳过] 找不到 {path}")
            continue
        runs = load_runs(path)
        gs = grade_arm(runs)
        summaries.append(summarize(tag, runs, gs))
        graded[tag] = {g.task_id: g for g in gs}

    if len(graded) == 2:
        a, b = args.arms[0], args.arms[1]
        shared = sorted(set(graded[a]) & set(graded[b]))
        print(f"\n--- 配对对照 (A={a} vs B={b})，共 {len(shared)} 个任务 ---")
        for label, attr in (("hit（弱）", "hit"), ("verified_hit（可对外说）", "verified_hit")):
            ga, gb = graded[a], graded[b]
            only_a = sum(1 for t in shared if getattr(ga[t], attr) and not getattr(gb[t], attr))
            only_b = sum(1 for t in shared if getattr(gb[t], attr) and not getattr(ga[t], attr))
            both = sum(1 for t in shared if getattr(ga[t], attr) and getattr(gb[t], attr))
            print(f"  {label:<26} 都中={both:<3} 仅A={only_a:<3} 仅B={only_b:<3}")
            if only_a + only_b:
                from math import comb

                n = only_a + only_b
                k = min(only_a, only_b)
                p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)
                print(f"    McNemar 精确检验 p = {p:.4g}  （不一致对数 n={n}）")
                if p >= 0.05:
                    # 这条必须显式打出来：小样本下"看着差很多"经常不显著
                    print("    → **不显著**：样本量不足以支撑差异结论，只能当方向性观察")
            else:
                print("    两臂无分歧 → 无法检验（'不显著'的一种，**不是'等效'**）")

    print("\n--- 诚实说明 ---")
    print("  · process_ok 是**过程**判据，不是'答对'：查了仓库也可能答错")
    print("  · verified_hit 仍建立在关键词判据之上，只是剔掉了明显没查证据的")
    print("  · 要真正提升质量侧结论，需按 docs/r5.0-acceptance.md §4.5 把任务判据")
    print("    改成'精确值命中'，此脚本是过渡，不是终点")
    print("  · 本机 7B 口径：质量侧结论只在本地模型上成立")
    print("  · 在判据换掉之前，材料里不得出现'裁剪后成功率不降'")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "arms": summaries,
                    "judge_definition": {
                        "hit": "tasks.is_hit（关键词命中，弱）",
                        "process_ok": "n_tool_calls>0 且 stopped_reason=='answered'",
                        "verified_hit": "hit AND process_ok（可对外说）",
                        "suspicious_hit": "hit AND NOT process_ok（可能蒙对，单独计数）",
                    },
                    "note": "事后重判，未重跑；两臂可比性不受影响",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
