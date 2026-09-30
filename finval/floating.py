"""浮动利率合约与定盘管理。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping

from .calendar import BusinessDayConvention, Calendar
from .conventions import DayCount, Frequency, UnsupportedConventionError
from .contract import StubType
from .events import FixingCorrection, PartialTermination


class MissingFixingError(ValueError):
    """估值时某计息期已经定盘，但缺少已知历史定盘值。

    禁止用预测曲线悄悄替代已定盘（或应已定盘）的利率。
    """


@dataclass(frozen=True)
class FloatingRateContract:
    contract_id: str
    notional: float
    start_date: date
    end_date: date
    frequency: Frequency = Frequency.QUARTERLY
    day_count: DayCount = DayCount.ACT_360
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    calendar: Calendar = None
    eom: bool = True
    stub: StubType = StubType.SHORT_FIRST
    notional_exchange: bool = False
    index_name: str = "FLOAT"          # 基准利率名称，如 3M-LPR / SOFR
    fixing_lag_business_days: int = 2  # 定盘日相对计息起息日的营业日滞后
    spread: float = 0.0                # 加点（年化，简单加到定盘/远期利率上）
    fixings: Mapping[date, float] = field(default_factory=dict)
    fixing_corrections: tuple = ()     # 历史定盘更正事件
    partial_terminations: tuple = ()   # 已发生的部分提前终止事件

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
        if self.fixing_lag_business_days < 0:
            raise ValueError("定盘滞后营业日数不能为负")
        if self.calendar is None:
            object.__setattr__(self, "calendar", Calendar())
        object.__setattr__(self, "fixings", dict(self.fixings or {}))
        object.__setattr__(
            self, "fixing_corrections", tuple(self.fixing_corrections or ()))
        object.__setattr__(
            self, "partial_terminations", tuple(self.partial_terminations or ()))

    def fixing_date(self, accrual_start: date) -> date:
        """计息期定盘日：计息起息日往前推 fixing_lag_business_days 个营业日。"""
        return self.calendar.subtract_business_days(
            accrual_start, self.fixing_lag_business_days)

    def known_fixing(self, fixing_date: date) -> float | None:
        return self.fixings.get(fixing_date)

    def effective_fixing(self, fixing_date: date, as_of: date | None = None):
        """返回 (定盘值, 应用的更正或 None)。

        as_of 之前的估值只看 notice_date <= as_of 的更正；
        as_of 为 None 时应用全部已知更正。多个更正按 notice_date 后者优先。
        """
        value = self.fixings.get(fixing_date)
        applied = None
        for c in sorted(self.fixing_corrections, key=lambda c: c.notice_date):
            if c.fixing_date != fixing_date:
                continue
            if as_of is not None and c.notice_date > as_of:
                continue
            value = c.corrected_value
            applied = c
        return value, applied
