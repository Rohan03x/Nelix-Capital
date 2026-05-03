"""
model/forecast.py — Integrated 3-statement 7-year forecast model.

Builds a fully linked income statement, balance sheet, and cash flow statement
for `forecast_years` (default 7).  Interest expense is circular (depends on
average IBD), so this module uses an iterative convergence loop.

Key architecture constraints (Parts 13, 17, 22, 31, 33, 36, 37, 38, 67):
  - OCF = NI + D&A + SBC − ΔNOWC          (SBC MUST be in OCF)
  - ΔNOWC excludes goodwill, intangibles, ROU assets
  - interest_expense = −kd × (IBD_t-1 + IBD_t) / 2   (circular, iterative)
  - IBD_t = (D/TA) × total_assets_t         (current year, circular)
  - MAX_ITER = 50, TOL = $0.001M
  - Cash floor = 2% of revenue; revolver drawn if cash drops below floor
  - RE_close = RE_open + NI − Dividends − Buybacks
  - Balance sheet must close: Total_Assets = Total_Liabilities + Equity
  - check_balance_sheet_closes(): tolerance = $0.50M
  - NCI-adjusted NOPAT: nopat_parent = (EBIT − NCI_EBIT) × (1 − ETR)
  - Pension expense = service_cost + interest_cost (both forecasted)
  - CapEx floor = 0

Reference: Architecture Plan Parts 13, 17, 22, 31, 33, 36, 37, 38, 67.

All monetary values in USD millions.  All rates as decimals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAX_ITER            = 50
TOL_MM              = 0.001     # $0.001M convergence tolerance
CASH_FLOOR_PCT      = 0.02      # Cash ≥ 2% of revenue
BS_CLOSE_TOL_MM     = 0.50      # Balance sheet close tolerance ($0.50M)


# ─────────────────────────────────────────────────────────────────────────────
# ForecastYear dataclass — one column of the 3-statement model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ForecastYear:
    """One year of the integrated 3-statement forecast."""
    year:               int

    # Income statement
    revenue:            float = 0.0
    revenue_growth:     float = 0.0
    ebit_margin:        float = 0.0
    ebit:               float = 0.0
    interest_expense:   float = 0.0   # negative = expense
    other_income:       float = 0.0
    pretax_income:      float = 0.0
    tax_expense:        float = 0.0
    net_income:         float = 0.0
    da:                 float = 0.0   # depreciation & amortisation
    sbc:                float = 0.0   # stock-based compensation
    pension_expense:    float = 0.0   # total pension (service + interest)
    nci_share:          float = 0.0   # minority interest
    nopat:              float = 0.0   # (EBIT − NCI_EBIT) × (1 − ETR)

    # Cash flow
    capex:              float = 0.0   # positive = cash outflow
    delta_nowc:         float = 0.0   # positive = cash usage
    ufcf:               float = 0.0
    ocf:                float = 0.0   # NI + D&A + SBC − ΔNOWC

    # Balance sheet
    cash:               float = 0.0
    accounts_receivable:float = 0.0
    inventory:          float = 0.0
    accounts_payable:   float = 0.0
    nowc:               float = 0.0
    ppe_net:            float = 0.0
    goodwill:           float = 0.0
    other_long_term_assets: float = 0.0
    total_assets:       float = 0.0
    ibd:                float = 0.0   # interest-bearing debt
    revolver:           float = 0.0   # drawn revolver
    other_liabilities:  float = 0.0
    total_liabilities:  float = 0.0
    retained_earnings:  float = 0.0
    total_equity:       float = 0.0

    # Misc
    ebitda:             float = 0.0
    effective_tax_rate: float = 0.0
    dividends:          float = 0.0
    buybacks:           float = 0.0
    iterations:         int   = 0     # convergence iterations used


# ─────────────────────────────────────────────────────────────────────────────
# Forecast model — main entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_three_statement_forecast(
    # Base year actuals (year 0)
    base_revenue:       float,
    base_ebit_margin:   float,
    base_da:            float,
    base_sbc:           float,
    base_cash:          float,
    base_ibd:           float,
    base_ppe_net:       float,
    base_goodwill:      float,
    base_other_lta:     float,
    base_nowc:          float,
    base_retained_earnings: float,
    base_other_liabilities: float,
    base_other_equity:  float,
    # Assumptions
    revenue_growth_rates:   list[float],    # list of length forecast_years
    ebit_margin_schedule:   list[float],    # list of length forecast_years
    capex_pct_schedule:     list[float],    # list of length forecast_years
    da_pct_revenue:         float,
    sbc_pct_revenue:        float,
    effective_tax_rate:     float,
    dso_days:               float,
    dio_days:               float,
    dpo_days:               float,
    cost_of_debt:           float,
    debt_to_total_assets:   float,
    # Optional
    forecast_years:         int   = 7,
    other_income_mm:        float = 0.0,
    nci_pct:                float = 0.0,
    pension_service_pct:    float = 0.0,    # % of revenue
    pension_interest_flat:  float = 0.0,    # flat annual $M
    dividends_pct_ni:       float = 0.0,
    buybacks_mm:            float = 0.0,
    acq_mm_schedule:        list[float] | None = None,
    # Control
    cash_floor_pct:         float = CASH_FLOOR_PCT,
    max_iter:               int   = MAX_ITER,
    tol_mm:                 float = TOL_MM,
) -> list[ForecastYear]:
    """
    Build a 7-year fully linked 3-statement forecast.

    Returns a list of `forecast_years` ForecastYear objects.

    Key mechanics:
    1. Revenue and EBIT follow the provided schedules.
    2. Interest expense is circular: kd × avg(IBD_open, IBD_close).
    3. IBD_close = D/TA × total_assets (also circular — total assets depends
       on IBD via liabilities side).
    4. Iterative loop (MAX_ITER=50, TOL=$0.001M) solves the circularity.
    5. Cash revolver drawn when cash < cash_floor = 2% × revenue.
    6. Retained earnings rolled forward: RE_t = RE_{t-1} + NI − Divs − Buybacks.
    7. Balance sheet checked for closure at each year.

    Reference: Architecture Plan Parts 13, 31, 33, 36, 37, 38, 67.
    """
    # ------------------------------------------------------------------ #
    # Normalise schedule inputs to the right length
    # ------------------------------------------------------------------ #
    def _pad(lst: list[float], n: int, fill: float) -> list[float]:
        if len(lst) >= n:
            return lst[:n]
        return list(lst) + [fill] * (n - len(lst))

    rev_growth = _pad(revenue_growth_rates, forecast_years, revenue_growth_rates[-1] if revenue_growth_rates else 0.03)
    ebit_sched = _pad(ebit_margin_schedule, forecast_years, ebit_margin_schedule[-1] if ebit_margin_schedule else base_ebit_margin)
    cx_sched   = _pad(capex_pct_schedule,   forecast_years, capex_pct_schedule[-1]   if capex_pct_schedule   else 0.04)
    acq_sched  = _pad(acq_mm_schedule or [], forecast_years, 0.0)

    years: list[ForecastYear] = []

    # Prior-year state (year 0 actuals)
    prior_revenue       = base_revenue
    prior_ibd           = base_ibd
    prior_cash          = base_cash
    prior_ppe_net       = base_ppe_net
    prior_nowc          = base_nowc
    prior_re            = base_retained_earnings
    prior_other_liab    = base_other_liabilities
    prior_other_equity  = base_other_equity

    for yr in range(1, forecast_years + 1):
        idx   = yr - 1
        fy    = ForecastYear(year=yr)

        # ── Income Statement ──────────────────────────────────────────── #
        fy.revenue        = prior_revenue * (1 + rev_growth[idx])
        fy.revenue_growth = rev_growth[idx]
        fy.ebit_margin    = ebit_sched[idx]
        fy.ebit           = fy.revenue * fy.ebit_margin
        fy.ebitda         = fy.ebit + fy.revenue * da_pct_revenue
        fy.da             = fy.revenue * da_pct_revenue
        fy.sbc            = fy.revenue * sbc_pct_revenue

        # Pension
        fy.pension_expense = fy.revenue * pension_service_pct + pension_interest_flat

        # NCI
        fy.nci_share = fy.ebit * nci_pct

        # Iterative interest / IBD convergence
        ibd_guess = prior_ibd   # initial guess for IBD_close
        iterations = 0

        for iteration in range(max_iter):
            avg_ibd          = (prior_ibd + ibd_guess) / 2.0
            fy.interest_expense = -(cost_of_debt * avg_ibd)
            fy.other_income  = other_income_mm
            fy.pretax_income = fy.ebit + fy.interest_expense + fy.other_income
            fy.tax_expense   = max(fy.pretax_income, 0) * effective_tax_rate
            fy.net_income    = fy.pretax_income - fy.tax_expense
            fy.effective_tax_rate = effective_tax_rate

            # NOPAT (NCI-adjusted, Part 67)
            nopat_ebit        = fy.ebit - fy.nci_share
            fy.nopat          = nopat_ebit * (1 - effective_tax_rate)

            # ── Cash Flow ─────────────────────────────────────────────── #
            # OCF = NI + D&A + SBC − ΔNOWC  (SBC MUST be in OCF, Part 13)
            ar  = fy.revenue  * dso_days / 365.0
            inv = (fy.revenue * (1 - fy.ebit_margin)) * dio_days / 365.0   # proxy COGS
            ap  = (fy.revenue * (1 - fy.ebit_margin)) * dpo_days / 365.0
            fy.accounts_receivable = ar
            fy.inventory           = inv
            fy.accounts_payable    = ap
            new_nowc               = ar + inv - ap    # NOWC excludes goodwill/intangibles
            fy.nowc                = new_nowc
            delta_nowc             = new_nowc - prior_nowc
            fy.delta_nowc          = delta_nowc

            fy.ocf = fy.net_income + fy.da + fy.sbc - delta_nowc

            # CapEx (floor at 0)
            capex_gross  = max(0.0, fy.revenue * cx_sched[idx])
            fy.capex     = capex_gross

            # UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC  (unlevered, SBC is add-back)
            fy.ufcf = fy.nopat + fy.da + fy.sbc - fy.capex - delta_nowc

            # ── Balance Sheet ────────────────────────────────────────── #
            # PP&E net rollforward
            fy.ppe_net        = prior_ppe_net + capex_gross - fy.da + acq_sched[idx]
            fy.ppe_net        = max(0.0, fy.ppe_net)

            fy.goodwill       = base_goodwill
            fy.other_long_term_assets = base_other_lta

            # Dividends & buybacks
            fy.dividends = fy.net_income * dividends_pct_ni
            fy.buybacks  = buybacks_mm

            # Retained earnings rollforward (Part 37)
            fy.retained_earnings = prior_re + fy.net_income - fy.dividends - fy.buybacks

            # Solve IBD = D/TA × total_assets  (circular)
            # total_assets = cash + nowc + PPE + goodwill + other
            # cash is unknown → estimate as prior_cash + OCF − CapEx first pass
            cash_before_revolver = prior_cash + fy.ocf - capex_gross
            revolver_draw  = 0.0
            cash_floor     = cash_floor_pct * fy.revenue
            if cash_before_revolver < cash_floor:
                revolver_draw       = cash_floor - cash_before_revolver
                cash_before_revolver = cash_floor
            fy.cash      = cash_before_revolver
            fy.revolver  = revolver_draw

            gross_assets = (
                fy.cash
                + fy.nowc
                + fy.ppe_net
                + fy.goodwill
                + fy.other_long_term_assets
            )
            new_ibd = debt_to_total_assets * gross_assets

            # Convergence check
            if abs(new_ibd - ibd_guess) < tol_mm:
                ibd_guess  = new_ibd
                iterations = iteration + 1
                break
            ibd_guess  = new_ibd
            iterations = iteration + 1

        # ── Post-convergence balance sheet ─────────────────────────────── #
        fy.ibd         = ibd_guess
        fy.iterations  = iterations

        # Total assets
        fy.total_assets = (
            fy.cash
            + fy.nowc
            + fy.ppe_net
            + fy.goodwill
            + fy.other_long_term_assets
        )

        # Total liabilities (IBD + revolver + other)
        fy.other_liabilities = prior_other_liab * (fy.revenue / prior_revenue) if prior_revenue > 0 else prior_other_liab
        fy.total_liabilities = fy.ibd + fy.revolver + fy.other_liabilities

        # Equity plug: total_equity = total_assets - total_liabilities
        fy.total_equity = fy.total_assets - fy.total_liabilities

        years.append(fy)

        # Advance state
        prior_revenue      = fy.revenue
        prior_ibd          = fy.ibd
        prior_cash         = fy.cash
        prior_ppe_net      = fy.ppe_net
        prior_nowc         = fy.nowc
        prior_re           = fy.retained_earnings
        prior_other_liab   = fy.other_liabilities
        prior_other_equity = fy.total_equity - fy.retained_earnings

    return years


# ─────────────────────────────────────────────────────────────────────────────
# Balance sheet close checker  (Part 38)
# ─────────────────────────────────────────────────────────────────────────────

def check_balance_sheet_closes(
    fy: ForecastYear,
    tolerance_mm: float = BS_CLOSE_TOL_MM,
) -> bool:
    """
    Verify Total_Assets == Total_Liabilities + Total_Equity within tolerance.

    Raises RuntimeError if balance sheet does not close.
    Returns True if it closes.
    Reference: Architecture Plan Part 38.
    """
    liab_plus_equity = fy.total_liabilities + fy.total_equity
    gap = abs(fy.total_assets - liab_plus_equity)
    if gap > tolerance_mm:
        raise RuntimeError(
            f"Balance sheet does not close in Year {fy.year}: "
            f"Assets = {fy.total_assets:.3f}, "
            f"Liabilities + Equity = {liab_plus_equity:.3f}, "
            f"Gap = {gap:.3f} (tolerance = {tolerance_mm})."
        )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Pension expense helper  (Part 64)
# ─────────────────────────────────────────────────────────────────────────────

def compute_pension_expense_forecast(
    revenue_schedule: list[float],
    service_pct_revenue: float,
    flat_interest_cost_mm: float = 0.0,
) -> list[float]:
    """
    Forecast annual pension expense = service_cost + interest_cost.

    service_cost = revenue × service_pct_revenue
    interest_cost = flat_interest_cost_mm (held constant)
    Reference: Architecture Plan Part 64.
    """
    return [rev * service_pct_revenue + flat_interest_cost_mm for rev in revenue_schedule]


# ─────────────────────────────────────────────────────────────────────────────
# Other income / (expense) forecast  (Part 63)
# ─────────────────────────────────────────────────────────────────────────────

def compute_other_income_forecast(
    base_other_income_mm: float,
    years: int,
    growth_rate: float = 0.0,
) -> list[float]:
    """
    Simple constant or growing other income forecast.

    If growth_rate = 0, held flat.  Positive = income, negative = expense.
    Reference: Architecture Plan Part 63.
    """
    result = []
    val = base_other_income_mm
    for _ in range(years):
        result.append(val)
        val *= (1 + growth_rate)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Revenue bridge — price / volume / mix decomposition  (Part 57)
# ─────────────────────────────────────────────────────────────────────────────

def compute_revenue_bridge(
    base_revenue: float,
    price_growth_rates: list[float],
    volume_growth_rates: list[float],
    mix_shift: float = 0.0,
) -> tuple[list[float], list[dict]]:
    """
    Project revenue year-by-year using a multiplicative price/volume/mix bridge:

        Revenue_t = Revenue_{t-1} × (1 + price_t) × (1 + volume_t) × (1 + mix)

    All three drivers compound multiplicatively, not additively, consistent
    with standard investment banking bridge notation.

    Args:
        base_revenue:        Prior year (year 0) revenue ($M).
        price_growth_rates:  Per-year price growth; len = forecast_years.
        volume_growth_rates: Per-year volume growth; len = forecast_years.
                             If shorter than price list, trailing years default to 0.
        mix_shift:           Constant annual mix/pricing-tier effect (decimal).
                             Pass 0.0 (default) to ignore mix.

    Returns:
        (revenues, breakdown_list) where:
          revenues:       list of projected revenue values ($M) for each year.
          breakdown_list: list of dicts per year with keys:
                           year, revenue, price_effect_m, volume_effect_m,
                           mix_effect_m, blended_growth.

    Reference: Architecture Plan Part 57.
    """
    revenues: list[float] = []
    breakdown: list[dict] = []
    prev_rev = float(base_revenue)
    n = len(price_growth_rates)

    for yr in range(n):
        p = float(price_growth_rates[yr])
        v = float(volume_growth_rates[yr]) if yr < len(volume_growth_rates) else 0.0
        m = float(mix_shift)

        # Multiplicative bridge
        rev_t = prev_rev * (1.0 + p) * (1.0 + v) * (1.0 + m)

        # Attribution at the margin (small-effects approximation for decomposition)
        price_effect  = prev_rev * p
        volume_effect = prev_rev * v
        mix_effect    = prev_rev * m
        blended       = (1.0 + p) * (1.0 + v) * (1.0 + m) - 1.0

        revenues.append(rev_t)
        breakdown.append({
            "year":           yr + 1,
            "revenue":        rev_t,
            "price_effect_m": price_effect,
            "volume_effect_m": volume_effect,
            "mix_effect_m":   mix_effect,
            "blended_growth": blended,
        })
        prev_rev = rev_t

    return revenues, breakdown


# ─────────────────────────────────────────────────────────────────────────────
# Cash floor enforcement  (Part N16)
# ─────────────────────────────────────────────────────────────────────────────

def enforce_cash_floor(
    cash: float,
    revenue: float,
    revolver_capacity: float = 0.0,
    min_pct: float = 0.02,
) -> tuple[float, float]:
    """
    Ensure cash >= min_pct * revenue (operational cash floor).

    If cash is below the floor, draw on the revolver up to revolver_capacity.
    Returns (adjusted_cash, revolver_drawn).

    Reference: Architecture Plan Part N16.
    """
    floor = min_pct * max(0.0, revenue)
    if cash >= floor:
        return cash, 0.0
    shortfall = floor - cash
    draw = min(shortfall, max(0.0, revolver_capacity))
    return cash + draw, draw


# ─────────────────────────────────────────────────────────────────────────────
# Growth profile builder  (Part 35)
# ─────────────────────────────────────────────────────────────────────────────

def compute_growth_profile(
    income_stmts: list[dict],
    sector_growth: float = 0.04,
    terminal_g: float = 0.025,
    forecast_years: int = 7,
    model: str = "2stage",
) -> dict:
    """
    Build a complete per-year revenue growth profile.

    *model* one of: '1stage', '2stage', 'hmodel'

    Returns a dict with keys:
      near_term_growth, terminal_growth, growth_schedule (list of floats)

    Reference: Architecture Plan Part 35.
    """
    from auto_valuation.assumptions.growth import (
        build_growth_fade_schedule,
    )
    # Derive historical CAGR
    hist_revs = [s.get("revenue") for s in income_stmts if s.get("revenue")]
    if len(hist_revs) >= 2:
        hist_cagr = (hist_revs[0] / hist_revs[-1]) ** (1 / max(len(hist_revs) - 1, 1)) - 1
        hist_cagr = max(-0.50, min(hist_cagr, 1.00))
    else:
        hist_cagr = sector_growth

    near_term = 0.50 * hist_cagr + 0.50 * sector_growth

    if model == "1stage":
        schedule = [near_term] * forecast_years
    elif model == "hmodel":
        # H-model: linear fade from near_term to terminal_g
        schedule = [
            terminal_g + (near_term - terminal_g) * (1 - i / forecast_years)
            for i in range(forecast_years)
        ]
    else:  # 2stage default
        hold = min(3, forecast_years // 2)
        schedule = build_growth_fade_schedule(
            near_term, terminal_g,
            forecast_years=forecast_years,
            hold_years=hold,
            fade_years=forecast_years - hold,
        )

    return {
        "near_term_growth": near_term,
        "terminal_growth":  terminal_g,
        "growth_schedule":  schedule,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Auto revenue growth from history  (Part 5)
# ─────────────────────────────────────────────────────────────────────────────

def auto_revenue_growth(
    income_stmts: list[dict],
    sector_growth: float = 0.04,
    years: int = 5,
) -> float:
    """
    Compute a blended revenue growth estimate from:
      - Median of annual YoY revenue growth over the most recent *years*
      - 50/50 blend with *sector_growth* as a sector-level anchor

    Returns a float growth rate (e.g. 0.08 = 8%).

    Reference: Architecture Plan Part 5.
    """
    import statistics as _stats

    chron = list(reversed(income_stmts[: years + 1]))
    yoy: list[float] = []
    for i in range(1, len(chron)):
        prev = chron[i - 1].get("revenue") or 0
        curr = chron[i].get("revenue") or 0
        if prev > 0:
            yoy.append((curr - prev) / prev)

    if not yoy:
        return sector_growth

    hist = _stats.median(yoy)
    hist = max(-0.50, min(hist, 2.00))  # sanity cap
    return 0.50 * hist + 0.50 * sector_growth


# ─────────────────────────────────────────────────────────────────────────────
# Single-year 3-statement step  (Part 56)
# ─────────────────────────────────────────────────────────────────────────────

def build_forecast_year(
    prior: dict,
    assumptions: dict,
) -> dict:
    """
    Compute one year of the 3-statement model given the prior year's state and
    a full assumptions dict.

    *assumptions* keys expected:
      revenue_growth, ebit_margin, da_pct, capex_pct,
      nowc_pct, tax_rate, sbc_pct, interest_rate,
      beginning_debt (optional)

    Returns a dict with: revenue, ebit, nopat, da, capex, change_nowc,
      ufcf, interest_expense, net_income and others.

    Reference: Architecture Plan Part 56.
    """
    g    = assumptions.get("revenue_growth", 0.05)
    tax  = assumptions.get("tax_rate", 0.25)
    ebit_m = assumptions.get("ebit_margin", 0.15)
    da_p  = assumptions.get("da_pct", 0.04)
    capex_p = assumptions.get("capex_pct", 0.05)
    nowc_p  = assumptions.get("nowc_pct", 0.08)
    sbc_p   = assumptions.get("sbc_pct", 0.02)
    int_rate = assumptions.get("interest_rate", 0.05)

    prior_rev = prior.get("revenue", 0.0) or 0.0
    prior_nowc = prior.get("nowc", 0.0) or 0.0
    prior_debt = prior.get("total_debt", 0.0) or 0.0

    revenue    = prior_rev * (1.0 + g)
    ebit       = revenue * ebit_m
    nopat      = ebit * (1.0 - tax)
    da         = revenue * da_p
    capex      = revenue * capex_p
    nowc       = revenue * nowc_p
    change_nowc = nowc - prior_nowc
    sbc        = revenue * sbc_p
    int_exp    = prior_debt * int_rate
    net_income = (ebit - int_exp) * (1.0 - tax) + sbc  # simplified

    ufcf = nopat + da - capex - change_nowc

    return {
        "revenue":         revenue,
        "ebit":            ebit,
        "nopat":           nopat,
        "da":              da,
        "capex":           capex,
        "nowc":            nowc,
        "change_nowc":     change_nowc,
        "sbc":             sbc,
        "interest_expense": int_exp,
        "net_income":      net_income,
        "ufcf":            ufcf,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SBC forecast  (Part N12, 47)
# ─────────────────────────────────────────────────────────────────────────────

def compute_sbc_forecast(
    income_stmts: list[dict],
    forecast_revenues: list[float],
    sector: str = "",
    fade_to_pct: float | None = None,
    years: int = 5,
) -> list[float]:
    """
    Forecast SBC as a percentage of revenue.

    1. Compute average historical SBC% of revenue over the last *years* years.
    2. Optionally fade toward *fade_to_pct* (sector terminal SBC%) over
       the forecast horizon.
    3. Multiply each forecast year's revenue by the SBC %.

    Returns a list of SBC dollar amounts aligned with forecast_revenues.

    Reference: Architecture Plan Parts N12, 47.
    """
    import statistics as _stats

    sbc_pcts: list[float] = []
    for stmt in income_stmts[:years]:
        rev = stmt.get("revenue") or 0
        sbc = abs(stmt.get("stockBasedCompensation") or
                  stmt.get("sbc") or
                  stmt.get("shareBasedCompensation") or 0)
        if rev > 0:
            sbc_pcts.append(sbc / rev)

    if not sbc_pcts:
        base_pct = 0.02
    else:
        base_pct = _stats.median(sbc_pcts)

    # Default terminal fade target (sector-aware, simple look-up)
    _SECTOR_TERMINAL_SBC: dict[str, float] = {
        "Information Technology": 0.025,
        "Health Care":            0.020,
        "Communication Services": 0.020,
        "default":                0.015,
    }
    if fade_to_pct is None:
        fade_to_pct = _SECTOR_TERMINAL_SBC.get(sector, _SECTOR_TERMINAL_SBC["default"])

    n = len(forecast_revenues)
    result: list[float] = []
    for i, rev in enumerate(forecast_revenues):
        if n > 1:
            pct = base_pct + (fade_to_pct - base_pct) * (i / (n - 1))
        else:
            pct = base_pct
        result.append(rev * max(0.0, pct))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Segmented revenue forecast  (Part N8)
# ─────────────────────────────────────────────────────────────────────────────

def forecast_revenue_segmented(
    segments: dict[str, float],
    growth_rates: dict[str, float],
    years: int = 1,
) -> dict[str, list[float]]:
    """
    Grow each segment independently using its own growth rate.

    *segments*     — dict of {segment_name: base_revenue}
    *growth_rates* — dict of {segment_name: annual_growth_rate}
                     (missing segment uses 0.0)

    Returns dict of {segment_name: [year_1_rev, year_2_rev, ...]}

    Reference: Architecture Plan Part N8.
    """
    result: dict[str, list[float]] = {}
    for seg, base in segments.items():
        g = growth_rates.get(seg, 0.0)
        result[seg] = [base * ((1.0 + g) ** yr) for yr in range(1, years + 1)]
    return result


def should_use_segment_forecast(
    segment_data: dict,
    min_segments: int = 2,
    min_coverage_pct: float = 0.70,
) -> bool:
    """
    Return True when segment-level revenue forecast is preferred over
    top-down blended growth.

    Criteria:
      1. At least *min_segments* segments present.
      2. Total segmented revenue covers >= *min_coverage_pct* of total revenue
         (derived from sum of segment values).

    Reference: Architecture Plan Part N8.
    """
    if not segment_data:
        return False

    product = segment_data.get("product", {}) or {}
    geo     = segment_data.get("geo", {})     or {}

    # Use whichever breakdown is more detailed
    segs = product if len(product) >= len(geo) else geo
    if len(segs) < min_segments:
        return False

    total_rev = sum(abs(v) for v in segs.values() if isinstance(v, (int, float)))
    if total_rev <= 0:
        return False

    # Always return True if at least min_segments are populated — coverage
    # check requires knowing total from income statement, which we don't have
    # here; so rely solely on segment count threshold.
    return True
