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

    def __post_init__(self) -> None:
        if self.factor <= 0:
            raise ValueError("单位变化系数必须为正")
        if self.factor == 1:
            raise ValueError("单位变化系数不能为 1（无实际变化）")

    @property
    def kind(self) -> str:
        return "SPLIT" if self.factor > 1 else "MERGE"


@dataclass(frozen=True)
class FractionElection:
    """零碎权益选择：单位拆并产生零碎单位时的处理方式。

    method: RETAIN 保留可累积 / CASH 现金替代 / TRANSFER 转给另一持有人。
    effective_date 为选择截止日：单位变化生效日不晚于截止日（含同日）时
    该选择生效；received_date 为收到选择的时刻。未作选择时默认 RETAIN。
    """

    event_id: str
    holder: str
    unit_change_event_id: str
    method: str
    effective_date: date
    received_date: date
    basis: str
    to_holder: Optional[str] = None

    def __post_init__(self) -> None:
        if self.method not in ("RETAIN", "CASH", "TRANSFER"):
            raise ValueError(f"未知零碎权益处理方式: {self.method!r}")
        if self.method == "TRANSFER":
            if self.to_holder is None:
                raise ValueError("TRANSFER 选择必须给出 to_holder")
            if self.to_holder == self.holder:
                raise ValueError("零碎权益不能转给自己")
        elif self.to_holder is not None:
            raise ValueError("非 TRANSFER 选择不能给出 to_holder")


@dataclass(frozen=True)
class ElectionRevocation:
    """撤回已达成的零碎权益选择（可部分撤回）。

    portion in (0,1] 为撤回比例，被撤回部分恢复为保留累积；
    已完成的现金分配不改写，差额以 REVOCATION_ADJUSTMENT 调整记录体现。
    """

    event_id: str
    election_event_id: str
    portion: float
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if not 0 < self.portion <= 1:
            raise ValueError("撤回比例必须在 (0, 1] 区间")


@dataclass(frozen=True)
class CashPriceFixing:
    """现金替代价格：可晚于份额变化确定；更正只产生新定价事件。

    corrects_event_id 指向被更正的定价事件；更正在分配台账中体现为
    PRICE_ADJUSTMENT 调整记录，原 CASH_IN_LIEU 记录保持不变。
    """

    event_id: str
    unit_change_event_id: str
    price_per_unit: float
    received_date: date
    basis: str
    corrects_event_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.price_per_unit <= 0:
            raise ValueError("现金替代价格必须为正")


@dataclass(frozen=True)
class AggregationBatch:
    """零碎权益汇总处置批次：同一证券多位持有人的零碎份额统一处理。

    residual_rule 为无法均分最小货币单位时的可复核分配规则：
    LARGEST_REMAINDER（最大余数优先，并列按持有人名排序）或
    HOLDER_ORDER（按持有人名字典序）。规则写入报告，可逐笔复核；
    不足一个最小货币单位的余量单独列示，不默默丢弃也不重复补齐。
    """

    event_id: str
    unit_change_event_id: str
    residual_rule: str
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if self.residual_rule not in ("LARGEST_REMAINDER", "HOLDER_ORDER"):
            raise ValueError(f"未知余量分配规则: {self.residual_rule!r}")


@dataclass(frozen=True)
class AggregationElection:
    """持有人对某汇总批次的选择：RETAIN 各自保留 / SELL 合并出售 /
    COMPENSATE 按批次统一补偿价补偿。未选择默认 RETAIN。"""

    event_id: str
    batch_id: str
    holder: str
    method: str
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if self.method not in ("RETAIN", "SELL", "COMPENSATE"):
            raise ValueError(f"未知汇总处置方式: {self.method!r}")


@dataclass(frozen=True)
class AggregationWithdrawal:
    """撤回汇总选择（可部分撤回）。

    汇总完成（成交）前撤回：被撤回部分退出参与，可参与数量重新计算；
    完成后撤回：已收现金不当作未发生，只形成 WITHDRAWAL_ADJUSTMENT
    追回调整，被撤回单位恢复为保留。
    """

    event_id: str
    election_event_id: str
    portion: float
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if not 0 < self.portion <= 1:
            raise ValueError("撤回比例必须在 (0, 1] 区间")


@dataclass(frozen=True)
class AggregationExecution:
    """汇总批次成交：合并出售价、总费用与统一补偿价。

    total_fee 为出售方承担的总费用，按参与份额比例分摊；
    compensation_price_per_unit 为 COMPENSATE 参与者的统一补偿单价，
    无 COMPENSATE 参与者时可为 None。成交即批次完成。
    """

    event_id: str
    batch_id: str
    price_per_unit: float
    total_fee: float
    received_date: date
    basis: str
    compensation_price_per_unit: Optional[float] = None

    def __post_init__(self) -> None:
        if self.price_per_unit <= 0:
            raise ValueError("合并出售单价必须为正")
        if self.total_fee < 0:
            raise ValueError("汇总费用不能为负")
        if (self.compensation_price_per_unit is not None
                and self.compensation_price_per_unit <= 0):
            raise ValueError("统一补偿单价必须为正")


@dataclass(frozen=True)
class AggregationPriceRevision:
    """批次完成后迟到的价格修订：只形成追加 PRICE_ADJUSTMENT 调整，
    已分配的成交现金记录保持原样，不当作未发生。"""

    event_id: str
    execution_event_id: str
    new_price_per_unit: float
    received_date: date
    basis: str

    def __post_init__(self) -> None:
        if self.new_price_per_unit <= 0:
            raise ValueError("修订价格必须为正")
