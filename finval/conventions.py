"""计息约定（day count）与付息频率。"""

from __future__ import annotations

from datetime import date
from enum import Enum


class UnsupportedConventionError(ValueError):
    """输入选择了系统尚不支持的计息或日期约定。"""


class DayCount(str, Enum):
    ACT_360 = "ACT/360"
    ACT_365F = "ACT/365F"
    THIRTY_360 = "30/360"


class Frequency(str, Enum):
    MONTHLY = "P1M"
    QUARTERLY = "P3M"
    SEMIANNUAL = "P6M"
    ANNUAL = "P1Y"

    @property
    def months(self) -> int:
        return {
            Frequency.MONTHLY: 1,
            Frequency.QUARTERLY: 3,
            Frequency.SEMIANNUAL: 6,
            Frequency.ANNUAL: 12,
        }[self]


def is_month_end(d: date) -> bool:
    return d == _last_of_month(d.year, d.month)


def _shift_months(d: date, months: int, snap_eom: bool = True) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    import calendar
    last_of_month = date(year, month, calendar.monthrange(year, month)[1])
    # 输入日为月末（含 2/28 这类）时，结果对齐目标月月末
    if d.day > last_of_month.day or (
        snap_eom and d == _last_of_month(d.year, d.month)
    ):
        return last_of_month
    return date(year, month, d.day)


def _last_of_month(year: int, month: int) -> date:
    import calendar
    return date(year, month, calendar.monthrange(year, month)[1])


def shift_months(d: date, months: int, snap_eom: bool = True) -> date:
    return _shift_months(d, months, snap_eom=snap_eom)


def year_fraction(start: date, end: date, day_count: DayCount) -> float:
    """按选择的计息约定计算 [start, end) 的年化计息因子。"""
    if day_count == DayCount.ACT_360:
        return (end - start).days / 360.0
    if day_count == DayCount.ACT_365F:
        return (end - start).days / 365.0
    if day_count == DayCount.THIRTY_360:
        d1, d2 = start.day, end.day
        d1 = 30 if d1 == 31 else d1
        d2 = 30 if d2 == 31 and d1 == 30 else d2
        return ((end.year - start.year) * 360
                + (end.month - start.month) * 30
                + (d2 - d1)) / 360.0
    raise UnsupportedConventionError(f"暂不支持的计息约定: {day_count!r}")
