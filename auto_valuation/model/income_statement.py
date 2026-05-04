"""
model/income_statement.py — Revenue, EBIT, NOPAT, D&A, and UFCF helpers.

Reference: Architecture Plan Parts 2, 3, 5, 6, 10, 11, 13, 16, 20, 25, 26, 27.

All monetary values in USD millions.
"""

from __future__ import annotations

import math
from typing import Any

from auto_valuation.utils.error import safe_divide, ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Revenue growth  (Part 5)
# ─────────────────────────────────────────────────────────────────────────────

def historical_revenue_cagr(
    income_stmts: list[dict],
    years: int = 5,
) -> float:
    """
    Compute historical revenue CAGR over `years` years.
    Returns 0.0 if insufficient data.
    Reference: Part 5.
    """
    stmts = sorted(income_stmts, key=lambda s: s.get("calendarYear", ""), reverse=True)
    if len(stmts) < years + 1:
        years = len(stmts) - 1
    if years <= 0:
        return 0.0

    rev_recent = stmts[0].get("revenue") or 0
    rev_base   = stmts[years].get("revenue") or 0
    if rev_base <= 0:
        return 0.0

    return (rev_recent / rev_base) ** (1 / years) - 1


def infer_revenue_lifecycle_stage(
    base_revenue: float,
    near_term_growth: float,
    terminal_growth: float,
) -> str:
    """Classify revenue maturity for dynamic fade scheduling."""
    revenue = float(base_revenue or 0.0)
    near = float(near_term_growth or 0.0)
    terminal = float(terminal_growth or 0.0)
    spread = near - terminal
    if near <= 0 or spread <= 0.005:
        return "declining"
    if near >= 0.18 or (revenue > 0 and revenue < 1_000 and near >= 0.10):
        return "hypergrowth"
    if near >= 0.10 or (revenue > 0 and revenue < 10_000 and near >= 0.06):
        return "growth"
    if revenue >= 50_000 and near <= 0.06:
        return "mature"
    return "standard"


def revenue_growth_fade_schedule(
    near_term_growth: float,
    terminal_growth: float,
    forecast_years: int = 10,
    fade_start_year: int = 3,
    lifecycle_stage: str | None = None,
) -> list[float]:
    """Return annual revenue growth rates with optional lifecycle-aware fade."""
    if forecast_years <= 0:
        return []

    stage = (lifecycle_stage or "standard").lower()
    hold_year = int(fade_start_year or 1)
    curvature = 1.0
    if stage == "hypergrowth":
        hold_year = min(max(2, hold_year), max(1, forecast_years - 1))
        curvature = 0.75
    elif stage == "growth":
        hold_year = min(max(2, hold_year), max(1, forecast_years - 1))
        curvature = 0.90
    elif stage == "mature":
        hold_year = 1
        curvature = 1.25
    elif stage == "declining":
        hold_year = 1
        curvature = 1.0

    hold_year = max(1, min(hold_year, forecast_years))
    if forecast_years == 1:
        return [terminal_growth]

    schedule: list[float] = []
    fade_years = max(1, forecast_years - hold_year)
    for year in range(1, forecast_years + 1):
        if year <= hold_year:
            growth = near_term_growth
        else:
            progress = (year - hold_year) / fade_years
            shaped_progress = max(0.0, min(1.0, progress)) ** curvature
            growth = near_term_growth + (terminal_growth - near_term_growth) * shaped_progress
        schedule.append(growth)
    schedule[-1] = terminal_growth
    return schedule


def build_revenue_forecast(
    base_revenue: float,
    near_term_growth: float,
    terminal_growth: float,
    forecast_years: int = 10,
    fade_start_year: int = 3,
    lifecycle_stage: str | None = None,
) -> list[float]:
    """
    Build a revenue forecast list using a linear growth fade.

    Years 1 to fade_start_year: near_term_growth (held flat)
    Years fade_start_year+1 to forecast_years: linear interpolation from
      near_term_growth → terminal_growth

    Returns a list of `forecast_years` revenue values (not growth rates).
    Reference: Part 5.
    """
    if lifecycle_stage == "auto":
        lifecycle_stage = infer_revenue_lifecycle_stage(
            base_revenue,
            near_term_growth,
            terminal_growth,
        )
    growth_schedule = revenue_growth_fade_schedule(
        near_term_growth,
        terminal_growth,
        forecast_years,
        fade_start_year,
        lifecycle_stage,
    )
    revenues: list[float] = []
    prev = base_revenue
    for g in growth_schedule:
        prev = prev * (1 + g)
        revenues.append(prev)
    return revenues


# ─────────────────────────────────────────────────────────────────────────────
# EBIT margin  (Parts 6, 10)
# ─────────────────────────────────────────────────────────────────────────────

def historical_ebit_margin(
    income_stmts: list[dict],
    use_normalized: bool = True,
    years: int = 3,
) -> float:
    """
    Compute median EBIT margin over last `years` years.
    Uses `ebit_normalized` if available (and use_normalized=True), else `ebit`/`operatingIncome`.
    Reference: Part 6.
    """
    margins: list[float] = []
    for s in income_stmts[:years]:
        rev = s.get("revenue") or 0
        if rev <= 0:
            continue
        if use_normalized and s.get("ebit_normalized") is not None:
            ebit = s["ebit_normalized"]
        else:
            ebit = s.get("ebit") or s.get("operatingIncome") or 0
        margins.append(ebit / rev)

    if not margins:
        return 0.0
    return sorted(margins)[len(margins) // 2]   # median


def build_ebit_margin_forecast(
    base_margin: float,
    target_margin: float,
    forecast_years: int = 10,
    fade_years: int = 7,
) -> list[float]:
    """
    Fade EBIT margin from base_margin to target_margin over fade_years,
    then hold at target_margin for remaining forecast_years.

    Reference: Part 10.
    """
    margins: list[float] = []
    for yr in range(1, forecast_years + 1):
        if yr <= fade_years:
            m = base_margin + (target_margin - base_margin) * (yr / fade_years)
        else:
            m = target_margin
        margins.append(m)
    return margins


# ─────────────────────────────────────────────────────────────────────────────
# D&A  (Part 16)
# ─────────────────────────────────────────────────────────────────────────────

def historical_da_pct(income_stmts: list[dict], years: int = 3) -> float:
    """
    Historical D&A as % of revenue (median over last `years` years).
    Reference: Part 16.
    """
    pcts: list[float] = []
    for s in income_stmts[:years]:
        rev = s.get("revenue") or 0
        da  = s.get("da") or s.get("depreciationAndAmortization") or 0
        if rev > 0 and da != 0:
            pcts.append(abs(da) / rev)
    if not pcts:
        return 0.03   # 3% fallback
    return sorted(pcts)[len(pcts) // 2]


# ─────────────────────────────────────────────────────────────────────────────
# Tax rate normalisation  (Part 13)
# ─────────────────────────────────────────────────────────────────────────────

_STATUTORY_RATE_US = 0.21   # Post-TCJA US federal rate


def normalise_tax_rate(
    income_stmts: list[dict],
    statutory_rate: float = _STATUTORY_RATE_US,
    years: int = 5,
    max_rate: float = 0.40,
    min_rate: float = 0.05,
) -> float:
    """
    Derive the forward normalised effective tax rate.

    Logic:
      - Average effective tax rate = tax_expense / ebt (excluding years with negative EBT)
      - Cap at max_rate, floor at min_rate
      - If fewer than 2 valid years, fall back to statutory_rate

    Reference: Part 13.
    """
    effective_rates: list[float] = []
    for s in income_stmts[:years]:
        tax   = s.get("tax_expense") or s.get("incomeTaxExpense") or 0
        ebt   = s.get("ebt") or s.get("pretaxIncome") or (
                    (s.get("ebit") or s.get("operatingIncome") or 0)
                    - abs(s.get("interest_expense") or s.get("interestExpense") or 0)
                )
        if ebt > 0 and tax is not None:
            rate = abs(tax) / ebt
            if min_rate <= rate <= max_rate:
                effective_rates.append(rate)

    if len(effective_rates) < 2:
        return statutory_rate

    return sum(effective_rates) / len(effective_rates)


# ─────────────────────────────────────────────────────────────────────────────
# NOPAT and UFCF  (Parts 3, 20, 25)
# ─────────────────────────────────────────────────────────────────────────────

def compute_nopat(ebit: float, tax_rate: float) -> float:
    """
    NOPAT = EBIT × (1 − tax_rate).
    Reference: Part 3.
    """
    return ebit * (1.0 - tax_rate)


def compute_ufcf(
    ebit: float,
    tax_rate: float,
    da: float,
    capex: float,
    delta_nowc: float,
    sbc: float = 0.0,
    delta_deferred_tax: float = 0.0,
) -> float:
    """
    Unlevered Free Cash Flow (UFCF):
        UFCF = NOPAT + D&A + SBC + ΔDeferred Tax − CapEx − ΔNOWC

    SBC is a non-cash charge that reduces NI but has no cash outflow — it
    must be added back exactly like D&A. (v4.0 A.1 CRITICAL correction.)

    delta_deferred_tax: increase in net deferred tax liability (positive = add-back).
    When the deferred tax liability grows, cash taxes paid are less than income-statement
    tax expense, so the difference is a non-cash add-back in the UFCF waterfall.
    Per Macabacus Exhibit A UFCF waterfall (Session 14 gap A).

    All inputs in USD millions. capex should be POSITIVE (cash outflow).
    delta_nowc = NOWC_t − NOWC_{t-1} (positive = cash consumed).
    sbc = stock-based compensation (positive = non-cash expense to add back).

    Reference: Parts 3.1, 25; v4.0 A.1; Macabacus UFCF waterfall.
    """
    nopat = compute_nopat(ebit, tax_rate)
    return nopat + da + sbc + delta_deferred_tax - capex - delta_nowc


def compute_historical_ufcf(
    income_stmts: list[dict],
    cash_flows: list[dict],
    balance_sheets: list[dict],
    tax_rate: float | None = None,
) -> list[dict]:
    """
    Back-calculate historical UFCF for the last N years to anchor the model.

    Returns a list of dicts (most-recent-first), each with keys:
      calendarYear, revenue, ebit, nopat, da, capex, delta_nowc, ufcf

    Reference: Part 25.
    """
    if tax_rate is None:
        tax_rate = normalise_tax_rate(income_stmts)

    results: list[dict] = []

    # Sort descending by calendarYear
    is_sorted = sorted(income_stmts, key=lambda s: s.get("calendarYear", ""), reverse=True)
    cf_map    = {
        c.get("calendarYear", ""): c
        for c in (cash_flows or [])
    }
    bs_sorted = sorted(balance_sheets, key=lambda s: s.get("calendarYear", ""), reverse=True)

    def _nowc(bs: dict) -> float:
        ar  = bs.get("accounts_receivable") or bs.get("netReceivables") or 0
        inv = bs.get("inventory") or bs.get("inventory") or 0
        ap  = bs.get("accounts_payable") or bs.get("accountPayables") or 0
        return ar + inv - ap

    for i, stmt in enumerate(is_sorted):
        yr  = stmt.get("calendarYear", "")
        rev = stmt.get("revenue") or 0
        ebit = (
            stmt.get("ebit_normalized")
            or stmt.get("ebit")
            or stmt.get("operatingIncome")
            or 0
        )
        cf  = cf_map.get(yr, {})
        da  = abs(stmt.get("da") or stmt.get("depreciationAndAmortization")
                  or cf.get("da") or cf.get("depreciationAndAmortization") or 0)
        capex = abs(cf.get("capex") or cf.get("capitalExpenditure") or 0)
        sbc   = abs(cf.get("sbc") or cf.get("stockBasedCompensation")
                    or cf.get("shareBasedCompensation") or 0)

        # ΔNOWC: compare to prior year BS (next in sorted list = one year earlier)
        delta_nowc = 0.0
        if i < len(bs_sorted) and i + 1 < len(bs_sorted):
            nowc_curr = _nowc(bs_sorted[i])
            nowc_prev = _nowc(bs_sorted[i + 1])
            delta_nowc = nowc_curr - nowc_prev

        nopat = compute_nopat(ebit, tax_rate)
        ufcf  = compute_ufcf(ebit, tax_rate, da, capex, delta_nowc, sbc)

        results.append({
            "calendarYear": yr,
            "revenue":      rev,
            "ebit":         ebit,
            "nopat":        nopat,
            "da":           da,
            "capex":        capex,
            "delta_nowc":   delta_nowc,
            "sbc":          sbc,
            "ufcf":         ufcf,
        })

    return results


# ─────────────────────────────────────────────────────────────────────────────
# SBC treatment  (Part 26)
# ─────────────────────────────────────────────────────────────────────────────

def average_sbc_pct_revenue(
    income_stmts: list[dict],
    cash_flows: list[dict],
    years: int = 3,
) -> float:
    """
    Return average SBC as % of revenue over last `years` years.
    Used for dilution modelling (SBC gross-up in diluted share count).
    Reference: Part 26.
    """
    cf_map = {c.get("calendarYear", ""): c for c in (cash_flows or [])}
    pcts: list[float] = []
    for s in income_stmts[:years]:
        yr  = s.get("calendarYear", "")
        rev = s.get("revenue") or 0
        cf  = cf_map.get(yr, {})
        sbc = abs(cf.get("sbc") or cf.get("stockBasedCompensation") or 0)
        if rev > 0 and sbc > 0:
            pcts.append(sbc / rev)
    if not pcts:
        return 0.0
    return sum(pcts) / len(pcts)


# ─────────────────────────────────────────────────────────────────────────────
# EBITA — Non-deductible goodwill amortization  (Macabacus UFCF Exhibit A)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ebita(ebit: float, goodwill_amort: float = 0.0) -> float:
    """
    EBITA = EBIT + amortization of non-deductible goodwill.

    Per Macabacus UFCF methodology (Exhibit A), the standard UFCF walk starts
    from EBIT and then adds back "amortization of non-deductible goodwill" to
    reach EBITA before computing taxes.  This is required because non-deductible
    goodwill amortization reduces reported EBIT but does NOT reduce the tax bill,
    so taxes should be computed on EBITA not EBIT.

    Post-SFAS 142 (US GAAP): goodwill is not amortized under GAAP but may be
    amortized for IFRS or tax purposes.  For IFRS companies with goodwill
    amortization, or for US companies with tax goodwill amortization that is not
    deductible for book purposes, this adjustment is needed.

    UFCF (Macabacus Exhibit A method):
        EBIT
        + Amort of non-deductible goodwill
        = EBITA
        − Taxes on EBITA  (EBITA × projected tax rate)
        = Unlevered net income
        + D&A and other non-cash charges affecting EBIT (excl. goodwill)
        − CapEx
        − ΔNOWC
        = UFCF

    Args:
        ebit          : Earnings before interest and taxes ($M).
        goodwill_amort: Amortization of non-deductible goodwill ($M, positive).

    Returns:
        float — EBITA ($M).

    Reference: Macabacus "Unlevered Free Cash Flow" Exhibit A.
    """
    return ebit + max(goodwill_amort, 0.0)


def compute_ufcf_from_ebita(
    ebit: float,
    tax_rate: float,
    da: float,
    capex: float,
    delta_nowc: float,
    goodwill_amort: float = 0.0,
    sbc: float = 0.0,
) -> float:
    """
    UFCF using the Macabacus EBITA-based method (Exhibit A):

        UFCF = EBITA × (1 − tax_rate) + D&A + SBC − CapEx − ΔNOWC

    where EBITA = EBIT + non-deductible goodwill amortization.

    The difference vs. standard compute_ufcf() is that taxes are levied on
    EBITA rather than EBIT, which matters when goodwill_amort is non-zero and
    non-deductible for tax purposes.

    For US GAAP companies (no goodwill amortization): goodwill_amort = 0.0 and
    this function is exactly equivalent to compute_ufcf().

    Args:
        ebit          : EBIT ($M).
        tax_rate      : Effective / marginal tax rate (decimal).
        da            : D&A (deductible for tax; excludes goodwill amort) ($M, positive).
        capex         : Capital expenditure ($M, positive = outflow).
        delta_nowc    : Change in NOWC ($M; positive = increase = cash use).
        goodwill_amort: Non-deductible goodwill amortization ($M, positive).
        sbc           : Stock-based compensation ($M, positive = addback).

    Returns:
        float — UFCF ($M).

    Reference: Macabacus "Unlevered Free Cash Flow" Exhibit A.
    """
    ebita = compute_ebita(ebit, goodwill_amort)
    unlevered_ni = ebita * (1.0 - tax_rate)
    return unlevered_ni + da + sbc - capex - delta_nowc


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → build_revenue_forecast
compute_revenue_forecast = build_revenue_forecast

#: Canonical checklist name → normalise_tax_rate
normalize_effective_tax_rate = normalise_tax_rate

#: Canonical checklist name → historical_revenue_cagr
compute_revenue_cagr = historical_revenue_cagr


def compute_segment_forecast(
    segments: list[dict],
    forecast_years: int = 7,
    base_growth: float = 0.05,
) -> dict[str, list[float]]:
    """
    Forecast revenue for each segment independently.

    Each segment dict should have ``name`` and ``revenue`` (LTM value).
    Growth defaults to ``base_growth`` per year unless the segment supplies
    ``growth_rate``.

    Returns a dict mapping segment name → list of forecast revenues
    (length = forecast_years).

    Reference: Architecture Plan Part 45.2.
    """
    result: dict[str, list[float]] = {}
    for seg in (segments or []):
        name  = seg.get("name") or seg.get("segment") or "Unknown"
        base  = float(seg.get("revenue") or seg.get("revenue_ltm") or 0)
        g     = float(seg.get("growth_rate") or base_growth)
        forecasts: list[float] = []
        val = base
        for _ in range(forecast_years):
            val = val * (1 + g)
            forecasts.append(round(val, 4))
        result[name] = forecasts
    return result
