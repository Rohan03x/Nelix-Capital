"""
model/dilution.py — Diluted share count via Treasury Stock Method (TSM).

Reference: Architecture Plan Parts 8, 26, 36, 53, 54.

All share counts in millions of shares.
All monetary values in USD millions (share prices in USD).
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# Treasury Stock Method  (Part 36)
# ─────────────────────────────────────────────────────────────────────────────

def treasury_stock_method(
    basic_shares_mm: float,
    options_outstanding_mm: float,
    options_avg_strike: float,
    current_price: float,
) -> float:
    """
    Apply TSM for in-the-money options:

        Net new shares = Options × (1 − Strike / Price)
        (only when Price > Strike; out-of-the-money options are excluded)

    Returns diluted share count in millions.
    Reference: Part 36.
    """
    if current_price <= 0 or options_outstanding_mm <= 0:
        return basic_shares_mm

    if options_avg_strike >= current_price:
        # Out-of-the-money — no dilution
        return basic_shares_mm

    net_new = options_outstanding_mm * (1.0 - options_avg_strike / current_price)
    return basic_shares_mm + max(0.0, net_new)


def treasury_stock_method_warrants(
    basic_shares_mm: float,
    warrants_outstanding_mm: float,
    warrant_avg_strike: float,
    current_price: float,
) -> float:
    """
    TSM for warrants — identical formula to stock options.
    Reference: Part 36.
    """
    return treasury_stock_method(
        basic_shares_mm,
        warrants_outstanding_mm,
        warrant_avg_strike,
        current_price,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Restricted Stock Units (RSUs)  (Part 53)
# ─────────────────────────────────────────────────────────────────────────────

def add_rsu_dilution(
    shares_mm: float,
    unvested_rsus_mm: float,
    assumed_tax_withhold_pct: float = 0.40,
) -> float:
    """
    Add dilution from unvested RSUs.
    RSUs vest and are settled in shares (net of tax withholding).

    Net new shares = Unvested RSUs × (1 − tax_withhold_pct)
    Reference: Part 53.
    """
    net_rsus = unvested_rsus_mm * (1.0 - assumed_tax_withhold_pct)
    return shares_mm + max(0.0, net_rsus)


# ─────────────────────────────────────────────────────────────────────────────
# Convertible notes  (Part 54)
# ─────────────────────────────────────────────────────────────────────────────

def convertible_dilution(
    shares_mm: float,
    convertible_face_mm: float,
    conversion_price: float,
    current_price: float,
    method: str = "if_converted",   # "if_converted" | "tsm"
) -> float:
    """
    Dilution from convertible notes.

    if_converted: add all potential shares (Face / conversion_price)
    tsm: same as TSM — net of treasury stock buyback equivalent

    Only dilutive if current_price > conversion_price.
    Reference: Part 54.
    """
    if convertible_face_mm <= 0 or conversion_price <= 0:
        return shares_mm

    potential_shares = convertible_face_mm / conversion_price

    if current_price <= conversion_price:
        # Out-of-the-money — not dilutive
        return shares_mm

    if method == "tsm":
        # TSM: net new = potential_shares × (1 - conversion_price / current_price)
        net_new = potential_shares * (1.0 - conversion_price / current_price)
        return shares_mm + max(0.0, net_new)
    else:
        # if-converted: add all potential shares
        return shares_mm + potential_shares


# ─────────────────────────────────────────────────────────────────────────────
# Fully diluted share count  (Part 8, 36)
# ─────────────────────────────────────────────────────────────────────────────

def compute_fully_diluted_shares(
    basic_shares_mm: float,
    current_price: float,
    options_outstanding_mm: float = 0.0,
    options_avg_strike:     float = 0.0,
    warrants_outstanding_mm: float = 0.0,
    warrants_avg_strike:    float = 0.0,
    unvested_rsus_mm:       float = 0.0,
    rsu_tax_withhold_pct:   float = 0.40,
    convertible_face_mm:    float = 0.0,
    convertible_price:      float = 0.0,
    convertible_method:     str   = "if_converted",
) -> dict[str, float]:
    """
    Full diluted share count incorporating all dilutive securities.

    Returns a dict with:
      basic_shares, after_options, after_warrants, after_rsus,
      after_convertibles (= fully diluted), total_dilution_mm
    """
    s = basic_shares_mm

    # Step 1: Options
    after_options = treasury_stock_method(s, options_outstanding_mm, options_avg_strike, current_price)

    # Step 2: Warrants
    after_warrants = treasury_stock_method_warrants(
        after_options, warrants_outstanding_mm, warrants_avg_strike, current_price
    )

    # Step 3: RSUs
    after_rsus = add_rsu_dilution(after_warrants, unvested_rsus_mm, rsu_tax_withhold_pct)

    # Step 4: Convertibles
    after_convertibles = convertible_dilution(
        after_rsus, convertible_face_mm, convertible_price, current_price, convertible_method
    )

    return {
        "basic_shares":       basic_shares_mm,
        "after_options":      after_options,
        "after_warrants":     after_warrants,
        "after_rsus":         after_rsus,
        "after_convertibles": after_convertibles,
        "fully_diluted_mm":   after_convertibles,
        "total_dilution_mm":  after_convertibles - basic_shares_mm,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Intrinsic value per share  (Part 8)
# ─────────────────────────────────────────────────────────────────────────────

def compute_price_per_share(
    equity_value_mm: float,
    fully_diluted_shares_mm: float,
) -> float:
    """
    Intrinsic value per share = Equity Value (USD millions) / Shares (millions).
    Returns 0.0 if shares ≤ 0.
    """
    return safe_divide(equity_value_mm, fully_diluted_shares_mm, 0.0)
