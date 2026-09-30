"""贴现曲线：贴现因子对数线性插值，曲线本身不可变。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class DiscountCurve:
    curve_id: str
    valuation_date: date
    pillar_dates: tuple[date, ...]
    discount_factors: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.pillar_dates) != len(self.discount_factors):
            raise ValueError("期限日与贴现因子数量不一致")
        if not self.pillar_dates:
            raise ValueError("曲线至少需要一个期限点")
        if list(self.pillar_dates) != sorted(self.pillar_dates):
            raise ValueError("期限日必须升序排列")
        if any(df <= 0 for df in self.discount_factors):
            raise ValueError("贴现因子必须为正")
        if self.pillar_dates[0] < self.valuation_date:
            raise ValueError("最早期限日不得早于估值日")

    def discount_factor(self, d: date) -> float:
        """对贴现因子取对数后按日线性插值；曲线外取端值平坦外推。"""
        if d < self.valuation_date:
            raise ValueError("不能对估值日之前的日期贴现")
        dates, dfs = self.pillar_dates, self.discount_factors
        if d == self.valuation_date:
            if dates[0] == self.valuation_date:
                return dfs[0]
            return 1.0
        if d <= dates[0]:
            x0 = self.valuation_date
            y0, y1 = 1.0, dfs[0]
            x1 = dates[0]
        elif d >= dates[-1]:
            return dfs[-1]
        else:
            for i in range(1, len(dates)):
                if d <= dates[i]:
                    x0, x1 = dates[i - 1], dates[i]
                    y0, y1 = dfs[i - 1], dfs[i]
                    break
        t = (d - x0).days / (x1 - x0).days
        return math.exp(math.log(y0) + t * (math.log(y1) - math.log(y0)))

    def parallel_shift(self, spread: float) -> "DiscountCurve":
        """整体平移零息利率（连续复利），返回新曲线，原曲线不变。"""
        new_dfs = tuple(
            df * math.exp(-spread * (pd - self.valuation_date).days / 365.0)
            for pd, df in zip(self.pillar_dates, self.discount_factors)
        )
        return DiscountCurve(
            curve_id=f"{self.curve_id}_shift{spread:g}",
            valuation_date=self.valuation_date,
            pillar_dates=self.pillar_dates,
            discount_factors=new_dfs,
        )

    def forward_rate(self, start: date, end: date, day_count: object) -> float:
        """由贴现因子推导 [start, end) 的简单复利远期利率。"""
        from .conventions import year_fraction

        tau = year_fraction(start, end, day_count)
        if tau <= 0:
            raise ValueError("远期利率区间必须严格为正")
        df_start = self.discount_factor(start)
        df_end = self.discount_factor(end)
        return (df_start / df_end - 1.0) / tau
