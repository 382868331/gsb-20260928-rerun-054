from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum


class DayCountConvention(Enum):
    ACT_365_FIXED = "ACT/365F"
    ACT_360 = "ACT/360"
    THIRTY_360 = "30/360"
    THIRTY_E_360 = "30E/360"


class UnsupportedConventionError(ValueError):
    pass


def year_fraction(start: date, end: date, convention: DayCountConvention) -> Decimal:
    if end <= start:
        raise ValueError("Accrual end must be after accrual start")
    if convention is DayCountConvention.ACT_365_FIXED:
        return Decimal((end - start).days) / Decimal("365")
    if convention is DayCountConvention.ACT_360:
        return Decimal((end - start).days) / Decimal("360")
    if convention in (DayCountConvention.THIRTY_360, DayCountConvention.THIRTY_E_360):
        d1, d2 = _thirty_360_days(start, end, convention)
        days = 360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1
        return Decimal(days) / Decimal("360")
    raise UnsupportedConventionError(f"Unsupported day-count convention: {convention}")


def _thirty_360_days(start: date, end: date, convention: DayCountConvention) -> tuple[int, int]:
    d1 = start.day
    d2 = end.day
    if convention is DayCountConvention.THIRTY_E_360:
        return min(d1, 30), min(d2, 30)
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30
    return d1, d2
