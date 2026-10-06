"""M5 配对比较：两次运行同题对齐，报告不一致题与 token 差。

口径纪律（docs/afac-track4.md §4.0 / v4.0 遗产）：
- 配对 = 同一批题、同一份语料、同一份代码，唯一变量是被测的那一维
  （这里通常是 k_option 或运行次序）；
- 不一致对为 0 时只能说「未观察到差异」，**不许说「等价」或「证明无差」**
  ——分辨力下限的诚实表述；不为 0 时报方向与逐题明细，不自动主张显著性
  （n=31 的题量下 McNemar 大多没有分辨力）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = data["runs"]
    if len(runs) != 1:
        raise SystemExit(f"{path} 含 {len(runs)} 个 mode，请用单 mode 的运行文件比较")
    mode = next(iter(runs))
    details = {d["qid"]: d for d in runs[mode]["details"]}
    report = runs[mode]["report"]
    return {"mode": mode, "details": details, "report": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    args = parser.parse_args()
    a = _load(PROJECT_ROOT / args.run_a)
    b = _load(PROJECT_ROOT / args.run_b)

    common = sorted(set(a["details"]) & set(b["details"]))
    print(f"A：{args.run_a}（mode={a['mode']}）acc {a['report']['accuracy']:.2%}，"
          f"tokens {a['report']['total_tokens']:,}")
    print(f"B：{args.run_b}（mode={b['mode']}）acc {b['report']['accuracy']:.2%}，"
          f"tokens {b['report']['total_tokens']:,}")
    print(f"共同题目 {len(common)} 道")

    discordant = []
    for qid in common:
        da, db = a["details"][qid], b["details"][qid]
        if da["pred"] != db["pred"] or da["correct"] != db["correct"]:
            discordant.append((qid, da["pred"], db["pred"], da["gold"]))
    print(f"\n不一致题 {len(discordant)} 道（A 对 B 错 / A 错 B 对 / 双错但答案不同）：")
    a_win = b_win = both_wrong = 0
    for qid, pa, pb, gold in discordant:
        ca, cb = pa == gold, pb == gold
        if ca and not cb:
            a_win += 1
        elif cb and not ca:
            b_win += 1
        else:
            both_wrong += 1
        mark = "A对" if ca and not cb else ("B对" if cb and not ca else "双错")
        print(f"  {qid}: A={pa or '空'} B={pb or '空'} gold={gold}（{mark}）")
    print(f"\n方向：A 占优 {a_win}，B 占优 {b_win}，双错 {both_wrong}")
    if not discordant:
        print("不一致对为 0：未观察到差异——不主张等价（分辨力下限内）")
    else:
        print(f"分辨力口径：n={len(common)} 的一致/不一致对不足以支撑显著性主张，"
              "只报方向与明细")

    delta = b["report"]["total_tokens"] - a["report"]["total_tokens"]
    print(f"\ntoken 差（B−A）：{delta:+,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
