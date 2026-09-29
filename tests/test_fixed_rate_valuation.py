from datetime import date
from decimal import Decimal
import unittest

from financial_contracts import (
    BusinessCalendar,
    BusinessDayConvention,
    CashFlowType,
    DayCountConvention,
    DiscountCurve,
    FixedRateContract,
    UnsupportedConventionError,
    ValuationBatch,
    generate_fixed_schedule,
    value_contract,
    year_fraction,
)


class FixedRateValuationTests(unittest.TestCase):
    def test_short_first_stub_and_month_end_payment_adjustment(self) -> None:
        calendar = BusinessCalendar()
        contract = FixedRateContract(
            contract_id="SHORT-STUB",
            notional=1_000_000,
            fixed_rate=0.05,
            currency="CNY",
            effective_date=date(2024, 1, 31),
            first_payment_date=date(2024, 2, 29),
            maturity_date=date(2024, 8, 31),
            payment_frequency_months=3,
            day_count_convention=DayCountConvention.ACT_365_FIXED,
            business_day_convention=BusinessDayConvention.FOLLOWING,
        )

        schedule = generate_fixed_schedule(contract, calendar)

        coupon_flows = [flow for flow in schedule if flow.cash_flow_type is CashFlowType.FIXED_COUPON]
        self.assertEqual([flow.accrual_end for flow in coupon_flows], [date(2024, 2, 29), date(2024, 5, 31), date(2024, 8, 31)])
        self.assertEqual([flow.payment_date for flow in coupon_flows], [date(2024, 2, 29), date(2024, 5, 31), date(2024, 9, 2)])
        self.assertEqual(schedule[0].year_fraction, Decimal("29") / Decimal("365"))
        self.assertEqual(schedule[0].amount, Decimal("3972.60"))
        self.assertEqual(schedule[2].year_fraction, Decimal("92") / Decimal("365"))
        self.assertEqual(schedule[2].amount, Decimal("12602.74"))
        self.assertEqual(schedule[-1].amount, Decimal("1000000.00"))

    def test_present_value_keeps_schedule_immutable_across_curve_changes(self) -> None:
        calendar = BusinessCalendar()
        contract = FixedRateContract(
            contract_id="PV-001",
            notional=1_000_000,
            fixed_rate=0.05,
            currency="CNY",
            effective_date=date(2024, 1, 31),
            first_payment_date=date(2024, 2, 29),
            maturity_date=date(2024, 8, 31),
            payment_frequency_months=3,
            day_count_convention=DayCountConvention.ACT_365_FIXED,
            business_day_convention=BusinessDayConvention.FOLLOWING,
        )
        first_curve = DiscountCurve(
            curve_id="FLAT-1",
            currency="CNY",
            valuation_date=date(2024, 1, 31),
            points=(
                (date(2024, 1, 31), Decimal("1")),
                (date(2024, 2, 29), Decimal("0.997")),
                (date(2024, 5, 31), Decimal("0.993")),
                (date(2024, 9, 2), Decimal("0.98")),
            ),
        )
        second_curve = DiscountCurve(
            curve_id="FLAT-2",
            currency="CNY",
            valuation_date=date(2024, 1, 31),
            points=(
                (date(2024, 1, 31), Decimal("1")),
                (date(2024, 2, 29), Decimal("0.997")),
                (date(2024, 5, 31), Decimal("0.993")),
                (date(2024, 9, 2), Decimal("0.97")),
            ),
        )

        batch = ValuationBatch(calendar)
        first = batch.run("valuation-1", contract, first_curve)
        second = batch.run("valuation-2", contract, second_curve)

        self.assertNotEqual(first.present_value, second.present_value)
        self.assertEqual(first.cash_flows[0].cash_flow, second.cash_flows[0].cash_flow)
        self.assertEqual(first.present_value, Decimal("1008825.89"))
        self.assertEqual(second.present_value, Decimal("998699.86"))

    def test_supported_and_rejected_day_count_conventions(self) -> None:
        start = date(2024, 1, 31)
        end = date(2024, 2, 29)

        self.assertEqual(year_fraction(start, end, DayCountConvention.ACT_360), Decimal("29") / Decimal("360"))
        self.assertEqual(year_fraction(start, end, DayCountConvention.THIRTY_360), Decimal("29") / Decimal("360"))
        self.assertEqual(year_fraction(start, end, DayCountConvention.THIRTY_E_360), Decimal("29") / Decimal("360"))
        with self.assertRaises(UnsupportedConventionError):
            year_fraction(start, end, "ACT/ACT")  # type: ignore[arg-type]

    def test_rejects_curve_payment_date_outside_range(self) -> None:
        calendar = BusinessCalendar()
        contract = FixedRateContract(
            contract_id="PV-OUT",
            notional=1_000_000,
            fixed_rate=0.05,
            currency="CNY",
            effective_date=date(2024, 1, 31),
            maturity_date=date(2024, 4, 30),
            payment_frequency_months=3,
            day_count_convention=DayCountConvention.ACT_365_FIXED,
        )
        curve = DiscountCurve(
            curve_id="SHORT",
            currency="CNY",
            valuation_date=date(2024, 1, 31),
            points=((date(2024, 1, 31), Decimal("1")), (date(2024, 4, 1), Decimal("0.99"))),
        )

        with self.assertRaises(ValueError):
            value_contract(valuation_id="bad", contract=contract, curve=curve, calendar=calendar)


if __name__ == "__main__":
    unittest.main()
