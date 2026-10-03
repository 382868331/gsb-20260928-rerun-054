"""持有人分配与对账：权益转让、单位拆分/合并下各方份额、补偿记录与总额守恒。

转让不改变合约现金流总额，只改变份额归属：
- 已支付的款项仍归原支付记录（按支付日时已到达的转让/撤销计算）；
- 尚未支付的本金、利息与调整款按当前已到达事件分配到各持有人；
- 转让通知迟到或随后撤销时，历史视角保持原结论，
  当前视角以 COMPENSATION 补偿记录说明差额。

单位拆分/合并只改变计量单位口径，不改变经济权益比例与应收金额：
- 每笔现金流按支付日落在单位变化前后分别采用变化前/后的单位解释；
- 固定单位数量的转让按生效时点单位口径折算为权益比例，与固定比例
  转让分别记录（spec_kind 可追查），后续合并不会把两种约定混为一谈；
- 金额按最小可分单位（rounding_unit）舍入，未分配余量逐笔显式记录，
  不藏进任何一位持有人的份额。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .events import RightsTransfer, TransferRevocation, UnitMerge, UnitSplit
from .valuation import Valuation

ORIGINAL_HOLDER = "ORIGINAL"
DEFAULT_ROUNDING_UNIT = 1e-6


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
    units: Optional[float] = None   # 该份额按支付日单位口径对应的单位数量


@dataclass(frozen=True)
class CashflowTotal:
    """原合约视角下单笔现金流总额（分配前）。"""

    cashflow_seq: int
    cashflow_index: int
    payment_date: date
    cashflow_kind: str
    amount: float
    total_units: float = 1.0        # 支付日口径下的合约总单位数


@dataclass(frozen=True)
class CashflowResidual:
    """逐笔未分配余量：舍入归属显式记录，不摊给任何持有人。"""

    cashflow_seq: int
    cashflow_index: int
    payment_date: date
    residual: float
    rounding_unit: float


@dataclass(frozen=True)
class HolderPosition:
    """某持有人在 as_of 时点的持仓：单位数量与原合约权益比例。"""

    holder: str
    units: float
    equity_ratio: float             # 占原合约权益的比例


def _event_sort_key(event) -> tuple:
    """确定性事件排序：与接收顺序无关，同一知识集合得到同一结论。"""
    return (
        getattr(event, "received_date", date.min),
        getattr(event, "effective_date", None) or getattr(event, "fixing_date", date.min),
        getattr(event, "event_id", ""),
    )


def _ordered_events(valuation: Valuation) -> tuple:
    return tuple(sorted(valuation.events, key=_event_sort_key))


def _active_transfers(valuation: Valuation, as_of: date) -> tuple[RightsTransfer, ...]:
    """as_of 时刻已到达且未被撤销的转让，按生效日/到达日排序。"""
    transfers = {}
    revoked = set()
    for event in _ordered_events(valuation):
        if isinstance(event, RightsTransfer) and event.received_date <= as_of:
            transfers[event.event_id] = event
        elif isinstance(event, TransferRevocation) and event.received_date <= as_of:
            revoked.add(event.transfer_event_id)
    active = [t for tid, t in transfers.items() if tid not in revoked]
    active.sort(key=lambda t: (t.effective_date, t.received_date, t.event_id))
    return tuple(active)


def _active_unit_changes(valuation: Valuation, as_of: date) -> tuple:
    """as_of 时刻已到达的单位拆分/合并，按生效日/到达日排序。"""
    changes = [e for e in _ordered_events(valuation)
               if isinstance(e, (UnitSplit, UnitMerge)) and e.received_date <= as_of]
    changes.sort(key=lambda e: (e.effective_date, e.received_date, e.event_id))
    return tuple(changes)


def _total_units(changes, at_date: date, initial_units: float) -> float:
    """at_date 时点（按支付日口径）的合约总单位数。"""
    total = initial_units
    for change in changes:
        if change.effective_date <= at_date:
            if isinstance(change, UnitSplit):
                total *= change.factor
            else:
                total /= change.factor
    return total


def _transfer_ratio(transfer: RightsTransfer, changes, initial_units: float) -> float:
    """转让的权益比例：固定比例直接采用；固定单位数量按生效时点单位口径折算。"""
    if transfer.ratio is not None:
        return transfer.ratio
    total = _total_units(changes, transfer.effective_date, initial_units)
    if total <= 0:
        raise ValueError(f"转让 {transfer.event_id} 生效时合约总单位数非正")
    return transfer.units / total


def _applicable(transfers, payment_date: date):
    return [t for t in transfers
            if t.effective_date <= payment_date
            and (t.end_date is None or payment_date <= t.end_date)]


def _round_to(value: float, unit: float) -> float:
    return round(round(value / unit) * unit, 6)


def _split(amount: float, transfers, payment_date: date, changes,
           initial_units: float, rounding_unit: float) -> tuple[dict, float]:
    """按转让比例把一笔现金流分到各持有人。

    每份按 rounding_unit 独立舍入，未分配余量显式返回，
    不藏进任何一位持有人的份额。
    """
    shares = {ORIGINAL_HOLDER: 1.0}
    for t in _applicable(transfers, payment_date):
        ratio = _transfer_ratio(t, changes, initial_units)
        shares[t.from_holder] = shares.get(t.from_holder, 0.0) - ratio
        shares[t.to_holder] = shares.get(t.to_holder, 0.0) + ratio
    for holder, share in shares.items():
        if share < -1e-9:
            raise ValueError(f"持有人 {holder} 累计分出超过原权益")
    items = [(h, s) for h, s in shares.items() if abs(s) > 1e-12]
    amounts = {h: _round_to(amount * s, rounding_unit) for h, s in items}
    residual = round(amount - sum(amounts.values()), 6)
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
    for event in _ordered_events(valuation):
        if (isinstance(event, (UnitSplit, UnitMerge))
                and event.effective_date <= payment_date
                and payment_date < event.received_date <= as_of):
            causes.append(event)
    for event in valuation.events:
        if isinstance(event, TransferRevocation) and payment_date < event.received_date <= as_of:
            target = transfers.get(event.transfer_event_id)
            if target is not None and covers(target):
                causes.append(event)
    if not causes:
        return None, None
    latest = max(causes, key=lambda e: (e.received_date, e.event_id))
    return latest.event_id, latest.basis


def allocate(
    valuation: Valuation,
    as_of: Optional[date] = None,
    view: str = "CURRENT",
    initial_units: float = 1.0,
    rounding_unit: float = DEFAULT_ROUNDING_UNIT,
) -> "AllocationReport":
    """把估值记录中的逐笔现金流分配到各持有人。

    view="CURRENT"：按 as_of 已到达的全部事件分配，已支付款项保持原支付记录，
    差额以 COMPENSATION 补偿记录体现；
    view="HISTORICAL"：只承认 as_of 前已到达的事件，不生成补偿记录。
    initial_units 为合约初始单位数；rounding_unit 为金额最小可分单位，
    舍入余量逐笔显式记录（CashflowResidual），不摊给任何持有人。
    """
    if view not in ("CURRENT", "HISTORICAL"):
        raise ValueError(f"未知对账视角: {view!r}")
    if initial_units <= 0:
        raise ValueError("初始单位数必须为正")
    if rounding_unit <= 0:
        raise ValueError("最小可分单位必须为正")
    if as_of is None:
        as_of = valuation.valuation_date
    current_transfers = _active_transfers(valuation, as_of)
    current_changes = _active_unit_changes(valuation, as_of)

    shares: list[HolderShare] = []
    totals: list[CashflowTotal] = []
    residuals: list[CashflowResidual] = []
    for seq, expl in enumerate(valuation.explanations):
        if expl.payment_state == "CANCELLED" or expl.schedule_state == "CANCELLED":
            continue
        amount = expl.amount
        total_units = _total_units(current_changes, expl.payment_date, initial_units)
        totals.append(CashflowTotal(seq, expl.index, expl.payment_date,
                                    expl.cashflow_kind, amount, total_units))

        def _units_for(part: float) -> Optional[float]:
            if amount == 0:
                return None
            return round(part / amount * total_units, 6)

        is_paid = expl.payment_state == "PAID" and expl.payment_date <= as_of
        if is_paid:
            # 已支付：按支付日时已到达的事件归属，历史结论不变。
            paid_transfers = _active_transfers(valuation, expl.payment_date)
            paid_changes = _active_unit_changes(valuation, expl.payment_date)
            paid_alloc, paid_residual = _split(
                amount, paid_transfers, expl.payment_date,
                paid_changes, initial_units, rounding_unit)
            for holder, part in paid_alloc.items():
                shares.append(HolderShare(
                    seq, holder, expl.index, expl.payment_date, part,
                    "PAID", "SCHEDULED_SHARE", expl.cashflow_kind,
                    expl.event_id, expl.basis, _units_for(part)))
            residual = paid_residual
            if view == "CURRENT":
                current_alloc, _ = _split(
                    amount, current_transfers, expl.payment_date,
                    current_changes, initial_units, rounding_unit)
                event_id, basis = _late_cause(valuation, expl.payment_date, as_of)
                compensation_sum = 0.0
                for holder in sorted(set(paid_alloc) | set(current_alloc)):
                    delta = round(current_alloc.get(holder, 0.0)
                                  - paid_alloc.get(holder, 0.0), 6)
                    if delta:
                        compensation_sum += delta
                        shares.append(HolderShare(
                            seq, holder, expl.index, as_of, delta,
                            "FUTURE", "COMPENSATION", expl.cashflow_kind,
                            event_id, basis, _units_for(delta)))
                residual = round(amount
                                 - sum(paid_alloc.values())
                                 - compensation_sum, 6)
        else:
            alloc, residual = _split(
                amount, current_transfers, expl.payment_date,
                current_changes, initial_units, rounding_unit)
            for holder, part in alloc.items():
                shares.append(HolderShare(
                    seq, holder, expl.index, expl.payment_date, part,
                    "FUTURE", "SCHEDULED_SHARE", expl.cashflow_kind,
                    expl.event_id, expl.basis, _units_for(part)))
        residuals.append(CashflowResidual(
            seq, expl.index, expl.payment_date, residual, rounding_unit))

    positions = _positions(
        valuation, as_of, current_transfers, current_changes, initial_units)
    return AllocationReport(
        contract_id=valuation.contract_id,
        valuation_date=valuation.valuation_date,
        as_of=as_of,
        view=view,
        shares=tuple(shares),
        cashflow_totals=tuple(totals),
        residuals=tuple(residuals),
        positions=positions,
        initial_units=initial_units,
        rounding_unit=rounding_unit,
    )


def _positions(valuation, as_of, transfers, changes, initial_units) -> tuple[HolderPosition, ...]:
    """as_of 时点各持有人持仓：单位数量与占原合约权益的比例。"""
    ratios = {ORIGINAL_HOLDER: 1.0}
    for t in transfers:
        if t.effective_date > as_of:
            continue
        ratio = _transfer_ratio(t, changes, initial_units)
        ratios[t.from_holder] = ratios.get(t.from_holder, 0.0) - ratio
        ratios[t.to_holder] = ratios.get(t.to_holder, 0.0) + ratio
    total = _total_units(changes, as_of, initial_units)
    return tuple(
        HolderPosition(holder=h, units=round(r * total, 6), equity_ratio=round(r, 12))
        for h, r in sorted(ratios.items()) if abs(r) > 1e-12
    )


@dataclass(frozen=True)
class AllocationReport:
    """一次分配对账结果：原合约视角与各持有人视角。"""

    contract_id: str
    valuation_date: date
    as_of: date
    view: str
    shares: tuple[HolderShare, ...]
    cashflow_totals: tuple[CashflowTotal, ...]
    residuals: tuple[CashflowResidual, ...] = ()
    positions: tuple[HolderPosition, ...] = ()
    initial_units: float = 1.0
    rounding_unit: float = DEFAULT_ROUNDING_UNIT

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
        """原合约视角：逐笔总额、已分配合计与未分配余量对照。"""
        residual_by_seq = {r.cashflow_seq: r for r in self.residuals}
        rows = []
        for total in self.cashflow_totals:
            allocated = round(sum(
                s.amount for s in self.shares if s.cashflow_seq == total.cashflow_seq), 6)
            recon = residual_by_seq.get(total.cashflow_seq)
            residual = recon.residual if recon else round(total.amount - allocated, 6)
            rows.append({
                "cashflow_index": total.cashflow_index,
                "payment_date": total.payment_date,
                "cashflow_kind": total.cashflow_kind,
                "amount": total.amount,
                "total_units": total.total_units,
                "allocated": allocated,
                "residual": residual,
                "rounding_unit": self.rounding_unit,
                "difference": round(allocated + residual - total.amount, 6),
            })
        return tuple(rows)

    def check_conservation(self, tol: float = 1e-6) -> bool:
        """总额守恒：每笔现金流的各方份额（含补偿）加未分配余量等于原金额。"""
        for row in self.contract_view():
            if abs(row["difference"]) > tol:
                raise AssertionError(
                    f"第 {row['cashflow_index']} 笔 {row['cashflow_kind']} 不守恒: "
                    f"差额 {row['difference']}")
        return True

    def position_of(self, holder: str) -> HolderPosition:
        for pos in self.positions:
            if pos.holder == holder:
                return pos
        raise KeyError(f"对账结果中不存在持有人 {holder!r} 的持仓")

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
        "units": share.units,
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
        units=data.get("units"),
    )


def _residual_to_dict(residual: CashflowResidual) -> dict:
    return {
        "cashflow_seq": residual.cashflow_seq,
        "cashflow_index": residual.cashflow_index,
        "payment_date": residual.payment_date.isoformat(),
        "residual": residual.residual,
        "rounding_unit": residual.rounding_unit,
    }


def _residual_from_dict(data: dict) -> CashflowResidual:
    return CashflowResidual(
        cashflow_seq=data["cashflow_seq"],
        cashflow_index=data["cashflow_index"],
        payment_date=date.fromisoformat(data["payment_date"]),
        residual=data["residual"],
        rounding_unit=data["rounding_unit"],
    )


def _position_to_dict(position: HolderPosition) -> dict:
    return {
        "holder": position.holder,
        "units": position.units,
        "equity_ratio": position.equity_ratio,
    }


def _position_from_dict(data: dict) -> HolderPosition:
    return HolderPosition(
        holder=data["holder"],
        units=data["units"],
        equity_ratio=data["equity_ratio"],
    )


def _report_to_dict(report: AllocationReport) -> dict:
    return {
        "contract_id": report.contract_id,
        "valuation_date": report.valuation_date.isoformat(),
        "as_of": report.as_of.isoformat(),
        "view": report.view,
        "shares": [_share_to_dict(s) for s in report.shares],
        "residuals": [_residual_to_dict(r) for r in report.residuals],
        "positions": [_position_to_dict(p) for p in report.positions],
        "initial_units": report.initial_units,
        "rounding_unit": report.rounding_unit,
        "cashflow_totals": [
            {
                "cashflow_seq": t.cashflow_seq,
                "cashflow_index": t.cashflow_index,
                "payment_date": t.payment_date.isoformat(),
                "cashflow_kind": t.cashflow_kind,
                "amount": t.amount,
                "total_units": t.total_units,
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
        residuals=tuple(_residual_from_dict(item)
                        for item in data.get("residuals", ())),
        positions=tuple(_position_from_dict(item)
                        for item in data.get("positions", ())),
        initial_units=data.get("initial_units", 1.0),
        rounding_unit=data.get("rounding_unit", DEFAULT_ROUNDING_UNIT),
        cashflow_totals=tuple(CashflowTotal(
            cashflow_seq=item["cashflow_seq"],
            cashflow_index=item["cashflow_index"],
            payment_date=date.fromisoformat(item["payment_date"]),
            cashflow_kind=item["cashflow_kind"],
            amount=item["amount"],
            total_units=item.get("total_units", 1.0),
        ) for item in data["cashflow_totals"]),
    )
