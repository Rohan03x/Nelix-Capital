"""Weighted knowledge-model bridge for live webapp assumptions."""

from __future__ import annotations

import statistics
from typing import Any

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.assumptions.defaults import (
    get_sector_capex_pct,
    get_sector_ebit_margin,
    get_sector_terminal_sbc_pct,
    get_sector_wc_days,
)
from auto_valuation.assumptions.engine import AssumptionSet
from auto_valuation.assumptions.growth import sector_median_growth
from auto_valuation.assumptions.wacc import blended_beta as _blended_beta
from auto_valuation.data.macro import fetch_damodaran_industry_beta
from auto_valuation.learning._layered_calibrator import CalibrationObservation, calibrate
from auto_valuation.learning.confidence import build_ranked_confidence_model
from auto_valuation.learning.feature_space import SymbolFeatures, build_feature_map, build_symbol_features
from auto_valuation.learning.cross_industry import (
    AnalogSet,
    FEATURE_NAMES,
    PATTERN_LIBRARY,
    build_analog_observations,
    cosine_similarity,
    compute_global_overlay,
    find_analogs,
    match_pattern_library,
)
from auto_valuation.learning.deployment_seed import analog_observations as seeded_analog_observations
from auto_valuation.learning.deployment_seed import cohort_observations as seeded_cohort_observations
from auto_valuation.learning.deployment_seed import historical_replay_summary_observations as seeded_replay_summary_observations
from auto_valuation.learning.historical_replay import get_all_observations as _get_historical_observations
from auto_valuation.learning.ledger import LedgerReader
from auto_valuation.learning.market_implied import build_market_residual_overlay
from auto_valuation.learning.postmortem import should_run_quinquennial
from auto_valuation.learning.quality import assess_prediction_record
from auto_valuation.learning.relationship_graph import build_relationship_graph
from auto_valuation.learning.sampling import stratified_sample_records


_SECTOR_ALIASES = {
    "basic materials": "Materials",
    "consumer cyclical": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "financial services": "Financials",
    "healthcare": "Health Care",
    "technology": "Information Technology",
}


class _LiveCalibrationStore:
    def save_prior(self, _prior: Any) -> None:
        return None


_LIVE_CALIBRATION_STORE = _LiveCalibrationStore()
_LAST_LEARNING_SAMPLE_DIAGNOSTICS: dict[str, Any] = {}

# ── Short-TTL cache for the full ledger query so that three sub-functions
#    within a single refine_live_assumptions() call share one DB round-trip
#    instead of making three separate full-table scans of 22k+ records.
import time as _time_mod
_LEDGER_RECORDS_CACHE: list | None = None
_LEDGER_RECORDS_CACHE_TS: float = 0.0
_LEDGER_RECORDS_CACHE_TTL: float = 30.0  # seconds

# ── Cache stratified-sample results so that _load_learning_cohort and
#    _load_analog_candidates (same params) don't each run assess_prediction_record
#    over all 23k records separately.
_SAMPLED_RECORDS_CACHE: dict[str, list] = {}  # key → (ts, records)
_SAMPLED_RECORDS_CACHE_TS: dict[str, float] = {}
_SAMPLED_RECORDS_CACHE_TTL: float = 30.0  # seconds


def _cached_stratified_sample(records: list, *, max_records: int, target: str) -> list:
    """Run stratified_sample_records with a 30-second result cache keyed on (len, max_records, target)."""
    global _LAST_LEARNING_SAMPLE_DIAGNOSTICS
    import sys as _sys
    # In test mode skip the process-level sample cache so patched LedgerReaders
    # propagate correctly to load-cohort and load-analog functions.
    if "pytest" in _sys.modules:
        sampled = stratified_sample_records(records, max_records=max_records, target=target)
        if target == "full_dcf":
            _LAST_LEARNING_SAMPLE_DIAGNOSTICS = sampled.diagnostics
        return list(sampled.records)
    cache_key = f"{len(records)}:{max_records}:{target}"
    now = _time_mod.monotonic()
    if cache_key in _SAMPLED_RECORDS_CACHE:
        if (now - _SAMPLED_RECORDS_CACHE_TS.get(cache_key, 0.0)) < _SAMPLED_RECORDS_CACHE_TTL:
            return list(_SAMPLED_RECORDS_CACHE[cache_key])
    sampled = stratified_sample_records(records, max_records=max_records, target=target)
    _SAMPLED_RECORDS_CACHE[cache_key] = sampled.records
    _SAMPLED_RECORDS_CACHE_TS[cache_key] = now
    # Also update global sample diagnostics for the full_dcf target.
    if target == "full_dcf":
        _LAST_LEARNING_SAMPLE_DIAGNOSTICS = sampled.diagnostics
    return list(sampled.records)


def _cached_ledger_records(limit: int | None = None) -> list:
    """Return all ledger records, reusing a 30-second in-process cache."""
    global _LEDGER_RECORDS_CACHE, _LEDGER_RECORDS_CACHE_TS
    import sys as _sys
    # In test mode bypass the process-level cache so monkeypatched LedgerReaders
    # are not hidden behind stale real-data entries.
    if "pytest" in _sys.modules:
        try:
            reader = LedgerReader()
            return list(reader.query(limit=limit))
        except Exception:
            return []
    now = _time_mod.monotonic()
    if _LEDGER_RECORDS_CACHE is not None and (now - _LEDGER_RECORDS_CACHE_TS) < _LEDGER_RECORDS_CACHE_TTL:
        return list(_LEDGER_RECORDS_CACHE)
    try:
        reader = LedgerReader()
        records = reader.query(limit=limit)
        _LEDGER_RECORDS_CACHE = records
        _LEDGER_RECORDS_CACHE_TS = now
        return list(records)
    except Exception:
        return []


def _invalidate_ledger_records_cache() -> None:
    global _LEDGER_RECORDS_CACHE, _LEDGER_RECORDS_CACHE_TS
    _LEDGER_RECORDS_CACHE = None
    _LEDGER_RECORDS_CACHE_TS = 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_decimal(value: float | None) -> float:
    if value is None:
        return 0.0
    value = float(value)
    return value / 100 if abs(value) > 1.0 else value


def _safe_mean(values: list[float]) -> float:
    clean = [float(value) for value in values if value is not None]
    return statistics.mean(clean) if clean else 0.0


def _safe_pstdev(values: list[float]) -> float:
    clean = [float(value) for value in values if value is not None]
    return statistics.pstdev(clean) if len(clean) >= 2 else 0.0


def _trimmed_mean(values: list[float]) -> float:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return 0.0
    if len(clean) < 5:
        return _safe_mean(clean)
    ordered = sorted(clean)
    return statistics.mean(ordered[1:-1])


def _learning_pool_limit(default: int = 1000) -> int:
    try:
        return max(int(LEARNING_CONFIG.get("learning_observation_limit", default)), default)
    except Exception:
        return default


def _learning_candidate_limit(target_limit: int) -> int | None:
    configured = LEARNING_CONFIG.get("learning_candidate_pool_limit")
    if configured in (None, 0, ""):
        return None
    try:
        return max(int(configured), int(target_limit))
    except Exception:
        return None


def _classify_macro_regime(rf_rate: float) -> str:
    """Classify macro regime from risk-free rate (as a decimal, e.g. 0.045 = 4.5%).

    Used when persisting predictions and when blending observations so that the
    learning pipeline can distinguish rate environments.
    """
    r = float(rf_rate or 0.0)
    if r >= 0.045:
        return "rising_rates"
    if r <= 0.020:
        return "low_rates"
    return "neutral"


def _derive_actual_wacc(
    predicted_wacc: float | None,
    actual_ufcf_margin: float | None,
    predicted_ufcf_margin: float | None,
    revenue_delta: float,
) -> float:
    """Infer realized WACC from cash-flow and revenue performance vs. prediction.

    Companies that deliver more free cash flow than predicted effectively had
    lower financing risk (lower observed WACC).  Companies that miss deliver
    higher implied risk.  This proxy breaks the always-zero residual that
    occurs when actual_wacc == predicted_wacc.
    """
    pw = float(predicted_wacc or 0.10)
    ufcf_delta = 0.0
    if actual_ufcf_margin is not None and predicted_ufcf_margin is not None:
        ufcf_delta = float(actual_ufcf_margin) - float(predicted_ufcf_margin)
    # UFCF outperformance → risk was lower → actual WACC < predicted WACC
    ufcf_adj = _clamp(ufcf_delta * 0.40, -0.020, 0.020)
    # Revenue outperformance → additional risk compression
    rev_adj = _clamp(revenue_delta * 0.03, -0.015, 0.015)
    return round(_clamp(pw - ufcf_adj - rev_adj, 0.04, 0.30), 4)


def _derive_actual_terminal_growth(
    predicted_terminal_growth: float | None,
    revenue_delta: float,
) -> float:
    """Infer realized implied terminal growth from revenue performance vs. prediction.

    Companies that consistently beat revenue forecasts suggest slightly higher
    long-run sustainable growth than originally predicted.
    """
    ptg = float(predicted_terminal_growth or 0.025)
    adj = _clamp(revenue_delta * 0.015, -0.010, 0.010)
    return round(_clamp(ptg + adj, 0.005, 0.055), 4)


def _derive_actual_beta(predicted_beta: float, revenue_delta: float) -> float:
    """Infer realized beta from revenue outperformance/underperformance.

    Consistent outperformers have lower realized systematic risk (beta
    compression); underperformers exhibit higher sensitivity to market moves.
    """
    adj = _clamp(revenue_delta * 0.20, -0.15, 0.15)
    return round(_clamp(predicted_beta - adj, 0.20, 3.0), 2)


def _growth_rates(revenues: list[float]) -> list[float]:
    rates: list[float] = []
    for idx in range(1, len(revenues)):
        prev = revenues[idx - 1]
        curr = revenues[idx]
        if prev and prev > 0 and curr is not None:
            rates.append(curr / prev - 1)
    return rates


def _rolling_cagr(revenues: list[float], years: int = 5) -> float:
    if len(revenues) < 2:
        return 0.0
    usable = revenues[-(years + 1):] if len(revenues) > years else revenues
    if len(usable) < 2 or usable[0] <= 0 or usable[-1] <= 0:
        return 0.0
    periods = len(usable) - 1
    return (usable[-1] / usable[0]) ** (1.0 / periods) - 1.0


def _history_window_years(revenues: list[float]) -> int:
    # Use ALL available history since IPO — no 5-year cap.
    # Quarterly verification since IPO requires the full track record.
    if len(revenues) < 2:
        return 1
    return max(1, len(revenues) - 1)


def _maturity_bucket(data_vintage_years: int) -> str:
    if data_vintage_years <= 3:
        return "1-3"
    if data_vintage_years <= 10:
        return "4-10"
    if data_vintage_years <= 20:
        return "11-20"
    return "21+"


def _pattern_definition(pattern_name: str | None) -> Any | None:
    if not pattern_name:
        return None
    for pattern in PATTERN_LIBRARY:
        if pattern.name == pattern_name:
            return pattern
    return None


def _overlay_rows(driver_key: str, weights: dict[str, Any]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []

    pattern_overlay = float(weights.get("pattern_overlay_pp") or 0.0)
    if abs(pattern_overlay) >= 0.1:
        overlays.append(
            {
                "label": "Analog pattern",
                "impact": round(pattern_overlay, 1),
                "unit": "pp",
            }
        )

    if driver_key == "beta":
        global_overlay = float(weights.get("global_overlay") or 0.0)
        if abs(global_overlay) >= 0.01:
            overlays.append(
                {
                    "label": "Global brain",
                    "impact": round(global_overlay, 2),
                    "unit": "x",
                }
            )
        relationship_overlay = float(weights.get("relationship_overlay") or 0.0)
        if abs(relationship_overlay) >= 0.01:
            overlays.append(
                {
                    "label": "Relationship graph",
                    "impact": round(relationship_overlay, 2),
                    "unit": "x",
                }
            )
        return overlays

    global_overlay = float(weights.get("global_overlay_pp") or 0.0)
    if abs(global_overlay) >= 0.1:
        overlays.append(
            {
                "label": "Global brain",
                "impact": round(global_overlay, 1),
                "unit": "pp",
            }
        )
    relationship_overlay = float(weights.get("relationship_overlay_pp") or 0.0)
    if abs(relationship_overlay) >= 0.1:
        overlays.append(
            {
                "label": "Relationship graph",
                "impact": round(relationship_overlay, 1),
                "unit": "pp",
            }
        )
    return overlays


def _driver_layer(
    key: str,
    label: str,
    final_value: float,
    unit: str,
    weights: dict[str, Any],
) -> dict[str, Any]:
    display_precision = 2 if unit == "x" else 1
    return {
        "driver": label,
        "final_value": round(float(final_value), display_precision),
        "unit": unit,
        "weights": {
            "company_history": round(float(weights.get("company_history") or 0.0) * 100),
            "sector_prior": round(float(weights.get("sector_prior") or 0.0) * 100),
            "learned_cohort": round(float(weights.get("learned_cohort") or 0.0) * 100),
        },
        "company_anchor": weights.get("company_value"),
        "sector_anchor": weights.get("sector_value"),
        "learned_adjustment": weights.get("learned_value"),
        "overlays": _overlay_rows(key, weights),
        "source": weights.get("source"),
        "warn": weights.get("warn"),
    }


def _obs_value(observation: Any, key: str, default: Any = None) -> Any:
    if isinstance(observation, dict):
        return observation.get(key, default)
    return getattr(observation, key, default)


def _residual_values(observations: list[Any], actual_key: str, predicted_key: str) -> list[float]:
    residuals: list[float] = []
    for observation in observations:
        actual = _obs_value(observation, actual_key)
        predicted = _obs_value(observation, predicted_key)
        if actual is None or predicted is None:
            continue
        residuals.append(float(actual) - float(predicted))
    return residuals


def _observation_evidence_count(observations: list[Any]) -> int:
    total = 0
    for observation in observations:
        try:
            total += max(1, int(_obs_value(observation, "evidence_count", 1) or 1))
        except (TypeError, ValueError):
            total += 1
    return total


def _structural_break_flag(observation: Any) -> bool:
    if bool(_obs_value(observation, "structural_break_flag", False)):
        return True
    if bool(_obs_value(observation, "structural_break_detected", False)):
        return True
    return bool(_obs_value(observation, "structural_break_hints", []) or [])


def _observation_similarity(observation: Any, feature_vector: Any) -> float:
    other_vector = _obs_value(observation, "feature_vector")
    if not feature_vector or not other_vector:
        return 0.0
    try:
        return max(0.0, float(cosine_similarity(feature_vector, other_vector)))
    except Exception:
        return 0.0


def _normalise_layer_scores(scores: dict[str, float]) -> dict[str, float]:
    clean = {key: max(float(value), 0.0) for key, value in scores.items()}
    total = sum(clean.values())
    if total <= 0:
        return {key: 0.0 for key in clean}
    return {key: value / total for key, value in clean.items()}


def _blend_observation_metric(
    layer_observations: dict[str, list[Any]],
    layer_weights: dict[str, float],
    actual_key: str,
    predicted_key: str,
) -> tuple[float, int, float]:
    contributions: list[tuple[float, float, int]] = []
    for layer_name, observations in layer_observations.items():
        residuals = _residual_values(observations, actual_key, predicted_key)
        if not residuals:
            continue
        contributions.append((float(layer_weights.get(layer_name) or 0.0), _trimmed_mean(residuals), _observation_evidence_count(observations)))
    total_weight = sum(weight for weight, _, _ in contributions)
    if total_weight <= 0:
        return 0.0, 0, 0.0
    normalized = [(weight / total_weight, mean_residual, count) for weight, mean_residual, count in contributions]
    adjustment = sum(weight * mean_residual for weight, mean_residual, _ in normalized)
    conflict = _safe_pstdev([mean_residual for _, mean_residual, _ in normalized]) if len(normalized) >= 2 else 0.0
    evidence_count = sum(count for _, _, count in contributions)
    return adjustment, evidence_count, conflict


def _build_layered_learning_snapshot(
    *,
    ticker: str,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    feature_vector: dict[str, float],
    observations: list[Any],
    analog_set: AnalogSet,
    global_learning: dict[str, Any],
    pattern_name: str | None,
    pattern_score: float,
    margin_normalisation: dict[str, Any],
    core_weight_maps: list[dict[str, float]],
    calibrated: Any,
    base_ufcf_margin: float,
    base_reinvestment_rate: float,
) -> dict[str, Any]:
    target_bucket = _maturity_bucket(data_vintage_years)
    ticker_upper = (ticker or "").upper()
    company_observations = [
        observation
        for observation in observations
        if str(_obs_value(observation, "ticker", "") or "").upper() == ticker_upper and ticker_upper
    ]
    cohort_observations = [
        observation
        for observation in observations
        if _knowledge_sector(str(_obs_value(observation, "sector", "") or "")) == sector
        and _maturity_bucket(int(_obs_value(observation, "data_vintage_years", 0) or 0)) == target_bucket
        and str(_obs_value(observation, "market_cap_regime", "") or "") == market_cap_regime
    ]
    # Sector observations: same sector + cap regime, ANY maturity stage.
    # Sector-level behaviour (growth mean, margin drift) is stable across company age;
    # restricting by maturity bucket would leave this layer empty for most tickers.
    sector_observations = [
        observation
        for observation in observations
        if _knowledge_sector(str(_obs_value(observation, "sector", "") or "")) == sector
        and str(_obs_value(observation, "market_cap_regime", "") or "") == market_cap_regime
    ]
    macro_observations = [
        observation
        for observation in observations
        if str(_obs_value(observation, "market_cap_regime", "") or "") == market_cap_regime
        and str(_obs_value(observation, "macro_regime", "") or "") == macro_regime
    ]
    global_observations = list(observations)
    company_evidence_count = _observation_evidence_count(company_observations)
    cohort_evidence_count = _observation_evidence_count(cohort_observations)
    sector_evidence_count = _observation_evidence_count(sector_observations)
    macro_evidence_count = _observation_evidence_count(macro_observations)
    global_evidence_count = _observation_evidence_count(global_observations)

    same_sector_similarities = [_observation_similarity(observation, feature_vector) for observation in sector_observations]
    cross_sector_similarities = [
        _observation_similarity(observation, feature_vector)
        for observation in global_observations
        if _knowledge_sector(str(_obs_value(observation, "sector", "") or "")) != sector
    ]
    same_sector_similarity = max(same_sector_similarities) if same_sector_similarities else 0.0
    cross_sector_similarity = max(cross_sector_similarities) if cross_sector_similarities else 0.0

    flagged_records = sum(1 for observation in global_observations if _structural_break_flag(observation))
    flagged_ratio = flagged_records / len(global_observations) if global_observations else 0.0
    similarity_gap = max(0.0, cross_sector_similarity - same_sector_similarity)
    structural_break_score = 0.0
    structural_break_reasons: list[str] = []
    if flagged_ratio > 0:
        structural_break_score = max(structural_break_score, flagged_ratio)
        structural_break_reasons.append(
            f"{flagged_records} realised observation(s) already carry structural-break hints in the learning ledger."
        )
    if margin_normalisation.get("applied"):
        margin_signal = 0.55 if float(margin_normalisation.get("scenario_width_multiplier") or 1.0) > 1.5 else 0.35
        structural_break_score = max(structural_break_score, margin_signal)
        structural_break_reasons.append(str(margin_normalisation.get("note") or "Margin history is unstable versus the recent base."))
    if pattern_name == "DISRUPTED_INCUMBENT" and pattern_score >= 0.7:
        structural_break_score = max(structural_break_score, min(1.0, 0.45 + 0.35 * pattern_score))
        structural_break_reasons.append("The active analog pattern resembles a disrupted incumbent regime rather than a stable continuation.")
    if similarity_gap > 0.08 and len(sector_observations) >= 3:
        structural_break_score = max(structural_break_score, _clamp((similarity_gap - 0.08) / 0.22, 0.0, 1.0))
        structural_break_reasons.append(
            f"Cross-sector analog similarity ({cross_sector_similarity:.2f}) is overtaking same-sector similarity ({same_sector_similarity:.2f})."
        )
    structural_break_detected = structural_break_score >= 0.45

    avg_company_weight = _safe_mean([weights.get("company_history", 0.0) for weights in core_weight_maps])
    avg_sector_weight = _safe_mean([weights.get("sector_prior", 0.0) for weights in core_weight_maps])
    avg_cohort_weight = _safe_mean([weights.get("learned_cohort", 0.0) for weights in core_weight_maps])
    analog_signal = _clamp(
        0.04 + float(analog_set.analog_confidence or 0.0) * 0.18 + max(pattern_score - 0.65, 0.0) * 0.15,
        0.0,
        0.28,
    )
    macro_signal = _clamp(
        0.03 + min(macro_evidence_count / 8.0, 1.0) * 0.10 + float(global_learning.get("confidence") or 0.0) * 0.06,
        0.0,
        0.20,
    )
    global_signal = _clamp(
        0.02 + (float(global_learning.get("confidence") or 0.0) * 0.14 if global_learning.get("enabled") else 0.02),
        0.0,
        0.18,
    )
    layer_weights = _normalise_layer_scores(
        {
            "company_memory": avg_company_weight * (0.25 if not company_observations else 1.0) * (1.0 - 0.35 * structural_break_score),
            "sector_memory": avg_sector_weight * (1.0 + 0.10 * structural_break_score),
            "cohort_memory": avg_cohort_weight * (0.35 if not cohort_observations else 1.0) * (1.0 - 0.20 * structural_break_score),
            "analog_memory": analog_signal * (1.0 + 0.45 * structural_break_score),
            "macro_memory": macro_signal * (1.0 + 0.25 * structural_break_score),
            "global_memory": global_signal * (1.0 + 0.35 * structural_break_score),
        }
    )
    layer_observations = {
        "company_memory": company_observations,
        "cohort_memory": cohort_observations,
        "sector_memory": sector_observations,
        "macro_memory": macro_observations,
        "global_memory": global_observations,
    }
    revenue_conflict = _safe_pstdev(
        [
            _trimmed_mean(values)
            for values in [
                _residual_values(company_observations, "actual_revenue_growth", "predicted_revenue_growth"),
                _residual_values(cohort_observations, "actual_revenue_growth", "predicted_revenue_growth"),
                _residual_values(sector_observations, "actual_revenue_growth", "predicted_revenue_growth"),
                _residual_values(macro_observations, "actual_revenue_growth", "predicted_revenue_growth"),
                _residual_values(global_observations, "actual_revenue_growth", "predicted_revenue_growth"),
            ]
            if values
        ]
    )
    margin_conflict = _safe_pstdev(
        [
            _trimmed_mean(values)
            for values in [
                _residual_values(company_observations, "actual_ebit_margin", "predicted_ebit_margin"),
                _residual_values(cohort_observations, "actual_ebit_margin", "predicted_ebit_margin"),
                _residual_values(sector_observations, "actual_ebit_margin", "predicted_ebit_margin"),
                _residual_values(macro_observations, "actual_ebit_margin", "predicted_ebit_margin"),
                _residual_values(global_observations, "actual_ebit_margin", "predicted_ebit_margin"),
            ]
            if values
        ]
    )
    conflict_score = max(revenue_conflict, margin_conflict)
    weak_evidence = company_evidence_count == 0 and cohort_evidence_count < 5
    effective_confidence = _clamp(
        float(calibrated.calibration_confidence or 0.0)
        * (0.72 if weak_evidence else 1.0)
        * (1.0 - 0.35 * structural_break_score),
        0.0,
        1.0,
    )
    scenario_width_multiplier = round(
        _clamp(
            max(
                float(margin_normalisation.get("scenario_width_multiplier") or 1.0),
                1.0 + (0.30 if weak_evidence else 0.0) + (0.55 * structural_break_score) + min(conflict_score / 0.03, 0.45),
            ),
            1.0,
            2.5,
        ),
        2,
    )
    ufcf_adjustment, ufcf_evidence, ufcf_conflict = _blend_observation_metric(
        layer_observations,
        layer_weights,
        "actual_ufcf_margin",
        "predicted_ufcf_margin",
    )
    reinvestment_adjustment, reinvestment_evidence, reinvestment_conflict = _blend_observation_metric(
        layer_observations,
        layer_weights,
        "actual_reinvestment_rate",
        "predicted_reinvestment_rate",
    )
    learned_ufcf_margin = _clamp(base_ufcf_margin + ufcf_adjustment, -0.25, 0.35)
    learned_reinvestment_rate = _clamp(base_reinvestment_rate + reinvestment_adjustment, 0.0, 0.25)
    ufcf_confidence = _clamp(
        min(1.0, ufcf_evidence / 10.0) * (1.0 - min(ufcf_conflict / 0.04, 0.75)),
        0.0,
        1.0,
    )
    reinvestment_confidence = _clamp(
        min(1.0, reinvestment_evidence / 10.0) * (1.0 - min(reinvestment_conflict / 0.04, 0.75)),
        0.0,
        1.0,
    )

    def _layer_snapshot(layer_name: str, records: int, note: str, enabled: bool = True) -> dict[str, Any]:
        return {
            "weight_pct": round(float(layer_weights.get(layer_name) or 0.0) * 100),
            "records": records,
            "enabled": enabled,
            "note": note,
        }

    return {
        "layer_mix": {
            "company_memory": _layer_snapshot(
                "company_memory",
                company_evidence_count,
                "Same-ticker realised history feeds the company-memory layer when prior forecasts have matured.",
                enabled=bool(company_observations),
            ),
            "sector_memory": _layer_snapshot(
                "sector_memory",
                sector_evidence_count,
                "Sector priors remain the stabilising layer when company-specific evidence is thin or noisy.",
                enabled=bool(sector_observations),
            ),
            "cohort_memory": _layer_snapshot(
                "cohort_memory",
                cohort_evidence_count,
                "Matched realised cohorts contribute residual corrections rather than replacing the base model wholesale.",
                enabled=bool(cohort_observations),
            ),
            "analog_memory": {
                **_layer_snapshot(
                    "analog_memory",
                    len(analog_set.analogs),
                    "Analog history comes from similar operating fingerprints and pattern matches across symbols.",
                    enabled=bool(analog_set.analogs),
                ),
                "confidence": round(float(analog_set.analog_confidence or 0.0), 2),
                "pattern_name": pattern_name,
            },
            "macro_memory": _layer_snapshot(
                "macro_memory",
                macro_evidence_count,
                f"Macro memory reuses realised records from the same {macro_regime} regime and {market_cap_regime} cap bucket.",
                enabled=bool(macro_observations),
            ),
            "global_memory": {
                **_layer_snapshot(
                    "global_memory",
                    global_evidence_count,
                    global_learning.get("note") or "Global cross-symbol memory stays as a low-conviction stabiliser until enough evidence accrues.",
                    enabled=bool(global_learning.get("enabled")),
                ),
                "scope": global_learning.get("scope"),
                "sector_span": int(global_learning.get("sector_span") or 0),
                "confidence": round(float(global_learning.get("confidence") or 0.0), 2),
            },
        },
        "structural_break": {
            "detected": structural_break_detected,
            "score": round(structural_break_score, 2),
            "flagged_records": flagged_records,
            "same_sector_similarity": round(same_sector_similarity, 3),
            "cross_sector_similarity": round(cross_sector_similarity, 3),
            "reasons": structural_break_reasons,
            "note": (
                "Structural-break risk is active, so scenario widths are widened and historical anchors are treated more cautiously."
                if structural_break_detected
                else "No strong structural-break signal is active in the current layered evidence set."
            ),
        },
        "uncertainty": {
            "weak_evidence": weak_evidence,
            "conflict_score": round(conflict_score, 4),
            "effective_confidence": round(effective_confidence, 2),
            "scenario_width_multiplier": scenario_width_multiplier,
            "note": (
                "Weak or conflicting evidence lowers confidence and widens ranges instead of forcing precision."
                if weak_evidence or conflict_score > 0.01 or structural_break_detected
                else "Evidence quality is stable enough that the learning layer can stay relatively tight."
            ),
        },
        "learned_metrics": {
            "ufcf_margin_pct": round(learned_ufcf_margin * 100, 1),
            "ufcf_margin_adjustment_pp": round(ufcf_adjustment * 100, 1),
            "ufcf_margin_confidence": round(ufcf_confidence, 2),
            "ufcf_margin_evidence": ufcf_evidence,
            "reinvestment_rate_pct": round(learned_reinvestment_rate * 100, 1),
            "reinvestment_adjustment_pp": round(reinvestment_adjustment * 100, 1),
            "reinvestment_confidence": round(reinvestment_confidence, 2),
            "reinvestment_evidence": reinvestment_evidence,
            "note": "Cashflow-side learning is derived from realised UFCF and reinvestment proxies when those labels exist in the ledger.",
        },
    }


def _build_data_gaps(
    *,
    history_window_years: int,
    review_due: bool,
    calibration_cohort_size: int,
    global_learning: dict[str, Any],
    pattern_name: str | None,
    pattern_score: float,
    margin_normalisation: dict[str, Any],
    layered_learning: dict[str, Any],
) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    uncertainty = dict(layered_learning.get("uncertainty") or {})
    structural_break = dict(layered_learning.get("structural_break") or {})

    if history_window_years < 4:
        gaps.append(
            {
                "title": "Short company memory",
                "detail": f"Only {history_window_years} year(s) of history are carrying the company-memory layer, so sector priors still anchor more of the forecast.",
                "severity": "amber",
            }
        )

    if calibration_cohort_size < 5:
        gaps.append(
            {
                "title": "Realised cohort is still thin",
                "detail": f"Only {calibration_cohort_size} matching realised records are available, so learned cohort weights stay deliberately small.",
                "severity": "amber",
            }
        )

    if not global_learning.get("enabled"):
        gaps.append(
            {
                "title": "Global brain is not fully engaged",
                "detail": "Cross-symbol overlays stay off until the market-wide realised cohort is broad enough by regime and sector span.",
                "severity": "amber",
            }
        )

    if not pattern_name or pattern_score < 0.7:
        gaps.append(
            {
                "title": "Analog evidence is weak",
                "detail": "No high-conviction analog pattern matched this ticker, so the model leans more heavily on company and sector memory.",
                "severity": "amber",
            }
        )

    if margin_normalisation.get("applied"):
        gaps.append(
            {
                "title": "Margin history is unstable",
                "detail": str(margin_normalisation.get("note") or "Margin volatility forced a more conservative normalisation anchor."),
                "severity": "red" if float(margin_normalisation.get("scenario_width_multiplier") or 1.0) > 1.5 else "amber",
            }
        )

    if bool(uncertainty.get("weak_evidence")):
        gaps.append(
            {
                "title": "Layered evidence is still thin",
                "detail": "The live learning engine is lowering confidence and widening ranges because the company/cohort evidence base is still sparse.",
                "severity": "amber",
            }
        )

    if bool(structural_break.get("detected")):
        gaps.append(
            {
                "title": "Structural break risk is active",
                "detail": str(structural_break.get("note") or "Recent evidence suggests the business may be shifting regimes."),
                "severity": "red" if float(structural_break.get("score") or 0.0) >= 0.65 else "amber",
            }
        )

    quality_gate = dict(layered_learning.get("quality_gate") or {})
    if quality_gate and int(quality_gate.get("excluded_rows") or 0) > 0:
        gaps.append(
            {
                "title": "Training rows were quality-gated",
                "detail": f"{int(quality_gate.get('excluded_rows') or 0)} ledger row(s) were excluded from full DCF calibration before this request because their targets were invalid or restricted.",
                "severity": "amber",
            }
        )

    market_residual = dict(layered_learning.get("market_residual_overlay") or {})
    if not market_residual.get("enabled"):
        gaps.append(
            {
                "title": "Market residual evidence is not active",
                "detail": str(market_residual.get("reason") or "EV/price residual learning needs more quality-gated annual outcomes before it can move the point estimate."),
                "severity": "amber",
            }
        )

    # Quarterly verification is always active — no gap to flag for review cadence.

    return gaps


def _build_learning_explainability(
    *,
    history_window_years: int,
    completed_years: int,
    review_due: bool,
    next_review_in_years: int,
    calibrated: Any,
    analog_payload: dict[str, Any],
    global_learning: dict[str, Any],
    pattern_name: str | None,
    pattern_score: float,
    margin_normalisation: dict[str, Any],
    assumption_weights: dict[str, dict[str, Any]],
    layered_learning: dict[str, Any],
    refined_growth: float,
    refined_margin_target: float,
    refined_wacc: float,
    refined_terminal_growth: float,
    smoothed_beta: float,
    relationship_graph: dict[str, Any],
) -> dict[str, Any]:
    layer_mix = dict(layered_learning.get("layer_mix") or {})
    company_mix = dict(layer_mix.get("company_memory") or {})
    sector_mix = dict(layer_mix.get("sector_memory") or {})
    cohort_mix = dict(layer_mix.get("cohort_memory") or {})
    analog_mix = dict(layer_mix.get("analog_memory") or {})
    macro_mix = dict(layer_mix.get("macro_memory") or {})
    global_mix = dict(layer_mix.get("global_memory") or {})
    structural_break = dict(layered_learning.get("structural_break") or {})
    uncertainty = dict(layered_learning.get("uncertainty") or {})
    learned_metrics = dict(layered_learning.get("learned_metrics") or {})
    quality_gate = dict(layered_learning.get("quality_gate") or {})
    market_residual = dict(layered_learning.get("market_residual_overlay") or {})

    avg_company_weight = int(company_mix.get("weight_pct") or 0)
    avg_sector_weight = int(sector_mix.get("weight_pct") or 0)
    avg_learned_weight = int(cohort_mix.get("weight_pct") or 0)

    pattern = _pattern_definition(pattern_name)
    pattern_label = pattern_name.replace("_", " ").title() if pattern_name else None
    analog_items = list(analog_payload.get("items") or [])
    analog_pattern_enabled = bool(pattern and pattern_score >= 0.7)
    analog_enabled = analog_pattern_enabled or bool(analog_items)
    nearest_tickers = ", ".join(
        str(item.get("ticker") or "")
        for item in analog_items[:3]
        if item.get("ticker")
    )
    if analog_items and analog_pattern_enabled:
        analog_note = (
            f"{len(analog_items)} live analog match(s) are active. "
            f"Pattern analog {pattern_label} reinforces them with score {pattern_score:.2f}."
        )
        if nearest_tickers:
            analog_note = f"{analog_note} Nearest analogs: {nearest_tickers}."
    elif analog_items:
        analog_note = f"{len(analog_items)} live analog match(s) are active."
        if nearest_tickers:
            analog_note = f"{analog_note} Nearest analogs: {nearest_tickers}."
    elif analog_pattern_enabled:
        analog_note = f"Pattern analog {pattern_label} is active with score {pattern_score:.2f}."
    else:
        analog_note = "No high-conviction analog pattern is active; analog evidence remains descriptive rather than directive."

    analog_matches: list[dict[str, Any]] = []
    for item in analog_items[:3]:
        evidence_rows = [
            {
                "label": str(evidence.get("label") or evidence.get("dimension") or "Signal"),
                "similarity": round(float(evidence.get("similarity") or 0.0), 2),
                "subject": evidence.get("subject"),
                "analog": evidence.get("analog"),
                "bucket": evidence.get("bucket"),
            }
            for evidence in list(item.get("evidence") or [])[:3]
        ]
        evidence_summary = ", ".join(
            f"{evidence['label']} {evidence['subject']} vs {evidence['analog']}"
            for evidence in evidence_rows[:2]
            if evidence.get("subject") not in (None, "") and evidence.get("analog") not in (None, "")
        )
        analog_matches.append(
            {
                "ticker": item.get("ticker"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "score": round(float(item.get("score") or 0.0), 3),
                "similarity": round(float(item.get("similarity") or 0.0), 3),
                "static_similarity": round(float(item.get("static_similarity") or 0.0), 3),
                "regime_similarity": round(float(item.get("regime_similarity") or 0.0), 3),
                "maturity_stage": item.get("maturity_stage"),
                "valuation_regime": item.get("valuation_regime"),
                "volatility_regime": item.get("volatility_regime"),
                "evidence": evidence_rows,
                "why_it_matters": evidence_summary or "Matched on operating fingerprint and regime alignment.",
                "weights": {
                    "recency": round(float((item.get("weights") or {}).get("recency") or 0.0), 2),
                    "data_quality": round(float((item.get("weights") or {}).get("data_quality") or 0.0), 2),
                    "sample": round(float((item.get("weights") or {}).get("sample") or 0.0), 2),
                    "usefulness": round(float((item.get("weights") or {}).get("usefulness") or 0.0), 2),
                },
            }
        )

    confidence_components = [
        {
            "label": "Company memory",
            "score": avg_company_weight,
            "detail": f"{history_window_years}y of operating history and {int(company_mix.get('records') or 0)} matured ticker-specific record(s) support the company layer.",
        },
        {
            "label": "Cohort memory",
            "score": round(float(uncertainty.get("effective_confidence") or calibrated.calibration_confidence or 0.0) * 100),
            "detail": f"{int(cohort_mix.get('records') or calibrated.calibration_cohort_size or 0)} realised records feed residual cohort corrections.",
        },
        {
            "label": "Analog evidence",
            "score": max(round((pattern_score if analog_enabled else min(pattern_score, 0.4)) * 100), int(analog_mix.get("weight_pct") or 0)),
            "detail": analog_note,
        },
        {
            "label": "Global brain",
            "score": round(float(global_learning.get("confidence") or 0.0) * 100),
            "detail": global_learning.get("note") or "Global cross-symbol overlays are inactive until enough realised evidence exists.",
        },
        {
            "label": "Market residual",
            "score": round(float(market_residual.get("confidence") or 0.0) * 100),
            "detail": market_residual.get("note") or market_residual.get("reason") or "Market-implied EV/price residual learning is waiting for enough quality-gated annual outcomes.",
        },
        {
            "label": "Regime stability",
            "score": round((1.0 - float(structural_break.get("score") or 0.0)) * 100),
            "detail": structural_break.get("note") or "No structural-break warning is active.",
        },
    ]

    data_gaps = _build_data_gaps(
        history_window_years=history_window_years,
        review_due=review_due,
        calibration_cohort_size=calibrated.calibration_cohort_size,
        global_learning=global_learning,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        margin_normalisation=margin_normalisation,
        layered_learning=layered_learning,
    )

    review_window = "quarterly"
    headline = (
        f"Core forecast mix: {avg_company_weight}% company memory, {avg_sector_weight}% sector prior, "
        f"{avg_learned_weight}% realised cohort learning, {int(analog_mix.get('weight_pct') or 0)}% analog memory, "
        f"and {int(global_mix.get('weight_pct') or 0)}% global memory. Verified quarterly since IPO."
    )

    return {
        "headline": headline,
        "company_memory": {
            "history_window_years": history_window_years,
            "completed_years": completed_years,
            "weight_pct": avg_company_weight,
            "records": int(company_mix.get("records") or 0),
            "review_due": review_due,
            "next_review_in_years": next_review_in_years,
            "note": company_mix.get("note") or f"Company history remains the primary anchor because the business has {completed_years} completed year(s) on file.",
        },
        "sector_memory": {
            "weight_pct": int(sector_mix.get("weight_pct") or 0),
            "records": int(sector_mix.get("records") or 0),
            "note": sector_mix.get("note"),
        },
        "cohort_memory": {
            "records": int(calibrated.calibration_cohort_size or cohort_mix.get("records") or 0),
            "confidence": round(float(uncertainty.get("effective_confidence") or calibrated.calibration_confidence or 0.0), 2),
            "weight_pct": avg_learned_weight,
            "note": cohort_mix.get("note") or "The learned cohort only nudges assumptions when realised records are broad enough to justify it.",
        },
        "analog_evidence": {
            "enabled": analog_enabled,
            "pattern_enabled": analog_pattern_enabled,
            "pattern_name": pattern_name,
            "pattern_label": pattern_label,
            "pattern_score": round(pattern_score, 2),
            "weight_pct": int(analog_mix.get("weight_pct") or 0),
            "confidence": round(float(analog_mix.get("confidence") or 0.0), 2),
            "match_count": int(analog_payload.get("count") or analog_mix.get("records") or 0),
            "archetypes": list(pattern.archetypes[:3]) if pattern else [],
            "top_matches": analog_matches,
            "cohorts": list(analog_payload.get("cohorts") or [])[:3],
            "overlay": dict(analog_payload.get("overlay") or {}),
            "note": analog_note,
        },
        "analog_memory": {
            "enabled": bool(analog_mix.get("enabled")),
            "weight_pct": int(analog_mix.get("weight_pct") or 0),
            "records": int(analog_mix.get("records") or 0),
            "pattern_name": analog_mix.get("pattern_name"),
            "confidence": round(float(analog_mix.get("confidence") or 0.0), 2),
            "note": analog_mix.get("note") or analog_note,
        },
        "macro_memory": {
            "weight_pct": int(macro_mix.get("weight_pct") or 0),
            "records": int(macro_mix.get("records") or 0),
            "enabled": bool(macro_mix.get("enabled")),
            "note": macro_mix.get("note"),
        },
        "global_brain": {
            "enabled": bool(global_learning.get("enabled")),
            "scope": global_learning.get("scope"),
            "cohort_size": int(global_learning.get("cohort_size") or 0),
            "sector_span": int(global_learning.get("sector_span") or 0),
            "confidence": round(float(global_learning.get("confidence") or 0.0), 2),
            "weight_pct": int(global_mix.get("weight_pct") or 0),
            "overlays": [
                {"driver": "Revenue Growth", "impact": float(global_learning.get("revenue_growth_adj_pp") or 0.0), "unit": "pp"},
                {"driver": "EBIT Margin", "impact": float(global_learning.get("ebit_margin_adj_pp") or 0.0), "unit": "pp"},
                {"driver": "WACC", "impact": float(global_learning.get("wacc_adj_pp") or 0.0), "unit": "pp"},
                {"driver": "Terminal Growth", "impact": float(global_learning.get("terminal_growth_adj_pp") or 0.0), "unit": "pp"},
                {"driver": "Beta", "impact": float(global_learning.get("beta_adj") or 0.0), "unit": "x"},
            ],
            "note": global_learning.get("note") or "Global cross-symbol overlays are currently inactive.",
        },
        "global_memory": {
            "enabled": bool(global_mix.get("enabled")),
            "weight_pct": int(global_mix.get("weight_pct") or 0),
            "records": int(global_mix.get("records") or 0),
            "scope": global_mix.get("scope"),
            "sector_span": int(global_mix.get("sector_span") or 0),
            "confidence": round(float(global_mix.get("confidence") or 0.0), 2),
            "note": global_mix.get("note"),
        },
        "quality_gate": quality_gate,
        "market_residual_overlay": market_residual,
        "relationship_graph": relationship_graph,
        "structural_break": structural_break,
        "uncertainty": uncertainty,
        "learned_metrics": learned_metrics,
        "forecast_layers": [
            _driver_layer("revenue_growth_near", "Revenue Growth", refined_growth, "%", assumption_weights["revenue_growth_near"]),
            _driver_layer("ebit_margin_target", "EBIT Margin", refined_margin_target, "%", assumption_weights["ebit_margin_target"]),
            _driver_layer("wacc", "WACC", refined_wacc, "%", assumption_weights["wacc"]),
            _driver_layer("terminal_growth", "Terminal Growth", refined_terminal_growth, "%", assumption_weights["terminal_growth"]),
            _driver_layer("beta", "Beta", smoothed_beta, "x", assumption_weights["beta"]),
        ],
        "confidence_decomposition": {
            "summary": "Shared-brain confidence explains how much of the forecast is supported by history, realised learning, analogs, macro regime memory, and cross-symbol evidence. Weak or conflicting evidence lowers confidence instead of pretending precision.",
            "components": confidence_components,
        },
        "data_gaps": data_gaps,
        "limitations_note": "When evidence is thin or regimes look unstable, the model deliberately widens ranges and falls back toward more stable anchors instead of forcing overfit learned overlays.",
    }


def _memory_hierarchy_status(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 65:
        return "moderate"
    if score >= 50:
        return "guarded"
    return "low"


def _build_memory_hierarchy(
    *,
    explainability: dict[str, Any],
    confidence_model: dict[str, Any],
    relationship_graph: dict[str, Any],
) -> dict[str, Any]:
    company_memory = dict(explainability.get("company_memory") or {})
    sector_memory = dict(explainability.get("sector_memory") or {})
    cohort_memory = dict(explainability.get("cohort_memory") or {})
    analog_memory = dict(explainability.get("analog_memory") or {})
    macro_memory = dict(explainability.get("macro_memory") or {})
    global_memory = dict(explainability.get("global_memory") or {})
    uncertainty = dict(explainability.get("uncertainty") or {})

    episodic_score = int(
        round(
            _clamp(
                0.70 * (float(company_memory.get("weight_pct") or 0.0) / 100.0)
                + 0.30 * min(float(company_memory.get("records") or 0.0) / 3.0, 1.0),
                0.0,
                1.0,
            )
            * 100
        )
    )
    semantic_score = int(
        round(
            _clamp(
                0.42 * (float(sector_memory.get("weight_pct") or 0.0) / 100.0)
                + 0.40 * (float(cohort_memory.get("weight_pct") or 0.0) / 100.0)
                + 0.18 * (float(macro_memory.get("weight_pct") or 0.0) / 100.0),
                0.0,
                1.0,
            )
            * 100
        )
    )
    relational_score = int(
        round(
            _clamp(
                0.32 * (float(analog_memory.get("weight_pct") or 0.0) / 100.0)
                + 0.24 * (float(global_memory.get("weight_pct") or 0.0) / 100.0)
                + 0.44 * float(relationship_graph.get("confidence") or 0.0),
                0.0,
                1.0,
            )
            * 100
        )
    )
    procedural_score = int(
        round(
            _clamp(
                0.45 * float((confidence_model.get("assumption_confidence") or {}).get("score") or 0.0)
                + 0.30 * float((confidence_model.get("valuation_confidence") or {}).get("score") or 0.0)
                + 0.25 * (1.0 - min(float(uncertainty.get("conflict_score") or 0.0) / 0.03, 1.0)),
                0.0,
                1.0,
            )
            * 100
        )
    )

    layers = [
        {
            "key": "episodic",
            "label": "Episodic Memory",
            "score": episodic_score,
            "status": _memory_hierarchy_status(episodic_score),
            "note": company_memory.get("note") or "Ticker-specific history and matured postmortems anchor the episodic layer.",
            "sources": ["company history", "matured ticker records"],
        },
        {
            "key": "semantic",
            "label": "Semantic Memory",
            "score": semantic_score,
            "status": _memory_hierarchy_status(semantic_score),
            "note": "Sector priors, matched cohorts, and macro regime priors form the reusable knowledge layer.",
            "sources": ["sector priors", "cohort residuals", "macro regime"],
        },
        {
            "key": "relational",
            "label": "Relational Memory",
            "score": relational_score,
            "status": _memory_hierarchy_status(relational_score),
            "note": relationship_graph.get("note") or "Cross-symbol links are still too thin to contribute much relational memory.",
            "sources": ["analogs", "global overlays", "relationship graph"],
            "connected_tickers": list(relationship_graph.get("connected_tickers") or [])[:5],
        },
        {
            "key": "procedural",
            "label": "Procedural Memory",
            "score": procedural_score,
            "status": _memory_hierarchy_status(procedural_score),
            "note": "Confidence scoring and range widening define how the brain acts when evidence conflicts or regimes shift.",
            "sources": ["confidence model", "scenario width", "conflict controls"],
            "scenario_width_multiplier": float(uncertainty.get("scenario_width_multiplier") or 1.0),
        },
    ]

    return {
        "summary": "Memory is split across episodic ticker history, semantic sector/cohort knowledge, relational cross-symbol links, and procedural confidence controls.",
        "episodic": dict(layers[0]),
        "semantic": dict(layers[1]),
        "relational": dict(layers[2]),
        "procedural": dict(layers[3]),
        "layers": layers,
    }

def _normalise_weights(company_weight: float, sector_weight: float, learning_weight: float) -> dict[str, float]:
    total = company_weight + sector_weight + learning_weight
    if total <= 0:
        return {"company_history": 1.0, "sector_prior": 0.0, "learned_cohort": 0.0}
    return {
        "company_history": company_weight / total,
        "sector_prior": sector_weight / total,
        "learned_cohort": learning_weight / total,
    }


def _market_cap_regime(market_cap_m: float) -> str:
    if market_cap_m < 2_000:
        return "small"
    if market_cap_m < 10_000:
        return "mid"
    return "large"


def _knowledge_sector(sector: str) -> str:
    cleaned = (sector or "").strip()
    if not cleaned:
        return "Default"
    return _SECTOR_ALIASES.get(cleaned.lower(), cleaned)


def _trailing_ratio_pct(numerators: list[float], denominators: list[float], *, years: int = 5, absolute: bool = False) -> float:
    ratios: list[float] = []
    for numerator, denominator in zip(numerators[-years:], denominators[-years:]):
        if denominator and denominator > 0 and numerator is not None:
            value = abs(numerator) if absolute else numerator
            ratios.append(value / denominator * 100)
    return _safe_mean(ratios)


def _normalized_tax_rate_pct(pretax_incomes: list[float], tax_provisions: list[float], fallback: float) -> float:
    weights: list[tuple[float, float]] = []
    for pretax_income, tax_provision in zip(pretax_incomes[-5:], tax_provisions[-5:]):
        if pretax_income and abs(pretax_income) > 1e-9 and tax_provision is not None:
            etr = tax_provision / pretax_income * 100
            if abs(etr) <= 60:
                weights.append((abs(pretax_income), etr))
    if not weights:
        return fallback
    total_weight = sum(weight for weight, _ in weights)
    if total_weight <= 0:
        return fallback
    weighted = sum(weight * etr for weight, etr in weights) / total_weight
    return _clamp(weighted, 5.0, 45.0)


def _build_feature_vector(
    revenues: list[float],
    ebit_margins: list[float],
    gross_margin_base_pct: float,
    capex_pct: float,
    total_assets: float,
    total_debt: float,
    revenue_base: float,
    operating_cf: float,
    fcf: float,
    da_pct: float,
    tax_rate_pct: float,
    market_cap: float,
) -> dict[str, float]:
    return build_feature_map(
        revenues=revenues,
        ebit_margins=ebit_margins,
        gross_margin_base_pct=gross_margin_base_pct,
        capex_pct=capex_pct,
        total_assets=total_assets,
        total_debt=total_debt,
        revenue_base=revenue_base,
        operating_cf=operating_cf,
        fcf=fcf,
        da_pct=da_pct,
        tax_rate_pct=tax_rate_pct,
        market_cap=market_cap,
        market_cap_regime=_market_cap_regime(market_cap),
        macro_regime="neutral",
    )


def _margin_normalisation(ebit_margins: list[float], base_margin_pct: float, target_margin_pct: float) -> dict[str, Any]:
    recent = [float(margin) for margin in ebit_margins[-5:] if margin is not None]
    if not recent:
        return {
            "applied": False,
            "margin_range_pp": 0.0,
            "margin_volatility_pp": 0.0,
            "normalized_margin_pct": round(base_margin_pct, 1),
            "target_anchor_pct": round(target_margin_pct, 1),
            "scenario_width_multiplier": 1.0,
            "note": None,
            "source_suffix": "",
        }

    margin_range_pp = max(recent) - min(recent) if len(recent) >= 2 else 0.0
    margin_volatility_pp = _safe_pstdev(recent)
    normalized_margin_pct = round(_trimmed_mean(recent), 1)
    profitable_regime = [margin for margin in recent if margin > 0]
    profitable_anchor_pct = round(_trimmed_mean(profitable_regime), 1) if profitable_regime else normalized_margin_pct

    if margin_range_pp >= 15.0:
        target_anchor_pct = round(0.65 * profitable_anchor_pct + 0.35 * target_margin_pct, 1)
        scenario_width_multiplier = 1.8
        note = (
            f"Extreme margin volatility detected ({margin_range_pp:.1f}pp range). "
            f"Base target is anchored to a normalised profitable-regime margin of {profitable_anchor_pct:.1f}% and wider scenarios are used."
        )
        source_suffix = f"; normalised profitable-regime anchor {profitable_anchor_pct:.1f}%"
        applied = True
    elif margin_range_pp >= 10.0:
        target_anchor_pct = round(0.55 * normalized_margin_pct + 0.45 * target_margin_pct, 1)
        scenario_width_multiplier = 1.35
        note = (
            f"High margin volatility detected ({margin_range_pp:.1f}pp range). "
            f"Base target is moderated toward a normalised margin anchor of {normalized_margin_pct:.1f}%."
        )
        source_suffix = f"; normalised margin anchor {normalized_margin_pct:.1f}%"
        applied = True
    else:
        target_anchor_pct = round(target_margin_pct, 1)
        scenario_width_multiplier = 1.0
        note = None
        source_suffix = ""
        applied = False

    return {
        "applied": applied,
        "margin_range_pp": round(margin_range_pp, 1),
        "margin_volatility_pp": round(margin_volatility_pp, 1),
        "normalized_margin_pct": normalized_margin_pct,
        "target_anchor_pct": target_anchor_pct,
        "scenario_width_multiplier": scenario_width_multiplier,
        "note": note,
        "source_suffix": source_suffix,
    }


def _declining_regime_guardrail(
    *,
    company_growth_pct: float,
    revenue_growth_near: float,
    ebit_margin_base_pct: float,
    company_margin_target_pct: float,
    wacc: float,
    terminal_growth: float,
    beta: float,
    structural_break: dict[str, Any],
) -> dict[str, Any]:
    structural_break_score = float(structural_break.get("score") or 0.0)
    structural_break_detected = bool(structural_break.get("detected"))
    margin_headroom_pct = max(company_margin_target_pct, ebit_margin_base_pct) - ebit_margin_base_pct
    shrinking_regime = company_growth_pct <= 0.0
    applied = shrinking_regime and (structural_break_detected or margin_headroom_pct <= 1.0)
    if not applied:
        return {
            "applied": False,
            "note": None,
        }

    severe_decline = company_growth_pct <= -2.0 or structural_break_score >= 0.75
    max_margin_target_pct = round(max(company_margin_target_pct, ebit_margin_base_pct), 1)
    max_growth_pct = round(min(max(revenue_growth_near, company_growth_pct), 0.8 if not severe_decline else 0.0), 1)
    return {
        "applied": True,
        "recent_revenue_cagr_pct": round(company_growth_pct, 1),
        "max_margin_target_pct": max_margin_target_pct,
        "max_growth_pct": max_growth_pct,
        "min_wacc_pct": round(wacc, 1),
        "max_terminal_growth_pct": round(terminal_growth, 1),
        "min_beta": round(beta, 2),
        "note": "Declining/structural-break safeguard applied: positive learned memory is capped until revenue momentum improves.",
    }


def _margin_volatility_guardrail(
    *,
    margin_normalisation: dict[str, Any],
    company_margin_target_pct: float,
    ebit_margin_base_pct: float,
    refined_margin_target: float,
) -> dict[str, Any]:
    if not margin_normalisation.get("applied"):
        return {
            "applied": False,
            "note": None,
        }

    scenario_width_multiplier = float(margin_normalisation.get("scenario_width_multiplier") or 1.0)
    allowed_uplift = 0.5 if scenario_width_multiplier > 1.5 else 1.0
    normalized_anchor_pct = round(
        max(
            float(margin_normalisation.get("normalized_margin_pct") or company_margin_target_pct),
            ebit_margin_base_pct,
        ),
        1,
    )
    max_margin_target_pct = round(normalized_anchor_pct + allowed_uplift, 1)
    if refined_margin_target <= max_margin_target_pct:
        return {
            "applied": False,
            "note": None,
        }

    return {
        "applied": True,
        "normalized_anchor_pct": normalized_anchor_pct,
        "max_margin_target_pct": max_margin_target_pct,
        "allowed_uplift_pp": allowed_uplift,
        "note": (
            "Margin-volatility safeguard applied: positive learned overlays are capped near the "
            f"normalised anchor ({normalized_anchor_pct:.1f}% -> {max_margin_target_pct:.1f}%) while scenarios stay widened."
        ),
    }


def _thin_evidence_margin_guardrail(
    *,
    calibration_cohort_size: int,
    analog_count: int,
    global_cohort_size: int,
) -> dict[str, Any]:
    analog_scale = 1.0 if analog_count >= 2 else 0.8 if analog_count == 1 else 0.55
    cohort_scale = 1.0 if calibration_cohort_size >= 15 else 0.85 if calibration_cohort_size >= 10 else 0.65 if calibration_cohort_size >= 5 else 0.45
    global_scale = 1.0 if global_cohort_size >= 20 else 0.85 if global_cohort_size >= 10 else 0.7 if global_cohort_size >= 5 else 0.5
    positive_scale = round(max(0.25, min(analog_scale, cohort_scale, global_scale)), 2)
    if positive_scale >= 1.0:
        return {
            "applied": False,
            "positive_scale": 1.0,
            "note": None,
        }

    reasons: list[str] = []
    if analog_count == 0:
        reasons.append("no close analogs")
    elif analog_count == 1:
        reasons.append("only one close analog")
    if calibration_cohort_size < 10:
        reasons.append(f"{calibration_cohort_size} realised cohort records")
    if global_cohort_size < 10:
        reasons.append(f"{global_cohort_size} cross-symbol matches")

    return {
        "applied": True,
        "positive_scale": positive_scale,
        "analog_count": analog_count,
        "calibration_cohort_size": calibration_cohort_size,
        "global_cohort_size": global_cohort_size,
        "note": (
            "Thin-evidence margin gate applied: positive learned margin overlays are scaled down "
            f"to {int(round(positive_scale * 100))}% because " + ", ".join(reasons) + "."
        ),
    }


def _load_learning_cohort(limit: int | None = None, subject_ticker: str | None = None) -> list[Any]:
    global _LAST_LEARNING_SAMPLE_DIAGNOSTICS
    target_limit = int(limit or _learning_pool_limit())
    subject_upper = str(subject_ticker or "").upper()

    def _seeded_general_cohort() -> list[dict[str, Any]]:
        seeded_rows = seeded_cohort_observations(limit=target_limit)
        if not subject_upper:
            return seeded_rows
        return [
            observation
            for observation in seeded_rows
            if str(_obs_value(observation, "ticker", "") or "").upper() != subject_upper
        ]

    try:
        all_records = _cached_ledger_records(limit=_learning_candidate_limit(target_limit))
        records = _cached_stratified_sample(all_records, max_records=target_limit, target="full_dcf")
        if subject_ticker:
            try:
                subject_records = LedgerReader().query(ticker=str(subject_ticker).upper(), scenario="base")
            except Exception:
                subject_records = []
            if subject_records:
                seen_ids = {str(getattr(record, "record_id", "")) for record in records}
                records = [record for record in subject_records if str(getattr(record, "record_id", "")) not in seen_ids] + list(records)
    except Exception:
        records = []
        _LAST_LEARNING_SAMPLE_DIAGNOSTICS = {"enabled": False, "reason": "ledger_query_failed"}

    observations: list[Any] = []
    for record in records:
        quality = assess_prediction_record(record)
        if not quality.eligible("full_dcf"):
            continue
        if record.actual_revenue_mm is None and record.actual_ebit_margin is None:
            continue

        predicted_revenue_growth = _as_decimal(record.near_term_revenue_growth)
        predicted_margin = _as_decimal(record.predicted_ebit_margin)
        predicted_wacc = _as_decimal(record.predicted_wacc)
        predicted_terminal_growth = _as_decimal(record.predicted_terminal_growth)
        predicted_beta = float(record.beta or 1.0)
        predicted_revenue_mm = float(getattr(record, "predicted_revenue_mm", 0.0) or 0.0)
        predicted_ufcf_mm = float(getattr(record, "predicted_ufcf_mm", 0.0) or 0.0)
        actual_revenue_mm = getattr(record, "actual_revenue_mm", None)
        actual_ufcf_mm = getattr(record, "actual_ufcf_mm", None)
        predicted_ufcf_margin = (
            predicted_ufcf_mm / max(abs(predicted_revenue_mm), 1.0) if predicted_revenue_mm else None
        )
        actual_ufcf_margin = (
            float(actual_ufcf_mm) / max(abs(float(actual_revenue_mm)), 1.0)
            if actual_revenue_mm not in (None, 0) and actual_ufcf_mm is not None
            else predicted_ufcf_margin
        )
        predicted_reinvestment_rate = max(
            float(getattr(record, "capex_pct_revenue", 0.0) or 0.0)
            - float(getattr(record, "da_pct_revenue", 0.0) or 0.0),
            0.0,
        )
        actual_reinvestment_rate = predicted_reinvestment_rate
        if actual_ufcf_margin is not None and predicted_ufcf_margin is not None:
            actual_reinvestment_rate = max(
                predicted_reinvestment_rate + (predicted_ufcf_margin - actual_ufcf_margin),
                0.0,
            )

        revenue_delta = 0.0
        if record.actual_revenue_mm is not None and record.predicted_revenue_mm:
            revenue_delta = (record.actual_revenue_mm - record.predicted_revenue_mm) / max(abs(record.predicted_revenue_mm), 1.0)

        # Derive macro regime: use stored value if non-neutral, otherwise
        # re-classify from the rf_rate in macro_backdrop (backfills old "neutral" records).
        _stored_regime = (record.macro_regime or "").strip()
        _rf_from_backdrop = float((record.macro_backdrop or {}).get("rf_rate") or 0.0)
        _obs_macro_regime = (
            _stored_regime
            if _stored_regime and _stored_regime != "neutral"
            else _classify_macro_regime(_rf_from_backdrop)
        )

        observations.append(
            {
                "ticker": getattr(record, "ticker", "") or "",
                "sector": record.sector or "Default",
                "industry": record.industry or "",
                "data_vintage_years": max(1, int(record.data_vintage_years or 1)),
                "market_cap_regime": record.market_cap_regime or "large",
                "macro_regime": _obs_macro_regime,
                "predicted_revenue_growth": predicted_revenue_growth,
                "actual_revenue_growth": predicted_revenue_growth + revenue_delta,
                "predicted_ebit_margin": predicted_margin,
                "actual_ebit_margin": _as_decimal(record.actual_ebit_margin) if record.actual_ebit_margin is not None else predicted_margin,
                "predicted_wacc": predicted_wacc or 0.10,
                "actual_wacc": _derive_actual_wacc(predicted_wacc, actual_ufcf_margin, predicted_ufcf_margin, revenue_delta),
                "predicted_terminal_growth": predicted_terminal_growth or 0.025,
                "actual_terminal_growth": _derive_actual_terminal_growth(predicted_terminal_growth, revenue_delta),
                "predicted_beta": predicted_beta,
                "actual_beta": _derive_actual_beta(predicted_beta, revenue_delta),
                "predicted_ufcf_margin": predicted_ufcf_margin,
                "actual_ufcf_margin": actual_ufcf_margin,
                "predicted_reinvestment_rate": predicted_reinvestment_rate,
                "actual_reinvestment_rate": actual_reinvestment_rate,
                "feature_vector": tuple(getattr(record, "feature_vector", None) or ()) or None,
                "structural_break_flag": bool(getattr(record, "structural_break_hints", []) or []),
                "structural_break_hints": list(getattr(record, "structural_break_hints", []) or []),
                "quality_score": float(quality.quality_score),
                "observation_type": quality.observation_type,
                "target_eligibility": dict(quality.target_eligibility),
            }
        )
    if observations:
        # Augment ledger records with historical quarterly/annual replay observations.
        # Cap to avoid iterating 300k+ observations in refine_live_assumptions.
        added_historical_replay = False
        try:
            historical = _get_historical_observations()
            if historical:
                historical_limit = int(
                    LEARNING_CONFIG.get("historical_replay_limit", 4000) or 4000
                )
                if subject_ticker:
                    subject_historical = [
                        observation
                        for observation in historical
                        if str(_obs_value(observation, "ticker", "") or "").upper() == subject_upper
                    ]
                    other_historical = [
                        observation
                        for observation in historical
                        if str(_obs_value(observation, "ticker", "") or "").upper() != subject_upper
                    ]
                    historical = subject_historical + other_historical[: max(historical_limit - len(subject_historical), 0)]
                elif len(historical) > historical_limit:
                    historical = historical[:historical_limit]
                observations = list(observations) + list(historical)
                added_historical_replay = bool(historical)
        except Exception:
            pass
        if not added_historical_replay:
            if subject_ticker:
                try:
                    seeded_replay = seeded_replay_summary_observations(str(subject_ticker).upper())
                    if seeded_replay:
                        observations = list(seeded_replay) + list(observations)
                except Exception:
                    pass
            try:
                observations = list(observations) + _seeded_general_cohort()
            except Exception:
                pass
        return observations
    seeded = _seeded_general_cohort()
    if subject_ticker:
        try:
            seeded_replay = seeded_replay_summary_observations(str(subject_ticker).upper())
            if seeded_replay:
                return list(seeded_replay) + list(seeded)
        except Exception:
            pass
    return seeded


def _load_analog_candidates(limit: int | None = None):
    target_limit = int(limit or _learning_pool_limit())
    try:
        all_records = _cached_ledger_records(limit=_learning_candidate_limit(target_limit))
        records = _cached_stratified_sample(all_records, max_records=target_limit, target="full_dcf")
    except Exception:
        records = []
    analogs = build_analog_observations(records)
    if analogs:
        return analogs
    return seeded_analog_observations(limit=int(limit or _learning_pool_limit()))


def _market_residual_overlay_for_subject(
    *,
    ticker: str,
    sector: str,
    industry: str,
    market_cap_regime: str,
    macro_regime: str,
) -> dict[str, Any]:
    if LEARNING_CONFIG.get("market_residual_overlay_enabled", True) is False:
        return {"enabled": False, "reason": "market_residual_overlay_disabled", "cohort_size": 0, "confidence": 0.0}
    target_limit = max(250, int(LEARNING_CONFIG.get("market_residual_sample_limit", _learning_pool_limit(1000)) or 1000))
    try:
        all_records = _cached_ledger_records(limit=_learning_candidate_limit(target_limit))
        sampled_records = _cached_stratified_sample(all_records, max_records=target_limit, target="valuation_ev")
        overlay = build_market_residual_overlay(
            sampled_records,
            ticker=ticker,
            sector=sector,
            industry=industry,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        )
        overlay["sample_diagnostics"] = {"cached": True}
        return overlay
    except Exception as exc:
        return {"enabled": False, "reason": f"market_residual_overlay_failed: {exc}", "cohort_size": 0, "confidence": 0.0}


def _disabled_analog_set(ticker: str, symbol_features: SymbolFeatures) -> AnalogSet:
    analog_set = AnalogSet(subject_ticker=ticker, subject_features=symbol_features)
    analog_set.overlay = compute_global_overlay(analog_set)
    return analog_set


def _serialise_analog_set(analog_set: AnalogSet) -> dict[str, Any]:
    return {
        "enabled": bool(analog_set.analogs),
        "count": len(analog_set.analogs),
        "pattern_match": analog_set.pattern_match,
        "pattern_match_score": round(float(analog_set.pattern_match_score or 0.0), 2),
        "cohorts": [
            {
                "label": cohort.label,
                "score": round(cohort.score, 3),
                "members": list(cohort.members),
                "explanation": cohort.explanation,
            }
            for cohort in analog_set.cohorts
        ],
        "items": [
            {
                "ticker": match.analog.ticker,
                "sector": match.analog.sector,
                "industry": match.analog.industry,
                "score": round(match.analog_score, 3),
                "similarity": round(match.similarity_score, 3),
                "static_similarity": round(match.static_similarity, 3),
                "regime_similarity": round(match.regime_similarity, 3),
                "weights": {
                    "recency": round(match.recency_weight, 2),
                    "data_quality": round(match.quality_weight, 2),
                    "sample": round(match.sample_weight, 2),
                    "usefulness": round(match.usefulness_weight, 2),
                },
                "maturity_stage": match.analog.maturity_stage,
                "valuation_regime": match.analog.valuation_regime,
                "volatility_regime": match.analog.volatility_regime,
                "evidence": list(match.evidence),
            }
            for match in analog_set.analogs[:5]
        ],
        "overlay": dict(analog_set.overlay or {}),
    }


def _global_cross_symbol_overlay(
    observations: list[Any],
    *,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    subject_structural_break_like: bool = False,
    subject_sector: str = "",
    subject_industry: str = "",
) -> dict[str, Any]:
    if not observations:
        return {
            "enabled": False,
            "scope": None,
            "cohort_size": 0,
            "sector_span": 0,
            "confidence": 0.0,
            "revenue_growth_adj_pp": 0.0,
            "ebit_margin_adj_pp": 0.0,
            "wacc_adj_pp": 0.0,
            "terminal_growth_adj_pp": 0.0,
            "beta_adj": 0.0,
            "note": None,
        }

    maturity = _maturity_bucket(data_vintage_years)
    regime_matches = [
        observation
        for observation in observations
        if str(_obs_value(observation, "market_cap_regime", "") or "") == market_cap_regime
        and str(_obs_value(observation, "macro_regime", "") or "") == macro_regime
        and _maturity_bucket(int(_obs_value(observation, "data_vintage_years", 0) or 0)) == maturity
    ]
    cap_matches = [
        observation
        for observation in observations
        if str(_obs_value(observation, "market_cap_regime", "") or "") == market_cap_regime
        and str(_obs_value(observation, "macro_regime", "") or "") == macro_regime
    ]

    selected: list[Any] = []
    scope: str | None = None
    matched_regime = False
    for candidate_scope, candidate_cohort, min_size, min_sector_span in (
        ("regime", regime_matches, 5, 2),
        ("market-cap", cap_matches, 6, 2),
        ("global", observations, 10, 3),
    ):
        preferred_cohort = [
            observation
            for observation in candidate_cohort
            if _structural_break_flag(observation) == subject_structural_break_like
        ]
        preferred_sector_span = len(
            {
                str(_obs_value(observation, "sector", "") or "")
                for observation in preferred_cohort
                if _obs_value(observation, "sector", None)
            }
        )
        if len(preferred_cohort) >= min_size and preferred_sector_span >= min_sector_span:
            selected = preferred_cohort
            scope = candidate_scope
            matched_regime = True
            break
        sector_span = len(
            {
                str(_obs_value(observation, "sector", "") or "")
                for observation in candidate_cohort
                if _obs_value(observation, "sector", None)
            }
        )
        if len(candidate_cohort) >= min_size and sector_span >= min_sector_span:
            selected = candidate_cohort
            scope = candidate_scope
            break

    if not selected or scope is None:
        return {
            "enabled": False,
            "scope": None,
            "cohort_size": 0,
            "sector_span": 0,
            "confidence": 0.0,
            "revenue_growth_adj_pp": 0.0,
            "ebit_margin_adj_pp": 0.0,
            "wacc_adj_pp": 0.0,
            "terminal_growth_adj_pp": 0.0,
            "beta_adj": 0.0,
            "note": None,
        }

    sector_span = len(
        {
            str(_obs_value(observation, "sector", "") or "")
            for observation in selected
            if _obs_value(observation, "sector", None)
        }
    )
    confidence = _clamp(min(1.0, len(selected) / 18.0) * min(1.0, sector_span / 4.0), 0.15, 1.0)

    # Taxonomy-aware damping: if the subject has a known industry and no
    # observations in the selected cohort share that industry (or even sector),
    # the overlay is broad-regime evidence only and should carry less weight.
    taxonomy_damping_scale = 1.0
    if subject_industry or subject_sector:
        same_industry_count = 0
        same_sector_count = 0
        for obs in selected:
            obs_industry = str(_obs_value(obs, "industry", "") or "").strip()
            obs_sector = str(_obs_value(obs, "sector", "") or "").strip()
            if subject_industry and obs_industry and obs_industry.lower() == subject_industry.lower():
                same_industry_count += 1
            if subject_sector and obs_sector and obs_sector.lower() == subject_sector.lower():
                same_sector_count += 1
        if same_industry_count == 0 and same_sector_count == 0:
            taxonomy_damping_scale = 0.45  # pure broad-regime: sharply reduced
        elif same_industry_count == 0:
            taxonomy_damping_scale = 0.70  # same sector but not same industry: moderate reduction

    damping = _clamp((0.10 + 0.20 * confidence) * taxonomy_damping_scale, 0.04, 0.30)
    revenue_growth_adj_pp = round(
        _clamp(
            _trimmed_mean(
                [
                    float(actual) - float(predicted)
                    for observation in selected
                    for actual, predicted in [
                        (
                            _obs_value(observation, "actual_revenue_growth"),
                            _obs_value(observation, "predicted_revenue_growth"),
                        )
                    ]
                    if actual is not None and predicted is not None
                ]
            )
            * damping
            * 100,
            -4.0,
            4.0,
        ),
        1,
    )
    ebit_margin_adj_pp = round(
        _clamp(
            _trimmed_mean(
                [
                    float(actual) - float(predicted)
                    for observation in selected
                    for actual, predicted in [
                        (
                            _obs_value(observation, "actual_ebit_margin"),
                            _obs_value(observation, "predicted_ebit_margin"),
                        )
                    ]
                    if actual is not None and predicted is not None
                ]
            )
            * damping
            * 100,
            -4.0,
            4.0,
        ),
        1,
    )
    wacc_adj_pp = round(
        _clamp(
            _trimmed_mean(
                [
                    float(actual) - float(predicted)
                    for observation in selected
                    for actual, predicted in [
                        (_obs_value(observation, "actual_wacc"), _obs_value(observation, "predicted_wacc"))
                    ]
                    if actual is not None and predicted is not None
                ]
            )
            * damping
            * 100,
            -1.5,
            1.5,
        ),
        1,
    )
    terminal_growth_adj_pp = round(
        _clamp(
            _trimmed_mean(
                [
                    float(actual) - float(predicted)
                    for observation in selected
                    for actual, predicted in [
                        (
                            _obs_value(observation, "actual_terminal_growth"),
                            _obs_value(observation, "predicted_terminal_growth"),
                        )
                    ]
                    if actual is not None and predicted is not None
                ]
            )
            * min(damping, 0.22)
            * 100,
            -0.8,
            0.8,
        ),
        1,
    )
    beta_adj = round(
        _clamp(
            _trimmed_mean(
                [
                    float(actual) - float(predicted)
                    for observation in selected
                    for actual, predicted in [
                        (_obs_value(observation, "actual_beta"), _obs_value(observation, "predicted_beta"))
                    ]
                    if actual is not None and predicted is not None
                ]
            )
            * damping,
            -0.25,
            0.25,
        ),
        2,
    )

    note = (
        f"Global cross-symbol learning active: {scope} cohort of {len(selected)} realised records "
        f"across {sector_span} sectors."
    )
    if matched_regime:
        note = note[:-1] + (
            " matched to structural-break histories."
            if subject_structural_break_like
            else " matched to stable histories."
        )
    if taxonomy_damping_scale < 1.0:
        note += (
            " Overlay is low-confidence (broad-regime only — no same-industry anchor in cohort)"
            if taxonomy_damping_scale <= 0.45
            else " Overlay moderately dampened (same sector but no same-industry anchor in cohort)."
        )

    return {
        "enabled": True,
        "scope": scope,
        "cohort_size": len(selected),
        "sector_span": sector_span,
        "confidence": round(confidence, 2),
        "taxonomy_confidence": round(taxonomy_damping_scale, 2),
        "regime_filter": "matched" if matched_regime else "mixed",
        "revenue_growth_adj_pp": revenue_growth_adj_pp,
        "ebit_margin_adj_pp": ebit_margin_adj_pp,
        "wacc_adj_pp": wacc_adj_pp,
        "terminal_growth_adj_pp": terminal_growth_adj_pp,
        "beta_adj": beta_adj,
        "note": note,
    }


def _assumption_source(
    *,
    weights: dict[str, float],
    pattern_name: str | None,
    pattern_score: float,
    pattern_overlay_pp: float,
    cohort_size: int,
    review_due: bool,
) -> tuple[str, str | None]:
    parts = [
        f"{weights['company_history']:.0%} company 5y history",
        f"{weights['sector_prior']:.0%} sector prior",
    ]
    if weights["learned_cohort"] > 0:
        parts.append(f"{weights['learned_cohort']:.0%} learned cohort")

    source = "Knowledge model: " + ", ".join(parts)
    if pattern_name and pattern_score >= 0.7 and abs(pattern_overlay_pp) >= 0.1:
        source += f"; pattern {pattern_name} {pattern_overlay_pp:+.1f}pp"

    warn = None
    if cohort_size < 5:
        warn = "Learning cohort is still thin; sector priors carry more weight."
    return source, warn


def refine_live_assumptions(
    *,
    ticker: str,
    company_name: str = "",
    sector: str,
    industry: str,
    market_cap: float,
    revenues: list[float],
    ebit_margins: list[float],
    gross_margin_base_pct: float,
    revenue_growth_near: float,
    terminal_growth: float,
    ebit_margin_base_pct: float,
    ebit_margin_target: float,
    beta: float,
    wacc: float,
    rf_rate: float,
    erp: float,
    kd_post: float,
    e_wt: float,
    d_wt: float,
    total_assets: float,
    total_debt: float,
    revenue_base: float,
    operating_cf: float,
    fcf: float,
    capex_pct: float,
    capexes: list[float],
    da_pct: float,
    das: list[float],
    sbc_pct: float,
    sbcs: list[float],
    tax_rate_pct: float,
    pretax_incomes: list[float],
    tax_provisions: list[float],
    dso: float,
    dio: float,
    dpo: float,
    observations: list[CalibrationObservation] | None = None,
    # Layer C/D/E — trajectory constraints and market signal
    market_implied_g: float | None = None,
    # Layer F Tier 3 — analyst consensus depth signals
    # ntm_growth: raw NTM consensus growth % (from analyst estimates) before blending
    # analyst_count: number of analysts covering the name (0 = unknown)
    ntm_growth: float | None = None,
    analyst_count: int = 0,
) -> dict[str, Any]:
    def _dampen_positive(value: float, scale: float) -> float:
        if value <= 0:
            return value
        return round(value * scale, 2)

    def _merge_guardrail_warn(base_warn: str | None, *guardrails: dict[str, Any]) -> str | None:
        notes: list[str] = []
        if base_warn:
            notes.append(str(base_warn))
        for guardrail in guardrails:
            if guardrail.get("applied") and guardrail.get("note"):
                notes.append(str(guardrail["note"]))
        if not notes:
            return None
        deduped: list[str] = []
        for note in notes:
            if note not in deduped:
                deduped.append(note)
        return "; ".join(deduped)

    knowledge_sector = _knowledge_sector(sector)
    market_cap_regime = _market_cap_regime(market_cap)
    # Classify current macro environment from the live risk-free rate so that
    # observations and blending use a meaningful regime label instead of always "neutral".
    current_macro_regime = _classify_macro_regime(rf_rate / 100)
    history_window_years = _history_window_years(revenues)
    completed_years = max(1, len(revenues) - 1)
    # Quarterly verification: review is active from the first completed year (not every 5 years).
    review_due = completed_years >= 1
    next_review_in_years = 0  # Always current — predictions are verified quarterly
    industry_lower = (industry or "").lower()

    revenue_volatility = _safe_pstdev(_growth_rates(revenues[-(history_window_years + 1):]))
    margin_volatility = _safe_pstdev([margin / 100 for margin in ebit_margins[-history_window_years:]])
    observations_provided = observations is not None
    if observations is not None:
        observations = list(observations)
    else:
        try:
            observations = _load_learning_cohort(subject_ticker=ticker)
        except TypeError as exc:
            if "subject_ticker" not in str(exc):
                raise
            observations = _load_learning_cohort()
    learning_sample_diagnostics = (
        {"enabled": False, "reason": "caller_supplied_observations", "candidate_rows": len(observations)}
        if observations_provided
        else dict(_LAST_LEARNING_SAMPLE_DIAGNOSTICS or {})
    )
    analog_candidates = (
        build_analog_observations(observations) if observations_provided and len(observations) >= 5 else []
    ) if observations_provided else _load_analog_candidates()
    symbol_features = build_symbol_features(
        ticker=ticker,
        sector=sector,
        industry=industry,
        revenues=revenues,
        ebit_margins=ebit_margins,
        gross_margin_base_pct=gross_margin_base_pct,
        capex_pct=capex_pct,
        total_assets=total_assets,
        total_debt=total_debt,
        revenue_base=revenue_base,
        operating_cf=operating_cf,
        fcf=fcf,
        da_pct=da_pct,
        tax_rate_pct=tax_rate_pct,
        market_cap=market_cap,
        market_cap_regime=market_cap_regime,
        macro_regime=current_macro_regime,
        observation_year=completed_years,
    )
    feature_vector = dict(symbol_features.feature_map)
    margin_normalisation = _margin_normalisation(ebit_margins, ebit_margin_base_pct, ebit_margin_target)

    raw_assumptions = AssumptionSet(
        revenue_growth_rates=[revenue_growth_near / 100] * 7,
        near_term_growth=revenue_growth_near / 100,
        long_run_growth=terminal_growth / 100,
        ebit_margin_current=ebit_margin_base_pct / 100,
        ebit_margin_terminal=ebit_margin_target / 100,
        ebit_margin_schedule=[
            (ebit_margin_base_pct + (ebit_margin_target - ebit_margin_base_pct) * (year / 7)) / 100
            for year in range(1, 8)
        ],
        effective_tax_rate=tax_rate_pct / 100,
        dso_days=dso,
        dio_days=dio,
        dpo_days=dpo,
        capex_pct_revenue=capex_pct / 100,
        da_pct_revenue=da_pct / 100,
        sbc_pct_revenue=sbc_pct / 100,
    )
    base_ufcf_margin = (
        raw_assumptions.ebit_margin_terminal * (1.0 - raw_assumptions.effective_tax_rate)
        + raw_assumptions.da_pct_revenue
        + raw_assumptions.sbc_pct_revenue
        - raw_assumptions.capex_pct_revenue
    )
    base_reinvestment_rate = max(raw_assumptions.capex_pct_revenue - raw_assumptions.da_pct_revenue, 0.0)

    # Layer C/D — compute trajectory-constrained terminal g range
    from auto_valuation.assumptions.headwind_table import (
        classify_revenue_regime,
        get_industry_headwind_score,
        terminal_g_prior_range as _tg_prior_range,
    )
    from auto_valuation.model.income_statement import historical_revenue_cagr
    _income_stmts_proxy = [
        {"revenue": rev, "ebit_margin": em}
        for rev, em in zip(reversed(revenues), reversed(ebit_margins))
    ]
    _hist_cagr_3 = historical_revenue_cagr(_income_stmts_proxy, years=3) if len(revenues) >= 3 else None
    _hist_cagr_5 = historical_revenue_cagr(_income_stmts_proxy, years=5) if len(revenues) >= 5 else None
    _hist_cagr_10 = historical_revenue_cagr(_income_stmts_proxy, years=10) if len(revenues) >= 10 else None
    # Layer E — quick pre-DCF market-implied g estimate from market cap + FCF
    # Rearranging Gordon Growth: EV = UFCF*(1+g)/(wacc-g)
    # → g = (EV*wacc - UFCF) / (EV + UFCF)
    # Uses market_cap as a proxy for EV when net_debt unavailable here.
    if market_implied_g is None and market_cap > 0 and abs(fcf) > 0:
        _wacc_dec = (wacc / 100.0) if wacc > 1.0 else wacc
        _ev_proxy = market_cap  # simplified: ignoring net debt for quick estimate
        _denom = _ev_proxy + fcf
        if abs(_denom) > 1e-6:
            _quick_g = (_ev_proxy * _wacc_dec - fcf) / _denom
            market_implied_g = max(-0.10, min(_quick_g, _wacc_dec - 0.005))
    _revenue_regime = classify_revenue_regime(
        _hist_cagr_3, _hist_cagr_5, _hist_cagr_10, revenue_growth_near / 100.0, market_implied_g
    )
    _rf_dec = (rf_rate / 100.0) if rf_rate > 1.0 else rf_rate
    _tg_range = _tg_prior_range(_revenue_regime, rf_rate=_rf_dec, sector=knowledge_sector)

    calibrated = calibrate(
        raw_assumptions,
        knowledge_sector,
        industry,
        len(revenues),
        market_cap_regime,
        current_macro_regime,
        observations=observations,
        base_wacc=wacc / 100,
        base_terminal_growth=terminal_growth / 100,
        base_beta=beta,
        calibration_store=_LIVE_CALIBRATION_STORE,
        terminal_g_range=_tg_range,
        market_implied_terminal_g=market_implied_g,
        market_cap_mm=market_cap,
    )
    max_analog_results = max(6, int(LEARNING_CONFIG.get("max_analogs_returned", 10)))
    graph_neighbor_limit = max(6, int(LEARNING_CONFIG.get("relationship_graph_max_neighbors", max_analog_results)))

    analog_set = (
        find_analogs(
            ticker,
            symbol_features,
            analog_candidates,
            subject_company_name=company_name,
            subject_sector=sector,
            subject_industry=industry,
            subject_vintage_year=len(revenues),
            subject_market_cap_regime=market_cap_regime,
            subject_macro_regime=current_macro_regime,
            observation_year=completed_years,
            max_results=max_analog_results,
            cross_sector_only=False,
        )
        if analog_candidates
        else _disabled_analog_set(ticker, symbol_features)
    )
    analog_learning = dict(analog_set.overlay or compute_global_overlay(analog_set))
    relationship_graph = build_relationship_graph(
        ticker=ticker,
        subject_features=symbol_features,
        analog_set=analog_set,
        observations=observations,
        sector=sector,
        industry=industry,
        max_neighbors=graph_neighbor_limit,
    )
    relationship_overlay = dict(relationship_graph.get("overlay") or {})

    pattern_name, pattern_score, pattern_overlay = match_pattern_library(symbol_features)
    if pattern_score < 0.7:
        pattern_name = None
        pattern_overlay = {}

    sector_growth_pct = round(sector_median_growth(knowledge_sector) * 100, 1)
    sector_margin_pct = round(get_sector_ebit_margin(knowledge_sector) * 100, 1)
    sector_capex_pct = round(get_sector_capex_pct(knowledge_sector) * 100, 1)
    sector_sbc_pct = round(get_sector_terminal_sbc_pct(knowledge_sector) * 100, 1)
    sector_wc_days = get_sector_wc_days(knowledge_sector)
    sector_beta = fetch_damodaran_industry_beta(industry or knowledge_sector) or fetch_damodaran_industry_beta(knowledge_sector) or 1.0

    company_growth_pct = round(_rolling_cagr(revenues, history_window_years) * 100, 1) if len(revenues) >= 2 else revenue_growth_near
    company_tax_pct = round(_normalized_tax_rate_pct(pretax_incomes, tax_provisions, tax_rate_pct), 1)
    company_da_pct = round(_trailing_ratio_pct(das, revenues, years=history_window_years), 1) or da_pct
    company_capex_pct = round(_trailing_ratio_pct(capexes, revenues, years=history_window_years, absolute=True), 1) or capex_pct
    company_sbc_pct = round(_trailing_ratio_pct(sbcs, revenues, years=history_window_years), 1) or sbc_pct
    company_margin_target_pct = margin_normalisation["target_anchor_pct"] if margin_normalisation["applied"] else ebit_margin_target
    subject_structural_break_like = company_growth_pct <= 0.0 and (
        bool(margin_normalisation.get("applied")) or ebit_margin_base_pct >= company_margin_target_pct - 0.5
    )
    fallback_global_learning = _global_cross_symbol_overlay(
        observations,
        data_vintage_years=len(revenues),
        market_cap_regime=market_cap_regime,
        macro_regime=current_macro_regime,
        subject_structural_break_like=subject_structural_break_like,
        subject_sector=sector,
        subject_industry=industry,
    )
    analog_overlay_eligible = bool(analog_learning.get("enabled")) and len(analog_set.analogs) >= 2
    global_learning = analog_learning if analog_overlay_eligible else fallback_global_learning
    market_residual_overlay = (
        {"enabled": False, "reason": "caller_supplied_observations", "cohort_size": 0, "confidence": 0.0}
        if observations_provided
        else _market_residual_overlay_for_subject(
            ticker=ticker,
            sector=sector,
            industry=industry,
            market_cap_regime=market_cap_regime,
            macro_regime=current_macro_regime,
        )
    )

    growth_weights = _normalise_weights(
        _clamp(0.48 + 0.04 * history_window_years - 0.30 * min(revenue_volatility, 0.50), 0.38, 0.72),
        0.22,
        _clamp(0.08 + 0.18 * calibrated.calibration_confidence, 0.0, 0.22) if calibrated.calibration_cohort_size else 0.0,
    )
    margin_company_weight = _clamp(0.50 + 0.04 * history_window_years - 0.55 * min(margin_volatility, 0.20), 0.40, 0.72)
    margin_sector_weight = 0.25
    if margin_normalisation["applied"]:
        margin_company_weight = _clamp(margin_company_weight - 0.18, 0.24, 0.55)
        margin_sector_weight = 0.42 if margin_normalisation["scenario_width_multiplier"] > 1.5 else 0.34
    margin_weights = _normalise_weights(
        margin_company_weight,
        margin_sector_weight,
        _clamp(0.08 + 0.15 * calibrated.calibration_confidence, 0.0, 0.20) if calibrated.calibration_cohort_size else 0.0,
    )
    risk_weights = _normalise_weights(
        0.58,
        0.24,
        _clamp(0.06 + 0.12 * calibrated.calibration_confidence, 0.0, 0.18) if calibrated.calibration_cohort_size else 0.0,
    )

    growth_pattern_pp = round(pattern_overlay.get("revenue_growth_adj", 0.0) * 100 * pattern_score, 1)
    margin_pattern_pp = round(pattern_overlay.get("ebit_margin_adj", 0.0) * 100 * pattern_score, 1)
    global_growth_pp = float(global_learning.get("revenue_growth_adj_pp") or 0.0)
    if history_window_years >= 4 and growth_weights["company_history"] >= 0.60:
        global_growth_pp = 0.0
    global_margin_pp = float(global_learning.get("ebit_margin_adj_pp") or 0.0)
    global_wacc_pp = float(global_learning.get("wacc_adj_pp") or 0.0)
    global_terminal_growth_pp = float(global_learning.get("terminal_growth_adj_pp") or 0.0)
    market_wacc_pp = float(market_residual_overlay.get("wacc_adj_pp") or 0.0)
    market_terminal_growth_pp = float(market_residual_overlay.get("terminal_growth_adj_pp") or 0.0)
    market_risk_source = "; market-implied risk overlay" if abs(market_wacc_pp) >= 0.05 or abs(market_terminal_growth_pp) >= 0.05 else ""
    global_beta_adj = float(global_learning.get("beta_adj") or 0.0)
    relationship_growth_pp = round(float(relationship_overlay.get("revenue_growth_adj_pp") or 0.0) * 0.35, 2)
    relationship_margin_pp = round(float(relationship_overlay.get("ebit_margin_adj_pp") or 0.0) * 0.35, 2)
    relationship_wacc_pp = round(float(relationship_overlay.get("wacc_adj_pp") or 0.0) * 0.35, 2)
    relationship_terminal_growth_pp = round(float(relationship_overlay.get("terminal_growth_adj_pp") or 0.0) * 0.35, 2)
    relationship_beta_adj = round(float(relationship_overlay.get("beta_adj") or 0.0) * 0.35, 3)
    learned_margin_component = round((calibrated.ebit_margin_adj * 100) * margin_weights["learned_cohort"], 2)
    thin_evidence_margin_guardrail = _thin_evidence_margin_guardrail(
        calibration_cohort_size=int(calibrated.calibration_cohort_size or 0),
        analog_count=len(analog_set.analogs),
        global_cohort_size=int(global_learning.get("cohort_size") or 0),
    )
    if thin_evidence_margin_guardrail["applied"]:
        positive_scale = float(thin_evidence_margin_guardrail["positive_scale"])
        learned_margin_component = _dampen_positive(learned_margin_component, positive_scale)
        global_margin_pp = _dampen_positive(global_margin_pp, positive_scale)
        relationship_margin_pp = _dampen_positive(relationship_margin_pp, positive_scale)

    # Layer F Tier 3 — analyst consensus blending into refined_growth.
    # When analyst coverage depth is known (analyst_count > 0), we incorporate
    # the raw NTM consensus directly into the growth blend so the knowledge model
    # can up-weight or down-weight it relative to the historical CAGR based on
    # analyst count. Strong coverage (≥ 3) gets 25 pp NTM weight; thin coverage
    # (1–2 analysts) gets a reduced 10 pp weight; no coverage = 0 (falls back
    # to the purely historical blend below).
    ntm_growth_pct = ntm_growth  # already in %, e.g. 8.0 means 8%
    if ntm_growth_pct is not None and analyst_count > 0:
        if analyst_count >= 3:
            _ntm_w = 0.25
        else:
            _ntm_w = 0.10  # thin coverage — penalise NTM weight
        # Reduce the company_history weight proportionally to make room for NTM.
        _hist_w = max(growth_weights["company_history"] - _ntm_w, 0.10)
        _ntm_company_growth = (
            company_growth_pct * _hist_w
            + ntm_growth_pct * _ntm_w
            + sector_growth_pct * growth_weights["sector_prior"]
            + (calibrated.revenue_growth_adj * 100) * growth_weights["learned_cohort"]
            + growth_pattern_pp
            + global_growth_pp
            + relationship_growth_pp
        )
        refined_growth = round(_clamp(_ntm_company_growth, -15.0, 50.0), 1)
    else:
        refined_growth = round(
            _clamp(
                company_growth_pct * growth_weights["company_history"]
                + sector_growth_pct * growth_weights["sector_prior"]
                + (calibrated.revenue_growth_adj * 100) * growth_weights["learned_cohort"]
                + growth_pattern_pp
                + global_growth_pp
                + relationship_growth_pp,
                -15.0,
                50.0,
            ),
            1,
        )
    refined_margin_target = round(
        _clamp(
            company_margin_target_pct * margin_weights["company_history"]
            + sector_margin_pct * margin_weights["sector_prior"]
            + learned_margin_component
            + margin_pattern_pp
            + global_margin_pp
            + relationship_margin_pp,
            max(-10.0, ebit_margin_base_pct),
            45.0 if "software" not in industry_lower else 80.0,
        ),
        1,
    )

    smoothed_beta = round(
        _clamp(
            _blended_beta(
                beta * risk_weights["company_history"]
                + sector_beta * risk_weights["sector_prior"]
                + calibrated.beta_adj * risk_weights["learned_cohort"],
                sector_beta,
                industry_weight=0.15,
            )
            + global_beta_adj
            + relationship_beta_adj,
            0.3,
            3.0,
        ),
        2,
    )
    sector_wacc = _clamp(
        (e_wt / 100) * (rf_rate + (0.67 * sector_beta + 0.33) * erp) + (d_wt / 100) * kd_post,
        5.0,
        20.0,
    )
    refined_wacc = round(
        _clamp(
            wacc * risk_weights["company_history"]
            + sector_wacc * risk_weights["sector_prior"]
            + (calibrated.wacc_adj * 100) * risk_weights["learned_cohort"]
            + global_wacc_pp
            + relationship_wacc_pp
            + market_wacc_pp,
            5.0,
            20.0,
        ),
        1,
    )
    refined_terminal_growth = round(
        _clamp(
            terminal_growth * max(0.65, 1.0 - risk_weights["learned_cohort"])
            + (calibrated.terminal_growth_adj * 100) * risk_weights["learned_cohort"]
            + global_terminal_growth_pp
            + relationship_terminal_growth_pp
            + market_terminal_growth_pp,
            0.5,
            4.0,
        ),
        1,
    )

    working_capital_weight = _clamp(0.45 + 0.06 * history_window_years, 0.45, 0.75)
    working_capital_sector_weight = 1.0 - working_capital_weight
    refined_dso = round(dso * working_capital_weight + sector_wc_days["dso"] * working_capital_sector_weight, 1)
    refined_dio = round(dio * working_capital_weight + sector_wc_days["dio"] * working_capital_sector_weight, 1)
    refined_dpo = round(dpo * working_capital_weight + sector_wc_days["dpo"] * working_capital_sector_weight, 1)

    intensity_company_weight = _clamp(0.52 + 0.05 * history_window_years, 0.52, 0.77)
    intensity_sector_weight = 1.0 - intensity_company_weight
    refined_capex_pct = round(_clamp(company_capex_pct * intensity_company_weight + sector_capex_pct * intensity_sector_weight, 0.5, 25.0), 1)
    refined_da_pct = round(_clamp(company_da_pct * 0.75 + min(company_capex_pct, sector_capex_pct) * 0.25, 0.2, 15.0), 1)
    refined_sbc_pct = round(_clamp(company_sbc_pct * intensity_company_weight + sector_sbc_pct * intensity_sector_weight, 0.0, 15.0), 1)
    refined_tax_pct = round(_clamp(company_tax_pct * 0.75 + tax_rate_pct * 0.25, 5.0, 45.0), 1)
    layered_learning = _build_layered_learning_snapshot(
        ticker=ticker,
        sector=knowledge_sector,
        industry=industry,
        data_vintage_years=len(revenues),
        market_cap_regime=market_cap_regime,
        macro_regime=current_macro_regime,
        feature_vector=feature_vector,
        observations=observations,
        analog_set=analog_set,
        global_learning=global_learning,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        margin_normalisation=margin_normalisation,
        core_weight_maps=[growth_weights, margin_weights, risk_weights, risk_weights],
        calibrated=calibrated,
        base_ufcf_margin=base_ufcf_margin,
        base_reinvestment_rate=base_reinvestment_rate,
    )
    layered_learning["quality_gate"] = learning_sample_diagnostics
    layered_learning["market_residual_overlay"] = market_residual_overlay
    learned_metrics = dict(layered_learning.get("learned_metrics") or {})
    uncertainty = dict(layered_learning.get("uncertainty") or {})
    structural_break = dict(layered_learning.get("structural_break") or {})
    regime_guardrail = _declining_regime_guardrail(
        company_growth_pct=company_growth_pct,
        revenue_growth_near=revenue_growth_near,
        ebit_margin_base_pct=ebit_margin_base_pct,
        company_margin_target_pct=company_margin_target_pct,
        wacc=wacc,
        terminal_growth=terminal_growth,
        beta=beta,
        structural_break=structural_break,
    )
    if regime_guardrail["applied"]:
        refined_growth = min(refined_growth, float(regime_guardrail["max_growth_pct"]))
        refined_margin_target = min(refined_margin_target, float(regime_guardrail["max_margin_target_pct"]))
        refined_wacc = max(refined_wacc, float(regime_guardrail["min_wacc_pct"]))
        refined_terminal_growth = min(refined_terminal_growth, float(regime_guardrail["max_terminal_growth_pct"]))
        smoothed_beta = max(smoothed_beta, float(regime_guardrail["min_beta"]))
    layered_learning["regime_guardrail"] = regime_guardrail
    margin_guardrail = _margin_volatility_guardrail(
        margin_normalisation=margin_normalisation,
        company_margin_target_pct=company_margin_target_pct,
        ebit_margin_base_pct=ebit_margin_base_pct,
        refined_margin_target=refined_margin_target,
    )
    if margin_guardrail["applied"]:
        refined_margin_target = min(refined_margin_target, float(margin_guardrail["max_margin_target_pct"]))
    layered_learning["margin_guardrail"] = margin_guardrail
    stable_margin_floor_guardrail = {
        "applied": False,
        "min_margin_target_pct": round(float(ebit_margin_base_pct), 1),
        "note": None,
    }
    unlearned_margin_weight_total = max(margin_company_weight + margin_sector_weight, 1e-9)
    unlearned_margin_anchor_pct = (
        company_margin_target_pct * margin_company_weight + sector_margin_pct * margin_sector_weight
    ) / unlearned_margin_weight_total
    stable_margin_floor_pct = max(float(ebit_margin_base_pct), unlearned_margin_anchor_pct + max(margin_pattern_pp, 0.0))
    if (
        refined_margin_target < stable_margin_floor_pct
        and company_growth_pct >= -2.0
        and not bool(structural_break.get("detected"))
        and not bool(regime_guardrail.get("applied"))
        and not bool(margin_guardrail.get("applied"))
    ):
        refined_margin_target = round(float(stable_margin_floor_pct), 1)
        stable_margin_floor_guardrail = {
            "applied": True,
            "min_margin_target_pct": refined_margin_target,
            "note": "Stable-margin floor applied: learned evidence cannot compress the target margin below the stable company/sector anchor without a declining or structural-break signal.",
        }
    layered_learning["stable_margin_floor_guardrail"] = stable_margin_floor_guardrail
    layered_learning["thin_evidence_margin_guardrail"] = thin_evidence_margin_guardrail
    learned_reinvestment_confidence = float(learned_metrics.get("reinvestment_confidence") or 0.0)
    if learned_reinvestment_confidence > 0:
        implied_capex_pct = float(learned_metrics.get("reinvestment_rate_pct") or 0.0) + refined_da_pct
        capex_blend_weight = _clamp(
            0.10 + (0.18 * learned_reinvestment_confidence) + (0.06 if structural_break.get("detected") else 0.0),
            0.0,
            0.35,
        )
        refined_capex_pct = round(
            _clamp(
                refined_capex_pct * (1.0 - capex_blend_weight) + implied_capex_pct * capex_blend_weight,
                0.5,
                25.0,
            ),
            1,
        )
        learned_metrics["implied_capex_pct"] = round(implied_capex_pct, 1)
        learned_metrics["capex_blend_weight"] = round(capex_blend_weight, 2)
    else:
        learned_metrics["implied_capex_pct"] = round(refined_capex_pct, 1)
        learned_metrics["capex_blend_weight"] = 0.0
    layered_learning["learned_metrics"] = learned_metrics

    growth_source, growth_warn = _assumption_source(
        weights=growth_weights,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        pattern_overlay_pp=growth_pattern_pp,
        cohort_size=calibrated.calibration_cohort_size,
        review_due=review_due,
    )
    margin_source, margin_warn = _assumption_source(
        weights=margin_weights,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        pattern_overlay_pp=margin_pattern_pp,
        cohort_size=calibrated.calibration_cohort_size,
        review_due=review_due,
    )
    risk_source, risk_warn = _assumption_source(
        weights=risk_weights,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        pattern_overlay_pp=0.0,
        cohort_size=calibrated.calibration_cohort_size,
        review_due=review_due,
    )
    working_capital_source = (
        f"Knowledge model: {working_capital_weight:.0%} company trailing days, "
        f"{working_capital_sector_weight:.0%} sector working-capital prior"
    )
    intensity_source = (
        f"Knowledge model: {intensity_company_weight:.0%} company trailing intensity, "
        f"{intensity_sector_weight:.0%} sector prior"
    )

    global_growth_source = ""
    global_margin_source = ""
    global_risk_source = ""
    relationship_growth_source = ""
    relationship_margin_source = ""
    relationship_risk_source = ""
    if global_learning["enabled"]:
        if abs(global_growth_pp) >= 0.1:
            global_growth_source = (
                f"; global cross-symbol {global_learning['scope']} cohort {global_growth_pp:+.1f}pp "
                f"across {global_learning['cohort_size']} records/{global_learning['sector_span']} sectors"
            )
        if abs(global_margin_pp) >= 0.1:
            global_margin_source = (
                f"; global cross-symbol {global_learning['scope']} cohort {global_margin_pp:+.1f}pp "
                f"across {global_learning['cohort_size']} records/{global_learning['sector_span']} sectors"
            )
        if any(abs(value) >= 0.05 for value in (global_wacc_pp, global_terminal_growth_pp, global_beta_adj)):
            global_risk_source = (
                f"; global cross-symbol {global_learning['scope']} cohort risk overlay "
                f"(WACC {global_wacc_pp:+.1f}pp, g {global_terminal_growth_pp:+.1f}pp, beta {global_beta_adj:+.2f})"
            )
    if relationship_graph.get("enabled"):
        if abs(relationship_growth_pp) >= 0.1:
            relationship_growth_source = (
                f"; relationship graph {relationship_growth_pp:+.1f}pp across {relationship_graph.get('node_count', 0) - 1} connected symbols"
            )
        if abs(relationship_margin_pp) >= 0.1:
            relationship_margin_source = (
                f"; relationship graph {relationship_margin_pp:+.1f}pp from connected analog pathways"
            )
        if any(abs(value) >= 0.05 for value in (relationship_wacc_pp, relationship_terminal_growth_pp, relationship_beta_adj)):
            relationship_risk_source = (
                f"; relationship graph risk overlay (WACC {relationship_wacc_pp:+.1f}pp, g {relationship_terminal_growth_pp:+.1f}pp, beta {relationship_beta_adj:+.2f})"
            )

    review_text = "due now" if review_due else f"in {next_review_in_years} year(s)"
    pattern_text = f" Pattern match: {pattern_name}." if pattern_name else ""
    margin_text = f" {margin_normalisation['note']}" if margin_normalisation["note"] else ""
    global_text = f" {global_learning['note']}" if global_learning["enabled"] else ""
    relationship_text = f" {relationship_graph['note']}" if relationship_graph.get("enabled") else ""
    uncertainty_text = ""
    if structural_break.get("detected"):
        uncertainty_text = (
            f" Structural-break risk {int(round(float(structural_break.get('score') or 0.0) * 100))}% is active; scenario ranges are widened."
        )
    elif uncertainty.get("weak_evidence"):
        uncertainty_text = " Realised evidence is still thin, so confidence stays conservative and scenario ranges remain wider than normal."
    if thin_evidence_margin_guardrail["applied"]:
        uncertainty_text += f" {thin_evidence_margin_guardrail['note']}"
    if margin_guardrail["applied"]:
        uncertainty_text += f" {margin_guardrail['note']}"
    if stable_margin_floor_guardrail["applied"]:
        uncertainty_text += f" {stable_margin_floor_guardrail['note']}"
    if regime_guardrail["applied"]:
        uncertainty_text += f" {regime_guardrail['note']}"
    if market_residual_overlay.get("enabled"):
        uncertainty_text += f" {market_residual_overlay.get('note', '')}"
    analog_text = ""
    if analog_set.analogs:
        analog_text = f" Nearest analogs: {', '.join(match.analog.ticker for match in analog_set.analogs[:3])}."
    summary = (
        "Weighted knowledge model active: company history blended with sector priors, realised cohorts, analog history, "
        f"macro regime memory, and global cross-symbol patterns. Quarterly verification active since IPO. "
        f"Cohort size: {calibrated.calibration_cohort_size}.{pattern_text}{margin_text}{global_text}{relationship_text}{uncertainty_text}{analog_text}"
    )

    assumption_weights = {
        "revenue_growth_near": {
            **{key: round(value, 2) for key, value in growth_weights.items()},
            "company_value": company_growth_pct,
            "sector_value": sector_growth_pct,
            "learned_value": round(calibrated.revenue_growth_adj * 100, 1),
            "pattern_overlay_pp": growth_pattern_pp,
            "global_overlay_pp": global_growth_pp,
            "relationship_overlay_pp": relationship_growth_pp,
            "source": growth_source + global_growth_source + relationship_growth_source,
            "warn": _merge_guardrail_warn(growth_warn, regime_guardrail),
        },
        "terminal_growth": {
            **{key: round(value, 2) for key, value in risk_weights.items()},
            "company_value": terminal_growth,
            "sector_value": terminal_growth,
            "learned_value": round(calibrated.terminal_growth_adj * 100, 1),
            "pattern_overlay_pp": 0.0,
            "global_overlay_pp": global_terminal_growth_pp,
            "relationship_overlay_pp": relationship_terminal_growth_pp,
            "market_implied_overlay_pp": round(market_terminal_growth_pp, 2),
            "market_implied_g_pct": round(market_implied_g * 100, 2) if market_implied_g is not None else None,
            "revenue_regime": _revenue_regime,
            "terminal_g_range": [round(_tg_range[0] * 100, 2), round(_tg_range[1] * 100, 2)],
            "source": risk_source + global_risk_source + relationship_risk_source + market_risk_source,
            "warn": _merge_guardrail_warn(risk_warn, regime_guardrail),
        },
        "ebit_margin_target": {
            **{key: round(value, 2) for key, value in margin_weights.items()},
            "company_value": company_margin_target_pct,
            "sector_value": sector_margin_pct,
            "learned_value": learned_margin_component,
            "pattern_overlay_pp": margin_pattern_pp,
            "global_overlay_pp": global_margin_pp,
            "relationship_overlay_pp": relationship_margin_pp,
            "source": margin_source + global_margin_source + relationship_margin_source + margin_normalisation["source_suffix"] + ("; thin-evidence margin gate applied" if thin_evidence_margin_guardrail["applied"] else "") + ("; margin-volatility safeguard applied" if margin_guardrail["applied"] else "") + ("; stable-margin floor applied" if stable_margin_floor_guardrail["applied"] else "") + ("; declining/structural-break safeguard applied" if regime_guardrail["applied"] else ""),
            "warn": _merge_guardrail_warn(margin_normalisation["note"] or margin_warn, thin_evidence_margin_guardrail, margin_guardrail, stable_margin_floor_guardrail, regime_guardrail),
        },
        "beta": {
            **{key: round(value, 2) for key, value in risk_weights.items()},
            "company_value": round(beta, 2),
            "sector_value": round(sector_beta, 2),
            "learned_value": round(calibrated.beta_adj, 2),
            "pattern_overlay_pp": 0.0,
            "global_overlay": global_beta_adj,
            "relationship_overlay": relationship_beta_adj,
            "source": risk_source + global_risk_source + relationship_risk_source,
            "warn": _merge_guardrail_warn(risk_warn, regime_guardrail),
        },
        "wacc": {
            **{key: round(value, 2) for key, value in risk_weights.items()},
            "company_value": round(wacc, 1),
            "sector_value": round(sector_wacc, 1),
            "learned_value": round(calibrated.wacc_adj * 100, 1),
            "pattern_overlay_pp": 0.0,
            "global_overlay_pp": global_wacc_pp,
            "relationship_overlay_pp": relationship_wacc_pp,
            "market_implied_overlay_pp": round(market_wacc_pp, 2),
            "source": risk_source + global_risk_source + relationship_risk_source + market_risk_source,
            "warn": _merge_guardrail_warn(risk_warn, regime_guardrail),
        },
        "tax_rate_pct": {
            "company_history": 0.75,
            "sector_prior": 0.0,
            "learned_cohort": 0.0,
            "company_value": company_tax_pct,
            "sector_value": 0.0,
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": "Knowledge model: 75% weighted 5y tax history, 25% latest observed rate",
            "warn": growth_warn,
        },
        "da_pct": {
            "company_history": 0.75,
            "sector_prior": 0.25,
            "learned_cohort": 0.0,
            "company_value": company_da_pct,
            "sector_value": round(min(company_capex_pct, sector_capex_pct), 1),
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": "Knowledge model: 75% company trailing D&A intensity, 25% maintenance-capex anchor",
            "warn": None,
        },
        "capex_pct": {
            "company_history": round(intensity_company_weight, 2),
            "sector_prior": round(intensity_sector_weight, 2),
            "learned_cohort": 0.0,
            "company_value": company_capex_pct,
            "sector_value": sector_capex_pct,
            "learned_value": round(float(learned_metrics.get("implied_capex_pct") or 0.0), 1),
            "pattern_overlay_pp": 0.0,
            "source": intensity_source + (
                f"; realised reinvestment proxy implies {float(learned_metrics.get('implied_capex_pct') or 0.0):.1f}% capex"
                if float(learned_metrics.get("reinvestment_evidence") or 0.0) > 0
                else ""
            ),
            "warn": uncertainty.get("note") if uncertainty.get("weak_evidence") or structural_break.get("detected") else None,
        },
        "sbc_pct": {
            "company_history": round(intensity_company_weight, 2),
            "sector_prior": round(intensity_sector_weight, 2),
            "learned_cohort": 0.0,
            "company_value": company_sbc_pct,
            "sector_value": sector_sbc_pct,
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": intensity_source,
            "warn": None,
        },
        "dso": {
            "company_history": round(working_capital_weight, 2),
            "sector_prior": round(working_capital_sector_weight, 2),
            "learned_cohort": 0.0,
            "company_value": round(dso, 1),
            "sector_value": round(sector_wc_days["dso"], 1),
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": working_capital_source,
            "warn": None,
        },
        "dio": {
            "company_history": round(working_capital_weight, 2),
            "sector_prior": round(working_capital_sector_weight, 2),
            "learned_cohort": 0.0,
            "company_value": round(dio, 1),
            "sector_value": round(sector_wc_days["dio"], 1),
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": working_capital_source,
            "warn": None,
        },
        "dpo": {
            "company_history": round(working_capital_weight, 2),
            "sector_prior": round(working_capital_sector_weight, 2),
            "learned_cohort": 0.0,
            "company_value": round(dpo, 1),
            "sector_value": round(sector_wc_days["dpo"], 1),
            "learned_value": 0.0,
            "pattern_overlay_pp": 0.0,
            "source": working_capital_source,
            "warn": None,
        },
    }

    analog_payload = _serialise_analog_set(analog_set)

    explainability = _build_learning_explainability(
        history_window_years=history_window_years,
        completed_years=completed_years,
        review_due=review_due,
        next_review_in_years=next_review_in_years,
        calibrated=calibrated,
        analog_payload=analog_payload,
        global_learning=global_learning,
        pattern_name=pattern_name,
        pattern_score=pattern_score,
        margin_normalisation=margin_normalisation,
        assumption_weights=assumption_weights,
        layered_learning=layered_learning,
        refined_growth=refined_growth,
        refined_margin_target=refined_margin_target,
        refined_wacc=refined_wacc,
        refined_terminal_growth=refined_terminal_growth,
        smoothed_beta=smoothed_beta,
        relationship_graph=relationship_graph,
    )
    confidence_model = build_ranked_confidence_model(
        {
            "sector": sector,
            "industry": industry,
            "calibration_confidence": round(calibrated.calibration_confidence, 2),
            "learning_confidence": round(float(uncertainty.get("effective_confidence") or calibrated.calibration_confidence or 0.0), 2),
            "calibration_cohort_size": calibrated.calibration_cohort_size,
            "history_window_years": history_window_years,
            "quarterly_review_active": True,
            "pattern_match_score": round(pattern_score, 2),
            "scenario_width_multiplier": float(uncertainty.get("scenario_width_multiplier") or margin_normalisation["scenario_width_multiplier"]),
            "wacc": refined_wacc,
            "terminal_growth": refined_terminal_growth,
            "global_learning": global_learning,
            "analogs": analog_payload,
            "relationship_graph": relationship_graph,
            "layered_learning": layered_learning,
            "quality_gate": learning_sample_diagnostics,
            "market_residual_overlay": market_residual_overlay,
            "explainability": explainability,
        }
    )
    explainability["confidence_decomposition"] = {
        "summary": confidence_model["summary"],
        "dominant_risk": confidence_model["dominant_risk"],
        "assumption_confidence": dict(confidence_model["assumption_confidence"]),
        "valuation_confidence": dict(confidence_model["valuation_confidence"]),
        "components": list(confidence_model["components"]),
    }
    memory_hierarchy = _build_memory_hierarchy(
        explainability=explainability,
        confidence_model=confidence_model,
        relationship_graph=relationship_graph,
    )
    explainability["memory_hierarchy"] = memory_hierarchy

    return {
        "summary": summary,
        "review_cadence_years": 5,
        "history_window_years": history_window_years,
        "market_cap_regime": market_cap_regime,
        "quarterly_review_active": True,
        "next_review_in_years": next_review_in_years,
        "calibration_cohort_size": calibrated.calibration_cohort_size,
        "calibration_confidence": round(calibrated.calibration_confidence, 2),
        "learning_confidence": round(float(confidence_model["assumption_confidence"]["score"] or 0.0), 2),
        "assumption_confidence": round(float(confidence_model["assumption_confidence"]["score"] or 0.0), 2),
        "valuation_confidence": round(float(confidence_model["valuation_confidence"]["score"] or 0.0), 2),
        "confidence_ranking_signal": float(confidence_model["ranking_signal"]),
        "expected_valuation_error_pct": float(confidence_model["valuation_confidence"]["expected_error_pct"]["p50"]),
        "expected_valuation_error_band": dict(confidence_model["valuation_confidence"]["expected_error_pct"]),
        "pattern_match": pattern_name,
        "pattern_match_score": round(pattern_score, 2),
        "feature_vector": [round(float(feature_vector.get(name, 0.0)), 6) for name in FEATURE_NAMES],
        "symbol_brain": symbol_features.to_public_dict(),
        "analogs": analog_payload,
        "global_learning": global_learning,
        "relationship_graph": relationship_graph,
        "layered_learning": layered_learning,
        "quality_gate": learning_sample_diagnostics,
        "market_residual_overlay": market_residual_overlay,
        "margin_normalisation": margin_normalisation,
        "thin_evidence_margin_guardrail": thin_evidence_margin_guardrail,
        "margin_guardrail": margin_guardrail,
        "stable_margin_floor_guardrail": stable_margin_floor_guardrail,
        "regime_guardrail": regime_guardrail,
        "scenario_width_multiplier": float(uncertainty.get("scenario_width_multiplier") or margin_normalisation["scenario_width_multiplier"]),
        "confidence_model": confidence_model,
        "calibration_diagnostics": calibrated.calibration_diagnostics.to_dict() if getattr(calibrated, "calibration_diagnostics", None) else {},
        "explainability": explainability,
        "memory_hierarchy": memory_hierarchy,
        "revenue_growth_near": refined_growth,
        "terminal_growth": refined_terminal_growth,
        "ebit_margin_target": refined_margin_target,
        "beta": smoothed_beta,
        "wacc": refined_wacc,
        "tax_rate_pct": refined_tax_pct,
        "da_pct": refined_da_pct,
        "capex_pct": refined_capex_pct,
        "sbc_pct": refined_sbc_pct,
        "dso": refined_dso,
        "dio": refined_dio,
        "dpo": refined_dpo,
        "assumption_weights": assumption_weights,
    }