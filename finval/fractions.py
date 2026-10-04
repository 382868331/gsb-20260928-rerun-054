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
                bucket["available_cash"] += r.cash_amount
            elif r.kind in ("PRICE_ADJUSTMENT", "REVOCATION_ADJUSTMENT"):
                bucket["adjustments"] += r.cash_amount
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

    elections: dict = {}
    for e in events:
        if isinstance(e, FractionElection) and e.received_date <= as_of:
            elections[(e.holder, e.unit_change_event_id)] = e
    revoked: dict = {}
    revocation_basis: dict = {}
    for e in events:
        if isinstance(e, ElectionRevocation) and e.received_date <= as_of:
            revoked[e.election_event_id] = revoked.get(e.election_event_id, 0.0) + e.portion
            revocation_basis[e.election_event_id] = (e.event_id, e.basis)
    for eid, portion in revoked.items():
        if portion > 1 + _EPS:
            raise ValueError(f"选择 {eid} 累计撤回比例超过 1")

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

    for uc in unit_changes:
        factor_after = unit_factor_at(unit_changes, uc.effective_date)
        shares = _equity_shares(transfers, unit_changes, uc.effective_date, total_units)
        price_chain = _price_chain(events, uc.event_id, as_of)
        for holder in sorted(set(shares) | set(removed)):
            equity = shares.get(holder, 0.0) - removed.get(holder, 0.0)
            if equity <= _EPS:
                continue
            units_after = equity * total_units * factor_after
            frac = units_after - math.floor(units_after + _EPS)
            if frac <= _EPS:
                continue
            generated.append(GeneratedFraction(uc.event_id, holder, frac))
            election = elections.get((holder, uc.event_id))
            if election is not None and election.effective_date < uc.effective_date:
                election = None   # 选择截止日早于变化生效日，不适用
            method = election.method if election is not None else "RETAIN"
            portion = revoked.get(election.event_id, 0.0) if election else 0.0
            elected_frac = frac * (1 - portion)
            revoked_frac = frac * portion
            ratio_denom = total_units * factor_after

            if revoked_frac > _EPS:
                rev_id, rev_basis = revocation_basis[election.event_id]
                add(holder, uc.event_id, "RETAINED", revoked_frac,
                    None, None, rev_id, rev_basis)
            if elected_frac <= _EPS:
                continue
            if method == "RETAIN":
                add(holder, uc.event_id, "RETAINED", elected_frac,
                    None, None,
                    election.event_id if election else None,
                    election.basis if election else "默认保留累积")
            elif method == "TRANSFER":
                removed[holder] = removed.get(holder, 0.0) + elected_frac / ratio_denom
                removed[election.to_holder] = (
                    removed.get(election.to_holder, 0.0) - elected_frac / ratio_denom)
                add(holder, uc.event_id, "TRANSFER_OUT", elected_frac,
                    None, None, election.event_id, election.basis)
                add(election.to_holder, uc.event_id, "TRANSFER_IN", elected_frac,
                    None, None, election.event_id, election.basis)
            else:  # CASH
                removed[holder] = removed.get(holder, 0.0) + elected_frac / ratio_denom
                if not price_chain:
                    add(holder, uc.event_id, "PENDING_CASH", elected_frac,
                        None, None, election.event_id, election.basis)
                    continue
                base = price_chain[0]
                add(holder, uc.event_id, "CASH_IN_LIEU", elected_frac,
                    base.price_per_unit, elected_frac * base.price_per_unit,
                    base.event_id, base.basis)
                for prev, nxt in zip(price_chain, price_chain[1:]):
                    add(holder, uc.event_id, "PRICE_ADJUSTMENT", elected_frac,
                        nxt.price_per_unit,
                        elected_frac * (nxt.price_per_unit - prev.price_per_unit),
                        nxt.event_id, nxt.basis)
                if revoked_frac > _EPS:
                    final_price = price_chain[-1].price_per_unit
                    rev_id, rev_basis = revocation_basis[election.event_id]
                    add(holder, uc.event_id, "REVOCATION_ADJUSTMENT", revoked_frac,
                        final_price, -(revoked_frac * final_price),
                        rev_id, rev_basis)

    return FractionReport(
        contract_id=valuation.contract_id,
        as_of=as_of,
        generated=tuple(generated),
        records=tuple(records),
        residuals=tuple(residuals),
    )
