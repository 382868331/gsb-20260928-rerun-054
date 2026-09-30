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

    def zero_rate(self, d: date) -> float:
        """连续复利年化零息利率：-ln(DF(t)) / ACT/365 年。"""
        if d <= self.valuation_date:
            raise ValueError("零息利率期限必须晚于曲线估值日")
        return -math.log(self.discount_factor(d)) / (
            (d - self.valuation_date).days / 365.0)

    def forward_rate(self, start: date, end: date) -> float:
        """[start, end) 上的连续复利远期利率（按实际天数/365 年化）。"""
        if start < self.valuation_date:
            raise ValueError("远期利率起点不得早于曲线估值日")
        if end <= start:
            raise ValueError("远期利率终点必须晚于起点")
        log_df_start = (
            0.0 if start == self.valuation_date
            else math.log(self.discount_factor(start)))
        log_df_end = math.log(self.discount_factor(end))
        return (log_df_start - log_df_end) / ((end - start).days / 365.0)

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


@dataclass(frozen=True)
class ForecastCurve(DiscountCurve):
    """浮动利率预测曲线；可独立于贴现曲线平移，历史定盘不经过本曲线。"""

    def parallel_shift(self, spread: float) -> "ForecastCurve":
        new_dfs = tuple(
            df * math.exp(-spread * (pd - self.valuation_date).days / 365.0)
            for pd, df in zip(self.pillar_dates, self.discount_factors)
        )
        return ForecastCurve(
            curve_id=f"{self.curve_id}_shift{spread:g}",
            valuation_date=self.valuation_date,
            pillar_dates=self.pillar_dates,
            discount_factors=new_dfs,
        )
