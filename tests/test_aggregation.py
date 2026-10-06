import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import replace
from datetime import date

import pytest

from finval import (
    AggregationBatch,
    AggregationElection,
    AggregationExecution,
    AggregationPriceRevision,
    AggregationReport,
    AggregationWithdrawal,
    BusinessDayConvention,
    DayCount,
    DiscountCurve,
    FixedRateContract,
    Frequency,
    RightsTransfer,
    StubType,
    UnitChange,
    ValuationBatch,
    aggregation_settlement,
    compare_processes,
)


def make_contract(events=(), total_units=100.0):
    return FixedRateContract(
        contract_id="AGG-1",
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


def make_curve():
    return DiscountCurve(
        curve_id="C",
        valuation_date=date(2025, 3, 15),
        pillar_dates=(date(2026, 3, 31),),
        discount_factors=(0.98,),
    )


# 乙 20% -> 6.6667 单位（零碎 2/3）；丙 1/30 -> 1.1111 单位（零碎 1/9）
T_YI = RightsTransfer(
    event_id="T-YI", to_holder="乙", ratio=0.2,
    effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
    basis="转让协议")
T_BING = RightsTransfer(
    event_id="T-BING", to_holder="丙", ratio=1 / 30,
    effective_date=date(2025, 9, 1), received_date=date(2025, 9, 1),
    basis="转让协议")
MERGE = UnitChange(
    event_id="U1", effective_date=date(2025, 10, 1), factor=1 / 3,
    received_date=date(2025, 10, 1), basis="3并1")
BATCH_LR = AggregationBatch(
    event_id="B1", unit_change_event_id="U1",
    residual_rule="LARGEST_REMAINDER",
    received_date=date(2025, 10, 2), basis="汇总处置批次")
SELL_YI = AggregationElection(
    event_id="E-YI", batch_id="B1", holder="乙", method="SELL",
    received_date=date(2025, 10, 3), basis="合并出售选择")
SELL_BING = AggregationElection(
    event_id="E-BING", batch_id="B1", holder="丙", method="SELL",
    received_date=date(2025, 10, 3), basis="合并出售选择")

FRAC_YI = 2 / 3
FRAC_BING = 1 / 9
AS_OF = date(2025, 12, 1)


def run(events, as_of=AS_OF):
    contract = make_contract(events=events)
    valuation = ValuationBatch("B").run(contract, make_curve())
    return aggregation_settlement(valuation, as_of=as_of)


def base_events(extra=(), batch=BATCH_LR):
    return (T_YI, T_BING, MERGE, batch, SELL_YI, SELL_BING) + tuple(extra)


def test_sell_proceeds_and_fee_allocated_by_participation():
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.07,
        received_date=date(2025, 10, 10), basis="成交确认")
    report = run(base_events((execution,)))
    report.check_conservation()

    proceeds = {r.holder: r for r in report.records if r.kind == "PROCEEDS"}
    assert proceeds["乙"].exact_cash == pytest.approx(0.1 * FRAC_YI)
    assert proceeds["丙"].exact_cash == pytest.approx(0.1 * FRAC_BING)
    fees = {r.holder: r for r in report.records if r.kind == "FEE"}
    total_units = FRAC_YI + FRAC_BING
    assert fees["乙"].exact_cash == pytest.approx(-0.07 * FRAC_YI / total_units)
    assert fees["丙"].exact_cash == pytest.approx(-0.07 * FRAC_BING / total_units)
    # 费用合计 = 总费用；成交现金合计 = 总价（均含余量分配）
    assert sum(r.cash_amount for r in fees.values()) == pytest.approx(-0.07)
    assert sum(r.cash_amount for r in proceeds.values()) == pytest.approx(
        round(0.1 * total_units, 6))
    # ORIGINAL 未选择，默认保留
    retained = {r.holder for r in report.records if r.kind == "RETAINED"}
    assert "ORIGINAL" in retained


def test_residual_rule_is_reviewable():
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.0,
        received_date=date(2025, 10, 10), basis="成交确认")
    report = run(base_events((execution,)))
    # 乙 余数 0.6667 > 丙 0.1111：最大余数规则把最小货币单位分给乙
    proceeds_allocs = [a for a in report.residual_allocations
                       if a.scope == "PROCEEDS"]
    assert [(a.holder, a.minimal_units) for a in proceeds_allocs] == [("乙", 1)]
    booked = {r.holder: r.cash_amount for r in report.records
              if r.kind == "PROCEEDS"}
    assert booked["乙"] == pytest.approx(0.066667)   # 0.066666 + 余量 1 单位
    assert booked["丙"] == pytest.approx(0.011111)
    # 不足一个最小货币单位的余量单列，不丢弃不补齐
    subs = [s for s in report.sub_unit_residuals if s.scope == "PROCEEDS"]
    exact_total = 0.1 * (FRAC_YI + FRAC_BING)
    assert sum(booked.values()) + sum(s.amount for s in subs) == \
        pytest.approx(exact_total)

    # 换成 HOLDER_ORDER：同一最小货币单位改按持有人序分给丙
    batch_ho = replace(BATCH_LR, residual_rule="HOLDER_ORDER")
    report_ho = run(base_events((execution,), batch=batch_ho))
    allocs_ho = [a for a in report_ho.residual_allocations
                 if a.scope == "PROCEEDS"]
    assert [(a.holder, a.minimal_units) for a in allocs_ho] == [("丙", 1)]
    report_ho.check_conservation()


def test_compensate_batch_uses_uniform_price_without_fee():
    comp_bing = replace(SELL_BING, method="COMPENSATE")
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.07,
        compensation_price_per_unit=0.09,
        received_date=date(2025, 10, 10), basis="成交确认")
    events = (T_YI, T_BING, MERGE, BATCH_LR, SELL_YI, comp_bing, execution)
    report = run(events)
    report.check_conservation()
    comp = [r for r in report.records if r.kind == "COMPENSATION"]
    assert len(comp) == 1
    assert comp[0].holder == "丙"
    assert comp[0].exact_cash == pytest.approx(0.09 * FRAC_BING)
    # 补偿参与者不分摊出售费用
    fees = [r for r in report.records if r.kind == "FEE"]
    assert {r.holder for r in fees} == {"乙"}
    assert fees[0].exact_cash == pytest.approx(-0.07)


def test_withdraw_before_execution_recomputes_participation():
    withdrawal = AggregationWithdrawal(
        event_id="W1", election_event_id="E-YI", portion=0.5,
        received_date=date(2025, 10, 5), basis="成交前撤回")
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.0,
        received_date=date(2025, 10, 10), basis="成交确认")
    report = run(base_events((withdrawal, execution)))
    report.check_conservation()
    proceeds = {r.holder: r for r in report.records if r.kind == "PROCEEDS"}
    # 乙被撤回一半，可参与数量重新计算
    assert proceeds["乙"].units == pytest.approx(FRAC_YI * 0.5)
    assert proceeds["丙"].units == pytest.approx(FRAC_BING)
    retained_yi = [r for r in report.records
                   if r.kind == "RETAINED" and r.holder == "乙"]
    assert sum(r.units for r in retained_yi) == pytest.approx(FRAC_YI * 0.5)
    summary = report.cash_summary()
    assert summary["乙"]["retained_units"] == pytest.approx(FRAC_YI * 0.5)


def test_late_revision_and_late_withdrawal_only_append():
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.0,
        received_date=date(2025, 10, 10), basis="成交确认")
    revision = AggregationPriceRevision(
        event_id="R1", execution_event_id="X1", new_price_per_unit=0.12,
        received_date=date(2025, 10, 20), basis="迟到价格修订")
    late_withdrawal = AggregationWithdrawal(
        event_id="W1", election_event_id="E-YI", portion=0.5,
        received_date=date(2025, 10, 25), basis="成交后撤回")
    events = base_events((execution, revision, late_withdrawal))

    before = run(events, as_of=date(2025, 10, 15))
    after = run(events)
    after.check_conservation()
    # 已完成的分配记录不被改写：成交时点的记录是后续报告的前缀
    assert [r.kind for r in before.records] == \
        [r.kind for r in after.records[:len(before.records)]]
    assert before.records == after.records[:len(before.records)]

    adjustments = [r for r in after.records if r.kind == "PRICE_ADJUSTMENT"]
    assert {r.holder for r in adjustments} == {"乙", "丙"}
    adj_yi = [r for r in adjustments if r.holder == "乙"][0]
    assert adj_yi.exact_cash == pytest.approx(FRAC_YI * 0.02)
    # 成交后撤回：已收现金不当作未发生，只追加追回调整
    clawbacks = [r for r in after.records
                 if r.kind == "WITHDRAWAL_ADJUSTMENT"]
    assert len(clawbacks) == 1
    assert clawbacks[0].holder == "乙"
    assert clawbacks[0].exact_cash == pytest.approx(
        -(FRAC_YI * 0.5) * 0.12)   # 按修订后价格追回
    # 被撤回单位恢复为保留
    summary = after.cash_summary()
    assert summary["乙"]["retained_units"] == pytest.approx(FRAC_YI * 0.5)


def test_compare_processes_stepwise_conservation():
    withdrawal = AggregationWithdrawal(
        event_id="W1", election_event_id="E-YI", portion=0.5,
        received_date=date(2025, 10, 5), basis="撤回")
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.07,
        received_date=date(2025, 10, 10), basis="成交确认")
    events = base_events((withdrawal, execution))
    contract = make_contract(events=events)
    valuation = ValuationBatch("B").run(contract, make_curve())

    comparison = compare_processes(valuation, "B1", as_of=AS_OF)
    generated = FRAC_YI + FRAC_BING  # 乙、丙的零碎单位（ORIGINAL 默认保留）
    # ORIGINAL 未选择，其零碎单位也算产生总量
    original_frac = (1 - 0.2 - 1 / 30) * 100 / 3
    original_frac = original_frac - int(original_frac)
    generated_total = generated + original_frac
    comparison.check_conservation(generated_total)

    # 先撤回后定价：成交时参与单位已扣除被撤回部分
    wf_exec = [s for s in comparison.withdraw_first if s.label == "成交"][0]
    assert wf_exec.executed_units == pytest.approx(FRAC_YI * 0.5 + FRAC_BING)
    assert wf_exec.remaining_units == pytest.approx(0.0)
    # 先定价后撤回：成交时全额参与，撤回只形成事后调整
    pf_exec = [s for s in comparison.price_first if s.label == "成交"][0]
    assert pf_exec.executed_units == pytest.approx(FRAC_YI + FRAC_BING)
    pf_wd = [s for s in comparison.price_first if s.label == "撤回"][0]
    assert pf_wd.executed_units == pytest.approx(FRAC_YI * 0.5 + FRAC_BING)
    assert pf_wd.retained_units > pf_exec.retained_units
    # 两种过程费用守恒：累计费用都等于总费用
    assert wf_exec.cumulative_fee == pytest.approx(0.07)
    assert pf_exec.cumulative_fee == pytest.approx(0.07)


def test_save_load_reopen_and_duplicate_notification(tmp_path):
    execution = AggregationExecution(
        event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.0,
        received_date=date(2025, 10, 10), basis="成交确认")
    revision = AggregationPriceRevision(
        event_id="R1", execution_event_id="X1", new_price_per_unit=0.12,
        received_date=date(2025, 10, 20), basis="迟到价格修订")
    # 同一成交通知重复到达
    duplicate = replace(execution, received_date=date(2025, 10, 11))
    events = base_events((execution, duplicate, revision))

    first = run(events, as_of=date(2025, 10, 15))
    path = tmp_path / "aggregation.json"
    first.save_json(path)
    reopened = AggregationReport.load_json(path)
    assert reopened == first

    # 重开后继续处理同一批次：既有记录不变，迟到修订只追加
    continued = run(events)
    continued.check_conservation()
    assert continued.records[:len(first.records)] == first.records
    assert any(r.kind == "PRICE_ADJUSTMENT" for r in continued.records)
    # 重复通知没有再次分配
    assert "X1" in continued.duplicates_ignored
    proceeds = [r for r in continued.records if r.kind == "PROCEEDS"]
    assert len(proceeds) == 2   # 乙、丙各一笔，未因重复通知翻倍
    assert sum(r.cash_amount for r in proceeds) == pytest.approx(
        round(0.1 * (FRAC_YI + FRAC_BING), 6))
