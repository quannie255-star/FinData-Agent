"""ChatBI 演示：用自然语言问你的数据，拿带徽章的答案。

    uv run python scripts/chatbi_demo.py "中芯国际 2025 年 9 月 4 日的收盘价是多少？"
    uv run python scripts/chatbi_demo.py -i              # 交互模式
    uv run python scripts/chatbi_demo.py --source fixture "..."   # 用回放数据

默认连生产仓库（只读）；仓库不存在时回退到入库的回放 fixture，保证任何
clone 出来的人都能直接跑。

刻意不做的事：不接 LLM、不做前端、不猜意图。解析是确定性规则，答不上来
就报错——这个 demo 要展示的是**答案出口那道门禁**，不是对话能力。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from findata.agent.llm import OpenAICompatClient
from findata.chatbi import answer

WAREHOUSE = Path("data/warehouse.duckdb")
FIXTURE_DIR = Path("eval/fixtures/replay_20260915")


def connect(source: str) -> tuple[duckdb.DuckDBPyConnection, str]:
    if source == "warehouse" or (source == "auto" and WAREHOUSE.exists()):
        if not WAREHOUSE.exists():
            raise SystemExit(f"生产仓库不存在：{WAREHOUSE}（先跑 scripts/ingest_finance.py）")
        # 只读打开：演示脚本绝不能改写生产数据。
        return duckdb.connect(str(WAREHOUSE), read_only=True), f"生产仓库 {WAREHOUSE}"
    if not FIXTURE_DIR.exists():
        raise SystemExit(f"回放 fixture 不存在：{FIXTURE_DIR}")
    con = duckdb.connect()
    for table in ("stock_daily", "trading_calendar", "valuation_daily", "stock_universe"):
        con.execute(
            f"CREATE VIEW {table} AS SELECT * "
            f"FROM read_parquet('{FIXTURE_DIR / (table + '.parquet')}')"
        )
    return con, f"回放 fixture {FIXTURE_DIR}"


def ask(con, question: str, client=None, show_cost: bool = False) -> None:
    print(f"\n问：{question}")
    try:
        ans = answer(con, question, client=client)
    except ValueError as exc:
        print(f"  解析失败：{exc}")
        return
    print(ans.render(show_cost=show_cost))
    if ans.guard.traps:
        print(f"  陷阱类型：{'、'.join(ans.guard.traps)}")
        for ev in ans.guard.evidence:
            print(f"  证据：{ev}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ChatBI 演示")
    ap.add_argument("question", nargs="?", help="自然语言问题")
    ap.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    ap.add_argument("--source", default="auto", choices=["auto", "warehouse", "fixture"])
    ap.add_argument("--llm", action="store_true", help="用 LLM 解析（默认本机 Ollama）")
    ap.add_argument("--llm-base-url", default="http://localhost:11434/v1")
    # 默认 3B：填槽是抽取任务不是推理任务，3B 与 7B 在解析层评测里准确率打平
    # （7B 只是少降级 2 次），而 3B 起得快、8GB 显存随便跑。
    ap.add_argument("--llm-model", default="qwen2.5:3b")
    ap.add_argument("--llm-api-key", default="ollama", help="Ollama 不需要真 key，占位即可")
    ap.add_argument("--show-cost", action="store_true", help="显示解析来源与字符成本")
    args = ap.parse_args()

    con, label = connect(args.source)
    print(f"数据源：{label}")

    client = None
    if args.llm:
        client = OpenAICompatClient(
            base_url=args.llm_base_url,
            api_key=args.llm_api_key,
            model=args.llm_model,
        )
        # 连不上不报错：parse_with_llm 会捕获并降级到规则解析，demo 照常可用。
        print(f"LLM：{args.llm_model} @ {args.llm_base_url}（不可用则自动降级规则解析）")

    if args.interactive:
        print("输入问题，空行退出。")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            ask(con, q, client, args.show_cost)
        return

    if not args.question:
        ap.error("给一个问题，或用 -i 进交互模式")
    ask(con, args.question, client, args.show_cost)


if __name__ == "__main__":
    main()
