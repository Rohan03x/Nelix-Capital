"""
model/ev_bridge.py — Canonical EV → Equity value per share bridge.

Reference: Architecture Plan Parts 3.5, 16, 45, 59.1, 60 (net debt), 78.3.

Walk:
  Enterprise Value
  − Interest-bearing debt (IBD)
  − Preferred equity
  − Non-controlling interests (NCI)
  + Cash and equivalents  (only unrestricted)
  + Short-term investments  (liquid)
  ± Equity investments   (add if not in NOPAT, deduct otherwise)
  − Unfunded pension obligations (net of deferred tax asset)
  − Finance lease liabilities (if not in IBD and leases not capitalised in WACC)
  = Equity value  (100% of consolidated equity)
  ÷ Diluted shares outstanding
  = Equity value per share

All monetary values in USD millions.
Shares in millions.  Price in USD per share.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EVBridgeInputs:
    """All inputs required for the EV → equity-per-share bridge."""
    enterprise_value_mm: float = 0.0

    # Debt deductions
    short_term_debt_mm:      float = 0.0
    long_term_debt_mm:       float = 0.0
    finance_leases_mm:       float = 0.0   # IFRS 16 / ASC 842 right-of-use liabilities
    preferred_equity_mm:     float = 0.0
    nci_mm:                  float = 0.0   # minority interest at market / book

    # Cash additions
    cash_mm:                 float = 0.0
    restricted_cash_mm:      float = 0.0   # excluded from bridge (not freely available)
    short_term_investments_mm: float = 0.0

    # Equity investments
    equity_investments_mm:   float = 0.0   # affiliates / minority stakes
    equity_investments_in_nopat: bool = False  # True → already in UFCF, deduct from EV

    # Pension
    pension_underfunded_mm:  float = 0.0   # gross underfunding
    pension_deferred_tax_mm: float = 0.0   # related DTA (reduces net pension liability)

    # Convertibles — ITM convertibles treated as equity
    convertible_debt_mm:     float = 0.0   # if ITM, remove from debt, add shares
    convertible_itm:         bool = False

    # Shares
    diluted_shares_mm:       float = 0.0


@dataclass
class EVBridgeResult:
    """Output of the EV → equity-per-share bridge."""
    enterprise_value_mm:     float = 0.0
    total_debt_mm:           float = 0.0
    preferred_equity_mm:     float = 0.0
    nci_mm:                  float = 0.0
    net_cash_mm:             float = 0.0
    net_pension_liability_mm: float = 0.0
    equity_investments_adj_mm: float = 0.0
    net_debt_mm:             float = 0.0    # = debt + preferred + nci − cash − ST inv
    equity_value_mm:         float = 0.0
    diluted_shares_mm:       float = 0.0
    equity_value_per_share:  float = 0.0
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Core bridge functions
# ─────────────────────────────────────────────────────────────────────────────

def should_add_equity_investments(
    equity_investments_mm: float,
    in_nopat: bool,
) -> float:
    """
    Determine equity-investment adjustment to the EV bridge.

    If equity investments are INCLUDED in NOPAT (e.g. equity method income),
    their value is already embedded in the DCF EV → ADD them back to equity.

    If equity investments are NOT in NOPAT (unlisted stakes, excluded from
    the operating model), their value is MISSING from the DCF EV → ADD them.

    In practice: equity investments are almost always ADDED to the bridge
    unless the analyst explicitly modelled them in the operating FCF.

    Returns: float — the value to ADD to equity (always positive when relevant).
    Reference: Architecture Plan Part 45.
    """
    if equity_investments_mm <= 0:
        return 0.0
    # Standard treatment: add equity investments to equity value
    # (they are typically not modelled in the core operating DCF)
    return equity_investments_mm


def handle_convertible_notes(
    enterprise_value_mm: float,
    convertible_debt_mm: float,
    conversion_price: float,
    current_price: float,
    basic_shares_mm: float,
    conversion_ratio: float | None = None,
) -> tuple[float, float, float]:
    """
    Handle convertible notes in the EV bridge.

    If ITM (current_price > conversion_price):
      - Remove convertible principal from net debt deduction
      - Add implied conversion shares to diluted count
      - This prevents double-counting (both as debt and equity dilution)

    Returns: (adjusted_convertible_debt_mm, net_dilution_shares_mm, itm)

    Where:
      adjusted_convertible_debt_mm = 0.0 if ITM (treated as equity), else convertible_debt_mm
      net_dilution_shares_mm = implied new shares if ITM
    Reference: Architecture Plan Part 78.3.
    """
    itm = current_price > conversion_price > 0 if conversion_price else False

    if not itm:
        return convertible_debt_mm, 0.0, False

    # Compute implied conversion shares
    if conversion_ratio is not None:
        # Direct: par_value / conversion_price × conversion_ratio
        implied_shares_mm = (convertible_debt_mm * 1e6 / 1000) / conversion_price * conversion_ratio / 1e6
    else:
        # Approximate: par / conversion_price (assumes $1,000 face per bond)
        implied_shares_mm = convertible_debt_mm / conversion_price * 1000.0 / 1e6

    logger.info(
        f"Convertible notes are ITM (current ${current_price:.2f} > conversion ${conversion_price:.2f}). "
        f"Treating as equity: ${convertible_debt_mm:.0f}m debt → {implied_shares_mm:.2f}m dilutive shares."
    )
    return 0.0, implied_shares_mm, True


def compute_equity_value_per_share(inputs: EVBridgeInputs) -> EVBridgeResult:
    """
    Full EV → equity value per share bridge.

    Walk:
      EV
      − Short-term debt
      − Long-term debt
      − Finance leases (if add_leases=True)
      − Preferred equity
      − NCI
      + Cash (unrestricted only)
      + Short-term investments
      ± Equity investments (see should_add_equity_investments)
      − Net pension liability (pension_underfunded − pension DTA)
      = Equity value
      ÷ Diluted shares
      = Equity per share

    Reference: Architecture Plan Parts 3.5, 16, 45, 60, 78.3.
    """
    result = EVBridgeResult()
    result.enterprise_value_mm = inputs.enterprise_value_mm
    warnings = []

    # Total debt (IBD components)
    total_debt = (
        inputs.short_term_debt_mm
        + inputs.long_term_debt_mm
        + inputs.finance_leases_mm
        # Convertibles already handled: if ITM they're set to 0 in inputs
        + (0.0 if inputs.convertible_itm else inputs.convertible_debt_mm)
    )
    result.total_debt_mm = total_debt
    result.preferred_equity_mm = inputs.preferred_equity_mm
    result.nci_mm = inputs.nci_mm

    # Net cash (unrestricted cash + liquid investments)
    usable_cash = inputs.cash_mm - inputs.restricted_cash_mm
    if usable_cash < 0:
        warnings.append(
            f"Restricted cash (${inputs.restricted_cash_mm:.0f}m) > "
            f"total cash (${inputs.cash_mm:.0f}m) — capped at zero."
        )
        usable_cash = 0.0
    net_cash = usable_cash + inputs.short_term_investments_mm
    result.net_cash_mm = net_cash

    # Net pension liability
    net_pension = max(0.0, inputs.pension_underfunded_mm - inputs.pension_deferred_tax_mm)
    result.net_pension_liability_mm = net_pension
    if net_pension > 0:
        warnings.append(
            f"Pension underfunding (net of DTA): ${net_pension:.0f}m deducted from equity."
        )

    # Equity investments adjustment
    equity_inv_adj = should_add_equity_investments(
        inputs.equity_investments_mm,
        inputs.equity_investments_in_nopat,
    )
    result.equity_investments_adj_mm = equity_inv_adj

    # Net debt
    net_debt = total_debt + inputs.preferred_equity_mm + inputs.nci_mm - net_cash
    result.net_debt_mm = net_debt

    # Equity value
    equity_value = (
        inputs.enterprise_value_mm
        - net_debt
        - net_pension
        + equity_inv_adj
    )

    if equity_value < 0:
        warnings.append(
            f"Equity value is negative (${equity_value:.0f}m). "
            f"Debt (${net_debt:.0f}m) + pension (${net_pension:.0f}m) exceeds EV. "
            f"Company may be technically insolvent. Price capped at $0."
        )

    result.equity_value_mm = equity_value
    result.diluted_shares_mm = inputs.diluted_shares_mm
    result.warnings = warnings

    # Per share
    if inputs.diluted_shares_mm > 0:
        result.equity_value_per_share = max(0.0, equity_value / inputs.diluted_shares_mm)
    else:
        result.equity_value_per_share = 0.0
        warnings.append("Diluted shares = 0 — cannot compute per-share value.")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Market-implied WACC (reverse solve)
# ─────────────────────────────────────────────────────────────────────────────

def compute_market_implied_wacc(
    current_price: float,
    diluted_shares_mm: float,
    net_debt_mm: float,
    fcf_year1: float,
    terminal_growth: float,
    forecast_ufcf: list[float],    # years 1..N-1 (before terminal year)
    wacc_lo: float = 0.04,
    wacc_hi: float = 0.25,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float:
    """
    Find the WACC implied by the current market price using pure-Python bisection.

    Solve for WACC such that:
        PV(forecast UFCF) + PV(terminal value) − net_debt = equity_value_market

    where equity_value_market = current_price × diluted_shares_mm.

    Returns the implied WACC (as a decimal, e.g. 0.095 = 9.5%).
    Reference: Architecture Plan Part 59.2.
    """
    if current_price <= 0 or diluted_shares_mm <= 0:
        return float("nan")

    equity_value_market = current_price * diluted_shares_mm
    target_ev = equity_value_market + net_debt_mm

    def _ev_at_wacc(w: float) -> float:
        if w <= terminal_growth:
            return float("inf")
        n = len(forecast_ufcf)
        pv = 0.0
        for t, fcf in enumerate(forecast_ufcf, start=1):
            pv += fcf / (1.0 + w) ** t
        # Terminal value at end of year N
        tv = fcf_year1 / (w - terminal_growth)
        pv += tv / (1.0 + w) ** n
        return pv

    # Bisection
    lo, hi = wacc_lo, wacc_hi
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        ev_mid = _ev_at_wacc(mid)
        if abs(ev_mid - target_ev) < tol * target_ev:
            return mid
        if ev_mid > target_ev:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# FCFE direct equity value  (Part 63.2)
# ─────────────────────────────────────────────────────────────────────────────

def fcfe_equity_value(
    fcfe_series: list[float],
    cost_of_equity: float,
    diluted_shares: float,
    terminal_growth: float = 0.025,
) -> dict:
    """
    Compute equity value per share directly from a FCFE series using the
    Gordon-growth perpetuity for terminal value.

    fcfe_series     — list of projected FCFE ($M) for each forecast year;
                      the last entry seeds the terminal value.
    cost_of_equity  — levered cost of equity (decimal, e.g. 0.10).
    diluted_shares  — diluted shares outstanding (millions).
    terminal_growth — perpetuity growth rate applied to the last FCFE entry.

    Returns a dict with:
      pv_fcfe_mm          — PV of forecast FCFEs ($M)
      pv_tv_mm            — PV of terminal value ($M)
      total_equity_value_mm
      equity_value_per_share

    Reference: Architecture Plan Part 63.2.
    """
    if cost_of_equity <= terminal_growth:
        raise ValueError(
            f"cost_of_equity ({cost_of_equity:.2%}) must exceed "
            f"terminal_growth ({terminal_growth:.2%})."
        )
    if diluted_shares <= 0:
        raise ValueError("diluted_shares must be positive.")
    if not fcfe_series:
        raise ValueError("fcfe_series must be non-empty.")

    ke = cost_of_equity
    # PV of forecast FCFEs
    pv_fcfe = sum(cf / (1.0 + ke) ** (t + 1) for t, cf in enumerate(fcfe_series))

    # Terminal value (Gordon growth on last FCFE)
    last_fcfe = fcfe_series[-1]
    tv = last_fcfe * (1.0 + terminal_growth) / (ke - terminal_growth)
    pv_tv = tv / (1.0 + ke) ** len(fcfe_series)

    total_equity = pv_fcfe + pv_tv
    per_share    = total_equity / diluted_shares

    return {
        "pv_fcfe_mm":             round(pv_fcfe, 4),
        "pv_tv_mm":               round(pv_tv, 4),
        "total_equity_value_mm":  round(total_equity, 4),
        "equity_value_per_share": round(per_share, 4),
    }
