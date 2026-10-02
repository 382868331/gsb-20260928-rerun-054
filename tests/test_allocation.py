from datetime import date
import math

import pytest

from finval import (
    BusinessDayConvention,
    AssignmentRevocation,
    DayCount,
    DiscountCurve,
    FixedRateContract,
    Frequency,
    FutureInterestAssignment,
    PartialTermination,
    StubType,
    ValuationBatch,
    build_holder_allocation,
    reconcile_allocations,
)


def curve(valuation_date, rate=0.03):
    end = date(2026, 7, 1)
    return DiscountCurve(
        "DISC",
        valuation_date,
        (end,),
        (math.exp(-rate * (end - valuation_date).days / 365),),
    )


def make_contract(events):
    return FixedRateContract(
        contract_id="ASSIGN-1",
        notional=1_000_000.0,
        fixed_rate=0.06,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 7, 1),
        frequency=Frequency.SEMIANNUAL,
        day_count=DayCount.ACT_360,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
        stub=StubType.NONE,
        notional_exchange=True,
        events=events,
        holder_id="A",
    )


def shares_for(valuation, payment_date, component):
    return {
        share.holder: share
        for share in valuation.holder_allocations.shares
        if share.cashflow.payment_date == payment_date
        and share.component == component
    }


def test_assignment_after_payment_partial_termination_and_reassignment_conserve_totals():
    first_transfer = FutureInterestAssignment(
        event_id="ASSIGN-A-B",
        transferor="A",
        transferee="B",
        proportion=0.30,
        effective_date=date(2025, 7, 2),
        received_date=date(2025, 7, 2),
        basis="付息日后转让通知",
    )
    termination = PartialTermination(
        event_id="TERM-200",
        effective_date=date(2025, 8, 1),
        reduction_amount=200_000.0,
        received_date=date(2025, 7, 20),
        basis="部分终止",
    )
    second_transfer = FutureInterestAssignment(
        event_id="ASSIGN-B-C",
        transferor="B",
        transferee="C",
        proportion=0.25,
        effective_date=date(2025, 9, 1),
        received_date=date(2025, 9, 1),
        basis="剩余权益再转让",
    )
    contract = make_contract((first_transfer, termination, second_transfer))
    valuation = ValuationBatch("B").run(contract, curve(date(2025, 10, 1)))
    report = valuation.holder_allocations

    paid_interest = shares_for(valuation, date(2025, 7, 1), "INTEREST")
    assert set(paid_interest) == {"A"}
    assert paid_interest["A"].paid_amount == pytest.approx(1_000_000 * 0.06 * 181 / 360)

    termination_principal = shares_for(valuation, date(2025, 8, 1), "PRINCIPAL")
    assert set(termination_principal) == {"A", "B"}
    assert termination_principal["A"].fraction == 0.70
    assert termination_principal["B"].fraction == 0.30

    final_interest = shares_for(valuation, date(2026, 7, 1), "INTEREST")
    final_principal = shares_for(valuation, date(2026, 7, 1), "PRINCIPAL")
    assert {holder: item.fraction for holder, item in final_interest.items()} == {
        "A": 0.70,
        "B": 0.05,
        "C": 0.25,
    }
    assert {holder: item.fraction for holder, item in final_principal.items()} == {
        "A": 0.70,
        "B": 0.05,
        "C": 0.25,
    }

    economic_total = sum(
        explanation.amount
        for explanation in valuation.explanations
        if explanation.payment_state != "CANCELLED"
    )
    assert sum(share.amount for share in report.shares) == pytest.approx(economic_total)
    for position in report.positions:
        assert position.total_amount == pytest.approx(
            position.paid_amount + position.receivable_amount)


def test_overlapping_assignments_cannot_transfer_more_than_original_interest():
    events = (
        FutureInterestAssignment(
            event_id="ASSIGN-1", transferor="A", transferee="B", proportion=0.70,
            effective_date=date(2025, 2, 1), received_date=date(2025, 2, 1), basis="第一次"),
        FutureInterestAssignment(
            event_id="ASSIGN-2", transferor="A", transferee="C", proportion=0.40,
            effective_date=date(2025, 3, 1), received_date=date(2025, 3, 1), basis="交叠第二次"),
    )
    with pytest.raises(ValueError, match="超过可转让原权益"):
        ValuationBatch("B").run(make_contract(events), curve(date(2025, 3, 2)))


def test_assignee_cannot_reassign_more_than_acquired_interest():
    events = (
        FutureInterestAssignment(
            event_id="A-TO-B", transferor="A", transferee="B", proportion=0.30,
            effective_date=date(2025, 2, 1), received_date=date(2025, 2, 1), basis="第一次"),
        FutureInterestAssignment(
            event_id="B-TO-C", transferor="B", transferee="C", proportion=0.40,
            effective_date=date(2025, 3, 1), received_date=date(2025, 3, 1), basis="超额再转让"),
    )
    with pytest.raises(ValueError, match="仅持有 0.300000"):
        ValuationBatch("B").run(make_contract(events), curve(date(2025, 3, 2)))


def test_late_assignment_notice_keeps_paid_history_and_records_current_compensation():
    assignment = FutureInterestAssignment(
        event_id="LATE-NOTICE",
        transferor="A",
        transferee="B",
        proportion=0.40,
        effective_date=date(2025, 6, 1),
        received_date=date(2025, 7, 5),
        basis="迟到转让通知",
    )
    historical = ValuationBatch("OLD").run(
        make_contract((assignment,)), curve(date(2025, 6, 30)))
    current = ValuationBatch("NEW").run(
        make_contract((assignment,)), curve(date(2025, 7, 10)))

    old_paid = shares_for(historical, date(2025, 7, 1), "INTEREST")
    new_paid = shares_for(current, date(2025, 7, 1), "INTEREST")
    assert set(old_paid) == {"A"}
    assert set(new_paid) == {"A"}

    compensation = current.holder_allocations.compensations[0]
    assert compensation.event_id == "LATE-NOTICE"
    assert (compensation.from_holder, compensation.to_holder) == ("A", "B")
    assert compensation.amount == pytest.approx(
        new_paid["A"].amount * 0.40)
    reconciliation = reconcile_allocations(
        historical.holder_allocations, current.holder_allocations)
    future_changes = reconciliation.for_event("LATE-NOTICE")
    assert future_changes
    assert reconciliation.historical_date == date(2025, 6, 30)
    assert reconciliation.current.compensations[0].reason == "LATE_ASSIGNMENT_NOTICE"


def test_revocation_after_payment_keeps_actual_receipt_and_compensates_transferor():
    assignment = FutureInterestAssignment(
        event_id="REVOKED",
        transferor="A",
        transferee="B",
        proportion=0.40,
        effective_date=date(2025, 6, 1),
        received_date=date(2025, 6, 1),
        basis="已通知转让",
    )
    revocation = AssignmentRevocation(
        event_id="UNDO",
        assignment_id="REVOKED",
        received_date=date(2025, 7, 5),
        basis="转让随后撤销",
    )
    historical = ValuationBatch("OLD").run(
        make_contract((assignment, revocation)), curve(date(2025, 7, 1)))
    current = ValuationBatch("NEW").run(
        make_contract((assignment, revocation)), curve(date(2025, 7, 10)))

    historical_paid = shares_for(historical, date(2025, 7, 1), "INTEREST")
    current_paid = shares_for(current, date(2025, 7, 1), "INTEREST")
    assert set(historical_paid) == {"A", "B"}
    assert historical_paid["B"].fraction == 0.40
    assert set(current_paid) == {"A", "B"}
    compensation = current.holder_allocations.compensations[0]
    assert (compensation.from_holder, compensation.to_holder) == ("B", "A")
    assert compensation.reason == "ASSIGNMENT_REVOKED_AFTER_PAYMENT"
    assert compensation.amount == pytest.approx(historical_paid["B"].amount)


def test_reload_keeps_receivable_paid_and_compensation_records_separate(tmp_path):
    assignment = FutureInterestAssignment(
        event_id="PERSIST-LATE",
        transferor="A",
        transferee="B",
        proportion=0.40,
        effective_date=date(2025, 6, 1),
        received_date=date(2025, 7, 5),
        basis="持久化迟到通知",
    )
    batch = ValuationBatch("PERSIST")
    saved = batch.run(make_contract((assignment,)), curve(date(2025, 7, 10)))
    path = tmp_path / "allocations.json"
    batch.save_json(path)

    reopened = ValuationBatch.load_json(path)
    loaded = reopened.records[0]
    assert loaded.holder_allocations == saved.holder_allocations
    paid_position = loaded.holder_allocations.position("A")
    assert paid_position.paid_amount > 0.0
    assert paid_position.receivable_amount > 0.0
    assert paid_position.compensation_payable > 0.0
    assert loaded.holder_allocations.position("B").compensation_receivable > 0.0


def test_allocation_report_can_be_built_standalone():
    valuation = ValuationBatch("B").run(make_contract(()), curve(date(2025, 1, 1)))
    report = build_holder_allocation(valuation, "CUSTOM-OWNER")
    assert report.original_holder == "CUSTOM-OWNER"
    assert {share.holder for share in report.shares} == {"CUSTOM-OWNER"}
