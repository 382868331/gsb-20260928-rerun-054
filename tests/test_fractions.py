import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import date

import pytest

from finval import (
    BusinessDayConvention,
    CashPriceFixing,
    DayCount,
    DiscountCurve,
    ElectionRevocation,
    FixedRateContract,
    FractionElection,
    Frequency,
    RightsTransfer,
    StubType,
    UnitChange,
    ValuationBatch,
    fraction_settlement,
)


def make_contract(events=(), total_units=100.0):
    return FixedRateContract(
        contract_id="FR-1",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.SHORT_FIRST,
        total_units=total_units,
        events=tuple(events),
    )


def make_curve(valuation_date):
    return DiscountCurve(
        curve_id="C",
        valuation_date=valuation_date,
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(0.98,),
    )


T_YI = RightsTransfer(
    event_id="T-YI", to_holder="乙", ratio=1 / 3,
    effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
    basis="转让协议")
MERGE1 = UnitChange(
    event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
    received_date=date(2025, 10, 1), basis="3并1")
MERGE2 = UnitChange(
    event_id="U2", effective_date=date(2025, 11, 1), factor=0.5,
    received_date=date(2025, 11, 1), basis="2并1")
AS_OF = date(2025, 12, 1)


def run(events):
    contract = make_contract(events=events)
    return ValuationBatch("B").run(contract, make_curve(AS_OF))


def generated_map(report):
    return {(g.unit_change_event_id, g.holder): g.units
            for g in report.generated}


def test_consecutive_merges_retain_accumulates_exact():
    # 连续两次合并，未作选择默认保留累积；内部用精确分数，不用显示舍入值
    valuation = run((T_YI, MERGE1, MERGE2))
    report = fraction_settlement(valuation, as_of=AS_OF)
    report.check_conservation()

    gen = generated_map(report)
    # U1（1/3 口径）：乙 100/9 单位 -> 零碎 1/9；ORIGINAL 200/9 -> 零碎 2/9
    assert gen[("U1", "乙")] == pytest.approx(1 / 9)
    assert gen[("U1", "ORIGINAL")] == pytest.approx(2 / 9)
    # U2（1/6 口径，保留的零碎继续参与）：乙 50/9 -> 零碎 5/9；ORIGINAL 100/9 -> 零碎 1/9
    assert gen[("U2", "乙")] == pytest.approx(5 / 9)
    assert gen[("U2", "ORIGINAL")] == pytest.approx(1 / 9)

    retained = report.retained_units()
    assert retained["乙"] == pytest.approx(1 / 9 + 5 / 9)
    assert retained["ORIGINAL"] == pytest.approx(2 / 9 + 1 / 9)
    assert all(r.kind == "RETAINED" for r in report.records)
    assert report.total_residual == 0.0


def test_cross_holder_fraction_transfer():
    # 乙把 U1 零碎单位转给 ORIGINAL；转入部分参与 ORIGINAL 后续合并
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="TRANSFER", to_holder="ORIGINAL",
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
        basis="余量转让协议")
    valuation = run((T_YI, MERGE1, MERGE2, election))
    report = fraction_settlement(valuation, as_of=AS_OF)
    report.check_conservation()

    out = [r for r in report.records if r.kind == "TRANSFER_OUT"]
    into = [r for r in report.records if r.kind == "TRANSFER_IN"]
    assert len(out) == len(into) == 1
    assert out[0].units == pytest.approx(1 / 9)
    assert into[0].holder == "ORIGINAL"

    # ORIGINAL 在 U2 的权益 = 2/3 + (1/9)/(100/3)，零碎单位相应变化
    gen = generated_map(report)
    expected_equity = 2 / 3 + (1 / 9) / (100 / 3)
    expected_units = expected_equity * 100 / 6
    assert gen[("U2", "ORIGINAL")] == pytest.approx(
        expected_units - int(expected_units))
    # 乙在 U1 的零碎已转出（权益扣减 1/300），U2 单位 5.5 -> 零碎 0.5
    assert gen[("U2", "乙")] == pytest.approx(0.5)


def test_cash_pending_then_priced_then_corrected():
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="CASH",
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
        basis="现金替代选择")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=9.9,
        received_date=date(2025, 10, 20), basis="管理人定价公告")
    correction = CashPriceFixing(
        event_id="P2", unit_change_event_id="U1", price_per_unit=10.0,
        received_date=date(2025, 11, 5), basis="价格更正通知",
        corrects_event_id="P1")
    events = (T_YI, MERGE1, election, price, correction)

    # 价格未知期间：待定现金权益与可用现金分列
    pending = fraction_settlement(run(events), as_of=date(2025, 10, 15))
    summary = pending.cash_summary()
    assert summary["乙"]["pending_units"] == pytest.approx(1 / 9)
    assert summary["乙"]["available_cash"] == 0.0
    assert [r.kind for r in pending.holder_records("乙")] == ["PENDING_CASH"]
    pending.check_conservation()

    # 定价后：可用现金 = 1/9 * 9.9 = 1.1
    priced = fraction_settlement(run(events), as_of=date(2025, 10, 25))
    cash = [r for r in priced.holder_records("乙") if r.kind == "CASH_IN_LIEU"]
    assert len(cash) == 1
    assert cash[0].cash_amount == pytest.approx(1.1)
    assert cash[0].event_id == "P1"
    priced.check_conservation()

    # 更正价格：只追加 PRICE_ADJUSTMENT，原分配记录不变
    corrected = fraction_settlement(run(events), as_of=AS_OF)
    cash = [r for r in corrected.holder_records("乙") if r.kind == "CASH_IN_LIEU"]
    adj = [r for r in corrected.holder_records("乙")
           if r.kind == "PRICE_ADJUSTMENT"]
    assert len(cash) == 1 and cash[0].cash_amount == pytest.approx(1.1)
    assert len(adj) == 1
    assert adj[0].event_id == "P2"
    assert adj[0].exact_cash == pytest.approx((1 / 9) * 0.1)
    assert adj[0].cash_amount == pytest.approx(round((1 / 9) * 0.1, 6))
    corrected.check_conservation()


def test_correction_then_partial_revocation_keeps_original_allocation():
    unit_change = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=0.25,
        received_date=date(2025, 10, 1), basis="4并1")
    election = FractionElection(
        event_id="E1", holder="ORIGINAL", unit_change_event_id="U1",
        method="CASH", effective_date=date(2025, 10, 1),
        received_date=date(2025, 10, 1), basis="现金替代选择")
    first_price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=7.2,
        received_date=date(2025, 10, 20), basis="首次定价")
    corrected_price = CashPriceFixing(
        event_id="P2", unit_change_event_id="U1", price_per_unit=8.4,
        received_date=date(2025, 11, 1), basis="价格更正",
        corrects_event_id="P1")
    revocation = ElectionRevocation(
        event_id="R1", election_event_id="E1", portion=0.25,
        received_date=date(2025, 11, 10), basis="撤回四分之一")
    events = (unit_change, election, first_price, corrected_price, revocation)
    contract = make_contract(events=events, total_units=1.0)
    valuation = ValuationBatch("B").run(contract, make_curve(AS_OF))
    report = fraction_settlement(valuation, as_of=AS_OF)
    report.check_conservation()

    records = report.holder_records("ORIGINAL")
    assert [r.kind for r in records] == [
        "CASH_IN_LIEU", "PRICE_ADJUSTMENT", "RETAINED",
        "REVOCATION_ADJUSTMENT"]
    cash, price_adjustment, retained, clawback = records
    assert cash.units == pytest.approx(0.25)
    assert cash.cash_amount == pytest.approx(1.8)
    assert price_adjustment.units == pytest.approx(0.25)
    assert price_adjustment.exact_cash == pytest.approx(0.3)
    assert retained.units == pytest.approx(0.0625)
    assert clawback.units == pytest.approx(0.0625)
    assert clawback.price == pytest.approx(8.4)
    assert clawback.exact_cash == pytest.approx(-0.525)
    assert report.cash_summary()["ORIGINAL"] == {
        "pending_units": 0.0,
        "available_cash": pytest.approx(1.575),
        "adjustments": pytest.approx(-0.225),
    }


def test_revocation_before_pricing_only_restores_units():
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="CASH", effective_date=date(2025, 10, 1),
        received_date=date(2025, 10, 1), basis="现金替代选择")
    revocation = ElectionRevocation(
        event_id="R1", election_event_id="E1", portion=0.25,
        received_date=date(2025, 10, 10), basis="撤回前未定价")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=8.4,
        received_date=date(2025, 10, 20), basis="撤回后定价")
    report = fraction_settlement(
        run((T_YI, MERGE1, election, revocation, price)), as_of=AS_OF)
    report.check_conservation()

    records = report.holder_records("乙")
    assert [r.kind for r in records] == ["RETAINED", "CASH_IN_LIEU"]
    retained, cash = records
    assert retained.units == pytest.approx(1 / 36)
    assert cash.units == pytest.approx(1 / 12)
    assert cash.cash_amount == pytest.approx((1 / 12) * 8.4)
    assert not [r for r in records if r.kind == "REVOCATION_ADJUSTMENT"]
    assert report.cash_summary()["乙"]["adjustments"] == 0.0


def test_consecutive_price_corrections_append_each_delta_once():
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="CASH", effective_date=date(2025, 10, 1),
        received_date=date(2025, 10, 1), basis="现金替代选择")
    prices = (
        CashPriceFixing(event_id="P1", unit_change_event_id="U1",
                        price_per_unit=7.2, received_date=date(2025, 10, 20),
                        basis="首次定价"),
        CashPriceFixing(event_id="P2", unit_change_event_id="U1",
                        price_per_unit=8.4, received_date=date(2025, 11, 1),
                        basis="第一次更正", corrects_event_id="P1"),
        CashPriceFixing(event_id="P3", unit_change_event_id="U1",
                        price_per_unit=8.0, received_date=date(2025, 11, 2),
                        basis="第二次更正", corrects_event_id="P2"),
    )
    report = fraction_settlement(run((T_YI, MERGE1, election) + prices), as_of=AS_OF)
    adjustments = [r for r in report.holder_records("乙")
                   if r.kind == "PRICE_ADJUSTMENT"]
    assert [r.event_id for r in adjustments] == ["P2", "P3"]
    assert adjustments[0].exact_cash == pytest.approx((1 / 9) * 1.2)
    assert adjustments[1].exact_cash == pytest.approx((1 / 9) * -0.4)
    assert report.cash_summary()["乙"]["available_cash"] == pytest.approx(
        (1 / 9) * 8.0)


def test_correction_and_partial_revocation_conserve_across_holders():
    t_bing = RightsTransfer(
        event_id="T-BING", to_holder="丙", ratio=1 / 3,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="转让协议")
    elections = tuple(
        FractionElection(
            event_id=f"E-{holder}", holder=holder,
            unit_change_event_id="U1", method="CASH",
            effective_date=date(2025, 10, 1),
            received_date=date(2025, 10, 1), basis="现金替代选择")
        for holder in ("乙", "丙", "ORIGINAL"))
    events = (
        T_YI, t_bing, MERGE1,
        *elections,
        CashPriceFixing(event_id="P1", unit_change_event_id="U1",
                        price_per_unit=7.2, received_date=date(2025, 10, 20),
                        basis="首次定价"),
        CashPriceFixing(event_id="P2", unit_change_event_id="U1",
                        price_per_unit=8.4, received_date=date(2025, 11, 1),
                        basis="价格更正", corrects_event_id="P1"),
        ElectionRevocation(event_id="R-ALL", election_event_id="E-乙",
                           portion=0.25, received_date=date(2025, 11, 5),
                           basis="乙撤回"),
        ElectionRevocation(event_id="R-BING", election_event_id="E-丙",
                           portion=0.25, received_date=date(2025, 11, 5),
                           basis="丙撤回"),
        ElectionRevocation(event_id="R-ORIG", election_event_id="E-ORIGINAL",
                           portion=0.25, received_date=date(2025, 11, 5),
                           basis="ORIGINAL撤回"),
    )
    report = fraction_settlement(run(events), as_of=AS_OF)
    report.check_conservation()

    summaries = report.cash_summary()
    for holder in ("乙", "丙", "ORIGINAL"):
        assert summaries[holder]["available_cash"] == pytest.approx(0.7)
        assert summaries[holder]["adjustments"] == pytest.approx(
            (1 / 9) * 1.2 - (1 / 36) * 8.4)
    assert sum(v["available_cash"] for v in summaries.values()) == pytest.approx(2.1)
    assert sum(report.retained_units().values()) == pytest.approx(3 / 36)


def test_fraction_settlement_survives_valuation_save_restore(tmp_path):
    election = FractionElection(
        event_id="E1", holder="ORIGINAL", unit_change_event_id="U1",
        method="CASH", effective_date=date(2025, 10, 1),
        received_date=date(2025, 10, 1), basis="现金替代选择")
    unit_change = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=0.25,
        received_date=date(2025, 10, 1), basis="4并1")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=8.4,
        received_date=date(2025, 10, 20), basis="定价")
    contract = make_contract(
        events=(unit_change, election, price), total_units=1.0)
    batch = ValuationBatch("B")
    batch.run(contract, make_curve(AS_OF))
    path = tmp_path / "valuation.json"
    batch.save_json(path)
    restored = ValuationBatch.load_json(path).get("FR-1", "C")
    expected = fraction_settlement(batch.get("FR-1", "C"), as_of=AS_OF)
    actual = fraction_settlement(restored, as_of=AS_OF)
    assert actual.records == expected.records
    assert actual.cash_summary() == expected.cash_summary()


def test_partial_election_revocation_tracked_adjustment():
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="CASH",
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
        basis="现金替代选择")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=9.9,
        received_date=date(2025, 10, 20), basis="管理人定价公告")
    revocation = ElectionRevocation(
        event_id="R1", election_event_id="E1", portion=0.5,
        received_date=date(2025, 11, 10), basis="部分撤回通知")
    events = (T_YI, MERGE1, election, price, revocation)

    # 撤回到达前：全额现金替代
    before = fraction_settlement(run(events), as_of=date(2025, 11, 1))
    assert [r.kind for r in before.holder_records("乙")] == ["CASH_IN_LIEU"]
    assert before.holder_records("乙")[0].units == pytest.approx(1 / 9)

    # 撤回一半：一半恢复保留，已分配现金以调整记录追回，原记录不改写
    after = fraction_settlement(run(events), as_of=AS_OF)
    after.check_conservation()
    yi = after.holder_records("乙")
    kinds = [r.kind for r in yi]
    assert kinds == ["CASH_IN_LIEU", "RETAINED", "REVOCATION_ADJUSTMENT"]
    cash, retained, clawback = yi
    assert retained.units == pytest.approx(1 / 18)
    assert retained.event_id == "R1"
    assert cash.units == pytest.approx(1 / 9)
    assert cash.cash_amount == pytest.approx(1.1)
    assert clawback.exact_cash == pytest.approx(-0.55)
    assert clawback.event_id == "R1"
    summary = after.cash_summary()
    assert summary["乙"]["available_cash"] == pytest.approx(0.55)
    assert summary["乙"]["adjustments"] == pytest.approx(-0.55)


def test_rounding_residuals_reconcile_across_holders():
    # 多个持有人现金替代产生循环小数舍入余量：逐笔记录、总和可核对
    t_bing = RightsTransfer(
        event_id="T-BING", to_holder="丙", ratio=1 / 3,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="转让协议")
    elections = tuple(
        FractionElection(
            event_id=f"E-{h}", holder=h, unit_change_event_id="U1",
            method="CASH",
            effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
            basis="现金替代选择")
        for h in ("乙", "丙", "ORIGINAL"))
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=1 / 3,
        received_date=date(2025, 10, 20), basis="管理人定价公告")
    events = (T_YI, t_bing, MERGE1) + elections + (price,)
    report = fraction_settlement(run(events), as_of=AS_OF)
    report.check_conservation()

    cash = [r for r in report.records if r.kind == "CASH_IN_LIEU"]
    assert len(cash) == 3
    # 每人 1/9 单位 * 1/3 元 = 1/27 元，落账 0.037037，余量逐笔记录
    for r in cash:
        assert r.cash_amount == pytest.approx(round(1 / 27, 6))
        assert r.exact_cash == pytest.approx(1 / 27)
    booked = sum(r.cash_amount for r in cash)
    exact = sum(r.exact_cash for r in cash)
    assert report.total_residual != 0.0
    assert report.total_residual == pytest.approx(exact - booked)
    assert booked + report.total_residual == pytest.approx(exact)


def test_same_day_ordering_and_deadline():
    # 选择截止日与合并生效日同一天：视为有效（同日即已生效）
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1",
        method="CASH",
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
        basis="截止日当天选择")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=9.0,
        received_date=date(2025, 10, 2), basis="定价公告")
    report = fraction_settlement(
        run((T_YI, MERGE1, election, price)), as_of=AS_OF)
    assert [r.kind for r in report.holder_records("乙")] == ["CASH_IN_LIEU"]
    report.check_conservation()

    # 截止日早于合并生效日：选择不适用，默认保留累积
    late = FractionElection(
        event_id="E2", holder="乙", unit_change_event_id="U1",
        method="CASH",
        effective_date=date(2025, 9, 30), received_date=date(2025, 9, 30),
        basis="截止日过早")
    report2 = fraction_settlement(
        run((T_YI, MERGE1, late, price)), as_of=AS_OF)
    assert [r.kind for r in report2.holder_records("乙")] == ["RETAINED"]
    report2.check_conservation()
