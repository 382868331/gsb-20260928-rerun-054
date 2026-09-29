from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .calendar import BusinessDayConvention
from .daycount import DayCountConvention


@dataclass(frozen=True, kw_only=True)
class FixedRateContract:
    contract_id: str
    notional: float
    fixed_rate: float
    currency: str
    effective_date: date
    maturity_date: date
    payment_frequency_months: int
    day_count_convention: DayCountConvention
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    first_payment_date: date | None = None
    payment_lag_days: int = 0
    payment_at_maturity: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.fixed_rate < 0:
            raise ValueError("fixed_rate must not be negative")
        if not self.currency:
            raise ValueError("currency is required")
        if self.payment_frequency_months not in (1, 3, 6, 12):
            raise ValueError("payment_frequency_months must be one of 1, 3, 6, 12")
        if self.payment_lag_days < 0:
            raise ValueError("payment_lag_days must not be negative")
        if self.effective_date >= self.maturity_date:
            raise ValueError("maturity_date must be after effective_date")
        if self.first_payment_date is not None:
            if not self.effective_date < self.first_payment_date <= self.maturity_date:
                raise ValueError("first_payment_date must be after effective date and on or before maturity")
        if not isinstance(self.day_count_convention, DayCountConvention):
            raise ValueError("day_count_convention must be a DayCountConvention")
        if not isinstance(self.business_day_convention, BusinessDayConvention):
            raise ValueError("business_day_convention must be a BusinessDayConvention")
