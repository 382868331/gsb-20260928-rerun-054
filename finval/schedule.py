"""由固定利率合约生成实际支付日程（含月末、顺延、残期处理）。

日程构造先得到不调整的理论周期日，再统一做营业日调整；
调整后的周期日同时作为下一段计息起息日（ISDA 常规口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .conventions import Frequency, is_month_end, shift_months, year_fraction


@dataclass(frozen=True)
class Cashflow:
    index: int
    accrual_start: date
    accrual_end: date
    unadjusted_end: date
    payment_date: date
    accrual_factor: float
    interest_amount: float
    notional_amount: float
    fixing_date: date | None = None
    reference_rate: float | None = None
    rate_source: str = "FIXED"       # FIXED / FIXING / FORECAST

    @property
    def amount(self) -> float:
        return self.interest_amount + self.notional_amount


def _unadjusted_anchors(
    start: date,
    end: date,
    frequency: Frequency,
    eom: bool,
    stub: StubType,
) -> list[date]:
    """返回含起息日、不含到期日的理论周期日列表，并处理残期。"""
    step = frequency.months

    if stub.value.endswith("FIRST"):
        # 从到期日倒推
        snap = eom
        anchors = [end]
        while True:
            earlier = shift_months(anchors[0], -step, snap_eom=snap)
            same_ym = earlier.year == start.year and earlier.month == start.month
            # 非月末起息：倒推到与起息日同月即进入残期范围，停止；
            # 月末起息：仅当锚点恰好等于起息日才停止（完整周期情形）。
            reached_start = same_ym and (
                not is_month_end(start) or earlier.day == start.day)
            if reached_start or earlier < start:
                break
            anchors.insert(0, earlier)
        first_anchor = anchors[0]
        if first_anchor == start:
            return anchors[:-1]  # 无残期：首锚点即起息日
        if stub.value == "LONG_FIRST":
            return [start] + anchors[1:-1]  # 合并首期与残期
        return [start] + anchors[:-1]      # SHORT_FIRST

    if stub.value.endswith("LAST"):
        snap = eom
        anchors = [start]
        while True:
            later = shift_months(anchors[-1], step, snap_eom=snap)
            if later > end:
                break
            anchors.append(later)
        if anchors[-1] == end:
            return anchors[:-1]  # 无残期
        if stub.value == "LONG_LAST":
            return anchors[:-1]  # 合并末段与残期
        return anchors          # SHORT_LAST

    # NONE：必须恰好为完整周期
    regular = [start]
    while regular[-1] < end:
        regular.append(shift_months(regular[-1], step, snap_eom=eom))
    if regular[-1] != end:
        raise ValueError("起息日与到期日不是完整付息周期，请指定残期类型")
    return regular[:-1]


def build_period_dates(
    start: date,
    end: date,
    frequency: Frequency,
    eom: bool,
    stub: StubType,
    calendar: Any,
    bdc: Any,
    payment_lag_business_days: int = 0,
) -> list[tuple[date, date, date, date]]:
    """返回每段 (计息起, 计息止(调整后), 理论周期日, 支付日)。"""
    anchors = _unadjusted_anchors(start, end, frequency, eom, stub)
    period_ends = anchors[1:] + [end]
    periods = []
    adj_start = calendar.adjust(anchors[0], bdc)
    for unadj_end in period_ends:
        adj_end = calendar.adjust(unadj_end, bdc)
        if payment_lag_business_days:
            payment_date = calendar.shift_business_days(
                adj_end, payment_lag_business_days)
        else:
            payment_date = adj_end
        periods.append((adj_start, adj_end, unadj_end, payment_date))
        adj_start = adj_end
    return periods


def generate_schedule(contract: "FixedRateContract") -> tuple[Cashflow, ...]:
    """生成逐笔现金流；返回不可变元组，后续曲线变动不会修改它。"""
    periods = build_period_dates(
        contract.start_date, contract.end_date, contract.frequency,
        contract.eom, contract.stub, contract.calendar,
        contract.business_day_convention)

    cashflows = []
    for i, (adj_start, adj_end, unadj_end, payment_date) in enumerate(periods, start=1):
        factor = year_fraction(adj_start, adj_end, contract.day_count)
        redemption = (
            contract.notional
            if contract.notional_exchange and unadj_end == contract.end_date
            else 0.0
        )
        cashflows.append(Cashflow(
            index=i,
            accrual_start=adj_start,
            accrual_end=adj_end,
            unadjusted_end=unadj_end,
            payment_date=payment_date,
            accrual_factor=factor,
            interest_amount=round(contract.notional * contract.fixed_rate * factor, 6),
            notional_amount=redemption,
            reference_rate=contract.fixed_rate,
            rate_source="FIXED",
        ))
    return tuple(cashflows)
