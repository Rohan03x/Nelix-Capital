"""
model/ddm.py — Dividend Discount Model (DDM) valuation.

Reference: Architecture Plan Part 33.2 (equity-holder DCF), CFI DCF training guide.

The DDM values equity directly by discounting expected dividends per share (DPS)
back to the present using the cost of equity (ke).  Three standard variants:

  1. Gordon Growth Model (GGM) — single perpetuity stage:
       P = DPS_1 / (ke − g)

  2. Two-stage DDM — high-growth stage + stable perpetuity:
       P = Σ DPS_t / (1 + ke)^t  [t = 1..n]  +  TV / (1 + ke)^n
       TV = DPS_n × (1 + g_stable) / (ke_stable − g_stable)

  3. H-model — linear fade from high growth to stable growth:
       P = DPS_0 × (1 + g_stable) / (ke − g_stable)
         + DPS_0 × H × (g_high − g_stable) / (ke − g_stable)
       where H = half-life of the high-growth period.

All prices / DPS in USD.  Rates in decimal (0.10 = 10%).
"""

from __future__ import annotations

import math
from auto_valuation.utils.error import safe_divide, ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Gordon Growth Model  (single-stage)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ddm_gordon(
    dps_next: float,
    cost_of_equity: float,
    terminal_growth: float,
) -> float:
    """
    Gordon Growth Model (GGM) — single-stage DDM:
        P = DPS_1 / (ke − g)

    DPS_1 must be the NEXT period's dividend (already grown by g if using DPS_0).
    Raises ValueError if ke ≤ g (model undefined).

    Args:
        dps_next       : Expected dividend per share in the next period ($).
        cost_of_equity : Required return on equity (ke), e.g. 0.09 for 9%.
        terminal_growth: Stable dividend growth rate (g), e.g. 0.03 for 3%.

    Returns:
        float — intrinsic value per share ($).

    Reference: CFI DCF Training Guide; Architecture Plan Part 33.2.
    """
    spread = cost_of_equity - terminal_growth
    if spread <= 0:
        raise ValueError(
            f"Cost of equity ({cost_of_equity:.2%}) must exceed terminal growth "
            f"({terminal_growth:.2%}) in GGM DDM."
        )
    if dps_next <= 0:
        import warnings as _w
        _w.warn("dps_next ≤ 0 — DDM price will be zero.", ValuationWarning, stacklevel=2)
        return 0.0
    return dps_next / spread


# ─────────────────────────────────────────────────────────────────────────────
# Two-stage DDM
# ─────────────────────────────────────────────────────────────────────────────

def compute_ddm_two_stage(
    dps_0: float,
    near_growth: float,
    stable_growth: float,
    ke_near: float,
    ke_stable: float,
    near_years: int = 5,
) -> float:
    """
    Two-stage Dividend Discount Model.

    Stage 1: Dividends grow at near_growth for near_years years.
    Stage 2: Dividends grow at stable_growth in perpetuity (GGM terminal value).

    The terminal value is discounted back through Stage 1.

    Args:
        dps_0        : Current (last paid) dividend per share ($).
        near_growth  : Stage 1 dividend growth rate per year (decimal).
        stable_growth: Stage 2 (terminal) growth rate (decimal).
        ke_near      : Cost of equity for Stage 1 discounting.
        ke_stable    : Cost of equity for Stage 2 (terminal) — often same as ke_near.
        near_years   : Number of years in Stage 1 (default 5).

    Returns:
        float — intrinsic value per share ($).

    Reference: CFI DCF Training Guide; Architecture Plan Part 33.2.
    """
    if ke_stable <= stable_growth:
        raise ValueError(
            f"ke_stable ({ke_stable:.2%}) must exceed stable_growth ({stable_growth:.2%})."
        )
    if near_years < 1:
        raise ValueError("near_years must be ≥ 1.")

    pv_dividends = 0.0
    dps_t = dps_0

    for t in range(1, near_years + 1):
        dps_t = dps_t * (1.0 + near_growth)
        pv_dividends += dps_t / (1.0 + ke_near) ** t

    # Terminal value at end of Stage 1
    dps_n1 = dps_t * (1.0 + stable_growth)  # first Stage 2 dividend
    if ke_stable <= stable_growth:
        raise ValueError("ke_stable ≤ stable_growth in terminal stage.")
    tv = dps_n1 / (ke_stable - stable_growth)
    pv_tv = tv / (1.0 + ke_near) ** near_years

    return pv_dividends + pv_tv


# ─────────────────────────────────────────────────────────────────────────────
# H-model DDM  (linear growth fade)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ddm_h_model(
    dps_0: float,
    high_growth: float,
    stable_growth: float,
    cost_of_equity: float,
    half_life: float,
) -> float:
    """
    H-model DDM (Fuller & Hsia, 1984).

    Assumes growth fades linearly from high_growth to stable_growth over 2×H years.
    Provides a closed-form approximation that avoids explicit year-by-year projection.

        P = DPS_0 × (1 + g_stable) / (ke − g_stable)
          + DPS_0 × H × (g_high − g_stable) / (ke − g_stable)

    Args:
        dps_0           : Current dividend per share ($).
        high_growth     : Initial high growth rate (decimal).
        stable_growth   : Terminal stable growth rate (decimal).
        cost_of_equity  : Required return on equity (ke).
        half_life       : H = half of the high-growth period in years (e.g. 5 for 10yr fade).

    Returns:
        float — intrinsic value per share ($).

    Reference: Fuller & Hsia (1984); commonly used in IB equity research.
    """
    spread = cost_of_equity - stable_growth
    if spread <= 0:
        raise ValueError(
            f"cost_of_equity ({cost_of_equity:.2%}) must exceed stable_growth ({stable_growth:.2%})."
        )
    stable_value = dps_0 * (1.0 + stable_growth) / spread
    high_growth_premium = dps_0 * half_life * (high_growth - stable_growth) / spread
    return stable_value + high_growth_premium


# ─────────────────────────────────────────────────────────────────────────────
# Implied cost of equity from current price  (reverse-solve)
# ─────────────────────────────────────────────────────────────────────────────

def implied_ke_from_price(
    current_price: float,
    dps_next: float,
    terminal_growth: float,
) -> float:
    """
    Back-solve the implied cost of equity given a stock's current price.

    From GGM: P = DPS_1 / (ke − g)
    Therefore:      ke = DPS_1 / P + g

    Args:
        current_price  : Current market price per share ($).
        dps_next       : Expected next-period DPS ($).
        terminal_growth: Perpetuity growth rate (g).

    Returns:
        float — implied cost of equity (e.g. 0.09 = 9%).

    Reference: CFA curriculum; Architecture Plan Part 33.2.
    """
    if current_price <= 0:
        raise ValueError("current_price must be positive.")
    return safe_divide(dps_next, current_price, 0.0) + terminal_growth


# ─────────────────────────────────────────────────────────────────────────────
# Payout ratio helpers
# ─────────────────────────────────────────────────────────────────────────────

def compute_payout_ratio(dps: float, eps: float) -> float:
    """
    Dividend payout ratio = DPS / EPS.

    Returns 0.0 if EPS ≤ 0.

    Reference: CFI DCF Training Guide.
    """
    if eps <= 0:
        return 0.0
    return safe_divide(dps, eps, 0.0)


def compute_sustainable_growth_rate(roe: float, payout_ratio: float) -> float:
    """
    Sustainable (Gordon) growth rate = ROE × (1 − payout_ratio) = ROE × retention_ratio.

    The sustainable growth rate is the rate at which a company can grow
    using only internally generated funds (no external equity issuance).

    Args:
        roe          : Return on equity (decimal).
        payout_ratio : Dividend payout ratio (decimal, 0–1).

    Returns:
        float — sustainable growth rate.

    Reference: CFI; Damodaran "Investment Valuation" chapter on DDM.
    """
    retention_ratio = max(0.0, 1.0 - payout_ratio)
    return roe * retention_ratio
