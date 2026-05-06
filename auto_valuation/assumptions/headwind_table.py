"""
assumptions/headwind_table.py — Structural decline detection and terminal growth prior ranges.

Layer C/D of the DCF Accuracy Improvement Plan:
  C — Trajectory-constrained terminal g priors
  D — Structural decline detection using multi-signal scoring

Usage:
    from auto_valuation.assumptions.headwind_table import (
        compute_structural_decline_flag,
        terminal_g_prior_range,
    )
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Industry headwind scores
# A score of 1.0 means one confirmed structural headwind; 2.0 means two.
# Threshold for structural decline classification: >= 1.5
# ---------------------------------------------------------------------------

_INDUSTRY_HEADWIND_SCORE: dict[str, float] = {
    # Secular decliners (high score)
    "Tobacco": 2.0,
    "Print Media": 2.0,
    "Newspaper": 2.0,
    "Landline Telecom": 1.5,
    "Coal Mining": 2.0,
    "Fossil Fuel Extraction": 1.5,
    "Physical Retail": 1.5,
    "Department Stores": 2.0,
    "Video Rental": 2.0,
    "Traditional Photography": 2.0,
    "Optical Disc Manufacturing": 2.0,
    "Land-Based Casinos": 1.5,
    "Legacy Auto OEM": 1.5,
    "Thermal Power": 1.5,
    # Moderate headwinds
    "Oil & Gas": 1.0,
    "Conventional Insurance": 0.5,
    "Broadcast Television": 1.0,
    "Radio Broadcasting": 1.5,
    "Wireline Networks": 1.0,
    "Retail Banking": 0.5,
    "Office REITs": 1.0,
    "Mall REITs": 1.5,
    "Traditional Auto Parts": 1.0,
    "Chemical Manufacturing": 0.5,
    "Generic Pharma": 0.5,
    # Neutral / positive
    "Software": 0.0,
    "Semiconductors": 0.0,
    "Cloud Computing": 0.0,
    "E-Commerce": 0.0,
    "Biotech": 0.0,
    "Renewable Energy": 0.0,
    "Electric Vehicles": 0.0,
    "Medical Devices": 0.0,
    "Asset Management": 0.0,
    "default": 0.0,
}


def get_industry_headwind_score(industry: str | None) -> float:
    """Return the structural headwind score for an industry."""
    if industry is None:
        return _INDUSTRY_HEADWIND_SCORE["default"]
    return _INDUSTRY_HEADWIND_SCORE.get(industry, _INDUSTRY_HEADWIND_SCORE["default"])


# ---------------------------------------------------------------------------
# Revenue trajectory regime classification
# Produces a terminal_g prior range [low, high]
# ---------------------------------------------------------------------------

def classify_revenue_regime(
    cagr_3yr: float | None,
    cagr_5yr: float | None,
    cagr_10yr: float | None,
    ntm_growth: float | None,
    market_implied_g: float | None = None,
) -> str:
    """
    Classify revenue trajectory regime from multi-window CAGRs.

    Regimes:
      - "strong_growth"    : 3yr CAGR > 10%
      - "moderate_growth"  : 3yr CAGR > 4%
      - "stable"           : 3yr CAGR in [-1%, 4%]
      - "mild_decline"     : 3yr CAGR in [-5%, -1%)
      - "structural_decline": 3yr CAGR < -5%, or 5yr CAGR < -3% and 10yr CAGR < 0%
    """
    c3 = cagr_3yr or 0.0
    c5 = cagr_5yr or 0.0
    c10 = cagr_10yr or 0.0
    mig = market_implied_g or 0.0

    if c3 < -0.05 or (c5 < -0.03 and c10 < 0.0):
        return "structural_decline"
    if c3 < -0.01:
        return "mild_decline"
    if c3 >= 0.10:
        return "strong_growth"
    if c3 >= 0.04:
        return "moderate_growth"
    return "stable"


# Regime → terminal g prior range [low, high]
_REGIME_PRIOR_RANGE: dict[str, tuple[float, float]] = {
    "structural_decline": (-0.06, 0.01),
    "mild_decline":       (-0.03, 0.02),
    "stable":             (-0.01, 0.04),
    "moderate_growth":    ( 0.00, 0.05),
    "strong_growth":      ( 0.01, 0.06),
}


def terminal_g_prior_range(
    revenue_regime: str,
    *,
    rf_rate: float | None = None,
    sector: str | None = None,
) -> tuple[float, float]:
    """
    Return the [low, high] terminal growth prior range for a given revenue regime.

    Optionally anchors the upper bound to the risk-free rate + 100bps premium
    when rf_rate is provided (avoids implying supra-sovereign growth at terminal).
    """
    low, high = _REGIME_PRIOR_RANGE.get(revenue_regime, (-0.03, 0.04))
    if rf_rate is not None:
        # Cap high at rf_rate + 1.0% — no company should grow faster than debt in perpetuity
        high = min(high, rf_rate + 0.010)
        # Floor low can stay at -0.06 regardless of rf_rate
    return (low, high)


# ---------------------------------------------------------------------------
# Structural decline detection
# ---------------------------------------------------------------------------

def compute_structural_decline_flag(
    cagr_3yr: float | None,
    cagr_10yr: float | None,
    market_implied_g: float | None,
    structural_break_score: float,
    industry_headwind_score: float,
    *,
    minimum_signals: int = 3,
) -> tuple[bool, list[str]]:
    """
    Detect structural decline using multi-signal scoring.

    Returns (is_structural_decline: bool, triggered_signals: list[str]).
    Threshold: at least `minimum_signals` must fire.

    Signals:
      1. 10yr revenue CAGR < 0%
      2. 3yr CAGR < -3%
      3. Market-implied g < -2%
      4. Structural break score > 0.7
      5. Industry headwind score >= 1.5
    """
    signals: list[str] = []
    if cagr_10yr is not None and cagr_10yr < 0.0:
        signals.append("10yr_cagr_negative")
    if cagr_3yr is not None and cagr_3yr < -0.03:
        signals.append("3yr_cagr_below_neg3pct")
    if market_implied_g is not None and market_implied_g < -0.02:
        signals.append("market_implied_g_below_neg2pct")
    if structural_break_score > 0.70:
        signals.append("structural_break_score_high")
    if industry_headwind_score >= 1.5:
        signals.append("industry_headwind_score_high")

    is_decline = len(signals) >= minimum_signals
    return is_decline, signals


__all__ = [
    "compute_structural_decline_flag",
    "classify_revenue_regime",
    "terminal_g_prior_range",
    "get_industry_headwind_score",
]
