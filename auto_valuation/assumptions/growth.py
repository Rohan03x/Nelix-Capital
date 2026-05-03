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


# ─────────────────────────────────────────────────────────────────────────────
# Growth anchor: blend sources  (Part 5, 38)
# ─────────────────────────────────────────────────────────────────────────────

def blend_growth_estimate(
    historical_cagr: float,
    ntm_consensus: float | None,
    sector_median_growth: float | None,
    weights: tuple[float, float, float] = (0.40, 0.40, 0.20),
) -> float:
    """
    Blend three growth anchors into a single near-term growth rate:
      - historical_cagr   (weight 0.40 default)
      - ntm_consensus     (weight 0.40 default — or 0 if not available)
      - sector_median     (weight 0.20 default — or redistributed if not available)

    Missing sources have their weight redistributed pro-rata to the others.
    Reference: Parts 5, 38.
    """
    sources = [
        (historical_cagr, weights[0]),
        (ntm_consensus,   weights[1]),
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

def build_margin_fade_schedule(
    base_margin: float,
    target_margin: float,
    forecast_years: int = 10,
    fade_years: int = 7,
) -> list[float]:
    """
    Linear fade from base_margin to target_margin over fade_years.
    After fade_years, margin is held at target_margin.

    Reference: Part 10.
    """
    schedule: list[float] = []
    for yr in range(1, forecast_years + 1):
        if yr <= fade_years:
            m = base_margin + (target_margin - base_margin) * (yr / fade_years)
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
) -> dict[str, Any]:
    """
    Produce a complete set of growth and margin assumptions.

    Returns a dict with:
      near_term_growth, terminal_growth, growth_schedule,
      base_ebit_margin, target_ebit_margin, margin_schedule,
      sources (dict of individual anchors)

    Reference: Parts 35, 38, 42.
    """
    # 1. Historical CAGR
    from auto_valuation.model.income_statement import (
        historical_revenue_cagr, historical_ebit_margin
    )
    hist_cagr  = historical_revenue_cagr(income_stmts, years=historical_cagr_years)
    base_margin = historical_ebit_margin(income_stmts, use_normalized=True, years=3)

    # 2. NTM consensus
    ntm_growth = extract_ntm_growth(ntm_estimates, income_stmts)

    # 3. Sector anchors
    sec_growth  = sector_median_growth(sector)
    sec_margin  = sector_median_ebit_margin(sector)

    # 4. Blend near-term growth
    near_term = blend_growth_estimate(hist_cagr, ntm_growth, sec_growth)

    # 5. Schedules
    growth_schedule = build_growth_fade_schedule(
        near_term, terminal_growth, forecast_years, hold_years, fade_years
    )
    margin_schedule = build_margin_fade_schedule(
        base_margin, sec_margin, forecast_years, fade_years
    )

    return {
        "near_term_growth":  near_term,
        "terminal_growth":   terminal_growth,
        "growth_schedule":   growth_schedule,
        "base_ebit_margin":  base_margin,
        "target_ebit_margin": sec_margin,
        "margin_schedule":   margin_schedule,
        "sources": {
            "historical_cagr":       hist_cagr,
            "ntm_consensus_growth":  ntm_growth,
            "sector_median_growth":  sec_growth,
            "sector_median_margin":  sec_margin,
        },
    }
