"""
model/discounting.py — Exact-date discounting: XNPV and XIRR.

Standard Python NPV/IRR assumes equal integer periods.  XNPV/XIRR use
actual calendar dates so stub years, mid-year conventions, and terminal
value dates are handled precisely.

Reference: Architecture Plan Part 72 (CFI DCF Model Training Guide best practice).

All cash flows in USD millions.  Rates as decimals.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# XNPV — exact-date net present value  (Part 72)
# ─────────────────────────────────────────────────────────────────────────────

def compute_xnpv(
    rate: float,
    cashflows: Sequence[float],
    dates: Sequence[date],
) -> float:
    """
    XNPV: discount each cash flow by its exact calendar date.

    PV = Σ CF_i / (1 + rate)^((dates[i] - dates[0]).days / 365.25)

    Args:
        rate:      Annual discount rate (e.g. 0.09 for 9%).
        cashflows: Cash flow amounts (+ve = inflow, -ve = outflow).
        dates:     One date per cash flow.  dates[0] is the valuation date.

    Returns:
        Present value as of dates[0].

    Reference: Architecture Plan Part 72.
    """
    if len(cashflows) != len(dates):
        raise ValueError("cashflows and dates must have the same length.")
    if rate <= -1:
        raise ValueError(f"rate must be > -1, got {rate}.")

    t0 = dates[0]
    pv = 0.0
    for cf, d in zip(cashflows, dates):
        years = (d - t0).days / 365.25
        pv += cf / (1.0 + rate) ** years
    return pv


# ─────────────────────────────────────────────────────────────────────────────
# XIRR — exact-date internal rate of return  (Part 72)
# ─────────────────────────────────────────────────────────────────────────────

def compute_xirr(
    cashflows: Sequence[float],
    dates: Sequence[date],
    guess: float = 0.10,
) -> float:
    """
    XIRR: find the annual rate r such that XNPV(r, cashflows, dates) = 0.

    Uses bisection for robust root-finding (no external dependencies).

    Args:
        cashflows: Must include at least one sign change (one negative
                   investment + positive future flows).
        dates:     One date per cash flow.
        guess:     Initial rate hint (unused; bisection uses fixed bounds).

    Returns:
        Annualised IRR as a decimal.

    Raises:
        ValueError: if no IRR solution found or no sign change in cashflows.

    Reference: Architecture Plan Part 72.
    """
    # Validate sign change
    pos = any(c > 0 for c in cashflows)
    neg = any(c < 0 for c in cashflows)
    if not (pos and neg):
        raise ValueError(
            "XIRR requires at least one positive and one negative cash flow."
        )

    def _npv(r: float) -> float:
        return compute_xnpv(r, cashflows, dates)

    # Bisection over [-99.9%, 10000%]
    lo, hi = -0.999, 100.0
    f_lo, f_hi = _npv(lo), _npv(hi)
    if f_lo * f_hi > 0:
        raise ValueError(
            "XIRR: no solution found in [-99.9%, 10000%]. "
            "Check that cash flows have a sign change."
        )
    for _ in range(100):
        mid   = (lo + hi) / 2.0
        f_mid = _npv(mid)
        if abs(f_mid) < 1e-8 or (hi - lo) < 1e-10:
            return float(mid)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return float((lo + hi) / 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Build date array for a DCF cash-flow schedule
# ─────────────────────────────────────────────────────────────────────────────

def build_dcf_dates(
    valuation_date: date,
    forecast_years: int,
    mid_year_convention: bool = True,
    fiscal_year_end_month: int = 12,
) -> list[date]:
    """
    Build the list of cash-flow dates for a DCF with `forecast_years`
    explicit forecast periods plus a terminal value.

    Mid-year convention: CF for year t arrives at t − 0.5 years.
    End-of-year convention: CF for year t arrives at t years.

    Returns a list of length forecast_years + 1:
        [CF_y1_date, CF_y2_date, ..., CF_yN_date, TV_date]

    The TV is discounted at end-of-period (year N), regardless of convention.

    Reference: Architecture Plan Part 72.
    """
    dates = []
    for yr in range(1, forecast_years + 1):
        if mid_year_convention:
            offset_years = yr - 0.5
        else:
            offset_years = float(yr)
        offset_days = int(round(offset_years * 365.25))
        dates.append(valuation_date + timedelta(days=offset_days))

    # Terminal value always at end of year N (not mid-year)
    tv_offset_days = int(round(forecast_years * 365.25))
    dates[-1] = valuation_date + timedelta(days=tv_offset_days)

    return dates


# ─────────────────────────────────────────────────────────────────────────────
# Standard integer-period NPV (fallback when dates unavailable)
# ─────────────────────────────────────────────────────────────────────────────

def compute_pv_cashflows(
    cashflows: Sequence[float],
    rate: float,
    mid_year: bool = True,
) -> list[float]:
    """
    Discount each cash flow at integer periods (or mid-year).

    Returns a list of present values matching the input cashflows.
    """
    pvs = []
    for i, cf in enumerate(cashflows):
        t = (i + 1) - (0.5 if mid_year else 0)
        pvs.append(cf / (1.0 + rate) ** t)
    return pvs
