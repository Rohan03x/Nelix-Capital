"""
assumptions/growth.py — Revenue growth and EBIT margin assumption builders.

Reference: Architecture Plan Parts 5, 6, 10, 11, 35, 38, 42.

Integrates:
  - Historical CAGR (from fetcher/cleaner)
  - NTM analyst consensus (from FMP estimates)
  - Damodaran sector median growth
  - Fade schedule toward terminal growth rate
"""

from __future__ import annotations

import statistics
from typing import Any

from auto_valuation.assumptions.headwind_table import (
    classify_revenue_regime,
    compute_structural_decline_flag,
    get_industry_headwind_score,
    terminal_g_prior_range,
)


# ─────────────────────────────────────────────────────────────────────────────
# Growth anchor: blend sources  (Part 5, 38)
# ─────────────────────────────────────────────────────────────────────────────

def blend_growth_estimate(
    historical_cagr: float,
    ntm_consensus: float | None,
    sector_median_growth: float | None,
    weights: tuple[float, float, float] = (0.40, 0.40, 0.20),
    *,
    analyst_count: int = 0,
    sector_analyst_accuracy: float | None = None,
) -> float:
    """
    Blend three growth anchors into a single near-term growth rate:
      - historical_cagr   (weight 0.40 default)
      - ntm_consensus     (weight 0.40 default — or 0 if not available)
      - sector_median     (weight 0.20 default — or redistributed if not available)

    Missing sources have their weight redistributed pro-rata to the others.

    analyst_count : number of analyst estimates (< 3 signals sparse coverage → reduce NTM weight)
    sector_analyst_accuracy : sector-level analyst track record (< 0.5 → reduce NTM weight)

    Reference: Parts 5, 38; ADAPTIVE_DCF_IMPROVEMENT_PLAN.md Layer F Tier 3.
    """
    # Dynamic NTM weight adjustment based on analyst coverage depth and accuracy.
    # Only penalise when we have explicit thin-coverage evidence (analyst_count > 0).
    ntm_w = weights[1]
    if ntm_consensus is not None:
        if analyst_count > 0 and analyst_count < 3:
            ntm_w = max(0.10, ntm_w - 0.15)
        if sector_analyst_accuracy is not None and sector_analyst_accuracy < 0.50:
            ntm_w = max(0.10, ntm_w - 0.10)

    sources = [
        (historical_cagr, weights[0]),
        (ntm_consensus,   ntm_w),
        (sector_median_growth, weights[2]),
    ]
    # Filter available sources
    available = [(v, w) for v, w in sources if v is not None]
    if not available:
        return 0.05   # absolute fallback

    total_weight = sum(w for _, w in available)
    blended = sum(v * w / total_weight for v, w in available)
    return blended


# ─────────────────────────────────────────────────────────────────────────────
# NTM estimates integration  (Part 35)
# ─────────────────────────────────────────────────────────────────────────────

def extract_ntm_growth(ntm_estimates: dict | None, income_stmts: list[dict]) -> float | None:
    """
    Extract implied NTM revenue growth from analyst consensus.

    ntm_estimates: dict from fetch_ntm_estimates() — contains
      estimatedRevenueAvg, estimatedRevenueLow, estimatedRevenueHigh, etc.

    Reference: Part 35.
    """
    if not ntm_estimates:
        return None

    ntm_rev = ntm_estimates.get("estimatedRevenueAvg") or 0
    if ntm_rev <= 0:
        return None

    # Latest annual revenue
    if not income_stmts:
        return None
    latest_rev = income_stmts[0].get("revenue") or 0
    if latest_rev <= 0:
        return None

    return (ntm_rev - latest_rev) / latest_rev


# ─────────────────────────────────────────────────────────────────────────────
# Growth fade schedule  (Part 11, 42)
# ─────────────────────────────────────────────────────────────────────────────

def build_growth_fade_schedule(
    near_term_growth: float,
    terminal_growth: float,
    forecast_years: int = 10,
    hold_years: int = 3,
    fade_years: int | None = None,
) -> list[float]:
    """
    Build a per-year growth rate schedule:
      Years 1 to hold_years: near_term_growth (flat)
      Years hold_years+1 onwards: linear fade toward terminal_growth

    fade_years: number of years over which to fade (default = forecast_years - hold_years).

    Reference: Parts 11, 42.
    """
    if fade_years is None:
        fade_years = max(1, forecast_years - hold_years)

    schedule: list[float] = []
    for yr in range(1, forecast_years + 1):
        if yr <= hold_years:
            g = near_term_growth
        else:
            elapsed = yr - hold_years
            g = near_term_growth + (terminal_growth - near_term_growth) * min(
                elapsed / fade_years, 1.0
            )
        schedule.append(g)
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# EBIT margin fade  (Part 10)
# ─────────────────────────────────────────────────────────────────────────────

# Per-sector mean-reversion speed (alpha) for EBIT margin convergence.
# Higher alpha = faster mean-reversion toward sector median.
# Source: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md Layer G.
_SECTOR_MARGIN_REVERSION_SPEED: dict[str, float] = {
    "Information Technology":  0.22,
    "Health Care":             0.20,
    "Biotechnology":           0.25,
    "Consumer Discretionary":  0.18,
    "Consumer Staples":        0.15,
    "Industrials":             0.12,
    "Energy":                  0.13,
    "Materials":               0.14,
    "Financials":              0.16,
    "Utilities":               0.10,
    "Real Estate":             0.11,
    "Communication Services":  0.20,
    "default":                 0.18,
}


def build_margin_fade_schedule(
    base_margin: float,
    target_margin: float,
    forecast_years: int = 10,
    fade_years: int = 7,
    *,
    sector: str | None = None,
    reversion_speed: float | None = None,
) -> list[float]:
    """
    Mean-reversion fade from base_margin toward target_margin.

    Uses: margin_t = margin_{t-1} + alpha * (target_margin - margin_{t-1})
    where alpha is a per-sector speed-of-adjustment parameter.

    After fade_years the margin is held at target_margin.

    Reference: Part 10; ADAPTIVE_DCF_IMPROVEMENT_PLAN.md Layer G.
    """
    alpha = reversion_speed
    if alpha is None:
        alpha = _SECTOR_MARGIN_REVERSION_SPEED.get(
            sector or "", _SECTOR_MARGIN_REVERSION_SPEED["default"]
        )
    schedule: list[float] = []
    m = base_margin
    for yr in range(1, forecast_years + 1):
        if yr <= fade_years:
            m = m + alpha * (target_margin - m)
        else:
            m = target_margin
        schedule.append(m)
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Sector median helpers  (Part 38)
# ─────────────────────────────────────────────────────────────────────────────

# Approximate 5-year sector median revenue growth rates (source: Damodaran 2024)
_SECTOR_MEDIAN_GROWTH: dict[str, float] = {
    "Information Technology":     0.110,
    "Health Care":                0.080,
    "Consumer Discretionary":     0.070,
    "Consumer Staples":           0.045,
    "Industrials":                0.055,
    "Financials":                 0.060,
    "Energy":                     0.040,
    "Materials":                  0.040,
    "Utilities":                  0.035,
    "Real Estate":                0.045,
    "Communication Services":     0.075,
    "default":                    0.060,
}

_SECTOR_MEDIAN_EBIT_MARGIN: dict[str, float] = {
    "Information Technology":     0.200,
    "Health Care":                0.150,
    "Consumer Discretionary":     0.080,
    "Consumer Staples":           0.110,
    "Industrials":                0.100,
    "Financials":                 0.250,   # typically net income margin for banks
    "Energy":                     0.120,
    "Materials":                  0.115,
    "Utilities":                  0.175,
    "Real Estate":                0.300,   # typically NOI margin
    "Communication Services":     0.150,
    "default":                    0.120,
}


def sector_median_growth(sector: str) -> float:
    """Return approximate sector median revenue growth rate."""
    return _SECTOR_MEDIAN_GROWTH.get(sector, _SECTOR_MEDIAN_GROWTH["default"])


def sector_median_ebit_margin(sector: str) -> float:
    """Return approximate sector median EBIT margin (fade target)."""
    return _SECTOR_MEDIAN_EBIT_MARGIN.get(sector, _SECTOR_MEDIAN_EBIT_MARGIN["default"])


# ─────────────────────────────────────────────────────────────────────────────
# Master assumption builder  (Part 35, 38, 42)
# ─────────────────────────────────────────────────────────────────────────────

def build_growth_assumptions(
    income_stmts: list[dict],
    ntm_estimates: dict | None,
    sector: str,
    terminal_growth: float,
    forecast_years: int = 10,
    hold_years: int = 3,
    fade_years: int = 7,
    historical_cagr_years: int = 5,
    *,
    industry: str | None = None,
    market_implied_g: float | None = None,
    structural_break_score: float = 0.0,
    rf_rate: float | None = None,
    analyst_count: int = 0,
    sector_analyst_accuracy: float | None = None,
) -> dict[str, Any]:
    """
    Produce a complete set of growth and margin assumptions.

    Returns a dict with:
      near_term_growth, terminal_growth, growth_schedule,
      base_ebit_margin, target_ebit_margin, margin_schedule,
      revenue_regime, terminal_g_range, structural_decline_flag,
      structural_decline_signals,
      sources (dict of individual anchors)

    Reference: Parts 35, 38, 42.  Layer C/D of DCF Accuracy Improvement Plan.
    """
    # 1. Historical CAGRs (multi-window for trajectory detection)
    from auto_valuation.model.income_statement import (
        historical_revenue_cagr, historical_ebit_margin
    )
    hist_cagr  = historical_revenue_cagr(income_stmts, years=historical_cagr_years)
    hist_cagr_3yr = historical_revenue_cagr(income_stmts, years=3) if len(income_stmts) >= 3 else hist_cagr
    hist_cagr_10yr = historical_revenue_cagr(income_stmts, years=10) if len(income_stmts) >= 10 else None
    base_margin = historical_ebit_margin(income_stmts, use_normalized=True, years=3)

    # 2. NTM consensus
    ntm_growth = extract_ntm_growth(ntm_estimates, income_stmts)

    # 3. Sector anchors
    sec_growth  = sector_median_growth(sector)
    sec_margin  = sector_median_ebit_margin(sector)

    # 4. Blend near-term growth
    near_term = blend_growth_estimate(
        hist_cagr, ntm_growth, sec_growth,
        analyst_count=analyst_count,
        sector_analyst_accuracy=sector_analyst_accuracy,
    )

    # 5. Layer C — Trajectory-constrained terminal g prior range
    revenue_regime = classify_revenue_regime(
        hist_cagr_3yr, hist_cagr, hist_cagr_10yr, ntm_growth, market_implied_g
    )
    tg_range = terminal_g_prior_range(revenue_regime, rf_rate=rf_rate, sector=sector)

    # Clamp the provided terminal_growth to the trajectory-consistent range
    terminal_growth_constrained = max(tg_range[0], min(terminal_growth, tg_range[1]))

    # 6. Layer D — Structural decline detection
    headwind_score = get_industry_headwind_score(industry)
    is_structural_decline, decline_signals = compute_structural_decline_flag(
        cagr_3yr=hist_cagr_3yr,
        cagr_10yr=hist_cagr_10yr,
        market_implied_g=market_implied_g,
        structural_break_score=structural_break_score,
        industry_headwind_score=headwind_score,
    )

    # 7. Schedules (use constrained terminal growth)
    growth_schedule = build_growth_fade_schedule(
        near_term, terminal_growth_constrained, forecast_years, hold_years, fade_years
    )
    margin_schedule = build_margin_fade_schedule(
        base_margin, sec_margin, forecast_years, fade_years, sector=sector
    )

    return {
        "near_term_growth":         near_term,
        "terminal_growth":          terminal_growth_constrained,
        "terminal_g_range":         tg_range,
        "revenue_regime":           revenue_regime,
        "structural_decline_flag":  is_structural_decline,
        "structural_decline_signals": decline_signals,
        "growth_schedule":          growth_schedule,
        "base_ebit_margin":         base_margin,
        "target_ebit_margin":       sec_margin,
        "margin_schedule":          margin_schedule,
        "sources": {
            "historical_cagr":       hist_cagr,
            "historical_cagr_3yr":   hist_cagr_3yr,
            "historical_cagr_10yr":  hist_cagr_10yr,
            "ntm_consensus_growth":  ntm_growth,
            "sector_median_growth":  sec_growth,
            "sector_median_margin":  sec_margin,
            "headwind_score":        headwind_score,
        },
    }
