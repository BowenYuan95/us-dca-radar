#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYSE 交易日历适配器。

优先使用成熟库 exchange_calendars（XNYS）；库不可用或超出其支持范围时，
回退到内置的 2017–2028 表。二者都不覆盖的年份抛 CalendarRangeError，
绝不静默按「周一至周五」退化。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("Australia/Melbourne")

CALENDAR_MIN_YEAR = 2017
CALENDAR_MAX_YEAR = 2028


class CalendarRangeError(ValueError):
    """请求日期超出所有日历后端的支持范围。"""


# 内置 NYSE 休市日（2017–2028）。2029 起必须升级/扩展，不允许 weekday 退化。
NYSE_HOLIDAYS = {
    # 2017
    date(2017, 1, 2), date(2017, 1, 16), date(2017, 2, 20), date(2017, 4, 14),
    date(2017, 5, 29), date(2017, 7, 4), date(2017, 9, 4), date(2017, 11, 23),
    date(2017, 12, 25),
    # 2018
    date(2018, 1, 1), date(2018, 1, 15), date(2018, 2, 19), date(2018, 3, 30),
    date(2018, 5, 28), date(2018, 7, 4), date(2018, 9, 3), date(2018, 11, 22),
    date(2018, 12, 5), date(2018, 12, 25),
    # 2019
    date(2019, 1, 1), date(2019, 1, 21), date(2019, 2, 18), date(2019, 4, 19),
    date(2019, 5, 27), date(2019, 7, 4), date(2019, 9, 2), date(2019, 11, 28),
    date(2019, 12, 25),
    # 2020
    date(2020, 1, 1), date(2020, 1, 20), date(2020, 2, 17), date(2020, 4, 10),
    date(2020, 5, 25), date(2020, 7, 3), date(2020, 9, 7), date(2020, 11, 26),
    date(2020, 12, 25),
    # 2021
    date(2021, 1, 1), date(2021, 1, 18), date(2021, 2, 15), date(2021, 4, 2),
    date(2021, 5, 31), date(2021, 7, 5), date(2021, 9, 6), date(2021, 11, 25),
    date(2021, 12, 24),
    # 2022
    date(2022, 1, 17), date(2022, 2, 21), date(2022, 4, 15), date(2022, 5, 30),
    date(2022, 6, 20), date(2022, 7, 4), date(2022, 9, 5), date(2022, 11, 24),
    date(2022, 12, 26),
    # 2023
    date(2023, 1, 2), date(2023, 1, 16), date(2023, 2, 20), date(2023, 4, 7),
    date(2023, 5, 29), date(2023, 6, 19), date(2023, 7, 4), date(2023, 9, 4),
    date(2023, 11, 23), date(2023, 12, 25),
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025（含 1/9 卡特国葬）
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
    # 2028
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
    date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
    date(2028, 12, 25),
}

# 内置 NYSE 提前收盘日（13:00 ET）。
EARLY_CLOSES = {
    # 2017–2021（历史回放用；已知官方日期）
    date(2017, 11, 24),
    date(2018, 7, 3), date(2018, 11, 23), date(2018, 12, 24),
    date(2019, 7, 3), date(2019, 11, 29), date(2019, 12, 24),
    date(2020, 11, 27), date(2020, 12, 24),
    date(2021, 11, 26),
    # 2022–2028
    date(2022, 11, 25),
    date(2023, 7, 3), date(2023, 11, 24),
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
    date(2028, 7, 3), date(2028, 11, 24),
}


class BuiltinCalendar:
    source = "builtin"

    def supports(self, d):
        return CALENDAR_MIN_YEAR <= d.year <= CALENDAR_MAX_YEAR

    def _require(self, d):
        if not self.supports(d):
            raise CalendarRangeError(
                f"{d.isoformat()} 超出内置 NYSE 日历支持范围 "
                f"{CALENDAR_MIN_YEAR}–{CALENDAR_MAX_YEAR}；请安装/升级 exchange_calendars 或扩展内置表")

    def is_trading_day(self, d):
        self._require(d)
        return d.weekday() < 5 and d not in NYSE_HOLIDAYS

    def sessions(self, start, end):
        self._require(start)
        self._require(end)
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                yield cur
            cur += timedelta(days=1)

    def session_open(self, d):
        self._require(d)
        return datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET_TZ)

    def session_close(self, d):
        self._require(d)
        hour = 13 if d in EARLY_CLOSES else 16
        return datetime(d.year, d.month, d.day, hour, 0, tzinfo=ET_TZ)


class ExchangeCalendarsAdapter:
    source = "exchange_calendars"

    def __init__(self, cal):
        self.cal = cal
        self.first = cal.first_session.date()
        self.last = cal.last_session.date()

    def supports(self, d):
        return self.first <= d <= self.last

    def _require(self, d):
        if not self.supports(d):
            raise CalendarRangeError(
                f"{d.isoformat()} 超出 exchange_calendars(XNYS) 支持范围 "
                f"{self.first}–{self.last}")

    def is_trading_day(self, d):
        self._require(d)
        import pandas as pd
        return bool(self.cal.is_session(pd.Timestamp(d)))

    def sessions(self, start, end):
        self._require(start)
        self._require(end)
        import pandas as pd
        idx = self.cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        for ts in idx:
            yield ts.date()

    def session_open(self, d):
        self._require(d)
        import pandas as pd
        return self.cal.session_open(pd.Timestamp(d)).tz_convert(ET_TZ).to_pydatetime()

    def session_close(self, d):
        self._require(d)
        import pandas as pd
        return self.cal.session_close(pd.Timestamp(d)).tz_convert(ET_TZ).to_pydatetime()


class CalendarService:
    """按日期选择 exchange_calendars 或内置表，并明确报告来源。"""

    def __init__(self, prefer_library=True):
        self.builtin = BuiltinCalendar()
        self.library = None
        self.library_error = ""
        if prefer_library:
            try:
                import exchange_calendars as xcals
                self.library = ExchangeCalendarsAdapter(xcals.get_calendar("XNYS"))
            except Exception as exc:  # noqa: BLE001
                self.library_error = str(exc)

    def supports(self, d):
        return (self.library is not None and self.library.supports(d)) or self.builtin.supports(d)

    def source_for(self, d):
        if self.library is not None and self.library.supports(d):
            return self.library.source
        if self.builtin.supports(d):
            return self.builtin.source
        return "unsupported"

    def _backend(self, d):
        if self.library is not None and self.library.supports(d):
            return self.library
        if self.builtin.supports(d):
            return self.builtin
        raise CalendarRangeError(
            f"{d.isoformat()} 超出所有 NYSE 日历后端；不给交易建议。"
            + (f" 库错误：{self.library_error}" if self.library_error else ""))

    def is_trading_day(self, d):
        return self._backend(d).is_trading_day(d)

    def sessions(self, start, end):
        # 允许区间跨后端边界：逐日选择，但区间两端都必须受支持。
        if not self.supports(start) or not self.supports(end):
            raise CalendarRangeError(f"日历区间 {start}–{end} 超出支持范围")
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                yield cur
            cur += timedelta(days=1)

    def session_open(self, d):
        return self._backend(d).session_open(d)

    def session_close(self, d):
        return self._backend(d).session_close(d)

    def next_trading_day(self, d):
        cur = d + timedelta(days=1)
        for _ in range(30):
            if self.supports(cur) and self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise CalendarRangeError(f"{d} 之后 30 天内找不到交易日")

    def prev_trading_day(self, d):
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.supports(cur) and self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise CalendarRangeError(f"{d} 之前 30 天内找不到交易日")

    def last_trading_day_on_or_before(self, d):
        cur = d
        for _ in range(30):
            if self.supports(cur) and self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise CalendarRangeError(f"{d} 之前 30 天内找不到交易日")

    def first_trading_day_of_month(self, d):
        cur = date(d.year, d.month, 1)
        for _ in range(31):
            if self.supports(cur) and self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise CalendarRangeError(f"{d.year}-{d.month:02d} 没有交易日")

    def last_trading_day_of_month(self, d):
        cur = date(d.year + (d.month == 12), (d.month % 12) + 1, 1) - timedelta(days=1)
        for _ in range(31):
            if self.supports(cur) and self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise CalendarRangeError(f"{d.year}-{d.month:02d} 没有交易日")

    def month_trading_days(self, d):
        return list(self.sessions(self.first_trading_day_of_month(d),
                                  self.last_trading_day_of_month(d)))


CALENDAR = CalendarService()
