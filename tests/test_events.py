import math
from datetime import date

import pytest

from finval import (
    Calendar,
    DayCount,
    DiscountCurve,
    FixingCorrection,
    FloatingRateContract,
    Frequency,
    PartialTermination,
    StubType,
    ValuationBatch,
    compare_schedules,
    generate_floating_schedule,
)


def flat_curve(curve_id, valuation_date, rate=0.03, end=date(2026, 3, 31)):
    return DiscountCurve(
        curve_id=curve_id,
        valuation_date=valuation_date,
        pillar_dates=(end,),
        discount_factors=(math.exp(-rate * (end - valuation_date).days / 365),),
    )


# 支付日: 5/30, 8/29, 11/28, 2026/2/27；定盘日: 2/26, 5/28, 8/27, 11/26
FIXINGS = {
    date(2025, 2, 26): 0.040,
    date(2025, 5, 28): 0.042,
    date(2025, 8, 27): 0.045,
    date(2025, 11, 26): 0.048,
}


def make_contract(**kw):
    args = dict(
        contract_id="FLT-EVT",
        notional=1_000_000.0,
        start_date=date(2025, 2, 28),
        end_date=date(2026, 2, 28),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        calendar=Calendar(holidays={date(2025, 5, 1), date(2025, 10, 1)}),
        stub=StubType.NONE,
        index_name="3M-TEST",
        fixing_lag_business_days=2,
        spread=0.005,
        fixings=FIXINGS,
    )
    args.update(kw)
    return FloatingRateContract(**args)


def test_correction_before_payment_updates_cashflow():
    correction = FixingCorrection(
        fixing_date=date(2025, 2, 26), corrected_value=0.043,
        notice_date=date(2025, 3, 10), reference="FIX-REV-1")
    contract = make_contract(fixing_corrections=(correction,))

    # 更正到达前的估值仍使用当时资料（原值 0.040）
    before = ValuationBatch("B1").run(
        contract, flat_curve("D1", date(2025, 3, 5)))
    assert before.explanations[0].fixing_value == 0.040
    assert before.explanations[0].rate == pytest.approx(0.045)

    # 更正到达后：相关现金流按更正值，且可追溯依据
    after = ValuationBatch("B2").run(
        contract, flat_curve("D2", date(2025, 4, 15)))
    e = after.explanations[0]
    assert e.fixing_value == 0.043
    assert e.rate == pytest.approx(0.048)
    assert e.payment_state == "FIXED"
    assert "FIX-REV-1" in e.basis
    # 旧估值快照不受新估值影响
    assert before.explanations[0].fixing_value == 0.040


def test_correction_after_payment_creates_adjustment_not_recompute():
    correction = FixingCorrection(
        fixing_date=date(2025, 2, 26), corrected_value=0.043,
        notice_date=date(2025, 9, 1), reference="FIX-REV-2")
    contract = make_contract(fixing_corrections=(correction,))
    v = ValuationBatch("B").run(contract, flat_curve("D", date(2025, 10, 15)))

    paid = v.explanations[0]
    # 已支付金额保持支付时的实际值（0.040 + spread），不被悄悄重算
    assert paid.status == "PAID"
    assert paid.payment_state == "PAID"
    assert paid.fixing_value == 0.040
    factor = v.cashflows[0].accrual_factor
    assert paid.interest_amount == pytest.approx(1_000_000 * 0.045 * factor)

    # 差额生成独立的调整款项，在下一个未付支付日结算
    adj = [e for e in v.explanations if e.payment_state == "ADJUSTED"]
    assert len(adj) == 1
    assert adj[0].interest_amount == pytest.approx(1_000_000 * 0.003 * factor)
    adj_cf = [cf for cf in v.cashflows if cf.rate_source == "ADJUSTMENT"][0]
    assert adj_cf.payment_date == date(2025, 11, 28)
    assert "FIX-REV-2" in adj[0].basis
    # 调整款项计入 NPV
    assert v.npv == pytest.approx(sum(v.present_values))


def test_correction_after_valuation_date_not_applied():
    correction = FixingCorrection(
        fixing_date=date(2025, 2, 26), corrected_value=0.043,
        notice_date=date(2025, 12, 1), reference="FIX-REV-3")
    contract = make_contract(fixing_corrections=(correction,))
    v = ValuationBatch("B").run(contract, flat_curve("D", date(2025, 10, 15)))
    assert v.explanations[0].fixing_value == 0.040
    assert not [e for e in v.explanations if e.payment_state == "ADJUSTED"]


def test_partial_termination_mid_period_splits_accrual():
    term = PartialTermination(
        termination_date=date(2025, 10, 15), terminated_notional=400_000.0,
        notice_date=date(2025, 10, 10), reference="TERM-1")
    contract = make_contract(partial_terminations=(term,))
    cfs = generate_floating_schedule(contract, as_of=date(2025, 10, 20))

    current = cfs[2]  # 8/29 -> 11/28
    assert current.segments == (
        (date(2025, 8, 29), date(2025, 10, 15), 1_000_000.0),
        (date(2025, 10, 15), date(2025, 11, 28), 600_000.0),
    )
    assert current.outstanding_notional == 600_000.0
    expected = (
        1_000_000 * 0.050 * (47 / 360) + 600_000 * 0.050 * (44 / 360))
    assert current.interest_amount == pytest.approx(expected, abs=1e-4)
    assert "TERM-1" in current.basis
    # 终止生效前的期次不受影响
    assert cfs[0].segments == (
        (date(2025, 2, 28), date(2025, 5, 30), 1_000_000.0),)


def test_termination_exactly_on_payment_date_boundary():
    # 11/28 是第 3 期支付日、第 4 期计息起息日
    term = PartialTermination(
        termination_date=date(2025, 11, 28), terminated_notional=400_000.0,
        notice_date=date(2025, 11, 20), reference="TERM-2")
    contract = make_contract(partial_terminations=(term,))
    cfs = generate_floating_schedule(contract, as_of=date(2025, 11, 25))

    # 第 3 期按终止前本金全额计息
    assert cfs[2].segments == (
        (date(2025, 8, 29), date(2025, 11, 28), 1_000_000.0),)
    # 第 4 期整期按剩余本金
    assert cfs[3].segments == (
        (date(2025, 11, 28), date(2026, 2, 27), 600_000.0),)


def test_termination_exactly_on_fixing_date_keeps_fixing():
    # 11/26（周三，营业日）是第 4 期定盘日
    term = PartialTermination(
        termination_date=date(2025, 11, 26), terminated_notional=400_000.0,
        notice_date=date(2025, 11, 20), reference="TERM-3")
    contract = make_contract(partial_terminations=(term,))
    v = ValuationBatch("B").run(contract, flat_curve("D", date(2025, 11, 24)))

    last = v.explanations[3]
    # 定盘日照常定盘，利率仍使用该定盘值；本金按终止后剩余计息
    assert last.rate_source == "FIXING"
    assert last.rate == pytest.approx(0.053)
    assert last.outstanding_notional == 600_000.0
    assert last.interest_amount == pytest.approx(
        600_000 * 0.053 * v.cashflows[3].accrual_factor, abs=1e-4)


def test_termination_on_holiday_adjusted_consistently():
    # 10/1 为假日，MODIFIED_FOLLOWING 调整到 10/2
    term = PartialTermination(
        termination_date=date(2025, 10, 1), terminated_notional=300_000.0,
        notice_date=date(2025, 9, 25), reference="TERM-4")
    contract = make_contract(partial_terminations=(term,))
    cfs = generate_floating_schedule(contract, as_of=date(2025, 10, 20))
    assert cfs[2].segments == (
        (date(2025, 8, 29), date(2025, 10, 2), 1_000_000.0),
        (date(2025, 10, 2), date(2025, 11, 28), 700_000.0),
    )


def test_full_termination_cancels_remaining_cashflows():
    term = PartialTermination(
        termination_date=date(2025, 8, 29), terminated_notional=1_000_000.0,
        notice_date=date(2025, 8, 28), reference="TERM-FULL")
    contract = make_contract(partial_terminations=(term,))
    v = ValuationBatch("B").run(contract, flat_curve("D", date(2025, 10, 15)))

    states = [e.payment_state for e in v.explanations]
    assert states == ["PAID", "PAID", "CANCELLED", "CANCELLED"]
    assert v.explanations[2].amount == 0.0
    assert v.explanations[2].present_value == 0.0
    assert "TERM-FULL" in v.explanations[2].basis
    assert v.npv == 0.0


def test_compare_schedules_before_after_events():
    plain = make_contract()
    correction = FixingCorrection(
        fixing_date=date(2025, 8, 27), corrected_value=0.047,
        notice_date=date(2025, 9, 5), reference="FIX-REV-9")
    term = PartialTermination(
        termination_date=date(2025, 11, 28), terminated_notional=500_000.0,
        notice_date=date(2025, 10, 1), reference="TERM-9")
    events = make_contract(
        fixing_corrections=(correction,), partial_terminations=(term,))

    valuation_date = date(2025, 10, 15)
    before = ValuationBatch("B1").run(
        plain, flat_curve("D", valuation_date), flat_curve("F", valuation_date, 0.05))
    after = ValuationBatch("B2").run(
        events, flat_curve("D", valuation_date), flat_curve("F", valuation_date, 0.05))

    diffs = {d.index: d for d in compare_schedules(before, after)}
    assert diffs[1].change == "UNCHANGED"
    assert diffs[3].change == "CORRECTED"
    assert diffs[3].after_amount != diffs[3].before_amount
    assert "FIX-REV-9" in diffs[3].basis
    assert diffs[4].change == "REDUCED"  # 终止后期利息按剩余本金
    assert after.explanations[3].outstanding_notional == 500_000.0


def test_save_and_reload_preserves_history(tmp_path):
    correction = FixingCorrection(
        fixing_date=date(2025, 2, 26), corrected_value=0.043,
        notice_date=date(2025, 9, 1), reference="FIX-REV-2")
    contract = make_contract(fixing_corrections=(correction,))

    batch = ValuationBatch("BATCH-X")
    v1 = batch.run(contract, flat_curve("D1", date(2025, 4, 15)))
    v2 = batch.run(contract, flat_curve("D2", date(2025, 10, 15)))
    path = tmp_path / "batch.json"
    batch.save(path)

    loaded = ValuationBatch.load(path)
    assert loaded.batch_id == "BATCH-X"
    assert len(loaded.records) == 2
    # 旧日程快照与历史结论保持可查
    assert loaded.get("FLT-EVT", "D1").npv == v1.npv
    assert loaded.get("FLT-EVT", "D2").npv == v2.npv
    assert loaded.get("FLT-EVT", "D1").cashflows == v1.cashflows
    assert loaded.get("FLT-EVT", "D2").explanations == v2.explanations

    # 重开后可继续估值，旧记录不被覆盖
    v3 = loaded.run(contract, flat_curve("D3", date(2025, 12, 1)))
    assert len(loaded.records) == 3
    assert loaded.get("FLT-EVT", "D1").npv == v1.npv
    assert v3.curve_id == "D3"
