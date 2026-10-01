import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import date
from dataclasses import replace

import pytest

from finval import (
    AllocationReport,
    BusinessDayConvention,
    DayCount,
    DiscountCurve,
    FixedRateContract,
    FixingCorrection,
    FloatingRateContract,
    Frequency,
    PartialTermination,
    RightsTransfer,
    StubType,
    TransferRevocation,
    ValuationBatch,
    allocate,
)


def make_contract(events=()):
    # 2025-03-15 -> 2026-03-31 半年付息：支付日 2025-09-30 / 2026-03-31
    return FixedRateContract(
        contract_id="TR-1",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 3, 15),
        end_date=date(2026, 3, 31),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.SHORT_FIRST,
        events=tuple(events),
    )


def make_curve(valuation_date, df=0.98):
    return DiscountCurve(
        curve_id="C",
        valuation_date=valuation_date,
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(df,),
    )


CF1 = round(1_000_000 * 0.06 * 199 / 360, 6)   # 33166.666667
CF2 = round(1_000_000 * 0.06 * 182 / 360, 6)   # 30333.333333


def test_late_transfer_notice_near_coupon_date():
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 9, 1), received_date=date(2025, 10, 10),
        basis="转让协议")
    contract = make_contract(events=(transfer,))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 10, 15)))

    # 历史视角（通知到达前）：全部归原持有人，保持原结论
    hist = allocate(valuation, as_of=date(2025, 10, 5), view="HISTORICAL")
    assert hist.totals_by_holder() == {
        "ORIGINAL": {"received": CF1, "receivable": CF2, "compensation": 0.0}}
    hist.check_conservation()

    # 当前视角：已支付款项仍归原支付记录，差额以补偿记录说明
    cur = allocate(valuation, as_of=date(2025, 10, 15), view="CURRENT")
    comp = [s for s in cur.shares if s.kind == "COMPENSATION"]
    assert {(s.holder, s.amount) for s in comp} == {
        ("乙", round(0.4 * CF1, 6)), ("ORIGINAL", round(-0.4 * CF1, 6))}
    assert all(s.event_id == "T1" and s.basis == "转让协议" for s in comp)
    totals = cur.totals_by_holder()
    assert totals["ORIGINAL"]["received"] == CF1
    assert totals["乙"]["receivable"] == round(0.4 * CF2, 6)
    assert totals["ORIGINAL"]["receivable"] == round(CF2 - 0.4 * CF2, 6)
    cur.check_conservation()


def test_transfer_after_partial_termination_and_overlap():
    termination = PartialTermination(
        event_id="TERM-1", effective_date=date(2025, 12, 1),
        reduction_amount=200_000.0, received_date=date(2025, 11, 20),
        basis="提前终止")
    t1 = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.5,
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 1),
        basis="转让一")
    t2 = RightsTransfer(
        event_id="T2", to_holder="丙", ratio=0.5,
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 2),
        basis="转让二")
    contract = make_contract(events=(termination, t1, t2))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 12, 15)))
    report = allocate(valuation)
    report.check_conservation()

    totals = report.totals_by_holder()
    # 终止返还款 20 万已由 T1/T2 覆盖前收到？支付日 2025-12-01 时 T1/T2 均已到达
    term_shares = [s for s in report.shares if s.cashflow_kind == "TERMINATION_PRINCIPAL"]
    assert sum(s.amount for s in term_shares) == pytest.approx(200_000.0)
    assert totals["乙"]["received"] == pytest.approx(100_000.0)
    assert totals["丙"]["received"] == pytest.approx(100_000.0)
    # 剩余利息按终止后本金重算并全部转出，原持有人应收为 0
    assert totals["ORIGINAL"]["receivable"] == pytest.approx(0.0)
    assert totals["乙"]["receivable"] + totals["丙"]["receivable"] > 0

    # 交叠转让累计超过原权益：明确拒绝
    t3 = RightsTransfer(
        event_id="T3", to_holder="丁", ratio=0.6,
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 3),
        basis="超额转让")
    bad = make_contract(events=(termination, t1, replace(t3, ratio=0.6)))
    valuation_bad = ValuationBatch("B2").run(bad, make_curve(date(2025, 12, 15)))
    with pytest.raises(ValueError, match="超过原权益"):
        allocate(valuation_bad)


def test_revocation_after_counterparty_paid():
    t1 = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.3,
        effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
        basis="转让协议")
    revocation = TransferRevocation(
        event_id="R1", transfer_event_id="T1",
        received_date=date(2025, 10, 20), basis="撤销通知")
    contract = make_contract(events=(t1, revocation))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 10, 25)))

    # 历史视角（撤销到达前）：转让有效，乙已收 30%
    hist = allocate(valuation, as_of=date(2025, 10, 5), view="HISTORICAL")
    hist_totals = hist.totals_by_holder()
    assert hist_totals["乙"]["received"] == pytest.approx(round(0.3 * CF1, 6))
    assert hist_totals["乙"]["receivable"] == pytest.approx(round(0.3 * CF2, 6))
    hist.check_conservation()

    # 当前视角：已付款项保持原支付记录，撤销差额以补偿记录回补
    cur = allocate(valuation, as_of=date(2025, 10, 25), view="CURRENT")
    comp = [s for s in cur.shares if s.kind == "COMPENSATION"]
    assert {(s.holder, s.amount) for s in comp} == {
        ("乙", round(-0.3 * CF1, 6)), ("ORIGINAL", round(0.3 * CF1, 6))}
    assert all(s.event_id == "R1" for s in comp)
    totals = cur.totals_by_holder()
    assert totals["乙"]["received"] == pytest.approx(round(0.3 * CF1, 6))
    assert totals["乙"]["receivable"] == pytest.approx(0.0)
    assert totals["ORIGINAL"]["receivable"] == pytest.approx(CF2)
    cur.check_conservation()
    # 总额守恒：各方已收+应收+补偿合计等于合约现金流总额
    grand = sum(sum(b.values()) for b in totals.values())
    assert grand == pytest.approx(CF1 + CF2)


def test_fixing_correction_adjustment_allocated_to_holders():
    contract = FloatingRateContract(
        contract_id="FLT-T",
        notional=1_000_000.0,
        start_date=date(2025, 2, 28),
        end_date=date(2026, 2, 28),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.NONE,
        index_name="3M-TEST",
        fixing_lag_business_days=2,
        fixings={date(2025, 2, 26): 0.040, date(2025, 5, 29): 0.042},
        events=(
            FixingCorrection(
                event_id="FIX-1", fixing_date=date(2025, 2, 26),
                old_value=0.040, new_value=0.050,
                received_date=date(2025, 6, 1),
                settlement_date=date(2025, 6, 20),
                basis="定盘更正"),
            RightsTransfer(
                event_id="T1", to_holder="乙", ratio=0.5,
                effective_date=date(2025, 6, 1), received_date=date(2025, 6, 1),
                basis="转让协议"),
        ),
    )
    discount = make_curve(date(2025, 6, 10), df=0.97)
    forecast = DiscountCurve("FWD", date(2025, 6, 10),
                             (date(2026, 2, 28),), (0.96,))
    valuation = ValuationBatch("B").run(contract, discount, forecast)
    report = allocate(valuation)
    report.check_conservation()

    delta = round(1_000_000 * 0.01 * 92 / 360, 6)
    adj = [s for s in report.shares if s.cashflow_kind == "ADJUSTMENT"]
    assert sum(s.amount for s in adj) == pytest.approx(delta)
    assert sum(s.amount for s in adj if s.holder == "乙") == pytest.approx(delta / 2)


def test_save_and_reload_preserves_holder_buckets(tmp_path):
    termination = PartialTermination(
        event_id="TERM-1", effective_date=date(2025, 12, 1),
        reduction_amount=200_000.0, received_date=date(2025, 11, 20),
        basis="提前终止")
    t1 = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 10, 1), received_date=date(2025, 10, 10),
        basis="转让协议")
    contract = make_contract(events=(termination, t1))
    batch = ValuationBatch("B")
    valuation = batch.run(contract, make_curve(date(2025, 12, 15)))
    report = allocate(valuation)

    # 估值批次重开：事件与日程快照完整，重算分配结果一致
    batch_path = tmp_path / "batch.json"
    batch.save_json(batch_path)
    reloaded = ValuationBatch.load_json(batch_path).get("TR-1", "C")
    assert allocate(reloaded).totals_by_holder() == report.totals_by_holder()

    # 对账报告重开：各方已收 / 应收 / 补偿三类桶互不覆盖
    report_path = tmp_path / "report.json"
    report.save_json(report_path)
    restored = AllocationReport.load_json(report_path)
    assert restored == report
    for holder, bucket in restored.totals_by_holder().items():
        assert bucket == report.totals_by_holder()[holder]
        assert set(bucket) == {"received", "receivable", "compensation"}
    restored.check_conservation()
