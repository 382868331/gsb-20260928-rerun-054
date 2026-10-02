"""未来权益转让、持有人份额与双视角对账。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .events import AssignmentRevocation, FutureInterestAssignment

INTEREST = "INTEREST"
PRINCIPAL = "PRINCIPAL"
ADJUSTMENT = "ADJUSTMENT"


@dataclass(frozen=True)
class CashflowKey:
    index: int
    payment_date: date
    event_id: str | None
    cashflow_kind: str


@dataclass(frozen=True)
class HolderShare:
    cashflow: CashflowKey
    holder: str
    component: str
    amount: float
    paid_amount: float
    receivable_amount: float
    present_value: float
    fraction: float
    assignment_ids: tuple[str, ...]
    payment_state: str


@dataclass(frozen=True)
class CompensationRecord:
    event_id: str
    cashflow: CashflowKey
    from_holder: str
    to_holder: str
    component: str
    amount: float
    reason: str
    date: date | None = None


@dataclass(frozen=True)
class HolderPosition:
    holder: str
    total_amount: float
    paid_amount: float
    receivable_amount: float
    present_value: float
    compensation_receivable: float
    compensation_payable: float


@dataclass(frozen=True)
class AllocationReport:
    contract_id: str
    valuation_date: date
    original_holder: str
    shares: tuple[HolderShare, ...]
    compensations: tuple[CompensationRecord, ...]

    @property
    def positions(self) -> tuple[HolderPosition, ...]:
        holders = sorted(
            {share.holder for share in self.shares}
            | {item.from_holder for item in self.compensations}
            | {item.to_holder for item in self.compensations}
        )
        result = []
        for holder in holders:
            shares = [s for s in self.shares if s.holder == holder]
            incoming = [c for c in self.compensations if c.to_holder == holder]
            outgoing = [c for c in self.compensations if c.from_holder == holder]
            result.append(HolderPosition(
                holder=holder,
                total_amount=round(sum(s.amount for s in shares), 6),
                paid_amount=round(sum(s.paid_amount for s in shares), 6),
                receivable_amount=round(sum(s.receivable_amount for s in shares), 6),
                present_value=round(sum(s.present_value for s in shares), 6),
                compensation_receivable=round(sum(c.amount for c in incoming), 6),
                compensation_payable=round(sum(c.amount for c in outgoing), 6),
            ))
        return tuple(result)

    def position(self, holder: str) -> HolderPosition:
        for item in self.positions:
            if item.holder == holder:
                return item
        raise KeyError(f"不存在持有人 {holder}")


@dataclass(frozen=True)
class AllocationChange:
    cashflow: CashflowKey
    holder: str
    component: str
    historical_amount: float
    current_amount: float
    delta: float
    event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reconciliation:
    historical_date: date
    current_date: date
    changes: tuple[AllocationChange, ...]
    historical: AllocationReport
    current: AllocationReport

    def for_event(self, event_id: str) -> tuple[AllocationChange, ...]:
        return tuple(change for change in self.changes if event_id in change.event_ids)


def _events(valuation) -> tuple[object, ...]:
    return tuple(getattr(valuation, "events", ()) or ())


def _assignments(events) -> tuple[FutureInterestAssignment, ...]:
    return tuple(e for e in events if isinstance(e, FutureInterestAssignment))


def _revocations(events) -> Mapping[str, AssignmentRevocation]:
    return {e.assignment_id: e for e in events if isinstance(e, AssignmentRevocation)}


def _active_revoked(revocation, notice_deadline, payment_date) -> bool:
    if revocation.received_date > notice_deadline:
        return False
    if revocation.effective_date is not None and revocation.effective_date > payment_date:
        return False
    return True


def _validate_assignments(
    assignments: tuple[FutureInterestAssignment, ...],
    original_holder: str,
) -> None:
    balances: dict[str, dict[str, float]] = {}
    for assignment in sorted(
        assignments,
        key=lambda a: (a.effective_date, a.received_date, a.event_id),
    ):
        for component in assignment.normalized_scope:
            component_balances = balances.setdefault(component, {original_holder: 1.0})
            available = component_balances.get(assignment.transferor, 0.0)
            if assignment.proportion > available + 1e-10:
                raise ValueError(
                    f"转让 {assignment.event_id} 分出 {assignment.proportion:.6f}，"
                    f"但 {assignment.transferor} 仅持有 {available:.6f}，超过可转让原权益")
            component_balances[assignment.transferor] = available - assignment.proportion
            component_balances[assignment.transferee] = (
                component_balances.get(assignment.transferee, 0.0) + assignment.proportion
            )


def _component_amounts(explanation) -> tuple[tuple[str, float], ...]:
    if explanation.payment_state == "CANCELLED":
        return ()
    amounts = []
    if explanation.interest_amount:
        kind = ADJUSTMENT if explanation.cashflow_kind == "ADJUSTMENT" else INTEREST
        amounts.append((kind, explanation.interest_amount))
    if explanation.notional_amount:
        amounts.append((PRINCIPAL, explanation.notional_amount))
    return tuple(amounts)


def _key(explanation) -> CashflowKey:
    return CashflowKey(
        index=explanation.index,
        payment_date=explanation.payment_date,
        event_id=explanation.event_id,
        cashflow_kind=explanation.cashflow_kind,
    )


def _ownership_at(
    *,
    assignments,
    revocations,
    original_holder,
    component,
    payment_date,
    as_of_date,
    use_actual_payment_knowledge,
):
    holdings = {original_holder: 1.0}
    trail: list[str] = []
    for assignment in sorted(
        assignments,
        key=lambda a: (a.effective_date, a.received_date, a.event_id),
    ):
        notice_deadline = payment_date if use_actual_payment_knowledge else as_of_date
        if assignment.effective_date > payment_date:
            continue
        if assignment.received_date > notice_deadline:
            continue
        if component not in assignment.normalized_scope:
            continue
        revocation = revocations.get(assignment.event_id)
        if revocation is not None and _active_revoked(revocation, notice_deadline, payment_date):
            continue
        available = holdings.get(assignment.transferor, 0.0)
        amount = assignment.proportion if available + 1e-10 >= assignment.proportion else available
        if amount <= 1e-10:
            continue
        holdings[assignment.transferor] = available - amount
        holdings[assignment.transferee] = holdings.get(assignment.transferee, 0.0) + amount
        trail.append(assignment.event_id)
    filtered = {
        holder: fraction
        for holder, fraction in holdings.items()
        if abs(fraction) > 1e-10
    }
    return filtered, tuple(trail)


def build_holder_allocation(valuation, original_holder: str = "ORIGINAL") -> AllocationReport:
    events = _events(valuation)
    assignments = tuple(
        assignment for assignment in _assignments(events)
        if assignment.received_date <= valuation.valuation_date
    )
    revocations = _revocations(events)
    _validate_assignments(assignments, original_holder)
    shares: list[HolderShare] = []
    compensations: list[CompensationRecord] = []

    for explanation in valuation.explanations:
        key = _key(explanation)
        for component, amount in _component_amounts(explanation):
            actual_ownership, actual_trail = _ownership_at(
                assignments=assignments,
                revocations=revocations,
                original_holder=original_holder,
                component=component,
                payment_date=explanation.payment_date,
                as_of_date=valuation.valuation_date,
                use_actual_payment_knowledge=True,
            )
            current_ownership, current_trail = _ownership_at(
                assignments=assignments,
                revocations=revocations,
                original_holder=original_holder,
                component=component,
                payment_date=explanation.payment_date,
                as_of_date=valuation.valuation_date,
                use_actual_payment_knowledge=False,
            )
            if explanation.payment_state == "PAID":
                ownership, trail = actual_ownership, actual_trail
            else:
                ownership, trail = current_ownership, current_trail

            for holder, fraction in sorted(ownership.items()):
                share_amount = round(amount * fraction, 6)
                paid = share_amount if explanation.payment_state == "PAID" else 0.0
                receivable = 0.0 if explanation.payment_state == "PAID" else share_amount
                pv = round((explanation.present_value or 0.0) * fraction, 6)
                shares.append(HolderShare(
                    cashflow=key,
                    holder=holder,
                    component=component,
                    amount=share_amount,
                    paid_amount=paid,
                    receivable_amount=receivable,
                    present_value=pv,
                    fraction=round(fraction, 10),
                    assignment_ids=trail,
                    payment_state=explanation.payment_state,
                ))

            if explanation.payment_state == "PAID":
                for assignment in assignments:
                    if component not in assignment.normalized_scope:
                        continue
                    if assignment.effective_date > explanation.payment_date:
                        continue
                    revocation = revocations.get(assignment.event_id)
                    if (assignment.received_date > explanation.payment_date
                            and assignment.received_date <= valuation.valuation_date
                            and not (
                                revocation is not None
                                and _active_revoked(
                                    revocation,
                                    valuation.valuation_date,
                                    explanation.payment_date))):
                        compensations.append(CompensationRecord(
                            event_id=assignment.event_id,
                            cashflow=key,
                            from_holder=assignment.transferor,
                            to_holder=assignment.transferee,
                            component=component,
                            amount=round(amount * assignment.proportion, 6),
                            reason="LATE_ASSIGNMENT_NOTICE",
                            date=assignment.received_date,
                        ))
                    if (revocation is not None
                            and _active_revoked(
                                revocation,
                                valuation.valuation_date,
                                explanation.payment_date)
                            and assignment.received_date <= explanation.payment_date
                            ):
                        compensations.append(CompensationRecord(
                            event_id=revocation.event_id,
                            cashflow=key,
                            from_holder=assignment.transferee,
                            to_holder=assignment.transferor,
                            component=component,
                            amount=round(amount * assignment.proportion, 6),
                            reason="ASSIGNMENT_REVOKED_AFTER_PAYMENT",
                            date=revocation.received_date,
                        ))

    return AllocationReport(
        contract_id=valuation.contract_id,
        valuation_date=valuation.valuation_date,
        original_holder=original_holder,
        shares=tuple(shares),
        compensations=tuple(compensations),
    )


def reconcile_allocations(
    historical: AllocationReport,
    current: AllocationReport,
) -> Reconciliation:
    def index(report):
        return {
            (s.cashflow.index, s.cashflow.payment_date, s.cashflow.event_id,
             s.cashflow.cashflow_kind, s.holder, s.component): s
            for s in report.shares
        }

    old_values = index(historical)
    new_values = index(current)
    changes = []
    for key in sorted(set(old_values) | set(new_values)):
        old_share = old_values.get(key)
        new_share = new_values.get(key)
        old_value = 0.0 if old_share is None else old_share.amount
        new_value = 0.0 if new_share is None else new_share.amount
        delta = round(new_value - old_value, 6)
        if abs(delta) <= 1e-8:
            continue
        index_no, payment_date, event_id, kind, holder, component = key
        event_ids = tuple(dict.fromkeys(
            tuple(new_share.assignment_ids if new_share is not None else ())
            + tuple(old_share.assignment_ids if old_share is not None else ())
            + ((event_id,) if event_id else ())
        ))
        changes.append(AllocationChange(
            cashflow=CashflowKey(index_no, payment_date, event_id, kind),
            holder=holder,
            component=component,
            historical_amount=round(old_value, 6),
            current_amount=round(new_value, 6),
            delta=delta,
            event_ids=event_ids,
        ))
    return Reconciliation(
        historical_date=historical.valuation_date,
        current_date=current.valuation_date,
        changes=tuple(changes),
        historical=historical,
        current=current,
    )
