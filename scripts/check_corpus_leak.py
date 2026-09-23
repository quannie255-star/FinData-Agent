#!/usr/bin/env python
"""语料泄漏检查：**一次实验的语料里，答案卡在不在；模型有没有真的看到它。**

为什么需要这个脚本（不是"防御性编码"，是实测事故的产物）
--------------------------------------------------------
R5.0 的语料根是本仓库的冻结快照，而 `src/findata/contextbudget/tasks.py`
**同时写着 32 道题和每题的 `must_contain` 判据**。于是那批实验等于
**把答案卡放在考场里**。后果被量化过：

  34 条真实 `search_code` 查询，在含答案卡的语料上有 **14 条**返回更大，
  每条多出约 +4,667 字符，多出来的正是 `tasks.py`。
  最极端的一条：查「五档处置」
    含答案卡 → 4,718 字符（连带返回那道题的 `must_contain`）
    无答案卡 → 22 字符（"No matches"）

而这份泄漏**偏向短清单臂**：`active7b-fz` 有 5 个任务引用了考题定义
（2 个本会被判"命中"），`all7b-fz` 只有 2 个、0 个假命中。
⇒ "短清单质量不降反升"这条结论，在无泄漏语料上重算后**不成立**。

**所以这不是一个卫生检查，它是一个会改变结论的闸门。**

三步，全部可复现
----------------
1. **路径判据**：受保护路径在不在（便宜、精确）。
2. **内容判据**：按内容找，与文件名无关（抓改名的副本；实测抓到了路径判据
   漏掉的 2 个文件）。
3. **重放判据**（可选，`--replay-queries-from`）：拿真实 trace 里的查询
   在两个语料上各跑一次，比返回大小、看有没有夹带答案卡。前两步说
   "答案卡在不在"，只有这一步说"**模型有没有真的看到它**"。

退出码
------
0 = 两步/三步都没发现泄漏（**只能说"按现有指纹没找到"**）
3 = 发现答案卡（受保护路径或内容命中）—— 供 CI / 门禁用
2 = 参数或输入有误（语料根不存在、trace 读不到）

诚实边界
--------
- 判据 2 只认得 `grading.LEAK_FINGERPRINTS` 里那几个形状。**没报 ≠ 干净**；
  要下的结论只能是"按现有指纹在 N 个文件里没找到"。
- 判据 3 用的查询**来自别的一批 trace**（通常是泄漏那批）。它回答的是
  "**这批查询**会夹带什么"，不是"任意查询会夹带什么"。所以它是**下界的下界**。
- 两个语料根的差异必须由指纹说明白，不能只说"我把文件删了"。

用法
----
    # 只查语料（两步）
    uv run python scripts/check_corpus_leak.py --corpus-root <语料根>

    # 加上重放：拿某批 trace 的真实查询，比两个语料上的返回
    uv run python scripts/check_corpus_leak.py --corpus-root <含卡语料> \
        --replay-queries-from examples/context-audit/runs-active7b-fz.jsonl \
        --compare-root <无卡语料>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from findata.contextbudget.corpus import corpus_manifest  # noqa: E402
from findata.contextbudget.grading import LEAK_FINGERPRINTS  # noqa: E402
from findata.contextbudget.leakcheck import (  # noqa: E402
    EXAM_CARD_RELPATHS,
    check_exam_card,
)

EXIT_CLEAN = 0
EXIT_LEAK_FOUND = 3
EXIT_BAD_INPUT = 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="语料泄漏检查：答案卡在不在、模型有没有看到")
    p.add_argument("--corpus-root", required=True, help="待检查的语料根")
    p.add_argument(
        "--replay-queries-from",
        default="",
        help="从这批 trace 的 runs-*.jsonl 里取真实 search_code 查询来重放（可选）",
    )
    p.add_argument(
        "--compare-root",
        default="",
        help="重放时的对照语料根（通常是已移出答案卡的那个）；不给则只报绝对量",
    )
    p.add_argument("--max-queries", type=int, default=200, help="重放查询数上限")
    return p.parse_args()


def collect_queries(trace: Path, *, limit: int) -> list[str]:
    """从落盘 trace 里取**真实用过的** search_code 查询（去重、保序）。

    用真实查询而不是我编的查询：编出来的查询会不自觉地避开或撞上答案卡，
    两边都不是证据。这是"重放"这个词在这里的全部意义。
    """
    seen: dict[str, None] = {}
    with trace.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            for turn in json.loads(line).get("turns") or []:
                for call in turn.get("tool_calls") or []:
                    if call.get("name") != "search_code":
                        continue
                    q = (call.get("arguments") or {}).get("query")
                    if q:
                        seen.setdefault(str(q), None)
    return list(seen)[:limit]


def replay(queries: list[str], *, target: str, control: str) -> dict[str, Any]:
    """同一查询分别在 target / control 上各跑一次。

    工具是**确定性**的（同一语料同一查询 ⇒ 同一返回），所以这个对比不引入采样噪声；
    两边的差异只可能来自语料。
    """
    from findata.contextbudget.tools import search_code, set_corpus_root

    rows: list[dict[str, Any]] = []
    for q in queries:
        set_corpus_root(target)
        a = search_code(q)
        leaked_in_a = [p.pattern for p in LEAK_FINGERPRINTS if p.search(a.content)]
        row: dict[str, Any] = {
            "query": q,
            "target_chars": a.payload_chars,
            "target_result_code": a.result_code,
            "target_leak_fingerprints": leaked_in_a,
        }
        if control:
            set_corpus_root(control)
            b = search_code(q)
            row["control_chars"] = b.payload_chars
            row["delta_chars"] = a.payload_chars - b.payload_chars
        rows.append(row)
    set_corpus_root(None)  # 复位：别把这个进程的全局状态留给调用方
    return {"n_queries": len(rows), "rows": rows}


def summarize_replay(rep: dict[str, Any]) -> dict[str, Any]:
    rows = rep["rows"]
    leaked = [r for r in rows if r["target_leak_fingerprints"]]
    out: dict[str, Any] = {
        "n_queries": len(rows),
        "n_queries_returning_leak_fingerprints": len(leaked),
        "leaked_queries": [r["query"] for r in leaked],
    }
    deltas = [r["delta_chars"] for r in rows if "delta_chars" in r]
    if deltas:
        bigger = [d for d in deltas if d > 0]
        out.update({
            "n_queries_compared": len(deltas),
            "n_queries_larger_on_target": len(bigger),
            "total_extra_chars_on_target": sum(bigger),
            "max_extra_chars_on_target": max(bigger) if bigger else 0,
        })
    return out


def main() -> int:
    args = parse_args()
    base = Path(args.corpus_root)
    if not base.is_dir():
        print(f"语料根不存在：{base}")
        return EXIT_BAD_INPUT

    print("=" * 78)
    print("语料泄漏检查：答案卡在不在、模型有没有真的看到")
    print("=" * 78)

    man = corpus_manifest(base)
    print(f"语料根   : {base}")
    print(f"语料指纹 : {man['sha256'][:16]}  {man['n_files']} 文件  {man['chars']:,} 字符")

    chk = check_exam_card(base)
    print("\n--- 判据 1：受保护路径（声明式清单）---")
    for rel in chk.present:
        print(f"  ✗ 在   : {rel}")
    for rel in chk.absent:
        print(f"  · 不在 : {rel}")

    print("\n--- 判据 2：内容指纹（与文件名无关）---")
    if not chk.content_hits:
        print(f"  未命中（扫了 {chk.n_files_scanned} 个文件，跳过 {chk.n_files_skipped} 个）")
        print(f"  ⚠ 注意：只扫到「这些形状」—— {len(EXAM_CARD_RELPATHS)} 个声明路径 + ")
        print(f"    {len(LEAK_FINGERPRINTS)} 个指纹。**未命中不等于干净。**")
    else:
        for rel, pats in sorted(chk.content_hits.items()):
            print(f"  ✗ {rel}")
            print(f"      命中指纹: {pats}")
        print(f"  （扫了 {chk.n_files_scanned} 个文件，跳过 {chk.n_files_skipped} 个）")

    replay_summary: dict[str, Any] | None = None
    if args.replay_queries_from:
        trace = Path(args.replay_queries_from)
        if not trace.is_file():
            print(f"\ntrace 读不到：{trace}")
            return EXIT_BAD_INPUT
        queries = collect_queries(trace, limit=args.max_queries)
        print(f"\n--- 判据 3：重放真实查询（{len(queries)} 条，来自 {trace.name}）---")
        rep = replay(queries, target=str(base), control=args.compare_root)
        replay_summary = summarize_replay(rep)
        n_hit = replay_summary["n_queries_returning_leak_fingerprints"]
        print(f"  返回里带答案卡指纹的查询: {n_hit} / {replay_summary['n_queries']}")
        if "n_queries_compared" in replay_summary:
            print(f"  与对照语料比返回大小（{replay_summary['n_queries_compared']} 条可比）:")
            print(f"    更 大 的查询数 : {replay_summary['n_queries_larger_on_target']}")
            print(f"    多出的总字符  : {replay_summary['total_extra_chars_on_target']:,}")
            print(f"    单条最多多出  : {replay_summary['max_extra_chars_on_target']:,}")
        worst = sorted(rep["rows"], key=lambda r: -r["target_chars"])[:5]
        print("  返回最大的 5 条（看夹带规模）:")
        for r in worst:
            d = f"{r['delta_chars']:+,}" if "delta_chars" in r else "—"
            mark = "✗夹带" if r["target_leak_fingerprints"] else "  "
            print(f"    {mark} {r['target_chars']:>7,} 字符 (差 {d:>8s})  {r['query'][:38]}")

    print("\n--- 结论 ---")
    card = chk.has_card
    if card:
        print("  ✗ 语料里有答案卡 —— 这一批的**命中数已被高估**，对外报数前必须")
        print("    ① 把受保护路径移出语料重建快照（记新指纹），或")
        print("    ② 用 grading.has_exam_leak 把引用考题定义的答案单列剔除。")
    elif card is None:
        print("  ? 无法核对（语料根不存在）—— 未知不等于没有")
    else:
        print("  ✓ 按现有判据没找到答案卡。**这是「没找到」，不是「保证没有」。**")

    if replay_summary is not None:
        print("\n--- 重放的边界 ---")
        print(f"  · 这 {replay_summary['n_queries']} 条查询**来自另一批 trace**，")
        print("    所以它是「这批查询会夹带什么」的下界，不是「任意查询会夹带什么」。")
        print("  · 工具是确定性的，所以这里的差异不含采样噪声，只含语料差异。")

    return EXIT_LEAK_FOUND if card else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
