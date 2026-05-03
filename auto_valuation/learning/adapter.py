"""Apply learning signals back into the assumptions pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from auto_valuation.assumptions.engine import AssumptionSet

from ._layered_calibrator import CalibratedAssumptions, calibrate
from .confidence import ConfidenceBundle, compute_intervals
from .cross_industry import AnalogObservation, AnalogSet, apply_overlay, compute_overlay, find_analogs
from .online_research import ResearchInsight, compute_signal_adjustments


@dataclass
class AdaptedAssumptionSet(CalibratedAssumptions):
    confidence_intervals: ConfidenceBundle | None = None
    analog_set: AnalogSet | None = None
    research_insights: list[ResearchInsight] = field(default_factory=list)
    model_confidence_score: float = 0.0
    adjustment_sources: dict[str, str] = field(default_factory=dict)


def _apply_signal_adjustments(calibrated: CalibratedAssumptions, signal_adjustments: dict[str, float]) -> CalibratedAssumptions:
    if not signal_adjustments:
        return calibrated

    revenue_shift = float(signal_adjustments.get("revenue_growth_adj", 0.0))
    margin_shift = float(signal_adjustments.get("ebit_margin_adj", 0.0))
    wacc_shift = float(signal_adjustments.get("wacc_adj", 0.0))
    terminal_growth_shift = float(signal_adjustments.get("terminal_growth_adj", 0.0))

    updated_sources = dict(calibrated.calibration_sources)
    for key, value in signal_adjustments.items():
        updated_sources[key] = f"research_signal:{value:+.4f}"

    return replace(
        calibrated,
        near_term_growth=calibrated.near_term_growth + revenue_shift,
        revenue_growth_adj=calibrated.revenue_growth_adj + revenue_shift,
        revenue_growth_band=(
            calibrated.revenue_growth_band[0] + revenue_shift,
            calibrated.revenue_growth_band[1] + revenue_shift,
        ),
        revenue_growth_rates=[value + revenue_shift for value in calibrated.revenue_growth_rates],
        ebit_margin_current=calibrated.ebit_margin_current + margin_shift,
        ebit_margin_terminal=calibrated.ebit_margin_terminal + margin_shift,
        ebit_margin_adj=calibrated.ebit_margin_adj + margin_shift,
        ebit_margin_band=(
            calibrated.ebit_margin_band[0] + margin_shift,
            calibrated.ebit_margin_band[1] + margin_shift,
        ),
        ebit_margin_schedule=[value + margin_shift for value in calibrated.ebit_margin_schedule],
        wacc_adj=calibrated.wacc_adj + wacc_shift,
        wacc_band=(calibrated.wacc_band[0] + wacc_shift, calibrated.wacc_band[1] + wacc_shift),
        long_run_growth=calibrated.long_run_growth + terminal_growth_shift,
        terminal_growth_adj=calibrated.terminal_growth_adj + terminal_growth_shift,
        terminal_growth_band=(
            calibrated.terminal_growth_band[0] + terminal_growth_shift,
            calibrated.terminal_growth_band[1] + terminal_growth_shift,
        ),
        calibration_sources=updated_sources,
    )


def adapt_assumptions(
    ticker: str,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    raw_assumptions: AssumptionSet,
    research_insights: list[ResearchInsight] | None = None,
    *,
    observations: list[Any] | None = None,
    analog_candidates: list[AnalogObservation] | None = None,
    feature_vector: dict[str, float] | tuple[float, ...] | list[float] | None = None,
    base_wacc: float = 0.10,
    base_terminal_growth: float | None = None,
    base_beta: float = 1.0,
    structural_break_risk: float = 0.0,
    macro_uncertainty: float = 0.0,
    calibration_store: Any | None = None,
) -> AdaptedAssumptionSet:
    """Pipeline that calibrates, overlays analogs, applies research, and scores confidence."""
    research_insights = research_insights or []
    calibrated = calibrate(
        raw_assumptions,
        sector,
        industry,
        data_vintage_years,
        market_cap_regime,
        macro_regime,
        observations=observations,
        base_wacc=base_wacc,
        base_terminal_growth=base_terminal_growth,
        base_beta=base_beta,
        calibration_store=calibration_store,
        ticker=ticker,
        feature_vector=feature_vector,
    )

    analog_set = AnalogSet(subject_ticker=ticker)
    if feature_vector is not None and analog_candidates:
        analog_set = find_analogs(
            ticker,
            feature_vector,
            analog_candidates,
            subject_sector=sector,
            subject_industry=industry,
            subject_vintage_year=data_vintage_years,
        )
        calibrated = apply_overlay(calibrated, compute_overlay(analog_set))

    calibrated = _apply_signal_adjustments(calibrated, compute_signal_adjustments(research_insights))
    diagnostics = getattr(calibrated, "calibration_diagnostics", None)
    learned_structural_break_risk = float(
        getattr(getattr(diagnostics, "structural_break", None), "score", 0.0) or 0.0
    )
    effective_structural_break_risk = max(structural_break_risk, learned_structural_break_risk)
    confidence_bundle = compute_intervals(
        calibrated,
        data_vintage_years,
        calibrated.calibration_confidence,
        analog_set.analog_confidence,
        forecast_years=max(len(calibrated.revenue_growth_rates), len(calibrated.ebit_margin_schedule), 7),
        structural_break_risk=effective_structural_break_risk,
        macro_uncertainty=macro_uncertainty,
        cohort_size=calibrated.calibration_cohort_size,
    )

    adjustment_sources = dict(calibrated.calibration_sources)
    if analog_set.analogs:
        adjustment_sources["analog_overlay"] = f"analogs:{len(analog_set.analogs)}"
    if research_insights:
        adjustment_sources["research"] = f"insights:{len(research_insights)}"

    return AdaptedAssumptionSet(
        **{
            **calibrated.__dict__,
            "confidence_intervals": confidence_bundle,
            "analog_set": analog_set,
            "research_insights": research_insights,
            "model_confidence_score": confidence_bundle.overall_score,
            "adjustment_sources": adjustment_sources,
        }
    )