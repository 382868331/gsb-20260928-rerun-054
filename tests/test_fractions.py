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
    FractionLedger,
    Frequency,
    RightsTransfer,
    StubType,
    UnitChange,
    ValuationBatch,
    fraction_ledger,
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


def make_valuation(events, valuation_date=date(2025, 12, 1), total_units=100.0):
    contract = make_contract(events=events, total_units=total_units)
    return ValuationBatch("B").run(contract, make_curve(valuation_date))


TRANSFER_40 = RightsTransfer(
    event_id="T1", to_holder="乙", ratio=0.4,
    effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
    basis="转让 40%")


def test_keep_fraction_accumulates_across_consecutive_changes():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="7并1")
    split = UnitChange(
        event_id="U2", effective_date=date(2025, 11, 1), factor=2.0,
        received_date=date(2025, 11, 1), basis="1拆2")
    valuation = make_valuation((TRANSFER_40, merge, split))
    ledger = fraction_ledger(valuation, date(2025, 12, 1))

    # 乙：40 -> 13+1/3（保留）-> 26+2/3（保留），ORIGINAL：60 -> 20 -> 40
    assert ledger.position_of("乙") == pytest.approx(40 / 3 * 2)
    assert ledger.position_of("ORIGINAL") == pytest.approx(40.0)
    kept = [e for e in ledger.holder_entries("乙") if e.kind == "UNITS_KEPT"]
    assert [e.unit_change_event_id for e in kept] == ["U1", "U2"]
    assert kept[0].units == pytest.approx(1 / 3)
    assert kept[1].units == pytest.approx(2 / 3)
    ledger.check_conservation()


def test_cash_pending_then_priced_then_corrected():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1")
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 9, 30), basis="现金替代选择")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=3.0,
        received_date=date(2025, 10, 20), basis="首次定价")
    correction = CashPriceFixing(
        event_id="P2", unit_change_event_id="U1", price_per_unit=3.6,
        received_date=date(2025, 11, 1), basis="价格更正")

    # 价格未知期间：待定现金权益与可用现金分列
    v1 = make_valuation((TRANSFER_40, merge, election))
    pending = fraction_ledger(v1, date(2025, 10, 15))
    assert pending.pending_cash_units("乙") == pytest.approx(1 / 3)
    assert pending.available_cash("乙") == 0.0
    assert pending.position_of("乙") == pytest.approx(13.0)
    pending.check_conservation()

    # 定价后：待定转为可用现金
    v2 = make_valuation((TRANSFER_40, merge, election, price))
    priced = fraction_ledger(v2, date(2025, 10, 25))
    assert priced.pending_cash_units("乙") == 0.0
    assert priced.available_cash("乙") == pytest.approx(1.0)
    priced.check_conservation()

    # 更正价格：原记录不变，差额以 ADJUSTMENT 体现
    v3 = make_valuation((TRANSFER_40, merge, election, price, correction))
    corrected = fraction_ledger(v3, date(2025, 11, 5))
    cash_entry = [e for e in corrected.holder_entries("乙")
                  if e.kind == "CASH"][0]
    assert cash_entry.price == 3.0
    assert cash_entry.cash == pytest.approx(1.0)
    adj = [e for e in corrected.holder_entries("乙") if e.kind == "ADJUSTMENT"]
    assert len(adj) == 1
    assert adj[0].event_id == "P2"
    assert adj[0].cash == pytest.approx(0.2)
    assert corrected.available_cash("乙") == pytest.approx(1.2)
    corrected.check_conservation()


def test_transfer_election_moves_fraction_to_other_holder():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1")
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="TRANSFER",
        to_holder="丙", received_date=date(2025, 9, 30), basis="余量转给丙")
    valuation = make_valuation((TRANSFER_40, merge, election))
    ledger = fraction_ledger(valuation, date(2025, 10, 15))

    assert ledger.position_of("乙") == pytest.approx(13.0)
    assert ledger.position_of("丙") == pytest.approx(1 / 3)
    assert ledger.position_of("ORIGINAL") == pytest.approx(20.0)
    entry = [e for e in ledger.holder_entries("乙")
             if e.kind == "UNITS_TRANSFERRED"][0]
    assert entry.to_holder == "丙"
    assert entry.units == pytest.approx(1 / 3)
    ledger.check_conservation()


def test_partial_revocation_after_pricing_creates_traceable_adjustment():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1")
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 9, 30), basis="现金替代选择")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=3.0,
        received_date=date(2025, 10, 20), basis="定价")
    revoke_half = ElectionRevocation(
        event_id="R1", election_event_id="E1", fraction=0.5,
        received_date=date(2025, 11, 1), basis="撤回一半选择")
    valuation = make_valuation((TRANSFER_40, merge, election, price, revoke_half))
    ledger = fraction_ledger(valuation, date(2025, 11, 5))

    # 原 CASH 记录不改写；撤回部分以 ADJUSTMENT 冲回现金并回补单位
    cash_entry = [e for e in ledger.holder_entries("乙")
                  if e.kind == "CASH"][0]
    assert cash_entry.units == pytest.approx(1 / 3)
    assert cash_entry.cash == pytest.approx(1.0)
    adj = [e for e in ledger.holder_entries("乙") if e.kind == "ADJUSTMENT"]
    assert len(adj) == 1
    assert adj[0].event_id == "R1"
    assert adj[0].cash == pytest.approx(-0.5)
    assert adj[0].units == pytest.approx(1 / 6)
    assert ledger.available_cash("乙") == pytest.approx(0.5)
    assert ledger.position_of("乙") == pytest.approx(13 + 1 / 6)
    ledger.check_conservation()


def test_same_day_split_transfer_and_deadline_have_clear_basis():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1",
        election_deadline=date(2025, 10, 1))
    election = FractionElection(          # 截止日当天到达：生效
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 10, 1), basis="截止日当天选择")
    transfer = RightsTransfer(            # 同日转移：按拆并后口径折算
        event_id="T2", to_holder="丙", ratio=0.1,
        effective_date=date(2025, 10, 1), received_date=date(2025, 9, 25),
        basis="同日转移")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=3.0,
        received_date=date(2025, 10, 5), basis="定价")
    valuation = make_valuation((TRANSFER_40, merge, election, transfer, price))
    ledger = fraction_ledger(valuation, date(2025, 10, 10))

    assert ledger.available_cash("乙") == pytest.approx(1.0)   # 截止日当天选择生效
    assert ledger.position_of("乙") == pytest.approx(13.0)
    # ORIGINAL 60 -> 20，再按拆并后口径转出 0.1 * 100 / 3 单位给丙
    assert ledger.position_of("丙") == pytest.approx(10 / 3)
    assert ledger.position_of("ORIGINAL") == pytest.approx(20 - 10 / 3)
    ledger.check_conservation()


def test_late_election_defaults_to_keep():
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1")
    late = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 10, 2), basis="逾期选择")
    valuation = make_valuation((TRANSFER_40, merge, late))
    ledger = fraction_ledger(valuation, date(2025, 10, 15))

    assert ledger.pending_cash_units("乙") == 0.0
    assert ledger.position_of("乙") == pytest.approx(40 / 3)   # 默认保留
    assert [e.kind for e in ledger.holder_entries("乙")] == ["UNITS_KEPT"]
    ledger.check_conservation()


def test_rounding_residual_reconciles_across_holders():
    t_b = RightsTransfer(
        event_id="TB", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="40%")
    t_c = RightsTransfer(
        event_id="TC", to_holder="丙", ratio=0.11,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="11%")
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 7,
        received_date=date(2025, 10, 1), basis="7并1")
    e_b = FractionElection(
        event_id="EB", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 9, 30), basis="现金替代")
    e_c = FractionElection(
        event_id="EC", holder="丙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 9, 30), basis="现金替代")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=0.3,
        received_date=date(2025, 10, 5), basis="定价")
    valuation = make_valuation((t_b, t_c, merge, e_b, e_c, price))
    ledger = fraction_ledger(valuation, date(2025, 10, 10))

    # 乙 40 -> 5+5/7，丙 11 -> 1+4/7，均现金替代，价格 0.3 产生舍入余量
    exact_b, exact_c = (5 / 7) * 0.3, (4 / 7) * 0.3
    recorded_b, recorded_c = round(exact_b, 6), round(exact_c, 6)
    assert ledger.available_cash("乙") == pytest.approx(recorded_b)
    assert ledger.available_cash("丙") == pytest.approx(recorded_c)
    summary = ledger.residual_summary()
    assert summary["U1"] == pytest.approx(
        (exact_b - recorded_b) + (exact_c - recorded_c))
    assert summary["TOTAL"] == pytest.approx(summary["U1"])
    assert summary["TOTAL"] != 0.0
    # 余量单列：已记录现金 + 余量 = 精确现金，不多分也不少分
    assert ledger.available_cash() + summary["TOTAL"] == pytest.approx(
        exact_b + exact_c)
    ledger.check_conservation()


def test_ledger_save_load_roundtrip(tmp_path):
    merge = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
        received_date=date(2025, 10, 1), basis="3并1")
    election = FractionElection(
        event_id="E1", holder="乙", unit_change_event_id="U1", choice="CASH",
        received_date=date(2025, 9, 30), basis="现金替代")
    price = CashPriceFixing(
        event_id="P1", unit_change_event_id="U1", price_per_unit=3.0,
        received_date=date(2025, 10, 20), basis="定价")
    valuation = make_valuation((TRANSFER_40, merge, election, price))
    ledger = fraction_ledger(valuation, date(2025, 10, 25))
    path = tmp_path / "ledger.json"
    ledger.save_json(path)
    assert FractionLedger.load_json(path) == ledger
