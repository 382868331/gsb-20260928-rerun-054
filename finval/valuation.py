"""估值与估值批次：换曲线产生新估值，绝不覆盖已生成的支付日程。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .contract import FixedRateContract
from .curve import DiscountCurve
from .floating import FloatingRateContract, MissingFixingError
from .schedule import Cashflow, generate_floating_schedule, generate_schedule


@dataclass(frozen=True)
class CashflowExplanation:
    """单笔现金流的利率来源与估值口径解释。"""

    index: int
    status: str                       # FUTURE / PAID
    rate_source: str                  # FIXED_CONTRACT / FIXING / FORECAST
    rate: Optional[float]
    spread: float
    fixing_date: Optional[date]
    fixing_value: Optional[float]
    forecast_curve_id: Optional[str]
    forecast_start: Optional[date]
    forecast_end: Optional[date]
    interest_amount: Optional[float]
    notional_amount: float
    amount: float
    discount_factor: Optional[float]
    present_value: float
    payment_state: str = ""           # FIXED / PENDING / PAID / CANCELLED / ADJUSTED
    basis: str = ""                   # 生效依据，可追溯到定盘/更正/终止事件
    outstanding_notional: Optional[float] = None


@dataclass(frozen=True)
class Valuation:
    contract_id: str
    curve_id: str
    valuation_date: date
    cashflows: tuple[Cashflow, ...]              # 日程快照
    present_values: tuple[float, ...]            # 与 cashflows 一一对应
    discount_factors: tuple[Optional[float], ...]
    explanations: tuple[CashflowExplanation, ...] = ()
    forecast_curve_id: Optional[str] = None

    @property
    def npv(self) -> float:
        return round(sum(self.present_values), 6)


def _discount_and_explain(
    *,
    contract,
    cashflows: tuple[Cashflow, ...],
    curve: DiscountCurve,
    forecast_curve: Optional[DiscountCurve],
) -> Valuation:
    valuation_date = curve.valuation_date
    dfs: list[Optional[float]] = []
    pvs: list[float] = []
    explanations: list[CashflowExplanation] = []

    for cf in cashflows:
        spread = getattr(contract, "spread", 0.0)
        rate = cf.rate
        fixing_value = None
        forecast_id = None
        forecast_start = None
        forecast_end = None
        interest = cf.interest_amount

        if cf.rate_source == "FIXING":
            # 已支付的期间冻结在支付日实际使用的定盘值；
            # 支付日之后才到达的更正不改动已支付金额，差额由调整款项体现。
            fix_as_of = min(valuation_date, cf.payment_date)
            fixing_value, _ = contract.effective_fixing(cf.fixing_date, fix_as_of)
            if fixing_value is None:
                raise MissingFixingError(
                    f"{contract.contract_id} 第 {cf.index} 期定盘日 {cf.fixing_date} "
                    f"缺少已知定盘值，不能使用预测曲线替代")
            if cf.payment_date <= valuation_date and not cf.cancelled:
                from .schedule import _weighted_interest

                rate = fixing_value + spread
                interest = _weighted_interest(
                    cf.segments, rate, contract.day_count)

        if cf.cancelled and interest is None:
            interest = 0.0

        if cf.rate_source == "FORECAST" and not cf.cancelled:
            # 定盘日已到/已过却没有历史值：明确报错，绝不静默预测。
            if cf.fixing_date <= valuation_date:
                raise MissingFixingError(
                    f"{contract.contract_id} 第 {cf.index} 期定盘日 {cf.fixing_date} "
                    f"不晚于估值日 {valuation_date}，但缺少定盘值")
            if forecast_curve is None:
                raise MissingFixingError(
                    f"{contract.contract_id} 第 {cf.index} 期尚未定盘，必须提供预测曲线")
            forward = forecast_curve.forward_rate(
                cf.accrual_start, cf.accrual_end, contract.day_count)
            rate = forward + spread
            interest = round(contract.notional * rate * cf.accrual_factor, 6)
            forecast_id = forecast_curve.curve_id
            forecast_start = cf.accrual_start
            forecast_end = cf.accrual_end

        amount = interest + cf.notional_amount

        if cf.cancelled:
            status = "CANCELLED"
            df = None
            pv = 0.0
        elif cf.payment_date <= valuation_date:
            status = "PAID"
            df = None
            pv = 0.0
        else:
            status = "FUTURE"
            df = curve.discount_factor(cf.payment_date)
            pv = round(amount * df, 6)

        if cf.cancelled:
            payment_state = "CANCELLED"
        elif cf.rate_source == "ADJUSTMENT":
            payment_state = "ADJUSTED"
        elif status == "PAID":
            payment_state = "PAID"
        elif cf.rate_source in ("FIXED_CONTRACT", "FIXING"):
            payment_state = "FIXED"
        else:
            payment_state = "PENDING"

        dfs.append(df)
        pvs.append(pv)
        explanations.append(CashflowExplanation(
            index=cf.index,
            status=status,
            rate_source=cf.rate_source,
            rate=rate,
            spread=spread,
            fixing_date=cf.fixing_date,
            fixing_value=fixing_value,
            forecast_curve_id=forecast_id,
            forecast_start=forecast_start,
            forecast_end=forecast_end,
            interest_amount=interest,
            notional_amount=cf.notional_amount,
            amount=amount,
            discount_factor=df,
            present_value=pv,
            payment_state=payment_state,
            basis=cf.basis,
            outstanding_notional=cf.outstanding_notional,
        ))

    return Valuation(
        contract_id=contract.contract_id,
        curve_id=curve.curve_id,
        valuation_date=valuation_date,
        cashflows=cashflows,
        present_values=tuple(pvs),
        discount_factors=tuple(dfs),
        explanations=tuple(explanations),
        forecast_curve_id=forecast_curve.curve_id if forecast_curve else None,
    )


def value_contract(
    contract: FixedRateContract,
    curve: DiscountCurve,
    forecast_curve: Optional[DiscountCurve] = None,
) -> Valuation:
    """用指定曲线估值合约；每次调用都保留独立日程快照。"""
    cashflows = generate_schedule(contract, as_of=curve.valuation_date)
    return _discount_and_explain(
        contract=contract,
        cashflows=cashflows,
        curve=curve,
        forecast_curve=forecast_curve,
    )


def _correction_adjustments(
    contract: FloatingRateContract,
    cashflows: tuple[Cashflow, ...],
    valuation_date: date,
) -> tuple[Cashflow, ...]:
    """为“支付日后才到达”的定盘更正生成调整款项。

    已支付金额保持原样，绝不重算成另一笔实际支付；
    差额作为独立的 ADJUSTMENT 现金流，在下一个未付支付日结算。
    """
    from .schedule import _weighted_interest

    extra = []
    future_payments = sorted(
        {cf.payment_date for cf in cashflows if cf.payment_date > valuation_date})
    for cf in cashflows:
        if cf.rate_source != "FIXING" or cf.payment_date > valuation_date:
            continue
        paid_value, _ = contract.effective_fixing(
            cf.fixing_date, as_of=cf.payment_date)
        now_value, correction = contract.effective_fixing(
            cf.fixing_date, as_of=valuation_date)
        if correction is None or paid_value == now_value:
            continue
        paid_interest = _weighted_interest(
            cf.segments, paid_value + contract.spread, contract.day_count)
        diff = round(cf.interest_amount - paid_interest, 6)
        if diff == 0:
            continue
        if future_payments:
            pay_date = future_payments[0]
        else:
            pay_date = contract.calendar.adjust(
                correction.notice_date, contract.business_day_convention)
        ref = correction.reference or correction.notice_date
        extra.append(Cashflow(
            index=1000 + cf.index,
            accrual_start=cf.accrual_start,
            accrual_end=cf.accrual_end,
            unadjusted_end=cf.unadjusted_end,
            payment_date=pay_date,
            accrual_factor=cf.accrual_factor,
            interest_amount=diff,
            notional_amount=0.0,
            rate=None,
            fixing_date=cf.fixing_date,
            rate_source="ADJUSTMENT",
            outstanding_notional=cf.outstanding_notional,
            cancelled=False,
            basis=(f"ADJUSTMENT for CORRECTION[{ref}] fixing {cf.fixing_date}: "
                   f"paid {paid_value} -> corrected {now_value}"),
            segments=cf.segments,
        ))
    return tuple(extra)


def value_floating_contract(
    contract: FloatingRateContract,
    curve: DiscountCurve,
    forecast_curve: Optional[DiscountCurve] = None,
) -> Valuation:
    """估值浮动利率合约：历史定盘金额固定，未来定盘仅来自预测曲线。"""
    cashflows = generate_floating_schedule(contract, as_of=curve.valuation_date)
    cashflows = cashflows + _correction_adjustments(
        contract, cashflows, curve.valuation_date)
    return _discount_and_explain(
        contract=contract,
        cashflows=cashflows,
        curve=curve,
        forecast_curve=forecast_curve,
    )


@dataclass(frozen=True)
class ScheduleDiff:
    """两次估值间单笔现金流的差异，可追溯到生效依据。"""

    index: int
    change: str          # UNCHANGED / CORRECTED / REDUCED / CANCELLED /
                         # ADJUSTMENT_ADDED / AMOUNT_CHANGED
    before_amount: Optional[float]
    after_amount: Optional[float]
    basis: str


def compare_schedules(before: Valuation, after: Valuation) -> tuple[ScheduleDiff, ...]:
    """对照更正/终止前后的日程快照，逐笔给出差异与依据。"""
    before_map = {e.index: e for e in before.explanations}
    diffs = []
    for e in after.explanations:
        b = before_map.get(e.index)
        if b is None:
            diffs.append(ScheduleDiff(
                index=e.index, change="ADJUSTMENT_ADDED",
                before_amount=None, after_amount=e.amount, basis=e.basis))
            continue
        if e.payment_state == "CANCELLED" and b.payment_state != "CANCELLED":
            change = "CANCELLED"
        elif e.amount != b.amount and "CORRECTION" in e.basis:
            change = "CORRECTED"
        elif (e.outstanding_notional is not None
              and e.outstanding_notional != b.outstanding_notional):
            change = "REDUCED"
        elif e.amount != b.amount:
            change = "AMOUNT_CHANGED"
        else:
            change = "UNCHANGED"
        diffs.append(ScheduleDiff(
            index=e.index, change=change,
            before_amount=b.amount, after_amount=e.amount, basis=e.basis))
    return tuple(diffs)


class ValuationBatch:
    """一次估值批次：同一合约可在不同曲线下得到多条互不覆盖的估值记录。"""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self._records: list[Valuation] = []

    def run(
        self,
        contract,
        curve: DiscountCurve,
        forecast_curve: Optional[DiscountCurve] = None,
    ) -> Valuation:
        if isinstance(contract, FloatingRateContract):
            valuation = value_floating_contract(contract, curve, forecast_curve)
        else:
            valuation = value_contract(contract, curve, forecast_curve)
        self._records.append(valuation)
        return valuation

    @property
    def records(self) -> tuple[Valuation, ...]:
        return tuple(self._records)

    def get(self, contract_id: str, curve_id: str) -> Valuation:
        for v in self._records:
            if v.contract_id == contract_id and v.curve_id == curve_id:
                return v
        raise KeyError(f"批次中不存在 {contract_id} @ {curve_id}")

    def save(self, path) -> None:
        """把批次（含全部估值记录与日程快照）保存为 JSON。"""
        from .persistence import save_batch

        save_batch(self, path)

    @classmethod
    def load(cls, path) -> "ValuationBatch":
        """从 JSON 恢复批次；旧日程快照与历史结论保持可查，可继续估值。"""
        from .persistence import load_batch

        return load_batch(path)
