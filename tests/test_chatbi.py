"""ChatBI 解析层与门禁的测试。

LLM 路径用确定性桩（MockLLMClient）验证：CI 里没有模型、也不该花钱调真
模型，但**解析协议、白名单校验、降级链路必须能被自动验证**——沿用
agent/llm.py 的做法。桩不是凑数的，它钉的是「模型说错了系统会怎么办」。

门禁用例同时钉住两侧：有停牌必须报警，没停牌必须放行。只测一侧的话，
一个恒真报警器也能全绿。
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from findata.agent.llm import LLMError, MockLLMClient
from findata.chatbi import answer, answer_structured
from findata.chatbi.parser import parse_rule, parse_with_llm
from findata.dq.badges import BadgeLevel

FIXTURE_DIR = "eval/fixtures/replay_20260915"
_TABLES = ("stock_daily", "trading_calendar", "valuation_daily", "stock_universe")


@pytest.fixture()
def con():
    c = duckdb.connect()
    for t in _TABLES:
        c.execute(
            f"CREATE VIEW {t} AS SELECT * "
            f"FROM read_parquet('{FIXTURE_DIR}/{t}.parquet')"
        )
    yield c
    c.close()


# ── 规则解析 ──


def test_rule_parse_single_day_price(con):
    q = parse_rule(con, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert q.symbol == "688981"
    assert q.intent == "price"
    assert q.as_of == date(2025, 9, 4)


def test_rule_parse_range_change(con):
    q = parse_rule(con, "中芯国际从 2025 年 8 月 29 日到 9 月 12 日涨了多少？")
    assert q.symbol == "688981"
    assert q.intent == "change"
    assert q.start == date(2025, 8, 29)
    # 第二个日期缺年份，跟随第一个日期的年份
    assert q.end == date(2025, 9, 12)


def test_rule_parse_month_expands_to_full_month(con):
    q = parse_rule(con, "中国神华 2025 年 8 月的日均成交量是多少？")
    assert q.intent == "aggregate"
    assert q.start == date(2025, 8, 1)
    assert q.end == date(2025, 8, 31)


def test_rule_parse_aggregate_by_turnover_words(con):
    q = parse_rule(con, "中芯国际 2025 年 9 月有多少个交易日有成交？")
    assert q.intent == "aggregate"


def test_rule_parse_does_not_guess_year(con):
    """没有年份可参照时宁可不解析，也不猜一个年份。

    猜错年份会让门禁在错误的区间上判可信，那比解析失败危险得多。
    """
    q = parse_rule(con, "中芯国际 9 月 4 日的收盘价是多少？")
    assert q.as_of is None


# ── LLM 解析：成功、幻觉、不可用 ──


def test_llm_parse_ok(con):
    client = MockLLMClient(
        replies=['{"symbol":"688981","intent":"price","as_of":"2025-09-04",'
                 '"start":null,"end":null}']
    )
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm"
    assert out.parsed.symbol == "688981"
    assert out.parsed.as_of == date(2025, 9, 4)
    assert out.usage_chars > 0


def test_llm_hallucinated_symbol_falls_back(con):
    """模型编了一个池子里没有的代码：不采用，降级到规则解析。"""
    client = MockLLMClient(
        replies=['{"symbol":"999999","intent":"price","as_of":"2025-09-04"}']
    )
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert out.parsed.symbol == "688981"  # 规则解析仍然认得出来


def test_llm_bad_json_falls_back(con):
    client = MockLLMClient(replies=["这不是 JSON，是一段解释"])
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert out.error == "输出不是 JSON"


def test_llm_illegal_intent_falls_back(con):
    client = MockLLMClient(replies=['{"symbol":"688981","intent":"guess","as_of":"2025-09-04"}'])
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert "intent 非法" in out.error


def test_llm_unavailable_falls_back(con):
    """Ollama 没起 / 没配 key：必须降级，绝不能让 demo 崩掉。"""

    class _Dead:
        def complete(self, system: str, user: str) -> str:
            raise LLMError("请求失败")

    out = parse_with_llm(con, _Dead(), "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert out.parsed.symbol == "688981"


def test_llm_null_symbol_falls_back(con):
    """模型认不出标的：拿 null 去查库会在全表上跑，比认不出更危险。"""
    client = MockLLMClient(replies=['{"symbol":null,"intent":"price","as_of":"2025-09-04"}'])
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert out.error == "symbol 缺失"


def test_llm_incomplete_slots_fall_back(con):
    """半个槽比没有更危险：引擎会拿 None 拼 SQL，门禁还会照着错误区间判可信。"""
    client = MockLLMClient(replies=['{"symbol":"688981","intent":"price","as_of":null}'])
    out = parse_with_llm(con, client, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert out.source == "llm->rule"
    assert out.error == "price 缺 as_of"

    client = MockLLMClient(replies=['{"symbol":"688981","intent":"change","start":"2025-08-29"}'])
    out = parse_with_llm(con, client, "中芯国际 2025 年 8 月 29 日到 9 月 12 日涨了多少？")
    assert out.source == "llm->rule"
    assert out.error == "区间缺 start/end"


# ── 门禁两侧 ──


def test_guard_flags_stale_price(con):
    ans = answer_structured(con, symbol="688981", intent="price", as_of=date(2025, 9, 4))
    assert ans.badge is BadgeLevel.CAUTION
    assert "停牌" in ans.guard.message
    assert "stale_price" in ans.guard.traps


def test_guard_flags_zero_pct_gap(con):
    """区间被停牌压成单点，涨跌幅必然 0% —— 必须判不可用。"""
    ans = answer_structured(
        con,
        symbol="600030",
        intent="change",
        start=date(2022, 1, 19),
        end=date(2022, 1, 27),
    )
    assert ans.badge is BadgeLevel.UNUSABLE
    assert not ans.guard.usable


def test_guard_releases_clean_stock(con):
    """对照组：茅台无停牌，必须放行。只测报警侧的门禁是假门禁。"""
    ans = answer_structured(con, symbol="600519", intent="price", as_of=date(2025, 9, 15))
    assert ans.badge is BadgeLevel.BASELINE
    assert ans.guard.traps == ()


def test_guard_releases_clean_window(con):
    """有停牌史的标的，区间不在窗口内也必须放行。"""
    ans = answer_structured(
        con,
        symbol="688981",
        intent="change",
        start=date(2025, 10, 9),
        end=date(2025, 10, 20),
    )
    assert ans.badge is BadgeLevel.BASELINE


def test_natural_language_end_to_end(con):
    """端到端：自然语言进，带徽章的答案出。"""
    ans = answer(con, "中芯国际 2025 年 9 月 4 日的收盘价是多少？")
    assert ans.badge is BadgeLevel.CAUTION
    assert ans.parser == "rule"
    assert ans.value == 114.76
