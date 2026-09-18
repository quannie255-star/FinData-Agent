"""采集真实 Agent 轨迹：拿题集的问句去问 ChatBI，把每一步落进 JSONL。

**这里没有一句合成数据。** 轨迹来自 findata 自己的 ChatBI 真跑一遍——
23 道题 × 2 种解析器 = 46 条，其中含解析降级事件的那几条，就是天然的
tool-calling 失败样本。

为什么这比"写个脚本造假数据"强：
  · 失败是真的失败，不是我们编出来的失败模式；
  · 重跑就能复现，别人能验；
  · 解析器一换（规则 ↔ LLM），失败形态就变——这本身就是对照组。

    uv run python scripts/run_agent_trace.py                 # 只跑规则解析
    uv run python scripts/run_agent_trace.py --llm           # 两种解析器都跑
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb
import yaml

from findata.agent.llm import OpenAICompatClient
from findata.agentops.schema import Trace, write_traces
from findata.chatbi import answer

BENCH_PATH = Path("eval/golden/trustbench.yaml")
VARIANTS_PATH = Path("eval/golden/parser_variants.yaml")
FIXTURE_DIR = Path("eval/fixtures/replay_20260915")
OUT_PATH = Path("examples/agent-traces.jsonl")

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


def _load() -> list[tuple[str, dict]]:
    cases: list[tuple[str, dict]] = []
    for group, path in (("main", BENCH_PATH), ("variant", VARIANTS_PATH)):
        if not path.exists():
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases += [(group, c) for c in doc["cases"]]
    return cases


def _golden(c: dict) -> dict:
    """golden 一并落盘：轨迹不带标签就只能看、不能判。"""
    params = c.get("params") or {}
    p = {k: (v if k == "symbol" else date.fromisoformat(v)) for k, v in params.items()}
    return {
        "symbol": c["symbol"],
        "intent": TRAP_INTENT.get(c.get("trap")) or c.get("intent", "change"),
        "start": str(p["start"]) if p.get("start") else None,
        "end": str(p["end"]) if p.get("end") else None,
        "as_of": str(p["as_of"]) if p.get("as_of") else None,
        "group": None,  # 由调用方补
        "trap": c.get("trap"),
    }


def collect(con, group: str, c: dict, client=None) -> Trace:
    g = _golden(c)
    g["group"] = group
    t = Trace(task=c["question"], golden=g)
    try:
        answer(con, c["question"], client=client, trace=t)
    except ValueError as exc:
        # finish 会整体覆盖 outcome，而引擎在抛错前已经写了机器可读的错误码
        # （slot_incomplete:*）——不能让人类可读的异常把它盖掉，否则归因
        # 只能落到 else 分支，全部误判成"认不出标的"。
        code = t.outcome.get("error") or "unknown"
        t.finish(error=f"aborted:{code}", detail=str(exc))
    # 对照字段必须在 finish 之后补：中断的轨迹同样要打标签，认不出槽位
    # 就是解析不符，不能因为没有徽章就从分母里溜掉。
    t.outcome["expected_symbol"] = g["symbol"]
    t.outcome["expected_intent"] = g["intent"]
    t.outcome["parse_matches_golden"] = _parse_matches(t, g)
    if c.get("refuse"):
        # 该拒绝的题：报错 = 正确拒绝；给出了数字 = 编了个前提还照答。
        t.outcome["should_refuse"] = True
        t.outcome["refused"] = bool(t.outcome.get("error"))
    return t


def _parse_matches(t: Trace, g: dict) -> bool:
    """解析槽位是否与 golden 一致——供归因区分"解析错了"与"门禁错了"。"""
    for s in t.steps:
        if s.name == "parse_query":
            r = s.result
            return (
                r.get("symbol") == g["symbol"]
                and r.get("intent") == g["intent"]
                and r.get("as_of") == g["as_of"]
                and r.get("start") == g["start"]
                and r.get("end") == g["end"]
            )
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="采集真实 Agent 轨迹")
    ap.add_argument("--llm", action="store_true", help="同时用 LLM 解析再跑一遍")
    ap.add_argument("--llm-base-url", default="http://localhost:11434/v1")
    ap.add_argument("--llm-model", default="qwen2.5:3b")
    ap.add_argument("--llm-api-key", default="ollama")
    ap.add_argument("-o", "--out", default=str(OUT_PATH))
    args = ap.parse_args()

    con = _connect()
    cases = _load()

    def _gen():
        for group, c in cases:
            yield collect(con, group, c)
            if args.llm:
                yield collect(con, group, c, client=client)

    client = None
    if args.llm:
        client = OpenAICompatClient(
            base_url=args.llm_base_url, api_key=args.llm_api_key, model=args.llm_model
        )

    n = write_traces(args.out, _gen())

    # 落盘后再流式读回来统计——顺带自检读写两端都是流式的、能对上。
    from findata.agentops.schema import iter_traces
    from findata.agentops.triage import SEV_LOUD, SEV_SILENT, summarize

    traces = list(iter_traces(args.out))
    s = summarize(traces)
    lines = [
        f"Agent 轨迹归因报告（{args.out}）",
        f"轨迹 {s['n']} 条，解析器分布 "
        + "、".join(f"{k}×{v['n']}" for k, v in s["by_parser"].items()),
        "",
        "按严重度（这一刀最重要）",
    ]
    for sev, label in ((SEV_SILENT, "静默错"), (SEV_LOUD, "吵着失败"), ("ok", "通顺")):
        n = s["by_severity"].get(sev, 0)
        note = {
            SEV_SILENT: "← 链路没断但取错口径，门禁发现不了，最危险",
            SEV_LOUD: "← 中断或降级，系统知道自己不知道，看得见",
            "ok": "",
        }[sev]
        lines.append(f"  {label:<10}{n:>3}/{s['n']}   {note}")
    lines += ["", "按根因类别"]
    for cat, n in sorted(s["by_category"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {cat:<26}{n:>3}")
    lines += ["", "按解析器（静默错 / 吵着失败）"]
    for p, d in s["by_parser"].items():
        lines.append(f"  {p:<12}n={d['n']:<3} 静默 {d['silent']}   吵 {d['loud']}")
    lines += ["", "失败轨迹逐条"]
    for d, t in zip(s["diags"], traces, strict=True):
        if not d.failed:
            continue
        lines += [
            f"  [{d.severity}] {t.run_id} {t.parser} {d.label}",
            f"      问：{t.task}",
        ]
        lines += [f"      · {e}" for e in d.evidence]
        if d.suggestion:
            lines.append(f"      → {d.suggestion}")
    text = "\n".join(lines)
    print()
    print(text)
    Path("examples/agent-trace-report.txt").write_text(text + "\n", encoding="utf-8")
    print("\n已归档 → examples/agent-trace-report.txt")


if __name__ == "__main__":
    main()
