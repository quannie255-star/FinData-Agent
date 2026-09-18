"""解析层评测：规则解析 vs LLM 解析，谁更准、值不值这份成本。

跑两组题：
  main     TrustBench 的 17 个问句（模板生成）——**规则解析在这里天然占优**，
           所以这一组只能用来兜底（降级链路不能把对的改错），不能用来
           证明"规则比 LLM 好"，那是自己送分；
  variant  6 道长尾问法（eval/golden/parser_variants.yaml），专挑规则词表
           覆盖不到的说法——评测要区分得出差异，就得给基线出它会错的题。

三层看：
  slot        槽位完全匹配率——symbol、intent、日期三个槽全对才算对；
  degrade     LLM 降级次数与原因（降级不是失败，是护栏生效）；
  downstream  把解析结果喂进门禁，看最终徽章与 golden 是否一致。

第三层才是决定性的：解析错一格不一定要命，只要没改变"这个数字能不能引用"
的结论。反过来，槽位全对却改了徽章，才是真事故。

    uv run python scripts/run_parser_eval.py                 # 只跑规则解析（离线）
    uv run python scripts/run_parser_eval.py --llm           # 同时跑本机 Ollama

门禁只卡 main 组（规则解析必须满分，且两条解析路径的徽章都要与 golden 一致）。
variant 组是诊断用的，不进门禁——它的作用是回答"LLM 到底值不值"，而答案
本身需要先看数据再下结论，不该由门禁预设。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import yaml

from findata.agent.llm import OpenAICompatClient
from findata.chatbi import answer, answer_structured
from findata.chatbi.parser import parse_rule, parse_with_llm

BENCH_PATH = Path("eval/golden/trustbench.yaml")
VARIANTS_PATH = Path("eval/golden/parser_variants.yaml")
FIXTURE_DIR = Path("eval/fixtures/replay_20260915")
OUT_PATH = Path("examples/parser-eval-report.txt")

# 陷阱 → 查询意图。与 scripts/run_trustbench.py 的 TRAP_INTENT 保持一致
# （题集是同一份，改一边必须改另一边，否则两处评分会互相打脸）。
TRAP_INTENT = {
    "stale_price": "price",
    "suspended_window": "change",
    "gap_span": "change",
    "calendar_mismatch": "aggregate",
}

FAILED = "解析失败"


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for table in ("stock_daily", "trading_calendar", "valuation_daily", "stock_universe"):
        con.execute(
            f"CREATE VIEW {table} AS SELECT * "
            f"FROM read_parquet('{FIXTURE_DIR / (table + '.parquet')}')"
        )
    return con


@dataclass
class Golden:
    symbol: str
    intent: str
    start: date | None
    end: date | None
    as_of: date | None

    def __str__(self) -> str:
        if self.intent == "price":
            return f"{self.intent} {self.symbol} as_of={self.as_of}"
        return f"{self.intent} {self.symbol} {self.start}~{self.end}"


def _golden(c: dict) -> Golden:
    p = {k: (v if k == "symbol" else date.fromisoformat(v)) for k, v in c["params"].items()}
    return Golden(
        symbol=c["symbol"],
        intent=TRAP_INTENT.get(c.get("trap")) or c.get("intent", "change"),
        start=p.get("start"),
        end=p.get("end"),
        as_of=p.get("as_of"),
    )


def _load() -> list[tuple[str, dict]]:
    cases: list[tuple[str, dict]] = []
    for group, path in (("main", BENCH_PATH), ("variant", VARIANTS_PATH)):
        if not path.exists():
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases += [(group, c) for c in doc["cases"]]
    return cases


def _ask_mark(con, question: str, client=None) -> str:
    """自然语言问一句，拿到门禁徽章；认不出槽位时引擎会报错，如实记为失败。"""
    try:
        return answer(con, question, client=client).mark
    except ValueError:
        return FAILED


def _diff(g: Golden, p) -> str:
    """列出填错的槽，空串表示全对。"""
    bad = []
    if p.symbol != g.symbol:
        bad.append(f"symbol({p.symbol})")
    if p.intent != g.intent:
        bad.append(f"intent({p.intent})")
    if p.as_of != g.as_of:
        bad.append(f"as_of({p.as_of})")
    if p.start != g.start:
        bad.append(f"start({p.start})")
    if p.end != g.end:
        bad.append(f"end({p.end})")
    return "、".join(bad)


def run(client=None, model: str = "") -> tuple[str, bool]:
    cases = _load()
    con = _connect()

    rows: list[dict] = []
    chars = 0
    degrade_reasons: dict[str, int] = {}
    field_err: dict[str, int] = {"symbol": 0, "intent": 0, "as_of": 0, "start": 0, "end": 0}

    for group, c in cases:
        q = c["question"]
        g = _golden(c)
        expect_mark = answer_structured(
            con,
            symbol=g.symbol,
            intent=g.intent,
            start=g.start,
            end=g.end,
            as_of=g.as_of,
        ).mark

        rp = parse_rule(con, q)
        rule_diff = _diff(g, rp)
        rec: dict = {
            "group": group,
            "id": c["id"],
            "golden": str(g),
            "rule_ok": not rule_diff,
            "rule": "✓" if not rule_diff else f"✗ {rule_diff}",
            "rule_mark": _ask_mark(con, q),
            "expect_mark": expect_mark,
        }

        if client is not None:
            out = parse_with_llm(con, client, q)
            llm_diff = _diff(g, out.parsed)
            chars += out.usage_chars
            if out.source == "llm->rule":
                degrade_reasons[out.error] = degrade_reasons.get(out.error, 0) + 1
            for f in field_err:
                if f in llm_diff:
                    field_err[f] += 1
            rec["llm_ok"] = not llm_diff
            rec["llm"] = "✓" if not llm_diff else f"✗ {llm_diff}"
            rec["llm_src"] = out.source
            rec["llm_err"] = out.error
            rec["llm_mark"] = _ask_mark(con, q, client=client)
        rows.append(rec)

    lines = [
        f"解析层评测 —— 规则解析 vs LLM 解析（{model or 'rule only'}）",
        f"题集：main {sum(r['group'] == 'main' for r in rows)} 题（模板生成，规则占优）+ "
        f"variant {sum(r['group'] == 'variant' for r in rows)} 题（长尾问法，规则盲区）",
        "golden = 各题 YAML params；数据源为入库 fixture，结果可复现",
        "",
        f"{'组':<9}{'题号':<8}{'golden':<40}{'规则':<24}{'LLM':<30}{'门禁一致'}",
        "-" * 124,
    ]
    for r in rows:
        llm_col = r.get("llm", "—")
        if r.get("llm_src") == "llm->rule":
            llm_col += f" [降级：{r['llm_err']}]"
        if client is None:
            same = "✓" if r["rule_mark"] == r["expect_mark"] else "✗"
        else:
            same = "✓" if r["rule_mark"] == r["expect_mark"] == r["llm_mark"] else "✗"
        lines.append(
            f"{r['group']:<9}{r['id']:<8}{r['golden']:<40}{r['rule']:<24}{llm_col:<30}{same}"
        )
    lines.append("-" * 124)

    def _sum(group: str) -> dict:
        sub = [r for r in rows if r["group"] == group]
        n = len(sub)
        d = {
            "n": n,
            "rule": sum(r["rule_ok"] for r in sub),
            "rule_ok_mark": sum(r["rule_mark"] == r["expect_mark"] for r in sub),
        }
        if client is not None:
            d["llm"] = sum(r.get("llm_ok", False) for r in sub)
            # 「模型独立答对」= 没降级且槽位全对。混在一起会高估模型：
            # 降级后是规则解析在兜底，那不算模型的能力。
            d["llm_pure"] = sum(
                r.get("llm_ok", False) and r.get("llm_src") == "llm" for r in sub
            )
            d["degrade"] = sum(r.get("llm_src") == "llm->rule" for r in sub)
            d["rule_badge"] = sum(r["rule_mark"] == r["expect_mark"] for r in sub)
            d["llm_badge"] = sum(r.get("llm_mark") == r["expect_mark"] for r in sub)
        return d

    main, var = _sum("main"), _sum("variant")
    lines += ["", "汇总（slot 槽位完全匹配 / badge 下游徽章与 golden 一致）"]
    lines.append(
        f"  main    （模板题，规则占优）    n={main['n']}  "
        f"规则 slot {main['rule']}/{main['n']} {main['rule'] / main['n']:6.1%}"
    )
    if client is not None:
        lines.append(
            f"                                    "
            f"LLM slot {main['llm']}/{main['n']} {main['llm'] / main['n']:6.1%}"
            f"  降级 {main['degrade']}（模型独立答对 {main['llm_pure']}）"
        )
    lines.append(
        f"  variant （长尾题，规则盲区）    n={var['n']}  "
        f"规则 slot {var['rule']}/{var['n']} {var['rule'] / var['n']:6.1%}"
    )
    if client is not None:
        lines.append(
            f"                                    "
            f"LLM slot {var['llm']}/{var['n']} {var['llm'] / var['n']:6.1%}"
            f"  降级 {var['degrade']}（模型独立答对 {var['llm_pure']}）"
        )
        lines += [
            "",
            "  badge（下游徽章与 golden 一致——这条才是决定性的）",
            f"    main     规则路径 {main['rule_badge']}/{main['n']}   "
            f"LLM 路径 {main['llm_badge']}/{main['n']}",
            f"    variant  规则路径 {var['rule_badge']}/{var['n']}   "
            f"LLM 路径 {var['llm_badge']}/{var['n']}",
        ]
    if client is not None:
        lines += [
            "",
            f"  LLM 总降级 {sum(r.get('llm_src') == 'llm->rule' for r in rows)}/{len(rows)}，"
            f"字符成本 {chars}",
        ]
        if degrade_reasons:
            top = sorted(degrade_reasons.items(), key=lambda kv: -kv[1])
            lines.append("  降级原因：" + "；".join(f"{k} ×{v}" for k, v in top))
        hit = {k: v for k, v in field_err.items() if v}
        lines.append(
            "  错槽分布："
            + ("；".join(f"{k} ×{v}" for k, v in hit.items()) if hit else "无")
        )
        n_same = sum(
            1 for r in rows if r["rule_mark"] == r["expect_mark"] == r.get("llm_mark")
        )
        lines += [
            "",
            f"  downstream 两条解析路径的徽章都与 golden 一致   {n_same}/{len(rows)}  "
            f"{n_same / len(rows):6.1%}",
            "  解析错一格不一定要命；改了徽章才是真事故。",
        ]

    # 门禁只卡 main 组：规则解析满分 + 两条路径徽章都与 golden 一致。
    # variant 组是诊断，不预设谁该赢。
    passed = main["rule"] == main["n"] and main["rule_ok_mark"] == main["n"]
    if client is not None:
        passed = passed and main["llm_badge"] == main["n"]
    lines += [
        "",
        f"门禁（main 组）：规则解析 100% 且下游徽章与 golden 一致 → "
        f"{'通过' if passed else '未通过'}",
    ]
    return "\n".join(lines), passed


def main() -> None:
    ap = argparse.ArgumentParser(description="解析层评测")
    ap.add_argument("--llm", action="store_true", help="同时评测 LLM 解析（本机 Ollama）")
    ap.add_argument("--llm-base-url", default="http://localhost:11434/v1")
    # 3B 够用：填槽是抽取任务不是推理任务，而且 8GB 显存跑得动、答得快。
    ap.add_argument("--llm-model", default="qwen2.5:3b")
    ap.add_argument("--llm-api-key", default="ollama", help="Ollama 不需要真 key，占位即可")
    ap.add_argument("-o", "--out", default=str(OUT_PATH))
    ap.add_argument("--strict", action="store_true", help="不达标则以非零码退出")
    args = ap.parse_args()

    client = None
    if args.llm:
        client = OpenAICompatClient(
            base_url=args.llm_base_url,
            api_key=args.llm_api_key,
            model=args.llm_model,
        )
        print(f"LLM：{args.llm_model} @ {args.llm_base_url}")

    text, passed = run(client, model=args.llm_model if args.llm else "")
    print(text)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\n已归档 → {out}")
    if args.strict and not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
