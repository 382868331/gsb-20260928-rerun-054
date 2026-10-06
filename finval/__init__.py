"""金融合约估值系统：固定利率现金流（合约、日历、曲线、估值批次）。"""

from .calendar import Calendar, BusinessDayConvention
from .conventions import DayCount, Frequency, UnsupportedConventionError
from .contract import FixedRateContract, StubType
from .events import (
    AggregationBatch,
    AggregationElection,
    AggregationExecution,
    AggregationPriceRevision,
    AggregationWithdrawal,
    CashPriceFixing,
    ElectionRevocation,
    FixingCorrection,
    FractionElection,
    PartialTermination,
    RightsTransfer,
    TransferRevocation,
    UnitChange,
)
from .floating import FloatingRateContract, MissingFixingError
from .schedule import Cashflow, generate_floating_schedule, generate_schedule
from .curve import DiscountCurve
from .valuation import CashflowExplanation, Valuation, ValuationBatch, value_floating_contract
from .allocation import (
    ORIGINAL_HOLDER,
    AllocationReport,
    CashflowTotal,
    HolderPosition,
    HolderShare,
    allocate,
    holder_positions,
    unit_factor_at,
)
from .fractions import (
    FractionRecord,
    FractionReport,
    GeneratedFraction,
    fraction_settlement,
)
from .aggregation import (
    AggregationRecord,
    AggregationReport,
    ProcessComparison,
    ProcessStep,
    ResidualAllocation,
    SubUnitResidual,
    aggregation_settlement,
    compare_processes,
)

__all__ = [
    "Calendar",
    "BusinessDayConvention",
    "DayCount",
    "Frequency",
    "UnsupportedConventionError",
    "FixedRateContract",
    "FloatingRateContract",
    "StubType",
    "MissingFixingError",
    "FixingCorrection",
    "CashPriceFixing",
    "ElectionRevocation",
    "FractionElection",
    "PartialTermination",
    "RightsTransfer",
    "TransferRevocation",
    "UnitChange",
    "Cashflow",
    "CashflowExplanation",
    "generate_schedule",
    "generate_floating_schedule",
    "DiscountCurve",
    "value_floating_contract",
    "Valuation",
    "ValuationBatch",
    "ORIGINAL_HOLDER",
    "AllocationReport",
    "CashflowTotal",
    "HolderPosition",
    "HolderShare",
    "allocate",
    "holder_positions",
    "unit_factor_at",
    "AggregationBatch",
    "AggregationElection",
    "AggregationExecution",
    "AggregationPriceRevision",
    "AggregationWithdrawal",
    "AggregationRecord",
    "AggregationReport",
    "ProcessComparison",
    "ProcessStep",
    "ResidualAllocation",
    "SubUnitResidual",
    "aggregation_settlement",
    "compare_processes",
    "FractionRecord",
    "FractionReport",
    "GeneratedFraction",
    "fraction_settlement",
]
