"""
model/itax_shield.py — Interest Tax Shield (ITS) computation for APV.

The Adjusted Present Value (APV) framework splits firm value into:
  APV = Unlevered NPV + PV(Interest Tax Shield) + PV(Tax Loss Carryforwards)

This module computes the ITS component.

Reference: Architecture Plan Part 17.2.

ITS reconciliation (FFCF → EFCF):
  OFCF  = UFCF (operating free cash flow, unlevered)
  EFCF  = UFCF − interest_expense × (1 − tax_rate) + net_new_debt
          (equity free cash flow, after debt service)
  FFCF  = EFCF + ΔIBD  (firm free cash flow)
  ITS   = min(interest_expense × tax_rate, taxes_paid_current_year)

All monetary values in USD millions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Interest Tax Shield
# ─────────────────────────────────────────────────────────────────────────────

def compute_its(
    ibd_schedule: list[float],   # IBD balance at end of each year (year 0 to N)
    kd_pretax: float,            # pre-tax cost of debt
    tax_rate: float,             # effective / statutory tax rate
    taxes_paid_schedule: list[float] | None = None,  # actual taxes paid (cash basis)
) -> list[float]:
    """
    Compute the interest tax shield (ITS) for each forecast year.

    ITS_t = min(interest_expense_t × tax_rate, taxes_paid_t)

    Interest expense in year t uses average IBD: (IBD_{t-1} + IBD_t) / 2.
    ITS is capped at actual taxes paid in year t because you cannot shield
    more tax than you actually owe (ITS cannot create a tax refund in isolation).

    If taxes_paid_schedule is None, ITS is not capped (assume taxes > ITS).

    Args:
        ibd_schedule       : list of IBD balances, length = N+1 (year 0 through N).
        kd_pretax          : pre-tax cost of debt (e.g. 0.05 = 5%).
        tax_rate           : effective tax rate (e.g. 0.21).
        taxes_paid_schedule: optional list of taxes paid per year, length = N.

    Returns:
        list of ITS values for years 1..N  (length = N).

    Reference: Architecture Plan Part 17.2.
    """
    n = len(ibd_schedule) - 1
    if n <= 0:
        return []

    its_list = []
    for t in range(1, n + 1):
        avg_ibd = (ibd_schedule[t - 1] + ibd_schedule[t]) / 2.0
        interest_t = avg_ibd * kd_pretax
        raw_its = interest_t * tax_rate

        if taxes_paid_schedule is not None and len(taxes_paid_schedule) >= t:
            taxes_t = taxes_paid_schedule[t - 1]
            its = min(raw_its, max(0.0, taxes_t))
        else:
            its = raw_its

        its_list.append(its)
    return its_list


def pv_its(
    its_schedule: list[float],   # ITS per year (from compute_its)
    ku: float,                   # unlevered cost of capital (discount rate for ITS)
    mid_year: bool = True,
) -> float:
    """
    Present value of the interest tax shield stream.

    In the Miles-Ezzell (1980) framework, ITS is discounted at the unlevered
    cost of equity (ku) because leverage is rebalanced periodically.

    In the Modigliani-Miller (1963) framework (constant debt), ITS is discounted
    at kd (cost of debt).  This module uses ku (Miles-Ezzell) as the default for
    a DCF model with target capital structure.

    Args:
        its_schedule : list of ITS values for years 1..N.
        ku           : unlevered cost of capital (use WACC for approximation).
        mid_year     : True = mid-year convention (exponent t - 0.5).

    Returns:
        float — PV of all ITS values.

    Reference: Architecture Plan Part 17.2.
    """
    pv = 0.0
    for t, its in enumerate(its_schedule, start=1):
        exponent = t - 0.5 if mid_year else t
        pv += its / (1.0 + ku) ** exponent
    return pv


# ─────────────────────────────────────────────────────────────────────────────
# FCFE / FFCF reconciliation
# ─────────────────────────────────────────────────────────────────────────────

def compute_fcfe(
    ufcf: float,          # unlevered free cash flow (UFCF)
    interest_expense: float,  # interest expense for the year (positive = expense)
    tax_rate: float,
    net_new_debt: float = 0.0,  # new debt issued − repaid (positive = net issuance)
) -> float:
    """
    Equity Free Cash Flow (FCFE):
        FCFE = UFCF − interest_expense × (1 − tax_rate) + net_new_debt

    This is the cash available to equity holders after paying debt service.
    Reference: Architecture Plan Part 17.2, Part 33.2.
    """
    after_tax_interest = interest_expense * (1.0 - tax_rate)
    return ufcf - after_tax_interest + net_new_debt


def compute_ffcf(
    ufcf: float,
    delta_ibd: float,  # change in IBD for the year: IBD_t - IBD_{t-1}
) -> float:
    """
    Firm Free Cash Flow (FFCF):
        FFCF = UFCF + ΔIBD

    FFCF = EFCF + ΔIBD (by construction; both forms yield the same result)
    Reference: Architecture Plan Part 17.2.
    """
    return ufcf + delta_ibd


def its_reconciliation_check(
    ufcf_list: list[float],
    fcfe_list: list[float],
    its_list: list[float],
    ibd_schedule: list[float],
    interest_schedule: list[float],
    tax_rate: float,
    logger_name: str | None = None,
) -> list[dict]:
    """
    Cross-check the ITS reconciliation identity:
        OFCF + ITS = FFCF = EFCF + ΔIBD

    Flags any year where the discrepancy exceeds $0.01M (rounding tolerance).

    Returns a list of dicts: {'year', 'ofcf', 'its', 'ffcf', 'efcf', 'delta_ibd',
                               'check_ofcf_plus_its', 'check_efcf_plus_dibd', 'ok'}
    Reference: Architecture Plan Part 17.2.
    """
    log = logging.getLogger(logger_name or __name__)
    results = []
    n = min(len(ufcf_list), len(fcfe_list), len(its_list), len(interest_schedule))

    for t in range(n):
        ofcf = ufcf_list[t]
        efcf = fcfe_list[t]
        its = its_list[t]
        delta_ibd = ibd_schedule[t + 1] - ibd_schedule[t] if len(ibd_schedule) > t + 1 else 0.0

        ffcf_from_ofcf = ofcf + its
        ffcf_from_efcf = efcf + delta_ibd
        discrepancy = abs(ffcf_from_ofcf - ffcf_from_efcf)
        ok = discrepancy < 0.01

        if not ok:
            log.warning(
                f"ITS reconciliation discrepancy in year {t+1}: "
                f"OFCF+ITS={ffcf_from_ofcf:.3f}, EFCF+ΔIBD={ffcf_from_efcf:.3f}, "
                f"gap={discrepancy:.3f}m."
            )

        results.append({
            "year": t + 1,
            "ofcf": ofcf,
            "its": its,
            "efcf": efcf,
            "delta_ibd": delta_ibd,
            "ffcf_from_ofcf": ffcf_from_ofcf,
            "ffcf_from_efcf": ffcf_from_efcf,
            "discrepancy": discrepancy,
            "ok": ok,
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Adjusted Present Value (APV)  (Part 17.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_apv(
    ufcf_schedule: list[float],
    ku: float,
    ibd_schedule: list[float],
    kd_pretax: float,
    tax_rate: float,
    terminal_growth: float,
    taxes_paid_schedule: list[float] | None = None,
    mid_year: bool = True,
) -> dict[str, float]:
    """
    Adjusted Present Value (APV) — Method 2 DCF.

    APV = EV_unlevered + PV(ITS)

    where:
      EV_unlevered = PV of all UFCFs discounted at the unlevered cost of equity (ku).
      PV(ITS)      = present value of all interest tax shields.

    The APV method is economically equivalent to the standard WACC-based DCF
    (Method 1), but splits the value of leverage explicitly.  It is most useful
    when capital structure changes materially over the forecast period or for
    distressed company analysis (Part 17.1).

    Args:
        ufcf_schedule:        List of UFCF values for years 1..N ($M).
        ku:                   Unlevered cost of equity (= WACC if fully equity-financed).
                              Approximation: use the unlevered beta CAPM cost.
        ibd_schedule:         IBD balances from year 0 to N (length = N+1).
        kd_pretax:            Pre-tax cost of debt.
        tax_rate:             Effective tax rate.
        terminal_growth:      Terminal growth rate for ITS perpetuity (same as DCF terminal g).
        taxes_paid_schedule:  Optional — actual taxes paid, to cap ITS.
        mid_year:             Use mid-year convention (True = standard).

    Returns:
        dict with:
          'ev_unlevered'  — PV of UFCFs at unlevered cost (no ITS)
          'pv_its'        — PV of all ITS values
          'apv'           — EV_unlevered + PV(ITS)
          'its_schedule'  — list of year-by-year ITS values

    Reference: Architecture Plan Part 17.2.
    """
    n = len(ufcf_schedule)

    # 1) PV of UFCFs at unlevered cost (no debt benefit)
    ev_unlevered = 0.0
    for t, fcf in enumerate(ufcf_schedule, start=1):
        exponent = t - 0.5 if mid_year else t
        ev_unlevered += fcf / (1.0 + ku) ** exponent

    # Terminal value of UFCFs using Gordon Growth, discounted at ku
    if n > 0:
        last_fcf = ufcf_schedule[-1]
        spread = ku - terminal_growth
        if spread > 0:
            tv_ufcf = last_fcf / spread
            exponent = n - 0.5 if mid_year else n
            ev_unlevered += tv_ufcf / (1.0 + ku) ** exponent

    # 2) PV of Interest Tax Shield
    its_list = compute_its(ibd_schedule, kd_pretax, tax_rate, taxes_paid_schedule)
    pv_its_val = pv_its(its_list, ku, mid_year)

    # Terminal value of ITS in perpetuity (ITS_n / (ku - g))
    if its_list and (ku - terminal_growth) > 0:
        its_tv = its_list[-1] / (ku - terminal_growth)
        exponent = n - 0.5 if mid_year else n
        pv_its_val += its_tv / (1.0 + ku) ** exponent

    return {
        "ev_unlevered": ev_unlevered,
        "pv_its":       pv_its_val,
        "apv":          ev_unlevered + pv_its_val,
        "its_schedule": its_list,
    }

