"""
model/shares.py — Diluted share count rollforward and warrant dilution.

Reference: Architecture Plan Parts 3.6, 44, 67.2.

Functions:
  diluted_shares_tsm()          — Treasury Stock Method for options/warrants
  compute_warrant_dilution()    — dilution from outstanding warrants
  rollforward_basic_shares()    — basic share count rollforward
  compute_diluted_shares()      — final diluted share count for valuation

All share counts in millions.  Prices / strikes in USD per share.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Treasury Stock Method (TSM)
# ─────────────────────────────────────────────────────────────────────────────

def diluted_shares_tsm(
    basic_shares_mm: float,
    options_outstanding_mm: float = 0.0,
    options_avg_strike: float = 0.0,
    current_price: float = 0.0,
    restricted_stock_units_mm: float = 0.0,
    performance_share_units_mm: float = 0.0,
    psu_vesting_probability: float = 1.0,
) -> float:
    """
    Compute diluted share count using the Treasury Stock Method (TSM).

    TSM formula for options:
        Net shares added = options × (1 − strike / market_price)
        Only in-the-money options add dilution (strike < price).

    RSUs and PSUs: assumed to vest at face value (no strike price).
    PSUs are multiplied by vesting probability (0-2.0x target for performance).

    Args:
        basic_shares_mm           : basic shares outstanding (millions).
        options_outstanding_mm    : total stock options outstanding (millions).
        options_avg_strike        : weighted-average exercise price ($).
        current_price             : current stock price ($).
        restricted_stock_units_mm : unvested RSUs (millions).
        performance_share_units_mm: PSUs at target level (millions).
        psu_vesting_probability   : expected PSU payout as fraction of target.

    Returns:
        float — diluted shares in millions.

    Reference: Architecture Plan Parts 3.6, 44.
    """
    diluted = basic_shares_mm

    # Options: in-the-money only
    if options_outstanding_mm > 0 and options_avg_strike > 0 and current_price > 0:
        if current_price > options_avg_strike:
            # Net dilutive shares = options × (price − strike) / price
            net_options = options_outstanding_mm * (current_price - options_avg_strike) / current_price
            diluted += max(0.0, net_options)
        # Out-of-the-money options add zero dilution under TSM

    # RSUs: vest at face value (no strike)
    diluted += restricted_stock_units_mm

    # PSUs: vest at target × probability
    diluted += performance_share_units_mm * psu_vesting_probability

    return diluted


def compute_warrant_dilution(
    basic_shares_mm: float,
    warrants_outstanding_mm: float,
    warrant_strike: float,
    current_price: float,
) -> float:
    """
    Compute dilution from outstanding warrants using TSM.

    Warrants are long-dated options (typically 5-7 year term) issued as part of
    debt financings, SPACs, or equity offerings.

    Unlike employee options, warrants often have fixed strike prices and no
    forfeiture.  In-the-money warrants add dilution; out-of-the-money do not.

    Returns:
        float — diluted shares after warrant dilution (millions).

    Reference: Architecture Plan Part 44.
    """
    if warrants_outstanding_mm <= 0 or current_price <= 0:
        return basic_shares_mm

    if current_price > warrant_strike > 0:
        net_warrants = warrants_outstanding_mm * (current_price - warrant_strike) / current_price
        return basic_shares_mm + max(0.0, net_warrants)
    return basic_shares_mm


# ─────────────────────────────────────────────────────────────────────────────
# Basic shares rollforward
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_basic_shares(
    opening_shares_mm: float,
    new_issuances_mm: float = 0.0,    # shares issued (equity offerings, SBC exercise, etc.)
    buybacks_mm: float = 0.0,         # shares repurchased (in millions)
    net_sbc_vesting_mm: float = 0.0,  # net SBC shares vesting after tax withholding
) -> float:
    """
    Roll forward basic shares outstanding for one forecast year.

    closing = opening + issuances + net_sbc_vesting - buybacks

    Buybacks are in million shares (not dollars).  To convert from $M buybacks:
        shares_repurchased_mm = buybacks_mm_dollars / price_per_share

    Returns:
        float — closing basic shares (millions).

    Reference: Architecture Plan Part 3.6.
    """
    closing = opening_shares_mm + new_issuances_mm + net_sbc_vesting_mm - buybacks_mm
    return max(0.0, closing)


def rollforward_shares_forecast(
    opening_basic_mm: float,
    forecast_years: int,
    annual_net_issuance_mm: float = 0.0,   # net share count change per year
    options_mm: float = 0.0,
    options_strike: float = 0.0,
    rsus_mm: float = 0.0,
    psus_mm: float = 0.0,
    current_price: float = 0.0,
) -> list[dict]:
    """
    Build a multi-year diluted share count forecast.

    Assumes:
      - Basic shares change by annual_net_issuance_mm per year.
      - Diluted shares computed via TSM each year.
      - Options/RSU/PSU counts held constant (conservative; actual grants
        and forfeitures should come from overrides.json in a live model).

    Returns:
        list of dicts: {'year', 'basic_shares_mm', 'diluted_shares_mm'}
    """
    results = []
    basic = opening_basic_mm
    for yr in range(1, forecast_years + 1):
        basic = rollforward_basic_shares(basic, new_issuances_mm=annual_net_issuance_mm)
        diluted = diluted_shares_tsm(
            basic_shares_mm=basic,
            options_outstanding_mm=options_mm,
            options_avg_strike=options_strike,
            current_price=current_price,
            restricted_stock_units_mm=rsus_mm,
            performance_share_units_mm=psus_mm,
        )
        results.append({
            "year": yr,
            "basic_shares_mm": round(basic, 4),
            "diluted_shares_mm": round(diluted, 4),
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Final diluted shares for valuation
# ─────────────────────────────────────────────────────────────────────────────

def compute_diluted_shares(
    basic_shares_mm: float,
    options_outstanding_mm: float = 0.0,
    options_avg_strike: float = 0.0,
    warrants_outstanding_mm: float = 0.0,
    warrant_strike: float = 0.0,
    rsus_mm: float = 0.0,
    psus_mm: float = 0.0,
    convertible_shares_mm: float = 0.0,  # dilution from ITM convertibles
    current_price: float = 0.0,
) -> float:
    """
    Compute total diluted share count for the EV→equity-per-share bridge.

    Includes:
      - Basic shares
      - In-the-money options (via TSM)
      - RSUs and PSUs (at target)
      - Warrants (in-the-money via TSM)
      - ITM convertible note dilution (if convertibles in-the-money)

    Note: convertible notes IN-THE-MONEY should be treated as equity:
      - Add their implied shares to diluted count
      - Remove their principal from net debt in the EV bridge
    Reference: Architecture Plan Parts 3.6, 44, 78.3.
    """
    diluted = diluted_shares_tsm(
        basic_shares_mm=basic_shares_mm,
        options_outstanding_mm=options_outstanding_mm,
        options_avg_strike=options_avg_strike,
        current_price=current_price,
        restricted_stock_units_mm=rsus_mm,
        performance_share_units_mm=psus_mm,
    )
    # Warrants
    if warrants_outstanding_mm > 0:
        if current_price > warrant_strike > 0:
            net_warrants = warrants_outstanding_mm * (current_price - warrant_strike) / current_price
            diluted += max(0.0, net_warrants)

    # ITM convertibles
    diluted += convertible_shares_mm

    return diluted


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical alias (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → compute_diluted_shares (TSM method)
compute_diluted_shares_tsm = compute_diluted_shares
