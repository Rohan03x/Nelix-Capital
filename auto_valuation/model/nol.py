"""
model/nol.py — Net Operating Loss (NOL) / Tax Loss Carryforward (TLC) tracking.

Reference: Macabacus APV page ("Exhibit B – Tax-Loss Carryforwards"),
           Architecture Plan Part 17.2.

Key rules (US):
  • Pre-TCJA (pre-2018):   unlimited carryforward, 2-year carryback.
  • Post-TCJA (2018+):     unlimited carryforward, 0-year carryback,
                            80% taxable income utilisation cap.
  • Section 382 limitation: after ownership change (>50% in 3 years), annual
    NOL utilisation is capped at:
        cap = FMV_of_company × long_term_tax_exempt_rate

The APV framework values the TLC as the PV of future tax savings:
    PV(TLC) = Σ  (NOL_used_t × tax_rate) / (1 + ku)^t

All monetary values in USD millions.
"""

from __future__ import annotations

import logging

from auto_valuation.utils.error import safe_divide

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# NOL schedule computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_nol_carryforward(
    nol_opening: float,
    taxable_income_schedule: list[float],
    utilisation_cap_pct: float = 0.80,
    section_382_annual_cap_mm: float | None = None,
) -> list[dict]:
    """
    Compute year-by-year NOL utilisation and carryforward balance.

    Each year:
      1. Carry forward opening NOL balance.
      2. If taxable_income_t > 0, compute maximum usable NOL:
            usable = min(nol_balance, taxable_income × utilisation_cap_pct)
         Apply Section 382 cap if provided:
            usable = min(usable, section_382_annual_cap_mm)
      3. Reduce taxable income by NOL used → effective_taxable_income.
      4. Carry forward remaining NOL.

    NOTE: negative taxable income in a year INCREASES the NOL carryforward
    (generates a new loss to be carried forward; carryback is ignored here).

    Args:
        nol_opening             : Opening NOL balance ($M, positive).
        taxable_income_schedule : Pre-NOL taxable income for each year ($M).
                                  Negative values = losses → increase NOL pool.
        utilisation_cap_pct     : Max fraction of taxable income shielded by NOL
                                  in any year (TCJA default = 0.80; pre-TCJA = 1.0).
        section_382_annual_cap_mm: Optional annual utilisation cap from Sec. 382 ($M).

    Returns:
        list of dicts per year:
          {'year', 'taxable_income', 'nol_opening', 'nol_used',
           'effective_taxable_income', 'nol_closing'}

    Reference: Macabacus APV "Exhibit B – Tax-Loss Carryforwards".
    """
    schedule = []
    nol_balance = max(nol_opening, 0.0)

    for t, ti in enumerate(taxable_income_schedule, start=1):
        opening_bal = nol_balance

        if ti <= 0:
            # Loss year: NOL balance grows; no utilisation
            nol_used = 0.0
            effective_ti = ti
            nol_balance += abs(ti)
        else:
            # Profit year: utilise NOL
            max_usable = ti * utilisation_cap_pct
            if section_382_annual_cap_mm is not None and section_382_annual_cap_mm > 0:
                max_usable = min(max_usable, section_382_annual_cap_mm)
            nol_used = min(nol_balance, max(max_usable, 0.0))
            effective_ti = ti - nol_used
            nol_balance = nol_balance - nol_used

        schedule.append({
            "year":                     t,
            "taxable_income":           ti,
            "nol_opening":              opening_bal,
            "nol_used":                 nol_used,
            "effective_taxable_income": effective_ti,
            "nol_closing":              nol_balance,
        })

    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Tax saving from NOL  (for APV)
# ─────────────────────────────────────────────────────────────────────────────

def apply_nol_to_tax(
    taxable_income: float,
    nol_available: float,
    tax_rate: float,
    utilisation_cap_pct: float = 0.80,
) -> tuple[float, float, float]:
    """
    Apply available NOL to reduce taxes in a single year.

    Returns:
        (taxes_paid, nol_used, nol_remaining)

    taxes_paid = (taxable_income − nol_used) × tax_rate
    nol_used   = min(nol_available, taxable_income × utilisation_cap_pct)
    nol_remaining = nol_available − nol_used

    Args:
        taxable_income     : Pre-NOL taxable income ($M). If ≤ 0, no NOL is used.
        nol_available      : Opening NOL balance available ($M).
        tax_rate           : Marginal corporate tax rate (decimal).
        utilisation_cap_pct: Max fraction of taxable income shielded (0.80 = TCJA default).

    Returns:
        tuple: (taxes_paid, nol_used, nol_remaining) all in $M.

    Reference: Macabacus APV "Tax-Loss Carryforwards"; Architecture Plan Part 17.2.
    """
    if taxable_income <= 0 or nol_available <= 0:
        taxes = max(0.0, taxable_income * tax_rate)
        return taxes, 0.0, nol_available

    max_usable = taxable_income * utilisation_cap_pct
    nol_used = min(nol_available, max_usable)
    effective_ti = max(0.0, taxable_income - nol_used)
    taxes_paid = effective_ti * tax_rate
    nol_remaining = nol_available - nol_used

    return taxes_paid, nol_used, nol_remaining


# ─────────────────────────────────────────────────────────────────────────────
# PV of Tax Loss Carryforward  (APV component)
# ─────────────────────────────────────────────────────────────────────────────

def pv_nol_carryforward(
    nol_schedule: list[dict],
    tax_rate: float,
    ku: float,
    mid_year: bool = True,
) -> float:
    """
    Present value of the tax savings from NOL carryforward utilisation.

    Each year's tax saving = nol_used_t × tax_rate
    PV = Σ  (nol_used_t × tax_rate) / (1 + ku)^(t − 0.5 or t)

    This PV is added to the APV (alongside PV of ITS) to arrive at total APV:
        APV = EV_unlevered + PV(ITS) + PV(TLC)

    Args:
        nol_schedule : Output from compute_nol_carryforward() — list of year dicts.
        tax_rate     : Marginal tax rate (decimal).
        ku           : Unlevered cost of capital (discount rate).
        mid_year     : True = mid-year convention.

    Returns:
        float — PV of all future NOL tax savings ($M).

    Reference: Macabacus APV "Exhibit B – Tax-Loss Carryforwards".
    """
    pv = 0.0
    for row in nol_schedule:
        t = row["year"]
        nol_used = row.get("nol_used", 0.0)
        tax_saving = nol_used * tax_rate
        if tax_saving > 0:
            exponent = t - 0.5 if mid_year else float(t)
            pv += tax_saving / (1.0 + ku) ** exponent
    return pv


# ─────────────────────────────────────────────────────────────────────────────
# Validation helper
# ─────────────────────────────────────────────────────────────────────────────

def check_nol_utilisation(
    nol_schedule: list[dict],
    warn_threshold_pct: float = 0.80,
) -> dict:
    """
    Flag whether the company is expected to fully utilise its NOL within
    the forecast period.

    Returns:
        dict with:
          'fully_utilised' : True if nol_closing reaches 0 during forecast.
          'remaining_nol'  : Closing NOL balance at end of schedule ($M).
          'pct_utilised'   : % of opening NOL utilised.
          'status'         : 'PASS' or 'WARN'.
          'message'        : Human-readable summary.

    Reference: Architecture Plan Part 17.2.
    """
    if not nol_schedule:
        return {"fully_utilised": False, "remaining_nol": 0.0, "pct_utilised": 0.0,
                "status": "PASS", "message": "No NOL schedule provided."}

    opening = nol_schedule[0]["nol_opening"]
    closing = nol_schedule[-1]["nol_closing"]
    total_used = sum(row["nol_used"] for row in nol_schedule)
    pct_utilised = safe_divide(total_used, opening, 0.0) if opening > 0 else 0.0
    fully_utilised = closing <= 0.01

    if pct_utilised >= warn_threshold_pct or fully_utilised:
        status = "PASS"
        msg = f"NOL ${opening:.0f}m — {pct_utilised:.0%} utilised in forecast; ${closing:.0f}m remaining."
    else:
        status = "WARN"
        msg = (
            f"NOL ${opening:.0f}m — only {pct_utilised:.0%} utilised; "
            f"${closing:.0f}m still outstanding at forecast end. "
            "Consider extending the forecast or discounting the terminal NOL separately."
        )

    return {
        "fully_utilised": fully_utilised,
        "remaining_nol":  closing,
        "pct_utilised":   pct_utilised,
        "status":         status,
        "message":        msg,
    }
