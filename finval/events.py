"""估值事件：历史定盘更正与部分提前终止。

事件带 notice_date（到达/生效日）：估值日早于 notice_date 的估值仍使用当时资料，
估值日晚于 notice_date 的估值才应用该事件。所有事件可追溯到 reference（生效依据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FixingCorrection:
    """历史定盘更正：某定盘日的官方修正值。

    notice_date 为更正到达日；到达之前的估值仍使用原始定盘值。
    若对应现金流已支付，不改动已支付金额，而是生成调整款项。
    """

    fixing_date: date
    corrected_value: float
    notice_date: date
    reference: str = ""          # 生效依据，如更正公告编号


@dataclass(frozen=True)
class PartialTermination:
    """已发生的部分提前终止。

    termination_date 为终止生效日（未调整）；计息边界按合约营业日约定调整。
    调整后的生效日落在哪个计息期内，就从该日起按剩余本金计息；
    恰好落在周期边界上时，前一完整周期按终止前本金全额计息。
    """

    termination_date: date
    terminated_notional: float
    notice_date: date
    reference: str = ""          # 生效依据，如终止通知编号
