"""历史定盘更正与部分提前终止事件。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Tuple


@dataclass(frozen=True)
class FixingCorrection:
    event_id: str
    fixing_date: date
    old_value: float
    new_value: float
    received_date: date
    settlement_date: date
    basis: str

    def __post_init__(self) -> None:
        if self.new_value == self.old_value:
            raise ValueError("更正后的定盘值不能与原值相同")
        if self.settlement_date < self.received_date:
            raise ValueError("更正款结算日不能早于更正到达日")


@dataclass(frozen=True)
class PartialTermination:
    event_id: str
    effective_date: date
    reduction_amount: float
    received_date: date
    basis: str
    settlement_date: date | None = None

    def __post_init__(self) -> None:
        if self.reduction_amount <= 0:
            raise ValueError("部分终止本金金额必须为正")
        if self.settlement_date is None:
            object.__setattr__(self, "settlement_date", self.effective_date)
        if self.settlement_date < self.effective_date:
            raise ValueError("部分终止付款日不能早于生效日")


@dataclass(frozen=True)
class FutureInterestAssignment:
    """未来权益转让。proportion 为转让方在适用范围内让出的原权益比例。"""

    event_id: str
    transferor: str
    transferee: str
    proportion: float
    effective_date: date
    received_date: date
    basis: str
    cashflow_scope: Tuple[str, ...] = ("INTEREST", "PRINCIPAL", "ADJUSTMENT")

    def __post_init__(self) -> None:
        if not 0.0 < self.proportion <= 1.0:
            raise ValueError("转让比例必须位于 (0, 1] 区间")
        if self.transferor == self.transferee:
            raise ValueError("转让双方不能相同")
        if not self.cashflow_scope:
            raise ValueError("适用现金流范围不能为空")
        allowed = {"INTEREST", "PRINCIPAL", "ADJUSTMENT", "ALL"}
        scope = tuple(dict.fromkeys(self.cashflow_scope))
        if any(item not in allowed for item in scope):
            raise ValueError(f"未知现金流范围，支持: {sorted(allowed)}")
        object.__setattr__(self, "cashflow_scope", scope)

    @property
    def normalized_scope(self) -> frozenset[str]:
        if "ALL" in self.cashflow_scope:
            return frozenset({"INTEREST", "PRINCIPAL", "ADJUSTMENT"})
        return frozenset(self.cashflow_scope)


@dataclass(frozen=True)
class AssignmentRevocation:
    """转让通知撤销；已发生的收款记录不回写，只形成对账补偿。"""

    event_id: str
    assignment_id: str
    received_date: date
    basis: str
    effective_date: date | None = None
