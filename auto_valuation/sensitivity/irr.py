"""
sensitivity/irr.py — Implied WACC, FCFE DCF, and exact-date IRR helpers.

Reference: Architecture Plan Part 72 (XIRR), Part 33 (FCFE), Part 43 (IRR).
"""

from __future__ import annotations

from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Implied WACC solver  (Part 43)
# ─────────────────────────────────────────────────────────────────────────────

def compute_implied_wacc(
    ufcfs: Sequence[float],
    terminal_value: float,
    pv_target: float,
    mid_year: bool = True,
    guess: float = 0.09,
) -> float:
    """
    Solve for the discount rate that makes PV(UFCFs + TV) = pv_target.

    Uses bisection on a closed-form integer-period NPV (no external deps).

    Args:
        ufcfs:          List of UFCFs for years 1..N (USD millions).
        terminal_value: Terminal value at end of year N (USD millions).
        pv_target:      Target present value (e.g. current EV).
        mid_year:       Use mid-year discounting if True.
        guess:          Unused, kept for API compatibility.

    Returns:
        Implied WACC as a decimal.

    Reference: Architecture Plan Part 43.
    """
    n = len(ufcfs)

    def _pv(r: float) -> float:
        total = 0.0
        for i, cf in enumerate(ufcfs):
            t = (i + 1) - (0.5 if mid_year else 0)
            total += cf / (1 + r) ** t
        total += terminal_value / (1 + r) ** n
        return total - pv_target

    lo, hi = 0.001, 5.0
    f_lo, f_hi = _pv(lo), _pv(hi)
    if f_lo * f_hi > 0:
        raise ValueError(
            f"compute_implied_wacc: no solution found for pv_target={pv_target:.1f}. "
            "Check that UFCFs and TV have a sign change around pv_target."
        )
    for _ in range(100):
        mid   = (lo + hi) / 2.0
        f_mid = _pv(mid)
        if abs(f_mid) < 1e-8 or (hi - lo) < 1e-10:
            return float(mid)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return float((lo + hi) / 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# FCFE levered equity cash flow  (Part 33.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_fcfe_series(
    ufcf_series: Sequence[float],
    interest_expense_series: Sequence[float],
    tax_rate: float,
    net_borrowings_series: Sequence[float],
) -> list[float]:
    """
    FCFE_t = UFCF_t + Interest_t × (1 − tax_rate) + Net_Borrowings_t

    Note: interest_expense is typically negative (cash outflow), so adding
    ITS restores the after-tax interest savings back to UFCF.

    Reference: Architecture Plan Part 33.2.
    """
    n = len(ufcf_series)
    fcfe_list = []
    for i in range(n):
        ufcf      = ufcf_series[i]
        int_exp   = interest_expense_series[i] if i < len(interest_expense_series) else 0.0
        net_borr  = net_borrowings_series[i]   if i < len(net_borrowings_series)  else 0.0
        # Interest expense is negative; ITS = -int_exp × tax_rate
        its       = -int_exp * tax_rate
        fcfe      = ufcf + its + net_borr
        fcfe_list.append(fcfe)
    return fcfe_list


# ─────────────────────────────────────────────────────────────────────────────
# Levered equity DCF  (FCFE-based)
# ─────────────────────────────────────────────────────────────────────────────

def compute_equity_value_from_fcfe(
    fcfe_series: Sequence[float],
    terminal_fcfe: float,
    cost_of_equity: float,
    terminal_growth: float,
    mid_year: bool = True,
) -> dict[str, float]:
    """
    Equity value = PV(FCFE_1..N) + PV(Terminal_Value).

    TV = terminal_fcfe / (ke − g).
    Reference: Architecture Plan Part 33.2.
    """
    if cost_of_equity <= terminal_growth:
        raise ValueError(
            f"cost_of_equity ({cost_of_equity:.4f}) must exceed "
            f"terminal_growth ({terminal_growth:.4f})."
        )

    n  = len(fcfe_series)
    ke = cost_of_equity

    pv_fcfe = 0.0
    for i, fcfe in enumerate(fcfe_series):
        t = (i + 1) - (0.5 if mid_year else 0)
        pv_fcfe += fcfe / (1 + ke) ** t

    tv = terminal_fcfe / (ke - terminal_growth)
    pv_tv = tv / (1 + ke) ** n

    equity_value = pv_fcfe + pv_tv

    return {
        "pv_fcfe_mm":       pv_fcfe,
        "terminal_value_mm": tv,
        "pv_tv_mm":         pv_tv,
        "equity_value_mm":  equity_value,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Implied share price from FCFE model
# ─────────────────────────────────────────────────────────────────────────────

def compute_fcfe_implied_price(
    equity_value_mm: float,
    diluted_shares_mm: float,
) -> float | None:
    """
    Implied share price = equity_value_mm / diluted_shares_mm.

    Returns None if shares_mm ≤ 0.
    Reference: Architecture Plan Part 33.2.
    """
    if not diluted_shares_mm or diluted_shares_mm <= 0:
        return None
    return equity_value_mm / diluted_shares_mm
