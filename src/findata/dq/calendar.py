"""交易日历。

为什么需要它：判断数据「停更」必须用交易日而不是自然日。
周五收盘后到周一开盘隔了 3 个自然日、0 个交易日，用自然日判断会把每个周末
都报成故障——这正是告警疲劳的来源之一。

同理，停牌导致的缺失和采集失败导致的缺失，在数据形态上完全一样，
只有结合交易日历 + 公司事件才能区分。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, timedelta


def _span(start: str, end: str) -> Iterator[date]:
    """闭区间日期序列，便于声明式地写节假日。"""
    cur = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while cur <= stop:
        yield cur
        cur += timedelta(days=1)


# 2024-2025 法定节假日（国务院公布口径）。
# 生产环境应接入真实交易日历（如 akshare tool_trade_date_hist_sina），
# 这里硬编码是为了让评测语料完全确定性、可复现。
_DEFAULT_HOLIDAYS: frozenset[date] = frozenset(
    [
        *_span("2024-01-01", "2024-01-01"),  # 元旦
        *_span("2024-02-10", "2024-02-17"),  # 春节
        *_span("2024-04-04", "2024-04-06"),  # 清明
        *_span("2024-05-01", "2024-05-05"),  # 劳动节
        *_span("2024-06-08", "2024-06-10"),  # 端午
        *_span("2024-09-15", "2024-09-17"),  # 中秋
        *_span("2024-10-01", "2024-10-07"),  # 国庆
        *_span("2025-01-01", "2025-01-01"),  # 元旦
        *_span("2025-01-28", "2025-02-04"),  # 春节
        *_span("2025-04-04", "2025-04-06"),  # 清明
        *_span("2025-05-01", "2025-05-05"),  # 劳动节
        *_span("2025-05-31", "2025-06-02"),  # 端午
        *_span("2025-10-01", "2025-10-08"),  # 国庆中秋
    ]
)


class TradingCalendar:
    """交易日判定与交易日计数。"""

    __slots__ = ("_holidays", "_extra")

    def __init__(
        self,
        holidays: Iterable[date] = (),
        extra_trading_days: Iterable[date] = (),
    ) -> None:
        self._holidays: frozenset[date] = frozenset(holidays)
        # 调休日：法定节假日里实际开市的周六日，或硬编码表未覆盖到的历史交易日。
        self._extra: frozenset[date] = frozenset(extra_trading_days)

    @classmethod
    def default(cls) -> TradingCalendar:
        return cls(_DEFAULT_HOLIDAYS)

    @property
    def holidays(self) -> frozenset[date]:
        return self._holidays

    def is_trading_day(self, day: date) -> bool:
        if day in self._extra:
            return True
        return day.weekday() < 5 and day not in self._holidays

    def with_observed(self, days: Iterable[date]) -> TradingCalendar:
        """用真实数据里出现过的日期扩充日历。

        硬编码节假日表只覆盖有限年份，而生产库常有更早的历史数据。
        仓库里真的出现过行情的日期必然是交易日，把它并进来，
        可以避免把旧数据误判成"非交易日的幽灵数据"。
        """
        return TradingCalendar(self._holidays, self._extra | frozenset(days))

    def trading_days(self, start: date, end: date) -> list[date]:
        """闭区间内的全部交易日。"""
        days: list[date] = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                days.append(cur)
            cur += timedelta(days=1)
        return days

    def prev_trading_day(self, day: date) -> date:
        """返回早于 day 的最近交易日。"""
        cur = day - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, day: date) -> date:
        cur = day + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def expected_last_date(self, asof: date) -> date:
        """T+1 数据源在 asof 时点应当到达的最后数据日期。

        当天数据通常收盘后才出，所以观察日当天不要求数据已到。
        不做这一步修正，每周一都会把全库报成停更。
        """
        return self.prev_trading_day(asof)

    def staleness(self, last_data_date: date, asof: date) -> int:
        """数据最后日期落后了几个交易日，0 表示未停更。"""
        expected = self.expected_last_date(asof)
        if last_data_date >= expected:
            return 0
        return len(self.trading_days(last_data_date + timedelta(days=1), expected))
