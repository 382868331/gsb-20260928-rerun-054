from __future__ import annotations

from dataclasses import dataclass
import calendar as calendar_module
from datetime import date, timedelta
from enum import Enum
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .calendar import BusinessCalendar
from .contracts import FixedRateContract
from .daycount import year_fraction


class CashFlowType(Enum):
    FIXED_COUPON = "fixed_coupon"
    PRINCIPAL = "principal"


@dataclass(frozen=True, kw_only=True)
class CashFlow:
    contract_id: str
    sequence: int
    cash_flow_type: CashFlowType
    accrual_start: date
    accrual_end: date
    payment_date: date
    notional: Decimal
    year_fraction: Decimal
    fixed_rate: Decimal
    amount: Decimal
    currency: str

    def with_payment_date(self, payment_date: date) -> "CashFlow":
        return CashFlow(
            contract_id=self.contract_id,
            sequence=self.sequence,
            cash_flow_type=self.cash_flow_type,
            accrual_start=self.accrual_start,
            accrual_end=self.accrual_end,
            payment_date=payment_date,
            notional=self.notional,
            year_fraction=self.year_fraction,
            fixed_rate=self.fixed_rate,
            amount=self.amount,
            currency=self.currency,
        )


def generate_fixed_schedule(contract: FixedRateContract, calendar: BusinessCalendar) -> list[CashFlow]:
    period_end_dates = _period_end_dates(contract)
    cash_flows: list[CashFlow] = []
    accrual_start = contract.effective_date
    sequence = 1

    with localcontext() as context:
        context.prec = 28
        notional = Decimal(str(contract.notional))
        fixed_rate = Decimal(str(contract.fixed_rate))

        for period_end in period_end_dates:
            accrual_year_fraction = year_fraction(accrual_start, period_end, contract.day_count_convention)
            interest_amount = _money(notional * fixed_rate * accrual_year_fraction)
            unadjusted_payment_date = period_end + timedelta(days=contract.payment_lag_days)
            payment_date = calendar.adjust(unadjusted_payment_date, contract.business_day_convention)
            cash_flows.append(
                CashFlow(
                    contract_id=contract.contract_id,
                    sequence=sequence,
                    cash_flow_type=CashFlowType.FIXED_COUPON,
                    accrual_start=accrual_start,
                    accrual_end=period_end,
                    payment_date=payment_date,
                    notional=notional,
                    year_fraction=accrual_year_fraction,
                    fixed_rate=fixed_rate,
                    amount=interest_amount,
                    currency=contract.currency,
                )
            )

            if period_end == contract.maturity_date and contract.payment_at_maturity:
                cash_flows.append(
                    CashFlow(
                        contract_id=contract.contract_id,
                        sequence=sequence,
                        cash_flow_type=CashFlowType.PRINCIPAL,
                        accrual_start=accrual_start,
                        accrual_end=period_end,
                        payment_date=payment_date,
                        notional=notional,
                        year_fraction=accrual_year_fraction,
                        fixed_rate=fixed_rate,
                        amount=notional,
                        currency=contract.currency,
                    )
                )
            accrual_start = period_end
            sequence += 1

    return cash_flows


def _period_end_dates(contract: FixedRateContract) -> list[date]:
    if contract.first_payment_date is None:
        dates = _regular_dates_backward(contract)
    else:
        regular_dates = _regular_dates_backward(contract)
        dates = [contract.first_payment_date]
        dates.extend(d for d in regular_dates if d > contract.first_payment_date)

    if dates[-1] != contract.maturity_date:
        raise ValueError("Generated schedule does not end on maturity date")
    if dates[0] <= contract.effective_date:
        raise ValueError("Generated first period end must be after effective date")
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise ValueError("Generated period end dates must be strictly increasing")
    return dates


def _regular_dates_backward(contract: FixedRateContract) -> list[date]:
    dates: list[date] = []
    candidate = contract.maturity_date
    while candidate > contract.effective_date:
        dates.append(candidate)
        candidate = _shift_months(candidate, -contract.payment_frequency_months)
    dates.reverse()
    return dates


def _shift_months(day: date, months: int) -> date:
    zero_based_month = day.month - 1 + months
    year = day.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    last_day = calendar_module.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
