"""历史事件：定盘更正、部分提前终止、未来权益转让与撤销。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


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
class RightsTransfer:
    """未来权益转让：把适用范围内现金流的一定比例转给另一持有人。

    ratio 为占原权益的比例；effective_date/end_date 按支付日界定适用范围；
    received_date 为收到转让通知的时刻，晚于支付日时历史结论不变，
    差额以补偿记录体现。
    """

    event_id: str
    to_holder: str
    ratio: float
    effective_date: date
    received_date: date
    basis: str
    from_holder: str = "ORIGINAL"
    end_date: Optional[date] = None   # 适用现金流终点（含），None 表示至到期

    def __post_init__(self) -> None:
        if not 0 < self.ratio <= 1:
            raise ValueError("转让比例必须在 (0, 1] 区间")
        if self.end_date is not None and self.end_date < self.effective_date:
            raise ValueError("转让适用终点不能早于起点")
        if self.to_holder == self.from_holder:
            raise ValueError("转让双方不能是同一持有人")


@dataclass(frozen=True)
class TransferRevocation:
    """撤销已达成的转让：撤销到达前的历史结论不变，当前视角回补差额。"""

    event_id: str
    transfer_event_id: str
    received_date: date
    basis: str
