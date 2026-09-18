"""查询结果的可信门禁：把「停牌」这类业务事实接到 ChatBI 的答案出口。

为什么需要单独这一层：ChatBI 的执行成功不等于答案可信。SQL 跑通只说明
语法对、有返回行，而停牌会让「存在的数字」变成「误导的数字」——

  · 问停牌日的收盘价，SQL 顺延返回停牌前的价格，且不说明它陈旧；
  · 问跨越停牌的区间涨跌幅，首尾相减把复牌跳空抹平，看起来像连续走势；
  · 问停牌区间的聚合值，分母被静默缩小，同一个数字能得出两个结论。

这一层不改写查询，只负责在**答案出口**拦一道，把业务事实翻译成人话。
它与 dq/triage.py 同源（同样用公司事件做归因），但作用点不同：triage 决定
告警该不该报，本层决定答案能不能被引用。

事实源优先级：仓库 corporate_event 表（生产）→ 停牌种子（fixture/回放）。
两者必须同源——种子就是 corporate_event 的确定性重建，这里不新造口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from findata.domains.finance.seeds import SUSPENSION_SEEDS
from findata.dq.badges import BADGE_MARK, BadgeLevel

# 陷阱类型与 eval/golden/trustbench.yaml::trap_types 一一对应，
# benchmark 的断言直接按这些 key 匹配，改名必须同步改题集。
TRAP_STALE_PRICE = "stale_price"
TRAP_SUSPENDED_WINDOW = "suspended_window"
TRAP_CALENDAR_MISMATCH = "calendar_mismatch"
TRAP_GAP_SPAN = "gap_span"


@dataclass(frozen=True)
class Suspension:
    """一段已知停牌。detail 是公告级来源的事实描述，不是模型编的。"""

    symbol: str
    start: date
    end: date
    detail: str


@dataclass(frozen=True)
class GuardResult:
    badge: BadgeLevel
    traps: tuple[str, ...]
    message: str
    evidence: tuple[str, ...]

    @property
    def mark(self) -> str:
        return BADGE_MARK[self.badge]

    @property
    def usable(self) -> bool:
        """✗ 不可用之外都算可引用，但 ⚠️ 必须带上警告一起引用。"""
        return self.badge is not BadgeLevel.UNUSABLE


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def load_suspensions(con) -> list[Suspension]:
    """读停牌事实。生产库走 corporate_event；回放 fixture 里没有这张表，
    回退到停牌种子（保证 benchmark 在 CI 里可复现）。"""
    try:
        rows = con.execute(
            "SELECT symbol, date, end_date, detail FROM corporate_event WHERE kind = 'suspension'"
        ).fetchall()
    except Exception:
        rows = []
    if rows:
        out = []
        for sym, d0, d1, detail in rows:
            out.append(
                Suspension(str(sym), _as_date(d0), _as_date(d1 or d0), (detail or "").strip())
            )
        return out
    return [
        Suspension(
            s["symbol"],
            s["date"],
            s["end_date"],
            s["detail"].split("来源")[0].strip(),
        )
        for s in SUSPENSION_SEEDS
    ]


def _trading_days_between(con, lo: date, hi: date) -> int:
    try:
        return con.execute(
            "SELECT count(*) FROM trading_calendar WHERE date > ? AND date <= ?", [lo, hi]
        ).fetchone()[0]
    except Exception:
        return max((hi - lo).days * 5 // 7, 0)  # 无日历时粗估，宁可不准也不要崩


def guard_query(
    con,
    *,
    symbol: str,
    intent: str = "price",
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
    computed_start: date | None = None,
    computed_end: date | None = None,
    n_points: int | None = None,
) -> GuardResult:
    """判定一个查询结果能不能被引用。

    intent: price（取某日价格）/ change（区间涨跌幅）/ aggregate（聚合计数）。
    computed_* 与 n_points 是 SQL 实际取到的区间与点数——用它们才能识破
    「区间被停牌压成一个点、涨跌幅必然 0%」这类陷阱。
    """
    susp = [s for s in load_suspensions(con) if s.symbol == symbol]
    if not susp:
        return GuardResult(
            BadgeLevel.BASELINE,
            (),
            "未查到该标的的停牌记录，通用检查通过（不等于已核验）。",
            (),
        )

    lo = start or as_of
    hi = end or as_of
    if lo is None or hi is None:
        return GuardResult(BadgeLevel.BASELINE, (), "未指定时间范围，无法做健康核验。", ())

    hit = [s for s in susp if not (s.end < lo or s.start > hi)]
    if not hit:
        return GuardResult(
            BadgeLevel.BASELINE,
            (),
            f"{lo}–{hi} 未覆盖已知停牌区间，通用检查通过（不等于已核验）。",
            (),
        )

    evidence = tuple(f"{s.symbol} {s.start}–{s.end}：{s.detail}" for s in hit)

    # 区间被压成单点：涨跌幅必然是 0%，这个数字没有意义，直接判不可用。
    if n_points is not None and n_points <= 1 and intent == "change":
        return GuardResult(
            BadgeLevel.UNUSABLE,
            (TRAP_GAP_SPAN,),
            (
                f"{lo} 起停牌至 {hi} 前，该区间只剩 {computed_start} 一个数据点，"
                f"涨跌幅必然为 0%——不是没有波动，是没有数据可比。"
            ),
            evidence,
        )

    # 查某日价格、且该日落在停牌期内：SQL 会顺延返回停牌前的价格。
    if intent == "price" and as_of is not None:
        for s in hit:
            if s.start <= as_of <= s.end:
                stale = _trading_days_between(con, computed_start or s.start, as_of)
                return GuardResult(
                    BadgeLevel.CAUTION,
                    (TRAP_STALE_PRICE,),
                    (
                        f"{as_of} 处于停牌期（{s.start}–{s.end}），无当日行情；"
                        f"返回的是停牌前最后一个交易日 {computed_start} 的价格，"
                        f"已滞后 {stale} 个交易日。"
                    ),
                    evidence,
                )

    # 聚合类：停牌日没有数据行，分母被静默缩小。
    if intent == "aggregate":
        return GuardResult(
            BadgeLevel.CAUTION,
            (TRAP_CALENDAR_MISMATCH,),
            (
                f"{lo}–{hi} 覆盖停牌区间（{hit[0].start}–{hit[0].end}），"
                f"停牌日不产生数据行：『日均』与『有成交天数』都存在两种口径"
                f"（按有成交日算 vs 按全部交易日算），必须写明用哪个。"
            ),
            evidence,
        )

    # 区间涨跌幅跨越停牌：首尾相减会抹平复牌跳空。
    # 必须把停牌起止日期写进警示——只说「有停牌」是不可核实的，
    # benchmark 的 must_mention 正是在钉这一条。
    windows = "、".join(f"{s.start}–{s.end}" for s in hit)
    days = sum(_trading_days_between(con, s.start, s.end) + 1 for s in hit)
    return GuardResult(
        BadgeLevel.CAUTION,
        (TRAP_SUSPENDED_WINDOW,),
        (
            f"{lo}–{hi} 跨越停牌区间（{windows}，共 {days} 个交易日无行情），"
            f"首尾价直接相减会把复牌跳空当成连续走势——这个数字不能单独引用。"
        ),
        evidence,
    )
