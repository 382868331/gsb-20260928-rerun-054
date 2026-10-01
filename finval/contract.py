"""固定利率合约定义。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Tuple

from .calendar import BusinessDayConvention, Calendar
from .conventions import DayCount, Frequency, UnsupportedConventionError


class StubType(str, Enum):
    NONE = "NONE"            # 完整周期，起止不匹配则报错
    SHORT_FIRST = "SHORT_FIRST"
    LONG_FIRST = "LONG_FIRST"
    SHORT_LAST = "SHORT_LAST"
    LONG_LAST = "LONG_LAST"


@dataclass(frozen=True)
class FixedRateContract:
    contract_id: str
    notional: float
    fixed_rate: float
    start_date: date
    end_date: date
    frequency: Frequency = Frequency.SEMIANNUAL
    day_count: DayCount = DayCount.ACT_360
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    calendar: Calendar = None
    eom: bool = True                # 起息日为月末时，各周期日也落在月末
    stub: StubType = StubType.SHORT_FIRST
    notional_exchange: bool = False  # 期末是否兑付名义本金
    events: Tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.day_count, DayCount):
            raise UnsupportedConventionError(
                f"未知计息约定: {self.day_count!r}，支持: {[d.value for d in DayCount]}")
        if not isinstance(self.business_day_convention, BusinessDayConvention):
            raise UnsupportedConventionError(
                f"未知营业日约定: {self.business_day_convention!r}")
        if not isinstance(self.frequency, Frequency):
            raise UnsupportedConventionError(f"未知付息频率: {self.frequency!r}")
        if not isinstance(self.stub, StubType):
            raise UnsupportedConventionError(f"未知残期类型: {self.stub!r}")
        if self.end_date <= self.start_date:
            raise ValueError("到期日必须晚于起息日")
        if self.notional <= 0:
            raise ValueError("名义本金必须为正")
        if self.calendar is None:
            object.__setattr__(self, "calendar", Calendar())
        object.__setattr__(self, "events", tuple(self.events or ()))
