"""
model/reit.py — REIT-specific valuation metrics: FFO, AFFO, CAP rate.

REITs are detected upstream (model/sector.py detect_reit()), and the DCF
pipeline swaps to FFO/AFFO multiples when a REIT is identified.

Reference: Architecture Plan Part 28 (REIT gating), Part 41 (FFO/AFFO).

All monetary values in USD millions.
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# FFO — Funds From Operations  (NAREIT standard)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ffo(
    net_income: float,
    depreciation_amortization: float,
    gains_on_sale_of_property: float = 0.0,
    impairments: float = 0.0,
) -> float:
    """
    FFO = Net Income + D&A − Gains on Sale of Property + Impairments.

    Signs:
      - net_income: positive = profit
      - depreciation_amortization: positive value to add back
      - gains_on_sale_of_property: positive = gain (subtract)
      - impairments: positive = charge (add back)

    Reference: Architecture Plan Part 41.1; NAREIT FFO White Paper.
    """
    return net_income + depreciation_amortization - gains_on_sale_of_property + impairments


# ─────────────────────────────────────────────────────────────────────────────
# AFFO — Adjusted Funds From Operations
# ─────────────────────────────────────────────────────────────────────────────

def compute_affo(
    ffo: float,
    maintenance_capex: float,
    straight_line_rent_adj: float = 0.0,
    recurring_non_cash_items: float = 0.0,
    leasing_commissions: float = 0.0,
    tenant_improvements: float = 0.0,
) -> float:
    """
    AFFO = FFO
           − Maintenance CapEx
           − Straight-Line Rent Adjustment
           − Non-cash compensation / other non-cash recurring
           − Leasing Commissions
           − Tenant Improvements

    All adjustment values should be positive (representing deductions from FFO).
    Reference: Architecture Plan Part 41.2.
    """
    return (
        ffo
        - maintenance_capex
        - straight_line_rent_adj
        - recurring_non_cash_items
        - leasing_commissions
        - tenant_improvements
    )


# ─────────────────────────────────────────────────────────────────────────────
# NAV — Net Asset Value approximation  (Part 41.3)
# ─────────────────────────────────────────────────────────────────────────────

def compute_reit_nav(
    noi_stabilised: float,
    cap_rate: float,
    cash_and_equivalents: float,
    other_assets: float,
    total_debt: float,
    preferred_equity: float = 0.0,
) -> dict[str, float]:
    """
    Simplified NAV approach for REITs.

    NAV = (NOI_stabilised / Cap_Rate) + Cash + Other_Assets
          − Total_Debt − Preferred_Equity

    Args:
        noi_stabilised:   Stabilised Net Operating Income (revenue − operating expenses).
        cap_rate:         Sector capitalisation rate (decimal, e.g. 0.055 for 5.5%).
        cash_and_equivalents: Liquid assets.
        other_assets:     Non-income-producing assets (land, dev pipeline, etc.).
        total_debt:       All IBD obligations.
        preferred_equity: Liquidation value of preferred stock.

    Returns:
        Dict with gross_asset_value, nav_equity, cap_rate.
    Reference: Architecture Plan Part 41.3.
    """
    if cap_rate <= 0:
        raise ValueError(f"cap_rate must be > 0, got {cap_rate}.")

    gross_asset_value = noi_stabilised / cap_rate
    nav_equity = (
        gross_asset_value
        + cash_and_equivalents
        + other_assets
        - total_debt
        - preferred_equity
    )
    return {
        "gross_asset_value_mm": gross_asset_value,
        "nav_equity_mm":        nav_equity,
        "cap_rate":             cap_rate,
    }


# ─────────────────────────────────────────────────────────────────────────────
# P/FFO and P/AFFO valuation multiples  (Part 41.4)
# ─────────────────────────────────────────────────────────────────────────────

def compute_reit_multiples(
    share_price: float,
    ffo_per_share: float,
    affo_per_share: float,
) -> dict[str, float | None]:
    """
    Compute P/FFO and P/AFFO trading multiples.

    Reference: Architecture Plan Part 41.4.
    """
    def _ratio(price: float, denom: float) -> float | None:
        return price / denom if denom and denom > 0 else None

    return {
        "p_ffo":  _ratio(share_price, ffo_per_share),
        "p_affo": _ratio(share_price, affo_per_share),
    }


def compute_implied_price_from_ffo_multiple(
    target_multiple: float,
    ffo_per_share: float,
    affo_per_share: float | None = None,
) -> dict[str, float]:
    """
    Implied share price from a target P/FFO (or P/AFFO) multiple.

    Reference: Architecture Plan Part 41.4.
    """
    result = {"implied_price_pffo": target_multiple * ffo_per_share}
    if affo_per_share is not None:
        result["implied_price_paffo"] = target_multiple * affo_per_share
    return result
