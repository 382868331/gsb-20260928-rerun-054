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
            fixing_value = contract.known_fixing(cf.fixing_date)
            if fixing_value is None:
                raise MissingFixingError(
                    f"{contract.contract_id} 第 {cf.index} 期定盘日 {cf.fixing_date} "
                    f"缺少已知定盘值，不能使用预测曲线替代")

        if cf.rate_source == "FORECAST":
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

        if cf.payment_date <= valuation_date:
            status = "PAID"
            df = None
            pv = 0.0
        else:
            status = "FUTURE"
            df = curve.discount_factor(cf.payment_date)
            pv = round(amount * df, 6)

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
    cashflows = generate_schedule(contract)
    return _discount_and_explain(
        contract=contract,
        cashflows=cashflows,
        curve=curve,
        forecast_curve=forecast_curve,
    )


def value_floating_contract(
    contract: FloatingRateContract,
    curve: DiscountCurve,
    forecast_curve: Optional[DiscountCurve] = None,
) -> Valuation:
    """估值浮动利率合约：历史定盘金额固定，未来定盘仅来自预测曲线。"""
    cashflows = generate_floating_schedule(contract)
    return _discount_and_explain(
        contract=contract,
        cashflows=cashflows,
        curve=curve,
        forecast_curve=forecast_curve,
    )


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
