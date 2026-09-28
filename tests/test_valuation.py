from datetime import date

import math

import pytest

from finval import (
    BusinessDayConvention,
    DayCount,
    DiscountCurve,
    FixedRateContract,
    Frequency,
    StubType,
    ValuationBatch,
)


def make_contract():
    # 跨月末短首期虚构合约：2025-03-15 -> 2026-03-31，半年付息
    return FixedRateContract(
        contract_id="PV-1",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.SHORT_FIRST,
    )


def test_pv_per_cashflow_and_total():
    contract = make_contract()
    curve = DiscountCurve(
        curve_id="FLAT-5PCT",
        valuation_date=date(2025, 3, 15),
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(math.exp(-0.05 * 381 / 365),),
    )
    batch = ValuationBatch("B001")
    v = batch.run(contract, curve)

    # 逐笔：金额 * 贴现因子
    expected_amounts = [1_000_000 * 0.06 * 199 / 360,
                        1_000_000 * 0.06 * 182 / 360]
    expected_pv = 0.0
    for cf, pv, amount in zip(v.cashflows, v.present_values, expected_amounts):
        df = curve.discount_factor(cf.payment_date)
        assert pv == pytest.approx(amount * df, abs=1e-3)
        expected_pv += amount * df
    assert v.npv == pytest.approx(expected_pv, abs=1e-3)


def test_curve_change_creates_new_valuation_without_touching_schedule():
    contract = make_contract()
    v_date = date(2025, 3, 15)
    curve_a = DiscountCurve(
        curve_id="FLAT-5PCT",
        valuation_date=v_date,
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(math.exp(-0.05 * 381 / 365),),
    )
    curve_b = curve_a.parallel_shift(0.01)  # 利率上移 1%，新曲线

    batch = ValuationBatch("B002")
    va = batch.run(contract, curve_a)
    vb = batch.run(contract, curve_b)

    assert va is not vb
    assert len(batch.records) == 2
    # 日程完全一致（金额、日期、计息因子均不变）
    assert va.cashflows == vb.cashflows
    # 贴现因子与现值随曲线变化
    assert vb.discount_factors[0] < va.discount_factors[0]
    assert vb.npv < va.npv
    # 两条记录仍可独立取回，没有相互覆盖
    assert batch.get("PV-1", "FLAT-5PCT").npv == va.npv
    assert batch.get("PV-1", "FLAT-5PCT_shift0.01").npv == vb.npv


def test_immutable_valuation_record():
    contract = make_contract()
    curve = DiscountCurve(
        curve_id="C",
        valuation_date=date(2025, 3, 15),
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(0.95,),
    )
    v = ValuationBatch("B").run(contract, curve)
    with pytest.raises(Exception):
        v.npv = 0.0


def test_notional_exchange_included_in_final_pv():
    contract = FixedRateContract(
        contract_id="BOND",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
        frequency=Frequency.ANNUAL,
        day_count=DayCount.ACT_365F,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.NONE,
        notional_exchange=True,
    )
    curve = DiscountCurve(
        curve_id="DF1",
        valuation_date=date(2025, 1, 1),
        pillar_dates=(date(2026, 1, 1),),
        discount_factors=(0.97,),
    )
    v = ValuationBatch("B").run(contract, curve)
    final = v.cashflows[0]
    assert final.notional_amount == 1_000_000.0
    assert final.amount == pytest.approx(1_000_000 + 1_000_000 * 0.06)
    assert v.npv == pytest.approx(final.amount * 0.97, abs=1e-3)
