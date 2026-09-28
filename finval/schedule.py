"""由固定利率合约生成实际支付日程（含月末、顺延、残期处理）。

日程构造先得到不调整的理论周期日，再统一做营业日调整；
调整后的周期日同时作为下一段计息起息日（ISDA 常规口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .conventions import is_month_end, shift_months, year_fraction

if TYPE_CHECKING:
    from .contract import FixedRateContract


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

    @property
    def amount(self) -> float:
        return self.interest_amount + self.notional_amount


def _unadjusted_anchors(contract: "FixedRateContract") -> list[date]:
    """返回含起息日、不含到期日的理论周期日列表，并处理残期。"""
    start, end = contract.start_date, contract.end_date
    step = contract.frequency.months
    stub = contract.stub

    if stub.value.endswith("FIRST"):
        # 从到期日倒推
        snap = contract.eom
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
        snap = contract.eom
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
        regular.append(shift_months(regular[-1], step, snap_eom=contract.eom))
    if regular[-1] != end:
        raise ValueError("起息日与到期日不是完整付息周期，请指定残期类型")
    return regular[:-1]


def generate_schedule(contract: "FixedRateContract") -> tuple[Cashflow, ...]:
    """生成逐笔现金流；返回不可变元组，后续曲线变动不会修改它。"""
    anchors = _unadjusted_anchors(contract)
    cal, bdc = contract.calendar, contract.business_day_convention
    end = contract.end_date

    period_ends = anchors[1:] + [end]
    cashflows = []
    adj_start = cal.adjust(anchors[0], bdc)
    for i, unadj_end in enumerate(period_ends, start=1):
        adj_end = cal.adjust(unadj_end, bdc)
        factor = year_fraction(adj_start, adj_end, contract.day_count)
        redemption = (
            contract.notional
            if contract.notional_exchange and unadj_end == end
            else 0.0
        )
        cashflows.append(Cashflow(
            index=i,
            accrual_start=adj_start,
            accrual_end=adj_end,
            unadjusted_end=unadj_end,
            payment_date=adj_end,
            accrual_factor=factor,
            interest_amount=round(
                contract.notional * contract.fixed_rate * factor, 6),
            notional_amount=redemption,
        ))
        adj_start = adj_end
    return tuple(cashflows)
