import math
from datetime import date

import pytest

from finval import (
    BusinessDayConvention,
    Calendar,
    CashflowExplanation,
    DayCount,
    DiscountCurve,
    FloatingRateContract,
    FixingCorrection,
    Frequency,
    MissingFixingError,
    PartialTermination,
    StubType,
    ValuationBatch,
    generate_floating_schedule,
)


def flat_curve(curve_id, valuation_date, rate, end):
    return DiscountCurve(
        curve_id=curve_id,
        valuation_date=valuation_date,
        pillar_dates=(end,),
        discount_factors=(math.exp(-rate * (end - valuation_date).days / 365),),
    )


def make_eom_contract(fixings):
    # 跨月末季度合约：2/28 起息；5/31 周六且 5/1 为假日，按 MODIFIED_FOLLOWING 回到 5/30
    return FloatingRateContract(
        contract_id="FLOAT-EOM",
        notional=1_000_000.0,
        start_date=date(2025, 2, 28),
        end_date=date(2026, 2, 28),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        calendar=Calendar(holidays={date(2025, 5, 1), date(2025, 10, 1)}),
        stub=StubType.NONE,
        index_name="3M-TEST",
        fixing_lag_business_days=2,
        spread=0.005,
        fixings=fixings,
    )


KNOWN_FIXINGS = {
    date(2025, 2, 26): 0.040,
    date(2025, 5, 28): 0.042,
    date(2025, 8, 27): 0.045,
}


def test_floating_schedule_fixing_dates_and_holiday_adjustment():
    contract = make_eom_contract(KNOWN_FIXINGS)
    cfs = generate_floating_schedule(contract)

    assert [cf.payment_date for cf in cfs] == [
        date(2025, 5, 30),
        date(2025, 8, 29),
        date(2025, 11, 28),
        date(2026, 2, 27),
    ]
    # 定盘日为计息起息日前两个营业日；首期 2/28（周五）-> 2/26（周三）
    assert [cf.fixing_date for cf in cfs] == [
        date(2025, 2, 26),
        date(2025, 5, 28),
        date(2025, 8, 27),
        date(2025, 11, 26),
    ]
    assert [cf.rate_source for cf in cfs[:3]] == ["FIXING"] * 3
    assert cfs[3].rate_source == "FORECAST"
    assert cfs[3].interest_amount is None
    # 已定盘金额在日程中固定
    assert cfs[0].rate == pytest.approx(0.045)
    assert cfs[0].interest_amount == pytest.approx(
        1_000_000 * 0.045 * cfs[0].accrual_factor, abs=1e-4)


def test_mid_period_valuation_keeps_current_fixing_and_forecasts_next():
    valuation_date = date(2025, 10, 15)  # 位于第 3 个计息期（8/29 -> 11/28）中间
    contract = make_eom_contract(KNOWN_FIXINGS)
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    valuation = ValuationBatch("B").run(contract, discount, forecast)

    explanations = valuation.explanations
    assert [e.status for e in explanations] == ["PAID", "PAID", "FUTURE", "FUTURE"]
    assert [e.rate_source for e in explanations] == [
        "FIXING", "FIXING", "FIXING", "FORECAST"]

    # 已支付现金流不再贴现
    assert explanations[0].present_value == 0.0
    assert explanations[0].discount_factor is None

    # 估值日所在期间已定盘：使用 8/27 的历史值 + spread，与预测曲线无关
    current = explanations[2]
    assert current.fixing_date == date(2025, 8, 27)
    assert current.fixing_value == 0.045
    assert current.rate == pytest.approx(0.050)
    assert current.forecast_curve_id is None

    # 下一期间未定盘：逐笔解释预测区间与曲线
    upcoming = explanations[3]
    expected_forward = forecast.forward_rate(
        date(2025, 11, 28), date(2026, 2, 27), DayCount.ACT_360)
    assert upcoming.forecast_curve_id == "FWD"
    assert upcoming.forecast_start == date(2025, 11, 28)
    assert upcoming.forecast_end == date(2026, 2, 27)
    assert upcoming.rate == pytest.approx(expected_forward + 0.005)
    assert upcoming.interest_amount == pytest.approx(
        1_000_000 * upcoming.rate * valuation.cashflows[3].accrual_factor, abs=1e-4)
    assert all(isinstance(e, CashflowExplanation) for e in explanations)


def test_missing_historical_fixing_is_never_silently_forecast():
    valuation_date = date(2025, 10, 15)
    incomplete = dict(KNOWN_FIXINGS)
    del incomplete[date(2025, 8, 27)]  # 当前期间定盘日已过，却缺少历史定盘
    contract = make_eom_contract(incomplete)
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    with pytest.raises(MissingFixingError, match="2025-08-27"):
        ValuationBatch("B").run(contract, discount, forecast)


def test_future_period_without_forecast_curve_is_rejected():
    valuation_date = date(2025, 10, 15)
    contract = make_eom_contract(KNOWN_FIXINGS)
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    with pytest.raises(MissingFixingError, match="预测曲线"):
        ValuationBatch("B").run(contract, discount)


def test_changing_forecast_curve_only_moves_unfixed_cashflows():
    valuation_date = date(2025, 10, 15)
    contract = make_eom_contract(KNOWN_FIXINGS)
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast_a = flat_curve("FWD-A", valuation_date, 0.04, date(2026, 3, 31))
    forecast_b = forecast_a.parallel_shift(0.02)

    batch = ValuationBatch("B")
    va = batch.run(contract, discount, forecast_a)
    vb = batch.run(contract, discount, forecast_b)

    # 旧版日程快照不随新曲线变化（含未定盘期间仍保持未预测状态）
    assert va.cashflows == vb.cashflows
    assert len(batch.records) == 2

    # 历史/已定盘金额与现值保持不变
    for idx in range(3):
        assert va.explanations[idx].amount == vb.explanations[idx].amount
        assert va.present_values[idx] == vb.present_values[idx]

    # 只有尚未固定的最后一期随预测曲线变化
    assert vb.explanations[3].rate > va.explanations[3].rate
    assert vb.present_values[3] > va.present_values[3]
    assert vb.npv != va.npv
    assert batch.get("FLOAT-EOM", "DISC") is va


def test_short_first_floating_stub_with_mid_period_valuation():
    # 3/15 为周六：FOLLOWING 顺延至 3/17（周一），短首期至 9/30
    contract = FloatingRateContract(
        contract_id="FLOAT-STUB",
        notional=1_000_000.0,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.FOLLOWING,
        calendar=Calendar(holidays={date(2025, 5, 1)}),
        stub=StubType.SHORT_FIRST,
        fixings={date(2025, 3, 13): 0.038},
    )
    valuation_date = date(2025, 6, 30)  # 位于短首期中间
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 4, 30))
    forecast = flat_curve("FWD", valuation_date, 0.044, date(2026, 4, 30))

    cfs = generate_floating_schedule(contract)
    assert len(cfs) == 2
    assert cfs[0].accrual_start == date(2025, 3, 17)
    assert cfs[0].payment_date == date(2025, 9, 30)
    assert cfs[0].fixing_date == date(2025, 3, 13)

    valuation = ValuationBatch("B").run(contract, discount, forecast)
    first, second = valuation.explanations
    assert first.status == "FUTURE"          # 尚在计息中，支付日未到
    assert first.rate_source == "FIXING"     # 但利率已经固定
    assert first.fixing_value == 0.038
    assert second.rate_source == "FORECAST"
    assert second.forecast_start == date(2025, 9, 30)
    assert second.forecast_end == date(2026, 3, 31)


def test_late_fixing_correction_keeps_actual_paid_amount_and_adds_adjustment():
    valuation_date = date(2025, 10, 15)
    correction = FixingCorrection(
        event_id="FIX-2",
        fixing_date=date(2025, 5, 28),
        old_value=0.042,
        new_value=0.050,
        received_date=date(2025, 10, 10),
        settlement_date=date(2025, 10, 20),
        basis="交易所更正通知",
    )
    contract = make_eom_contract(KNOWN_FIXINGS)
    object.__setattr__(contract, "events", (correction,))
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    before_contract = make_eom_contract(KNOWN_FIXINGS)
    before_curve = flat_curve("DISC", date(2025, 10, 9), 0.03, date(2026, 3, 31))
    before_forecast = flat_curve("FWD", date(2025, 10, 9), 0.05, date(2026, 3, 31))
    before = ValuationBatch("OLD").run(before_contract, before_curve, before_forecast)
    after = ValuationBatch("NEW").run(contract, discount, forecast)

    old_paid = after.explanations[1]
    adjustment = next(e for e in after.explanations
                       if e.event_id == "FIX-2" and e.cashflow_kind == "ADJUSTMENT")
    assert old_paid.payment_state == "PAID"
    assert old_paid.interest_amount == before.explanations[1].interest_amount
    assert old_paid.present_value == 0.0
    assert adjustment.cashflow_kind == "ADJUSTMENT"
    assert adjustment.payment_state == "FUTURE"
    assert adjustment.fixing_value == 0.050
    assert adjustment.basis == "交易所更正通知"
    assert adjustment.interest_amount > 0.0
    assert len(after.original_cashflows) == 4


def test_partial_termination_on_payment_and_fixing_boundary_reduces_later_notional():
    valuation_date = date(2025, 11, 28)
    termination = PartialTermination(
        event_id="TERM-200",
        effective_date=date(2025, 11, 28),
        reduction_amount=200_000.0,
        received_date=date(2025, 11, 20),
        basis="提前终止协议",
    )
    fixings = {**KNOWN_FIXINGS, date(2025, 11, 26): 0.048}
    contract = make_eom_contract(fixings)
    object.__setattr__(contract, "events", (termination,))
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    valuation = ValuationBatch("B").run(contract, discount, forecast)
    principal = next(e for e in valuation.explanations
                     if e.cashflow_kind == "TERMINATION_PRINCIPAL")
    cancelled = next(e for e in valuation.explanations if e.payment_state == "CANCELLED")
    later = valuation.explanations[-1]

    assert principal.amount == 200_000.0
    assert principal.payment_state == "PAID"
    assert principal.basis == "提前终止协议"
    assert cancelled.amount == -200_000.0
    assert cancelled.status == "CANCELLED"
    assert later.notional_amount == 0.0
    assert later.interest_amount == pytest.approx(
        800_000 * later.rate * valuation.cashflows[-1].accrual_factor, abs=1e-4)


def test_holiday_termination_uses_adjusted_effective_boundary():
    valuation_date = date(2025, 12, 2)
    calendar = Calendar(holidays={date(2025, 5, 1), date(2025, 10, 1), date(2025, 12, 25)})
    termination = PartialTermination(
        event_id="TERM-HOLIDAY",
        effective_date=date(2025, 12, 25),
        reduction_amount=250_000.0,
        received_date=date(2025, 12, 1),
        basis="圣诞前终止",
    )
    fixings = {**KNOWN_FIXINGS, date(2025, 11, 26): 0.048}
    contract = make_eom_contract(fixings)
    object.__setattr__(contract, "calendar", calendar)
    object.__setattr__(contract, "events", (termination,))
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    valuation = ValuationBatch("B").run(contract, discount, forecast)
    last = valuation.explanations[-1]
    principal = next(e for e in valuation.explanations if e.event_id == "TERM-HOLIDAY"
                      and e.cashflow_kind == "TERMINATION_PRINCIPAL")
    cancelled = next(e for e in valuation.explanations if e.event_id == "TERM-HOLIDAY"
                     and e.payment_state == "CANCELLED")
    assert principal.payment_date == date(2025, 12, 26)
    assert cancelled.payment_date == date(2025, 12, 26)
    assert principal.payment_state == "FUTURE"
    assert cancelled.payment_state == "CANCELLED"
    assert last.cashflow_kind == "SCHEDULED"
    scheduled = valuation.cashflows[-1]
    assert last.interest_amount == pytest.approx(
        1_000_000 * last.rate * (
            date(2025, 12, 26) - scheduled.accrual_start).days / 360
        + 750_000 * last.rate * (
            scheduled.accrual_end - date(2025, 12, 26)).days / 360,
        abs=1e-4)


def test_correction_arriving_exactly_on_fixing_date_fixes_forecast_period():
    valuation_date = date(2025, 8, 27)
    fixings = {date(2025, 2, 26): 0.040, date(2025, 5, 28): 0.042}
    correction = FixingCorrection(
        event_id="FIX-INITIAL",
        fixing_date=date(2025, 8, 27),
        old_value=0.0,
        new_value=0.045,
        received_date=date(2025, 8, 27),
        settlement_date=date(2025, 11, 28),
        basis="定盘日发布值",
    )
    contract = make_eom_contract(fixings)
    object.__setattr__(contract, "events", (correction,))
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))

    valuation = ValuationBatch("B").run(contract, discount, forecast)
    assert valuation.explanations[2].fix_state == "FIXED"
    assert valuation.explanations[2].fixing_value == 0.045


def test_batch_save_and_reload_keeps_schedule_snapshot_and_event_basis(tmp_path):
    valuation_date = date(2025, 10, 15)
    correction = FixingCorrection(
        event_id="FIX-PERSIST",
        fixing_date=date(2025, 5, 28),
        old_value=0.042,
        new_value=0.050,
        received_date=date(2025, 10, 10),
        settlement_date=date(2025, 10, 20),
        basis="可追溯更正依据",
    )
    contract = make_eom_contract(KNOWN_FIXINGS)
    object.__setattr__(contract, "events", (correction,))
    discount = flat_curve("DISC", valuation_date, 0.03, date(2026, 3, 31))
    forecast = flat_curve("FWD", valuation_date, 0.05, date(2026, 3, 31))
    batch = ValuationBatch("PERSIST")
    saved = batch.run(contract, discount, forecast)

    path = tmp_path / "valuation.json"
    batch.save_json(path)
    reopened = ValuationBatch.load_json(path)
    loaded = reopened.records[0]

    assert reopened.batch_id == "PERSIST"
    assert loaded.npv == saved.npv
    assert loaded.original_cashflows == saved.original_cashflows
    assert isinstance(loaded.events[0], FixingCorrection)
    adjustment = next(e for e in loaded.explanations if e.cashflow_kind == "ADJUSTMENT")
    assert adjustment.basis == "可追溯更正依据"
