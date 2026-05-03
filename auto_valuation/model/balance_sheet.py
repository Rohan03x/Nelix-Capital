"""
model/balance_sheet.py — PP&E rollforward, CapEx schedule, invested capital.

Reference: Architecture Plan Parts 15, 17, 22, 23.

All monetary values in USD millions.
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# CapEx model  (Part 17)
# ─────────────────────────────────────────────────────────────────────────────

def historical_capex_pct(
    income_stmts: list[dict],
    cash_flows: list[dict],
    years: int = 3,
) -> float:
    """
    Historical CapEx as % of revenue — median over last `years` years.
    Reference: Part 17.
    """
    cf_map = {c.get("calendarYear", ""): c for c in (cash_flows or [])}
    pcts: list[float] = []
    for s in income_stmts[:years]:
        yr    = s.get("calendarYear", "")
        rev   = s.get("revenue") or 0
        cf    = cf_map.get(yr, {})
        capex = abs(cf.get("capex") or cf.get("capitalExpenditure") or 0)
        if rev > 0 and capex > 0:
            pcts.append(capex / rev)
    if not pcts:
        return 0.04   # 4% fallback
    return sorted(pcts)[len(pcts) // 2]


def build_capex_forecast(
    revenues: list[float],
    capex_pct_revenue: float,
) -> list[float]:
    """
    Project CapEx for each forecast year as a fixed % of revenue.

    Returns a list of positive CapEx values (USD millions).
    Reference: Part 17.
    """
    return [rev * capex_pct_revenue for rev in revenues]


# ─────────────────────────────────────────────────────────────────────────────
# PP&E rollforward  (Part 22)
# ─────────────────────────────────────────────────────────────────────────────

def build_ppe_rollforward(
    opening_ppe_net: float,
    capex_schedule: list[float],
    da_schedule: list[float],
) -> list[dict]:
    """
    Simplified PP&E net rollforward:
        Closing PP&E = Opening PP&E + CapEx − D&A

    Returns list of dicts:
      year_index (1-based), opening_ppe, capex, da, closing_ppe

    Reference: Part 22.
    """
    records: list[dict] = []
    opening = opening_ppe_net
    for i, (capex, da) in enumerate(zip(capex_schedule, da_schedule)):
        closing = opening + capex - da
        records.append({
            "year_index":  i + 1,
            "opening_ppe": opening,
            "capex":       capex,
            "da":          da,
            "closing_ppe": closing,
        })
        opening = closing
    return records


# ─────────────────────────────────────────────────────────────────────────────
# D&A as % of PP&E or revenue  (Part 16, 23)
# ─────────────────────────────────────────────────────────────────────────────

def historical_da_pct_ppe(
    income_stmts: list[dict],
    cash_flows: list[dict],
    balance_sheets: list[dict],
    years: int = 3,
) -> float:
    """
    D&A as % of net PP&E (opening). If no PP&E data, falls back to 3% of revenue.
    Reference: Part 23.
    """
    bs_map = {b.get("calendarYear", ""): b for b in (balance_sheets or [])}
    cf_map = {c.get("calendarYear", ""): c for c in (cash_flows or [])}
    pcts: list[float] = []
    is_sorted = sorted(income_stmts, key=lambda s: s.get("calendarYear", ""), reverse=True)

    for i, s in enumerate(is_sorted[:years]):
        yr  = s.get("calendarYear", "")
        cf  = cf_map.get(yr, {})
        da  = abs(s.get("da") or s.get("depreciationAndAmortization")
                  or cf.get("da") or cf.get("depreciationAndAmortization") or 0)
        bs  = bs_map.get(yr, {})
        ppe = bs.get("ppe_net") or bs.get("propertyPlantEquipmentNet") or 0
        if ppe > 0 and da > 0:
            pcts.append(da / ppe)

    if not pcts:
        return 0.03
    return sum(pcts) / len(pcts)


# ─────────────────────────────────────────────────────────────────────────────
# Invested Capital (for ROIC computation)  (Part 15)
# ─────────────────────────────────────────────────────────────────────────────

def compute_invested_capital(balance_sheet: dict) -> float:
    """
    Invested Capital = Total Equity + Net Debt (ST + LT debt − cash)
    Alternative form: Total Assets − Non-Interest-Bearing Current Liabilities − Cash.

    Uses the equity + net debt approach.
    Reference: Part 15.
    """
    equity     = (balance_sheet.get("shareholders_equity")
                  or balance_sheet.get("totalStockholdersEquity") or 0)
    lt_debt    = (balance_sheet.get("long_term_debt")
                  or balance_sheet.get("longTermDebt") or 0)
    st_debt    = (balance_sheet.get("short_term_debt")
                  or balance_sheet.get("shortTermDebt") or 0)
    cash       = (balance_sheet.get("cash")
                  or balance_sheet.get("cashAndCashEquivalents") or 0)
    st_inv     = (balance_sheet.get("st_investments")
                  or balance_sheet.get("shortTermInvestments") or 0)
    nci        = (balance_sheet.get("nci")
                  or balance_sheet.get("minorityInterest") or 0)

    net_debt   = lt_debt + st_debt - cash - st_inv
    return equity + net_debt + nci


def compute_roic(nopat: float, invested_capital: float) -> float:
    """ROIC = NOPAT / Invested Capital. Returns 0.0 if IC ≤ 0."""
    return safe_divide(nopat, invested_capital, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Equity rollforward (forecast)  (Part 15)
# ─────────────────────────────────────────────────────────────────────────────

def build_equity_rollforward(
    opening_equity: float,
    net_income_schedule: list[float],
    dividends_pct_ni: float = 0.0,
    sbc_schedule: list[float] | None = None,
    buybacks_schedule: list[float] | None = None,
) -> list[dict]:
    """
    Equity rollforward:
        Closing Equity = Opening + Net Income − Dividends + SBC − Buybacks

    Note: SBC is non-cash and increases equity; buybacks reduce it.
    Both are simplified here — the DCF value driver is UFCF, not net income.

    Returns list of dicts per forecast year.
    Reference: Part 15.
    """
    records: list[dict] = []
    equity = opening_equity
    for i, ni in enumerate(net_income_schedule):
        dividends = ni * dividends_pct_ni
        sbc       = (sbc_schedule[i] if sbc_schedule else 0.0)
        buybacks  = (buybacks_schedule[i] if buybacks_schedule else 0.0)
        closing   = equity + ni - dividends + sbc - buybacks
        records.append({
            "year_index":    i + 1,
            "opening_equity": equity,
            "net_income":    ni,
            "dividends":     dividends,
            "sbc":           sbc,
            "buybacks":      buybacks,
            "closing_equity": closing,
        })
        equity = closing
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Deferred tax rollforward  (Part 53.1)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_deferred_tax(
    opening_dt_net: float,
    da_book: float,
    da_tax: float,
    rd_capitalised: float = 0.0,
    tax_rate: float = 0.21,
) -> float:
    """
    Simplified deferred tax rollforward.

    Deferred tax liability (DTL) arises when tax depreciation > book depreciation.
    Deferred tax asset (DTA) arises when book depreciation > tax depreciation.

        ΔDTL = (da_tax − da_book) × tax_rate
        closing_dt_net = opening_dt_net + ΔDTL + R&D_DTA_release

    Returns: closing net deferred tax position (negative = net DTA, positive = net DTL).
    Reference: Architecture Plan Part 53.1.
    """
    delta_dtl = (da_tax - da_book) * tax_rate
    rd_dta = rd_capitalised * tax_rate   # R&D cap creates a DTA that amortises over time
    return opening_dt_net + delta_dtl - rd_dta


# ─────────────────────────────────────────────────────────────────────────────
# Goodwill rollforward  (Part 53.2)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_goodwill(
    opening_goodwill: float,
    acquisitions_mm: float = 0.0,
    impairment_mm: float = 0.0,
    fx_adjustment_mm: float = 0.0,
) -> float:
    """
    Goodwill rollforward:
        closing = opening + acquisitions − impairment ± FX

    Under IFRS/US-GAAP, goodwill is NOT amortised (only impairment-tested annually).
    Acquisitions add goodwill; impairment reduces it.

    Returns: closing goodwill (USD millions).
    Reference: Architecture Plan Part 53.2.
    """
    return max(0.0, opening_goodwill + acquisitions_mm - impairment_mm + fx_adjustment_mm)


# ─────────────────────────────────────────────────────────────────────────────
# Intangibles rollforward  (Part 53.3)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_intangibles(
    opening_intangibles: float,
    new_intangibles_mm: float = 0.0,     # new identified intangibles from M&A
    amortisation_mm: float = 0.0,        # amortisation charge for the year
    impairment_mm: float = 0.0,
    amort_years: int = 10,               # for auto-calc of amort when amortisation_mm=0
) -> float:
    """
    Intangible assets rollforward:
        closing = opening + new_intangibles − amortisation − impairment

    If amortisation_mm is 0 and amort_years > 0, auto-estimates amortisation
    as opening / amort_years (straight-line approximation).

    Returns: closing intangibles balance (USD millions).
    Reference: Architecture Plan Part 53.3.
    """
    if amortisation_mm == 0 and amort_years > 0 and opening_intangibles > 0:
        amortisation_mm = opening_intangibles / amort_years
    return max(0.0, opening_intangibles + new_intangibles_mm - amortisation_mm - impairment_mm)


# ─────────────────────────────────────────────────────────────────────────────
# Retained earnings rollforward  (Part 76)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_retained_earnings(
    opening_re: float,
    net_income: float,
    dividends: float = 0.0,
    buybacks: float = 0.0,
    sbc_equity_credit: float = 0.0,   # non-cash SBC adds to equity through APIC, not RE
) -> float:
    """
    Retained earnings rollforward:
        closing_RE = opening_RE + net_income − dividends − buybacks

    Note: SBC flows through APIC (additional paid-in capital), not retained earnings.
    Dividends and buybacks reduce retained earnings.

    Returns: closing retained earnings (USD millions).
    Reference: Architecture Plan Part 76.
    """
    return opening_re + net_income - dividends - buybacks


# ─────────────────────────────────────────────────────────────────────────────
# Equity section builder  (Part 76)
# ─────────────────────────────────────────────────────────────────────────────

def build_equity_section(
    opening_common_equity: float,
    opening_re: float,
    opening_apic: float = 0.0,
    net_income: float = 0.0,
    dividends: float = 0.0,
    buybacks: float = 0.0,
    sbc_expense: float = 0.0,       # SBC flows through APIC
    new_equity_raised: float = 0.0,  # equity offerings
    other_ci: float = 0.0,          # other comprehensive income (FX, pensions, etc.)
) -> dict:
    """
    Build a full equity section rollforward for one forecast year.

    Components:
      Common stock & APIC: opening_apic + SBC + new equity issued
      Retained earnings:   opening_re + NI − dividends − buybacks
      Other CI:            other comprehensive income items
      Total equity:        APIC + RE + OCI

    Returns a dict with all equity section components.
    Reference: Architecture Plan Part 76.
    """
    closing_apic = opening_apic + sbc_expense + new_equity_raised
    closing_re   = rollforward_retained_earnings(
        opening_re=opening_re,
        net_income=net_income,
        dividends=dividends,
        buybacks=buybacks,
    )
    total_equity = closing_apic + closing_re + other_ci

    return {
        "opening_common_equity": opening_common_equity,
        "net_income":            net_income,
        "dividends":             dividends,
        "buybacks":              buybacks,
        "sbc_apic_credit":       sbc_expense,
        "new_equity_raised":     new_equity_raised,
        "other_ci":              other_ci,
        "closing_apic":          closing_apic,
        "closing_retained_earnings": closing_re,
        "closing_equity":        total_equity,
    }


# ─────────────────────────────────────────────────────────────────────────────
# APIC rollforward  (Part 14.1)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_apic(
    opening_apic: float,
    sbc_expense: float = 0.0,
    equity_issuances: float = 0.0,
    buybacks: float = 0.0,
    uses_treasury_stock: bool = False,
) -> float:
    """
    Additional Paid-in Capital (APIC) rollforward for one period.

    For companies that RETIRE shares immediately (no treasury stock — e.g. NIKE):
        Closing APIC = Opening APIC + SBC + equity_issuances − buybacks

    For companies that USE treasury stock (buybacks held as treasury):
        Closing APIC = Opening APIC + SBC + equity_issuances
        (buybacks do NOT reduce APIC; they increase treasury stock instead)

    Detection: if the company's balance sheet has a non-zero treasury stock line,
    set uses_treasury_stock=True.

    Args:
        opening_apic:       Prior-period APIC balance ($M).
        sbc_expense:        Stock-based compensation expensed in the period ($M, positive).
                            SBC is a non-cash charge that flows through APIC.
        equity_issuances:   Proceeds from new equity issuances / option exercises ($M).
        buybacks:           Shares repurchased ($M, positive = cash paid for buybacks).
        uses_treasury_stock: False = NIKE-style (buybacks reduce APIC);
                             True = treasury stock method (buybacks don't affect APIC).

    Returns:
        Closing APIC ($M).

    Reference: Architecture Plan Part 14.1.
    """
    closing = opening_apic + sbc_expense + equity_issuances
    if not uses_treasury_stock:
        closing -= buybacks
    return closing


# ─────────────────────────────────────────────────────────────────────────────
# AOCI rollforward  (Part 14.3)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_aoci(
    opening_aoci: float,
    fx_translation_gain_loss: float = 0.0,
    unrealized_securities_gain_loss: float = 0.0,
    pension_oci_adjustment: float = 0.0,
    cash_flow_hedge_gain_loss: float = 0.0,
    other_oci: float = 0.0,
) -> dict:
    """
    Accumulated Other Comprehensive Income (AOCI) rollforward for one period.

    OCI items (not recorded in the income statement) include:
      - Foreign currency translation adjustments
      - Unrealized gains/losses on AFS securities
      - Pension liability adjustments (actuarial gains/losses)
      - Cash flow hedge effective portions

    Closing AOCI = Opening AOCI + sum of OCI items

    Args:
        opening_aoci:                   Prior-period AOCI balance ($M, often negative).
        fx_translation_gain_loss:       CTA from translating foreign subsidiary financials.
        unrealized_securities_gain_loss: AFS / HTM mark-to-market adjustments.
        pension_oci_adjustment:         Actuarial gains/(losses) on defined benefit plans.
        cash_flow_hedge_gain_loss:      Effective portion of qualifying hedges.
        other_oci:                      All other OCI items not captured above.

    Returns:
        dict with 'total_oci' and 'closing_aoci'.

    Reference: Architecture Plan Part 14.3.
    """
    total_oci = (
        fx_translation_gain_loss
        + unrealized_securities_gain_loss
        + pension_oci_adjustment
        + cash_flow_hedge_gain_loss
        + other_oci
    )
    closing_aoci = opening_aoci + total_oci
    return {
        "opening_aoci":                    opening_aoci,
        "fx_translation":                  fx_translation_gain_loss,
        "unrealized_securities":           unrealized_securities_gain_loss,
        "pension_oci":                     pension_oci_adjustment,
        "cash_flow_hedge":                 cash_flow_hedge_gain_loss,
        "other_oci":                       other_oci,
        "total_oci":                       total_oci,
        "closing_aoci":                    closing_aoci,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → build_capex_forecast
compute_capex_forecast = build_capex_forecast

#: Canonical checklist name → build_ppe_rollforward
rollforward_ppe = build_ppe_rollforward

#: Canonical checklist name → rollforward_deferred_tax
deferred_tax_rollforward = rollforward_deferred_tax

#: Canonical checklist name → rollforward_goodwill
goodwill_rollforward = rollforward_goodwill

#: Canonical checklist name → rollforward_intangibles
intangibles_amortization_rollforward = rollforward_intangibles


def compute_da_forecast(
    opening_ppe: float,
    opening_intangibles: float,
    da_pct_ppe: float = 0.05,
    amort_pct_intangibles: float = 0.10,
) -> dict[str, float]:
    """
    Forecast depreciation and amortization for one forecast year.

    D&A = depreciation (% of opening PP&E) + amortization (% of opening intangibles).

    Reference: Architecture Plan Parts 3.1, 3.7.
    """
    depreciation   = opening_ppe        * da_pct_ppe
    amortization   = opening_intangibles * amort_pct_intangibles
    total_da       = depreciation + amortization
    return {
        "depreciation":  round(depreciation, 4),
        "amortization":  round(amortization, 4),
        "total_da":      round(total_da, 4),
    }


def capex_convergence_to_da(
    base_capex: float,
    base_da: float,
    convergence_years: int = 5,
    year: int = 1,
) -> float:
    """
    Linearly converge CapEx toward D&A over ``convergence_years``.

    In the terminal year, CapEx = D&A (steady-state assumption).
    For year 1 … convergence_years, CapEx transitions from ``base_capex``
    to ``base_da``.

    Reference: Architecture Plan Part 51.2.
    """
    if convergence_years <= 0 or year >= convergence_years:
        return base_da
    progress = year / convergence_years
    return base_capex + progress * (base_da - base_capex)

