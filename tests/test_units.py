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
    FixingCorrection,
    FloatingRateContract,
    Frequency,
    PartialTermination,
    RightsTransfer,
    StubType,
    TransferRevocation,
    UnitMerge,
    UnitSplit,
    ValuationBatch,
    allocate,
)


def make_contract(events=()):
    # 2025-03-15 -> 2026-03-31 半年付息：支付日 2025-09-30 / 2026-03-31
    return FixedRateContract(
        contract_id="U-1",
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


def test_split_changes_units_not_amounts():
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.4,
        effective_date=date(2025, 8, 1), received_date=date(2025, 8, 1),
        basis="转让协议")
    split = UnitSplit(
        event_id="S1", effective_date=date(2025, 10, 1), factor=2.0,
        received_date=date(2025, 9, 15), basis="拆分公告")
    baseline_events = (transfer,)
    events = (transfer, split)

    batch = ValuationBatch("B")
    base = allocate(batch.run(make_contract(baseline_events), make_curve(date(2025, 12, 1))),
                    initial_units=1000)
    report = allocate(batch.run(make_contract(events), make_curve(date(2025, 12, 1))),
                      initial_units=1000)
    report.check_conservation()

    # 单位变多不能凭空增加应收金额：各方已收/应收与无拆分时完全一致
    assert report.totals_by_holder() == base.totals_by_holder()

    # 持仓：权益比例不变，单位数量随拆分翻倍
    pos = {p.holder: p for p in report.positions}
    assert pos["乙"].equity_ratio == pytest.approx(0.4)
    assert pos["乙"].units == pytest.approx(800.0)        # 0.4 × 2000
    assert pos["ORIGINAL"].units == pytest.approx(1200.0)

    # 哪些现金流采用变化前后的解释：支付日落在拆分前/后
    totals = {t.cashflow_index: t for t in report.cashflow_totals}
    assert totals[1].total_units == pytest.approx(1000.0)  # 2025-09-30 拆分前
    assert totals[2].total_units == pytest.approx(2000.0)  # 2026-03-31 拆分后
    share_units = [s for s in report.shares if s.holder == "乙" and s.kind == "SCHEDULED_SHARE"]
    by_index = {s.cashflow_index: s for s in share_units}
    assert by_index[1].units == pytest.approx(400.0)       # 拆分前口径
    assert by_index[2].units == pytest.approx(800.0)       # 拆分后口径


def test_late_split_notice_reinterprets_fixed_unit_transfer():
    # 固定单位转让在支付日前到达；拆分生效早于支付日但通知迟到
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", units=400.0,
        ratio=None, effective_date=date(2025, 9, 20),
        received_date=date(2025, 9, 20), basis="固定单位转让")
    split = UnitSplit(
        event_id="S1", effective_date=date(2025, 9, 15), factor=2.0,
        received_date=date(2025, 10, 10), basis="迟到拆分通知")
    contract = make_contract((transfer, split))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 10, 15)))

    # 历史视角（拆分通知到达前）：400/1000 = 40%
    hist = allocate(valuation, as_of=date(2025, 10, 5),
                    view="HISTORICAL", initial_units=1000)
    hist_totals = hist.totals_by_holder()
    assert hist_totals["乙"]["received"] == pytest.approx(round(0.4 * CF1, 6))
    assert hist_totals["乙"]["receivable"] == pytest.approx(round(0.4 * CF2, 6))
    hist.check_conservation()

    # 当前视角：拆分后 400/2000 = 20%，已付款项保持原支付记录，差额以补偿说明
    cur = allocate(valuation, as_of=date(2025, 10, 15),
                   view="CURRENT", initial_units=1000)
    comp_rows = [s for s in cur.shares if s.kind == "COMPENSATION"]
    comp = {s.holder: s.amount for s in comp_rows}
    assert comp["乙"] == pytest.approx(-0.2 * CF1)
    assert comp["ORIGINAL"] == pytest.approx(0.2 * CF1)
    assert all(s.event_id == "S1" for s in comp_rows)
    totals = cur.totals_by_holder()
    assert totals["乙"]["received"] == pytest.approx(round(0.4 * CF1, 6))
    assert totals["乙"]["receivable"] == pytest.approx(round(0.2 * CF2, 6))
    cur.check_conservation()


def test_fixed_units_vs_fixed_ratio_survive_merge():
    split = UnitSplit(
        event_id="S1", effective_date=date(2025, 8, 1), factor=2.0,
        received_date=date(2025, 8, 1), basis="拆分")
    t_units = RightsTransfer(
        event_id="T-U", to_holder="乙", units=200.0, ratio=None,
        effective_date=date(2025, 8, 10), received_date=date(2025, 8, 10),
        basis="固定单位转让")
    t_ratio = RightsTransfer(
        event_id="T-R", to_holder="丙", ratio=0.05,
        effective_date=date(2025, 8, 10), received_date=date(2025, 8, 10),
        basis="固定比例转让")
    merge = UnitMerge(
        event_id="M1", effective_date=date(2025, 11, 1), factor=2.0,
        received_date=date(2025, 10, 15), basis="合并")
    contract = make_contract((split, t_units, t_ratio, merge))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 12, 1)))
    report = allocate(valuation, initial_units=1000)
    report.check_conservation()

    # 两种约定方式分别记录、可追查
    assert t_units.spec_kind == "UNITS"
    assert t_ratio.spec_kind == "RATIO"

    # 合并后持仓：200 单位（拆分时口径）-> 100 新单位，不是 200 新单位；
    # 固定比例 5% -> 50 新单位；两者不被混为一谈
    pos = {p.holder: p for p in report.positions}
    assert pos["乙"].equity_ratio == pytest.approx(0.10)
    assert pos["乙"].units == pytest.approx(100.0)
    assert pos["丙"].equity_ratio == pytest.approx(0.05)
    assert pos["丙"].units == pytest.approx(50.0)
    assert pos["ORIGINAL"].units == pytest.approx(850.0)

    # 经济归属按各自比例：乙 10%、丙 5%
    totals = report.totals_by_holder()
    assert totals["乙"]["receivable"] == pytest.approx(round(0.10 * CF2, 6))
    assert totals["丙"]["receivable"] == pytest.approx(round(0.05 * CF2, 6))


def test_rounding_residual_is_explicit_not_hidden():
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=1 / 3,
        effective_date=date(2025, 8, 1), received_date=date(2025, 8, 1),
        basis="三分之一转让")
    contract = make_contract((transfer,))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 12, 1)))
    report = allocate(valuation, initial_units=300, rounding_unit=0.01)
    report.check_conservation()

    # 每份都是最小可分单位的整数倍：余量没有藏进任何持有人
    for share in report.shares:
        assert share.amount / 0.01 == pytest.approx(round(share.amount / 0.01), abs=1e-6)

    # 未分配余量逐笔显式记录，并与总额对账
    view = {row["cashflow_index"]: row for row in report.contract_view()}
    for index, amount in ((1, CF1), (2, CF2)):
        row = view[index]
        assert row["rounding_unit"] == pytest.approx(0.01)
        assert abs(row["residual"]) < 0.01
        assert row["allocated"] + row["residual"] == pytest.approx(amount)
    residuals = {r.cashflow_index: r for r in report.residuals}
    assert residuals[1].residual == pytest.approx(view[1]["residual"])
    assert residuals[2].residual == pytest.approx(view[2]["residual"])


def make_floating_contract(events):
    return FloatingRateContract(
        contract_id="FLT-U",
        notional=1_000_000.0,
        start_date=date(2025, 2, 28),
        end_date=date(2026, 2, 28),
        frequency=Frequency.QUARTERLY,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.NONE,
        index_name="3M-TEST",
        fixing_lag_business_days=2,
        fixings={date(2025, 2, 26): 0.040, date(2025, 5, 29): 0.042,
                 date(2025, 8, 28): 0.041, date(2025, 11, 27): 0.043},
        events=tuple(events),
    )


def combo_events():
    return (
        RightsTransfer(
            event_id="T1", to_holder="乙", ratio=0.5,
            effective_date=date(2025, 6, 1), received_date=date(2025, 6, 1),
            basis="转让协议"),
        UnitSplit(
            event_id="S1", effective_date=date(2025, 7, 1), factor=4.0,
            received_date=date(2025, 6, 25), basis="拆分公告"),
        FixingCorrection(
            event_id="FIX-1", fixing_date=date(2025, 2, 26),
            old_value=0.040, new_value=0.050,
            received_date=date(2025, 6, 1),
            settlement_date=date(2025, 6, 20),
            basis="定盘更正"),
        PartialTermination(
            event_id="TERM-1", effective_date=date(2025, 11, 30),
            reduction_amount=200_000.0, received_date=date(2025, 11, 25),
            basis="提前终止"),
    )


def run_combo(events, valuation_date=date(2025, 12, 15)):
    contract = make_floating_contract(events)
    discount = make_curve(valuation_date, df=0.97)
    forecast = DiscountCurve("FWD", valuation_date,
                             (date(2026, 2, 28),), (0.96,))
    return ValuationBatch("B").run(contract, discount, forecast)


def test_combo_transfer_split_correction_termination():
    valuation = run_combo(combo_events())
    report = allocate(valuation, initial_units=1000, rounding_unit=0.01)
    report.check_conservation()

    # 持仓：乙一半权益，拆分后 0.5 × 4000 = 2000 单位
    pos = {p.holder: p for p in report.positions}
    assert pos["乙"].equity_ratio == pytest.approx(0.5)
    assert pos["乙"].units == pytest.approx(2000.0)

    # 终止返还款与更正调整款都按各自有效范围归属（各 50%）
    term = [s for s in report.shares if s.cashflow_kind == "TERMINATION_PRINCIPAL"]
    assert sum(s.amount for s in term) == pytest.approx(200_000.0, abs=0.02)
    assert sum(s.amount for s in term if s.holder == "乙") == pytest.approx(100_000.0, abs=0.02)
    adj = [s for s in report.shares if s.cashflow_kind == "ADJUSTMENT"]
    delta = round(1_000_000 * 0.01 * 92 / 360, 6)
    assert sum(s.amount for s in adj) == pytest.approx(delta, abs=0.02)
    assert sum(s.amount for s in adj if s.holder == "乙") == pytest.approx(delta / 2, abs=0.02)

    # 余量显式且小于最小可分单位
    for r in report.residuals:
        assert abs(r.residual) <= 0.01 + 1e-9


def test_revoke_not_yet_effective_transfer_only_affects_future():
    transfer = RightsTransfer(
        event_id="T1", to_holder="乙", ratio=0.3,
        effective_date=date(2026, 1, 1), received_date=date(2025, 10, 1),
        basis="未来生效转让")
    revocation = TransferRevocation(
        event_id="R1", transfer_event_id="T1",
        received_date=date(2025, 11, 1), basis="生效前撤销")
    contract = make_contract((transfer, revocation))
    valuation = ValuationBatch("B").run(contract, make_curve(date(2025, 12, 15)))
    report = allocate(valuation)
    report.check_conservation()

    # 未生效安排被撤销：如同从未存在，不产生任何补偿
    assert report.holders == ("ORIGINAL",)
    assert not [s for s in report.shares if s.kind == "COMPENSATION"]
    totals = report.totals_by_holder()
    assert totals["ORIGINAL"]["received"] == pytest.approx(CF1)
    assert totals["ORIGINAL"]["receivable"] == pytest.approx(CF2)


def test_notification_order_does_not_change_conclusion(tmp_path):
    events = combo_events()
    shuffled = (events[2], events[0], events[3], events[1])
    report_a = allocate(run_combo(events), initial_units=1000, rounding_unit=0.01)
    report_b = allocate(run_combo(shuffled), initial_units=1000, rounding_unit=0.01)

    # 同一知识时点、不同接收顺序：权益与应收解释一致
    assert report_a.shares == report_b.shares
    assert report_a.positions == report_b.positions
    assert report_a.totals_by_holder() == report_b.totals_by_holder()
    assert report_a.residuals == report_b.residuals

    # 保存重开后结论一致
    contract = make_floating_contract(events)
    discount = make_curve(date(2025, 12, 15), df=0.97)
    forecast = DiscountCurve("FWD", date(2025, 12, 15),
                             (date(2026, 2, 28),), (0.96,))
    batch = ValuationBatch("B")
    batch.run(contract, discount, forecast)
    path = tmp_path / "batch.json"
    batch.save_json(path)
    reloaded = ValuationBatch.load_json(path).get("FLT-U", "C")
    reopened = allocate(reloaded, initial_units=1000, rounding_unit=0.01)
    assert reopened.totals_by_holder() == report_a.totals_by_holder()
    assert reopened.positions == report_a.positions

    report_path = tmp_path / "report.json"
    report_a.save_json(report_path)
    restored = AllocationReport.load_json(report_path)
    assert restored == report_a
    restored.check_conservation()
