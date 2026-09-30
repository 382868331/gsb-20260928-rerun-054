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

    def add_holidays(self, dates) -> "Calendar":
        self.holidays.update(dates)
        return self

    def shift_business_days(self, d: date, offset: int) -> date:
        """按营业日平移 offset 个工作日；offset 为负时向前取，落点始终为营业日。"""
        step_days = timedelta(days=1 if offset >= 0 else -1)
        target = abs(offset)
        moved = 0
        while moved < target:
            d = d + step_days
            if self.is_business_day(d):
                moved += 1
        return d
