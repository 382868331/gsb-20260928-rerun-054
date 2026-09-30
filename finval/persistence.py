"""估值批次的 JSON 持久化：保存/恢复估值记录与日程快照。"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

from .schedule import Cashflow
from .valuation import CashflowExplanation, Valuation, ValuationBatch


def _encode(obj):
    if isinstance(obj, date):
        return {"__date__": obj.isoformat()}
    if isinstance(obj, tuple):
        return {"__tuple__": [_encode(x) for x in obj]}
    if isinstance(obj, list):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    return obj


def _decode(obj):
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    if isinstance(obj, dict):
        if "__date__" in obj:
            return date.fromisoformat(obj["__date__"])
        if "__tuple__" in obj:
            return tuple(_decode(x) for x in obj["__tuple__"])
        return {k: _decode(v) for k, v in obj.items()}
    return obj


def _record_to_dict(v: Valuation) -> dict:
    return {
        "contract_id": v.contract_id,
        "curve_id": v.curve_id,
        "valuation_date": _encode(v.valuation_date),
        "forecast_curve_id": v.forecast_curve_id,
        "cashflows": [_encode(dataclasses.asdict(cf)) for cf in v.cashflows],
        "present_values": list(v.present_values),
        "discount_factors": list(v.discount_factors),
        "explanations": [
            _encode(dataclasses.asdict(e)) for e in v.explanations],
    }


def _record_from_dict(d: dict) -> Valuation:
    d = _decode(d)
    return Valuation(
        contract_id=d["contract_id"],
        curve_id=d["curve_id"],
        valuation_date=d["valuation_date"],
        cashflows=tuple(Cashflow(**cf) for cf in d["cashflows"]),
        present_values=tuple(d["present_values"]),
        discount_factors=tuple(d["discount_factors"]),
        explanations=tuple(
            CashflowExplanation(**e) for e in d["explanations"]),
        forecast_curve_id=d["forecast_curve_id"],
    )


def save_batch(batch: ValuationBatch, path) -> None:
    payload = {
        "batch_id": batch.batch_id,
        "records": [_record_to_dict(v) for v in batch.records],
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_batch(path) -> ValuationBatch:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    batch = ValuationBatch(payload["batch_id"])
    batch._records.extend(_record_from_dict(d) for d in payload["records"])
    return batch

