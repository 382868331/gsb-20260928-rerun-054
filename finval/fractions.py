"""零碎权益台账：拆并产生的零碎单位的选择、定价、调整与守恒核对。

生效依据（同日事件的确定顺序）：
- 单位拆并、权益转让、零碎选择、撤回、定价一律按
  (effective_date, received_date, event_id) 排序；同日到达/同日截止视为已生效。
- FractionElection 通过 unit_change_event_id 明确绑定一次单位变化，
  且选择截止日不早于该变化生效日（含同日）才适用；未选择默认 RETAIN。

计算纪律：
- 内部一律使用未舍入的精确单位数与精确金额（exact_cash），
  只有落账的 cash_amount 舍入到最小可分单位（6 位小数）；
  每笔舍入余量单独记录，汇总于 total_residual，可核对总和，
  不并入任何持有人，也不重复分配。
- 现金替代价格可晚于份额变化到达：定价前为 PENDING_CASH（待定现金权益，
  只有单位数没有可用现金），定价后生成 CASH_IN_LIEU（可用现金）。
- 价格更正（CashPriceFixing.corrects_event_id）与选择撤回（ElectionRevocation）
  只产生 PRICE_ADJUSTMENT / REVOCATION_ADJUSTMENT 追踪记录，
  已完成的 CASH_IN_LIEU 分配记录保持原样。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .allocation import (
    ROUND_DECIMALS,
    _active_transfers,
    _active_unit_changes,
    _equity_shares,
    unit_factor_at,
)
from .events import (
    CashPriceFixing,
    ElectionRevocation,
    FractionElection,
    UnitChange,
)
from .valuation import Valuation

_EPS = 1e-9


@dataclass(frozen=True)
class GeneratedFraction:
    """某次单位变化为某持有人产生的零碎单位（精确值，未舍入）。"""

    unit_change_event_id: str
    holder: str
    units: float


@dataclass(frozen=True)
class FractionRecord:
    """零碎权益台账记录：保留 / 待定现金 / 现金替代 / 转移 / 调整。"""

    seq: int
    holder: str
    unit_change_event_id: str
    kind: str                    # RETAINED / PENDING_CASH / CASH_IN_LIEU /
                                 # TRANSFER_OUT / TRANSFER_IN /
                                 # PRICE_ADJUSTMENT / REVOCATION_ADJUSTMENT
    units: float                 # 精确单位数（该次单位变化后的口径）
    price: Optional[float]       # 适用单价；无现金含义时为 None
    cash_amount: Optional[float] # 落账现金（舍入到最小可分单位）
    exact_cash: Optional[float]  # 未舍入精确现金，用于守恒核对
    event_id: Optional[str]
    basis: Optional[str]


@dataclass(frozen=True)
class FractionReport:
    """一次零碎权益结算结果：逐条记录 + 舍入余量 + 守恒核对。"""

    contract_id: str
    as_of: date
    generated: tuple[GeneratedFraction, ...]
    records: tuple[FractionRecord, ...]
    residuals: tuple[float, ...]   # 每笔现金记录的舍入余量（exact - 落账）

    @property
    def total_residual(self) -> float:
        # 不按最小可分单位舍入，否则余量会被静默吞掉
        return round(sum(self.residuals), 12)

    def holder_records(self, holder: str) -> tuple[FractionRecord, ...]:
        return tuple(r for r in self.records if r.holder == holder)

    def cash_summary(self) -> dict:
        """各持有人现金视图：待定单位、可用现金、调整三类分列。"""
        out: dict = {}
        for r in self.records:
            bucket = out.setdefault(r.holder, {
                "pending_units": 0.0, "available_cash": 0.0, "adjustments": 0.0})
            if r.kind == "PENDING_CASH":
                bucket["pending_units"] += r.units
            elif r.kind == "CASH_IN_LIEU":
                bucket["available_cash"] += r.cash_amount or 0.0
            elif r.kind == "REVOCATION_ADJUSTMENT" and r.cash_amount is None:
                bucket["pending_units"] -= r.units
            elif r.kind in ("PRICE_ADJUSTMENT", "REVOCATION_ADJUSTMENT"):
                amount = r.cash_amount or 0.0
                bucket["adjustments"] += amount
                bucket["available_cash"] += amount
        return {h: {k: round(v, ROUND_DECIMALS) for k, v in bucket.items()}
                for h, bucket in out.items()}

    def retained_units(self) -> dict:
        """各持有人保留累积的零碎单位（含转入、含被撤回恢复部分）。"""
        out: dict = {}
        for r in self.records:
            if r.kind in ("RETAINED", "TRANSFER_IN"):
                out[r.holder] = out.get(r.holder, 0.0) + r.units
        return {h: round(u, 12) for h, u in sorted(out.items())}

    def check_conservation(self, tol: float = 1e-6) -> bool:
        """单位与资金双重守恒。

        单位：每次单位变化产生的零碎单位 = 保留 + 现金替代 + 转出；
        转入合计 = 转出合计（跨持有人转移不增不减总量）。
        资金：落账现金合计 + 舍入余量合计 = 精确现金合计。
        """
        disposed: dict = {}
        transferred_out = 0.0
        transferred_in = 0.0
        for r in self.records:
            if r.kind in ("RETAINED", "PENDING_CASH", "CASH_IN_LIEU",
                          "TRANSFER_OUT"):
                key = (r.unit_change_event_id, r.holder)
                disposed[key] = disposed.get(key, 0.0) + r.units
            elif r.kind == "REVOCATION_ADJUSTMENT":
                key = (r.unit_change_event_id, r.holder)
                disposed[key] = disposed.get(key, 0.0) - r.units
            if r.kind == "TRANSFER_OUT":
                transferred_out += r.units
            elif r.kind == "TRANSFER_IN":
                transferred_in += r.units
        for g in self.generated:
            key = (g.unit_change_event_id, g.holder)
            if abs(disposed.get(key, 0.0) - g.units) > tol:
                raise AssertionError(
                    f"单位不守恒: {key} 产生 {g.units}，处置 {disposed.get(key, 0.0)}")
        if abs(transferred_in - transferred_out) > tol:
            raise AssertionError(
                f"转移不守恒: 转出 {transferred_out}，转入 {transferred_in}")
        booked = sum(r.cash_amount for r in self.records
                     if r.cash_amount is not None)
        exact = sum(r.exact_cash for r in self.records
                    if r.exact_cash is not None)
        if abs(booked + sum(self.residuals) - exact) > tol:
            raise AssertionError(
                f"资金不守恒: 落账 {booked} + 余量 {sum(self.residuals)} != 精确 {exact}")
        return True


def _price_chain(events, uc_event_id: str, as_of: date) -> tuple[CashPriceFixing, ...]:
    """某次单位变化的定价链：基准价 + 依次更正，按到达先后链接。"""
    fixes = [e for e in events
             if isinstance(e, CashPriceFixing)
             and e.unit_change_event_id == uc_event_id
             and e.received_date <= as_of]
    known = {f.event_id for f in fixes}
    for f in fixes:
        if f.corrects_event_id is not None and f.corrects_event_id not in known:
            raise ValueError(f"定价更正 {f.event_id} 指向未知定价事件")
    bases = [f for f in fixes if f.corrects_event_id is None]
    if len(bases) > 1:
        raise ValueError(f"单位变化 {uc_event_id} 存在多个基准定价")
    chain = []
    current = bases[0] if bases else None
    while current is not None:
        chain.append(current)
        followers = [f for f in fixes if f.corrects_event_id == current.event_id]
        if len(followers) > 1:
            raise ValueError(f"定价 {current.event_id} 存在多个更正")
        current = followers[0] if followers else None
    return tuple(chain)


def fraction_settlement(valuation: Valuation, as_of: Optional[date] = None) -> FractionReport:
    """按 as_of 已到达的事件结算零碎权益台账。

    同一知识时点重跑结果一致；价格、撤回等迟到事件只追加记录，
    不改写既有记录的含义。
    """
    if as_of is None:
        as_of = valuation.valuation_date
    events = valuation.events
    transfers = _active_transfers(valuation, as_of)
    unit_changes = _active_unit_changes(valuation, as_of)
    total_units = getattr(valuation, "total_units", 1.0)

    generated: list[GeneratedFraction] = []
    records: list[FractionRecord] = []
    residuals: list[float] = []
    removed: dict = {}   # holder -> 已折现/转出的原合约权益比例
    seq = 0

    def add(holder, uc_id, kind, units, price, exact_cash, event_id, basis):
        nonlocal seq
        cash = None
        if exact_cash is not None:
            cash = round(exact_cash, ROUND_DECIMALS)
            residuals.append(round(exact_cash - cash, 12))
        records.append(FractionRecord(
            seq=seq, holder=holder, unit_change_event_id=uc_id, kind=kind,
            units=units, price=price, cash_amount=cash, exact_cash=exact_cash,
            event_id=event_id, basis=basis))
        seq += 1

    class Position:
        def __init__(self, uc, holder, units):
            self.uc = uc
            self.holder = holder
            self.total_units = units
            self.election = None
            self.pending_units = 0.0
            self.cash_units = 0.0
            self.price = None
            self.revoked_portion = 0.0
            self.settled = False
            self.pending_record = False

        def active_units(self):
            return self.total_units * (1.0 - self.revoked_portion)

    positions: dict = {}
    elections_by_key: dict = {}
    revoked_by_election: dict = {}
    revocation_basis: dict = {}
    chains = {uc.event_id: _price_chain(events, uc.event_id, as_of)
              for uc in unit_changes}

    def create_position(uc, holder, units):
        key = (uc.event_id, holder)
        pos = Position(uc, holder, units)
        positions[key] = pos
        generated.append(GeneratedFraction(uc.event_id, holder, units))
        election = elections_by_key.get(key)
        if election is not None and election.effective_date >= uc.effective_date:
            apply_election(pos, election)

    def remove_equity(holder, units, factor):
        if units:
            removed[holder] = removed.get(holder, 0.0) + units / (total_units * factor)

    def restore_equity(holder, units, factor):
        if units:
            removed[holder] = removed.get(holder, 0.0) - units / (total_units * factor)

    def known_price(uc_id, day):
        chain = chains.get(uc_id, ())
        known = [f for f in chain if f.received_date <= day]
        return known[-1] if known else None

    def apply_election(pos, election):
        if pos.election is not None or pos.settled:
            return
        pos.election = election
        pos.revoked_portion = revoked_by_election.get(election.event_id, 0.0)
        active = pos.active_units()
        factor = unit_factor_at(unit_changes, pos.uc.effective_date)
        if election.method == "RETAIN":
            if active > _EPS:
                add(pos.holder, pos.uc.event_id, "RETAINED", active,
                    None, None, election.event_id, election.basis)
            pos.settled = True
        elif election.method == "TRANSFER":
            if active > _EPS:
                remove_equity(pos.holder, active, factor)
                removed[election.to_holder] = (
                    removed.get(election.to_holder, 0.0) - active / (total_units * factor))
                add(pos.holder, pos.uc.event_id, "TRANSFER_OUT", active,
                    None, None, election.event_id, election.basis)
                add(election.to_holder, pos.uc.event_id, "TRANSFER_IN", active,
                    None, None, election.event_id, election.basis)
            pos.settled = True
        else:
            revoked_units = pos.total_units * pos.revoked_portion
            if revoked_units > _EPS:
                add(pos.holder, pos.uc.event_id, "RETAINED", revoked_units,
                    None, None,
                    revocation_basis[election.event_id][0],
                    revocation_basis[election.event_id][1])
            if active <= _EPS:
                pos.settled = True
                return
            fixing = known_price(pos.uc.event_id, election.received_date)
            if fixing is None:
                pos.pending_units = active
                pos.pending_record = True
            else:
                pos.cash_units = active
                pos.price = fixing.price_per_unit
                remove_equity(pos.holder, active, factor)
                add(pos.holder, pos.uc.event_id, "CASH_IN_LIEU", active,
                    fixing.price_per_unit, active * fixing.price_per_unit,
                    fixing.event_id, fixing.basis)

    timeline = [
        e for e in events
        if e.received_date <= as_of
        and isinstance(e, (FractionElection, CashPriceFixing, ElectionRevocation))
    ]
    timeline.extend(unit_changes)
    timeline.sort(key=lambda e: (e.received_date, e.event_id))

    # 选择、定价、撤回严格按到达顺序逐笔追加；单位变化在其通知到达时建账。
    for event in timeline:
        if isinstance(event, FractionElection):
            key = (event.unit_change_event_id, event.holder)
            elections_by_key[key] = event
            pos = positions.get(key)
            if pos is not None and event.effective_date >= pos.uc.effective_date:
                apply_election(pos, event)
        elif isinstance(event, ElectionRevocation):
            revoked_by_election[event.election_event_id] = (
                revoked_by_election.get(event.election_event_id, 0.0) + event.portion)
            total_portion = revoked_by_election[event.election_event_id]
            if total_portion > 1 + _EPS:
                raise ValueError(f"选择 {event.election_event_id} 累计撤回比例超过 1")
            revocation_basis[event.election_event_id] = (event.event_id, event.basis)
            for pos in positions.values():
                if pos.election is None or pos.election.event_id != event.election_event_id:
                    continue
                old_portion = pos.revoked_portion
                new_portion = min(total_portion, 1.0)
                delta_portion = max(0.0, new_portion - old_portion)
                revoked_units = pos.total_units * delta_portion
                if revoked_units <= _EPS:
                    continue
                pos.revoked_portion = new_portion
                factor = unit_factor_at(unit_changes, pos.uc.effective_date)
                add(pos.holder, pos.uc.event_id, "RETAINED", revoked_units,
                    None, None, event.event_id, event.basis)
                if pos.pending_units > _EPS:
                    delta = min(revoked_units, pos.pending_units)
                    pos.pending_units -= delta
                if pos.cash_units > _EPS:
                    delta = min(revoked_units, pos.cash_units)
                    pos.cash_units -= delta
                    restore_equity(pos.holder, delta, factor)
                    add(pos.holder, pos.uc.event_id, "REVOCATION_ADJUSTMENT", delta,
                        pos.price, -(delta * pos.price),
                        event.event_id, event.basis)
                if pos.pending_units <= _EPS and pos.cash_units <= _EPS:
                    pos.settled = True
        elif isinstance(event, CashPriceFixing):
            for pos in positions.values():
                if pos.uc.event_id != event.unit_change_event_id or pos.election is None:
                    continue
                factor = unit_factor_at(unit_changes, pos.uc.effective_date)
                if event.corrects_event_id is None:
                    if pos.pending_units > _EPS:
                        units = pos.pending_units
                        pos.pending_units = 0.0
                        pos.cash_units = units
                        pos.price = event.price_per_unit
                        remove_equity(pos.holder, units, factor)
                        add(pos.holder, pos.uc.event_id, "CASH_IN_LIEU", units,
                            event.price_per_unit, units * event.price_per_unit,
                            event.event_id, event.basis)
                elif pos.cash_units > _EPS and pos.price is not None:
                    chain = chains[pos.uc.event_id]
                    previous = next(
                        (f for f in chain if f.event_id == event.corrects_event_id), None)
                    if previous is not None and abs(pos.price - previous.price_per_unit) <= _EPS:
                        add(pos.holder, pos.uc.event_id, "PRICE_ADJUSTMENT", pos.cash_units,
                            event.price_per_unit,
                            pos.cash_units * (event.price_per_unit - previous.price_per_unit),
                            event.event_id, event.basis)
                        pos.price = event.price_per_unit
        elif isinstance(event, UnitChange):
            factor_after = unit_factor_at(unit_changes, event.effective_date)
            shares = _equity_shares(transfers, unit_changes, event.effective_date, total_units)
            for holder in sorted(set(shares) | set(removed)):
                equity = shares.get(holder, 0.0) - removed.get(holder, 0.0)
                if equity <= _EPS:
                    continue
                units_after = equity * total_units * factor_after
                frac = units_after - math.floor(units_after + _EPS)
                if frac > _EPS:
                    create_position(event, holder, frac)

    # 截止 as_of 没有有效选择的零碎单位，仍按默认保留累积建账。
    for pos in positions.values():
        if pos.pending_units > _EPS and pos.pending_record:
            add(pos.holder, pos.uc.event_id, "PENDING_CASH", pos.pending_units,
                None, None, pos.election.event_id, pos.election.basis)
        elif pos.election is None and not pos.settled:
            add(pos.holder, pos.uc.event_id, "RETAINED",
                pos.total_units, None, None, None, "默认保留累积")

    return FractionReport(
        contract_id=valuation.contract_id,
        as_of=as_of,
        generated=tuple(generated),
        records=tuple(records),
        residuals=tuple(residuals),
    )
