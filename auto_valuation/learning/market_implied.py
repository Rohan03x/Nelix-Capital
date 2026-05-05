"""Market-implied valuation labels and lightweight residual overlay."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from .quality import assess_prediction_record, as_decimal, safe_float, sector_specialist_reason


@dataclass(frozen=True)
class MarketImpliedSnapshot:
    record_id: str
    ticker: str
    valuation_residual_pct: float
    price_return_error_pct: float | None
    implied_wacc_pct: float | None
    implied_terminal_growth_pct: float | None
    implied_wacc_delta_pct: float | None
    implied_terminal_growth_delta_pct: float | None
    ev_revenue_multiple_residual: float | None
    quality_score: float
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "ticker": self.ticker,
            "valuation_residual_pct": self.valuation_residual_pct,
            "price_return_error_pct": self.price_return_error_pct,
            "implied_wacc_pct": self.implied_wacc_pct,
            "implied_terminal_growth_pct": self.implied_terminal_growth_pct,
            "implied_wacc_delta_pct": self.implied_wacc_delta_pct,
            "implied_terminal_growth_delta_pct": self.implied_terminal_growth_delta_pct,
            "ev_revenue_multiple_residual": self.ev_revenue_multiple_residual,
            "quality_score": self.quality_score,
            "reasons": list(self.reasons),
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _weighted_mean(values: list[tuple[float, float]]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values, key=lambda item: item[0])
    if len(ordered) >= 10:
        trim = max(1, int(len(ordered) * 0.10))
        ordered = ordered[trim:-trim] or ordered
    total_weight = sum(max(float(weight), 0.0) for _value, weight in ordered)
    if total_weight <= 0:
        return statistics.mean(value for value, _weight in ordered)
    return sum(float(value) * max(float(weight), 0.0) for value, weight in ordered) / total_weight


def _ticker_root(ticker: str) -> str:
    ticker_upper = (ticker or "").upper().strip()
    return ticker_upper.split(".", 1)[0]


def compute_market_implied_snapshot(record: Any) -> MarketImpliedSnapshot | None:
    quality = assess_prediction_record(record)
    if not quality.eligible("valuation_ev"):
        return None

    predicted_ev = safe_float(getattr(record, "predicted_ev_mm", None))
    actual_ev = safe_float(getattr(record, "actual_ev_mm", None))
    predicted_price = safe_float(getattr(record, "predicted_price_per_share", None))
    actual_price = safe_float(getattr(record, "actual_price_at_horizon", None))
    price_at_prediction = safe_float(getattr(record, "actual_price_at_prediction", None))
    predicted_revenue = safe_float(getattr(record, "predicted_revenue_mm", None))
    actual_revenue = safe_float(getattr(record, "actual_revenue_mm", None))
    predicted_ufcf = safe_float(getattr(record, "predicted_ufcf_mm", None))
    predicted_wacc = as_decimal(getattr(record, "predicted_wacc", None))
    predicted_terminal_growth = as_decimal(getattr(record, "predicted_terminal_growth", None))
    reasons: list[str] = []

    if not predicted_ev or predicted_ev <= 1.0 or actual_ev is None or actual_ev <= 1.0:
        return None
    valuation_residual_pct = actual_ev / predicted_ev - 1.0
    if abs(valuation_residual_pct) > 10.0:
        reasons.append("valuation_residual_outlier")

    price_return_error_pct = None
    if predicted_price and actual_price and price_at_prediction and price_at_prediction > 0.0:
        predicted_return = predicted_price / price_at_prediction - 1.0
        actual_return = actual_price / price_at_prediction - 1.0
        price_return_error_pct = (actual_return - predicted_return) * 100.0

    implied_wacc_pct = None
    implied_terminal_growth_pct = None
    implied_wacc_delta_pct = None
    implied_terminal_growth_delta_pct = None
    if predicted_ufcf and predicted_ufcf > 0.0 and predicted_wacc is not None and predicted_terminal_growth is not None:
        implied_wacc = predicted_terminal_growth + (predicted_ufcf * (1.0 + predicted_terminal_growth) / actual_ev)
        implied_terminal_growth = (actual_ev * predicted_wacc - predicted_ufcf) / max(actual_ev + predicted_ufcf, 1e-9)
        if 0.02 <= implied_wacc <= 0.35:
            implied_wacc_pct = implied_wacc * 100.0
            implied_wacc_delta_pct = (implied_wacc - predicted_wacc) * 100.0
        else:
            reasons.append("implied_wacc_out_of_bounds")
        if -0.05 <= implied_terminal_growth <= 0.08:
            implied_terminal_growth_pct = implied_terminal_growth * 100.0
            implied_terminal_growth_delta_pct = (implied_terminal_growth - predicted_terminal_growth) * 100.0
        else:
            reasons.append("implied_terminal_growth_out_of_bounds")
    else:
        reasons.append("implied_solver_insufficient_cashflow")

    ev_revenue_multiple_residual = None
    if predicted_revenue and actual_revenue and predicted_revenue > 0.0 and actual_revenue > 0.0:
        predicted_multiple = predicted_ev / predicted_revenue
        actual_multiple = actual_ev / actual_revenue
        if predicted_multiple > 0.0 and actual_multiple > 0.0:
            ev_revenue_multiple_residual = math.log(actual_multiple / predicted_multiple)

    return MarketImpliedSnapshot(
        record_id=str(getattr(record, "record_id", "") or ""),
        ticker=str(getattr(record, "ticker", "") or "").upper(),
        valuation_residual_pct=float(valuation_residual_pct),
        price_return_error_pct=price_return_error_pct,
        implied_wacc_pct=implied_wacc_pct,
        implied_terminal_growth_pct=implied_terminal_growth_pct,
        implied_wacc_delta_pct=implied_wacc_delta_pct,
        implied_terminal_growth_delta_pct=implied_terminal_growth_delta_pct,
        ev_revenue_multiple_residual=ev_revenue_multiple_residual,
        quality_score=float(quality.quality_score),
        reasons=reasons,
    )


def _match_score(record: Any, *, ticker: str, sector: str, industry: str, market_cap_regime: str, macro_regime: str) -> tuple[float, str]:
    record_ticker = str(getattr(record, "ticker", "") or "").upper()
    record_sector = str(getattr(record, "sector", "") or "").lower()
    record_industry = str(getattr(record, "industry", "") or "").lower()
    record_cap = str(getattr(record, "market_cap_regime", "") or "").lower()
    record_macro = str(getattr(record, "macro_regime", "") or "").lower()
    ticker_upper = (ticker or "").upper()
    record_ticker_root = _ticker_root(record_ticker)
    ticker_root = _ticker_root(ticker_upper)
    sector_lower = (sector or "").lower()
    industry_lower = (industry or "").lower()
    score = 0.15
    scope = "global"
    if ticker_upper and record_ticker == ticker_upper:
        score += 0.70
        scope = "company"
    elif ticker_root and record_ticker_root == ticker_root:
        score += 0.70
        scope = "company"
    if industry_lower and record_industry == industry_lower:
        score += 0.28
        scope = "industry" if scope == "global" else scope
    elif sector_lower and record_sector == sector_lower:
        score += 0.22
        scope = "sector" if scope == "global" else scope
    if market_cap_regime and record_cap == market_cap_regime.lower():
        score += 0.10
    if macro_regime and record_macro == macro_regime.lower():
        score += 0.08
    return min(score, 1.0), scope


def build_market_residual_overlay(
    records: Iterable[Any],
    *,
    ticker: str,
    sector: str,
    industry: str,
    market_cap_regime: str,
    macro_regime: str,
    min_records: int = 5,
) -> dict[str, Any]:
    specialist_reason = sector_specialist_reason(sector, industry)
    if specialist_reason:
        return {
            "enabled": False,
            "reason": specialist_reason,
            "cohort_size": 0,
            "confidence": 0.0,
            "applied_adjustment_decimal": 0.0,
            "applied_adjustment_pct": 0.0,
        }

    weighted_residuals: list[tuple[float, float]] = []
    scopes: dict[str, int] = {}
    quality_scores: list[float] = []
    snapshots: list[MarketImpliedSnapshot] = []
    for record in records:
        snapshot = compute_market_implied_snapshot(record)
        if snapshot is None:
            continue
        match_score, scope = _match_score(
            record,
            ticker=ticker,
            sector=sector,
            industry=industry,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        )
        if match_score < 0.32:
            continue
        residual = _clamp(snapshot.valuation_residual_pct, -3.0, 3.0)
        weight = max(0.0, match_score) * max(0.05, snapshot.quality_score)
        weighted_residuals.append((residual, weight))
        scopes[scope] = scopes.get(scope, 0) + 1
        quality_scores.append(snapshot.quality_score)
        snapshots.append(snapshot)

    cohort_size = len(weighted_residuals)
    if cohort_size < min_records:
        return {
            "enabled": False,
            "reason": "insufficient_market_residual_evidence",
            "cohort_size": cohort_size,
            "confidence": round(min(cohort_size / max(min_records, 1), 1.0) * 0.2, 2),
            "applied_adjustment_decimal": 0.0,
            "applied_adjustment_pct": 0.0,
            "scope_counts": scopes,
        }

    raw_residual = _weighted_mean(weighted_residuals)
    average_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
    average_match = sum(weight for _value, weight in weighted_residuals) / max(len(weighted_residuals), 1)
    confidence = _clamp(min(1.0, cohort_size / 25.0) * average_quality * _clamp(average_match, 0.25, 1.0), 0.0, 1.0)
    residual_weight = _clamp(0.08 + confidence * 0.26, 0.08, 0.34)
    if confidence < 0.22:
        residual_weight = 0.0
    applied_adjustment = _clamp(raw_residual * residual_weight, -0.25, 0.18)
    dominant_scope = max(scopes.items(), key=lambda item: item[1])[0] if scopes else "global"
    residuals_for_band = sorted(value for value, _weight in weighted_residuals)
    p10_index = int(max(0, min(len(residuals_for_band) - 1, len(residuals_for_band) * 0.10)))
    p90_index = int(max(0, min(len(residuals_for_band) - 1, len(residuals_for_band) * 0.90)))
    p10 = residuals_for_band[p10_index]
    p90 = residuals_for_band[p90_index]

    return {
        "enabled": applied_adjustment != 0.0,
        "reason": None if applied_adjustment != 0.0 else "low_confidence_market_residual_evidence",
        "scope": dominant_scope,
        "scope_counts": scopes,
        "cohort_size": cohort_size,
        "confidence": round(confidence, 2),
        "average_quality_score": round(average_quality, 3),
        "raw_residual_decimal": round(raw_residual, 5),
        "raw_residual_pct": round(raw_residual * 100.0, 1),
        "residual_model_weight": round(residual_weight, 3),
        "dcf_weight": round(1.0 - residual_weight, 3),
        "applied_adjustment_decimal": round(applied_adjustment, 5),
        "applied_adjustment_pct": round(applied_adjustment * 100.0, 1),
        "expected_residual_band_pct": {"p10": round(p10 * 100.0, 1), "p50": round(raw_residual * 100.0, 1), "p90": round(p90 * 100.0, 1)},
        "top_evidence": [snapshot.to_dict() for snapshot in snapshots[:5]],
        "note": (
            f"Market-implied residual overlay uses {cohort_size} quality-gated EV/price outcomes. "
            f"DCF weight {1.0 - residual_weight:.0%}, residual weight {residual_weight:.0%}."
        ),
    }
