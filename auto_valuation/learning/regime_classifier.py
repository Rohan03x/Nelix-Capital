"""
learning/regime_classifier.py — Revenue growth regime classifier.

Classifies companies into one of five terminal growth regimes using
historical CAGR windows, NTM consensus, and market-implied terminal g.

Regimes (aligned with headwind_table.classify_revenue_regime):
  - structural_decline  →  terminal g prior: [-6%, +1%]
  - mild_decline        →  terminal g prior: [-3%, +2%]
  - stable              →  terminal g prior: [-1%, +4%]
  - moderate_growth     →  terminal g prior: [ 0%, +5%]
  - strong_growth       →  terminal g prior: [+1%, +6%]

Can run as:
  1. Rule-based (always available, uses headwind_table logic)
  2. LightGBM (optional, trained offline via train_regime_classifier.py;
     falls back to rule-based if model file not found)
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_valuation.assumptions.headwind_table import (
    classify_revenue_regime,
    compute_structural_decline_flag,
    get_industry_headwind_score,
    terminal_g_prior_range,
)
from auto_valuation.learning.storage_paths import PACKAGE_ROOT, learning_models_dir

logger = logging.getLogger(__name__)

# Primary path: writable directory (R2-hydrated on serverless, or local data/ dir).
_MODEL_PATH = learning_models_dir() / "regime_classifier.pkl"
# Fallback: committed .pkl bundled in the package (always present, even on fresh serverless boot)
_MODEL_FALLBACK_PATH = PACKAGE_ROOT / "data" / "regime_classifier.pkl"

# Ordered list of regime labels (used as class indices for the classifier)
REGIME_LABELS: list[str] = [
    "structural_decline",
    "mild_decline",
    "stable",
    "moderate_growth",
    "strong_growth",
]


@dataclass
class RegimeClassification:
    regime: str
    confidence: float
    terminal_g_range: tuple[float, float]
    method: str  # "rule_based" | "lightgbm" | "ridge"
    feature_vector: dict[str, float]
    probabilities: dict[str, float]
    structural_decline_signals: list[str]
    # Layer F Tier 2 — Ridge regression near-term CAGR estimate (None if not available)
    predicted_near_term_cagr: float | None = None


def _build_feature_vector(
    cagr_3yr: float | None,
    cagr_5yr: float | None,
    cagr_10yr: float | None,
    ntm_growth: float | None,
    market_implied_g: float | None,
    structural_break_score: float,
    industry_headwind_score: float,
    revenue_volatility: float,
    margin_volatility: float,
    *,
    rf_rate: float = 0.035,
    wacc: float | None = None,
) -> dict[str, float]:
    """Build a fixed-width feature dict for the regime classifier."""
    def _safe(v: float | None, default: float = 0.0) -> float:
        return float(v) if v is not None else default

    cagr_3 = _safe(cagr_3yr)
    cagr_5 = _safe(cagr_5yr)
    cagr_10 = _safe(cagr_10yr)
    ntm = _safe(ntm_growth)
    mig = _safe(market_implied_g, default=float("nan"))

    return {
        "cagr_3yr": cagr_3,
        "cagr_5yr": cagr_5,
        "cagr_10yr": cagr_10,
        "ntm_growth": ntm,
        "market_implied_g": mig if not (mig != mig) else 0.0,  # replace NaN with 0
        "market_implied_g_available": 0.0 if (mig != mig) else 1.0,
        "structural_break_score": structural_break_score,
        "industry_headwind_score": industry_headwind_score,
        "revenue_volatility": revenue_volatility,
        "margin_volatility": margin_volatility,
        "rf_rate": rf_rate,
        "cagr_3_minus_cagr_10": cagr_3 - cagr_10,      # deceleration signal
        "cagr_trend": cagr_3 - cagr_5,                   # recent acceleration
        "ntm_vs_hist": ntm - cagr_5,                     # consensus optimism/pessimism
        "break_x_volatility": structural_break_score * revenue_volatility,
        "negative_cagr_3": float(cagr_3 < 0.0),
        "negative_cagr_10": float(cagr_10 < 0.0),
        "declining_trend": float(cagr_3 < cagr_5),
        "wacc": float(wacc) if wacc is not None else 0.10,
    }


def _rule_based_classify(
    feature_vector: dict[str, float],
    structural_decline_signals: list[str],
    *,
    rf_rate: float = 0.035,
    sector: str | None = None,
) -> RegimeClassification:
    """Pure rule-based classification using headwind_table logic."""
    cagr_3 = feature_vector.get("cagr_3yr", 0.0)
    cagr_5 = feature_vector.get("cagr_5yr", 0.0)
    cagr_10 = feature_vector.get("cagr_10yr", 0.0)
    ntm = feature_vector.get("ntm_growth", 0.0)
    mig = feature_vector.get("market_implied_g", 0.0) if feature_vector.get("market_implied_g_available", 0.0) > 0.5 else None

    regime = classify_revenue_regime(cagr_3, cagr_5, cagr_10, ntm, mig)
    tg_range = terminal_g_prior_range(regime, rf_rate=rf_rate, sector=sector)

    # Build soft probabilities via triangular scoring for each regime
    probs: dict[str, float] = {}
    for candidate in REGIME_LABELS:
        crange = terminal_g_prior_range(candidate, rf_rate=rf_rate)
        midpoint = (crange[0] + crange[1]) / 2.0
        dist = abs(cagr_3 - midpoint)
        probs[candidate] = max(0.0, 1.0 - dist * 10.0)
    total = sum(probs.values()) or 1.0
    probs = {k: v / total for k, v in probs.items()}

    return RegimeClassification(
        regime=regime,
        confidence=probs.get(regime, 0.5),
        terminal_g_range=tg_range,
        method="rule_based",
        feature_vector=feature_vector,
        probabilities=probs,
        structural_decline_signals=structural_decline_signals,
    )


def _lgbm_classify(
    feature_vector: dict[str, float],
    structural_decline_signals: list[str],
    *,
    rf_rate: float = 0.035,
    sector: str | None = None,
) -> RegimeClassification | None:
    """LightGBM classification — returns None if model not available."""
    try:
        import pickle
        import numpy as np

        model_path = _MODEL_PATH if _MODEL_PATH.exists() else _MODEL_FALLBACK_PATH
        if not model_path.exists():
            return None

        with open(model_path, "rb") as fh:
            model_bundle = pickle.load(fh)

        model = model_bundle["model"]
        feature_names = model_bundle["feature_names"]
        x = np.array([[feature_vector.get(f, 0.0) for f in feature_names]], dtype=float)
        raw_probs = model.predict_proba(x)[0]
        label_map: list[str] = model_bundle.get("labels", REGIME_LABELS)

        probs = {label: float(p) for label, p in zip(label_map, raw_probs)}
        regime = max(probs, key=probs.__getitem__)
        tg_range = terminal_g_prior_range(regime, rf_rate=rf_rate, sector=sector)

        return RegimeClassification(
            regime=regime,
            confidence=probs.get(regime, 0.5),
            terminal_g_range=tg_range,
            method="lightgbm",
            feature_vector=feature_vector,
            probabilities=probs,
            structural_decline_signals=structural_decline_signals,
        )
    except ImportError:
        logger.debug("LightGBM not installed; falling back to rule-based classifier")
        return None
    except Exception as exc:
        logger.warning("LightGBM classifier failed (%s); falling back to rule-based", exc)
        return None


def classify_regime(
    *,
    cagr_3yr: float | None,
    cagr_5yr: float | None,
    cagr_10yr: float | None,
    ntm_growth: float | None = None,
    market_implied_g: float | None = None,
    structural_break_score: float = 0.0,
    industry: str | None = None,
    revenue_volatility: float = 0.0,
    margin_volatility: float = 0.0,
    rf_rate: float = 0.035,
    wacc: float | None = None,
    sector: str | None = None,
    prefer_model: bool = True,
) -> RegimeClassification:
    """
    Classify the revenue regime for a company.

    Parameters
    ----------
    cagr_3yr, cagr_5yr, cagr_10yr : historical revenue CAGR windows
    ntm_growth : NTM analyst consensus revenue growth (optional)
    market_implied_g : reverse-DCF implied terminal growth (optional)
    structural_break_score : calibration structural break score (0-1)
    industry : company industry string (for headwind scoring)
    revenue_volatility, margin_volatility : rolling std of annual changes
    rf_rate : current risk-free rate (for capping terminal g range)
    wacc : cost of capital (used as feature)
    sector : GICS sector string (for terminal_g_prior_range)
    prefer_model : if True, try LightGBM model first, fall back to rules

    Returns
    -------
    RegimeClassification dataclass
    """
    headwind_score = get_industry_headwind_score(industry)
    _, decline_signals = compute_structural_decline_flag(
        cagr_3yr=cagr_3yr,
        cagr_10yr=cagr_10yr,
        market_implied_g=market_implied_g,
        structural_break_score=structural_break_score,
        industry_headwind_score=headwind_score,
    )

    fv = _build_feature_vector(
        cagr_3yr, cagr_5yr, cagr_10yr, ntm_growth, market_implied_g,
        structural_break_score, headwind_score, revenue_volatility, margin_volatility,
        rf_rate=rf_rate, wacc=wacc,
    )

    if prefer_model:
        result = _lgbm_classify(fv, decline_signals, rf_rate=rf_rate, sector=sector)
        if result is not None:
            return _add_tier2_cagr(result, fv)

    base_result = _rule_based_classify(fv, decline_signals, rf_rate=rf_rate, sector=sector)
    return _add_tier2_cagr(base_result, fv)


def _add_tier2_cagr(
    result: RegimeClassification,
    feature_vector: dict[str, float],
) -> RegimeClassification:
    """Enrich a RegimeClassification with the Layer F Tier 2 Ridge CAGR estimate."""
    try:
        from auto_valuation.learning.near_term_cagr_predictor import predict_near_term_cagr
        cagr = predict_near_term_cagr(result.regime, feature_vector)
        return RegimeClassification(
            regime=result.regime,
            confidence=result.confidence,
            terminal_g_range=result.terminal_g_range,
            method=result.method,
            feature_vector=result.feature_vector,
            probabilities=result.probabilities,
            structural_decline_signals=result.structural_decline_signals,
            predicted_near_term_cagr=cagr,
        )
    except Exception as exc:
        logger.debug("Tier 2 CAGR prediction failed: %s", exc)
        return result


__all__ = [
    "RegimeClassification",
    "classify_regime",
    "REGIME_LABELS",
]
