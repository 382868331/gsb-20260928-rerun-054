"""估值与估值批次：换曲线产生新估值，绝不覆盖已生成的支付日程。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .contract import FixedRateContract
from .curve import DiscountCurve
from .schedule import Cashflow, generate_schedule


@dataclass(frozen=True)
class Valuation:
    contract_id: str
    curve_id: str
    valuation_date: date
    cashflows: tuple[Cashflow, ...]          # 日程快照
    present_values: tuple[float, ...]        # 与 cashflows 一一对应
    discount_factors: tuple[float, ...]

    @property
    def npv(self) -> float:
        return round(sum(self.present_values), 6)


def value_contract(contract: FixedRateContract, curve: DiscountCurve) -> Valuation:
    """用指定曲线估值合约；每次调用都保留独立日程快照。"""
    cashflows = generate_schedule(contract)
    dfs = tuple(curve.discount_factor(cf.payment_date) for cf in cashflows)
    pvs = tuple(round(cf.amount * df, 6) for cf, df in zip(cashflows, dfs))
    return Valuation(
        contract_id=contract.contract_id,
        curve_id=curve.curve_id,
        valuation_date=curve.valuation_date,
        cashflows=cashflows,
        present_values=pvs,
        discount_factors=dfs,
    )


class ValuationBatch:
    """一次估值批次：同一合约可在不同曲线下得到多条互不覆盖的估值记录。"""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self._records: list[Valuation] = []

    def run(self, contract: FixedRateContract, curve: DiscountCurve) -> Valuation:
        valuation = value_contract(contract, curve)
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
