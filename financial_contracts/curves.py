from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext


@dataclass(frozen=True, kw_only=True)
class DiscountCurve:
    curve_id: str
    currency: str
    valuation_date: date
    points: tuple[tuple[date, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.curve_id:
            raise ValueError("curve_id is required")
        if not self.currency:
            raise ValueError("currency is required")
        normalized = tuple((point_date, Decimal(str(discount_factor)).normalize()) for point_date, discount_factor in self.points)
        if len(normalized) < 2:
            raise ValueError("At least two discount points are required")
        if any(left[0] >= right[0] for left, right in zip(normalized, normalized[1:])):
            raise ValueError("Discount points must have strictly increasing dates")
        if any(discount_factor <= 0 for _, discount_factor in normalized):
            raise ValueError("Discount factors must be positive")
        object.__setattr__(self, "points", normalized)

    def discount_factor(self, payment_date: date) -> Decimal:
        first_date, first_factor = self.points[0]
        last_date, last_factor = self.points[-1]
        if payment_date < first_date or payment_date > last_date:
            raise ValueError("Payment date is outside discount curve range")

        for (left_date, left_factor), (right_date, right_factor) in zip(self.points, self.points[1:]):
            if left_date <= payment_date <= right_date:
                if payment_date == left_date:
                    return left_factor
                if payment_date == right_date:
                    return right_factor
                total_seconds = (right_date - left_date).total_seconds()
                elapsed_seconds = (payment_date - left_date).total_seconds()
                weight = Decimal(elapsed_seconds) / Decimal(total_seconds)
                log_left = left_factor.ln()
                log_right = right_factor.ln()
                with localcontext() as context:
                    context.prec = 28
                    return (log_left + weight * (log_right - log_left)).exp()
        raise AssertionError("Unreachable discount interpolation branch")
