"""零碎权益汇总处置研究：多位持有人的零碎份额汇总出售/统一补偿。

处置方式（AggregationElection.method）：
- RETAIN：各自保留累积，不参与汇总；
- SELL：合并出售，成交现金与费用按实际参与份额（精确未舍入单位）
  比例分配；
- COMPENSATE：按批次指定的统一补偿价补偿，不分摊出售费用。

计算纪律：
- 内部分配一律使用未舍入精确单位与精确金额；落账现金舍入到最小
  货币单位（6 位小数）。
- 无法均分的最小货币单位按 AggregationBatch.residual_rule 分配
  （LARGEST_REMAINDER / HOLDER_ORDER），每次分配的接收人逐笔记录于
  residual_allocations，可复核；不足一个最小货币单位的余量单列于
  sub_unit_residuals，不默默丢弃，也不重复补齐。
- 汇总完成（成交）前撤回：被撤回部分退出，可参与数量重新计算；
  完成后撤回或迟到价格修订：只追加 WITHDRAWAL_ADJUSTMENT /
  PRICE_ADJUSTMENT 记录，已分配现金不当作未发生。
- 同一 event_id 的重复通知只取首次到达，记录于 duplicates_ignored，
  不会再次分配；报告可 save_json/load_json，重开后按更晚 as_of
  重跑时既有记录保持不变。
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
    AggregationBatch,
    AggregationElection,
    AggregationExecution,
    AggregationPriceRevision,
    AggregationWithdrawal,
)
from .valuation import Valuation

_EPS = 1e-9
_UNIT = 10 ** -ROUND_DECIMALS   # 最小货币单位


@dataclass(frozen=True)
class AggregationRecord:
    """汇总处置台账记录：保留 / 成交现金 / 费用 / 补偿 / 调整。"""

    seq: int
    holder: str
    batch_id: str
    kind: str                    # RETAINED / PROCEEDS / FEE / COMPENSATION /
                                 # PRICE_ADJUSTMENT / WITHDRAWAL_ADJUSTMENT
    units: float                 # 精确单位数（该批次单位变化后的口径）
    price: Optional[float]       # 适用单价；无现金含义时为 None
    cash_amount: Optional[float] # 落账现金（舍入到最小货币单位）
    exact_cash: Optional[float]  # 未舍入精确现金，用于守恒核对
    event_id: Optional[str]
    basis: Optional[str]


@dataclass(frozen=True)
class ResidualAllocation:
    """一次最小货币单位余量分配：scope 下某持有人多分得的单位个数。"""

    batch_id: str
    scope: str                   # PROCEEDS / FEE / COMPENSATION / 调整事件 id
    holder: str
    minimal_units: int


@dataclass(frozen=True)
class SubUnitResidual:
    """不足一个最小货币单位的余量：单列核对，不分配也不丢弃。"""

    batch_id: str
    scope: str
    amount: float


@dataclass(frozen=True)
class ProcessStep:
    """过程比较中的一步：事件后剩余可参与份额、累计现金与费用快照。"""

    label: str
    event_id: str
    remaining_units: float       # 已承诺参与但尚未成交的零碎单位
    executed_units: float        # 已成交单位
    retained_units: float        # 已退出汇总（保留/被撤回）的单位
    cumulative_cash: float       # 累计落账现金（净额，费用为负）
    cumulative_fee: float        # 累计落账费用（正数）
    undecided_units: float = 0.0  # 尚未作出选择的零碎单位


@dataclass(frozen=True)
class ProcessComparison:
    """先撤回后定价 vs 先定价后撤回 两种过程的逐步对照。"""

    batch_id: str
    withdraw_first: tuple[ProcessStep, ...]
    price_first: tuple[ProcessStep, ...]

    def check_conservation(self, generated_units: float, tol: float = 1e-6) -> bool:
        """两种过程每一步都满足：剩余 + 已成交 + 保留 = 产生总量。"""
        for name, steps in (("withdraw_first", self.withdraw_first),
                            ("price_first", self.price_first)):
            for step in steps:
                total = (step.remaining_units + step.executed_units
                         + step.retained_units + step.undecided_units)
                if abs(total - generated_units) > tol:
                    raise AssertionError(
                        f"{name} 步骤 {step.label} 单位不守恒: "
                        f"{total} != {generated_units}")
        return True


@dataclass(frozen=True)
class AggregationReport:
    """汇总处置结算结果：逐条记录 + 余量分配明细 + 守恒核对。"""

    contract_id: str
    as_of: date
    records: tuple[AggregationRecord, ...]
    residual_allocations: tuple[ResidualAllocation, ...]
    sub_unit_residuals: tuple[SubUnitResidual, ...]
    duplicates_ignored: tuple[str, ...] = ()

    def holder_records(self, holder: str) -> tuple[AggregationRecord, ...]:
        return tuple(r for r in self.records if r.holder == holder)

    def cash_summary(self) -> dict:
        """各持有人视图：参与/保留单位、净落账现金、费用、调整分列。"""
        out: dict = {}
        for r in self.records:
            bucket = out.setdefault(r.holder, {
                "retained_units": 0.0, "executed_units": 0.0,
                "net_cash": 0.0, "fees": 0.0, "adjustments": 0.0})
            if r.kind == "RETAINED":
                bucket["retained_units"] += r.units
            elif r.kind in ("PROCEEDS", "COMPENSATION"):
                bucket["executed_units"] += r.units
                bucket["net_cash"] += r.cash_amount or 0.0
            elif r.kind == "FEE":
                bucket["fees"] += -(r.cash_amount or 0.0)
                bucket["net_cash"] += r.cash_amount or 0.0
            else:  # PRICE_ADJUSTMENT / WITHDRAWAL_ADJUSTMENT
                bucket["adjustments"] += r.cash_amount or 0.0
                bucket["net_cash"] += r.cash_amount or 0.0
        return {h: {k: round(v, ROUND_DECIMALS) for k, v in bucket.items()}
                for h, bucket in sorted(out.items())}

    def check_conservation(self, tol: float = 1e-6) -> bool:
        """资金守恒：每笔精确现金 = 落账 + 余量分配 + 次最小单位余量。"""
        scopes: dict = {}
        for r in self.records:
            if r.exact_cash is None:
                continue
            scope = (r.batch_id, _record_scope(r))
            bucket = scopes.setdefault(scope, {"exact": 0.0, "booked": 0.0})
            bucket["exact"] += r.exact_cash
            bucket["booked"] += r.cash_amount
        for sub in self.sub_unit_residuals:
            scope = (sub.batch_id, sub.scope)
            bucket = scopes.setdefault(scope, {"exact": 0.0, "booked": 0.0})
            bucket["exact"] -= sub.amount
        for scope, bucket in scopes.items():
            if abs(bucket["exact"] - bucket["booked"]) > tol:
                raise AssertionError(
                    f"资金不守恒: {scope} 精确 {bucket['exact']} != "
                    f"落账 {bucket['booked']}")
        return True

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(_report_to_dict(self), ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> "AggregationReport":
        return _report_from_dict(
            json.loads(Path(path).read_text(encoding="utf-8")))


def _record_scope(record: AggregationRecord) -> str:
    if record.kind in ("PROCEEDS", "FEE", "COMPENSATION"):
        return record.kind
    return record.event_id or record.kind


def _report_to_dict(report: AggregationReport) -> dict:
    return {
        "contract_id": report.contract_id,
        "as_of": report.as_of.isoformat(),
        "records": [asdict(r) for r in report.records],
        "residual_allocations": [asdict(a) for a in report.residual_allocations],
        "sub_unit_residuals": [asdict(s) for s in report.sub_unit_residuals],
        "duplicates_ignored": list(report.duplicates_ignored),
    }


def _report_from_dict(data: dict) -> AggregationReport:
    return AggregationReport(
        contract_id=data["contract_id"],
        as_of=date.fromisoformat(data["as_of"]),
        records=tuple(AggregationRecord(**r) for r in data["records"]),
        residual_allocations=tuple(
            ResidualAllocation(**a) for a in data["residual_allocations"]),
        sub_unit_residuals=tuple(
            SubUnitResidual(**s) for s in data["sub_unit_residuals"]),
        duplicates_ignored=tuple(data.get("duplicates_ignored", ())),
    )


def _allocate_exact(amounts: dict, rule: str, batch_id: str, scope: str):
    """把精确金额按最小货币单位落账，余量按可复核规则分配。

    返回 (落账 dict, 余量分配 list, 次最小单位余量)。
    负金额按绝对值分配后取负；次最小单位余量单列，不丢弃不补齐。
    """
    sign = {h: (1.0 if x >= 0 else -1.0) for h, x in amounts.items()}
    scaled = {h: abs(x) / _UNIT for h, x in amounts.items()}
    floors = {h: math.floor(v + 1e-9) for h, v in scaled.items()}
    leftover = int(round(sum(scaled.values()) - sum(floors.values())))
    if rule == "LARGEST_REMAINDER":
        order = sorted(amounts, key=lambda h: (-(scaled[h] - floors[h]), h))
    else:  # HOLDER_ORDER
        order = sorted(amounts)
    allocations = []
    extra = {h: 0 for h in amounts}
    for h in order[:leftover]:
        extra[h] = 1
        allocations.append(ResidualAllocation(batch_id, scope, h, 1))
    booked = {h: sign[h] * (floors[h] + extra[h]) * _UNIT for h in amounts}
    sub_residual = round(sum(amounts.values()) - sum(booked.values()), 12)
    return booked, allocations, sub_residual


_AGGREGATION_EVENTS = (
    AggregationBatch,
    AggregationElection,
    AggregationWithdrawal,
    AggregationExecution,
    AggregationPriceRevision,
)

_EVENT_RANK = {
    AggregationElection: 0,
    AggregationWithdrawal: 1,
    AggregationExecution: 2,
    AggregationPriceRevision: 3,
}


def _generated_fractions(valuation: Valuation, uc_event_id: str,
                         as_of: date) -> dict:
    """某次单位变化为各持有人产生的零碎单位（精确值，未舍入）。"""
    transfers = _active_transfers(valuation, as_of)
    unit_changes = _active_unit_changes(valuation, as_of)
    target = None
    for change in unit_changes:
        if change.event_id == uc_event_id:
            target = change
            break
    if target is None:
        raise ValueError(f"单位变化 {uc_event_id} 在 {as_of} 尚未到达")
    total_units = getattr(valuation, "total_units", 1.0)
    factor_after = unit_factor_at(unit_changes, target.effective_date)
    shares = _equity_shares(transfers, unit_changes,
                            target.effective_date, total_units)
    out = {}
    for holder, equity in shares.items():
        units_after = equity * total_units * factor_after
        frac = units_after - math.floor(units_after + _EPS)
        if frac > _EPS:
            out[holder] = frac
    return out


def _dedupe_events(events, as_of: date):
    """按 event_id 去重：重复通知只取首次到达，不会再次分配。"""
    seen = {}
    duplicates = []
    for e in events:
        if not isinstance(e, _AGGREGATION_EVENTS) or e.received_date > as_of:
            continue
        key = e.event_id
        if key in seen:
            duplicates.append(key)
            continue
        seen[key] = e
    return seen, duplicates


class _BatchState:
    """单个批次的处置过程：参与份额、成交与调整。"""

    def __init__(self, batch: AggregationBatch, fractions: dict):
        self.batch = batch
        self.fractions = dict(fractions)
        self.pending: dict = {}        # holder -> [SELL, COMPENSATE] 待成交单位
        self.executed: dict = {}       # holder -> [已成交 SELL 单位, 已成交 COMP 单位]
        self.sell_price: Optional[float] = None
        self.comp_price: Optional[float] = None
        self.retained: dict = {}
        self.undecided = sum(fractions.values())
        self.records: list = []
        self.allocations: list = []
        self.sub_residuals: list = []
        self.steps: list = []
        self._seq = 0

    def _add(self, holder, kind, units, price, exact_cash, event_id, basis):
        cash = None
        if exact_cash is not None:
            cash = round(exact_cash, ROUND_DECIMALS)
            self.sub_residuals.append(SubUnitResidual(
                self.batch.event_id, event_id or kind,
                round(exact_cash - cash, 12)))
        self.records.append(AggregationRecord(
            seq=self._seq, holder=holder, batch_id=self.batch.event_id,
            kind=kind, units=units, price=price, cash_amount=cash,
            exact_cash=exact_cash, event_id=event_id, basis=basis))
        self._seq += 1

    def _add_allocated(self, holder_amounts: dict, kind, units_map: dict,
                       price, event_id, basis, scope):
        booked, allocations, sub = _allocate_exact(
            holder_amounts, self.batch.residual_rule, self.batch.event_id, scope)
        self.allocations.extend(allocations)
        if abs(sub) > 0:
            self.sub_residuals.append(
                SubUnitResidual(self.batch.event_id, scope, sub))
        for holder in sorted(holder_amounts):
            self.records.append(AggregationRecord(
                seq=self._seq, holder=holder, batch_id=self.batch.event_id,
                kind=kind, units=units_map[holder], price=price,
                cash_amount=round(booked[holder], ROUND_DECIMALS),
                exact_cash=holder_amounts[holder],
                event_id=event_id, basis=basis))
            self._seq += 1

    def _pending_of(self, holder):
        return self.pending.setdefault(holder, [0.0, 0.0])

    def _executed_of(self, holder):
        return self.executed.setdefault(holder, [0.0, 0.0])

    def _retain(self, holder, units, event_id, basis):
        if units <= _EPS:
            return
        self.retained[holder] = self.retained.get(holder, 0.0) + units
        self._add(holder, "RETAINED", units, None, None, event_id, basis)

    def _snapshot(self, label, event_id):
        remaining = sum(sum(p) for p in self.pending.values())
        executed = sum(sum(x) for x in self.executed.values())
        retained = sum(self.retained.values())
        cash = sum(r.cash_amount for r in self.records
                   if r.cash_amount is not None)
        fee = -sum(r.cash_amount for r in self.records if r.kind == "FEE")
        self.steps.append(ProcessStep(
            label=label, event_id=event_id,
            remaining_units=round(remaining, 12),
            executed_units=round(executed, 12),
            retained_units=round(retained, 12),
            cumulative_cash=round(cash, ROUND_DECIMALS),
            cumulative_fee=round(fee, ROUND_DECIMALS),
            undecided_units=round(self.undecided, 12)))

    def on_election(self, event: AggregationElection):
        holder = event.holder
        frac = self.fractions.get(holder, 0.0)
        if frac <= _EPS:
            return
        self.undecided -= frac
        if event.method == "RETAIN":
            self._retain(holder, frac, event.event_id, event.basis)
        else:
            slot = 0 if event.method == "SELL" else 1
            self._pending_of(holder)[slot] += frac
        self._snapshot(f"选择 {event.method}", event.event_id)

    def on_withdrawal(self, event: AggregationWithdrawal, election):
        holder = election.holder
        frac = self.fractions.get(holder, 0.0)
        units = frac * event.portion
        slot = 0 if election.method == "SELL" else 1
        pending = self._pending_of(holder)
        executed = self._executed_of(holder)
        if pending[slot] > _EPS:
            # 汇总完成前撤回：退出参与，可参与数量重新计算
            take = min(units, pending[slot])
            pending[slot] -= take
            self._retain(holder, take, event.event_id, event.basis)
            units -= take
        if units > _EPS and executed[slot] > _EPS:
            # 完成后撤回：已收现金不当作未发生，只追加追回调整
            take = min(units, executed[slot])
            executed[slot] -= take
            price = self.sell_price if slot == 0 else self.comp_price
            self._retain(holder, take, event.event_id, event.basis)
            self._add(holder, "WITHDRAWAL_ADJUSTMENT", take, price,
                      -(take * price), event.event_id, event.basis)
        self._snapshot("撤回", event.event_id)

    def on_execution(self, event: AggregationExecution):
        sellers = {h: p[0] for h, p in self.pending.items() if p[0] > _EPS}
        comps = {h: p[1] for h, p in self.pending.items() if p[1] > _EPS}
        if comps and event.compensation_price_per_unit is None:
            raise ValueError(
                f"批次 {self.batch.event_id} 存在统一补偿参与者但未给补偿价")
        if sellers:
            gross = {h: u * event.price_per_unit for h, u in sellers.items()}
            self._add_allocated(gross, "PROCEEDS", sellers,
                                event.price_per_unit, event.event_id,
                                event.basis, "PROCEEDS")
            if event.total_fee > _EPS:
                total_units = sum(sellers.values())
                fees = {h: -event.total_fee * u / total_units
                        for h, u in sellers.items()}
                zero_units = {h: 0.0 for h in sellers}
                self._add_allocated(fees, "FEE", zero_units,
                                    None, event.event_id, event.basis, "FEE")
            for h, u in sellers.items():
                self._executed_of(h)[0] += u
            self.sell_price = event.price_per_unit
        if comps:
            price = event.compensation_price_per_unit
            amounts = {h: u * price for h, u in comps.items()}
            self._add_allocated(amounts, "COMPENSATION", comps,
                                price, event.event_id, event.basis,
                                "COMPENSATION")
            for h, u in comps.items():
                self._executed_of(h)[1] += u
            self.comp_price = price
        for h in list(self.pending):
            self.pending[h] = [0.0, 0.0]
        self._snapshot("成交", event.event_id)

    def on_revision(self, event: AggregationPriceRevision):
        if self.sell_price is None:
            raise ValueError(
                f"价格修订 {event.event_id} 到达时批次尚未成交")
        holders = {h: x[0] for h, x in self.executed.items() if x[0] > _EPS}
        if holders:
            delta = event.new_price_per_unit - self.sell_price
            amounts = {h: u * delta for h, u in holders.items()}
            self._add_allocated(amounts, "PRICE_ADJUSTMENT", holders,
                                event.new_price_per_unit, event.event_id,
                                event.basis, event.event_id)
        self.sell_price = event.new_price_per_unit
        self._snapshot("价格修订", event.event_id)


def _settle(valuation: Valuation, as_of: date, order: Optional[dict] = None,
            only_batch: Optional[str] = None, keep_steps: bool = False):
    seen, duplicates = _dedupe_events(valuation.events, as_of)
    batches = [e for e in seen.values() if isinstance(e, AggregationBatch)]
    batches.sort(key=lambda b: (b.received_date, b.event_id))
    records, allocations, subs, steps = [], [], [], {}
    for batch in batches:
        if only_batch is not None and batch.event_id != only_batch:
            continue
        fractions = _generated_fractions(
            valuation, batch.unit_change_event_id, as_of)
        state = _BatchState(batch, fractions)
        elections = {e.event_id: e for e in seen.values()
                     if isinstance(e, AggregationElection)
                     and e.batch_id == batch.event_id}
        executions = {e.event_id: e for e in seen.values()
                      if isinstance(e, AggregationExecution)
                      and e.batch_id == batch.event_id}
        members = [
            e for e in seen.values()
            if e.event_id in elections or e.event_id in executions
            or (isinstance(e, AggregationWithdrawal)
                and e.election_event_id in elections)
            or (isinstance(e, AggregationPriceRevision)
                and e.execution_event_id in executions)]
        elections_holders = {e.holder for e in elections.values()}
        withdrawals = {}
        for e in members:
            if isinstance(e, AggregationWithdrawal):
                if e.election_event_id not in elections:
                    raise ValueError(
                        f"撤回 {e.event_id} 指向未知选择 {e.election_event_id}")
                withdrawals.setdefault(e.election_event_id, 0.0)
                withdrawals[e.election_event_id] += e.portion
        for eid, portion in withdrawals.items():
            if portion > 1 + _EPS:
                raise ValueError(f"选择 {eid} 累计撤回比例超过 1")

        def sort_key(e):
            if order is not None:
                return (order[type(e)], e.received_date, e.event_id)
            return (e.received_date, _EVENT_RANK[type(e)], e.event_id)

        # 未选择的持有人默认 RETAIN（先于处置时间线落账）
        elected = set()
        for holder in sorted(fractions):
            if holder not in elections_holders:
                state.undecided -= fractions[holder]
                state._retain(holder, fractions[holder], None, "默认保留")
        for event in sorted(members, key=sort_key):
            if isinstance(event, AggregationElection):
                if event.holder in elected:
                    raise ValueError(
                        f"持有人 {event.holder} 在批次 {batch.event_id} 重复选择")
                elected.add(event.holder)
                state.on_election(event)
            elif isinstance(event, AggregationWithdrawal):
                state.on_withdrawal(event, elections[event.election_event_id])
            elif isinstance(event, AggregationExecution):
                state.on_execution(event)
            else:
                state.on_revision(event)
        records.extend(state.records)
        allocations.extend(state.allocations)
        subs.extend(s for s in state.sub_residuals if abs(s.amount) > 0)
        if keep_steps:
            steps[batch.event_id] = tuple(state.steps)
    return records, allocations, subs, duplicates, steps


def aggregation_settlement(valuation: Valuation,
                           as_of: Optional[date] = None) -> AggregationReport:
    """按 as_of 已到达的事件结算全部汇总批次。

    同一知识时点重跑结果一致；重开后用更晚 as_of 继续处理同一批次时，
    既有记录保持不变，迟到事件只追加新记录。
    """
    if as_of is None:
        as_of = valuation.valuation_date
    records, allocations, subs, duplicates, _ = _settle(valuation, as_of)
    return AggregationReport(
        contract_id=valuation.contract_id,
        as_of=as_of,
        records=tuple(replace(r, seq=i) for i, r in enumerate(records)),
        residual_allocations=tuple(allocations),
        sub_unit_residuals=tuple(subs),
        duplicates_ignored=tuple(duplicates),
    )


def compare_processes(valuation: Valuation, batch_id: str,
                      as_of: Optional[date] = None) -> ProcessComparison:
    """比较同一批次的两种过程：先撤回后定价 vs 先定价后撤回。

    两种过程使用相同事件集合，仅改变撤回与成交的相对顺序；
    逐步记录剩余可参与份额、累计现金与费用，供守恒核对。
    """
    if as_of is None:
        as_of = valuation.valuation_date
    withdraw_first_order = {
        AggregationElection: 0, AggregationWithdrawal: 1,
        AggregationExecution: 2, AggregationPriceRevision: 3}
    price_first_order = {
        AggregationElection: 0, AggregationExecution: 1,
        AggregationPriceRevision: 2, AggregationWithdrawal: 3}
    steps = {}
    for name, order in (("withdraw_first", withdraw_first_order),
                        ("price_first", price_first_order)):
        _, _, _, _, batch_steps = _settle(
            valuation, as_of, order=order, only_batch=batch_id,
            keep_steps=True)
        steps[name] = batch_steps.get(batch_id, ())
    return ProcessComparison(
        batch_id=batch_id,
        withdraw_first=steps["withdraw_first"],
        price_first=steps["price_first"],
    )
