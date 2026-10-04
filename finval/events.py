"""历史事件：定盘更正、部分提前终止、权益转让/撤销、单位拆并与零碎权益选择。"""

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
    effective_date: date
    received_date: date
    basis: str
    ratio: Optional[float] = None
    units: Optional[float] = None
    from_holder: str = "ORIGINAL"
    end_date: Optional[date] = None   # 适用现金流终点（含），None 表示至到期

    def __post_init__(self) -> None:
        if (self.ratio is None) == (self.units is None):
            raise ValueError("ratio 与 units 必须且只能给出一个")
        if self.ratio is not None and not 0 < self.ratio <= 1:
            raise ValueError("转让比例必须在 (0, 1] 区间")
        if self.units is not None and self.units <= 0:
            raise ValueError("转让单位数量必须为正")
        if self.end_date is not None and self.end_date < self.effective_date:
            raise ValueError("转让适用终点不能早于起点")
        if self.to_holder == self.from_holder:
            raise ValueError("转让双方不能是同一持有人")

    @property
    def kind(self) -> str:
        return "RATIO" if self.ratio is not None else "UNITS"


@dataclass(frozen=True)
class TransferRevocation:
    """撤销已达成的转让：撤销到达前的历史结论不变，当前视角回补差额。"""

    event_id: str
    transfer_event_id: str
    received_date: date
    basis: str


@dataclass(frozen=True)
class UnitChange:
    """单位拆分或合并：每 1 单位变为 factor 单位，经济权益总量不变。

    factor > 1 为拆分（如 1 拆 10），0 < factor < 1 为合并（如 5 并 1
    即 factor=0.2）。effective_date 为单位口径切换日；received_date 为
    收到通知的时刻，迟到通知只改变当前解释，历史视角保持当时结论。
    """

    event_id: str
    effective_date: date
    factor: float
    received_date: date
    basis: str
    election_deadline: Optional[date] = None   # 零碎权益选择截止日，缺省为生效日

    def __post_init__(self) -> None:
        if self.factor <= 0:
            raise ValueError("单位变化系数必须为正")
        if self.factor == 1:
            raise ValueError("单位变化系数不能为 1（无实际变化）")
        if self.election_deadline is None:
            object.__setattr__(self, "election_deadline", self.effective_date)

    @property
    def kind(self) -> str:
        return "SPLIT" if self.factor > 1 else "MERGE"


@dataclass(frozen=True)
class FractionElection:
    """持有人对某次单位拆并产生的零碎份额作出选择。

    choice:
    - KEEP：保留可累积的零碎单位（默认，无需提交选择）；
    - CASH：零碎单位以现金替代，价格由 CashPriceFixing 确定，
      价格未知期间为待定现金权益；
    - TRANSFER：把零碎单位转给另一持有人（to_holder 必填）。

    选择须在该次拆并的 election_deadline（含）前到达才生效，
    逾期或未提交一律按 KEEP 处理。
    """

    event_id: str
    holder: str
    unit_change_event_id: str
    choice: str
    received_date: date
    basis: str
    to_holder: Optional[str] = None

    def __post_init__(self) -> None:
        if self.choice not in ("KEEP", "CASH", "TRANSFER"):
            raise ValueError(f"未知零碎权益选择: {self.choice!r}")
        if self.choice == "TRANSFER":
            if self.to_holder is None:
                raise ValueError("TRANSFER 选择必须给出 to_holder")
            if self.to_holder == self.holder:
                raise ValueError("零碎份额不能转给本人")
        elif self.to_holder is not None:
            raise ValueError("只有 TRANSFER 选择可以给出 to_holder")


@dataclass(frozen=True)
class ElectionRevocation:
    """撤回已达成的零碎权益选择；fraction 给出撤回比例（部分撤回）。

    撤回到达时若原选择尚未执行完毕（现金未定价、转让未生效），
    直接按撤回后的比例处理；若已执行（现金已定价或零碎单位已转出），
    原记录保持不变，另以可追踪的调整记录回补。
    """

    event_id: str
    election_event_id: str
    received_date: date
    basis: str
    fraction: float = 1.0        # 撤回比例，(0, 1]

    def __post_init__(self) -> None:
        if not 0 < self.fraction <= 1:
            raise ValueError("撤回比例必须在 (0, 1] 区间")


@dataclass(frozen=True)
class CashPriceFixing:
    """某次单位拆并的现金替代定价。

    同一 unit_change_event_id 的第二次及以后定价视为更正：
    已定价的现金记录保持原值，差额以 ADJUSTMENT 调整记录体现。
    """

    event_id: str
    unit_change_event_id: str
    price_per_unit: float
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if self.price_per_unit <= 0:
            raise ValueError("现金替代价格必须为正")
