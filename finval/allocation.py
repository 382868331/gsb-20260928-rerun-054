"""持有人分配与对账：权益转让、单位拆并下各方份额、补偿记录与总额守恒。

转让不改变合约现金流总额，只改变份额归属：
- 已支付的款项仍归原支付记录（按支付日时已到达的转让/撤销计算）；
- 尚未支付的本金、利息与调整款按当前已到达事件分配到各持有人；
- 转让通知迟到或随后撤销时，历史视角保持原结论，
  当前视角以 COMPENSATION 补偿记录说明差额。

单位拆分/合并（UnitChange）只改变计量单位口径，不改变经济权益总量：
固定比例转让（ratio）在任何单位口径下含义不变；固定单位转让（units）
按转让生效日的单位口径折算为权益比例，之后的拆分或合并都不会
改变其对应的原合约权益。每笔现金流的舍入余量单独列示，
不并入任一持有人份额。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .events import RightsTransfer, TransferRevocation, UnitChange
from .valuation import Valuation

ORIGINAL_HOLDER = "ORIGINAL"
ROUND_DECIMALS = 6   # 最小可分单位：金额舍入到小数点后 6 位


@dataclass(frozen=True)
class HolderShare:
    """某一持有人在某笔现金流上的份额或补偿记录。"""

    cashflow_seq: int
    holder: str
    cashflow_index: int
    payment_date: date
    amount: float
    payment_state: str          # PAID / FUTURE
    kind: str                   # SCHEDULED_SHARE / COMPENSATION
    cashflow_kind: str          # SCHEDULED / ADJUSTMENT / TERMINATION_PRINCIPAL ...
    event_id: Optional[str] = None
    basis: Optional[str] = None
    unit_factor: float = 1.0    # 该笔支付日适用的累计单位倍率（变化前后解释）


@dataclass(frozen=True)
class CashflowTotal:
    """原合约视角下单笔现金流总额（分配前）。"""

    cashflow_seq: int
    cashflow_index: int
    payment_date: date
    cashflow_kind: str
    amount: float
    unallocated_residual: float = 0.0   # 舍入余量：未分给任何持有人的部分


@dataclass(frozen=True)
class HolderPosition:
    """某一时点某持有人的持仓：计量单位数与对应原合约权益比例。"""

    holder: str
    units: float            # at_date 单位口径下的单位数
    equity_ratio: float     # 对应原合约权益的比例


def _active_transfers(valuation: Valuation, as_of: date) -> tuple[RightsTransfer, ...]:
    """as_of 时刻已到达且未被撤销的转让，按生效日/到达日排序。"""
    transfers = {}
    revoked = set()
    for event in valuation.events:
        if isinstance(event, RightsTransfer) and event.received_date <= as_of:
            transfers[event.event_id] = event
        elif isinstance(event, TransferRevocation) and event.received_date <= as_of:
            revoked.add(event.transfer_event_id)
    active = [t for tid, t in transfers.items() if tid not in revoked]
    active.sort(key=lambda t: (t.effective_date, t.received_date, t.event_id))
    return tuple(active)


def _applicable(transfers, payment_date: date):
    return [t for t in transfers
            if t.effective_date <= payment_date
            and (t.end_date is None or payment_date <= t.end_date)]


def _active_unit_changes(valuation: Valuation, as_of: date) -> tuple[UnitChange, ...]:
    """as_of 时刻已到达的单位拆分/合并，按生效日/到达日排序。"""
    changes = [e for e in valuation.events
               if isinstance(e, UnitChange) and e.received_date <= as_of]
    changes.sort(key=lambda c: (c.effective_date, c.received_date, c.event_id))
    return tuple(changes)


def unit_factor_at(unit_changes, day: date) -> float:
    """day 时点适用的累计单位倍率（拆分/合并只改变单位口径）。"""
    factor = 1.0
    for change in unit_changes:
        if change.effective_date <= day:
            factor *= change.factor
    return factor


def _transfer_fraction(transfer: RightsTransfer, unit_changes, total_units: float) -> float:
    """一笔转让对应的权益比例。

    ratio 转让：比例与单位口径无关；
    units 转让：固定单位数按转让生效日的单位口径折算，
    之后的拆分/合并不改变其对应的原合约权益。
    """
    if transfer.ratio is not None:
        return transfer.ratio
    denomination = total_units * unit_factor_at(unit_changes, transfer.effective_date)
    return transfer.units / denomination


def _equity_shares(transfers, unit_changes, payment_date: date, total_units: float) -> dict:
    """payment_date 时各持有人的权益比例（合计为 1）。"""
    shares = {ORIGINAL_HOLDER: 1.0}
    for t in _applicable(transfers, payment_date):
        fraction = _transfer_fraction(t, unit_changes, total_units)
        shares[t.from_holder] = shares.get(t.from_holder, 0.0) - fraction
        shares[t.to_holder] = shares.get(t.to_holder, 0.0) + fraction
    for holder, share in shares.items():
        if share < -1e-9:
            raise ValueError(f"持有人 {holder} 累计分出超过原权益")
    return {h: s for h, s in shares.items() if abs(s) > 1e-12}


def _split(amount: float, transfers, unit_changes, payment_date: date,
           total_units: float) -> tuple[dict, float]:
    """把一笔现金流分到各持有人，返回 (份额字典, 未分配舍入余量)。

    每个持有人份额独立舍入到最小可分单位，余量单独列示，
    不并入任一持有人；份额合计 + 余量 精确等于 amount。
    """
    shares = _equity_shares(transfers, unit_changes, payment_date, total_units)
    amounts = {h: round(amount * s, ROUND_DECIMALS) for h, s in shares.items()}
    residual = round(amount - sum(amounts.values()), ROUND_DECIMALS)
    return amounts, residual


def _late_cause(valuation: Valuation, payment_date: date, as_of: date):
    """支付日之后才到达、且适用于该笔现金流的事件（取最晚到达者）。"""
    transfers = {e.event_id: e for e in valuation.events
                 if isinstance(e, RightsTransfer)}

    def covers(t) -> bool:
        return (t.effective_date <= payment_date
                and (t.end_date is None or payment_date <= t.end_date))

    causes = [t for t in transfers.values()
              if covers(t) and payment_date < t.received_date <= as_of]
    for event in valuation.events:
        if isinstance(event, TransferRevocation) and payment_date < event.received_date <= as_of:
            target = transfers.get(event.transfer_event_id)
            if target is not None and covers(target):
                causes.append(event)
        elif isinstance(event, UnitChange) and payment_date < event.received_date <= as_of:
            if event.effective_date <= payment_date:
                causes.append(event)
    if not causes:
        return None, None
    latest = max(causes, key=lambda e: e.received_date)
    return latest.event_id, latest.basis


def allocate(valuation: Valuation, as_of: Optional[date] = None, view: str = "CURRENT") -> "AllocationReport":
    """把估值记录中的逐笔现金流分配到各持有人。

    view="CURRENT"：按 as_of 已到达的全部事件分配，已支付款项保持原支付记录，
    差额以 COMPENSATION 补偿记录体现；
    view="HISTORICAL"：只承认 as_of 前已到达的事件，不生成补偿记录。
    """
    if view not in ("CURRENT", "HISTORICAL"):
        raise ValueError(f"未知对账视角: {view!r}")
    if as_of is None:
        as_of = valuation.valuation_date
    current_transfers = _active_transfers(valuation, as_of)
    current_units = _active_unit_changes(valuation, as_of)
    total_units = getattr(valuation, "total_units", 1.0)

    shares: list[HolderShare] = []
    totals: list[CashflowTotal] = []
    for seq, expl in enumerate(valuation.explanations):
        if expl.payment_state == "CANCELLED" or expl.schedule_state == "CANCELLED":
            continue
        amount = expl.amount
        cf_unit_factor = unit_factor_at(current_units, expl.payment_date)
        is_paid = expl.payment_state == "PAID" and expl.payment_date <= as_of
        if is_paid:
            # 已支付：按支付日时已到达的事件归属，历史结论不变。
            paid_transfers = _active_transfers(valuation, expl.payment_date)
            paid_units = _active_unit_changes(valuation, expl.payment_date)
            paid_alloc, paid_residual = _split(
                amount, paid_transfers, paid_units, expl.payment_date, total_units)
            for holder, part in paid_alloc.items():
                shares.append(HolderShare(
                    seq, holder, expl.index, expl.payment_date, part,
                    "PAID", "SCHEDULED_SHARE", expl.cashflow_kind,
                    expl.event_id, expl.basis,
                    unit_factor_at(paid_units, expl.payment_date)))
            residual = paid_residual
            if view == "CURRENT":
                current_alloc, residual = _split(
                    amount, current_transfers, current_units,
                    expl.payment_date, total_units)
                event_id, basis = _late_cause(valuation, expl.payment_date, as_of)
                for holder in sorted(set(paid_alloc) | set(current_alloc)):
                    delta = round(current_alloc.get(holder, 0.0)
                                  - paid_alloc.get(holder, 0.0), ROUND_DECIMALS)
                    if delta:
                        shares.append(HolderShare(
                            seq, holder, expl.index, as_of, delta,
                            "FUTURE", "COMPENSATION", expl.cashflow_kind,
                            event_id, basis, cf_unit_factor))
        else:
            alloc, residual = _split(
                amount, current_transfers, current_units,
                expl.payment_date, total_units)
            for holder, part in alloc.items():
                shares.append(HolderShare(
                    seq, holder, expl.index, expl.payment_date, part,
                    "FUTURE", "SCHEDULED_SHARE", expl.cashflow_kind,
                    expl.event_id, expl.basis, cf_unit_factor))
        totals.append(CashflowTotal(seq, expl.index, expl.payment_date,
                                    expl.cashflow_kind, amount, residual))
    return AllocationReport(
        contract_id=valuation.contract_id,
        valuation_date=valuation.valuation_date,
        as_of=as_of,
        view=view,
        shares=tuple(shares),
        cashflow_totals=tuple(totals),
    )


def holder_positions(valuation: Valuation, as_of: Optional[date] = None,
                     at_date: Optional[date] = None) -> tuple[HolderPosition, ...]:
    """as_of 知识时点下，各持有人在 at_date 单位口径下的持仓。

    返回每个持有人的单位数与对应原合约权益比例；单位拆分/合并只改变
    单位数，不改变权益比例。at_date 缺省取 as_of。
    """
    if as_of is None:
        as_of = valuation.valuation_date
    if at_date is None:
        at_date = as_of
    transfers = _active_transfers(valuation, as_of)
    unit_changes = _active_unit_changes(valuation, as_of)
    total_units = getattr(valuation, "total_units", 1.0)
    shares = _equity_shares(transfers, unit_changes, at_date, total_units)
    factor = unit_factor_at(unit_changes, at_date)
    return tuple(
        HolderPosition(holder=h,
                       units=round(s * total_units * factor, ROUND_DECIMALS),
                       equity_ratio=round(s, 12))
        for h, s in sorted(shares.items()))


@dataclass(frozen=True)
class AllocationReport:
    """一次分配对账结果：原合约视角与各持有人视角。"""

    contract_id: str
    valuation_date: date
    as_of: date
    view: str
    shares: tuple[HolderShare, ...]
    cashflow_totals: tuple[CashflowTotal, ...]

    @property
    def holders(self) -> tuple[str, ...]:
        return tuple(sorted({s.holder for s in self.shares}))

    def holder_view(self, holder: str) -> tuple[HolderShare, ...]:
        return tuple(s for s in self.shares if s.holder == holder)

    def totals_by_holder(self) -> dict:
        """各持有人已收 / 应收 / 补偿三类合计，互不覆盖。"""
        out: dict = {}
        for s in self.shares:
            bucket = out.setdefault(
                s.holder, {"received": 0.0, "receivable": 0.0, "compensation": 0.0})
            if s.kind == "COMPENSATION":
                bucket["compensation"] += s.amount
            elif s.payment_state == "PAID":
                bucket["received"] += s.amount
            else:
                bucket["receivable"] += s.amount
        return {h: {k: round(v, 6) for k, v in bucket.items()}
                for h, bucket in out.items()}

    def contract_view(self) -> tuple[dict, ...]:
        """原合约视角：逐笔总额、已分配合计与未分配舍入余量对照。"""
        rows = []
        for total in self.cashflow_totals:
            allocated = round(sum(
                s.amount for s in self.shares if s.cashflow_seq == total.cashflow_seq),
                ROUND_DECIMALS)
            rows.append({
                "cashflow_index": total.cashflow_index,
                "payment_date": total.payment_date,
                "cashflow_kind": total.cashflow_kind,
                "amount": total.amount,
                "allocated": allocated,
                "unallocated_residual": total.unallocated_residual,
                "difference": round(
                    allocated + total.unallocated_residual - total.amount,
                    ROUND_DECIMALS),
            })
        return tuple(rows)

    def check_conservation(self, tol: float = 1e-6) -> bool:
        """总额守恒：各方份额（含补偿）加未分配余量等于原金额。"""
        for row in self.contract_view():
            if abs(row["difference"]) > tol:
                raise AssertionError(
                    f"第 {row['cashflow_index']} 笔 {row['cashflow_kind']} 不守恒: "
                    f"差额 {row['difference']}")
        return True

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(
            _report_to_dict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> "AllocationReport":
        return _report_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _share_to_dict(share: HolderShare) -> dict:
    return {
        "cashflow_seq": share.cashflow_seq,
        "holder": share.holder,
        "cashflow_index": share.cashflow_index,
        "payment_date": share.payment_date.isoformat(),
        "amount": share.amount,
        "payment_state": share.payment_state,
        "kind": share.kind,
        "cashflow_kind": share.cashflow_kind,
        "event_id": share.event_id,
        "basis": share.basis,
        "unit_factor": share.unit_factor,
    }


def _share_from_dict(data: dict) -> HolderShare:
    return HolderShare(
        cashflow_seq=data["cashflow_seq"],
        holder=data["holder"],
        cashflow_index=data["cashflow_index"],
        payment_date=date.fromisoformat(data["payment_date"]),
        amount=data["amount"],
        payment_state=data["payment_state"],
        kind=data["kind"],
        cashflow_kind=data["cashflow_kind"],
        event_id=data.get("event_id"),
        basis=data.get("basis"),
        unit_factor=data.get("unit_factor", 1.0),
    )


def _report_to_dict(report: AllocationReport) -> dict:
    return {
        "contract_id": report.contract_id,
        "valuation_date": report.valuation_date.isoformat(),
        "as_of": report.as_of.isoformat(),
        "view": report.view,
        "shares": [_share_to_dict(s) for s in report.shares],
        "cashflow_totals": [
            {
                "cashflow_seq": t.cashflow_seq,
                "cashflow_index": t.cashflow_index,
                "payment_date": t.payment_date.isoformat(),
                "cashflow_kind": t.cashflow_kind,
                "amount": t.amount,
                "unallocated_residual": t.unallocated_residual,
            }
            for t in report.cashflow_totals
        ],
    }


def _report_from_dict(data: dict) -> AllocationReport:
    return AllocationReport(
        contract_id=data["contract_id"],
        valuation_date=date.fromisoformat(data["valuation_date"]),
        as_of=date.fromisoformat(data["as_of"]),
        view=data["view"],
        shares=tuple(_share_from_dict(item) for item in data["shares"]),
        cashflow_totals=tuple(CashflowTotal(
            cashflow_seq=item["cashflow_seq"],
            cashflow_index=item["cashflow_index"],
            payment_date=date.fromisoformat(item["payment_date"]),
            cashflow_kind=item["cashflow_kind"],
            amount=item["amount"],
            unallocated_residual=item.get("unallocated_residual", 0.0),
        ) for item in data["cashflow_totals"]),
    )
