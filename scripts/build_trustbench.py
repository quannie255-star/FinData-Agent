"""TrustBench 生成器：造「SQL 正确但答案不该引用」的评测题。

为什么需要这份题集：Spider / BIRD 只测 SQL 准不准，而 2026 年的失败模式
已经不是语法（SQL-of-Thought：95–99% 生成 SQL 语法已正确），而是**语义
意图不匹配**。ChatBI 最容易骗人的一类问题长这样——SQL 完全跑得通、数字
也对，但它答非所问：问停牌日的收盘价，它顺延返回停牌前的价格且不说明；
问跨越停牌的区间涨跌幅，它给一个被复牌跳空主导的数字，看起来像连续走势。

本脚本只做一件事：把**真实停牌窗口**变成**可自动评分的题**。所有数字都
从 eval/fixtures/replay_20260915 实跑出来，不手写、不假设——题集的价值
全在「naive 答案确实和可信答案不一样」这一点上，自己编的数字没有意义。

设计约束：
1. 数据只来自入库的回放 fixture，仓库快照会漂移，fixture 不会；
2. naive_sql 刻意写成「一个正常 ChatBI 会生成的样子」（自然语言直译），
   不走本项目语义层的指标字典——本 benchmark 要测的是裸 ChatBI 的盲区，
   拿自家编译器当基线等于自己给自己送分；
3. truth 全部由停牌事实（domains/finance/seeds.py）+ 交易日历推导，
   与 dq/triage.py 的抑制判据同源，不新造一套口径。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb
import yaml

from findata.domains.finance.seeds import SUSPENSION_SEEDS

FIXTURE_DIR = Path("eval/fixtures/replay_20260915")
OUT_PATH = Path("eval/golden/trustbench.yaml")

# 陷阱分类学。四类覆盖「SQL 对但答案误导」的主要形态，新增题必须归入其一，
# 分类不收敛的题说明还没想清楚它到底在考什么。
TRAP_TYPES = {
    "stale_price": (
        "查询日落在停牌窗口内，SQL 顺延返回停牌前最后一个交易日的价格，却不说明它是陈旧的"
    ),
    "suspended_window": "区间跨越停牌，涨跌幅被复牌跳空主导，看起来是连续走势，实际是一次性跳动",
    "calendar_mismatch": "聚合/计数类指标的分母与真实交易日数不符——停牌日没有数据行，被静默排除",
    "gap_span": "区间端点落在停牌缺口上，实际取到的 start/end 不是用户指定的日期",
    # 对照组。没有负样本的 benchmark 无法证明门禁不是「见谁都报警」——
    # 一个恒真的报警器同样能拿 detection 100%，那是最危险的假满分。
    "clean": "对照组：该标的该区间没有停牌覆盖，门禁**不该**报警（报警即误报）",
}

# 题目定义：只声明「问什么」和「陷阱是什么」，数字交给 _build_case 实算。
CASES: list[dict] = [
    {
        "id": "tb-001",
        "trap": "stale_price",
        "symbol": "688981",
        "as_of": "2025-09-04",
        "question": "中芯国际 2025 年 9 月 4 日的收盘价是多少？",
    },
    {
        "id": "tb-002",
        "trap": "suspended_window",
        "symbol": "688981",
        "start": "2025-08-29",
        "end": "2025-09-12",
        "question": "中芯国际从 2025 年 8 月 29 日到 9 月 12 日涨了多少？",
    },
    {
        "id": "tb-003",
        "trap": "stale_price",
        "symbol": "601088",
        "as_of": "2025-08-10",
        "question": "中国神华 2025 年 8 月 10 日的收盘价是多少？",
    },
    {
        "id": "tb-004",
        "trap": "suspended_window",
        "symbol": "601088",
        "start": "2025-08-01",
        "end": "2025-08-22",
        "question": "中国神华 2025 年 8 月 1 日到 8 月 22 日的涨跌幅是多少？",
    },
    {
        "id": "tb-005",
        "trap": "stale_price",
        "symbol": "600030",
        "as_of": "2022-01-26",
        "question": "中信证券 2022 年 1 月 26 日的收盘价是多少？",
    },
    {
        "id": "tb-006",
        "trap": "suspended_window",
        "symbol": "600030",
        "start": "2022-01-14",
        "end": "2022-01-28",
        "question": "中信证券 2022 年 1 月 14 日到 1 月 28 日涨了多少？",
    },
    {
        "id": "tb-007",
        "trap": "stale_price",
        "symbol": "600900",
        "as_of": "2022-10-26",
        "question": "长江电力 2022 年 10 月 26 日的收盘价是多少？",
    },
    {
        "id": "tb-008",
        "trap": "suspended_window",
        "symbol": "600900",
        "start": "2022-10-25",
        "end": "2022-10-27",
        "question": "长江电力 2022 年 10 月 25 日到 10 月 27 日跌了多少？",
    },
    {
        "id": "tb-009",
        "trap": "calendar_mismatch",
        "symbol": "601088",
        "start": "2025-08-01",
        "end": "2025-08-31",
        "question": "中国神华 2025 年 8 月的日均成交量是多少？",
    },
    {
        "id": "tb-010",
        "trap": "calendar_mismatch",
        "symbol": "688981",
        "start": "2025-09-01",
        "end": "2025-09-30",
        "question": "中芯国际 2025 年 9 月有多少个交易日有成交？",
    },
    {
        "id": "tb-011",
        "trap": "gap_span",
        "symbol": "600030",
        "start": "2022-01-19",
        "end": "2022-01-27",
        "question": "中信证券 2022 年 1 月 19 日到 1 月 27 日涨了多少？",
    },
    # ── 对照组：门禁不该报警。两类都要有 ——
    #   c01/c02 标的本身无停牌（查「会不会被别的票的停牌误伤」）；
    #   c03–c06 标的有停牌史但区间不在窗口内（查区间判定准不准）。
    {
        "id": "tb-c01",
        "trap": "clean",
        "intent": "price",
        "symbol": "600519",
        "as_of": "2025-09-15",
        "question": "贵州茅台 2025 年 9 月 15 日的收盘价是多少？",
    },
    {
        "id": "tb-c02",
        "trap": "clean",
        "intent": "change",
        "symbol": "600519",
        "start": "2025-09-01",
        "end": "2025-09-30",
        "question": "贵州茅台 2025 年 9 月涨了多少？",
    },
    {
        "id": "tb-c03",
        "trap": "clean",
        "intent": "change",
        "symbol": "688981",
        "start": "2025-10-09",
        "end": "2025-10-20",
        "question": "中芯国际 2025 年 10 月 9 日到 10 月 20 日涨了多少？",
    },
    {
        "id": "tb-c04",
        "trap": "clean",
        "intent": "aggregate",
        "symbol": "601088",
        "start": "2025-09-01",
        "end": "2025-09-30",
        "question": "中国神华 2025 年 9 月的日均成交量是多少？",
    },
    {
        "id": "tb-c05",
        "trap": "clean",
        "intent": "change",
        "symbol": "600030",
        "start": "2022-03-01",
        "end": "2022-03-15",
        "question": "中信证券 2022 年 3 月 1 日到 3 月 15 日涨了多少？",
    },
    {
        "id": "tb-c06",
        "trap": "clean",
        "intent": "change",
        "symbol": "600900",
        "start": "2022-11-01",
        "end": "2022-11-15",
        "question": "长江电力 2022 年 11 月 1 日到 11 月 15 日涨了多少？",
    },
]


def _connect() -> duckdb.DuckDBPyConnection:
    """把回放 fixture 挂成内存视图。fixture 缺失直接报错——宁可跑不起来，
    也不要静默退化到会漂移的生产仓库。"""
    if not FIXTURE_DIR.exists():
        raise SystemExit(f"缺少回放 fixture：{FIXTURE_DIR}（先跑 scripts/build_replay_fixture.py）")
    con = duckdb.connect()
    for table in ("stock_daily", "trading_calendar", "valuation_daily", "stock_universe"):
        con.execute(
            f"CREATE VIEW {table} AS SELECT * "
            f"FROM read_parquet('{FIXTURE_DIR / (table + '.parquet')}')"
        )
    return con


def _suspension_of(symbol: str, as_of: date) -> dict | None:
    """找出覆盖 as_of 的停牌记录。种子是人工核实过的公司事件，
    是这套题唯一的『事实源』。"""
    for s in SUSPENSION_SEEDS:
        if s["symbol"] == symbol and s["date"] <= as_of <= s["end_date"]:
            return s
    return None


def _trading_days_between(con, lo: date, hi: date) -> int:
    return con.execute(
        "SELECT count(*) FROM trading_calendar WHERE date > ? AND date <= ?", [lo, hi]
    ).fetchone()[0]


def _last_row_before(con, symbol: str, as_of: date):
    return con.execute(
        "SELECT date, close FROM stock_daily WHERE symbol = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        [symbol, as_of],
    ).fetchone()


def _suspended_trading_days(con, symbol: str, lo: date, hi: date) -> list[str]:
    """窗口内「日历说该交易、但这只票没有数据行」的日期——停牌缺口的定义。"""
    rows = con.execute(
        "SELECT c.date FROM trading_calendar c "
        "WHERE c.date BETWEEN ? AND ? "
        "  AND NOT EXISTS (SELECT 1 FROM stock_daily s WHERE s.symbol = ? AND s.date = c.date) "
        "ORDER BY c.date",
        [lo, hi, symbol],
    ).fetchall()
    return [r[0].isoformat() for r in rows]


def _build_stale_price(con, c: dict) -> dict:
    """stale_price：naive SQL 顺延取停牌前最后一笔，答案存在但不代表查询日。"""
    sym, as_of = c["symbol"], date.fromisoformat(c["as_of"])
    susp = _suspension_of(sym, as_of)
    if susp is None:
        raise SystemExit(f"{c['id']}: {sym} 在 {as_of} 没有停牌种子，题目不成立")
    row = _last_row_before(con, sym, as_of)
    if row is None:
        raise SystemExit(f"{c['id']}: 无数据行")
    stale_days = _trading_days_between(con, row[0], as_of)
    return {
        "naive_sql": (
            f"SELECT date, close FROM stock_daily WHERE symbol = '{sym}' "
            f"AND date <= DATE '{as_of}' ORDER BY date DESC LIMIT 1"
        ),
        "naive_result": {"value": float(row[1]), "as_of": row[0].isoformat(), "unit": "元"},
        "truth": {
            "suspended_from": susp["date"].isoformat(),
            "suspended_to": susp["end_date"].isoformat(),
            "returned_as_of": row[0].isoformat(),
            "stale_trading_days": stale_days,
            "reason": susp["detail"].split("来源")[0].strip(),
        },
        "expected": {
            "must_flag": True,
            "must_mention": ["停牌", row[0].isoformat()],
            "trusted_answer": (
                f"{as_of} 处于停牌期（{susp['date']}–{susp['end_date']}），无当日行情；"
                f"{row[1]} 元是停牌前最后一个交易日 {row[0]} 的收盘价，"
                f"已滞后 {stale_days} 个交易日。"
            ),
        },
    }


def _build_suspended_window(con, c: dict) -> dict:
    """suspended_window：区间涨跌幅被复牌跳空主导。用「复牌当日变动占区间
    总变动的比例」量化误导程度——比例越接近 1，『连续走势』的错觉越强。"""
    sym = c["symbol"]
    start, end = date.fromisoformat(c["start"]), date.fromisoformat(c["end"])
    pts = con.execute(
        "SELECT date, close FROM stock_daily WHERE symbol = ? AND date BETWEEN ? AND ? "
        "ORDER BY date",
        [sym, start, end],
    ).fetchall()
    if len(pts) < 2:
        raise SystemExit(f"{c['id']}: 区间内数据点不足")
    start_close, end_close = float(pts[0][1]), float(pts[-1][1])
    total_move = (end_close / start_close - 1) * 100

    gaps = _suspended_trading_days(con, sym, start, end)
    # 复牌日：缺口之后的第一个有数据的交易日
    resume = con.execute(
        "SELECT date, close FROM stock_daily WHERE symbol = ? AND date > ? "
        "ORDER BY date ASC LIMIT 1",
        [sym, date.fromisoformat(gaps[-1]) if gaps else start],
    ).fetchone()
    pre_halt = con.execute(
        "SELECT date, close FROM stock_daily WHERE symbol = ? AND date < ? "
        "ORDER BY date DESC LIMIT 1",
        [sym, date.fromisoformat(gaps[0]) if gaps else start],
    ).fetchone()
    resume_move = None
    concentration = None
    if resume and pre_halt:
        resume_move = (float(resume[1]) / float(pre_halt[1]) - 1) * 100
        concentration = round(abs(resume_move / total_move), 3) if total_move else None

    return {
        "naive_sql": (
            f"SELECT (last_close / first_close - 1) * 100 AS pct FROM ("
            f"SELECT (SELECT close FROM stock_daily WHERE symbol='{sym}' "
            f"AND date >= DATE '{start}' "
            f"ORDER BY date ASC LIMIT 1) AS first_close, "
            f"(SELECT close FROM stock_daily WHERE symbol='{sym}' AND date <= DATE '{end}' "
            f"ORDER BY date DESC LIMIT 1) AS last_close)"
        ),
        "naive_result": {"value": round(total_move, 4), "unit": "%"},
        "truth": {
            "suspended_days": gaps,
            "n_suspended": len(gaps),
            "resume_date": resume[0].isoformat() if resume else None,
            "resume_move_pct": round(resume_move, 4) if resume_move is not None else None,
            "move_concentration": concentration,
        },
        "expected": {
            "must_flag": True,
            "must_mention": ["停牌", gaps[0]] if gaps else ["停牌"],
            "trusted_answer": (
                f"{round(total_move, 2)}% 是区间首尾价直接相减，但中间有 {len(gaps)} 个停牌日"
                f"（{gaps[0] if gaps else '?'}–{gaps[-1] if gaps else '?'}）；"
                + _jump_note(resume, resume_move, total_move, concentration)
            ),
        },
    }


def _jump_note(resume, resume_move, total_move, concentration) -> str:
    """把「复牌跳空 vs 区间总变动」的关系说成人话。

    concentration > 1 是最容易被掩盖的情形：复牌当日变动幅度**超过**区间
    首尾差，说明中间剧烈跳动后又被回补——首尾相减把这个过程整个抹平了。
    只报一个百分比在这里等于撒谎，所以单独成句。
    """
    if resume is None or resume_move is None or concentration is None:
        return "走势并非连续。"
    if concentration > 1:
        return (
            f"其中 {resume[0]} 复牌当日就变动了 {round(resume_move, 2)}%，"
            f"幅度**超过**区间总变动（{round(total_move, 2)}%）——中间剧烈跳动后又被回补，"
            f"首尾相减把这段过程整个抹平了。"
        )
    return (
        f"其中 {round(resume_move, 2)}% 发生在 {resume[0]} 复牌当日"
        f"（占区间总变动的 {concentration:.0%}），不是连续走势。"
    )


def _build_calendar_mismatch(con, c: dict) -> dict:
    """calendar_mismatch：停牌日没有数据行，聚合的分母被静默缩小。
    统计口径不写清楚，『日均』『有多少天有成交』就是错的。"""
    sym = c["symbol"]
    start, end = date.fromisoformat(c["start"]), date.fromisoformat(c["end"])
    n_rows, sum_vol = con.execute(
        "SELECT count(*), sum(volume) FROM stock_daily WHERE symbol = ? AND date BETWEEN ? AND ?",
        [sym, start, end],
    ).fetchone()
    n_calendar = con.execute(
        "SELECT count(*) FROM trading_calendar WHERE date BETWEEN ? AND ?", [start, end]
    ).fetchone()[0]
    gaps = _suspended_trading_days(con, sym, start, end)
    return {
        "naive_sql": (
            f"SELECT count(*) AS days, avg(volume) AS avg_vol FROM stock_daily "
            f"WHERE symbol = '{sym}' AND date BETWEEN DATE '{start}' AND DATE '{end}'"
        ),
        "naive_result": {
            "days_with_data": int(n_rows),
            "avg_volume": round(float(sum_vol) / int(n_rows), 2),
        },
        "truth": {
            "calendar_trading_days": int(n_calendar),
            "suspended_days": gaps,
            "n_suspended": len(gaps),
            "denominator_ambiguity": (
                f"『日均』可以按 {n_rows} 个有成交日算，也可以按 {n_calendar} 个交易日算"
                f"（停牌日计 0），两者相差 {round((n_calendar - n_rows) / n_calendar * 100, 1)}%"
            ),
        },
        "expected": {
            "must_flag": True,
            "must_mention": ["停牌", "口径"],
            "trusted_answer": (
                f"{start}–{end} 交易日历共 {n_calendar} 个交易日，但 {sym} "
                f"只有 {n_rows} 天有数据行——"
                f"{len(gaps)} 天停牌（{gaps[0] if gaps else '?'}–"
                f"{gaps[-1] if gaps else '?'}）不产生数据。"
                f"所以『日均成交量』按有成交日算是 {round(float(sum_vol) / int(n_rows), 2)}、"
                f"按全部交易日算（停牌日计 0）是 {round(float(sum_vol) / int(n_calendar), 2)}；"
                f"『有多少天有成交』答案是 {n_rows} 天而不是 {n_calendar} 天。"
                f"口径不写清楚，同一个数字能得出两个结论。"
            ),
        },
    }


def _build_gap_span(con, c: dict) -> dict:
    """gap_span：区间端点落在停牌缺口里，SQL 静默把端点挪到最近的可用数据，
    于是「用户以为的区间」和「实际计算的区间」不是一回事。

    最坏情形是区间被压成一个点，涨跌幅直接算出 0%——这不是「没波动」，
    是「区间里只剩一天」，而裸 ChatBI 会把它当成正常结果报出来。
    """
    sym = c["symbol"]
    start, end = date.fromisoformat(c["start"]), date.fromisoformat(c["end"])
    pts = con.execute(
        "SELECT date, close FROM stock_daily WHERE symbol = ? AND date BETWEEN ? AND ? "
        "ORDER BY date",
        [sym, start, end],
    ).fetchall()
    if not pts:
        raise SystemExit(f"{c['id']}: 区间内无任何数据行")
    actual_start, actual_end = pts[0][0], pts[-1][0]
    total_move = (float(pts[-1][1]) / float(pts[0][1]) - 1) * 100 if len(pts) > 1 else 0.0
    gaps = _suspended_trading_days(con, sym, start, end)

    if len(pts) == 1:
        verdict = (
            f"整个区间只剩 {actual_start} 一个数据点，涨跌幅必然是 0%——"
            f"不是没波动，是没数据可比。"
        )
    else:
        verdict = (
            f"实际计算的区间是 {actual_start}–{actual_end}，"
            f"与提问的 {start}–{end} 不是一段。"
        )
    return {
        "naive_sql": (
            f"SELECT (last_close / first_close - 1) * 100 AS pct FROM ("
            f"SELECT (SELECT close FROM stock_daily WHERE symbol='{sym}' "
            f"AND date >= DATE '{start}' "
            f"ORDER BY date ASC LIMIT 1) AS first_close, "
            f"(SELECT close FROM stock_daily WHERE symbol='{sym}' AND date <= DATE '{end}' "
            f"ORDER BY date DESC LIMIT 1) AS last_close)"
        ),
        "naive_result": {"value": round(total_move, 4), "unit": "%"},
        "truth": {
            "asked_window": [start.isoformat(), end.isoformat()],
            "computed_window": [actual_start.isoformat(), actual_end.isoformat()],
            "n_points": len(pts),
            "suspended_days": gaps,
            "endpoint_shifted": actual_start != start or actual_end != end,
        },
        "expected": {
            "must_flag": True,
            "must_mention": ["停牌", actual_start.isoformat()],
            "trusted_answer": (
                f"{start} 起停牌至 {end} 前，{sym} 在这段区间没有连续行情："
                f"SQL 实际只取到 {len(pts)} 个数据点（{actual_start}"
                f"{'–' + actual_end.isoformat() if len(pts) > 1 else ''}）。{verdict}"
            ),
        },
    }


def _build_clean(con, c: dict) -> dict:
    """对照组：正常区间，门禁**不该**报警。

    没有负样本的检测率是空洞的——一个「见谁都报」的恒真报警器同样能拿
    detection 100%，而它毫无价值。所以这组题判分是**反的**：报警即误报。

    自建题时先自证：断言区间确实没有缺口、查询日不落在任何停牌窗口。
    一道「以为是负例其实有停牌」的题会让门禁永远拿不到满分——错的会是题，
    不是门禁，这种题比没有更糟。
    """
    sym = c["symbol"]
    intent = c.get("intent", "change")
    start = date.fromisoformat(c["start"]) if "start" in c else None
    end = date.fromisoformat(c["end"]) if "end" in c else None
    as_of = date.fromisoformat(c["as_of"]) if "as_of" in c else None

    if start and end:
        gaps = _suspended_trading_days(con, sym, start, end)
        if gaps:
            raise SystemExit(f"{c['id']}: 自称对照组，但 {start}–{end} 有缺口 {gaps}，题造错了")
    if as_of:
        susp = _suspension_of(sym, as_of)
        if susp:
            raise SystemExit(
                f"{c['id']}: 自称对照组，但 {as_of} 落在停牌 {susp['date']}–{susp['end_date']}"
            )

    if intent == "price":
        row = _last_row_before(con, sym, as_of)
        naive_sql = (
            f"SELECT date, close FROM stock_daily WHERE symbol = '{sym}' "
            f"AND date <= DATE '{as_of}' ORDER BY date DESC LIMIT 1"
        )
        naive_result = {"value": float(row[1]), "as_of": row[0].isoformat(), "unit": "元"}
        answer = f"{row[1]} 元（{row[0]}）——当日正常交易，可直接引用。"
    elif intent == "change":
        pts = con.execute(
            "SELECT date, close FROM stock_daily WHERE symbol = ? AND date BETWEEN ? AND ? "
            "ORDER BY date",
            [sym, start, end],
        ).fetchall()
        total = (float(pts[-1][1]) / float(pts[0][1]) - 1) * 100
        naive_sql = (
            f"SELECT (last_close / first_close - 1) * 100 AS pct FROM ("
            f"SELECT (SELECT close FROM stock_daily WHERE symbol='{sym}' "
            f"AND date >= DATE '{start}' ORDER BY date ASC LIMIT 1) AS first_close, "
            f"(SELECT close FROM stock_daily WHERE symbol='{sym}' "
            f"AND date <= DATE '{end}' ORDER BY date DESC LIMIT 1) AS last_close)"
        )
        naive_result = {"value": round(total, 4), "unit": "%"}
        answer = f"{round(total, 2)}%——区间 {len(pts)} 个交易日连续无缺口，可直接引用。"
    else:
        n_rows, sum_vol = con.execute(
            "SELECT count(*), sum(volume) FROM stock_daily "
            "WHERE symbol = ? AND date BETWEEN ? AND ?",
            [sym, start, end],
        ).fetchone()
        naive_sql = (
            f"SELECT count(*) AS days, avg(volume) AS avg_vol FROM stock_daily "
            f"WHERE symbol = '{sym}' AND date BETWEEN DATE '{start}' AND DATE '{end}'"
        )
        naive_result = {
            "days_with_data": int(n_rows),
            "avg_volume": round(float(sum_vol) / int(n_rows), 2),
        }
        answer = f"日均 {naive_result['avg_volume']}（{n_rows} 天无缺口），口径唯一，可直接引用。"

    return {
        "naive_sql": naive_sql,
        "naive_result": naive_result,
        "truth": {"note": "该标的该区间无停牌覆盖，数据完整，口径无歧义"},
        "expected": {
            "must_flag": False,
            "must_mention": [],
            "trusted_answer": answer,
        },
    }


_BUILDERS = {
    "stale_price": _build_stale_price,
    "suspended_window": _build_suspended_window,
    "calendar_mismatch": _build_calendar_mismatch,
    "gap_span": _build_gap_span,
    "clean": _build_clean,
}


def build() -> dict:
    con = _connect()
    cases = []
    for c in CASES:
        builder = _BUILDERS.get(c["trap"])
        if builder is None:
            raise SystemExit(f"{c['id']}: 未知陷阱类型 {c['trap']}")
        body = builder(con, c)
        cases.append(
            {
                "id": c["id"],
                "question": c["question"],
                "trap": c["trap"],
                "symbol": c["symbol"],
                # 显式结构化参数：runner 直接调用门禁用，不必反解自然语言。
                "params": {k: v for k, v in c.items() if k in ("as_of", "start", "end")},
                # 对照组没有陷阱类型可推断意图，必须显式带出去。
                **({"intent": c["intent"]} if "intent" in c else {}),
                **body,
            }
        )
    return {
        "name": "trustbench",
        "version": "0.1.0",
        "description": (
            "「SQL 正确但答案不该引用」评测集。测的不是 SQL 能不能跑通"
            "（naive SQL 全部可执行），而是系统会不会把执行成功当成答案可信。"
        ),
        "source": str(FIXTURE_DIR),
        "generated_by": "scripts/build_trustbench.py",
        "trap_types": TRAP_TYPES,
        "scoring": {
            "execution": "naive_sql 可执行且返回非空——难度不在这层，基线应接近 100%",
            "detection": "是否触发可信警告（提及停牌/陈旧/口径）——裸 ChatBI 应接近 0%",
            "correction": "修正答案是否含 expected.must_mention 全部关键事实",
        },
        "cases": cases,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 TrustBench 题集")
    ap.add_argument("--check", action="store_true", help="只校验已生成题集是否与当前数据一致")
    ap.add_argument("-o", "--out", default=str(OUT_PATH))
    args = ap.parse_args()

    data = build()
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    out = Path(args.out)

    if args.check:
        if not out.exists():
            raise SystemExit(f"题集不存在：{out}")
        if out.read_text(encoding="utf-8") != text:
            raise SystemExit("题集与 fixture 不一致，请重新生成（数字漂移说明事实变了）")
        print(f"[trustbench] 一致：{len(data['cases'])} 题")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[trustbench] 已生成 {len(data['cases'])} 题 → {out}")
    for c in data["cases"]:
        print(f"  {c['id']} [{c['trap']}] {c['question']}")
        print(f"      naive → {c['naive_result']}")


if __name__ == "__main__":
    main()
