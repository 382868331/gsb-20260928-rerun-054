"""浮动利率合约：计息期、支付日、定盘日分别遵守各自约定。

已知历史定盘优先于预测曲线；已经发生定盘但缺少数据时显式报错，
绝不能被预测曲线悄悄替代。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping

from .calendar import BusinessDayConvention, Calendar
from .conventions import DayCount, Frequency, UnsupportedConventionError, year_fraction
from .contract import StubType
from .schedule import Cashflow, build_period_dates


class MissingFixingError(ValueError):
    """定盘日已到但缺少已知定盘值。"""


@dataclass(frozen=True)
class FloatingRateContract:
    contract_id: str
    notional: float
    index_name: str                       # 如 "3M" 期限 SHIBOR/LIBOR 等
    start_date: date
    end_date: date
    frequency: Frequency = Frequency.QUARTERLY
    day_count: DayCount = DayCount.ACT_360
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    calendar: Calendar = None
    eom: bool = True
    stub: StubType = StubType.SHORT_FIRST
    notional_exchange: bool = False
    spread: float = 0.0                   # 加点（年化，简单利差）
    fixing_lag_business_days: int = 0     # 定盘先于计息起息日的营业日数
    payment_lag_business_days: int = 0    # 支付晚于计息止日的营业日数
    known_fixings: Mapping[date, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.day_count, DayCount):
            raise UnsupportedConventionError(f"未知计息约定: {self.day_count!r}")
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
        if self.fixing_lag_business_days < 0 or self.payment_lag_business_days < 0:
            raise ValueError("营业日滞后期不能为负")
        if self.calendar is None:
            object.__setattr__(self, "calendar", Calendar())
        object.__setattr__(self, "known_fixings", dict(self.known_fixings))

    def fixing_date(self, accrual_start: date) -> date:
        """定盘日：计息起息日（调整后）向前推 fixing lag 个营业日。"""
        if self.fixing_lag_business_days == 0:
            return accrual_start
        return self.calendar.shift_business_days(
            accrual_start, -self.fixing_lag_business_days)


def generate_floating_schedule(
    contract: FloatingRateContract,
    valuation_date: date,
    forecast_curve=None,
) -> tuple[Cashflow, ...]:
    """生成浮动利率现金流。

    定盘日 < 估值日（含）视为已经发生：必须使用已知定盘，缺失则报错；
    定盘日晚于估值日时使用预测曲线远期利率。
    """
    periods = build_period_dates(
        contract.start_date, contract.end_date, contract.frequency,
        contract.eom, contract.stub, contract.calendar,
        contract.business_day_convention,
        payment_lag_business_days=contract.payment_lag_business_days)

    cashflows = []
    for i, (adj_start, adj_end, unadj_end, payment_date) in enumerate(periods, start=1):
        factor = year_fraction(adj_start, adj_end, contract.day_count)
        fixing_date = contract.fixing_date(adj_start)
        known = contract.known_fixings.get(fixing_date)

        if fixing_date <= valuation_date:
            if known is None:
                raise MissingFixingError(
                    f"{contract.contract_id} 第 {i} 期定盘日 {fixing_date} "
                    f"已到（计息期 {adj_start} 起），但缺少已知定盘值，"
                    "禁止用预测曲线替代历史定盘")
            rate, source = known, "FIXING"
        else:
            if forecast_curve is None:
                raise ValueError(
                    f"{contract.contract_id} 第 {i} 期定盘日 {fixing_date} "
                    "尚未发生，需要提供预测曲线")
            rate = forecast_curve.forward_rate(adj_start, adj_end)
            source = "FORECAST"

        all_in_rate = rate + contract.spread
        redemption = (
            contract.notional
            if contract.notional_exchange and unadj_end == contract.end_date
            else 0.0)
        cashflows.append(Cashflow(
            index=i,
            accrual_start=adj_start,
            accrual_end=adj_end,
            unadjusted_end=unadj_end,
            payment_date=payment_date,
            accrual_factor=factor,
            interest_amount=round(
                contract.notional * all_in_rate * factor, 6),
            notional_amount=redemption,
            fixing_date=fixing_date,
            reference_rate=round(rate, 10),
            rate_source=source,
        ))
    return tuple(cashflows)
