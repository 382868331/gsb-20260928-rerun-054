"""营业日日历与日期调整约定。"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum


class BusinessDayConvention(str, Enum):
    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    PRECEDING = "PRECEDING"


class Calendar:
    """以周末为非营业日、可附加假日的日历。"""

    def __init__(self, name: str = "WEEKEND", holidays=None) -> None:
        self.name = name
        self.holidays = set(holidays or ())

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def adjust(self, d: date, convention: BusinessDayConvention) -> date:
        if convention == BusinessDayConvention.UNADJUSTED:
            return d
        if convention == BusinessDayConvention.FOLLOWING:
            return self._following(d)
        if convention == BusinessDayConvention.PRECEDING:
            return self._preceding(d)
        if convention == BusinessDayConvention.MODIFIED_FOLLOWING:
            adjusted = self._following(d)
            if adjusted.month != d.month:
                return self._preceding(d)
            return adjusted
        raise ValueError(f"未知营业日约定: {convention!r}")

    def _following(self, d: date) -> date:
        while not self.is_business_day(d):
            d = d + timedelta(days=1)
        return d

    def _preceding(self, d: date) -> date:
        while not self.is_business_day(d):
            d = d - timedelta(days=1)
        return d

    def subtract_business_days(self, d: date, n: int) -> date:
        """返回 d 之前第 n 个营业日（d 本身不计入）。"""
        while n > 0:
            d = d - timedelta(days=1)
            if self.is_business_day(d):
                n -= 1
        return d

    def add_holidays(self, dates) -> "Calendar":
        self.holidays.update(dates)
        return self
