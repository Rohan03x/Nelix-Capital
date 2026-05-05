"""Normalized labels derived from prediction and realized learning rows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from .quality import LearningObservationQuality, assess_prediction_record, as_decimal, safe_float


@dataclass(frozen=True)
class LearningLabel:
    label_id: str
    record_id: str
    ticker: str
    target_name: str
    predicted_value: float | None
    target_value: float | None
    residual_value: float | None
    residual_pct: float | None
    label_status: str
    quality_score: float
    eligibility_scope: str
    source_name: str
    source_kind: str
    as_of_date: str | None
    aligned_period_end: str | None
    quality_reasons: list[str]
    observation_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_id": self.label_id,
            "record_id": self.record_id,
            "ticker": self.ticker,
            "target_name": self.target_name,
            "predicted_value": self.predicted_value,
            "target_value": self.target_value,
            "residual_value": self.residual_value,
            "residual_pct": self.residual_pct,
            "label_status": self.label_status,
            "quality_score": self.quality_score,
            "eligibility_scope": self.eligibility_scope,
            "source_name": self.source_name,
            "source_kind": self.source_kind,
            "as_of_date": self.as_of_date,
            "aligned_period_end": self.aligned_period_end,
            "quality_reasons": list(self.quality_reasons),
            "observation_type": self.observation_type,
        }


def _label_id(record_id: str, target_name: str, eligibility_scope: str) -> str:
    identity = f"{record_id}:{target_name}:{eligibility_scope}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def _pct_residual(actual: float | None, predicted: float | None) -> float | None:
    if actual is None or predicted in (None, 0.0):
        return None
    return (float(actual) - float(predicted)) / max(abs(float(predicted)), 1e-9)


def _source(record: Any) -> tuple[str, str, str | None, str | None]:
    context = dict(getattr(record, "prediction_context", None) or {})
    source_name = str(context.get("source") or "prediction_records")
    source_kind = str(context.get("data_source") or context.get("source_kind") or "ledger")
    as_of_date = str(getattr(record, "postmortem_date", "") or getattr(record, "run_date", "") or "") or None
    aligned_period_end = str(getattr(record, "horizon_target_date", "") or "") or None
    return source_name, source_kind, as_of_date, aligned_period_end


def _make_label(
    record: Any,
    quality: LearningObservationQuality,
    *,
    target_name: str,
    predicted_value: float | None,
    target_value: float | None,
    residual_value: float | None,
    residual_pct: float | None,
    eligibility_scope: str,
) -> LearningLabel:
    source_name, source_kind, as_of_date, aligned_period_end = _source(record)
    record_id = str(getattr(record, "record_id", "") or "")
    return LearningLabel(
        label_id=_label_id(record_id, target_name, eligibility_scope),
        record_id=record_id,
        ticker=str(getattr(record, "ticker", "") or "").upper(),
        target_name=target_name,
        predicted_value=predicted_value,
        target_value=target_value,
        residual_value=residual_value,
        residual_pct=residual_pct,
        label_status="complete" if target_value is not None else "pending",
        quality_score=float(quality.quality_score),
        eligibility_scope=eligibility_scope,
        source_name=source_name,
        source_kind=source_kind,
        as_of_date=as_of_date,
        aligned_period_end=aligned_period_end,
        quality_reasons=list(quality.hard_exclusion_reasons),
        observation_type=quality.observation_type,
    )


def labels_for_record(record: Any, quality: LearningObservationQuality | None = None) -> list[LearningLabel]:
    quality = quality or assess_prediction_record(record)
    labels: list[LearningLabel] = []
    predicted_revenue = safe_float(getattr(record, "predicted_revenue_mm", None))
    actual_revenue = safe_float(getattr(record, "actual_revenue_mm", None))
    predicted_margin = as_decimal(getattr(record, "predicted_ebit_margin", None))
    actual_margin = as_decimal(getattr(record, "actual_ebit_margin", None))
    predicted_ufcf = safe_float(getattr(record, "predicted_ufcf_mm", None))
    actual_ufcf = safe_float(getattr(record, "actual_ufcf_mm", None))
    predicted_ev = safe_float(getattr(record, "predicted_ev_mm", None))
    actual_ev = safe_float(getattr(record, "actual_ev_mm", None))
    predicted_price = safe_float(getattr(record, "predicted_price_per_share", None))
    actual_price = safe_float(getattr(record, "actual_price_at_horizon", None))
    price_at_prediction = safe_float(getattr(record, "actual_price_at_prediction", None))

    if quality.eligible("revenue"):
        labels.append(
            _make_label(
                record,
                quality,
                target_name="revenue_mm",
                predicted_value=predicted_revenue,
                target_value=actual_revenue,
                residual_value=(actual_revenue - predicted_revenue) if actual_revenue is not None and predicted_revenue is not None else None,
                residual_pct=_pct_residual(actual_revenue, predicted_revenue),
                eligibility_scope="operating_revenue",
            )
        )
    if quality.eligible("margin") and predicted_margin is not None and actual_margin is not None:
        labels.append(
            _make_label(
                record,
                quality,
                target_name="ebit_margin_pct",
                predicted_value=predicted_margin * 100.0,
                target_value=actual_margin * 100.0,
                residual_value=(actual_margin - predicted_margin) * 100.0,
                residual_pct=None,
                eligibility_scope="operating_margin",
            )
        )
    if quality.eligible("cashflow") and predicted_revenue and actual_revenue and predicted_ufcf is not None and actual_ufcf is not None:
        predicted_margin_pct = predicted_ufcf / predicted_revenue * 100.0
        actual_margin_pct = actual_ufcf / actual_revenue * 100.0
        labels.append(
            _make_label(
                record,
                quality,
                target_name="ufcf_margin_pct",
                predicted_value=predicted_margin_pct,
                target_value=actual_margin_pct,
                residual_value=actual_margin_pct - predicted_margin_pct,
                residual_pct=None,
                eligibility_scope="cashflow",
            )
        )
    if quality.eligible("valuation_ev"):
        residual_pct = _pct_residual(actual_ev, predicted_ev)
        labels.append(
            _make_label(
                record,
                quality,
                target_name="ev_mm",
                predicted_value=predicted_ev,
                target_value=actual_ev,
                residual_value=(actual_ev - predicted_ev) if actual_ev is not None and predicted_ev is not None else None,
                residual_pct=residual_pct,
                eligibility_scope="valuation_ev",
            )
        )
        labels.append(
            _make_label(
                record,
                quality,
                target_name="valuation_residual_pct",
                predicted_value=0.0,
                target_value=residual_pct,
                residual_value=residual_pct,
                residual_pct=residual_pct,
                eligibility_scope="market_implied",
            )
        )
    if quality.eligible("valuation_price"):
        labels.append(
            _make_label(
                record,
                quality,
                target_name="price_per_share",
                predicted_value=predicted_price,
                target_value=actual_price,
                residual_value=(actual_price - predicted_price) if actual_price is not None and predicted_price is not None else None,
                residual_pct=_pct_residual(actual_price, predicted_price),
                eligibility_scope="valuation_price",
            )
        )
    if quality.eligible("direction") and price_at_prediction:
        predicted_return = (predicted_price / price_at_prediction) - 1.0 if predicted_price is not None else None
        actual_return = (actual_price / price_at_prediction) - 1.0 if actual_price is not None else None
        if predicted_return is not None and actual_return is not None:
            labels.append(
                _make_label(
                    record,
                    quality,
                    target_name="price_return_pct",
                    predicted_value=predicted_return * 100.0,
                    target_value=actual_return * 100.0,
                    residual_value=(actual_return - predicted_return) * 100.0,
                    residual_pct=None,
                    eligibility_scope="direction",
                )
            )
    if quality.eligible("multiples") and predicted_ev and actual_ev and predicted_revenue and actual_revenue:
        predicted_multiple = predicted_ev / predicted_revenue
        actual_multiple = actual_ev / actual_revenue
        labels.append(
            _make_label(
                record,
                quality,
                target_name="ev_revenue_multiple",
                predicted_value=predicted_multiple,
                target_value=actual_multiple,
                residual_value=actual_multiple - predicted_multiple,
                residual_pct=_pct_residual(actual_multiple, predicted_multiple),
                eligibility_scope="market_implied",
            )
        )
    return labels


def build_labels(records: Iterable[Any]) -> list[LearningLabel]:
    labels: list[LearningLabel] = []
    for record in records:
        labels.extend(labels_for_record(record))
    return labels
