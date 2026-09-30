"""逐笔现金流的利率来源解释（定盘或预测分段）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RateExplanation:
    cashflow_index: int
    rate_source: str            # FIXED / FIXING / FORECAST
    fixing_date: date | None
    reference_rate: float       # 使用的基准利率（不含加点）
    spread: float
    all_in_rate: float
    accrual_start: date
    accrual_end: date
    forecast_curve_id: str | None = None   # FORECAST 时记录所用曲线
    status: str = "EXPECTED"               # EXPECTED / SETTLED
