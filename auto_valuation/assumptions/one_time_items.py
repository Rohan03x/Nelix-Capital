"""
assumptions/one_time_items.py — Detection and removal of one-time items
from historical financial statements.

Reference: Architecture Plan Parts 42.1, 42.2, 43.

Functions:
  detect_restructuring()   — flag large restructuring charges
  detect_impairments()     — flag goodwill/asset impairment charges
  detect_tcja_years()      — flag years affected by US TCJA transition tax
  normalize_ebit()         — add back detected one-time charges to EBIT

All monetary values in USD millions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────

RESTRUCTURING_THRESHOLD_PCT = 0.02   # > 2% of revenue is material
IMPAIRMENT_THRESHOLD_PCT    = 0.05   # > 5% of revenue is material
TCJA_YEARS                  = {2017, 2018}   # Transition tax years


# ─────────────────────────────────────────────────────────────────────────────
# Detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def detect_restructuring(
    income_stmts: list[dict],
    threshold_pct: float = RESTRUCTURING_THRESHOLD_PCT,
) -> list[dict]:
    """
    Detect years with material restructuring charges.

    FMP fields checked:
      - restructuringCharges
      - otherExpenses (proxy when restructuring is embedded)

    Returns a list of dicts: {'year', 'amount_mm', 'pct_of_revenue', 'flagged'}
    Reference: Architecture Plan Part 42.2.
    """
    results = []
    for stmt in income_stmts:
        year = stmt.get("calendarYear") or stmt.get("date", "")[:4]
        revenue = abs(stmt.get("revenue") or 1)
        charge = abs(
            stmt.get("restructuringCharges") or
            stmt.get("restructuringAndMergerAndAcquisitionRelatedCosts") or
            0
        )
        pct = charge / revenue if revenue > 0 else 0.0
        flagged = pct >= threshold_pct and charge > 0
        results.append({
            "year": year,
            "amount_mm": charge,
            "pct_of_revenue": pct,
            "flagged": flagged,
        })
        if flagged:
            logger.info(
                f"Restructuring detected in {year}: ${charge:.0f}m "
                f"({pct:.1%} of revenue). Will add back to normalised EBIT."
            )
    return results


def detect_impairments(
    income_stmts: list[dict],
    balance_sheets: list[dict] | None = None,
    threshold_pct: float = IMPAIRMENT_THRESHOLD_PCT,
) -> list[dict]:
    """
    Detect years with material goodwill or asset impairment charges.

    FMP fields checked:
      - goodwillImpairmentLosses
      - impairmentOfIntangibles
      - assetImpairmentCharges

    Returns a list of dicts: {'year', 'goodwill_impairment', 'other_impairment', 'flagged'}
    Reference: Architecture Plan Part 42.2.
    """
    results = []
    for stmt in income_stmts:
        year = stmt.get("calendarYear") or stmt.get("date", "")[:4]
        revenue = abs(stmt.get("revenue") or 1)

        goodwill_imp = abs(stmt.get("goodwillImpairmentLosses") or 0)
        intangibles_imp = abs(stmt.get("impairmentOfIntangibles") or 0)
        other_imp = abs(stmt.get("assetImpairmentCharges") or 0)
        total_imp = goodwill_imp + intangibles_imp + other_imp

        pct = total_imp / revenue if revenue > 0 else 0.0
        flagged = pct >= threshold_pct and total_imp > 0
        results.append({
            "year": year,
            "goodwill_impairment_mm": goodwill_imp,
            "intangibles_impairment_mm": intangibles_imp,
            "other_impairment_mm": other_imp,
            "total_impairment_mm": total_imp,
            "pct_of_revenue": pct,
            "flagged": flagged,
        })
        if flagged:
            logger.info(
                f"Impairment detected in {year}: ${total_imp:.0f}m "
                f"({pct:.1%} of revenue). Will add back to normalised EBIT."
            )
    return results


def detect_tcja_years(
    income_stmts: list[dict],
    tcja_years: set[int] | None = None,
) -> list[dict]:
    """
    Flag years affected by the US Tax Cuts and Jobs Act (TCJA, December 2017).

    Companies incurred one-time transition tax charges in 2017 and 2018.
    These inflated effective tax rates should be excluded from normalised ETR.

    Returns a list of dicts: {'year', 'is_tcja_year', 'transition_tax_mm'}
    Reference: Architecture Plan Part 43.1.
    """
    tcja_set = tcja_years or TCJA_YEARS
    results = []
    for stmt in income_stmts:
        year_str = str(stmt.get("calendarYear") or stmt.get("date", "")[:4])
        try:
            year_int = int(year_str)
        except (ValueError, TypeError):
            year_int = 0

        is_tcja = year_int in tcja_set
        # FMP does not break out transition tax separately; use unusually high ETR as signal
        ebt = stmt.get("pretaxIncome") or stmt.get("ebt") or 0
        tax = abs(stmt.get("incomeTaxExpense") or stmt.get("tax_expense") or 0)
        if ebt > 0:
            effective_rate = tax / ebt
        else:
            effective_rate = 0.0

        # TCJA transition tax signal: ETR > 40% in 2017/2018
        transition_signal = is_tcja and effective_rate > 0.40
        results.append({
            "year": year_str,
            "is_tcja_year": is_tcja,
            "effective_tax_rate": effective_rate,
            "transition_tax_signal": transition_signal,
        })
        if transition_signal:
            logger.info(
                f"TCJA transition tax signal in {year_str}: ETR={effective_rate:.0%}. "
                f"Exclude from normalised ETR computation."
            )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# EBIT normalisation
# ─────────────────────────────────────────────────────────────────────────────

def normalize_ebit(
    ebit: float,
    restructuring_mm: float = 0.0,
    impairment_mm: float = 0.0,
    legal_settlement_mm: float = 0.0,
    gain_on_sale_mm: float = 0.0,
) -> float:
    """
    Add back one-time charges to reported EBIT to derive normalised EBIT.

    Add back (increase EBIT):
      + restructuring_mm    — restructuring is non-recurring
      + impairment_mm       — impairments are non-cash non-recurring
      + legal_settlement_mm — one-time legal payments

    Deduct (decrease EBIT):
      - gain_on_sale_mm     — remove non-recurring gains

    Reference: Architecture Plan Parts 42.1, 42.2.
    """
    return (
        ebit
        + restructuring_mm
        + impairment_mm
        + legal_settlement_mm
        - gain_on_sale_mm
    )


def build_normalized_history(
    income_stmts: list[dict],
    restructuring_results: list[dict] | None = None,
    impairment_results: list[dict] | None = None,
) -> list[dict]:
    """
    Apply normalisation add-backs to each year of income statement history.

    Returns a copy of income_stmts with 'ebit_normalized' field added.
    Reference: Architecture Plan Parts 42.1, 42.2.
    """
    restr_by_year = {}
    if restructuring_results:
        for r in restructuring_results:
            if r["flagged"]:
                restr_by_year[str(r["year"])] = r["amount_mm"]

    imp_by_year = {}
    if impairment_results:
        for r in impairment_results:
            if r["flagged"]:
                imp_by_year[str(r["year"])] = r["total_impairment_mm"]

    normalised = []
    for stmt in income_stmts:
        year = str(stmt.get("calendarYear") or stmt.get("date", "")[:4])
        ebit = stmt.get("ebit") or stmt.get("operatingIncome") or 0.0
        restr = restr_by_year.get(year, 0.0)
        imp = imp_by_year.get(year, 0.0)

        new_stmt = dict(stmt)
        new_stmt["ebit_normalized"] = normalize_ebit(
            ebit=ebit,
            restructuring_mm=restr,
            impairment_mm=imp,
        )
        normalised.append(new_stmt)
    return normalised
