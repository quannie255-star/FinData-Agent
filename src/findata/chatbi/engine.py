"""最小 ChatBI 骨架：问题 → SQL → 执行 → 可信门禁 → 带徽章答案。

刻意做薄的原因：对话式 BI 的骨架（取数 + 出数）已经是红海，Databao /
WrenAI / JimuChatBI 都比从零写好。这一层存在的唯一目的是**让门禁有地方
挂**——差异化全在 guard.py，这里不堆功能、不做前端。

两个设计约束：
1. SQL 刻意写成「裸 ChatBI 会生成的样子」，不套本项目语义层的指标字典
   ——骨架要能复现**别人的**盲区，拿自家编译器当基线等于自己送分；
2. 解析可插拔（规则 / LLM），但无论哪条路，**门禁都是同一套规则**。
   解析错了顶多答非所问，门禁错了才是把不可信的数字放出去。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from findata.agentops.schema import STATUS_DEGRADED, STATUS_OK, Trace
from findata.chatbi.guard import GuardResult, guard_query
from findata.chatbi.parser import ParseOutcome, parse_rule, parse_with_llm
from findata.dq.badges import BadgeLevel


@dataclass(frozen=True)
class Answer:
    question: str
    sql: str
    value: float | None
    unit: str
    as_of: date | None
    guard: GuardResult
    parser: str = "rule"
    usage_chars: int = 0
    parse_error: str = ""

    @property
    def badge(self) -> BadgeLevel:
        return self.guard.badge

    @property
    def mark(self) -> str:
        return self.guard.mark

    def render(self, show_cost: bool = False) -> str:
        """给用户的完整答案：数字 + 徽章 + 警示（有陷阱时警示必须同屏出现）。"""
        num = f"{self.value}{self.unit}" if self.value is not None else "无数据"
        head = f"{num}（{self.as_of}）" if self.as_of else num
        lines = [f"{head}　{self.mark}"]
        if self.guard.message:
            lines.append(f"　└ {self.guard.message}")
        if not self.guard.usable:
            lines.append("　└ 该数字不建议引用。")
        if show_cost:
            # 成本如实入账：用了 LLM 就把往返字符数摆出来，降级也要标出来。
            tail = f"　└ 解析：{self.parser}"
            if self.usage_chars:
                tail += f"（{self.usage_chars} 字符往返）"
            if self.parse_error:
                tail += f"｜降级原因：{self.parse_error}"
            lines.append(tail)
        return "\n".join(lines)


def answer_structured(
    con,
    *,
    symbol: str,
    intent: str = "price",
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
    question: str = "",
    trace: Trace | None = None,
) -> Answer:
    """结构化入口：benchmark 与程序化调用走这里，不经过自然语言解析。"""
    if intent == "price":
        sql = (
            f"SELECT date, close FROM stock_daily WHERE symbol = '{symbol}' "
            f"AND date <= DATE '{as_of}' ORDER BY date DESC LIMIT 1"
        )
        row = con.execute(sql).fetchone()
        value = float(row[1]) if row else None
        got_as_of = row[0] if row else None
        unit, n_points = " 元", None if row is None else 1
        computed_start = got_as_of
    elif intent == "change":
        # 先问清区间里到底有几个数据点、端点各是哪天——停牌会把区间压扁，
        # 门禁要靠真实端点识破「涨跌幅 0%」这类陷阱，不能拿查询入参冒充。
        span = con.execute(
            f"SELECT min(date), max(date), count(*) FROM stock_daily "
            f"WHERE symbol = '{symbol}' AND date BETWEEN DATE '{start}' AND DATE '{end}'"
        ).fetchone()
        sql = (
            f"SELECT (last_close / first_close - 1) * 100 AS pct FROM ("
            f"SELECT (SELECT close FROM stock_daily WHERE symbol='{symbol}' "
            f"AND date >= DATE '{start}' ORDER BY date ASC LIMIT 1) AS first_close, "
            f"(SELECT close FROM stock_daily WHERE symbol='{symbol}' "
            f"AND date <= DATE '{end}' ORDER BY date DESC LIMIT 1) AS last_close)"
        )
        row = con.execute(sql).fetchone()
        value = round(float(row[0]), 4) if row and row[0] is not None else None
        n_points = int(span[2]) if span and span[2] is not None else 0
        computed_start = span[0] if span and span[0] else start
        got_as_of = span[1] if span and span[1] else end
        unit = "%"
    else:  # aggregate
        sql = (
            f"SELECT count(*) AS days, avg(volume) AS avg_vol FROM stock_daily "
            f"WHERE symbol = '{symbol}' AND date BETWEEN DATE '{start}' AND DATE '{end}'"
        )
        row = con.execute(sql).fetchone()
        value = round(float(row[1]), 2) if row and row[1] is not None else None
        n_points = int(row[0]) if row else 0
        unit, got_as_of, computed_start = " 手（日均）", end, start

    if trace is not None:
        # 先落 build/execute，再落 guard——顺序就是真实执行顺序，
        # 归因时要能看出"是取数错了还是判定错了"。
        trace.step(
            "build_sql",
            arguments={
                "symbol": symbol,
                "intent": intent,
                "start": str(start) if start else None,
                "end": str(end) if end else None,
                "as_of": str(as_of) if as_of else None,
            },
            result={"sql": sql},
        )
        trace.step(
            "execute_sql",
            arguments={"sql": sql},
            result={
                "value": value,
                "unit": unit,
                "as_of": str(got_as_of) if got_as_of else None,
                "n_points": n_points,
            },
        )

    guard = guard_query(
        con,
        symbol=symbol,
        intent=intent,
        start=start,
        end=end,
        as_of=as_of,
        computed_start=computed_start,
        computed_end=got_as_of,
        n_points=n_points,
    )
    if trace is not None:
        trace.step(
            "trust_check",
            arguments={
                "symbol": symbol,
                "intent": intent,
                "computed_start": str(computed_start) if computed_start else None,
                "computed_end": str(got_as_of) if got_as_of else None,
                "n_points": n_points,
            },
            result={
                "badge": guard.badge.name,
                "mark": guard.mark,
                "usable": guard.usable,
                "traps": list(guard.traps),
                "message": guard.message,
                "evidence": list(guard.evidence),
            },
        )
        trace.finish(value=value, unit=unit, badge=guard.badge.name, usable=guard.usable)

    return Answer(
        question=question,
        sql=sql,
        value=value,
        unit=unit,
        as_of=got_as_of,
        guard=guard,
        parser="structured",
    )


def answer(con, question: str, client=None, trace: Trace | None = None) -> Answer:
    """自然语言入口。client 为 None 走规则解析（离线可用），
    传入 LLM 客户端则先让模型填槽、校验失败自动降级。

    trace 不为 None 时把每一步工具调用落进轨迹，供下游探针与归因消费。
    """
    if client is None:
        outcome = ParseOutcome(parse_rule(con, question), "rule")
    else:
        outcome = parse_with_llm(con, client, question)

    q = outcome.parsed
    if trace is not None:
        trace.parser = outcome.source
        trace.step(
            "parse_query",
            arguments={"question": question, "mode": outcome.source},
            result={
                "symbol": q.symbol,
                "intent": q.intent,
                "start": str(q.start) if q.start else None,
                "end": str(q.end) if q.end else None,
                "as_of": str(q.as_of) if q.as_of else None,
                # 模型原文一并落盘：归因要讲证据，不能靠"大概是模型没填"。
                "raw": outcome.raw,
            },
            # 降级不是失败：护栏生效、链路仍继续。单独一档，别并进 error。
            status=STATUS_DEGRADED if outcome.source.endswith("->rule") else STATUS_OK,
            error=outcome.error,
            usage_chars=outcome.usage_chars,
        )

    if q.symbol is None:
        if trace is not None:
            trace.finish(error="symbol_unresolved")
        raise ValueError(f"没能从问题里认出标的：{question}")

    # 槽位完整性必须在拼 SQL 之前拦住。规则解析遇到「9 月 4 号」这类写法会
    # 退化成整月区间（as_of 为 None），带着 None 去拼 SQL 是 duckdb 的
    # ConversionException——对用户那是天书。认不出就说认不出。
    if q.intent == "price" and q.as_of is None:
        if trace is not None:
            trace.finish(error="slot_incomplete:as_of")
        raise ValueError(f"没能从问题里认出日期（需要年月日）：{question}")
    if q.intent != "price" and (q.start is None or q.end is None):
        if trace is not None:
            trace.finish(error="slot_incomplete:range")
        raise ValueError(f"没能从问题里认出完整区间（需要起止两端）：{question}")

    ans = answer_structured(
        con,
        symbol=q.symbol,
        intent=q.intent,
        start=q.start,
        end=q.end,
        as_of=q.as_of,
        question=question,
        trace=trace,
    )
    return Answer(
        question=ans.question,
        sql=ans.sql,
        value=ans.value,
        unit=ans.unit,
        as_of=ans.as_of,
        guard=ans.guard,
        parser=outcome.source,
        usage_chars=outcome.usage_chars,
        parse_error=outcome.error,
    )
