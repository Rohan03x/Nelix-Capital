"""Empirical-Bayes style calibration from accumulated prediction errors."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from auto_valuation.assumptions.engine import AssumptionSet
from auto_valuation.learning.residual_controls import (
    clamp_assumption_residual,
    robust_bounded_mean,
    robust_bounded_std,
)

try:
    from auto_valuation.config import LEARNING_CONFIG as _LEARNING_CONFIG
except ImportError:
    _LEARNING_CONFIG = {"min_calibration_observations": 5}

CALIBRATION_DB_PATH = Path(__file__).resolve().parent / "db" / "calibration.db"


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
    calibration_cohort_size: int = 0
    calibration_confidence: float = 0.0
    calibration_sources: dict[str, str] = field(default_factory=dict)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def maturity_bucket(data_vintage_years: int) -> str:
    if data_vintage_years <= 3:
        return "1-3"
    if data_vintage_years <= 10:
        return "4-10"
    if data_vintage_years <= 20:
        return "11-20"
    return "21+"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


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


class CalibrationStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else CALIBRATION_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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


def _cohort_confidence(cohort_size: int, min_observations: int) -> float:
    raw_confidence = min(1.0, cohort_size / 15.0)
    if cohort_size < min_observations:
        return min(raw_confidence, 0.35)
    return raw_confidence


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


def _filter_sector_fallback(observations: list[Any], sector: str) -> list[Any]:
    return [observation for observation in observations if _get(observation, "sector", "") == sector]


def _apply_shift(series: list[float], delta: float) -> list[float]:
    return [value + delta for value in series]


def _prior_from_errors(
    assumption_name: str,
    errors: list[float],
    *,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
) -> CalibrationPrior:
    return CalibrationPrior(
        prior_id=str(uuid.uuid4()),
        sector=sector,
        industry=industry,
        maturity_bucket=maturity_bucket(data_vintage_years),
        cap_regime=market_cap_regime,
        macro_regime=macro_regime,
        assumption_name=assumption_name,
        correction_mean=robust_bounded_mean(assumption_name, errors) if errors else 0.0,
        correction_std=robust_bounded_std(assumption_name, errors) if len(errors) > 1 else 0.0,
        cohort_size=len(errors),
        last_updated=date.today(),
    )


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
) -> CalibratedAssumptions:
    observations_list = list(observations or [])
    min_observations = int(min_observations or _LEARNING_CONFIG.get("min_calibration_observations", 5))
    calibration_store = calibration_store or CalibrationStore()

    exact = _filter_exact_cohort(
        observations_list,
        sector,
        industry,
        data_vintage_years,
        market_cap_regime,
        macro_regime,
    )
    sector_fallback = _filter_sector_fallback(observations_list, sector)
    if len(exact) >= min_observations:
        cohort = exact
    elif len(sector_fallback) >= min_observations:
        cohort = sector_fallback
    else:
        cohort = []

    terminal_growth_value = base_terminal_growth if base_terminal_growth is not None else raw_assumptions.long_run_growth
    if not cohort:
        return CalibratedAssumptions(
            **raw_assumptions.__dict__,
            revenue_growth_adj=raw_assumptions.near_term_growth,
            revenue_growth_band=(raw_assumptions.near_term_growth, raw_assumptions.near_term_growth),
            ebit_margin_adj=raw_assumptions.ebit_margin_terminal,
            ebit_margin_band=(raw_assumptions.ebit_margin_terminal, raw_assumptions.ebit_margin_terminal),
            wacc_adj=base_wacc,
            wacc_band=(base_wacc, base_wacc),
            terminal_growth_adj=terminal_growth_value,
            terminal_growth_band=(terminal_growth_value, terminal_growth_value),
            beta_adj=base_beta,
            beta_band=(base_beta, base_beta),
            calibration_cohort_size=0,
            calibration_confidence=0.0,
            calibration_sources={},
        )

    cohort_size = len(cohort)
    confidence = _cohort_confidence(cohort_size, min_observations)
    revenue_errors = _error_series(cohort, "actual_revenue_growth", "predicted_revenue_growth", assumption_name="revenue_growth")
    margin_errors = _error_series(cohort, "actual_ebit_margin", "predicted_ebit_margin", assumption_name="ebit_margin")
    wacc_errors = _error_series(cohort, "actual_wacc", "predicted_wacc", assumption_name="wacc")
    terminal_growth_errors = _error_series(cohort, "actual_terminal_growth", "predicted_terminal_growth", assumption_name="terminal_growth")
    beta_errors = _error_series(cohort, "actual_beta", "predicted_beta", assumption_name="beta")

    priors = {
        "revenue_growth": _prior_from_errors(
            "revenue_growth",
            revenue_errors,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        ),
        "ebit_margin": _prior_from_errors(
            "ebit_margin",
            margin_errors,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        ),
        "wacc": _prior_from_errors(
            "wacc",
            wacc_errors,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        ),
        "terminal_growth": _prior_from_errors(
            "terminal_growth",
            terminal_growth_errors,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        ),
        "beta": _prior_from_errors(
            "beta",
            beta_errors,
            sector=sector,
            industry=industry,
            data_vintage_years=data_vintage_years,
            market_cap_regime=market_cap_regime,
            macro_regime=macro_regime,
        ),
    }
    for prior in priors.values():
        calibration_store.save_prior(prior)

    revenue_delta = priors["revenue_growth"].correction_mean
    margin_delta = priors["ebit_margin"].correction_mean
    wacc_delta = priors["wacc"].correction_mean
    terminal_growth_delta = priors["terminal_growth"].correction_mean
    beta_delta = priors["beta"].correction_mean

    revenue_adj = raw_assumptions.near_term_growth + revenue_delta
    ebit_adj = raw_assumptions.ebit_margin_terminal + margin_delta
    wacc_adj = base_wacc + wacc_delta
    terminal_growth_value = terminal_growth_value + terminal_growth_delta
    beta_adj = base_beta + beta_delta

    source_tag = f"cohort:{sector}|{maturity_bucket(data_vintage_years)}|{market_cap_regime}|{macro_regime}|n={cohort_size}"
    revenue_band = (
        raw_assumptions.near_term_growth + _percentile(revenue_errors, 0.10),
        raw_assumptions.near_term_growth + _percentile(revenue_errors, 0.90),
    )
    margin_band = (
        raw_assumptions.ebit_margin_terminal + _percentile(margin_errors, 0.10),
        raw_assumptions.ebit_margin_terminal + _percentile(margin_errors, 0.90),
    )
    wacc_band = (
        base_wacc + _percentile(wacc_errors, 0.10),
        base_wacc + _percentile(wacc_errors, 0.90),
    )
    terminal_band = (
        (base_terminal_growth if base_terminal_growth is not None else raw_assumptions.long_run_growth) + _percentile(terminal_growth_errors, 0.10),
        (base_terminal_growth if base_terminal_growth is not None else raw_assumptions.long_run_growth) + _percentile(terminal_growth_errors, 0.90),
    )
    beta_band = (
        base_beta + _percentile(beta_errors, 0.10),
        base_beta + _percentile(beta_errors, 0.90),
    )

    return CalibratedAssumptions(
        **{
            **raw_assumptions.__dict__,
            "near_term_growth": revenue_adj,
            "long_run_growth": terminal_growth_value,
            "revenue_growth_rates": _apply_shift(list(raw_assumptions.revenue_growth_rates), revenue_delta),
            "ebit_margin_current": raw_assumptions.ebit_margin_current + margin_delta,
            "ebit_margin_terminal": ebit_adj,
            "ebit_margin_schedule": _apply_shift(list(raw_assumptions.ebit_margin_schedule), margin_delta),
            "revenue_growth_adj": revenue_adj,
            "revenue_growth_band": revenue_band,
            "ebit_margin_adj": ebit_adj,
            "ebit_margin_band": margin_band,
            "wacc_adj": wacc_adj,
            "wacc_band": wacc_band,
            "terminal_growth_adj": terminal_growth_value,
            "terminal_growth_band": terminal_band,
            "beta_adj": beta_adj,
            "beta_band": beta_band,
            "calibration_cohort_size": cohort_size,
            "calibration_confidence": confidence,
            "calibration_sources": {
                "revenue_growth_adj": source_tag,
                "ebit_margin_adj": source_tag,
                "wacc_adj": source_tag,
                "terminal_growth_adj": source_tag,
                "beta_adj": source_tag,
            },
        }
    )


__all__ = [
    "CalibratedAssumptions",
    "CalibrationObservation",
    "CalibrationPrior",
    "CalibrationStore",
    "calibrate",
    "maturity_bucket",
]
