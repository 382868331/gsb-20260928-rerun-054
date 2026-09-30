"""估值与估值批次：换曲线产生新估值，绝不覆盖已生成的支付日程。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .contract import FixedRateContract
from .curve import DiscountCurve
from .explanation import RateExplanation
from .floating import FloatingRateContract, generate_floating_schedule
from .schedule import Cashflow, generate_schedule


@dataclass(frozen=True)
class Valuation:
    contract_id: str
    curve_id: str
    valuation_date: date
    cashflows: tuple[Cashflow, ...]          # 日程快照
    present_values: tuple[float, ...]        # 与 cashflows 一一对应
    discount_factors: tuple[float, ...]
    forecast_curve_id: str | None = None
    rate_explanations: tuple[RateExplanation, ...] = ()

    @property
    def npv(self) -> float:
        return round(sum(self.present_values), 6)


def value_contract(contract: FixedRateContract, curve: DiscountCurve) -> Valuation:
    """用指定曲线估值合约；每次调用都保留独立日程快照。"""
    cashflows = generate_schedule(contract)
    return _assemble_valuation(contract, cashflows, curve, None)


def value_floating_contract(
    contract: FloatingRateContract,
    discount_curve: DiscountCurve,
    forecast_curve: DiscountCurve | None = None,
) -> Valuation:
    """估值浮动利率合约；历史定盘现金流的金额不随预测曲线变化。"""
    cashflows = generate_floating_schedule(
        contract, discount_curve.valuation_date, forecast_curve)
    return _assemble_valuation(
        contract, cashflows, discount_curve, forecast_curve)


def _assemble_valuation(contract, cashflows, discount_curve, forecast_curve):
    valuation_date = discount_curve.valuation_date
    spread = getattr(contract, "spread", 0.0)
    dfs = []
    pvs = []
    explanations = []
    for cf in cashflows:
        if cf.payment_date < valuation_date:
            # 估值日前已支付：保留历史金额，但现值为 0，不贴现
            dfs.append(None)
            pvs.append(0.0)
            status = "SETTLED"
        else:
            df = discount_curve.discount_factor(cf.payment_date)
            dfs.append(df)
            pvs.append(round(cf.amount * df, 6))
            status = "EXPECTED"
        explanations.append(RateExplanation(
            cashflow_index=cf.index,
            rate_source=cf.rate_source,
            fixing_date=cf.fixing_date,
            reference_rate=cf.reference_rate,
            spread=spread,
            all_in_rate=(
                cf.reference_rate + spread
                if cf.reference_rate is not None else None),
            accrual_start=cf.accrual_start,
            accrual_end=cf.accrual_end,
            forecast_curve_id=(
                forecast_curve.curve_id
                if cf.rate_source == "FORECAST" and forecast_curve is not None
                else None),
            status=status,
        ))
    return Valuation(
        contract_id=contract.contract_id,
        curve_id=discount_curve.curve_id,
        valuation_date=valuation_date,
        cashflows=cashflows,
        present_values=tuple(pvs),
        discount_factors=tuple(dfs),
        forecast_curve_id=(
            forecast_curve.curve_id if forecast_curve is not None else None),
        rate_explanations=tuple(explanations),
    )


class ValuationBatch:
    """一次估值批次：同一合约可在不同曲线下得到多条互不覆盖的估值记录。"""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self._records: list[Valuation] = []

    def run(
        self,
        contract,
        discount_curve: DiscountCurve,
        forecast_curve: DiscountCurve | None = None,
    ) -> Valuation:
        if isinstance(contract, FloatingRateContract):
            valuation = value_floating_contract(
                contract, discount_curve, forecast_curve)
        else:
            valuation = value_contract(contract, discount_curve)
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
