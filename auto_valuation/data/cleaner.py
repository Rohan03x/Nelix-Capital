"""
data/cleaner.py — Data normalisation, cleaning, and one-time item handling.

Reference: Architecture Plan Parts 2.3-2.8, 13, 28, 29, 42, 43, 46.1, 55.2, 55.3.

All functions operate on raw FMP dict lists and return cleaned versions.
Monetary fields are in USD millions throughout (after unit_normalize).
"""

from __future__ import annotations

import re
import warnings
from datetime import date, datetime
from typing import Any

from auto_valuation.utils.error import ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Unit normalisation  (Part 2.4)
# ─────────────────────────────────────────────────────────────────────────────

def unit_normalize(statements: list[dict], profile: dict) -> list[dict]:
    """
    Detect whether FMP is returning values in thousands / millions / billions
    and convert everything to USD millions.

    FMP states values in USD (not thousands) for US companies by default.
    For non-US companies the currency field in profile indicates the unit.
    Heuristic: if revenue for a known large-cap is < 1,000, values are in billions.
    If revenue > 10,000,000, values are in thousands.

    Reference: Part 2.4.
    """
    if not statements:
        return statements

    # Use the most-recent annual revenue as probe
    revenue_probe = None
    for stmt in statements:
        rev = stmt.get("revenue") or stmt.get("totalRevenue")
        if rev and rev != 0:
            revenue_probe = abs(rev)
            break

    if revenue_probe is None:
        return statements   # can't determine — leave as-is

    scale = 1.0
    if revenue_probe > 10_000_000:
        # Values are in thousands → divide by 1,000
        scale = 1.0 / 1_000
    elif revenue_probe < 1_000 and revenue_probe > 0:
        # Values are in billions → multiply by 1,000
        scale = 1_000.0

    if scale == 1.0:
        return statements   # already in millions

    _NUMERIC_SKIP = {"calendarYear", "period", "reportedCurrency", "cik", "link"}
    scaled = []
    for stmt in statements:
        new_stmt = {}
        for k, v in stmt.items():
            if isinstance(v, (int, float)) and k not in _NUMERIC_SKIP:
                new_stmt[k] = v * scale
            else:
                new_stmt[k] = v
        scaled.append(new_stmt)
    return scaled


# ─────────────────────────────────────────────────────────────────────────────
# Field name standardisation  (Part 2.8)
# ─────────────────────────────────────────────────────────────────────────────

# Maps FMP field names → internal canonical names
_FIELD_MAP: dict[str, str] = {
    # Income statement
    "revenue":                              "revenue",
    "totalRevenue":                         "revenue",
    "costOfRevenue":                        "cogs",
    "grossProfit":                          "gross_profit",
    "operatingIncome":                      "ebit",
    "ebitda":                               "ebitda",
    "depreciationAndAmortization":          "da",
    "researchAndDevelopmentExpenses":       "rd_expense",
    "sellingGeneralAndAdministrativeExpenses": "sga",
    "interestExpense":                      "interest_expense",
    "incomeTaxExpense":                     "tax_expense",
    "netIncome":                            "net_income",
    "eps":                                  "eps_basic",
    "epsdiluted":                           "eps_diluted",
    "weightedAverageShsOut":                "shares_basic",
    "weightedAverageShsOutDil":             "shares_diluted",
    # Cash flow
    "operatingCashFlow":                    "cfo",
    "capitalExpenditure":                   "capex",
    "freeCashFlow":                         "fcf_reported",
    "stockBasedCompensation":               "sbc",
    "changeInWorkingCapital":               "delta_wc_reported",
    # Balance sheet
    "cashAndCashEquivalents":               "cash",
    "shortTermInvestments":                 "st_investments",
    "netReceivables":                       "accounts_receivable",
    "inventory":                            "inventory",
    "totalCurrentAssets":                   "current_assets",
    "propertyPlantEquipmentNet":            "ppe_net",
    "goodwill":                             "goodwill",
    "intangibleAssets":                     "intangibles",
    "totalAssets":                          "total_assets",
    "accountPayables":                      "accounts_payable",
    "shortTermDebt":                        "short_term_debt",
    "totalCurrentLiabilities":              "current_liabilities",
    "longTermDebt":                         "long_term_debt",
    "totalDebt":                            "total_debt",
    "deferredIncomeTax":                    "deferred_tax_liability",
    "totalLiabilities":                     "total_liabilities",
    "retainedEarnings":                     "retained_earnings",
    "totalStockholdersEquity":              "shareholders_equity",
    "totalEquity":                          "total_equity",
    "minorityInterest":                     "nci",
    "commonStock":                          "common_stock",
    "additionalPaidInCapital":              "apic",
    "accumulatedOtherComprehensiveIncomeLoss": "aoci",
}


def standardise_field_names(statements: list[dict]) -> list[dict]:
    """
    Rename FMP snake_case and camelCase fields to internal canonical names.
    Unknown fields are preserved as-is so nothing is lost.
    Reference: Part 2.8.
    """
    result = []
    for stmt in statements:
        new = {}
        for k, v in stmt.items():
            new[_FIELD_MAP.get(k, k)] = v
        result.append(new)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate / restatement detection  (Part 46.1)
# ─────────────────────────────────────────────────────────────────────────────

def deduplicate_financial_data(statements: list[dict]) -> list[dict]:
    """
    Remove duplicate fiscal years (same calendarYear or same date-year).
    When duplicates exist keep the one with the higher revenue (more complete restatement).
    Warns if duplicates are detected.
    Reference: Part 46.1.
    """
    seen: dict[str, dict] = {}
    for stmt in statements:
        year_key = str(stmt.get("calendarYear") or stmt.get("date", "")[:4])
        if not year_key:
            continue
        if year_key not in seen:
            seen[year_key] = stmt
        else:
            # Keep the record with higher revenue (likely the restated/corrected one)
            existing_rev = abs(seen[year_key].get("revenue", 0) or 0)
            new_rev      = abs(stmt.get("revenue", 0) or 0)
            if new_rev > existing_rev:
                seen[year_key] = stmt
            warnings.warn(
                f"Duplicate fiscal year {year_key} detected — keeping higher-revenue record.",
                ValuationWarning,
                stacklevel=2,
            )
    # Return in original order (most-recent-first for FMP)
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# M&A year detection  (Part 2.7)
# ─────────────────────────────────────────────────────────────────────────────

def detect_ma_years(statements: list[dict], threshold: float = 0.15) -> list[str]:
    """
    Return a list of calendar years where YoY revenue growth exceeds `threshold`
    (default 15%) — likely acquisition years that will distort trend analysis.
    Reference: Part 2.7.
    """
    # statements are most-recent-first; reverse for chronological order
    chron = list(reversed(statements))
    ma_years: list[str] = []
    for i in range(1, len(chron)):
        prev_rev = chron[i - 1].get("revenue") or 0
        curr_rev = chron[i].get("revenue")     or 0
        if prev_rev and curr_rev and prev_rev > 0:
            growth = (curr_rev - prev_rev) / prev_rev
            if growth > threshold:
                year = str(chron[i].get("calendarYear") or chron[i].get("date", "")[:4])
                ma_years.append(year)
    return ma_years


# ─────────────────────────────────────────────────────────────────────────────
# One-time item normalisation  (Parts 2.6, 42.1, 42.2)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_one_time_items(income_stmts: list[dict]) -> list[dict]:
    """
    Add back goodwill impairment and other non-recurring charges to EBIT
    so the model uses clean recurring EBIT.

    FMP does not always separate goodwill impairment — this adjusts where detectable.
    The caller can supplement with overrides/{TICKER}.json for manual adjustments.
    Reference: Parts 2.6, 42.2.
    """
    result = []
    for stmt in income_stmts:
        stmt = dict(stmt)   # copy
        # FMP may provide impairmentOfGoodwill as a negative (expense)
        goodwill_impairment = abs(stmt.get("impairmentOfGoodwill") or 0)
        restructuring       = abs(stmt.get("restructuringCharges") or 0)

        if "ebit" in stmt and stmt["ebit"] is not None:
            stmt["ebit_normalized"] = stmt["ebit"] + goodwill_impairment + restructuring
        elif "operatingIncome" in stmt and stmt["operatingIncome"] is not None:
            stmt["ebit_normalized"] = stmt["operatingIncome"] + goodwill_impairment + restructuring
        else:
            stmt["ebit_normalized"] = stmt.get("ebit") or stmt.get("operatingIncome")

        stmt["goodwill_impairment_addback"] = goodwill_impairment
        stmt["restructuring_addback"]       = restructuring
        result.append(stmt)
    return result


def strip_discontinued_ops(income_stmts: list[dict], cash_flows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Remove discontinued operations from EBIT and cash flow.
    FMP field: discontinuedOperationsNetIncome (may be 0 for most companies).
    Reference: Part 42.1.
    """
    def _strip_is(stmt: dict) -> dict:
        stmt = dict(stmt)
        disc = stmt.get("discontinuedOperationsNetIncome") or 0
        if disc and "net_income" in stmt and stmt["net_income"] is not None:
            stmt["net_income"] = stmt["net_income"] - disc
        stmt["discontinued_ops_stripped"] = disc
        return stmt

    def _strip_cf(stmt: dict) -> dict:
        stmt = dict(stmt)
        # FMP sometimes includes discontinued ops in OCF; no reliable field → leave as-is
        return stmt

    return [_strip_is(s) for s in income_stmts], [_strip_cf(s) for s in cash_flows]


# ─────────────────────────────────────────────────────────────────────────────
# R&D capitalisation  (Part 55.2)
# ─────────────────────────────────────────────────────────────────────────────

# Amortisation period by sector (years)
_RD_AMORT_YEARS: dict[str, int] = {
    "Information Technology": 3,
    "Health Care":            10,
    "": 5,   # default
}


def capitalise_rd(
    income_stmts: list[dict],
    sector: str = "",
    amort_years: int | None = None,
) -> list[dict]:
    """
    Capitalise R&D expense:
      - Removes R&D from operating expenses → boosts EBIT
      - Adds back amortisation of R&D asset → reduces EBIT partially
      - Net effect: EBIT adjusted for current-year over/under-investment in R&D

    Reference: Part 55.2.
    Returns statements with additional fields:
      rd_asset_opening, rd_asset_closing, rd_amort, ebit_rd_adjusted.
    """
    if amort_years is None:
        amort_years = _RD_AMORT_YEARS.get(sector, 5)

    amort_rate = 1.0 / amort_years
    rd_asset = 0.0   # accumulated capitalised R&D asset

    # Process chronologically (reversed since FMP is most-recent-first)
    chron = list(reversed(income_stmts))
    result_chron = []
    for stmt in chron:
        stmt = dict(stmt)
        rd = abs(stmt.get("rd_expense") or stmt.get("researchAndDevelopmentExpenses") or 0)
        rd_amort   = rd_asset * amort_rate
        rd_asset   = rd_asset * (1 - amort_rate) + rd
        ebit       = stmt.get("ebit") or stmt.get("operatingIncome") or 0

        stmt["rd_asset_closing"]   = rd_asset
        stmt["rd_amort"]           = rd_amort
        stmt["ebit_rd_adjusted"]   = ebit + rd - rd_amort
        result_chron.append(stmt)

    return list(reversed(result_chron))


# ─────────────────────────────────────────────────────────────────────────────
# Revenue recognition red flags  (Part 55.3)
# ─────────────────────────────────────────────────────────────────────────────

def check_revenue_recognition_flags(statements: list[dict]) -> list[str]:
    """
    Return a list of warning strings for ASC 606 / IFRS 15 red flags:
      1. AR days accelerating faster than revenue growth (>15pp spread)
      2. Deferred revenue declining while revenue is growing (pull-forward)

    Reference: Part 55.3.
    """
    flags: list[str] = []
    if len(statements) < 2:
        return flags

    # Most-recent-first; use two most-recent years
    curr = statements[0]
    prev = statements[1]

    def _ar_days(s: dict) -> float | None:
        rev = s.get("revenue") or 0
        ar  = s.get("accounts_receivable") or s.get("netReceivables") or 0
        if rev > 0:
            return ar / rev * 365
        return None

    curr_ar_days = _ar_days(curr)
    prev_ar_days = _ar_days(prev)
    if curr_ar_days and prev_ar_days:
        curr_rev = curr.get("revenue") or 0
        prev_rev = prev.get("revenue") or 0
        rev_growth_pp = ((curr_rev / prev_rev) - 1) * 100 if prev_rev else 0
        ar_days_change = curr_ar_days - prev_ar_days
        if ar_days_change > rev_growth_pp + 15:
            flags.append(
                f"AR days grew {ar_days_change:.1f} days vs revenue growth "
                f"{rev_growth_pp:.1f}pp — possible revenue pull-forward (Part 55.3)."
            )

    curr_deferred = curr.get("deferredRevenue") or 0
    prev_deferred = prev.get("deferredRevenue") or 0
    curr_rev = curr.get("revenue") or 0
    prev_rev = prev.get("revenue") or 0
    if curr_deferred < prev_deferred and curr_rev > prev_rev:
        flags.append(
            "Deferred revenue declined while revenue grew — "
            "possible recognition acceleration (Part 55.3)."
        )

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# FX conversion helpers  (Parts 29, B.4)
# ─────────────────────────────────────────────────────────────────────────────

def get_annual_avg_fx(
    from_currency: str,
    to_currency: str,
    year: int,
    fx_overrides: dict | None = None,
) -> float:
    """
    Return the annual average FX rate (from_currency / to_currency) for *year*.

    Attempts to load from *fx_overrides* dict first (keyed by "{FROM}/{TO}/{YEAR}").
    Falls back to 1.0 (no conversion) when no rate is available.

    Note: full FX data fetch (via FRED or openexchangerates) is handled by
    data/fx.py.  This function is the lightweight lookup used by cleaner.py.

    Reference: Architecture Plan Part 29, B.4.
    """
    if from_currency.upper() == to_currency.upper():
        return 1.0
    if fx_overrides:
        key = f"{from_currency.upper()}/{to_currency.upper()}/{year}"
        if key in fx_overrides:
            return float(fx_overrides[key])
        # Also try reverse
        rev_key = f"{to_currency.upper()}/{from_currency.upper()}/{year}"
        if rev_key in fx_overrides:
            return 1.0 / float(fx_overrides[rev_key])
    return 1.0   # fallback: no conversion


def convert_to_reporting_currency(
    statements: list[dict],
    from_currency: str,
    to_currency: str,
    fiscal_years: list[int] | None = None,
    fx_overrides: dict | None = None,
) -> list[dict]:
    """
    Apply annual-average FX rate to all monetary fields in each statement.

    *statements* are most-recent-first (FMP convention).
    *fiscal_years* maps each index to a calendar year; derived from 'calendarYear'
    if not supplied.

    Balance sheet closing rates should be applied separately (not done here —
    this function converts IS/CF flows using average rates).

    Reference: Architecture Plan Part 29.
    """
    if from_currency.upper() == to_currency.upper():
        return statements

    _NUMERIC_SKIP = {"calendarYear", "period", "reportedCurrency", "cik", "link"}
    result = []
    for i, stmt in enumerate(statements):
        stmt = dict(stmt)
        yr = (
            fiscal_years[i]
            if fiscal_years and i < len(fiscal_years)
            else int(str(stmt.get("calendarYear") or stmt.get("date", "0000"))[:4] or 0)
        )
        rate = get_annual_avg_fx(from_currency, to_currency, yr, fx_overrides)
        new_stmt = {}
        for k, v in stmt.items():
            if isinstance(v, (int, float)) and k not in _NUMERIC_SKIP:
                new_stmt[k] = v * rate
            else:
                new_stmt[k] = v
        result.append(new_stmt)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Goodwill impairment normalisation  (Part 42.2, N4)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_goodwill_impairment(income_stmts: list[dict]) -> list[dict]:
    """
    Add back goodwill impairment charges to arrive at normalised EBIT.
    FMP field: 'impairmentOfGoodwill' (reported as a negative expense).

    Creates 'ebit_normalized_gw' on each statement.  If 'ebit_normalized' was
    already set by normalize_one_time_items(), this function adds on top of it.

    Reference: Architecture Plan Part N4.
    """
    result = []
    for stmt in income_stmts:
        stmt = dict(stmt)
        impairment = abs(stmt.get("impairmentOfGoodwill") or 0)
        base_ebit = (
            stmt.get("ebit_normalized")
            or stmt.get("ebit")
            or stmt.get("operatingIncome")
            or 0
        )
        stmt["ebit_normalized_gw"] = base_ebit + impairment
        stmt["goodwill_impairment_normalized"] = impairment
        result.append(stmt)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Below-EBIT item taxonomy  (Part 44, N6)
# ─────────────────────────────────────────────────────────────────────────────

def extract_below_ebit_items(income_stmt: dict) -> dict:
    """
    Classify items that sit below EBIT into recurring and non-recurring buckets.

    Returns a dict with:
      recurring_below_ebit  — interest income + recurring other income
      nonrecurring_below_ebit — FX gains/losses, gains on sale, one-time items
      interest_income
      interest_expense
      other_income_recurring
      other_income_nonrecurring

    Reference: Architecture Plan Part N6.
    """
    interest_income   = abs(income_stmt.get("interestIncome") or 0)
    interest_expense  = abs(income_stmt.get("interestExpense") or income_stmt.get("interest_expense") or 0)
    total_other       = income_stmt.get("totalOtherIncomeExpensesNet") or 0
    # Non-operating: exclude interest from total_other to isolate "other income"
    other_net         = total_other + interest_expense - interest_income

    # FMP fields for non-recurring items
    fx_gain           = income_stmt.get("foreignCurrencyTransactionGain") or 0
    gain_on_sale      = income_stmt.get("gainLossOnSaleOfAssets") or 0
    non_recurring     = fx_gain + gain_on_sale

    recurring = interest_income + max(0.0, other_net - non_recurring)
    return {
        "recurring_below_ebit":     recurring,
        "nonrecurring_below_ebit":  non_recurring,
        "interest_income":          interest_income,
        "interest_expense":         interest_expense,
        "other_income_recurring":   max(0.0, other_net - non_recurring),
        "other_income_nonrecurring": non_recurring,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Average NOWC  (Part N16)
# ─────────────────────────────────────────────────────────────────────────────

def compute_average_nowc(
    balance_sheets: list[dict],
    years: int = 3,
) -> float:
    """
    Compute the N-year average Net Operating Working Capital.

    NOWC = (accounts_receivable + inventory) - accounts_payable

    Uses the most recent *years* balance sheets (most-recent-first list).
    Returns 0.0 if no data is available.

    Reference: Architecture Plan Part N16.
    """
    nowc_values: list[float] = []
    for bs in balance_sheets[:years]:
        ar  = bs.get("accounts_receivable") or bs.get("netReceivables") or 0
        inv = bs.get("inventory") or 0
        ap  = bs.get("accounts_payable") or bs.get("accountPayables") or 0
        nowc_values.append(ar + inv - ap)
    if not nowc_values:
        return 0.0
    return sum(nowc_values) / len(nowc_values)


# ─────────────────────────────────────────────────────────────────────────────
# Outlier year detection  (Part N9)
# ─────────────────────────────────────────────────────────────────────────────

def detect_outlier_years(
    statements: list[dict],
    field: str = "revenue",
    iqr_multiplier: float = 3.0,
) -> list[str]:
    """
    Return calendar years where *field* deviates more than *iqr_multiplier* × IQR
    from the median — classic outlier detection for noisy FMP data.

    Returns a list of year strings (empty if no outliers or insufficient data).

    Reference: Architecture Plan Part N9.
    """
    if len(statements) < 4:
        return []

    values = []
    years: list[str] = []
    for s in statements:
        v = s.get(field)
        yr = str(s.get("calendarYear") or s.get("date", "")[:4])
        if v is not None and v != 0:
            values.append((yr, float(v)))

    if len(values) < 4:
        return []

    vals_only = sorted(v for _, v in values)
    n = len(vals_only)
    q1 = vals_only[n // 4]
    q3 = vals_only[3 * n // 4]
    iqr = q3 - q1
    lo  = q1 - iqr_multiplier * iqr
    hi  = q3 + iqr_multiplier * iqr

    outlier_years = [yr for yr, v in values if v < lo or v > hi]
    return outlier_years


# ─────────────────────────────────────────────────────────────────────────────
# R&D capitalisation — American-spelling alias  (Part 55.2)
# ─────────────────────────────────────────────────────────────────────────────

def capitalize_rd(
    income_stmts: list[dict],
    sector: str = "",
    amort_years: int | None = None,
) -> list[dict]:
    """
    American-spelling alias for capitalise_rd().
    Capitalise R&D expense and add amortisation schedule.
    Reference: Architecture Plan Part 55.2.
    """
    return capitalise_rd(income_stmts, sector=sector, amort_years=amort_years)


def adjust_ebit_for_rd_capitalization(
    income_stmts: list[dict],
    sector: str = "",
    amort_years: int | None = None,
) -> list[dict]:
    """
    Apply R&D capitalisation and replace 'ebit' with the adjusted value.

    This is a convenience wrapper around capitalize_rd() that writes
    'ebit_rd_adjusted' back to the 'ebit' key so downstream code picks
    it up transparently.

    Reference: Architecture Plan Part 55.2.
    """
    capitalised = capitalize_rd(income_stmts, sector=sector, amort_years=amort_years)
    result = []
    for stmt in capitalised:
        stmt = dict(stmt)
        if "ebit_rd_adjusted" in stmt and stmt["ebit_rd_adjusted"] is not None:
            stmt["ebit"] = stmt["ebit_rd_adjusted"]
        result.append(stmt)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Revenue quality check  (Part O14, Q3)
# ─────────────────────────────────────────────────────────────────────────────

def check_revenue_quality(income_stmts: list[dict]) -> list[str]:
    """
    Check for revenue quality red flags:
      1. Negative revenue in any year (data error or heavy returns)
      2. YoY revenue decline > 70% (possible divestiture / FMP error)
      3. Revenue == 0 in any of the most recent 3 years

    Returns a list of warning strings (empty if no issues).

    Reference: Architecture Plan Part O14 / Q3.
    """
    warnings_list: list[str] = []
    if not income_stmts:
        return warnings_list

    for i, stmt in enumerate(income_stmts[:5]):
        rev = stmt.get("revenue") or 0
        yr = str(stmt.get("calendarYear") or stmt.get("date", "")[:4])
        if rev < 0:
            warnings_list.append(
                f"Revenue is negative in {yr} ({rev:,.0f}M) — likely data error."
            )
        if rev == 0 and i < 3:
            warnings_list.append(
                f"Revenue is zero in {yr} — excluded from averages; verify data."
            )

    # YoY decline > 70%
    chron = list(reversed(income_stmts[:6]))
    for i in range(1, len(chron)):
        prev_rev = chron[i - 1].get("revenue") or 0
        curr_rev = chron[i].get("revenue") or 0
        yr = str(chron[i].get("calendarYear") or chron[i].get("date", "")[:4])
        if prev_rev > 0 and curr_rev >= 0:
            decline = (prev_rev - curr_rev) / prev_rev
            if decline > 0.70:
                warnings_list.append(
                    f"Revenue fell {decline:.0%} in {yr} — possible divestiture or FMP data error."
                )
    return warnings_list
