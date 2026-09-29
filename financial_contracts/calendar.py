from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class BusinessDayConvention(Enum):
    UNADJUSTED = "unadjusted"
    FOLLOWING = "following"
    MODIFIED_FOLLOWING = "modified_following"
    PRECEDING = "preceding"
    MODIFIED_PRECEDING = "modified_preceding"


@dataclass(frozen=True)
class BusinessCalendar:
    holidays: frozenset[date] = frozenset()
    weekend_weekdays: frozenset[int] = frozenset({5, 6})

    def __post_init__(self) -> None:
        if not self.weekend_weekdays:
            raise ValueError("weekend_weekdays must not be empty")
        if not (0 <= min(self.weekend_weekdays) and max(self.weekend_weekdays) <= 6):
            raise ValueError("weekend_weekdays must use Python weekday values from 0 to 6")

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_weekdays and day not in self.holidays

    def adjust(self, day: date, convention: BusinessDayConvention) -> date:
        if convention is BusinessDayConvention.UNADJUSTED or self.is_business_day(day):
            return day
        if convention is BusinessDayConvention.FOLLOWING:
            return self._next_business_day(day)
        if convention is BusinessDayConvention.PRECEDING:
            return self._previous_business_day(day)
        if convention is BusinessDayConvention.MODIFIED_FOLLOWING:
            adjusted = self._next_business_day(day)
            return self._previous_business_day(day) if adjusted.month != day.month else adjusted
        if convention is BusinessDayConvention.MODIFIED_PRECEDING:
            adjusted = self._previous_business_day(day)
            return self._next_business_day(day) if adjusted.month != day.month else adjusted
        raise UnsupportedBusinessDayConventionError(f"Unsupported business day convention: {convention}")

    def _next_business_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while not self.is_business_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def _previous_business_day(self, day: date) -> date:
        candidate = day - timedelta(days=1)
        while not self.is_business_day(candidate):
            candidate -= timedelta(days=1)
        return candidate


class UnsupportedBusinessDayConventionError(ValueError):
    pass
