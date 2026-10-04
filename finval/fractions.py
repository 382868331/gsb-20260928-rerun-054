"""零碎权益选择台账：保留 / 现金替代 / 转让他人，含定价、撤回与余量对账。

生效依据（同一日的处理顺序，先拆并、再转移、恢复记录最后）：
- 单位拆并（UnitChange）按生效日生效，产生零碎单位；持有人的选择
  （FractionElection）须在该次拆并的 election_deadline（含）前到达，
  逾期或未提交一律按 KEEP（保留可累积零碎单位）处理；
- 份额转移（RightsTransfer）同日生效时按拆并后的单位口径折算；
- 撤回（ElectionRevocation）若到达时原选择已执行（现金已定价或零碎
  单位已转出），原记录保持不变，另以 ADJUSTMENT 调整记录回补，
  回补单位自撤回到达日起参与后续拆并。

内部一律使用精确数量（不舍入）持续计算权益；只有现金金额按最小
可分单位（6 位小数）舍入，舍入余量按拆并批次单列可核对，
不并入任何持有人，也不重复分配。
"""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .allocation import (
    ORIGINAL_HOLDER,
    ROUND_DECIMALS,
    _active_transfers,
    _active_unit_changes,
    unit_factor_at,
)
from .events import (
    CashPriceFixing,
    ElectionRevocation,
    FractionElection,
    UnitChange,
)
from .valuation import Valuation

EPS = 1e-9


@dataclass(frozen=True)
class FractionEntry:
    """一条零碎权益台账记录（数量为精确值，非显示舍入值）。"""

    holder: str
    unit_change_event_id: str
    kind: str               # UNITS_KEPT / UNITS_TRANSFERRED / PENDING_CASH / CASH / ADJUSTMENT
    units: float            # 精确零碎单位数（ADJUSTMENT 现金冲回时为回补单位数）
    cash: Optional[float]   # 已定价现金（ADJUSTMENT 可为负），未定价为 None
    price: Optional[float]
    state: str              # PENDING / PRICED / ADJUSTMENT
    effective_date: date
    event_id: Optional[str] = None
    basis: Optional[str] = None
    to_holder: Optional[str] = None


@dataclass(frozen=True)
class HolderUnits:
    """某持有人 as_of 时点的精确单位持仓（显示舍入不得回用于计算）。"""

    holder: str
    units: float


def _known_events(valuation: Valuation, as_of: date):
    events = [e for e in valuation.events
              if getattr(e, "received_date", None) is not None and e.received_date <= as_of]
    unit_changes = {e.event_id: e for e in events if isinstance(e, UnitChange)}
    elections = [e for e in events if isinstance(e, FractionElection)]
    revocations = [e for e in events if isinstance(e, ElectionRevocation)]
    prices = [e for e in events if isinstance(e, CashPriceFixing)]
    all_unit_change_ids = {e.event_id for e in valuation.events if isinstance(e, UnitChange)}
    all_election_ids = {e.event_id for e in valuation.events if isinstance(e, FractionElection)}
    for election in elections:
        if election.unit_change_event_id not in all_unit_change_ids:
            raise ValueError(
                f"选择 {election.event_id} 引用了不存在的拆并事件 "
                f"{election.unit_change_event_id}")
    for price in prices:
        if price.unit_change_event_id not in all_unit_change_ids:
            raise ValueError(
                f"定价 {price.event_id} 引用了不存在的拆并事件 "
                f"{price.unit_change_event_id}")
    for revocation in revocations:
        if revocation.election_event_id not in all_election_ids:
            raise ValueError(
                f"撤回 {revocation.event_id} 引用了不存在的选择 "
                f"{revocation.election_event_id}")
    return unit_changes, elections, revocations, prices


def fraction_ledger(valuation: Valuation, as_of: Optional[date] = None) -> "FractionLedger":
    """重放 as_of 前已到达的事件，生成零碎权益台账。

    数量守恒：初始单位 × 累计拆并系数 = 期末持仓合计 + 现金替代单位 - 回补单位；
    现金守恒：已定价现金（含调整）+ 舍入余量 = 精确现金（单位 × 价格）。
    """
    if as_of is None:
        as_of = valuation.valuation_date
    total_units = getattr(valuation, "total_units", 1.0)
    unit_changes, elections, revocations, prices = _known_events(valuation, as_of)
    transfers = _active_transfers(valuation, as_of)
    all_unit_changes = _active_unit_changes(valuation, as_of)

    elections_by_key: dict = {}
    for election in elections:
        uc = unit_changes.get(election.unit_change_event_id)
        if uc is None or election.received_date > uc.election_deadline:
            continue    # 逾期选择不生效，按默认 KEEP 处理
        key = (election.holder, election.unit_change_event_id)
        previous = elections_by_key.get(key)
        if previous is None or election.received_date >= previous.received_date:
            elections_by_key[key] = election

    revocations_by_election: dict = {}
    for revocation in revocations:
        revocations_by_election.setdefault(revocation.election_event_id, []).append(revocation)

    prices_by_uc: dict = {}
    for price in prices:
        prices_by_uc.setdefault(price.unit_change_event_id, []).append(price)
    for uc_prices in prices_by_uc.values():
        uc_prices.sort(key=lambda p: (p.received_date, p.event_id))

    # 时间线：同一日先拆并(0)、再转移(1)、回补记录最后(2)
    timeline: list = []
    seq = 0
    for uc in sorted(unit_changes.values(),
                     key=lambda c: (c.effective_date, c.received_date, c.event_id)):
        heapq.heappush(timeline, (uc.effective_date, 0, seq, ("UNIT_CHANGE", uc)))
        seq += 1
    for transfer in transfers:
        heapq.heappush(timeline, (transfer.effective_date, 1, seq, ("TRANSFER", transfer)))
        seq += 1

    positions: dict = {ORIGINAL_HOLDER: float(total_units)}
    entries: list[FractionEntry] = []
    residuals: dict = {}
    exact_cash_total = 0.0
    recorded_cash_total = 0.0
    cashed_units_total = 0.0
    restored_units_total = 0.0

    def add_residual(uc_id: str, exact: float, recorded: float) -> None:
        residuals[uc_id] = round(residuals.get(uc_id, 0.0) + (exact - recorded),
                                 ROUND_DECIMALS + 6)

    while timeline:
        day, _order, _seq, payload = heapq.heappop(timeline)
        kind = payload[0]
        if kind == "UNIT_CHANGE":
            uc = payload[1]
            uc_prices = prices_by_uc.get(uc.event_id, [])
            first_price = uc_prices[0] if uc_prices else None
            for holder in sorted(positions):
                new_units = positions[holder] * uc.factor
                base = math.floor(new_units + EPS)
                frac = new_units - base
                if frac < EPS:
                    positions[holder] = new_units
                    continue
                election = elections_by_key.get((holder, uc.event_id))
                choice = election.choice if election is not None else "KEEP"
                revs = (revocations_by_election.get(election.event_id, [])
                        if election is not None else [])
                if choice == "KEEP":
                    positions[holder] = new_units
                    entries.append(FractionEntry(
                        holder, uc.event_id, "UNITS_KEPT", frac, None, None,
                        "PRICED", uc.effective_date,
                        election.event_id if election else None,
                        election.basis if election else "默认保留零碎单位"))
                    continue
                if choice == "CASH":
                    price_date = first_price.received_date if first_price else None
                    normal_rev = sum(
                        r.fraction for r in revs
                        if price_date is None or r.received_date <= price_date)
                    adj_revs = [r for r in revs
                                if price_date is not None and r.received_date > price_date]
                else:   # TRANSFER
                    normal_rev = sum(
                        r.fraction for r in revs if r.received_date <= uc.effective_date)
                    adj_revs = [r for r in revs if r.received_date > uc.effective_date]
                adj_rev = sum(r.fraction for r in adj_revs)
                if normal_rev + adj_rev > 1 + EPS:
                    raise ValueError(
                        f"选择 {election.event_id} 累计撤回比例超过 1")
                normal_rev = min(normal_rev, 1.0)
                kept = frac * normal_rev
                if choice == "CASH":
                    cash_units = frac * (1.0 - normal_rev)
                    positions[holder] = base + kept
                    cashed_units_total += cash_units
                    if first_price is None:
                        entries.append(FractionEntry(
                            holder, uc.event_id, "PENDING_CASH", cash_units,
                            None, None, "PENDING", uc.effective_date,
                            election.event_id, election.basis))
                    else:
                        price = first_price.price_per_unit
                        cash = round(cash_units * price, ROUND_DECIMALS)
                        exact_cash_total += cash_units * price
                        recorded_cash_total += cash
                        add_residual(uc.event_id, cash_units * price, cash)
                        entries.append(FractionEntry(
                            holder, uc.event_id, "CASH", cash_units, cash, price,
                            "PRICED", uc.effective_date,
                            election.event_id, election.basis))
                    for revocation in adj_revs:
                        restore_units = frac * revocation.fraction
                        restored_units_total += restore_units
                        heapq.heappush(timeline, (revocation.received_date, 2, seq,
                                                  ("RESTORE_CASH", holder, restore_units,
                                                   revocation, uc.event_id,
                                                   first_price.price_per_unit)))
                        seq += 1
                else:   # TRANSFER
                    moved = frac * (1.0 - normal_rev)
                    positions[holder] = base + kept
                    positions[election.to_holder] = (
                        positions.get(election.to_holder, 0.0) + moved)
                    entries.append(FractionEntry(
                        holder, uc.event_id, "UNITS_TRANSFERRED", moved,
                        None, None, "PRICED", uc.effective_date,
                        election.event_id, election.basis,
                        to_holder=election.to_holder))
                    for revocation in adj_revs:
                        restore_units = frac * revocation.fraction
                        heapq.heappush(timeline, (revocation.received_date, 2, seq,
                                                  ("RESTORE_TRANSFER",
                                                   election.to_holder, holder,
                                                   restore_units, revocation,
                                                   uc.event_id)))
                        seq += 1
            # 价格更正：第二次及以后定价产生 ADJUSTMENT，不改写原 CASH 记录
            for previous, current in zip(uc_prices, uc_prices[1:]):
                delta_price = current.price_per_unit - previous.price_per_unit
                for entry in [e for e in entries
                              if e.unit_change_event_id == uc.event_id
                              and e.kind == "CASH"]:
                    exact = entry.units * delta_price
                    recorded = round(exact, ROUND_DECIMALS)
                    exact_cash_total += exact
                    recorded_cash_total += recorded
                    add_residual(uc.event_id, exact, recorded)
                    entries.append(FractionEntry(
                        entry.holder, uc.event_id, "ADJUSTMENT", 0.0,
                        recorded, current.price_per_unit, "ADJUSTMENT",
                        current.received_date, current.event_id, current.basis))
        elif kind == "TRANSFER":
            transfer = payload[1]
            if transfer.ratio is not None:
                moved = (transfer.ratio * total_units
                         * unit_factor_at(all_unit_changes, day))
            else:
                moved = transfer.units
            remaining = positions.get(transfer.from_holder, 0.0) - moved
            if remaining < -1e-6:
                raise ValueError(
                    f"持有人 {transfer.from_holder} 累计分出超过其持有单位")
            positions[transfer.from_holder] = remaining
            positions[transfer.to_holder] = (
                positions.get(transfer.to_holder, 0.0) + moved)
        elif kind == "RESTORE_CASH":
            # 现金替代已定价后撤回：原 CASH 记录不变，冲回现金并回补单位
            _tag, holder, restore_units, revocation, uc_id, price = payload
            positions[holder] = positions.get(holder, 0.0) + restore_units
            exact = -(restore_units * price)
            recorded = round(exact, ROUND_DECIMALS)
            exact_cash_total += exact
            recorded_cash_total += recorded
            add_residual(uc_id, exact, recorded)
            entries.append(FractionEntry(
                holder, uc_id, "ADJUSTMENT", restore_units, recorded, price,
                "ADJUSTMENT", revocation.received_date,
                revocation.event_id, revocation.basis))
        elif kind == "RESTORE_TRANSFER":
            # 零碎单位已转出后撤回：原转移记录不变，调整记录把单位转回
            _tag, from_holder, to_holder, restore_units, revocation, uc_id = payload
            positions[from_holder] = positions.get(from_holder, 0.0) - restore_units
            positions[to_holder] = positions.get(to_holder, 0.0) + restore_units
            entries.append(FractionEntry(
                from_holder, uc_id, "ADJUSTMENT", -restore_units, None, None,
                "ADJUSTMENT", revocation.received_date,
                revocation.event_id, revocation.basis, to_holder=to_holder))
            entries.append(FractionEntry(
                to_holder, uc_id, "ADJUSTMENT", restore_units, None, None,
                "ADJUSTMENT", revocation.received_date,
                revocation.event_id, revocation.basis))
        else:
            raise AssertionError(f"未知时间线事件: {kind}")

    factor_product = 1.0
    for change in all_unit_changes:
        factor_product *= change.factor
    return FractionLedger(
        contract_id=valuation.contract_id,
        as_of=as_of,
        entries=tuple(entries),
        positions=tuple(HolderUnits(h, u) for h, u in sorted(positions.items())),
        cash_residuals=dict(residuals),
        initial_units=float(total_units),
        unit_factor_product=factor_product,
        cashed_units=cashed_units_total,
        restored_units=restored_units_total,
        exact_cash=exact_cash_total,
        recorded_cash=recorded_cash_total,
    )


@dataclass(frozen=True)
class FractionLedger:
    """零碎权益台账：选择结果、待定/可用现金、舍入余量与守恒校验。"""

    contract_id: str
    as_of: date
    entries: tuple
    positions: tuple
    cash_residuals: dict = field(default_factory=dict)
    initial_units: float = 0.0
    unit_factor_product: float = 1.0
    cashed_units: float = 0.0
    restored_units: float = 0.0
    exact_cash: float = 0.0
    recorded_cash: float = 0.0

    @property
    def holders(self) -> tuple:
        return tuple(p.holder for p in self.positions)

    def holder_entries(self, holder: str) -> tuple:
        return tuple(e for e in self.entries if e.holder == holder)

    def position_of(self, holder: str) -> float:
        for position in self.positions:
            if position.holder == holder:
                return position.units
        return 0.0

    def pending_cash_units(self, holder: Optional[str] = None) -> float:
        """价格未定的待定现金权益（单位数）。"""
        return sum(e.units for e in self.entries
                   if e.kind == "PENDING_CASH"
                   and (holder is None or e.holder == holder))

    def available_cash(self, holder: Optional[str] = None) -> float:
        """已定价现金（含价格更正与撤回调整），与待定现金权益分列。"""
        return round(sum(
            e.cash for e in self.entries
            if e.cash is not None and (holder is None or e.holder == holder)),
            ROUND_DECIMALS)

    def residual_summary(self) -> dict:
        """各拆并批次的现金舍入余量与总和，供多方核对。"""
        summary = dict(self.cash_residuals)
        summary["TOTAL"] = round(sum(self.cash_residuals.values()),
                                 ROUND_DECIMALS + 6)
        return summary

    def check_conservation(self, tol: float = 1e-6) -> bool:
        """单位与现金双重守恒校验。"""
        final_units = sum(p.units for p in self.positions)
        expected = (self.initial_units * self.unit_factor_product
                    - self.cashed_units + self.restored_units)
        if abs(final_units - expected) > tol:
            raise AssertionError(
                f"单位不守恒: 期末 {final_units} != 预期 {expected}")
        residual_total = sum(self.cash_residuals.values())
        if abs(self.recorded_cash + residual_total - self.exact_cash) > tol:
            raise AssertionError(
                f"现金不守恒: 已记录 {self.recorded_cash} + 余量 "
                f"{residual_total} != 精确 {self.exact_cash}")
        return True

    def save_json(self, path) -> None:
        Path(path).write_text(json.dumps(
            _ledger_to_dict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path) -> "FractionLedger":
        return _ledger_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _entry_to_dict(entry: FractionEntry) -> dict:
    return {
        "holder": entry.holder,
        "unit_change_event_id": entry.unit_change_event_id,
        "kind": entry.kind,
        "units": entry.units,
        "cash": entry.cash,
        "price": entry.price,
        "state": entry.state,
        "effective_date": entry.effective_date.isoformat(),
        "event_id": entry.event_id,
        "basis": entry.basis,
        "to_holder": entry.to_holder,
    }


def _entry_from_dict(data: dict) -> FractionEntry:
    return FractionEntry(
        holder=data["holder"],
        unit_change_event_id=data["unit_change_event_id"],
        kind=data["kind"],
        units=data["units"],
        cash=data.get("cash"),
        price=data.get("price"),
        state=data["state"],
        effective_date=date.fromisoformat(data["effective_date"]),
        event_id=data.get("event_id"),
        basis=data.get("basis"),
        to_holder=data.get("to_holder"),
    )


def _ledger_to_dict(ledger: FractionLedger) -> dict:
    return {
        "contract_id": ledger.contract_id,
        "as_of": ledger.as_of.isoformat(),
        "entries": [_entry_to_dict(e) for e in ledger.entries],
        "positions": [{"holder": p.holder, "units": p.units}
                      for p in ledger.positions],
        "cash_residuals": ledger.cash_residuals,
        "initial_units": ledger.initial_units,
        "unit_factor_product": ledger.unit_factor_product,
        "cashed_units": ledger.cashed_units,
        "restored_units": ledger.restored_units,
        "exact_cash": ledger.exact_cash,
        "recorded_cash": ledger.recorded_cash,
    }


def _ledger_from_dict(data: dict) -> FractionLedger:
    return FractionLedger(
        contract_id=data["contract_id"],
        as_of=date.fromisoformat(data["as_of"]),
        entries=tuple(_entry_from_dict(item) for item in data["entries"]),
        positions=tuple(HolderUnits(item["holder"], item["units"])
                        for item in data["positions"]),
        cash_residuals=dict(data.get("cash_residuals", {})),
        initial_units=data.get("initial_units", 0.0),
        unit_factor_product=data.get("unit_factor_product", 1.0),
        cashed_units=data.get("cashed_units", 0.0),
        restored_units=data.get("restored_units", 0.0),
        exact_cash=data.get("exact_cash", 0.0),
        recorded_cash=data.get("recorded_cash", 0.0),
    )
