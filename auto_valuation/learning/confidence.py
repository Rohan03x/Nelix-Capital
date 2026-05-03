"""Confidence intervals and Monte Carlo summaries for adaptive DCF forecasts."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Any, Callable

try:
    from auto_valuation.model.sector import FINANCIAL, MINING, REIT, detect_sector_type
except ImportError:
    FINANCIAL = "financial"
    MINING = "mining"
    REIT = "reit"

    def detect_sector_type(sector: str, industry: str = "") -> str:
        return "standard"


try:
    from auto_valuation.config import LEARNING_CONFIG as _LEARNING_CONFIG
except ImportError:
    _LEARNING_CONFIG = {
        "base_revenue_uncertainty": 0.06,
        "base_margin_uncertainty": 0.025,
        "base_wacc_uncertainty": 0.01,
        "uncertainty_growth_per_year": 0.08,
        "min_calibration_observations": 5,
        "monte_carlo_samples": 1000,
        "monte_carlo_seed": 42,
    }


Z10 = 1.2815515655446004
Z25 = 0.6744897501960817


@dataclass(frozen=True)
class ConfidenceInterval:
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    confidence_score: float
    driving_uncertainty: str


@dataclass(frozen=True)
class ConfidenceBundle:
    intervals: dict[str, list[ConfidenceInterval]] = field(default_factory=dict)
    overall_score: float = 0.0
    driving_uncertainty: str = "thin_data"

    def get(self, key: str) -> ConfidenceInterval | None:
        value = self.intervals.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        return value


@dataclass(frozen=True)
class MonteCarloSummary:
    samples: int
    ev_p10: float
    ev_p25: float
    ev_p50: float
    ev_p75: float
    ev_p90: float
    price_p10: float | None = None
    price_p25: float | None = None
    price_p50: float | None = None
    price_p75: float | None = None
    price_p90: float | None = None
    overall_score: float = 0.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _piecewise_interpolate(x_value: float, anchors: list[tuple[float, float]]) -> float:
    if not anchors:
        return 0.0
    ordered = sorted((float(x), float(y)) for x, y in anchors)
    if x_value <= ordered[0][0]:
        return ordered[0][1]
    if x_value >= ordered[-1][0]:
        return ordered[-1][1]
    for index in range(1, len(ordered)):
        left_x, left_y = ordered[index - 1]
        right_x, right_y = ordered[index]
        if x_value <= right_x:
            width = right_x - left_x
            if width <= 0:
                return right_y
            weight = (x_value - left_x) / width
            return left_y + (right_y - left_y) * weight
    return ordered[-1][1]


def _safe_mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _score_label(score: float) -> str:
    if score >= 0.80:
        return "high"
    if score >= 0.65:
        return "moderate"
    if score >= 0.50:
        return "guarded"
    if score >= 0.35:
        return "fragile"
    return "low"


def _grade_from_score(score_100: int) -> tuple[str, str, str]:
    if score_100 >= 80:
        return "A", "High Confidence", "green"
    if score_100 >= 65:
        return "B", "Moderate Confidence", "amber"
    if score_100 >= 50:
        return "C", "Guarded Confidence", "amber"
    if score_100 >= 35:
        return "D", "Fragile Confidence", "red"
    return "F", "Low Confidence", "red"


def _dashboard_suitability(payload: dict[str, Any]) -> tuple[bool, str]:
    sector = str(payload.get("sector") or "").strip()
    industry = str(payload.get("industry") or "").strip()

    if not sector or not industry:
        return False, "Sector and industry metadata are incomplete, so this DCF should be treated as provisional."

    sector_type = detect_sector_type(sector, industry)
    if sector_type == FINANCIAL:
        return False, "Financial companies are not a clean fit for UFCF DCF; use dividend or excess-return methods instead."
    if sector_type == REIT:
        return False, "Real estate and REIT-style businesses are better assessed with FFO/AFFO or NAV-based methods than a standard UFCF DCF."
    if sector_type == MINING:
        return False, "Mining and resource businesses are better assessed with NAV and reserve-life methods than a standard UFCF DCF."
    return True, ""


def _analog_dispersion(scores: list[float], analog_count: int) -> float:
    if analog_count <= 0:
        return 1.0
    if len(scores) < 2:
        return 0.55
    return _clamp(pstdev(scores) / 0.20)


def _forecast_layer_conflict(forecast_layers: list[dict[str, Any]]) -> float:
    conflicts: list[float] = []
    for layer in forecast_layers:
        anchors: list[float] = []
        for key in ("company_anchor", "sector_anchor", "learned_adjustment"):
            value = layer.get(key)
            if value is None:
                continue
            try:
                anchors.append(float(value))
            except (TypeError, ValueError):
                continue
        if len(anchors) < 2:
            continue
        scale = max(max(abs(anchor) for anchor in anchors), 1.0)
        conflicts.append((max(anchors) - min(anchors)) / scale)
    return _clamp(_safe_mean(conflicts)) if conflicts else 0.0


def _maintenance_freshness(payload: dict[str, Any]) -> tuple[float, str]:
    explainability = _mapping(payload.get("explainability"))
    current_snapshot = _mapping(explainability.get("current_snapshot"))
    maintenance = _mapping(explainability.get("maintenance"))
    realized_evidence = _mapping(explainability.get("realized_evidence"))

    if current_snapshot.get("persisted"):
        return 1.0, "Current base-case snapshot is persisted into the learning ledger."
    if maintenance.get("ran"):
        return 0.82, "Scheduled maintenance refreshed eligible postmortems and ledger evidence."
    if maintenance.get("reason") == "throttled":
        return 0.68, "Maintenance was run recently and is intentionally throttled between scans."
    if int(realized_evidence.get("matured_records") or 0) > 0:
        return 0.58, "Some matured realized evidence exists, but the latest session has not refreshed it yet."
    if current_snapshot or maintenance or realized_evidence:
        return 0.42, "Maintenance freshness is limited, so realized evidence may lag the latest fundamentals."
    return 0.55, "Maintenance freshness is neutral because no live maintenance state was provided."


def _data_quality_score(payload: dict[str, Any]) -> tuple[float, str]:
    explainability = _mapping(payload.get("explainability"))
    company_memory = _mapping(explainability.get("company_memory"))
    history_window_years = int(payload.get("history_window_years") or company_memory.get("history_window_years") or 0)
    completed_years = int(company_memory.get("completed_years") or history_window_years)
    review_due = bool(payload.get("quinquennial_review_due") or company_memory.get("review_due"))

    score = _clamp(
        0.55 * min(history_window_years / 5.0, 1.0)
        + 0.25 * min(completed_years / 10.0, 1.0)
        + 0.20 * (0.72 if review_due else 1.0)
    )
    detail = (
        f"{history_window_years}y history window, {completed_years} completed year(s), and a quinquennial review is due."
        if review_due
        else f"{history_window_years}y history window and {completed_years} completed year(s) support the live inputs."
    )
    return score, detail


def build_ranked_confidence_model(payload: dict[str, Any]) -> dict[str, Any]:
    layered_learning = _mapping(payload.get("layered_learning"))
    layer_mix = _mapping(layered_learning.get("layer_mix"))
    uncertainty = _mapping(layered_learning.get("uncertainty"))
    structural_break = _mapping(layered_learning.get("structural_break"))
    explainability = _mapping(payload.get("explainability"))
    forecast_layers = _rows(explainability.get("forecast_layers"))
    analog_payload = _mapping(payload.get("analogs"))
    analog_evidence = _mapping(explainability.get("analog_evidence"))
    analog_items = _rows(analog_payload.get("items"))
    global_learning = _mapping(payload.get("global_learning"))
    relationship_graph = _mapping(payload.get("relationship_graph") or explainability.get("relationship_graph"))

    company_records = int(_mapping(layer_mix.get("company_memory")).get("records") or 0)
    cohort_records = int(payload.get("calibration_cohort_size") or _mapping(layer_mix.get("cohort_memory")).get("records") or 0)
    sector_records = int(_mapping(layer_mix.get("sector_memory")).get("records") or 0)
    calibration_confidence = _clamp(float(payload.get("calibration_confidence") or 0.0))
    learning_confidence = _clamp(float(payload.get("learning_confidence") or calibration_confidence))
    weak_evidence = bool(uncertainty.get("weak_evidence") or cohort_records < _LEARNING_CONFIG.get("min_calibration_observations", 5))
    scenario_width_multiplier = max(1.0, float(payload.get("scenario_width_multiplier") or uncertainty.get("scenario_width_multiplier") or 1.0))
    scenario_penalty = _clamp((scenario_width_multiplier - 1.0) / 1.5)

    analog_count = int(analog_payload.get("count") or analog_evidence.get("match_count") or 0)
    analog_confidence = _clamp(float(analog_evidence.get("confidence") or _mapping(layer_mix.get("analog_memory")).get("confidence") or 0.0))
    pattern_score = _clamp(
        max(
            float(payload.get("pattern_match_score") or 0.0),
            float(analog_payload.get("pattern_match_score") or 0.0),
            float(analog_evidence.get("pattern_score") or 0.0),
        )
    )
    top_similarity = max((float(item.get("similarity") or 0.0) for item in analog_items), default=0.0)
    similarity_strength = _clamp(max(pattern_score, top_similarity))
    analog_scores = [float(item.get("score") or 0.0) for item in analog_items]
    analog_dispersion = _analog_dispersion(analog_scores, analog_count)
    graph_confidence = _clamp(float(relationship_graph.get("confidence") or 0.0))
    graph_node_count = int(relationship_graph.get("node_count") or 0)
    graph_edge_count = int(relationship_graph.get("edge_count") or 0)
    graph_sector_span = int(relationship_graph.get("sector_span") or 0)
    relational_strength = _clamp(
        0.55 * graph_confidence
        + 0.20 * min(graph_node_count / 6.0, 1.0)
        + 0.15 * min(graph_edge_count / 10.0, 1.0)
        + 0.10 * min(graph_sector_span / 3.0, 1.0)
    )

    base_conflict = _clamp(float(uncertainty.get("conflict_score") or 0.0) / 0.03)
    anchor_conflict = _forecast_layer_conflict(forecast_layers)
    layer_conflict = _clamp(max(base_conflict, anchor_conflict))
    structural_break_score = _clamp(float(structural_break.get("score") or 0.0))

    maintenance_freshness, maintenance_detail = _maintenance_freshness(payload)
    data_quality_score, data_quality_detail = _data_quality_score(payload)

    realized_evidence_depth = _clamp(
        0.42 * min(cohort_records / 10.0, 1.0)
        + 0.30 * min(company_records / 3.0, 1.0)
        + 0.18 * min(sector_records / 12.0, 1.0)
        + 0.10 * learning_confidence
    )
    analog_stability = _clamp(
        0.42 * analog_confidence
        + 0.20 * min(analog_count / 3.0, 1.0)
        + 0.23 * similarity_strength
        + 0.15 * pattern_score
        + 0.12 * relational_strength
        - 0.28 * analog_dispersion
    )
    layer_agreement = _clamp(1.0 - 0.72 * layer_conflict - 0.18 * scenario_penalty - 0.10 * (1.0 - learning_confidence))
    structural_stability = _clamp(1.0 - 0.78 * structural_break_score - 0.22 * scenario_penalty)

    spread = float(payload.get("wacc") or 0.0) - float(payload.get("terminal_growth") or 0.0)
    spread_score = _piecewise_interpolate(
        spread,
        [
            (1.5, 0.08),
            (2.5, 0.24),
            (3.5, 0.43),
            (4.5, 0.64),
            (5.5, 0.81),
            (7.0, 0.95),
        ],
    )
    discount_rate_sensitivity = _clamp(0.75 * spread_score + 0.25 * (1.0 - scenario_penalty))
    global_breadth = _clamp(
        0.65 * float(global_learning.get("confidence") or 0.0)
        + 0.20 * min(float(global_learning.get("cohort_size") or 0.0) / 12.0, 1.0)
        + 0.15 * min(float(global_learning.get("sector_span") or 0.0) / 4.0, 1.0)
        + 0.15 * relational_strength
    )

    assumption_support = _clamp(
        0.27 * realized_evidence_depth
        + 0.21 * layer_agreement
        + 0.16 * analog_stability
        + 0.14 * structural_stability
        + 0.11 * data_quality_score
        + 0.07 * maintenance_freshness
        + 0.04 * global_breadth
        + 0.05 * relational_strength
    )
    if weak_evidence:
        assumption_support *= 0.84
    if analog_count <= 1 and pattern_score < 0.70:
        assumption_support *= 0.93
    assumption_support = _clamp(assumption_support)

    expected_assumption_error = round(
        1.5 + 6.2 * (1.0 - assumption_support) + 1.5 * layer_conflict + 1.2 * structural_break_score,
        2,
    )
    assumption_confidence = _clamp(
        _piecewise_interpolate(
            expected_assumption_error,
            [
                (1.5, 0.92),
                (2.5, 0.84),
                (3.5, 0.76),
                (4.5, 0.68),
                (5.5, 0.59),
                (6.8, 0.48),
                (8.0, 0.36),
                (9.5, 0.24),
                (11.0, 0.14),
            ],
        )
    )

    expected_valuation_error_p50 = round(
        max(
            3.5,
            4.0
            + 10.0 * (1.0 - assumption_confidence)
            + 7.0 * (1.0 - discount_rate_sensitivity)
            + 4.5 * structural_break_score
            + 4.0 * scenario_penalty
            + 3.5 * (1.0 - analog_stability)
            + 2.5 * layer_conflict
            + 1.5 * (1.0 - maintenance_freshness),
        ),
        2,
    )
    expected_valuation_error_band = {
        "p50": expected_valuation_error_p50,
        "p75": round(expected_valuation_error_p50 * (1.25 + 0.10 * scenario_penalty), 2),
        "p90": round(expected_valuation_error_p50 * (1.55 + 0.20 * scenario_penalty), 2),
    }
    valuation_confidence = _clamp(
        _piecewise_interpolate(
            expected_valuation_error_p50,
            [
                (4.0, 0.93),
                (6.0, 0.86),
                (8.0, 0.78),
                (10.0, 0.69),
                (12.0, 0.60),
                (15.0, 0.49),
                (18.0, 0.39),
                (22.0, 0.29),
                (28.0, 0.18),
                (35.0, 0.10),
            ],
        )
    )

    assumption_score_100 = int(round(assumption_confidence * 100))
    valuation_score_100 = int(round(valuation_confidence * 100))
    dominant_risks = {
        "Realized evidence depth": 1.0 - realized_evidence_depth,
        "History & data quality": 1.0 - data_quality_score,
        "Analog stability": 1.0 - analog_stability,
        "Relational memory": 1.0 - relational_strength,
        "Layer agreement": 1.0 - layer_agreement,
        "Structural stability": 1.0 - structural_stability,
        "Scenario discipline": scenario_penalty,
        "Maintenance freshness": 1.0 - maintenance_freshness,
        "Discount-rate sensitivity": 1.0 - discount_rate_sensitivity,
    }
    dominant_risk = max(dominant_risks, key=dominant_risks.get)

    components = [
        {
            "label": "Realized evidence depth",
            "score": int(round(realized_evidence_depth * 100)),
            "category": "assumptions",
            "detail": f"{company_records} ticker-specific, {cohort_records} matched cohort, and {sector_records} sector records support the forecast.",
        },
        {
            "label": "History & data quality",
            "score": int(round(data_quality_score * 100)),
            "category": "assumptions",
            "detail": data_quality_detail,
        },
        {
            "label": "Analog stability",
            "score": int(round(analog_stability * 100)),
            "category": "assumptions",
            "detail": f"{analog_count} analog(s), top similarity {top_similarity:.2f}, pattern score {pattern_score:.2f}, dispersion {analog_dispersion:.2f}.",
        },
        {
            "label": "Relational memory",
            "score": int(round(relational_strength * 100)),
            "category": "assumptions",
            "detail": f"Relationship graph confidence {graph_confidence:.2f} across {graph_node_count} node(s), {graph_edge_count} edge(s), and {graph_sector_span} sector(s).",
        },
        {
            "label": "Layer agreement",
            "score": int(round(layer_agreement * 100)),
            "category": "assumptions",
            "detail": f"Company, sector, cohort, analog, and global inputs imply conflict {layer_conflict:.2f}; disagreement directly lowers confidence.",
        },
        {
            "label": "Structural stability",
            "score": int(round(structural_stability * 100)),
            "category": "assumptions",
            "detail": (
                f"Structural-break probability {structural_break_score:.2f} is active, so history is treated more cautiously."
                if structural_break_score > 0
                else "No strong structural-break signal is active in the current evidence set."
            ),
        },
        {
            "label": "Scenario discipline",
            "score": int(round((1.0 - scenario_penalty) * 100)),
            "category": "valuation",
            "detail": f"Scenario width multiplier {scenario_width_multiplier:.2f} widens the expected error band when evidence is weak or unstable.",
        },
        {
            "label": "Maintenance freshness",
            "score": int(round(maintenance_freshness * 100)),
            "category": "assumptions",
            "detail": maintenance_detail,
        },
        {
            "label": "Discount-rate sensitivity",
            "score": int(round(discount_rate_sensitivity * 100)),
            "category": "valuation",
            "detail": f"WACC–terminal growth spread is {spread:.1f}pp; tighter spreads make valuation errors amplify faster.",
        },
    ]

    summary = (
        f"Assumption confidence is {_score_label(assumption_confidence)} at {assumption_score_100}/100, while valuation confidence is "
        f"{_score_label(valuation_confidence)} at {valuation_score_100}/100. Expected valuation error bands are "
        f"about {expected_valuation_error_band['p50']:.1f}% at p50 and {expected_valuation_error_band['p90']:.1f}% at p90; "
        f"the biggest drag is {dominant_risk.lower()}."
    )
    dcf_suitable, suitability_note = _dashboard_suitability(payload)
    warnings = [
        component["detail"]
        for component in components
        if component["score"] <= 45
    ]
    if suitability_note:
        warnings.insert(0, suitability_note)

    return {
        "summary": summary,
        "dominant_risk": dominant_risk,
        "ranking_signal": round(valuation_confidence, 4),
        "assumption_confidence": {
            "score": round(assumption_confidence, 4),
            "score_100": assumption_score_100,
            "label": _score_label(assumption_confidence),
            "expected_error_index": expected_assumption_error,
        },
        "valuation_confidence": {
            "score": round(valuation_confidence, 4),
            "score_100": valuation_score_100,
            "label": _score_label(valuation_confidence),
            "expected_error_pct": expected_valuation_error_band,
        },
        "components": components,
        "dashboard_breakdown": {
            "total": valuation_score_100,
            "grade": _grade_from_score(valuation_score_100)[0],
            "label": _grade_from_score(valuation_score_100)[1],
            "color": _grade_from_score(valuation_score_100)[2],
            "dcf_suitable": dcf_suitable,
            "suitability_note": suitability_note,
            "warnings": warnings[:3],
            "dimensions": [
                {
                    "name": component["label"],
                    "score": component["score"],
                    "max_points": 100,
                    "pct": component["score"],
                    "status": "pass" if component["score"] >= 70 else "warn" if component["score"] >= 45 else "fail",
                    "comment": component["detail"],
                }
                for component in components
            ],
        },
    }


def vintage_multiplier(data_vintage_years: int) -> float:
    return max(1.0, 2.5 - 0.15 * data_vintage_years)


def calibration_multiplier(calibration_confidence: float) -> float:
    return 2.0 - calibration_confidence


def analog_multiplier(analog_confidence: float) -> float:
    return max(0.75, 1.25 - 0.5 * analog_confidence)


def driving_uncertainty_label(
    *,
    data_vintage_years: int,
    calibration_confidence: float,
    structural_break_risk: float,
    macro_uncertainty: float,
    cohort_size: int,
) -> str:
    if structural_break_risk >= 0.4:
        return "structural_risk"
    if macro_uncertainty >= 0.3:
        return "macro"
    if calibration_confidence < 0.45 or cohort_size < _LEARNING_CONFIG.get("min_calibration_observations", 5):
        return "model_bias"
    if data_vintage_years < 10:
        return "thin_data"
    return "macro"


def compute_model_confidence_score(
    *,
    calibration_confidence: float,
    data_vintage_years: int,
    analog_confidence: float,
    structural_break_risk: float,
    macro_uncertainty: float,
    cohort_size: int = 0,
) -> float:
    evidence_depth = _clamp(0.60 * min(cohort_size / 10.0, 1.0) + 0.40 * calibration_confidence)
    data_quality = _clamp(min(data_vintage_years, 15) / 15.0)
    analog_stability = _clamp(0.65 * analog_confidence + 0.35 * (1.0 - min(macro_uncertainty, 1.0)))
    stability = _clamp(1.0 - structural_break_risk)
    score = (
        0.32 * evidence_depth
        + 0.22 * data_quality
        + 0.18 * analog_stability
        + 0.18 * stability
        + 0.10 * (1.0 - macro_uncertainty)
    )
    if cohort_size < _LEARNING_CONFIG.get("min_calibration_observations", 5):
        score = min(score, 0.35)
    return max(0.0, min(1.0, score))


def _interval(point: float, sigma: float, score: float, driver: str) -> ConfidenceInterval:
    return ConfidenceInterval(
        p10=point - Z10 * sigma,
        p25=point - Z25 * sigma,
        p50=point,
        p75=point + Z25 * sigma,
        p90=point + Z10 * sigma,
        confidence_score=score,
        driving_uncertainty=driver,
    )


def compute_intervals(
    calibrated: Any,
    data_vintage_years: int,
    calibration_confidence: float,
    analog_confidence: float,
    *,
    forecast_years: int | None = None,
    structural_break_risk: float = 0.0,
    macro_uncertainty: float = 0.0,
    cohort_size: int = 0,
) -> ConfidenceBundle:
    """Build year-by-year uncertainty bands for key assumptions."""
    periods = forecast_years or max(
        len(getattr(calibrated, "revenue_growth_rates", []) or []),
        len(getattr(calibrated, "ebit_margin_schedule", []) or []),
        7,
    )

    driver = driving_uncertainty_label(
        data_vintage_years=data_vintage_years,
        calibration_confidence=calibration_confidence,
        structural_break_risk=structural_break_risk,
        macro_uncertainty=macro_uncertainty,
        cohort_size=cohort_size,
    )
    overall_score = compute_model_confidence_score(
        calibration_confidence=calibration_confidence,
        data_vintage_years=data_vintage_years,
        analog_confidence=analog_confidence,
        structural_break_risk=structural_break_risk,
        macro_uncertainty=macro_uncertainty,
        cohort_size=cohort_size,
    )

    base_rev_sigma = _LEARNING_CONFIG.get("base_revenue_uncertainty", 0.06)
    base_margin_sigma = _LEARNING_CONFIG.get("base_margin_uncertainty", 0.025)
    base_wacc_sigma = _LEARNING_CONFIG.get("base_wacc_uncertainty", 0.01)
    growth_per_year = _LEARNING_CONFIG.get("uncertainty_growth_per_year", 0.08)
    v_mult = vintage_multiplier(data_vintage_years)
    c_mult = calibration_multiplier(calibration_confidence)
    a_mult = analog_multiplier(analog_confidence)
    scenario_width_multiplier = max(1.0, float(getattr(calibrated, "scenario_width_multiplier", 1.0) or 1.0))

    revenue_points = list(getattr(calibrated, "revenue_growth_rates", []) or [getattr(calibrated, "revenue_growth_adj", 0.0)])
    margin_points = list(getattr(calibrated, "ebit_margin_schedule", []) or [getattr(calibrated, "ebit_margin_adj", 0.0)])
    wacc_point = float(getattr(calibrated, "wacc_adj", 0.10))
    terminal_growth_point = float(getattr(calibrated, "terminal_growth_adj", getattr(calibrated, "long_run_growth", 0.025)))

    intervals: dict[str, list[ConfidenceInterval]] = {
        "revenue_growth": [],
        "ebit_margin": [],
        "wacc": [],
        "terminal_growth": [],
    }
    for index in range(periods):
        horizon = index + 1
        multiplier = ((1.0 + growth_per_year) ** horizon) * v_mult * c_mult * a_mult * scenario_width_multiplier
        revenue_point = revenue_points[index] if index < len(revenue_points) else revenue_points[-1]
        margin_point = margin_points[index] if index < len(margin_points) else margin_points[-1]
        intervals["revenue_growth"].append(_interval(revenue_point, base_rev_sigma * multiplier, overall_score, driver))
        intervals["ebit_margin"].append(_interval(margin_point, base_margin_sigma * multiplier, overall_score, driver))
        intervals["wacc"].append(_interval(wacc_point, base_wacc_sigma * multiplier, overall_score, driver))
        intervals["terminal_growth"].append(_interval(terminal_growth_point, (base_wacc_sigma * 0.5) * multiplier, overall_score, driver))

    return ConfidenceBundle(intervals=intervals, overall_score=overall_score, driving_uncertainty=driver)


def run_learning_monte_carlo(
    base_dcf_kwargs: dict[str, Any],
    *,
    samples: int | None = None,
    seed: int | None = None,
    net_debt: float = 0.0,
    shares_mm: float = 0.0,
    confidence_bundle: ConfidenceBundle | None = None,
) -> MonteCarloSummary:
    """Reuse the existing Monte Carlo engine but package it for learning outputs."""
    from auto_valuation.sensitivity.analysis import run_monte_carlo_dcf

    samples = int(samples or _LEARNING_CONFIG.get("monte_carlo_samples", 1000))
    seed = int(seed if seed is not None else _LEARNING_CONFIG.get("monte_carlo_seed", 42))

    wacc_std = _LEARNING_CONFIG.get("base_wacc_uncertainty", 0.01)
    growth_std = _LEARNING_CONFIG.get("base_revenue_uncertainty", 0.06)
    margin_std = _LEARNING_CONFIG.get("base_margin_uncertainty", 0.025)
    terminal_g_std = _LEARNING_CONFIG.get("base_wacc_uncertainty", 0.01) / 2.0

    overall_score = 0.0
    if confidence_bundle is not None:
        overall_score = confidence_bundle.overall_score
        revenue_bands = confidence_bundle.intervals.get("revenue_growth", [])
        margin_bands = confidence_bundle.intervals.get("ebit_margin", [])
        wacc_bands = confidence_bundle.intervals.get("wacc", [])
        terminal_bands = confidence_bundle.intervals.get("terminal_growth", [])
        if revenue_bands:
            growth_std = abs(revenue_bands[0].p90 - revenue_bands[0].p10) / (2.0 * Z10)
        if margin_bands:
            margin_std = abs(margin_bands[0].p90 - margin_bands[0].p10) / (2.0 * Z10)
        if wacc_bands:
            wacc_std = abs(wacc_bands[0].p90 - wacc_bands[0].p10) / (2.0 * Z10)
        if terminal_bands:
            terminal_g_std = abs(terminal_bands[0].p90 - terminal_bands[0].p10) / (2.0 * Z10)

    mc = run_monte_carlo_dcf(
        base_dcf_kwargs,
        n_simulations=samples,
        wacc_std=wacc_std,
        growth_std=growth_std,
        margin_std=margin_std,
        terminal_g_std=terminal_g_std,
        net_debt=net_debt,
        shares_mm=shares_mm,
        seed=seed,
    )
    return MonteCarloSummary(
        samples=mc.n_simulations,
        ev_p10=mc.ev_p10,
        ev_p25=mc.ev_p25,
        ev_p50=mc.ev_median,
        ev_p75=mc.ev_p75,
        ev_p90=mc.ev_p90,
        price_p10=mc.price_p10,
        price_p25=mc.price_p25,
        price_p50=mc.price_median,
        price_p75=mc.price_p75,
        price_p90=mc.price_p90,
        overall_score=overall_score,
    )


def compute_confidence_interval(
    point_estimate: float,
    *,
    base_std: float,
    year_index: int,
    data_vintage_years: int,
    calibration_confidence: float,
    analog_confidence: float,
    structural_break_risk: float = 0.0,
    macro_uncertainty: float = 0.0,
    calibration_cohort_size: int | None = None,
    scale_with_value: bool = True,
) -> dict[str, float | str]:
    cohort_size = int(calibration_cohort_size or 0)
    driver = driving_uncertainty_label(
        data_vintage_years=data_vintage_years,
        calibration_confidence=calibration_confidence,
        structural_break_risk=structural_break_risk,
        macro_uncertainty=macro_uncertainty,
        cohort_size=cohort_size,
    )
    score = compute_model_confidence_score(
        calibration_confidence=calibration_confidence,
        data_vintage_years=data_vintage_years,
        analog_confidence=analog_confidence,
        structural_break_risk=structural_break_risk,
        macro_uncertainty=macro_uncertainty,
        cohort_size=cohort_size,
    )
    multiplier = ((1.0 + _LEARNING_CONFIG.get("uncertainty_growth_per_year", 0.08)) ** year_index)
    multiplier *= vintage_multiplier(data_vintage_years)
    multiplier *= calibration_multiplier(calibration_confidence)
    multiplier *= analog_multiplier(analog_confidence)
    sigma = base_std * multiplier
    if scale_with_value:
        sigma *= max(abs(point_estimate), 1.0)
    interval = _interval(point_estimate, sigma, score, driver)
    return {
        "p10": interval.p10,
        "p25": interval.p25,
        "p50": interval.p50,
        "p75": interval.p75,
        "p90": interval.p90,
        "confidence_score": interval.confidence_score,
        "driving_uncertainty": interval.driving_uncertainty,
    }


def _interval_mapping(interval: Any) -> dict[str, float]:
    if isinstance(interval, dict):
        return {
            "p10": float(interval.get("p10", interval.get("p50", 0.0))),
            "p25": float(interval.get("p25", interval.get("p50", 0.0))),
            "p50": float(interval.get("p50", 0.0)),
            "p75": float(interval.get("p75", interval.get("p50", 0.0))),
            "p90": float(interval.get("p90", interval.get("p50", 0.0))),
        }
    return {
        "p10": float(getattr(interval, "p10", getattr(interval, "p50", 0.0))),
        "p25": float(getattr(interval, "p25", getattr(interval, "p50", 0.0))),
        "p50": float(getattr(interval, "p50", 0.0)),
        "p75": float(getattr(interval, "p75", getattr(interval, "p50", 0.0))),
        "p90": float(getattr(interval, "p90", getattr(interval, "p50", 0.0))),
    }


def _ordered_percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * percentile
    lower = int(math.floor(index))
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def run_monte_carlo(
    intervals: dict[str, Any],
    evaluate: Callable[[dict[str, float]], float],
    *,
    samples: int = 1000,
    seed: int = 42,
) -> dict[str, float]:
    rng = random.Random(seed)
    outcomes: list[float] = []
    for _ in range(samples):
        sampled: dict[str, float] = {}
        for key, interval in intervals.items():
            band = _interval_mapping(interval)
            mean_value = band["p50"]
            sigma = abs(band["p90"] - band["p10"]) / (2.0 * Z10) if band["p90"] != band["p10"] else max(abs(mean_value) * 0.01, 1e-6)
            sampled[key] = rng.gauss(mean_value, sigma)
        outcomes.append(float(evaluate(sampled)))

    ordered = sorted(outcomes)
    return {
        "p10": _ordered_percentile(ordered, 0.10),
        "p25": _ordered_percentile(ordered, 0.25),
        "p50": _ordered_percentile(ordered, 0.50),
        "p75": _ordered_percentile(ordered, 0.75),
        "p90": _ordered_percentile(ordered, 0.90),
        "mean": sum(ordered) / len(ordered) if ordered else 0.0,
        "samples": float(len(ordered)),
    }