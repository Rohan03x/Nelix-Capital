"""Layered residual calibration from accumulated prediction errors."""

from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable

from auto_valuation.assumptions.engine import AssumptionSet
from auto_valuation.learning.cross_industry import cosine_similarity
from auto_valuation.learning.residual_controls import (
    clamp_assumption_residual,
    robust_bounded_std,
)
from auto_valuation.learning.storage_paths import learning_db_dir

try:
    from auto_valuation.config import LEARNING_CONFIG as _LEARNING_CONFIG
except ImportError:
    _LEARNING_CONFIG = {"min_calibration_observations": 5}

CALIBRATION_DB_PATH = learning_db_dir() / "calibration.db"


@dataclass(frozen=True)
class CalibrationObservation:
    sector: str
    industry: str
    data_vintage_years: int
    market_cap_regime: str
    macro_regime: str
    predicted_revenue_growth: float
    actual_revenue_growth: float
    predicted_ebit_margin: float
    actual_ebit_margin: float
    predicted_wacc: float
    actual_wacc: float
    predicted_terminal_growth: float
    actual_terminal_growth: float
    predicted_beta: float = 1.0
    actual_beta: float = 1.0
    ticker: str = ""
    predicted_ufcf_margin: float | None = None
    actual_ufcf_margin: float | None = None
    predicted_reinvestment_rate: float | None = None
    actual_reinvestment_rate: float | None = None
    structural_break_flag: bool = False
    feature_vector: dict[str, float] | tuple[float, ...] | list[float] | None = None
    quality_score: float = 1.0
    # ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S2/S3/M3) — point-in-time + growth dim
    as_of_year: int | None = None
    rf_rate_at_time: float | None = None
    growth_regime: str = "unknown"


@dataclass(frozen=True)
class CalibrationLayer:
    layer_name: str
    weight: float
    evidence_count: int
    residual_mean: float
    residual_std: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_name": self.layer_name,
            "weight": round(float(self.weight), 4),
            "evidence_count": int(self.evidence_count),
            "residual_mean": round(float(self.residual_mean), 6),
            "residual_std": round(float(self.residual_std), 6),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class AssumptionCalibrationSummary:
    assumption_name: str
    base_value: float
    adjusted_value: float
    residual_adjustment: float
    band: tuple[float, float]
    uncertainty: float
    confidence: float
    evidence_count: int
    conflict_score: float
    weak_evidence: bool
    dominant_layer: str | None
    layers: list[CalibrationLayer] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumption_name": self.assumption_name,
            "base_value": round(float(self.base_value), 6),
            "adjusted_value": round(float(self.adjusted_value), 6),
            "residual_adjustment": round(float(self.residual_adjustment), 6),
            "band": [round(float(self.band[0]), 6), round(float(self.band[1]), 6)],
            "uncertainty": round(float(self.uncertainty), 6),
            "confidence": round(float(self.confidence), 4),
            "evidence_count": int(self.evidence_count),
            "conflict_score": round(float(self.conflict_score), 6),
            "weak_evidence": bool(self.weak_evidence),
            "dominant_layer": self.dominant_layer,
            "layers": [layer.to_dict() for layer in self.layers],
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class StructuralBreakSummary:
    detected: bool = False
    score: float = 0.0
    resembles_sector: str | None = None
    own_sector_similarity: float = 0.0
    alternate_sector_similarity: float = 0.0
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": bool(self.detected),
            "score": round(float(self.score), 4),
            "resembles_sector": self.resembles_sector,
            "own_sector_similarity": round(float(self.own_sector_similarity), 4),
            "alternate_sector_similarity": round(float(self.alternate_sector_similarity), 4),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CalibrationDiagnostics:
    assumptions: dict[str, AssumptionCalibrationSummary] = field(default_factory=dict)
    layer_weights: dict[str, float] = field(default_factory=dict)
    layer_evidence_counts: dict[str, int] = field(default_factory=dict)
    effective_observation_count: int = 0
    overall_confidence: float = 0.0
    scenario_width_multiplier: float = 1.0
    structural_break: StructuralBreakSummary = field(default_factory=StructuralBreakSummary)
    warnings: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumptions": {key: summary.to_dict() for key, summary in self.assumptions.items()},
            "layer_weights": {key: round(float(value), 4) for key, value in self.layer_weights.items()},
            "layer_evidence_counts": {key: int(value) for key, value in self.layer_evidence_counts.items()},
            "effective_observation_count": int(self.effective_observation_count),
            "overall_confidence": round(float(self.overall_confidence), 4),
            "scenario_width_multiplier": round(float(self.scenario_width_multiplier), 4),
            "structural_break": self.structural_break.to_dict(),
            "warnings": list(self.warnings),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class _AssumptionSpec:
    actual_key: str
    predicted_key: str
    base_value: float
    min_sigma: float
    low: float | None = None
    high: float | None = None


@dataclass
class CalibrationPrior:
    prior_id: str
    sector: str
    industry: str
    maturity_bucket: str
    cap_regime: str
    macro_regime: str
    assumption_name: str
    correction_mean: float
    correction_std: float
    cohort_size: int
    last_updated: date


@dataclass
class CalibratedAssumptions(AssumptionSet):
    revenue_growth_adj: float = 0.0
    revenue_growth_band: tuple[float, float] = (0.0, 0.0)
    ebit_margin_adj: float = 0.0
    ebit_margin_band: tuple[float, float] = (0.0, 0.0)
    wacc_adj: float = 0.10
    wacc_band: tuple[float, float] = (0.10, 0.10)
    terminal_growth_adj: float = 0.025
    terminal_growth_band: tuple[float, float] = (0.025, 0.025)
    beta_adj: float = 1.0
    beta_band: tuple[float, float] = (1.0, 1.0)
    ufcf_margin_adj: float = 0.0
    ufcf_margin_band: tuple[float, float] = (0.0, 0.0)
    reinvestment_rate_adj: float = 0.0
    reinvestment_rate_band: tuple[float, float] = (0.0, 0.0)
    calibration_cohort_size: int = 0
    calibration_confidence: float = 0.0
    calibration_sources: dict[str, str] = field(default_factory=dict)
    calibration_diagnostics: CalibrationDiagnostics = field(default_factory=CalibrationDiagnostics)
    scenario_width_multiplier: float = 1.0


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _clamp(value: float, low: float | None = None, high: float | None = None) -> float:
    bounded = float(value)
    if low is not None:
        bounded = max(low, bounded)
    if high is not None:
        bounded = min(high, bounded)
    return bounded


def maturity_bucket(data_vintage_years: int) -> str:
    if data_vintage_years <= 3:
        return "1-3"
    if data_vintage_years <= 10:
        return "4-10"
    if data_vintage_years <= 20:
        return "11-20"
    return "21+"


def _error_series(
    observations: list[Any],
    actual_key: str,
    predicted_key: str,
    *,
    assumption_name: str | None = None,
) -> list[float]:
    errors: list[float] = []
    for observation in observations:
        actual = _get(observation, actual_key)
        predicted = _get(observation, predicted_key)
        if actual is None or predicted is None:
            continue
        raw_error = float(actual) - float(predicted)
        if assumption_name:
            bounded_error = clamp_assumption_residual(assumption_name, raw_error)
            if bounded_error is None:
                continue
            errors.append(bounded_error)
        else:
            errors.append(raw_error)
    return errors


# ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S3) — exponential time-decay weighting.
# An observation from N years ago contributes exp(-DECAY * N) of its weight.
# Half-life ≈ ln(2)/0.15 ≈ 4.6 years.
_TIME_DECAY_RATE: float = 0.15


def _observation_weight(observation: Any, current_year: int) -> float:
    as_of = _get(observation, "as_of_year")
    if as_of is None:
        return 1.0
    try:
        age = max(0.0, float(current_year) - float(as_of))
    except (TypeError, ValueError):
        return 1.0
    time_weight = math.exp(-_TIME_DECAY_RATE * age)
    quality_weight = _get(observation, "quality_score", 1.0)
    try:
        quality_weight = _clamp(float(quality_weight), 0.05, 1.0)
    except (TypeError, ValueError):
        quality_weight = 1.0
    return time_weight * quality_weight


def _weighted_robust_mean(
    observations: list[Any],
    actual_key: str,
    predicted_key: str,
    *,
    assumption_name: str | None = None,
    current_year: int | None = None,
) -> float:
    """Trimmed weighted mean of (actual - predicted) with exponential time decay.
    Falls back to unweighted ``_robust_mean`` when no ``as_of_year`` is present."""
    cur = current_year or date.today().year
    pairs: list[tuple[float, float]] = []
    for o in observations:
        a = _get(o, actual_key)
        p = _get(o, predicted_key)
        if a is None or p is None:
            continue
        err = float(a) - float(p)
        if assumption_name:
            bounded_err = clamp_assumption_residual(assumption_name, err)
            if bounded_err is None:
                continue
            err = bounded_err
        w = _observation_weight(o, cur)
        if w <= 0:
            continue
        pairs.append((err, w))
    if not pairs:
        return 0.0
    if all(abs(w - pairs[0][1]) < 1e-9 for _, w in pairs):
        return _robust_mean([e for e, _ in pairs])
    pairs.sort(key=lambda t: t[0])
    if len(pairs) > 4:
        trim = max(1, int(len(pairs) * 0.1))
        pairs = pairs[trim:-trim] or pairs
    total_w = sum(w for _, w in pairs)
    return sum(e * w for e, w in pairs) / total_w if total_w > 0 else 0.0



def _robust_mean(values: list[float]) -> float:
    clean = sorted(float(value) for value in values)
    if not clean:
        return 0.0
    if len(clean) <= 2:
        return sum(clean) / len(clean)
    trim = max(1, int(len(clean) * 0.1))
    if len(clean) - (trim * 2) < 2:
        return median(clean)
    trimmed = clean[trim:-trim]
    return sum(trimmed) / len(trimmed)


def _mad(values: list[float]) -> float:
    clean = [float(value) for value in values]
    if not clean:
        return 0.0
    center = median(clean)
    deviations = [abs(value - center) for value in clean]
    return median(deviations)


def _robust_std(values: list[float]) -> float:
    clean = [float(value) for value in values]
    if len(clean) < 2:
        return 0.0
    mad = _mad(clean)
    if mad > 0:
        return 1.4826 * mad
    center = _robust_mean(clean)
    variance = sum((value - center) ** 2 for value in clean) / len(clean)
    return math.sqrt(max(variance, 0.0))


def _band(point: float, sigma: float, low: float | None = None, high: float | None = None) -> tuple[float, float]:
    spread = 1.2815515655446004 * sigma
    return (_clamp(point - spread, low, high), _clamp(point + spread, low, high))


class CalibrationStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else CALIBRATION_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        return conn

    def _ensure_schema(self) -> None:
        with self._connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_priors (
                    prior_id TEXT PRIMARY KEY,
                    sector TEXT NOT NULL,
                    industry TEXT,
                    maturity_bucket TEXT NOT NULL,
                    cap_regime TEXT NOT NULL,
                    macro_regime TEXT NOT NULL,
                    assumption_name TEXT NOT NULL,
                    correction_mean REAL NOT NULL,
                    correction_std REAL NOT NULL,
                    cohort_size INTEGER NOT NULL,
                    last_updated TEXT NOT NULL,
                    UNIQUE(sector, industry, maturity_bucket, cap_regime, macro_regime, assumption_name)
                )
                """
            )
            conn.commit()

    def save_prior(self, prior: CalibrationPrior) -> None:
        with self._connect_db() as conn:
            conn.execute(
                """
                INSERT INTO calibration_priors (
                    prior_id, sector, industry, maturity_bucket, cap_regime, macro_regime,
                    assumption_name, correction_mean, correction_std, cohort_size, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sector, industry, maturity_bucket, cap_regime, macro_regime, assumption_name)
                DO UPDATE SET
                    correction_mean=excluded.correction_mean,
                    correction_std=excluded.correction_std,
                    cohort_size=excluded.cohort_size,
                    last_updated=excluded.last_updated,
                    prior_id=excluded.prior_id
                """,
                (
                    prior.prior_id,
                    prior.sector,
                    prior.industry,
                    prior.maturity_bucket,
                    prior.cap_regime,
                    prior.macro_regime,
                    prior.assumption_name,
                    prior.correction_mean,
                    prior.correction_std,
                    prior.cohort_size,
                    prior.last_updated.isoformat(),
                ),
            )
            conn.commit()


def _filter_exact_cohort(
    observations: list[Any],
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
) -> list[Any]:
    target_bucket = maturity_bucket(data_vintage_years)
    return [
        observation
        for observation in observations
        if _get(observation, "sector", "") == sector
        and (_get(observation, "industry", "") in (industry, "", None) or not industry)
        and maturity_bucket(int(_get(observation, "data_vintage_years", 0) or 0)) == target_bucket
        and _get(observation, "market_cap_regime", "") == market_cap_regime
        and _get(observation, "macro_regime", "") == macro_regime
    ]


def _filter_company_memory(observations: list[Any], ticker: str | None) -> list[Any]:
    if not ticker:
        return []
    ticker_upper = ticker.upper()
    return [observation for observation in observations if str(_get(observation, "ticker", "") or "").upper() == ticker_upper]


def _filter_sector_memory(
    observations: list[Any],
    sector: str,
    data_vintage_years: int,
    market_cap_regime: str,
) -> list[Any]:
    target_bucket = maturity_bucket(data_vintage_years)
    return [
        observation
        for observation in observations
        if _get(observation, "sector", "") == sector
        and maturity_bucket(int(_get(observation, "data_vintage_years", 0) or 0)) == target_bucket
        and _get(observation, "market_cap_regime", "") == market_cap_regime
    ]


def _filter_macro_memory(observations: list[Any], market_cap_regime: str, macro_regime: str) -> list[Any]:
    return [
        observation
        for observation in observations
        if _get(observation, "market_cap_regime", "") == market_cap_regime
        and _get(observation, "macro_regime", "") == macro_regime
    ]


def _exclude(observations: list[Any], used_ids: set[int]) -> list[Any]:
    return [observation for observation in observations if id(observation) not in used_ids]


def _filter_analog_memory(
    observations: list[Any],
    feature_vector: dict[str, float] | tuple[float, ...] | list[float] | None,
    used_ids: set[int],
) -> list[Any]:
    if feature_vector is None:
        return []
    min_similarity = float(_LEARNING_CONFIG.get("min_analog_similarity", 0.82))
    max_results = int(_LEARNING_CONFIG.get("max_analogs_returned", 8))
    scored: list[tuple[float, Any]] = []
    for observation in observations:
        if id(observation) in used_ids:
            continue
        analog_vector = _get(observation, "feature_vector")
        if analog_vector is None:
            continue
        similarity = cosine_similarity(feature_vector, analog_vector)
        if similarity < min_similarity:
            continue
        scored.append((similarity, observation))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [observation for _, observation in scored[:max_results]]


def _cohort_similarity(features: Any, observations: list[Any]) -> float:
    similarities: list[float] = []
    for observation in observations:
        vector = _get(observation, "feature_vector")
        if vector is None:
            continue
        similarity = cosine_similarity(features, vector)
        if similarity > 0:
            similarities.append(similarity)
    return _robust_mean(similarities) if similarities else 0.0


def _estimate_structural_break(
    observations: list[Any],
    *,
    sector: str,
    subject_features: dict[str, float] | tuple[float, ...] | list[float] | None,
) -> StructuralBreakSummary:
    if not observations:
        return StructuralBreakSummary()

    flagged_count = sum(1 for observation in observations if bool(_get(observation, "structural_break_flag", False)))
    flagged_ratio = flagged_count / len(observations)
    own_similarity = 0.0
    alt_similarity = 0.0
    alt_sector: str | None = None

    if subject_features is not None:
        same_sector = [observation for observation in observations if _get(observation, "sector", "") == sector]
        own_similarity = _cohort_similarity(subject_features, same_sector)
        sector_groups: dict[str, list[Any]] = {}
        for observation in observations:
            candidate_sector = str(_get(observation, "sector", "") or "")
            if not candidate_sector or candidate_sector == sector:
                continue
            sector_groups.setdefault(candidate_sector, []).append(observation)
        for candidate_sector, group in sector_groups.items():
            similarity = _cohort_similarity(subject_features, group)
            if similarity > alt_similarity:
                alt_similarity = similarity
                alt_sector = candidate_sector

    similarity_gap = alt_similarity - own_similarity
    similarity_signal = _clamp((similarity_gap - 0.05) / 0.20, 0.0, 1.0)
    score = _clamp(max(flagged_ratio, similarity_signal), 0.0, 1.0)
    detected = score >= 0.45
    rationale: str | None = None
    if similarity_signal >= flagged_ratio and alt_sector:
        rationale = f"Current feature profile resembles {alt_sector} analogs more than its legacy {sector} cohort."
    elif flagged_ratio > 0:
        rationale = f"{flagged_count} matured observations in this learning set already flagged structural breaks."
    return StructuralBreakSummary(
        detected=detected,
        score=score,
        resembles_sector=alt_sector,
        own_sector_similarity=round(own_similarity, 3),
        alternate_sector_similarity=round(alt_similarity, 3),
        rationale=rationale,
    )


def _layered_sources(
    observations: list[Any],
    *,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    ticker: str | None,
    feature_vector: dict[str, float] | tuple[float, ...] | list[float] | None,
) -> list[tuple[str, list[Any], float, str]]:
    company_memory = _filter_company_memory(observations, ticker)
    used_ids = {id(observation) for observation in company_memory}

    exact = _exclude(
        _filter_exact_cohort(
            observations,
            sector,
            industry,
            data_vintage_years,
            market_cap_regime,
            macro_regime,
        ),
        used_ids,
    )
    used_ids.update(id(observation) for observation in exact)

    sector_memory = _exclude(
        _filter_sector_memory(observations, sector, data_vintage_years, market_cap_regime),
        used_ids,
    )
    used_ids.update(id(observation) for observation in sector_memory)

    analog_memory = _filter_analog_memory(observations, feature_vector, used_ids)
    used_ids.update(id(observation) for observation in analog_memory)

    macro_memory = _exclude(_filter_macro_memory(observations, market_cap_regime, macro_regime), used_ids)
    used_ids.update(id(observation) for observation in macro_memory)

    global_memory = _exclude(observations, used_ids)

    return [
        ("company_memory", company_memory, 1.45, "Same-symbol realised history from prior matured forecasts."),
        ("cohort_memory", exact, 1.20, "Same industry, maturity, cap regime, and macro regime as the current valuation."),
        ("sector_memory", sector_memory, 0.95, "Same sector and maturity bucket, used when the exact cohort is sparse or noisy."),
        ("analog_memory", analog_memory, 0.90, "Cross-symbol analogs with highly similar operating fingerprints."),
        ("macro_memory", macro_memory, 0.75, "Same market-cap and macro regime, even when the sector differs."),
        ("global_memory", global_memory, 0.55, "Cross-symbol residual memory used only as a low-confidence stabilizer."),
    ]


def _apply_shift(series: list[float], delta: float) -> list[float]:
    return [value + delta for value in series]


def _save_prior(
    calibration_store: CalibrationStore,
    *,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    assumption_name: str,
    correction_mean: float,
    correction_std: float,
    cohort_size: int,
) -> None:
    calibration_store.save_prior(
        CalibrationPrior(
            prior_id=str(uuid.uuid4()),
            sector=sector,
            industry=industry,
            maturity_bucket=maturity_bucket(data_vintage_years),
            cap_regime=market_cap_regime,
            macro_regime=macro_regime,
            assumption_name=assumption_name,
            correction_mean=correction_mean,
            correction_std=correction_std,
            cohort_size=cohort_size,
            last_updated=date.today(),
        )
    )


def _build_assumption_summary(
    assumption_name: str,
    spec: _AssumptionSpec,
    layer_sources: list[tuple[str, list[Any], float, str]],
    min_observations: int,
    structural_break: StructuralBreakSummary,
) -> AssumptionCalibrationSummary:
    layer_payloads: list[tuple[str, int, float, float, float, str]] = []
    for layer_name, cohort, priority, rationale in layer_sources:
        errors = _error_series(cohort, spec.actual_key, spec.predicted_key, assumption_name=assumption_name)
        if not errors:
            continue
        # S3 — exponential time-decay weighted residual (recent obs > older obs).
        residual_mean = _weighted_robust_mean(
            cohort,
            spec.actual_key,
            spec.predicted_key,
            assumption_name=assumption_name,
        )
        residual_std = max(robust_bounded_std(assumption_name, errors), spec.min_sigma)
        scale_penalty = 1.0 + (residual_std / max(spec.min_sigma, 1e-6))
        raw_weight = priority * math.sqrt(len(errors)) / scale_penalty
        if layer_name in {"company_memory", "cohort_memory", "sector_memory"} and structural_break.detected:
            raw_weight *= max(0.2, 1.0 - (0.60 * structural_break.score))
        elif layer_name in {"analog_memory", "macro_memory", "global_memory"} and structural_break.score > 0:
            raw_weight *= 1.0 + (0.30 * structural_break.score)
        layer_payloads.append((layer_name, len(errors), residual_mean, residual_std, raw_weight, rationale))

    if not layer_payloads:
        adjusted = _clamp(spec.base_value, spec.low, spec.high)
        return AssumptionCalibrationSummary(
            assumption_name=assumption_name,
            base_value=spec.base_value,
            adjusted_value=adjusted,
            residual_adjustment=0.0,
            band=(adjusted, adjusted),
            uncertainty=0.0,
            confidence=0.0,
            evidence_count=0,
            conflict_score=0.0,
            weak_evidence=True,
            dominant_layer=None,
            layers=[],
            rationale="No matured evidence was available for this assumption, so the model keeps the unlearned base value.",
        )

    total_raw_weight = sum(payload[4] for payload in layer_payloads) or 1.0
    layers = [
        CalibrationLayer(
            layer_name=layer_name,
            weight=raw_weight / total_raw_weight,
            evidence_count=evidence_count,
            residual_mean=residual_mean,
            residual_std=residual_std,
            rationale=rationale,
        )
        for layer_name, evidence_count, residual_mean, residual_std, raw_weight, rationale in layer_payloads
    ]

    effective_count = round(sum(layer.weight * layer.evidence_count for layer in layers))
    conflict_score = _robust_std([layer.residual_mean for layer in layers]) if len(layers) > 1 else 0.0
    uncertainty = max(
        spec.min_sigma,
        sum(layer.weight * layer.residual_std for layer in layers),
        conflict_score,
    )
    weak_evidence = effective_count < min_observations
    if weak_evidence:
        uncertainty *= 1.35
    if structural_break.score > 0:
        uncertainty *= 1.0 + (0.75 * structural_break.score)

    adjustment = sum(layer.weight * layer.residual_mean for layer in layers)
    bounded_adjustment = clamp_assumption_residual(assumption_name, adjustment)
    adjustment = bounded_adjustment if bounded_adjustment is not None else 0.0
    if weak_evidence:
        adjustment = 0.0

    adjusted_value = _clamp(spec.base_value + adjustment, spec.low, spec.high)
    band = _band(adjusted_value, uncertainty, spec.low, spec.high)
    evidence_confidence = min(1.0, effective_count / max(float(min_observations * 2), 1.0))
    agreement_confidence = 1.0 / (1.0 + (conflict_score / max(spec.min_sigma, 1e-6)))
    confidence = _clamp(
        (0.60 * evidence_confidence) + (0.25 * agreement_confidence) + (0.15 * (1.0 - structural_break.score)),
        0.0,
        1.0,
    )
    if weak_evidence:
        confidence = min(confidence, 0.35)

    dominant_layer = max(layers, key=lambda item: item.weight).layer_name if layers else None
    notes: list[str] = []
    if dominant_layer:
        notes.append(f"{dominant_layer} provides the largest residual signal.")
    if weak_evidence:
        notes.append("Evidence is thin, so confidence is capped and the band is widened instead of forcing an adjustment.")
    elif conflict_score > spec.min_sigma:
        notes.append("Evidence layers disagree, so confidence is reduced and uncertainty is widened.")
    if structural_break.detected and structural_break.rationale:
        notes.append(structural_break.rationale)

    return AssumptionCalibrationSummary(
        assumption_name=assumption_name,
        base_value=spec.base_value,
        adjusted_value=adjusted_value,
        residual_adjustment=adjustment,
        band=band,
        uncertainty=uncertainty,
        confidence=confidence,
        evidence_count=effective_count,
        conflict_score=conflict_score,
        weak_evidence=weak_evidence,
        dominant_layer=dominant_layer,
        layers=layers,
        rationale=" ".join(notes),
    )


def _summary_source(
    summary: AssumptionCalibrationSummary,
    *,
    sector: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
) -> str:
    if not summary.layers:
        return "layered_calibration:none"
    layers = ", ".join(
        f"{layer.layer_name}:{layer.weight:.0%}/n={layer.evidence_count}"
        for layer in summary.layers
    )
    return f"layered:{sector}|{maturity_bucket(data_vintage_years)}|{market_cap_regime}|{macro_regime}|{layers}"


def _aggregate_layer_weights(summaries: dict[str, AssumptionCalibrationSummary]) -> tuple[dict[str, float], dict[str, int]]:
    weight_totals: dict[str, float] = {}
    count_totals: dict[str, int] = {}
    tracked = 0
    for summary in summaries.values():
        if not summary.layers:
            continue
        tracked += 1
        for layer in summary.layers:
            weight_totals[layer.layer_name] = weight_totals.get(layer.layer_name, 0.0) + layer.weight
            count_totals[layer.layer_name] = max(count_totals.get(layer.layer_name, 0), layer.evidence_count)
    if tracked <= 0:
        return {}, {}
    return ({key: value / tracked for key, value in weight_totals.items()}, count_totals)


def calibrate(
    raw_assumptions: AssumptionSet,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    *,
    observations: Iterable[Any] | None = None,
    base_wacc: float = 0.10,
    base_terminal_growth: float | None = None,
    base_beta: float = 1.0,
    min_observations: int | None = None,
    calibration_store: CalibrationStore | None = None,
    ticker: str | None = None,
    feature_vector: dict[str, float] | tuple[float, ...] | list[float] | None = None,
) -> CalibratedAssumptions:
    observations_list = list(observations or [])
    min_observations = int(min_observations or _LEARNING_CONFIG.get("min_calibration_observations", 5))
    calibration_store = calibration_store or CalibrationStore()

    terminal_base = base_terminal_growth if base_terminal_growth is not None else raw_assumptions.long_run_growth
    ufcf_margin_base = (
        raw_assumptions.ebit_margin_terminal * (1.0 - raw_assumptions.effective_tax_rate)
        + raw_assumptions.da_pct_revenue
        + raw_assumptions.sbc_pct_revenue
        - raw_assumptions.capex_pct_revenue
    )
    reinvestment_base = max(raw_assumptions.capex_pct_revenue - raw_assumptions.da_pct_revenue, 0.0)

    structural_break = _estimate_structural_break(
        observations_list,
        sector=sector,
        subject_features=feature_vector,
    )
    layer_sources = _layered_sources(
        observations_list,
        sector=sector,
        industry=industry,
        data_vintage_years=data_vintage_years,
        market_cap_regime=market_cap_regime,
        macro_regime=macro_regime,
        ticker=ticker,
        feature_vector=feature_vector,
    )

    summaries = {
        "revenue_growth": _build_assumption_summary(
            "revenue_growth",
            _AssumptionSpec("actual_revenue_growth", "predicted_revenue_growth", raw_assumptions.near_term_growth, 0.01, -0.25, 0.60),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "ebit_margin": _build_assumption_summary(
            "ebit_margin",
            _AssumptionSpec("actual_ebit_margin", "predicted_ebit_margin", raw_assumptions.ebit_margin_terminal, 0.01, -0.25, 0.60),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "wacc": _build_assumption_summary(
            "wacc",
            _AssumptionSpec("actual_wacc", "predicted_wacc", base_wacc, 0.005, 0.03, 0.25),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "terminal_growth": _build_assumption_summary(
            "terminal_growth",
            _AssumptionSpec("actual_terminal_growth", "predicted_terminal_growth", terminal_base, 0.003, 0.0, 0.06),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "beta": _build_assumption_summary(
            "beta",
            _AssumptionSpec("actual_beta", "predicted_beta", base_beta, 0.05, 0.20, 3.50),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "ufcf_margin": _build_assumption_summary(
            "ufcf_margin",
            _AssumptionSpec("actual_ufcf_margin", "predicted_ufcf_margin", ufcf_margin_base, 0.01, -0.30, 0.40),
            layer_sources,
            min_observations,
            structural_break,
        ),
        "reinvestment_rate": _build_assumption_summary(
            "reinvestment_rate",
            _AssumptionSpec("actual_reinvestment_rate", "predicted_reinvestment_rate", reinvestment_base, 0.008, 0.0, 0.25),
            layer_sources,
            min_observations,
            structural_break,
        ),
    }

    for assumption_name, summary in summaries.items():
        _save_prior(
            calibration_store,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
            assumption_name=assumption_name,
            correction_mean=summary.residual_adjustment,
            correction_std=summary.uncertainty,
            cohort_size=summary.evidence_count,
        )
        for layer in summary.layers:
            _save_prior(
                calibration_store,
                sector=sector,
                industry=industry,
                data_vintage_years=data_vintage_years,
                market_cap_regime=market_cap_regime,
                macro_regime=macro_regime,
                assumption_name=f"{assumption_name}@{layer.layer_name}",
                correction_mean=layer.residual_mean,
                correction_std=layer.residual_std,
                cohort_size=layer.evidence_count,
            )

    core_keys = ["revenue_growth", "ebit_margin", "wacc", "terminal_growth", "beta"]
    overall_confidence = _clamp(sum(summaries[name].confidence for name in core_keys) / len(core_keys), 0.0, 1.0)
    layer_weights, layer_counts = _aggregate_layer_weights({name: summaries[name] for name in core_keys})
    warnings: list[str] = []
    if any(summaries[name].weak_evidence for name in core_keys):
        warnings.append("Evidence is thin for at least one core assumption; ranges were widened and confidence capped.")
    if any(summaries[name].conflict_score > summaries[name].uncertainty * 0.5 for name in core_keys):
        warnings.append("Residual evidence conflicts across memory layers; the model is reporting lower confidence rather than forcing precision.")
    if structural_break.detected and structural_break.rationale:
        warnings.append(structural_break.rationale)
    scenario_width_multiplier = round(
        _clamp(1.0 + ((1.0 - overall_confidence) * 0.7) + (structural_break.score * 0.6), 1.0, 2.5),
        2,
    )

    diagnostics = CalibrationDiagnostics(
        assumptions=summaries,
        layer_weights=layer_weights,
        layer_evidence_counts=layer_counts,
        effective_observation_count=max((summary.evidence_count for summary in summaries.values()), default=0),
        overall_confidence=overall_confidence,
        scenario_width_multiplier=scenario_width_multiplier,
        structural_break=structural_break,
        warnings=warnings,
        rationale=(
            "Residual calibration blends company, cohort, sector, analog, macro, and global memory with robust statistics. "
            "Weak or conflicting evidence lowers confidence and widens ranges instead of forcing certainty."
        ),
    )

    revenue_delta = summaries["revenue_growth"].adjusted_value - raw_assumptions.near_term_growth
    margin_delta = summaries["ebit_margin"].adjusted_value - raw_assumptions.ebit_margin_terminal
    source_map = {
        "revenue_growth_adj": _summary_source(summaries["revenue_growth"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "ebit_margin_adj": _summary_source(summaries["ebit_margin"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "wacc_adj": _summary_source(summaries["wacc"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "terminal_growth_adj": _summary_source(summaries["terminal_growth"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "beta_adj": _summary_source(summaries["beta"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "ufcf_margin_adj": _summary_source(summaries["ufcf_margin"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
        "reinvestment_rate_adj": _summary_source(summaries["reinvestment_rate"], sector=sector, data_vintage_years=data_vintage_years, market_cap_regime=market_cap_regime, macro_regime=macro_regime),
    }

    return CalibratedAssumptions(
        **{
            **raw_assumptions.__dict__,
            "near_term_growth": summaries["revenue_growth"].adjusted_value,
            "long_run_growth": summaries["terminal_growth"].adjusted_value,
            "revenue_growth_rates": _apply_shift(list(raw_assumptions.revenue_growth_rates), revenue_delta),
            "ebit_margin_current": raw_assumptions.ebit_margin_current + margin_delta,
            "ebit_margin_terminal": summaries["ebit_margin"].adjusted_value,
            "ebit_margin_schedule": _apply_shift(list(raw_assumptions.ebit_margin_schedule), margin_delta),
            "revenue_growth_adj": summaries["revenue_growth"].adjusted_value,
            "revenue_growth_band": summaries["revenue_growth"].band,
            "ebit_margin_adj": summaries["ebit_margin"].adjusted_value,
            "ebit_margin_band": summaries["ebit_margin"].band,
            "wacc_adj": summaries["wacc"].adjusted_value,
            "wacc_band": summaries["wacc"].band,
            "terminal_growth_adj": summaries["terminal_growth"].adjusted_value,
            "terminal_growth_band": summaries["terminal_growth"].band,
            "beta_adj": summaries["beta"].adjusted_value,
            "beta_band": summaries["beta"].band,
            "ufcf_margin_adj": summaries["ufcf_margin"].adjusted_value,
            "ufcf_margin_band": summaries["ufcf_margin"].band,
            "reinvestment_rate_adj": summaries["reinvestment_rate"].adjusted_value,
            "reinvestment_rate_band": summaries["reinvestment_rate"].band,
            "calibration_cohort_size": diagnostics.effective_observation_count,
            "calibration_confidence": diagnostics.overall_confidence,
            "calibration_sources": source_map,
            "calibration_diagnostics": diagnostics,
            "scenario_width_multiplier": diagnostics.scenario_width_multiplier,
        }
    )


__all__ = [
    "AssumptionCalibrationSummary",
    "CalibratedAssumptions",
    "CalibrationDiagnostics",
    "CalibrationLayer",
    "CalibrationObservation",
    "CalibrationPrior",
    "CalibrationStore",
    "StructuralBreakSummary",
    "calibrate",
    "maturity_bucket",
]