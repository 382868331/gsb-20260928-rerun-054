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

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
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
    FractionPool,
    FractionPoolExecution,
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
    pool_id: Optional[str] = None
    fee_amount: Optional[float] = None
    exact_fee: Optional[float] = None


@dataclass(frozen=True)
class FractionReport:
    """一次零碎权益结算结果：逐条记录 + 舍入余量 + 守恒核对。"""

    contract_id: str
    as_of: date
    generated: tuple[GeneratedFraction, ...]
    records: tuple[FractionRecord, ...]
    residuals: tuple[float, ...]   # 每笔现金记录的舍入余量（exact - 落账）
    processed_notification_ids: tuple[str, ...] = ()

    @property
    def total_residual(self) -> float:
        # 不按最小可分单位舍入，否则余量会被静默吞掉
        return round(sum(self.residuals), 12)

    def holder_records(self, holder: str) -> tuple[FractionRecord, ...]:
        return tuple(r for r in self.records if r.holder == holder)

    def cash_summary(self) -> dict:
        """各持有人现金视图：待定单位、净可用现金、调整三类分列。

        可用现金是已确认现金分配与全部已确认价格/撤回调整后的净额；
        adjustments 仍单独列示调整发生额，便于审计轨迹核对。
        """
        out: dict = {}
        for r in self.records:
            bucket = out.setdefault(r.holder, {
                "pending_units": 0.0, "available_cash": 0.0, "adjustments": 0.0})
            if r.kind in ("PENDING_CASH", "POOL_PENDING"):
                bucket["pending_units"] += r.units
            elif r.kind in ("CASH_IN_LIEU", "PRICE_ADJUSTMENT",
                            "REVOCATION_ADJUSTMENT", "POOL_PROCEEDS",
                            "POOL_FEE", "POOL_ADJUSTMENT",
                            "POOL_FEE_ADJUSTMENT", "POOL_REVOCATION_ADJUSTMENT",
                            "POOL_REVOCATION_FEE_ADJUSTMENT"):
                if r.cash_amount is not None:
                    bucket["available_cash"] += r.cash_amount
                    if r.kind not in ("CASH_IN_LIEU", "POOL_PROCEEDS", "POOL_FEE"):
                        bucket["adjustments"] += r.cash_amount
        pending_keys = {
            (r.holder, r.unit_change_event_id)
            for r in self.records if r.kind == "PENDING_CASH"
        }
        for holder, bucket in out.items():
            for r in self.records:
                if (r.holder == holder
                        and r.kind == "REVOCATION_ADJUSTMENT"
                        and r.price is None
                        and (r.holder, r.unit_change_event_id) in pending_keys):
                    bucket["pending_units"] -= r.units
        return {h: {k: round(v, ROUND_DECIMALS) for k, v in bucket.items()}
                for h, bucket in sorted(out.items())}

    def retained_units(self) -> dict:
        """各持有人保留累积的零碎单位（含转入、含被撤回恢复部分）。"""
        out: dict = {}
        for r in self.records:
            if r.kind in ("RETAINED", "TRANSFER_IN"):
                out[r.holder] = out.get(r.holder, 0.0) + r.units
        return {h: round(u, 12) for h, u in sorted(out.items())}

    def pool_summary(self) -> dict:
        """按汇总批次列示成交总额、费用、净现金与来源份额。"""
        out: dict = {}
        for record in self.records:
            if record.pool_id is None:
                continue
            pool = out.setdefault(record.pool_id, {})
            holder = pool.setdefault(record.holder, {
                "source_units": 0.0, "gross_cash": 0.0,
                "fee_amount": 0.0, "net_cash": 0.0})
            if record.kind in ("POOL_PROCEEDS", "POOL_ADJUSTMENT",
                               "POOL_REVOCATION_ADJUSTMENT"):
                holder["gross_cash"] += record.cash_amount or 0.0
            if record.kind in ("POOL_FEE", "POOL_FEE_ADJUSTMENT",
                               "POOL_REVOCATION_FEE_ADJUSTMENT"):
                holder["fee_amount"] += record.fee_amount or 0.0
            if record.kind == "POOL_PROCEEDS":
                holder["source_units"] += record.units
        for pool in out.values():
            for holder in pool.values():
                holder["net_cash"] = round(
                    holder["gross_cash"] - holder["fee_amount"], ROUND_DECIMALS)
                for key in ("source_units", "gross_cash", "fee_amount"):
                    holder[key] = round(holder[key], ROUND_DECIMALS)
        return out

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
                          "TRANSFER_OUT", "POOL_PENDING", "POOL_PROCEEDS"):
                key = (r.unit_change_event_id, r.holder)
                disposed[key] = disposed.get(key, 0.0) + r.units
            if r.kind == "REVOCATION_ADJUSTMENT":
                key = (r.unit_change_event_id, r.holder)
                disposed[key] = disposed.get(key, 0.0) - r.units
            if r.kind == "POOL_REVOCATION_ADJUSTMENT":
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

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(_fraction_report_to_dict(self), ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> "FractionReport":
        return _fraction_report_from_dict(
            json.loads(Path(path).read_text(encoding="utf-8")))


def _record_to_dict(record: FractionRecord) -> dict:
    return asdict(record)


def _record_from_dict(data: dict) -> FractionRecord:
    return FractionRecord(**data)


def _fraction_report_to_dict(report: FractionReport) -> dict:
    return {
        "contract_id": report.contract_id,
        "as_of": report.as_of.isoformat(),
        "generated": [asdict(item) for item in report.generated],
        "records": [_record_to_dict(item) for item in report.records],
        "residuals": list(report.residuals),
        "processed_notification_ids": list(report.processed_notification_ids),
    }


def _fraction_report_from_dict(data: dict) -> FractionReport:
    return FractionReport(
        contract_id=data["contract_id"],
        as_of=date.fromisoformat(data["as_of"]),
        generated=tuple(GeneratedFraction(**item) for item in data["generated"]),
        records=tuple(_record_from_dict(item) for item in data["records"]),
        residuals=tuple(data["residuals"]),
        processed_notification_ids=tuple(data.get("processed_notification_ids", ())),
    )


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


def _largest_remainder(weights: dict, total: float) -> tuple[dict, dict]:
    """按权重分配已为最小货币单位整数倍的总额。

    余缺按精确余数从大到小补/扣一个最小货币单位；余数相同按持有人和来源
    事件号排序。因此每一分钱都有确定、可复核的归属，不留在未分配桶中。
    """
    weight_total = sum(weights.values())
    if weight_total <= _EPS:
        return {}, {}
    scale = 10 ** ROUND_DECIMALS
    exact = {key: total * weight / weight_total for key, weight in weights.items()}
    raw = {key: value * scale for key, value in exact.items()}
    floors = {key: int(math.floor(value + 1e-10)) for key, value in raw.items()}
    delta_units = int(round(total * scale)) - sum(floors.values())
    ordered = sorted(raw, key=lambda holder: -(raw[holder] - floors[holder]))
    if delta_units >= 0:
        for index in range(delta_units):
            floors[ordered[index % len(ordered)]] += 1
    else:
        for index in range(-delta_units):
            key = ordered[-(index % len(ordered)) - 1]
            floors[key] -= 1
    allocated = {key: value / scale for key, value in floors.items()}
    return allocated, exact


def _settle_fraction_pool(*, pool_id, source, events, as_of, add, add_pool,
                          removed, records, residuals,
                          processed_notification_ids):
    uc_id = source["uc_id"]
    denom = source["denom"]
    base_units = dict(source["units"])
    elections_by_holder = source["election"]
    executions = sorted(
        (e for e in events
         if isinstance(e, FractionPoolExecution)
         and e.pool_id == pool_id
         and e.received_date <= as_of),
        key=lambda e: (e.received_date, e.event_id))
    if not executions:
        return

    execution_by_id = {}
    for execution in executions:
        previous = execution_by_id.get(execution.event_id)
        if previous is None or (
                execution.received_date, execution.event_id) < (
                previous.received_date, previous.event_id):
            execution_by_id[execution.event_id] = execution
    unique_executions = sorted(
        execution_by_id.values(), key=lambda e: (e.received_date, e.event_id))
    base_execution = unique_executions[0]
    known_chain = {base_execution.event_id}
    for execution in unique_executions[1:]:
        if execution.corrects_event_id not in known_chain:
            raise ValueError(
                f"汇总通知 {execution.event_id} 的更正指向未知通知 "
                f"{execution.corrects_event_id}")
        known_chain.add(execution.event_id)
    processed_notification_ids.append(base_execution.event_id)
    gross_alloc, exact_gross = _largest_remainder(
        base_units, base_execution.gross_amount)
    fee_alloc, exact_fee = _largest_remainder(
        base_units, base_execution.fee_amount)
    for holder in sorted(base_units):
        units = base_units[holder]
        add_pool(FractionRecord(
            seq=0, holder=holder, unit_change_event_id=uc_id,
            kind="POOL_PROCEEDS", units=units, price=None,
            cash_amount=gross_alloc[holder], exact_cash=exact_gross[holder],
            event_id=base_execution.event_id, basis=base_execution.basis,
            pool_id=pool_id))
        if base_execution.fee_amount > _EPS:
            add_pool(FractionRecord(
                seq=0, holder=holder, unit_change_event_id=uc_id,
                kind="POOL_FEE", units=units, price=None,
                cash_amount=-fee_alloc[holder], exact_cash=-exact_fee[holder],
                event_id=base_execution.event_id, basis=base_execution.basis,
                pool_id=pool_id, fee_amount=fee_alloc[holder],
                exact_fee=exact_fee[holder]))
        removed[holder] = removed.get(holder, 0.0) + units / denom

    active_units = dict(base_units)
    prior_gross = base_execution.gross_amount
    prior_fee = base_execution.fee_amount
    election_to_holder = {
        election.event_id: holder
        for holder, election in elections_by_holder.items()}
    handled_revocations = set()

    def post_revocations(cutoff):
        nonlocal prior_gross, prior_fee
        revocations = sorted(
            (e for e in events
             if isinstance(e, ElectionRevocation)
             and e.election_event_id in election_to_holder
             and base_execution.received_date <= e.received_date <= cutoff
             and e.event_id not in handled_revocations),
            key=lambda e: (e.received_date, e.event_id))
        total_base_units = sum(base_units.values())
        for revocation in revocations:
            handled_revocations.add(revocation.event_id)
            holder = election_to_holder[revocation.election_event_id]
            original_units = base_units.get(holder, 0.0)
            current = active_units.get(holder, 0.0)
            revoked_units = min(original_units * revocation.portion, current)
            if revoked_units <= _EPS:
                continue
            active_units[holder] = current - revoked_units
            add(holder, uc_id, "RETAINED", revoked_units, None, None,
                revocation.event_id, revocation.basis)
            gross_clawback = revoked_units * prior_gross / total_base_units
            fee_refund = revoked_units * prior_fee / total_base_units
            add_pool(FractionRecord(
                seq=0, holder=holder, unit_change_event_id=uc_id,
                kind="POOL_REVOCATION_ADJUSTMENT", units=revoked_units,
                price=None, cash_amount=round(-gross_clawback, ROUND_DECIMALS),
                exact_cash=-gross_clawback, event_id=revocation.event_id,
                    basis=revocation.basis, pool_id=pool_id,
                    ))
            if fee_refund > _EPS:
                add_pool(FractionRecord(
                    seq=0, holder=holder, unit_change_event_id=uc_id,
                    kind="POOL_REVOCATION_FEE_ADJUSTMENT", units=revoked_units,
                    price=None, cash_amount=round(fee_refund, ROUND_DECIMALS),
                    exact_cash=fee_refund, event_id=revocation.event_id,
                    basis=revocation.basis, pool_id=pool_id,
                    fee_amount=round(-fee_refund, ROUND_DECIMALS),
                    exact_fee=-fee_refund))

    for execution in unique_executions[1:]:
        post_revocations(execution.received_date)
        processed_notification_ids.append(execution.event_id)
        weights = {h: u for h, u in active_units.items() if u > _EPS}
        delta_gross = execution.gross_amount - prior_gross
        delta_fee = execution.fee_amount - prior_fee
        gross_delta, gross_exact = _largest_remainder(weights, delta_gross)
        fee_delta, fee_exact = _largest_remainder(weights, delta_fee)
        for holder in sorted(weights):
            if abs(gross_exact[holder]) > _EPS:
                add_pool(FractionRecord(
                    seq=0, holder=holder, unit_change_event_id=uc_id,
                    kind="POOL_ADJUSTMENT", units=weights[holder], price=None,
                    cash_amount=gross_delta[holder],
                    exact_cash=gross_exact[holder], event_id=execution.event_id,
                    basis=execution.basis, pool_id=pool_id))
            if abs(fee_exact[holder]) > _EPS:
                add_pool(FractionRecord(
                    seq=0, holder=holder, unit_change_event_id=uc_id,
                    kind="POOL_FEE_ADJUSTMENT", units=weights[holder],
                    price=None, cash_amount=-fee_delta[holder],
                    exact_cash=-fee_exact[holder], event_id=execution.event_id,
                    basis=execution.basis, pool_id=pool_id,
                    fee_amount=fee_delta[holder], exact_fee=fee_exact[holder]))
        prior_gross = execution.gross_amount
        prior_fee = execution.fee_amount

    post_revocations(as_of)


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
    pool_sources: dict = {}
    processed_notification_ids: list[str] = []
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

    def add_pool(record):
        nonlocal seq
        records.append(replace(record, seq=seq))
        if record.exact_cash is not None:
            residuals.append(round(record.exact_cash - record.cash_amount, 12))
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
            ratio_denom = total_units * factor_after

            pool_notice = next(
                (e for e in events
                 if isinstance(e, FractionPool)
                  and e.pool_id == getattr(election, "pool_id", None)
                  and e.unit_change_event_id == uc.event_id
                  and e.received_date <= as_of),
                None)

            if method == "POOL" and (
                    pool_notice is None
                    or election.received_date > pool_notice.cutoff_date
                    or election.effective_date > pool_notice.cutoff_date):
                method = "RETAIN"

            if method == "RETAIN":
                add(holder, uc.event_id, "RETAINED", frac,
                    None, None,
                    election.event_id if election else None,
                    election.basis if election else "默认保留累积")
            elif method == "TRANSFER":
                portion = revoked.get(election.event_id, 0.0) if election else 0.0
                elected_frac = frac * (1.0 - portion)
                revoked_frac = frac * portion
                if revoked_frac > _EPS:
                    rev_id, rev_basis = revocation_basis[election.event_id]
                    add(holder, uc.event_id, "RETAINED", revoked_frac,
                        None, None, rev_id, rev_basis)
                if elected_frac <= _EPS:
                    continue
                removed[holder] = removed.get(holder, 0.0) + elected_frac / ratio_denom
                removed[election.to_holder] = (
                    removed.get(election.to_holder, 0.0) - elected_frac / ratio_denom)
                add(holder, uc.event_id, "TRANSFER_OUT", elected_frac,
                    None, None, election.event_id, election.basis)
                add(election.to_holder, uc.event_id, "TRANSFER_IN", elected_frac,
                    None, None, election.event_id, election.basis)
            elif method == "POOL":
                executions = sorted(
                    (e for e in events
                     if isinstance(e, FractionPoolExecution)
                     and e.pool_id == pool_notice.pool_id
                     and e.received_date <= as_of),
                    key=lambda e: (e.received_date, e.event_id))
                before_execution = 0.0
                before_events = []
                cutoff = executions[0].received_date if executions else as_of
                before_events = [
                    e for e in events
                    if isinstance(e, ElectionRevocation)
                    and e.election_event_id == election.event_id
                    and e.received_date < cutoff]
                before_events.sort(key=lambda e: (e.received_date, e.event_id))
                for event in before_events:
                    before_execution = min(1.0, before_execution + event.portion)
                revoked_before = frac * before_execution
                active_units = frac - revoked_before
                if revoked_before > _EPS:
                    rev_event = sorted(
                        before_events, key=lambda e: (e.received_date, e.event_id))[-1]
                    add(holder, uc.event_id, "RETAINED", revoked_before,
                        None, None, rev_event.event_id, rev_event.basis)
                if not executions:
                    if active_units > _EPS:
                        add(holder, uc.event_id, "POOL_PENDING", active_units,
                            None, None, election.event_id, election.basis)
                else:
                    source = pool_sources.setdefault(pool_notice.pool_id, {
                        "uc_id": uc.event_id, "notice": pool_notice,
                        "denom": ratio_denom, "units": {}, "election": {}})
                    source["units"][holder] = active_units
                    source["election"][holder] = election

            else:  # CASH
                pending_units = frac
                cash_units = 0.0
                latest_price = None
                timeline = []
                if election is not None:
                    timeline.append((election.received_date, 0, election.event_id, election))
                for price in price_chain:
                    timeline.append((price.received_date, 1, price.event_id, price))
                for event in events:
                    if (isinstance(event, ElectionRevocation)
                            and election is not None
                            and event.election_event_id == election.event_id
                            and event.received_date <= as_of):
                        timeline.append((event.received_date, 2, event.event_id, event))
                timeline.sort(key=lambda item: (item[0], item[1], item[2]))

                already_revoked = 0.0
                for _, _, _, event in timeline:
                    if isinstance(event, FractionElection):
                        if pending_units > _EPS and latest_price is None:
                            add(holder, uc.event_id, "PENDING_CASH", pending_units,
                                None, None, event.event_id, event.basis)
                    elif isinstance(event, CashPriceFixing):
                        if latest_price is None:
                            if pending_units > _EPS:
                                cash_units = pending_units
                                pending_units = 0.0
                                for index in range(len(records) - 1, -1, -1):
                                    prior = records[index]
                                    if (prior.holder == holder
                                            and prior.unit_change_event_id == uc.event_id
                                            and prior.kind in (
                                                "PENDING_CASH",
                                                "REVOCATION_ADJUSTMENT")
                                            and (prior.kind == "PENDING_CASH"
                                                 or prior.price is None)):
                                        del records[index]
                                        seq -= 1
                                add(holder, uc.event_id, "CASH_IN_LIEU", cash_units,
                                    event.price_per_unit,
                                    cash_units * event.price_per_unit,
                                    event.event_id, event.basis)
                        elif cash_units > _EPS:
                            add(holder, uc.event_id, "PRICE_ADJUSTMENT", cash_units,
                                event.price_per_unit,
                                cash_units * (event.price_per_unit - latest_price),
                                event.event_id, event.basis)
                        latest_price = event.price_per_unit
                    else:
                        revocation_portion = min(
                            max(event.portion - already_revoked, 0.0),
                            1.0 - already_revoked)
                        newly_revoked = frac * revocation_portion
                        already_revoked += revocation_portion
                        if newly_revoked <= _EPS:
                            continue
                        add(holder, uc.event_id, "RETAINED", newly_revoked,
                            None, None, event.event_id, event.basis)
                        if pending_units > _EPS:
                            revoked_now = min(newly_revoked, pending_units)
                            pending_units -= revoked_now
                            add(holder, uc.event_id, "REVOCATION_ADJUSTMENT", revoked_now,
                                None, None, event.event_id, event.basis)
                            newly_revoked -= revoked_now
                        if newly_revoked > _EPS and cash_units > _EPS:
                            revoked_now = min(newly_revoked, cash_units)
                            cash_units -= revoked_now
                            add(holder, uc.event_id, "REVOCATION_ADJUSTMENT", revoked_now,
                                latest_price, -(revoked_now * latest_price),
                                event.event_id, event.basis)

                if cash_units > _EPS:
                    removed[holder] = (
                        removed.get(holder, 0.0) + cash_units / ratio_denom)

    for pool_id in sorted(pool_sources):
        _settle_fraction_pool(
            pool_id=pool_id,
            source=pool_sources[pool_id],
            events=events,
            as_of=as_of,
            add=add,
            add_pool=add_pool,
            removed=removed,
            records=records,
            residuals=residuals,
            processed_notification_ids=processed_notification_ids,
        )

    return FractionReport(
        contract_id=valuation.contract_id,
        as_of=as_of,
        generated=tuple(generated),
        records=tuple(replace(record, seq=index)
                      for index, record in enumerate(records)),
        residuals=tuple(residuals),
        processed_notification_ids=tuple(dict.fromkeys(processed_notification_ids)),
    )
