"""事后重判落盘的 runs：用**分档判据**替代单一的关键词命中。

为什么需要它
------------
`run_context_audit.py` 只用 `tasks.is_hit`（关键词命中）当质量标签。跑全量时
看到它太弱：7B 的输出是长篇中文散文，答对了也可能没用那个词；反过来，
**一次工具都没调却"答对"**的（拿先验蒙的）它判不出来。

但这一步**不需要重跑**：`runs-<tag>.jsonl` 存了 `final_answer` 原文，
所以更强的判据可以**事后**套在同一批答案上，两臂可比性不受影响。

判据分四档，**分开报，不给单一"准确率"**
--------------------------------------
沿用 v3.0 的处置纪律：不要把一个混合体报成一个数。

1. `hit`           —— 关键词命中（弱判据，沿用 `tasks.is_hit`，保持可比）
2. `process_ok`    —— 过程判据：**确实查了仓库**且正常收尾
                      （`turns > 0` 且至少一次工具调用，且不是 error/timeout）
3. `verified_hit`  —— `hit` AND `process_ok`（弱判据 + 过程）
4. `exact_hit`     —— **精确值判据**（`contextbudget.grading`）：每条必需事实
                      都要出现，每条事实有别名组；且过程成立。
                      **这是目前唯一能支撑质量侧结论的档**，见 §4.5。

另外单列两个**排除档**，都不并入任何一边：

- **可疑档**：`hit` 但 `not process_ok` —— 关键词对了但没查仓库，**可能蒙对**。
- **判据泄漏档**：答案引用了**考题定义本身**（`grading.has_exam_leak`）。
  语料根就是本仓库的冻结快照，而 `src/findata/contextbudget/tasks.py`
  里同时写着 32 道题**和**每题的 `must_contain` —— 模型读它等于拿到答案卡。
  实测 active7b 臂 t23 整段在讲 `tasks.py`（跑题），却引用了
  `Task("t23", "…推导…", ("原文",), …)`，问题和判据一起被抄进答案。
  **这是语料缺陷，不是判据缺陷**，所以单列剔除并报数，不静默合并。

诚实说明
--------
- 四档都不是"答对"。`process_ok` 是过程判据；`exact_hit` 是**事实命中**判据
  ——它仍判不出"推理过程是否成立"。
- `exact_hit` 的偏差**两个方向都有**，不是只有漏判：
  ① 漏判——别名组手写，答对但写法没列出；
  ② 误判——别名组里没有判别力的宽泛词（依赖 / 防线 / 规则 / 通用）
     会被跑题或拒答的答案蹭中（反例：t22 的拒答句命中"防误报"）。
  所以 `exact_hit` 只能读成"必需事实出现过"，**不能**读成"答对了"。
- 本机 7B / 3B 模型：质量侧结论只在本地口径下成立。

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

from findata.contextbudget.grading import judge as judge_answer  # noqa: E402
from findata.contextbudget.grading import mcnemar_exact  # noqa: E402
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
    exact: bool = False
    failed_by: str = ""
    missing: tuple[str, ...] = ()
    leaked: bool = False

    @property
    def hit_clean(self) -> bool:
        """弱判据，但剔掉泄漏答案。历史读数仍用原始 `hit`，可比性另算。"""
        return self.hit and not self.leaked

    @property
    def verified_hit(self) -> bool:
        # 泄漏答案一律不算：它"命中的是判据本身"，不是证据。
        return self.hit and self.process_ok and not self.leaked

    @property
    def suspicious_hit(self) -> bool:
        """关键词对了但没查仓库 —— 可能蒙对。单独一档，不并入任何一边。"""
        return self.hit and not self.process_ok and not self.leaked


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
        verdict = judge_answer(task, answer, n_tool_calls=calls, stopped_reason=stopped)
        grades.append(
            Grade(
                task_id=task.id,
                task=task.question,
                hit=bool(is_hit(task, answer)),
                process_ok=process_ok,
                n_calls=calls,
                stopped_reason=stopped,
                exact=verdict.strong,
                failed_by=verdict.failed_by,
                missing=verdict.missing,
                leaked=verdict.leaked,
            )
        )
    return grades


def summarize(tag: str, runs: list[dict[str, Any]], grades: list[Grade]) -> dict[str, Any]:
    n = len(grades)
    hits = [g for g in grades if g.hit]  # 原始弱判据：保留以便与历史读数可比
    hits_clean = [g for g in grades if g.hit_clean]
    leaked = [g for g in grades if g.leaked]
    leaked_hits = [g for g in grades if g.leaked and g.hit]
    verified = [g for g in grades if g.verified_hit]
    suspicious = [g for g in grades if g.suspicious_hit]
    exact = [g for g in grades if g.exact]
    no_call = [g for g in grades if g.n_calls == 0]
    codes: list[str] = []
    parse_bad: list[str] = []
    for run in runs:
        codes.extend(tool_result_codes(run))
        parse_bad.extend(parse_errors(run))
    bad = [c for c in codes if c in BAD_CODES]
    n_process_ok = sum(1 for g in grades if g.process_ok)

    def _pct(k: int) -> str:
        return f"{k / n:.1%}" if n else "n/a"

    print(f"\n=== 臂 {tag}  n={n} ===")
    print(f"  ① hit∈原始（关键词，弱，含泄漏）: {len(hits):>3} / {n}  ({_pct(len(hits))})")
    print(
        f"  ①' hit（剔除泄漏后，**以此为准**）: {len(hits_clean):>3} / {n}"
        f"  ({_pct(len(hits_clean))})"
    )
    print(f"  ② process_ok（查了仓库+正常收尾）: {n_process_ok:>3} / {n}")
    print(
        f"  ③ verified_hit（①'且②）       : {len(verified):>3} / {n}  ({_pct(len(verified))})"
    )
    print(
        f"  ④ exact_hit（精确值，**以此为准**）: {len(exact):>3} / {n}  ({_pct(len(exact))})"
    )
    if leaked:
        print(
            f"  ⚠ **判据泄漏**（答案引用了考题定义）: {len(leaked):>3}"
            f"  任务 {[g.task_id for g in leaked]}"
        )
        print(
            f"      其中本来会被判「命中」的: {len(leaked_hits)}"
            f" {[g.task_id for g in leaked_hits]}"
            "  ⇒ 这些是**假命中**，已从①②③④全部剔除"
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
    if exact_but_not_weak := [g.task_id for g in grades if g.exact and not g.hit]:
        print(f"  ↑ 精确值过、关键词没过（别名组起作用的证据）: {exact_but_not_weak}")
    return {
        "tag": tag,
        "n": n,
        "hit_raw": len(hits),
        "hit": len(hits_clean),
        "process_ok": n_process_ok,
        "verified_hit": len(verified),
        "exact_hit": len(exact),
        "suspicious_hit": len(suspicious),
        "exam_leak": len(leaked),
        "exam_leak_that_would_hit": len(leaked_hits),
        "zero_tool_calls": len(no_call),
        "tool_calls_total": len(codes),
        "tool_codes_bad": len(bad),
        "tool_parse_errors": len(parse_bad),
        "suspicious_task_ids": [g.task_id for g in suspicious],
        "exam_leak_task_ids": [g.task_id for g in leaked],
        "zero_call_task_ids": [g.task_id for g in no_call],
        "exact_task_ids": [g.task_id for g in exact],
        "exact_but_not_keyword_task_ids": [
            g.task_id for g in grades if g.exact and not g.hit
        ],
        "keyword_but_not_exact": [
            {"task_id": g.task_id, "missing": list(g.missing)}
            for g in grades
            if g.hit_clean and not g.exact
        ],
        "leaked_but_keyword_hit": [
            {"task_id": g.task_id, "would_have_been": "hit / exact"}
            for g in grades
            if g.leaked and g.hit
        ],
    }


def corpus_audit(base: Path, tag: str) -> dict[str, Any]:
    """每个臂的**口径身份证**：语料根、指纹、裁剪策略、以及"答案卡在不在考场里"。

    最后这项是**算出来的，不是声明的**。2026-09-22 实测发现：语料根就是本仓库的
    冻结快照，而 `src/findata/contextbudget/tasks.py` 同时写着 32 道题和每题的
    `must_contain` —— 模型读到它等于拿到答案卡（active7b 臂 5 条答案引用考题定义，
    其中 2 条本来被判成"命中"）。

    这个检查之所以要**读磁盘**而不是靠 tag 命名约定：约定是自律，
    磁盘是证据。`-fx` 批次指向去泄漏快照这件事，必须能被任何人独立复核。
    """
    info: dict[str, Any] = {"tag": tag, "bill_found": False}
    bill_path = base / f"bill-{tag}.json"
    if bill_path.exists():
        try:
            bill = json.loads(bill_path.read_text(encoding="utf-8"))
            rep = bill.get("reproducibility") or {}
            scope = bill.get("scope") or {}
            info.update(
                bill_found=True,
                corpus_root=rep.get("corpus_root", ""),
                corpus_sha256=(rep.get("corpus") or {}).get("sha256", ""),
                corpus_n_files=(rep.get("corpus") or {}).get("n_files", 0),
                compact_policy=scope.get("compact_policy", "未记录"),
                compact_caps=scope.get("compact_caps") or {},
                compact_profile_sha256=rep.get("compact_profile_sha256", ""),
                n_tools_in_list=scope.get("n_tools_in_list", 0),
            )
        except (OSError, json.JSONDecodeError) as exc:
            info["bill_error"] = f"{type(exc).__name__}: {exc}"

    root = info.get("corpus_root") or ""
    if root:
        exam_card = Path(root) / "src" / "findata" / "contextbudget" / "tasks.py"
        info["exam_card_in_corpus"] = exam_card.exists()
        info["exam_card_path"] = str(exam_card)
    else:
        info["exam_card_in_corpus"] = None  # 未知 ≠ 没有
    return info


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
    corpora: dict[str, dict[str, Any]] = {}
    for tag in args.arms:
        path = base / f"runs-{tag}.jsonl"
        if not path.exists():
            print(f"\n[跳过] 找不到 {path}")
            continue
        runs = load_runs(path)
        gs = grade_arm(runs)
        summaries.append(summarize(tag, runs, gs))
        graded[tag] = {g.task_id: g for g in gs}
        corpora[tag] = corpus_audit(base, tag)

    print("\n--- 各臂口径（可复现身份，读磁盘算出来的）---")
    for tag, info in corpora.items():
        card = info.get("exam_card_in_corpus")
        card_txt = {True: "**在**（答案卡在考场里）", False: "已移出", None: "未知"}[card]
        print(
            f"  {tag:<18} 语料={info.get('corpus_sha256', '未记录')[:16] or '未记录':<16}"
            f" 文件={info.get('corpus_n_files', '?')}"
            f" 裁剪={info.get('compact_policy', '?')}"
            f" 答案卡={card_txt}"
        )
    roots = {i.get("corpus_sha256") for i in corpora.values() if i.get("corpus_sha256")}
    if len(roots) > 1:
        # 两臂语料不同 ⇒ 读数不可直接配对。这是**硬闸门**，不是提示。
        print("  ⚠ **两臂语料指纹不同**：本对照的'唯一变量'不成立，读之前先解释清楚")
    cards = {i.get("exam_card_in_corpus") for i in corpora.values()}
    if True in cards:
        print("  ⚠ **有臂的语料里带着答案卡**：该臂的命中数已被高估，本批剔除泄漏后再读")

    paired: dict[str, Any] = {}
    if len(graded) == 2:
        a, b = args.arms[0], args.arms[1]
        shared = sorted(set(graded[a]) & set(graded[b]))
        print(f"\n--- 配对对照 (A={a} vs B={b})，共 {len(shared)} 个任务 ---")
        for label, attr in (
            ("hit（弱·剔泄漏）", "hit_clean"),
            ("verified_hit（弱+过程）", "verified_hit"),
            ("exact_hit（精确值，为准）", "exact"),
        ):
            ga, gb = graded[a], graded[b]
            only_a = sum(1 for t in shared if getattr(ga[t], attr) and not getattr(gb[t], attr))
            only_b = sum(1 for t in shared if getattr(gb[t], attr) and not getattr(ga[t], attr))
            both = sum(1 for t in shared if getattr(ga[t], attr) and getattr(gb[t], attr))
            k = only_a + only_b  # 不一致对：配对检验**全部**的信息量都在这里
            print(
                f"  {label:<26} 都中={both:<3} 仅A={only_a:<3} 仅B={only_b:<3}"
                f"   不一致对 k={k}"
            )
            rec: dict[str, Any] = {
                "criterion": attr,
                "both": both,
                "only_A": only_a,
                "only_B": only_b,
                "discordant_k": k,
                "min_possible_p": 2.0 ** (1 - k) if k else 1.0,
            }
            if k:
                p, p1, min_p = mcnemar_exact(only_a, only_b)
                rec["mcnemar_exact_p"] = p
                rec["mcnemar_exact_p_one_sided_B_not_worse"] = p1
                print(f"    McNemar 精确检验 p = {p:.4g}  （不一致对数 k={k}）")
                # 单侧：只有"B（裁剪臂）不差"才是本项目的方向性假设，所以要报。
                # 但**不能只报单侧**——单侧更容易过线，只报它等于偷偷降低门槛。
                print(f"    单侧（假设 B 不差）p = {p1:.4g}")
                # 分辨力：见 grading.mcnemar_exact 的 docstring。
                # 这条比"不显著"强得多："不显著"会被读成"差不多"，
                # 而真相是"本对照没有能力分辨"——两句话结论完全不同。
                if min_p >= 0.05:
                    print(f"    → 分辨力下限：k={k} 时**最小可能 p = {min_p:.4g}** ≥ 0.05")
                    print("      ⇒ 本对照在统计上**测不出**两臂差异，不是'等效'也非'更优'")
                    print("      （k≥6 才可能显著；k 由任务数与两臂分歧率共同决定）")
                elif p >= 0.05:
                    print("    → **不显著**：样本量不足以支撑差异结论，只能当方向性观察")
                if only_a == 0 and only_b > 0:
                    # only_A=0 比 p 值更有信息量：裁剪臂一个任务都没输。
                    # 但它仍是**未检验**的观察，必须连同"单侧也够不到"一起说。
                    print(
                        f"    · 方向性：A 一个都没赢（仅A=0），B 赢 {only_b} 个"
                        " —— 但单侧 p 也没到 0.05"
                    )
            else:
                print("    两臂无分歧 → 无法检验（'不显著'的一种，**不是'等效'**）")
            paired[label] = rec

    print("\n--- 诚实说明 ---")
    print("  · 四档都不是'答对'：精确值命中判的是**事实出现**，判不出推理是否成立")
    print("  · 判据泄漏：上面逐臂算过'答案卡在不在语料里'。**在**的臂，其命中数已被高估")
    print("    （语料根 = 本仓库快照时，tasks.py 同时写着题目与 must_contain）")
    print("    ⇒ 已把该文件移出语料的批次（`-fx`）带的是**自己的语料指纹**，")
    print("      所以 `-fx` 与 `-fz` 的读数**不可混用**，只能各自报、各自注明指纹")
    print("  · exact_hit 的别名组手写且含宽泛词（依赖/防线/规则/通用）")
    print("    ⇒ 两个方向的偏差都有：漏判（写法没列出）+ 被跑题答案蹭中")
    print("    ⇒ 所以 exact_hit 只能读成'必需事实出现过'，不能读成'答对了'")
    print("  · 本机 7B/3B 口径：质量侧结论只在本地模型上成立")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "arms": summaries,
                    "corpora": corpora,
                    "judge_definition": {
                        "hit": "tasks.is_hit（关键词命中，弱）",
                        "process_ok": "n_tool_calls>0 且 stopped_reason=='answered'",
                        "verified_hit": "hit AND process_ok（弱判据 + 过程）",
                        "exact_hit": "必需事实全命中（别名组）AND process_ok —— 以此为准",
                        "suspicious_hit": "hit AND NOT process_ok（可能蒙对，单独计数）",
                    },
                    "note": "事后重判，未重跑；两臂可比性不受影响",
                    "paired": paired,
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
