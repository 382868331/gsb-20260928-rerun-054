"""浮动利率、已知历史定盘、预测分段与跨估值日场景测试。"""

from datetime import date
import math

import pytest

from finval import (
    BusinessDayConvention,
    Calendar,
    DayCount,
    DiscountCurve,
    FloatingRateContract,
    ForecastCurve,
    Frequency,
    MissingFixingError,
    StubType,
    ValuationBatch,
)


def _flat_curve(curve_cls, cid, v_date, rate, end):
    return curve_cls(
        curve_id=cid,
        valuation_date=v_date,
        pillar_dates=(end,),
        discount_factors=(math.exp(-rate * (end - v_date).days / 365.0),),
    )


def _contract(**overrides):
    defaults = dict(
        contract_id="FLOAT-1",
        notional=1_000_000.0,
        index_name="3M",
        # 短首期 + 跨月末：2025-02-13 起息，季度付息至 2025-11-30
        start_date=date(2025, 2, 13),
        end_date=date(2025, 11, 30),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.FOLLOWING,
        calendar=Calendar(holidays={date(2025, 9, 1)}),
        stub=StubType.SHORT_FIRST,
        fixing_lag_business_days=2,
        payment_lag_business_days=1,
        spread=0.005,
    )
    defaults.update(overrides)
    return FloatingRateContract(**defaults)


def test_schedule_respects_accrual_payment_and_fixing_conventions():
    contract = _contract()
    v_date = date(2025, 2, 10)
    forecast = _flat_curve(ForecastCurve, "FWD", v_date, 0.03, date(2026, 6, 30))
    from finval import generate_floating_schedule
    cfs = generate_floating_schedule(contract, v_date, forecast)

    # 理论周期日：短首期残期 02-13 -> 05-31（月末锚点）-> 08-31 -> 11-30
    assert [cf.unadjusted_end for cf in cfs] == [
        date(2025, 5, 31), date(2025, 8, 31), date(2025, 11, 30)]
    # 计息止 8/31 为周日，FOLLOWING 顺延；9/1 为假日 -> 9/2
    assert cfs[1].accrual_end == date(2025, 9, 2)
    assert cfs[2].accrual_start == date(2025, 9, 2)
    # 支付日滞后 1 个营业日；计息止 9/2（周二）-> 9/3
    assert cfs[1].payment_date == date(2025, 9, 3)
    # 定盘日为计息起息日前 2 个营业日：2025-02-13（周四）-> 02-11
    assert cfs[0].fixing_date == date(2025, 2, 11)
    # 9/2（周二）前推两个营业日 -> 8/28（周四）
    assert cfs[2].fixing_date == date(2025, 8, 28)


def test_due_fixing_without_known_value_is_rejected_never_forecast():
    # 估值日 2025-06-02：首期已定盘（定盘日 2/11），但未提供定盘值
    v_date = date(2025, 6, 2)
    forecast = _flat_curve(ForecastCurve, "FWD", v_date, 0.03, date(2026, 6, 30))
    with pytest.raises(MissingFixingError):
        ValuationBatch("B").run(_contract(), _flat_curve(
            DiscountCurve, "DF", v_date, 0.04, date(2026, 6, 30)), forecast)


def test_historical_fixing_amount_is_frozen_when_forecast_changes():
    # 估值日位于周期中间：首期已定盘并已支付；第二期已定盘未支付；第三期仍预测
    v_date = date(2025, 9, 15)
    contract = _contract(known_fixings={
        date(2025, 2, 11): 0.0210,   # 首期（支付日 6/2 之前，已支付）
        date(2025, 5, 29): 0.0250,  # 第二期定盘（计息起 6/2 前两营业日）
        date(2025, 8, 28): 0.0280,  # 第三期定盘日已到（8/28 < 9/15）
    })
    df_curve = _flat_curve(DiscountCurve, "DF", v_date, 0.04, date(2026, 6, 30))
    fwd_a = _flat_curve(ForecastCurve, "FWD-A", v_date, 0.03, date(2026, 6, 30))
    fwd_b = fwd_a.parallel_shift(0.02)  # 预测曲线上移 2%

    batch = ValuationBatch("B")
    va = batch.run(contract, df_curve, fwd_a)
    vb = batch.run(contract, df_curve, fwd_b)

    # 日程金额（含已定盘期）完全不受预测曲线变化影响
    assert va.cashflows == vb.cashflows

    # 首期已支付：保留金额用于解释，现值为 0，不参与贴现
    first = va.cashflows[0]
    assert first.payment_date < v_date
    assert va.discount_factors[0] is None
    assert va.present_values[0] == 0.0
    assert va.rate_explanations[0].status == "SETTLED"
    expected_first = 1_000_000 * (0.0210 + 0.005) * first.accrual_factor
    assert first.interest_amount == pytest.approx(expected_first, abs=1e-4)

    # 全部三期在 9/15 前都已定盘，因此没有 FORECAST 段，两条估值 NPV 相同
    assert {e.rate_source for e in va.rate_explanations} == {"FIXING"}
    assert va.npv == vb.npv


def test_forecast_shift_only_affects_not_yet_fixed_periods():
    # 估值日 2025-06-10：前两期已定盘（首期已支付），第三期未确定盘，走预测
    v_date = date(2025, 6, 10)
    contract = _contract(known_fixings={
        date(2025, 2, 11): 0.0210,
        date(2025, 5, 29): 0.0250,
    })
    df_curve = _flat_curve(DiscountCurve, "DF", v_date, 0.04, date(2026, 6, 30))
    fwd_a = _flat_curve(ForecastCurve, "FWD-A", v_date, 0.03, date(2026, 6, 30))
    fwd_b = fwd_a.parallel_shift(0.02)

    va = ValuationBatch("B").run(contract, df_curve, fwd_a)
    vb = ValuationBatch("B").run(contract, df_curve, fwd_b)

    assert va.cashflows[0] == vb.cashflows[0]       # 历史定盘金额冻结
    assert va.cashflows[1] == vb.cashflows[1]       # 已定盘期同样冻结
    assert va.cashflows[2].interest_amount < vb.cashflows[2].interest_amount
    assert va.npv < vb.npv

    # 逐笔可解释：哪次定盘 / 哪段预测
    e0, e1, e2 = va.rate_explanations
    assert e0.rate_source == "FIXING" and e0.fixing_date == date(2025, 2, 11)
    assert e0.reference_rate == pytest.approx(0.0210)
    assert e0.forecast_curve_id is None
    assert e1.rate_source == "FIXING" and e1.fixing_date == date(2025, 5, 29)
    assert e2.rate_source == "FORECAST"
    assert e2.all_in_rate == pytest.approx(e2.reference_rate + 0.005)
    assert e2.forecast_curve_id == "FWD-A"

    # 旧版估值记录的日程不随新曲线变化
    assert va.cashflows[0].interest_amount == vb.cashflows[0].interest_amount


def test_current_accruing_period_requires_fixing_when_fixing_date_passed():
    # 估值日恰好在第二期计息期中间，第二期定盘日 5/29 已过
    v_date = date(2025, 6, 20)
    forecast = _flat_curve(ForecastCurve, "FWD", v_date, 0.03, date(2026, 6, 30))
    contract = _contract(known_fixings={date(2025, 2, 11): 0.021})
    with pytest.raises(MissingFixingError):
        ValuationBatch("B").run(
            contract,
            _flat_curve(DiscountCurve, "DF", v_date, 0.04, date(2026, 6, 30)),
            forecast)


def test_fixed_contract_schedule_unchanged_by_forecast_scenarios():
    """固定利率合约估值结果不依赖预测曲线，日程依旧是不可变快照。"""
    from finval import FixedRateContract
    fixed = FixedRateContract(
        contract_id="FIXED",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 2, 13),
        end_date=date(2025, 11, 30),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.FOLLOWING,
        calendar=Calendar(holidays={date(2025, 9, 1)}),
        stub=StubType.SHORT_FIRST,
    )
    va = ValuationBatch("B").run(
        fixed, _flat_curve(DiscountCurve, "DF", date(2025, 2, 10), 0.04,
                           date(2026, 6, 30)))
    vb = ValuationBatch("B").run(
        fixed, _flat_curve(DiscountCurve, "DF2", date(2025, 2, 10), 0.05,
                           date(2026, 6, 30)))
    assert va.cashflows == vb.cashflows
    assert all(e.rate_source == "FIXED" for e in va.rate_explanations)
