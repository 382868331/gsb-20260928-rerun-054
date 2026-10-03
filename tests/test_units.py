import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import date

import pytest

from finval import (
    AllocationReport,
    BusinessDayConvention,
    DayCount,
    DiscountCurve,
    FixedRateContract,
    Frequency,
    PartialTermination,
    RightsTransfer,
    StubType,
    TransferRevocation,
    UnitChange,
    ValuationBatch,
    allocate,
    holder_positions,
)


def make_contract(events=(), total_units=100.0):
    # 2025-03-15 -> 2026-03-31 半年付息：支付日 2025-09-30 / 2026-03-31
    return FixedRateContract(
        contract_id="UC-1",
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


CF1 = round(1_000_000 * 0.06 * 199 / 360, 6)   # 33166.666667
CF2 = round(1_000_000 * 0.06 * 182 / 360, 6)   # 30333.333333


def positions_dict(valuation, as_of, at_date=None):
    return {p.holder: p for p in holder_positions(valuation, as_of, at_date)}


def test_split_changes_units_not_equity():
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="转让协议")
    split = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 15), factor=10.0,
        received_date=date(2025, 10, 15), basis="1拆10")
    contract = make_contract(events=(transfer, split))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 12, 1)))

    # 拆分前：100 单位口径；拆分后：1000 单位口径，权益比例不变
    before = positions_dict(valuation, date(2025, 12, 1), at_date=date(2025, 10, 1))
    assert before["乙"].units == pytest.approx(40.0)
    assert before["ORIGINAL"].units == pytest.approx(60.0)
    after = positions_dict(valuation, date(2025, 12, 1), at_date=date(2025, 12, 1))
    assert after["乙"].units == pytest.approx(400.0)
    assert after["ORIGINAL"].units == pytest.approx(600.0)
    assert after["乙"].equity_ratio == pytest.approx(0.4)
    assert after["ORIGINAL"].equity_ratio == pytest.approx(0.6)

    # 单位变多不能凭空增加应收金额：各方应收合计仍等于合约现金流
    report = allocate(valuation, as_of=date(2025, 12, 1))
    report.check_conservation()
    totals = report.totals_by_holder()
    assert totals["乙"]["receivable"] == pytest.approx(round(0.4 * CF2, 6))
    grand = sum(sum(b.values()) for b in totals.values())
    assert grand == pytest.approx(CF1 + CF2)

    # 每笔现金流标明采用的单位口径（变化前 1 / 变化后 10）
    factors = {s.payment_date: s.unit_factor for s in report.shares
               if s.kind == "SCHEDULED_SHARE"}
    assert factors[date(2025, 9, 30)] == pytest.approx(1.0)
    assert factors[date(2026, 3, 31)] == pytest.approx(10.0)


def test_fixed_units_vs_ratio_across_merge():
    t_units = RightsTransfer(
        event_id="TU", to_holder="乙", units=20.0,      # 拆分前 20/100 = 20%
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="固定单位转让")
    split = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 1), factor=2.0,
        received_date=date(2025, 10, 1), basis="1拆2")
    t_ratio = RightsTransfer(
        event_id="TR", to_holder="丙", ratio=0.5,       # 原持有人剩余权益的 50%
        effective_date=date(2025, 11, 1), received_date=date(2025, 11, 1),
        basis="比例转让", from_holder="ORIGINAL")
    merge = UnitChange(
        event_id="U2", effective_date=date(2026, 1, 1), factor=0.5,
        received_date=date(2026, 1, 1), basis="2并1")
    contract = make_contract(events=(t_units, split, t_ratio, merge))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2026, 1, 15)))

    # 合并不混淆两种约定：固定单位转让仍是 20% 权益，比例转让仍是 50%
    pos = positions_dict(valuation, date(2026, 1, 15))
    assert pos["乙"].equity_ratio == pytest.approx(0.2)
    assert pos["乙"].units == pytest.approx(20.0)        # 合并后回到 100 单位口径
    assert pos["丙"].equity_ratio == pytest.approx(0.5)
    assert pos["丙"].units == pytest.approx(50.0)
    assert pos["ORIGINAL"].equity_ratio == pytest.approx(0.3)

    report = allocate(valuation, as_of=date(2026, 1, 15))
    report.check_conservation()
    totals = report.totals_by_holder()
    assert totals["乙"]["receivable"] == pytest.approx(round(0.2 * CF2, 6))
    assert totals["丙"]["receivable"] == pytest.approx(round(0.5 * CF2, 6))


def test_late_split_notice_changes_unit_transfer_reading():
    # 拆分 8/1 生效但 10/10 才通知；9/1 约定转让 50 单位
    split = UnitChange(
        event_id="U1", effective_date=date(2025, 8, 1), factor=10.0,
        received_date=date(2025, 10, 10), basis="迟到拆分通知")
    t_units = RightsTransfer(
        event_id="TU", to_holder="乙", units=50.0,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="固定单位转让")
    contract = make_contract(events=(split, t_units))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 10, 15)))

    # 历史视角（拆分通知未到达）：50/100 = 50% 归乙
    hist = allocate(valuation, as_of=date(2025, 10, 5), view="HISTORICAL")
    hist_totals = hist.totals_by_holder()
    assert hist_totals["乙"]["received"] == pytest.approx(round(0.5 * CF1, 6))
    hist.check_conservation()

    # 当前视角：50 单位按拆分后口径为 50/1000 = 5%，差额以补偿说明
    cur = allocate(valuation, as_of=date(2025, 10, 15), view="CURRENT")
    comp = {s.holder: s.amount for s in cur.shares if s.kind == "COMPENSATION"}
    assert set(comp) == {"乙", "ORIGINAL"}
    assert comp["乙"] == pytest.approx((0.05 - 0.5) * CF1, abs=1e-5)
    assert comp["ORIGINAL"] == pytest.approx((0.5 - 0.05) * CF1, abs=1e-5)
    assert comp["乙"] == pytest.approx(-comp["ORIGINAL"])
    assert all(s.event_id == "U1" for s in cur.shares if s.kind == "COMPENSATION")
    totals = cur.totals_by_holder()
    assert totals["乙"]["receivable"] == pytest.approx(round(0.05 * CF2, 6))
    cur.check_conservation()


def test_rounding_residual_reported_not_hidden():
    third = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=1 / 3,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="三分之一")
    third2 = RightsTransfer(
        event_id="T2", to_holder="丙", ratio=1 / 3,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="三分之一")
    contract = make_contract(events=(third, third2))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 10, 15)))
    report = allocate(valuation, as_of=date(2025, 10, 15))

    # 每个持有人份额独立舍入，余量单列，不藏进任一持有人
    each = round(CF1 / 3, 6)
    paid = {s.holder: s.amount for s in report.shares if s.payment_state == "PAID"}
    assert paid == {"ORIGINAL": each, "乙": each, "丙": each}
    view = {row["payment_date"]: row for row in report.contract_view()}
    row1 = view[date(2025, 9, 30)]
    assert row1["allocated"] == pytest.approx(round(3 * each, 6))
    assert row1["unallocated_residual"] == pytest.approx(
        round(CF1 - 3 * each, 6))
    assert row1["unallocated_residual"] != 0.0
    report.check_conservation()   # 份额 + 余量 = 原金额


def test_combined_transfer_split_payment_termination_revocation(tmp_path):
    t1 = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="转让一")
    split = UnitChange(
        event_id="U1", effective_date=date(2025, 10, 15), factor=10.0,
        received_date=date(2025, 10, 20), basis="1拆10（迟到通知）")
    t2 = RightsTransfer(
        event_id="T2", to_holder="丙", units=30.0,       # 拆分后口径 30/1000 = 3%
        effective_date=date(2025, 11, 1), received_date=date(2025, 11, 1),
        basis="固定单位转让")
    termination = PartialTermination(
        event_id="TERM", effective_date=date(2025, 12, 1),
        reduction_amount=200_000.0, received_date=date(2025, 11, 20),
        basis="部分终止")
    t3 = RightsTransfer(                                  # 尚未生效即被撤销
        event_id="T3", to_holder="丁", ratio=0.5,
        effective_date=date(2026, 1, 1), received_date=date(2025, 12, 1),
        basis="未来转让")
    revoke3 = TransferRevocation(
        event_id="R3", transfer_event_id="T3",
        received_date=date(2025, 12, 10), basis="生效前撤销")
    events = (t1, split, t2, termination, t3, revoke3)
    contract = make_contract(events=events)
    batch = ValuationBatch("B")
    valuation = batch.run(contract, make_curve(date(2025, 12, 15)))
    as_of = date(2025, 12, 15)
    report = allocate(valuation, as_of=as_of)
    report.check_conservation()

    # CF1（9/30 已付）：支付日仅 T1 到达，乙收 40%，单位口径为拆分前
    paid_cf1 = [s for s in report.shares
                if s.payment_date == date(2025, 9, 30)
                and s.payment_state == "PAID"]
    assert {s.holder for s in paid_cf1} == {"ORIGINAL", "乙"}
    assert all(s.unit_factor == 1.0 for s in paid_cf1)
    # 迟到拆分通知不改变已付款项的金钱归属：无补偿记录
    assert not [s for s in report.shares
                if s.cashflow_index == paid_cf1[0].cashflow_index
                and s.kind == "COMPENSATION"]

    # 终止返还款（12/1 已付）：按 T1+拆分+T2 的归属 57/40/3
    term = [s for s in report.shares if s.cashflow_kind == "TERMINATION_PRINCIPAL"]
    assert sum(s.amount for s in term) == pytest.approx(200_000.0)
    assert sum(s.amount for s in term if s.holder == "乙") == pytest.approx(80_000.0)
    assert sum(s.amount for s in term if s.holder == "丙") == pytest.approx(6_000.0)
    assert all(s.unit_factor == 10.0 for s in term)

    # 撤销未生效安排只影响未来决定：无丁的份额，也无任何补偿记录
    assert "丁" not in report.holders
    assert not [s for s in report.shares if s.kind == "COMPENSATION"]

    # 当前持仓：57/40/3 权益，对应拆分后 570/400/30 单位
    pos = positions_dict(valuation, as_of)
    assert pos["ORIGINAL"].equity_ratio == pytest.approx(0.57)
    assert pos["ORIGINAL"].units == pytest.approx(570.0)
    assert pos["乙"].units == pytest.approx(400.0)
    assert pos["丙"].units == pytest.approx(30.0)

    # 历史视角（拆分通知到达前）：乙 40%，丙尚未出现
    hist = allocate(valuation, as_of=date(2025, 10, 19), view="HISTORICAL")
    hist_pos = positions_dict(valuation, date(2025, 10, 19))
    assert hist_pos["乙"].units == pytest.approx(40.0)
    assert "丙" not in hist_pos
    hist.check_conservation()

    # 保存重开后结论一致
    path = tmp_path / "batch.json"
    batch.save_json(path)
    reloaded = ValuationBatch.load_json(path).get("UC-1", "C")
    again = allocate(reloaded, as_of=as_of)
    assert again.totals_by_holder() == report.totals_by_holder()
    assert holder_positions(reloaded, as_of) == holder_positions(valuation, as_of)
    rpath = tmp_path / "report.json"
    report.save_json(rpath)
    assert AllocationReport.load_json(rpath) == report

    # 相同通知按不同顺序接收：同一知识时点结论一致
    shuffled = make_contract(events=(termination, t2, revoke3, split, t3, t1))
    valuation2 = ValuationBatch("B2").run(shuffled, make_curve(date(2025, 12, 15)))
    report2 = allocate(valuation2, as_of=as_of)
    assert report2.totals_by_holder() == report.totals_by_holder()
    assert holder_positions(valuation2, as_of) == holder_positions(valuation, as_of)
    report2.check_conservation()

