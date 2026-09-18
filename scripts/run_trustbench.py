"""TrustBench 执行器：量化「ChatBI 答得出但答不对」的比例。

跑两组对照：
  · 裸 ChatBI —— 只执行 SQL，把执行成功当成答案可信（这是现状）；
  · findata 门禁 —— 同样的 SQL，答案出口多一道可信判定。

三层评分与题集声明一致：
  execution   naive SQL 能否跑通（难度不在这层，应接近 100%）；
  detection   是否识别出「这个数字不该直接引用」；
  correction  给出的警示是否命中 expected.must_mention 的全部关键事实。

评测先行：门禁代码改坏了，这里的 detection 会掉下来，进 CI 即挂红。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb
import yaml

from findata.chatbi import answer_structured
from findata.dq.badges import BadgeLevel

BENCH_PATH = Path("eval/golden/trustbench.yaml")
FIXTURE_DIR = Path("eval/fixtures/replay_20260915")
OUT_PATH = Path("examples/trustbench-report.txt")

# 陷阱 → 查询意图。与 guard.py 的 intent 分支一一对应。
TRAP_INTENT = {
    "stale_price": "price",
    "suspended_window": "change",
    "gap_span": "change",
    "calendar_mismatch": "aggregate",
}


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for table in ("stock_daily", "trading_calendar", "valuation_daily", "stock_universe"):
        con.execute(
            f"CREATE VIEW {table} AS SELECT * "
            f"FROM read_parquet('{FIXTURE_DIR / (table + '.parquet')}')"
        )
    return con


def _fmt_naive(res: dict) -> str:
    if "value" in res:
        extra = f" ({res['as_of']})" if res.get("as_of") else ""
        return f"{res['value']}{res.get('unit', '')}{extra}"
    return f"{res.get('days_with_data')} 天 / 日均 {res.get('avg_volume')}"


def run(min_recall: float = 1.0) -> tuple[str, bool]:
    bench = yaml.safe_load(BENCH_PATH.read_text(encoding="utf-8"))
    con = _connect()
    rows: list[dict] = []
    n_exec = tp = fp = fn = tn = 0

    for c in bench["cases"]:
        params = {
            k: (date.fromisoformat(v) if k != "symbol" else v) for k, v in c["params"].items()
        }
        intent = TRAP_INTENT.get(c["trap"]) or c.get("intent", "change")

        # 裸 ChatBI：直接执行题集里那条 naive SQL
        try:
            row = con.execute(c["naive_sql"]).fetchone()
            exec_ok = row is not None and row[0] is not None
        except Exception:
            exec_ok = False

        ans = answer_structured(
            con,
            symbol=c["symbol"],
            intent=intent,
            start=params.get("start"),
            end=params.get("end"),
            as_of=params.get("as_of"),
            question=c["question"],
        )
        expect_flag = bool(c["expected"]["must_flag"])
        flagged = ans.badge is not BadgeLevel.BASELINE

        # 正负例分开计：只统计「该报的报了没」会漏掉恒真报警器。
        if expect_flag and flagged:
            tp += 1
            verdict = "✓命中"
        elif expect_flag and not flagged:
            fn += 1
            verdict = "✗漏检"
        elif not expect_flag and flagged:
            fp += 1
            verdict = "✗误报"
        else:
            tn += 1
            verdict = "✓放行"

        # 修正质量：正例看关键事实是否说全，负例看有没有乱说话
        must = c["expected"].get("must_mention", []) or []
        correct_ok = (
            all(str(k) in ans.guard.message for k in must) if must else (not flagged)
        )

        n_exec += exec_ok
        rows.append(
            {
                "id": c["id"],
                "trap": c["trap"],
                "expect": "应报警" if expect_flag else "应放行",
                "naive": _fmt_naive(c["naive_result"]),
                "exec": exec_ok,
                "mark": ans.mark,
                "verdict": verdict,
                "correct": correct_ok,
                "msg": ans.guard.message,
            }
        )

    n_pos, n_neg = tp + fn, fp + tn
    recall = tp / n_pos if n_pos else 0.0
    specificity = tn / n_neg if n_neg else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    n_correct = sum(r["correct"] for r in rows)

    lines = [
        f"TrustBench {bench['version']} —— 「SQL 正确但答案不该引用」",
        f"数据源：{bench['source']}（入库 fixture，结果可复现）",
        f"题数：{len(rows)}（正例 {n_pos} 有陷阱 / 负例 {n_neg} 对照组）",
        "",
        f"{'题号':<8}{'陷阱':<20}{'naive 结果':<26}{'裸ChatBI':<10}{'门禁':<12}{'判定'}",
        "-" * 108,
    ]
    for r in rows:
        lines.append(
            f"{r['id']:<8}{r['trap']:<20}{r['naive']:<26}"
            f"{'✓执行' if r['exec'] else '✗失败':<10}{r['mark']:<12}"
            f"{r['verdict']}{' ✓修正' if r['correct'] else ' ✗修正'}"
        )
    lines += [
        "-" * 108,
        "",
        "汇总",
        f"  execution    SQL 能跑通            {n_exec}/{len(rows)}"
        f"  {n_exec / len(rows):6.1%}   难度不在这层",
        "",
        f"  正例 {n_pos} 题（有陷阱）  检出 {tp}  漏检 {fn}   recall {recall:6.1%}",
        f"  负例 {n_neg} 题（对照组）  误报 {fp}  放行 {tn}   特异度 {specificity:6.1%}",
        f"  precision（报警中有多少是真陷阱）  {precision:6.1%}",
        f"  correction   关键事实说全          {n_correct}/{len(rows)}"
        f"  {n_correct / len(rows):6.1%}",
        "",
        "  对照：裸 ChatBI 从不报警 → recall 0%，特异度 100%。",
        "        「不报警」不是本事，precision 与 recall 双高才是。",
        "",
    ]
    for r in rows:
        lines += [f"{r['id']} {r['mark']}", f"    {r['msg']}", ""]

    passed = recall >= min_recall and fp == 0 and n_correct == len(rows)
    verdict = "通过" if passed else "未通过"
    lines.append(
        f"门禁：recall ≥ {min_recall:.0%}、误报 = 0、correction 全中 → {verdict}"
    )
    return "\n".join(lines), passed


def main() -> None:
    ap = argparse.ArgumentParser(description="跑 TrustBench")
    ap.add_argument("--min-recall", type=float, default=1.0)
    ap.add_argument("-o", "--out", default=str(OUT_PATH))
    ap.add_argument("--strict", action="store_true", help="不达标则以非零码退出（CI 用）")
    args = ap.parse_args()

    text, passed = run(args.min_recall)
    print(text)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\n已归档 → {out}")
    if args.strict and not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
