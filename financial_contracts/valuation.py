from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from .calendar import BusinessCalendar
from .contracts import FixedRateContract
from .curves import DiscountCurve
from .schedule import CashFlow, generate_fixed_schedule


@dataclass(frozen=True, kw_only=True)
class ValuationResult:
    valuation_id: str
    contract_id: str
    curve_id: str
    valuation_date: date
    currency: str
    cash_flows: tuple[ValuedCashFlow, ...]
    present_value: Decimal


@dataclass(frozen=True, kw_only=True)
class ValuedCashFlow:
    cash_flow: CashFlow
    discount_factor: Decimal
    present_value: Decimal


def value_contract(
    *,
    valuation_id: str,
    contract: FixedRateContract,
    curve: DiscountCurve,
    calendar: BusinessCalendar,
    valuation_date: date | None = None,
) -> ValuationResult:
    schedule = generate_fixed_schedule(contract, calendar)
    as_of = valuation_date or curve.valuation_date
    if curve.currency != contract.currency:
        raise ValueError("Curve currency must match contract currency")
    if as_of != curve.valuation_date:
        raise ValueError("Explicit valuation date must match curve valuation date")

    valued: list[ValuedCashFlow] = []
    with localcontext() as context:
        context.prec = 28
        for cash_flow in schedule:
            if cash_flow.payment_date < as_of:
                continue
            factor = curve.discount_factor(cash_flow.payment_date)
            present_value = (cash_flow.amount * factor).quantize(Decimal("0.01"))
            valued.append(ValuedCashFlow(cash_flow=cash_flow, discount_factor=factor, present_value=present_value))
        total = sum((item.present_value for item in valued), Decimal("0.00"))

    return ValuationResult(
        valuation_id=valuation_id,
        contract_id=contract.contract_id,
        curve_id=curve.curve_id,
        valuation_date=as_of,
        currency=contract.currency,
        cash_flows=tuple(valued),
        present_value=total,
    )


class ValuationBatch:
    def __init__(self, calendar: BusinessCalendar) -> None:
        self.calendar = calendar
        self._results: dict[str, ValuationResult] = {}

    @property
    def results(self) -> dict[str, ValuationResult]:
        return dict(self._results)

    def run(self, valuation_id: str, contract: FixedRateContract, curve: DiscountCurve) -> ValuationResult:
        if valuation_id in self._results:
            raise ValueError(f"Valuation ID already exists: {valuation_id}")
        result = value_contract(
            valuation_id=valuation_id,
            contract=contract,
            curve=curve,
            calendar=self.calendar,
        )
        self._results[valuation_id] = result
        return result
