"""Learning system for adaptive DCF calibration and uncertainty."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AdaptedAssumptionSet": ("adapter", "AdaptedAssumptionSet"),
    "adapt_assumptions": ("adapter", "adapt_assumptions"),
    "ErrorDriver": ("attribution", "ErrorDriver"),
    "aggregate_attributions": ("attribution", "aggregate_attributions"),
    "attribute_postmortem": ("attribution", "attribute_postmortem"),
    "CalibrationObservation": ("_layered_calibrator", "CalibrationObservation"),
    "CalibrationStore": ("_layered_calibrator", "CalibrationStore"),
    "CalibratedAssumptions": ("_layered_calibrator", "CalibratedAssumptions"),
    "calibrate": ("_layered_calibrator", "calibrate"),
    "ConfidenceBundle": ("confidence", "ConfidenceBundle"),
    "ConfidenceInterval": ("confidence", "ConfidenceInterval"),
    "MonteCarloSummary": ("confidence", "MonteCarloSummary"),
    "compute_intervals": ("confidence", "compute_intervals"),
    "run_learning_monte_carlo": ("confidence", "run_learning_monte_carlo"),
    "AnalogCohort": ("cross_industry", "AnalogCohort"),
    "AnalogMatch": ("cross_industry", "AnalogMatch"),
    "AnalogObservation": ("cross_industry", "AnalogObservation"),
    "AnalogSet": ("cross_industry", "AnalogSet"),
    "build_analog_observations": ("cross_industry", "build_analog_observations"),
    "build_relationship_graph": ("relationship_graph", "build_relationship_graph"),
    "compute_global_overlay": ("cross_industry", "compute_global_overlay"),
    "compute_overlay": ("cross_industry", "compute_overlay"),
    "find_analogs": ("cross_industry", "find_analogs"),
    "form_cohorts": ("cross_industry", "form_cohorts"),
    "FeatureObservation": ("feature_space", "FeatureObservation"),
    "SymbolFeatures": ("feature_space", "SymbolFeatures"),
    "build_feature_map": ("feature_space", "build_feature_map"),
    "build_symbol_features": ("feature_space", "build_symbol_features"),
    "LedgerReader": ("ledger", "LedgerReader"),
    "LedgerWriter": ("ledger", "LedgerWriter"),
    "LiveEvidenceBootstrapResult": ("live_evidence_bootstrap", "LiveEvidenceBootstrapResult"),
    "PredictionRecord": ("ledger", "PredictionRecord"),
    "LearningMaintenanceResult": ("maintenance", "LearningMaintenanceResult"),
    "extract_actuals_from_fundamentals": ("maintenance", "extract_actuals_from_fundamentals"),
    "run_live_evidence_bootstrap": ("maintenance", "run_live_evidence_bootstrap"),
    "run_scheduled_learning_maintenance": ("maintenance", "run_scheduled_learning_maintenance"),
    "ResearchInsight": ("online_research", "ResearchInsight"),
    "compute_signal_adjustments": ("online_research", "compute_signal_adjustments"),
    "fetch_insights": ("online_research", "fetch_insights"),
    "PostmortemRecord": ("postmortem", "PostmortemRecord"),
    "QuinquennialReport": ("postmortem", "QuinquennialReport"),
    "QuinquennialStore": ("postmortem", "QuinquennialStore"),
    "run_5year_postmortem": ("postmortem", "run_5year_postmortem"),
    "run_annual_postmortem": ("postmortem", "run_annual_postmortem"),
    "should_run_quinquennial": ("postmortem", "should_run_quinquennial"),
    "SymbolUniverseStore": ("universe", "SymbolUniverseStore"),
}

_SUBMODULES = {
    "adapter",
    "_layered_calibrator",
    "attribution",
    "calibrator",
    "confidence",
    "cross_industry",
    "feature_space",
    "ledger",
    "live_evidence_bootstrap",
    "maintenance",
    "online_research",
    "postmortem",
    "relationship_graph",
    "universe",
}

__all__ = sorted(set(_EXPORTS) | _SUBMODULES)


def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    module = import_module(f"{__name__}.{module_name}")
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))