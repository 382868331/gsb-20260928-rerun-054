"""由固定利率合约生成实际支付日程（含月末、顺延、残期处理）。

日程构造先得到不调整的理论周期日，再统一做营业日调整；
调整后的周期日同时作为下一段计息起息日（ISDA 常规口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Optional

from .conventions import is_month_end, shift_months, year_fraction

if TYPE_CHECKING:
    from .contract import FixedRateContract


@dataclass(frozen=True)
class Cashflow:
    index: int
    accrual_start: date
    accrual_end: date
    unadjusted_end: date
    payment_date: date
    accrual_factor: float
    interest_amount: Optional[float]
    notional_amount: float
    rate: Optional[float] = None              # 已定盘/固定利率；未定盘为 None
    fixing_date: Optional[date] = None
    rate_source: str = "FIXED_CONTRACT"       # FIXED_CONTRACT / FIXING / FORECAST
    outstanding_notional: Optional[float] = None  # 期末剩余本金（终止后）
    cancelled: bool = False                   # 本金已全额终止，本期取消
    basis: str = ""                           # 生效依据（定盘/更正/终止/预测）
    segments: tuple = ()                      # (起, 止, 本金) 计息分段

    @property
    def amount(self) -> Optional[float]:
        if self.interest_amount is None:
            return None
        return self.interest_amount + self.notional_amount


def _notional_steps(contract, as_of) -> list:
    """返回 [(生效边界日, 剩余本金, 终止事件)]，按生效日升序。"""
    terms = [
        t for t in getattr(contract, "partial_terminations", ()) or ()
        if as_of is None or t.notice_date <= as_of
    ]
    steps = []
    remaining = contract.notional
    for t in sorted(terms, key=lambda t: (t.termination_date, t.notice_date)):
        eff = contract.calendar.adjust(
            t.termination_date, contract.business_day_convention)
        remaining = max(0.0, round(remaining - t.terminated_notional, 10))
        steps.append((eff, remaining, t))
    return steps


def _period_segments(contract, steps, start, end):
    """把计息期 [start, end) 按终止生效日切成 (起, 止, 本金) 分段。

    边界规则：生效日恰好等于期初 -> 整期按剩余本金；
    恰好等于期末 -> 本期不受影响，下一期起按剩余本金。
    """
    current = contract.notional
    for eff, remaining, _ in steps:
        if eff <= start:
            current = remaining
    segments = []
    seg_start = start
    for eff, remaining, _ in steps:
        if start < eff < end:
            segments.append((seg_start, eff, current))
            seg_start = eff
            current = remaining
    segments.append((seg_start, end, current))
    return tuple(segments)


def _weighted_interest(segments, rate, day_count) -> float:
    return round(sum(
        notional * rate * year_fraction(s, e, day_count)
        for s, e, notional in segments), 6)


def _termination_basis(steps, start, end) -> str:
    # 标注期末之前（含期初已生效）的终止事件
    refs = [
        f"TERMINATION[{t.reference or t.notice_date}]@{eff}"
        for eff, _, t in steps
        if eff <= end
    ]
    return "; ".join(refs)


def _unadjusted_anchors(contract: "FixedRateContract") -> list[date]:
    """返回含起息日、不含到期日的理论周期日列表，并处理残期。"""
    start, end = contract.start_date, contract.end_date
    step = contract.frequency.months
    stub = contract.stub

    if stub.value.endswith("FIRST"):
        # 从到期日倒推
        snap = contract.eom
        anchors = [end]
        while True:
            earlier = shift_months(anchors[0], -step, snap_eom=snap)
            same_ym = earlier.year == start.year and earlier.month == start.month
            # 非月末起息：倒推到与起息日同月即进入残期范围，停止；
            # 月末起息：仅当锚点恰好等于起息日才停止（完整周期情形）。
            reached_start = same_ym and (
                not is_month_end(start) or earlier.day == start.day)
            if reached_start or earlier < start:
                break
            anchors.insert(0, earlier)
        first_anchor = anchors[0]
        if first_anchor == start:
            return anchors[:-1]  # 无残期：首锚点即起息日
        if stub.value == "LONG_FIRST":
            return [start] + anchors[1:-1]  # 合并首期与残期
        return [start] + anchors[:-1]      # SHORT_FIRST

    if stub.value.endswith("LAST"):
        snap = contract.eom
        anchors = [start]
        while True:
            later = shift_months(anchors[-1], step, snap_eom=snap)
            if later > end:
                break
            anchors.append(later)
        if anchors[-1] == end:
            return anchors[:-1]  # 无残期
        if stub.value == "LONG_LAST":
            return anchors[:-1]  # 合并末段与残期
        return anchors          # SHORT_LAST

    # NONE：必须恰好为完整周期
    regular = [start]
    while regular[-1] < end:
        regular.append(shift_months(regular[-1], step, snap_eom=contract.eom))
    if regular[-1] != end:
        raise ValueError("起息日与到期日不是完整付息周期，请指定残期类型")
    return regular[:-1]


def _build_periods(contract) -> tuple[list[date], list[date], list[date]]:
    """返回（理论锚点、每期理论止日、营业日调整后的起止边界）。"""
    anchors = _unadjusted_anchors(contract)
    end = contract.end_date
    period_ends = anchors[1:] + [end]
    boundaries = []
    cal, bdc = contract.calendar, contract.business_day_convention
    adj_start = cal.adjust(anchors[0], bdc)
    for unadj_end in period_ends:
        adj_end = cal.adjust(unadj_end, bdc)
        boundaries.append((adj_start, adj_end))
        adj_start = adj_end
    return anchors, period_ends, boundaries


def generate_schedule(
    contract: "FixedRateContract",
    as_of=None,
) -> tuple[Cashflow, ...]:
    """生成逐笔现金流；返回不可变元组，后续曲线变动不会修改它。

    as_of 之前到达的部分提前终止事件不生效（估值仍使用当时资料）。
    """
    anchors, period_ends, boundaries = _build_periods(contract)
    end = contract.end_date
    steps = _notional_steps(contract, as_of)
    cashflows = []
    for i, (unadj_end, (adj_start, adj_end)) in enumerate(
            zip(period_ends, boundaries), start=1):
        factor = year_fraction(adj_start, adj_end, contract.day_count)
        segments = _period_segments(contract, steps, adj_start, adj_end)
        outstanding = segments[-1][2]
        cancelled = segments[0][2] == 0.0
        redemption = (
            outstanding
            if contract.notional_exchange and unadj_end == end
            else 0.0
        )
        interest = 0.0 if cancelled else _weighted_interest(
            segments, contract.fixed_rate, contract.day_count)
        basis = f"CONTRACT fixed_rate={contract.fixed_rate}"
        term_basis = _termination_basis(steps, adj_start, adj_end)
        if term_basis:
            basis = f"{basis}; {term_basis}"
        cashflows.append(Cashflow(
            index=i,
            accrual_start=adj_start,
            accrual_end=adj_end,
            unadjusted_end=unadj_end,
            payment_date=adj_end,
            accrual_factor=factor,
            interest_amount=interest,
            notional_amount=redemption,
            rate=contract.fixed_rate,
            fixing_date=None,
            rate_source="FIXED_CONTRACT",
            outstanding_notional=outstanding,
            cancelled=cancelled,
            basis=basis,
            segments=segments,
        ))
    return tuple(cashflows)


def generate_floating_schedule(contract, as_of=None) -> tuple[Cashflow, ...]:
    """生成浮动利率逐笔现金流。

    已有定盘值的期间利息在日程中固定；尚未定盘期间 interest_amount 为 None，
    必须在估值时由预测曲线给出，且估值记录单独保存预测结果。
    as_of 之前到达的更正/终止事件不生效。
    """
    anchors, period_ends, boundaries = _build_periods(contract)
    end = contract.end_date
    steps = _notional_steps(contract, as_of)
    cashflows = []
    for i, (unadj_end, (adj_start, adj_end)) in enumerate(
            zip(period_ends, boundaries), start=1):
        factor = year_fraction(adj_start, adj_end, contract.day_count)
        fixing_date = contract.fixing_date(adj_start)
        segments = _period_segments(contract, steps, adj_start, adj_end)
        outstanding = segments[-1][2]
        cancelled = segments[0][2] == 0.0
        known, correction = contract.effective_fixing(fixing_date, as_of)
        if known is not None:
            rate = known + contract.spread
            interest = 0.0 if cancelled else _weighted_interest(
                segments, rate, contract.day_count)
            source = "FIXING"
            if correction is not None:
                basis = (f"CORRECTION[{correction.reference or correction.notice_date}] "
                         f"fixing {fixing_date} -> {known}")
            else:
                basis = f"FIXING {fixing_date}={known}"
        else:
            rate = None
            interest = None
            source = "FORECAST"
            basis = f"PENDING fixing {fixing_date}"
        redemption = (
            outstanding
            if contract.notional_exchange and unadj_end == end
            else 0.0
        )
        term_basis = _termination_basis(steps, adj_start, adj_end)
        if term_basis:
            basis = f"{basis}; {term_basis}"
        cashflows.append(Cashflow(
            index=i,
            accrual_start=adj_start,
            accrual_end=adj_end,
            unadjusted_end=unadj_end,
            payment_date=adj_end,
            accrual_factor=factor,
            interest_amount=interest,
            notional_amount=redemption,
            rate=rate,
            fixing_date=fixing_date,
            rate_source=source,
            outstanding_notional=outstanding,
            cancelled=cancelled,
            basis=basis,
            segments=segments,
        ))
    return tuple(cashflows)
