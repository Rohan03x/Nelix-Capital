"""Observation quality and target eligibility for learning rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TARGETS = (
    "revenue",
    "margin",
    "cashflow",
    "risk",
    "valuation_ev",
    "valuation_price",
    "multiples",
    "direction",
    "full_dcf",
)

SPECIALIST_REASON_BY_TOKEN = {
    "financial": "financial_sector_requires_specialist",
    "bank": "financial_sector_requires_specialist",
    "insurance": "financial_sector_requires_specialist",
    "reit": "reit_requires_specialist",
    "real estate investment trust": "reit_requires_specialist",
    "mining": "mining_requires_specialist",
    "metals": "mining_requires_specialist",
    "coal": "mining_requires_specialist",
}


@dataclass(frozen=True)
class LearningObservationQuality:
    quality_score: float
    target_eligibility: dict[str, bool]
    hard_exclusion_reasons: list[str]
    soft_warning_reasons: list[str]
    observation_type: str

    @property
    def full_dcf_eligible(self) -> bool:
        return bool(self.target_eligibility.get("full_dcf"))

    def eligible(self, target: str) -> bool:
        return bool(self.target_eligibility.get(target))

    @property
    def quality_tier(self) -> str:
        if self.quality_score >= 0.90:
            return "high"
        if self.quality_score >= 0.70:
            return "normal"
        if self.quality_score >= 0.50:
            return "downweight"
        if self.quality_score >= 0.30:
            return "restricted"
        return "audit_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality_score": round(float(self.quality_score), 3),
            "quality_tier": self.quality_tier,
            "target_eligibility": dict(self.target_eligibility),
            "hard_exclusion_reasons": list(self.hard_exclusion_reasons),
            "soft_warning_reasons": list(self.soft_warning_reasons),
            "observation_type": self.observation_type,
        }


def as_decimal(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 100.0 if abs(number) > 1.0 else number


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_observation_type(record: Any) -> str:
    context = dict(getattr(record, "prediction_context", None) or {})
    source = str(context.get("source") or "").lower()
    record_id = str(getattr(record, "record_id", "") or "").lower()
    horizon_label = str(getattr(record, "horizon_label", "") or "").lower()
    if "quarter" in source or "quarter_end" in context or "-q" in record_id or horizon_label.startswith("q"):
        return "quarterly_revenue"
    if source in {"price_only", "market_price_only"} or (
        safe_float(getattr(record, "predicted_revenue_mm", None), 0.0) in (None, 0.0)
        and safe_float(getattr(record, "predicted_ev_mm", None), 0.0) in (None, 0.0)
        and safe_float(getattr(record, "predicted_price_per_share", None), 0.0) not in (None, 0.0)
    ):
        return "price_only"
    if "historical_replay" in source or record_id.startswith("bootstrap::"):
        return "annual_dcf_historical_replay"
    if "webapp_live_dashboard" in source:
        return "annual_dcf_live"
    return "annual_dcf_base"


def sector_specialist_reason(sector: str | None, industry: str | None) -> str | None:
    text = f"{sector or ''} {industry or ''}".lower()
    for token, reason in SPECIALIST_REASON_BY_TOKEN.items():
        if token in text:
            return reason
    return None


def _append_once(items: list[str], reason: str) -> None:
    if reason and reason not in items:
        items.append(reason)


def assess_prediction_record(record: Any) -> LearningObservationQuality:
    observation_type = classify_observation_type(record)
    hard_reasons: list[str] = []
    soft_reasons: list[str] = []

    predicted_revenue = safe_float(getattr(record, "predicted_revenue_mm", None), 0.0) or 0.0
    actual_revenue = safe_float(getattr(record, "actual_revenue_mm", None))
    predicted_ev = safe_float(getattr(record, "predicted_ev_mm", None), 0.0) or 0.0
    actual_ev = safe_float(getattr(record, "actual_ev_mm", None))
    predicted_price = safe_float(getattr(record, "predicted_price_per_share", None), 0.0) or 0.0
    actual_price = safe_float(getattr(record, "actual_price_at_horizon", None))
    price_at_prediction = safe_float(getattr(record, "actual_price_at_prediction", None), 0.0) or 0.0
    predicted_margin = as_decimal(getattr(record, "predicted_ebit_margin", None))
    actual_margin = as_decimal(getattr(record, "actual_ebit_margin", None))
    predicted_ufcf = safe_float(getattr(record, "predicted_ufcf_mm", None), 0.0) or 0.0
    actual_ufcf = safe_float(getattr(record, "actual_ufcf_mm", None))
    predicted_wacc = as_decimal(getattr(record, "predicted_wacc", None))
    predicted_terminal_growth = as_decimal(getattr(record, "predicted_terminal_growth", None))
    predicted_equity_value = safe_float(getattr(record, "predicted_equity_value_mm", None), 0.0) or 0.0

    if predicted_revenue < 10.0:
        _append_once(hard_reasons, "predicted_revenue_too_small")
    if actual_revenue is not None and actual_revenue < 10.0:
        _append_once(hard_reasons, "actual_revenue_too_small")
    if predicted_ev <= 1.0:
        _append_once(hard_reasons, "predicted_ev_too_small")
    if predicted_price <= 1.0:
        _append_once(hard_reasons, "predicted_price_too_small")
    if predicted_price > 1.0 and predicted_equity_value <= 0.0 and observation_type not in {"quarterly_revenue", "price_only"}:
        _append_once(hard_reasons, "missing_shares_outstanding")
    if predicted_margin is None or abs(predicted_margin) > 1.0 or (actual_margin is not None and abs(actual_margin) > 1.0):
        _append_once(hard_reasons, "margin_out_of_bounds")
    if observation_type == "quarterly_revenue":
        _append_once(hard_reasons, "quarterly_record_not_valuation_eligible")
    if observation_type == "price_only":
        _append_once(hard_reasons, "price_only_label")

    specialist_reason = sector_specialist_reason(getattr(record, "sector", ""), getattr(record, "industry", ""))
    if specialist_reason:
        _append_once(hard_reasons, specialist_reason)

    ticker = str(getattr(record, "ticker", "") or "")
    if "." in ticker and ticker.rsplit(".", 1)[-1].upper() in {"OTC", "PINK"}:
        _append_once(soft_reasons, "otc_or_pink_listing")
    market_cap_regime = str(getattr(record, "market_cap_regime", "") or "").lower()
    if market_cap_regime in {"micro", "nano"}:
        _append_once(soft_reasons, "microcap_downweight")
    context = dict(getattr(record, "prediction_context", None) or {})
    if context.get("currency_confidence") is False:
        _append_once(hard_reasons, "currency_mismatch_possible")
    if context.get("split_confidence") is False:
        _append_once(hard_reasons, "split_adjustment_unknown")

    annual_core = observation_type not in {"quarterly_revenue", "price_only"}
    specialist_ok = specialist_reason is None
    revenue_ok = predicted_revenue >= 10.0 and actual_revenue is not None and actual_revenue >= 10.0
    margin_ok = revenue_ok and predicted_margin is not None and actual_margin is not None and abs(predicted_margin) <= 1.0 and abs(actual_margin) <= 1.0
    cashflow_ok = revenue_ok and predicted_ufcf != 0.0 and actual_ufcf is not None
    ev_ok = annual_core and specialist_ok and predicted_ev > 1.0 and actual_ev is not None and actual_ev > 1.0
    price_ok = annual_core and specialist_ok and predicted_price > 1.0 and actual_price is not None and actual_price > 0.0
    direction_ok = predicted_price > 1.0 and actual_price is not None and actual_price > 0.0 and price_at_prediction > 0.0
    risk_ok = annual_core and specialist_ok and margin_ok and predicted_wacc is not None and 0.02 <= predicted_wacc <= 0.35 and predicted_terminal_growth is not None
    full_dcf_ok = annual_core and specialist_ok and revenue_ok and margin_ok and predicted_ev > 1.0 and predicted_price > 1.0

    target_eligibility = {
        "revenue": revenue_ok,
        "margin": margin_ok and observation_type != "price_only",
        "cashflow": cashflow_ok and observation_type != "quarterly_revenue",
        "risk": risk_ok,
        "valuation_ev": ev_ok,
        "valuation_price": price_ok,
        "multiples": ev_ok and revenue_ok,
        "direction": direction_ok,
        "full_dcf": full_dcf_ok,
    }

    score = 1.0
    severe = {
        "currency_mismatch_possible",
        "split_adjustment_unknown",
        "predicted_ev_too_small",
        "predicted_price_too_small",
        "predicted_revenue_too_small",
        "actual_revenue_too_small",
        "margin_out_of_bounds",
    }
    for reason in hard_reasons:
        score -= 0.18 if reason in severe else 0.10
    for _reason in soft_reasons:
        score -= 0.08
    if not any(target_eligibility.values()):
        score = min(score, 0.25)
    elif not target_eligibility.get("full_dcf"):
        score = min(score, 0.68)
    score = max(0.0, min(1.0, score))

    return LearningObservationQuality(
        quality_score=round(score, 4),
        target_eligibility=target_eligibility,
        hard_exclusion_reasons=hard_reasons,
        soft_warning_reasons=soft_reasons,
        observation_type=observation_type,
    )
