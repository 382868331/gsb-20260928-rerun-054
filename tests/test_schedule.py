from datetime import date

import pytest

from finval import (
    BusinessDayConvention,
    Calendar,
    DayCount,
    FixedRateContract,
    Frequency,
    StubType,
    generate_schedule,
)


def test_month_end_anchors_follow_month_ends():
    # 虚构合约：2025-02-28（2月最后一天）起息，季度付息至 2025-11-30
    contract = FixedRateContract(
        contract_id="EOM-Q",
        notional=10_000_000.0,
        fixed_rate=0.04,
        start_date=date(2025, 2, 28),
        end_date=date(2025, 11, 30),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        stub=StubType.NONE,
    )
    cfs = generate_schedule(contract)
    assert [cf.unadjusted_end for cf in cfs] == [
        date(2025, 5, 31),
        date(2025, 8, 31),
        date(2025, 11, 30),
    ]


def test_weekend_modified_following_rolls_back_into_month():
    # 2025-08-31 是周日：FOLLOWING 顺延到 9/1；MODIFIED_FOLLOWING 回到 8/29（周五）
    cal = Calendar()
    assert cal.adjust(date(2025, 8, 31), BusinessDayConvention.FOLLOWING) == date(2025, 9, 1)
    assert cal.adjust(date(2025, 8, 31), BusinessDayConvention.MODIFIED_FOLLOWING) == date(2025, 8, 29)

    contract = FixedRateContract(
        contract_id="EOM-ROLL",
        notional=1.0,
        fixed_rate=0.0,
        start_date=date(2025, 2, 28),
        end_date=date(2025, 11, 30),
        frequency=Frequency.QUARTERLY,
        business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        stub=StubType.NONE,
    )
    cfs = generate_schedule(contract)
    assert cfs[1].payment_date == date(2025, 8, 29)
    # 调整后的周期日同时作为下一段起息日
    assert cfs[2].accrual_start == date(2025, 8, 29)


def test_holiday_blocks_adjustment():
    cal = Calendar(holidays={date(2025, 9, 1)})
    # 8/31 周日、9/1 假日 -> FOLLOWING 顺延到 9/2
    assert cal.adjust(date(2025, 8, 31), BusinessDayConvention.FOLLOWING) == date(2025, 9, 2)


def test_short_first_stub_interest_amounts():
    # 短首期：2025-03-15 起息，半年付息，到期 2026-03-31
    # 倒推锚点：2025-09-30、2026-03-31；首期残期 03-15 -> 09-30
    contract = FixedRateContract(
        contract_id="STUB-SHORT",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.SHORT_FIRST,
    )
    cfs = generate_schedule(contract)
    assert len(cfs) == 2
    first, second = cfs
    assert first.accrual_start == date(2025, 3, 15)
    assert first.accrual_end == date(2025, 9, 30)
    assert first.accrual_factor == pytest.approx((date(2025, 9, 30) - date(2025, 3, 15)).days / 360)
    assert first.interest_amount == pytest.approx(1_000_000 * 0.06 * 199 / 360, abs=1e-4)
    # 第二期为完整周期（9/30 -> 次年 3/31，182 天）
    assert second.accrual_start == date(2025, 9, 30)
    assert second.accrual_end == date(2026, 3, 31)
    assert second.interest_amount == pytest.approx(1_000_000 * 0.06 * 182 / 360, abs=1e-4)


def test_long_first_stub_merges_two_periods():
    contract = FixedRateContract(
        contract_id="STUB-LONG",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.LONG_FIRST,
    )
    cfs = generate_schedule(contract)
    # 首期合并 03-15 -> 2026-03-31 共一段
    assert len(cfs) == 1
    assert cfs[0].accrual_start == date(2025, 3, 15)
    assert cfs[0].accrual_end == date(2026, 3, 31)


@pytest.mark.parametrize("dc,expected", [
    (DayCount.ACT_360, 10 / 360),
    (DayCount.ACT_365F, 10 / 365),
    (DayCount.THIRTY_360, 10 / 360),
])
def test_day_count_conventions_are_selectable(dc, expected):
    from finval.conventions import year_fraction

    assert year_fraction(date(2025, 1, 10), date(2025, 1, 20), dc) == pytest.approx(expected)


def test_unsupported_conventions_are_rejected():
    from finval import UnsupportedConventionError

    with pytest.raises(UnsupportedConventionError):
        FixedRateContract(
            contract_id="BAD",
            notional=1.0,
            fixed_rate=0.05,
            start_date=date(2025, 1, 1),
            end_date=date(2026, 1, 1),
            day_count="ACT/ACT",  # 系统暂不支持，必须明确拒绝
        )


def test_no_stub_on_irregular_period_rejected():
    contract = FixedRateContract(
        contract_id="NO-STUB",
        notional=1.0,
        fixed_rate=0.05,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        stub=StubType.NONE,
    )
    with pytest.raises(ValueError):
        generate_schedule(contract)


def test_schedule_is_immutable_snapshot():
    contract = FixedRateContract(
        contract_id="SNAP",
        notional=1_000.0,
        fixed_rate=0.05,
        start_date=date(2025, 1, 31),
        end_date=date(2026, 1, 31),
        frequency=Frequency.ANNUAL,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.NONE,
    )
    cfs = generate_schedule(contract)
    with pytest.raises(AttributeError):
        cfs[0].interest_amount = 999.0
    assert cfs[0].interest_amount == pytest.approx(1_000 * 0.05 * 365 / 360, abs=1e-4)
