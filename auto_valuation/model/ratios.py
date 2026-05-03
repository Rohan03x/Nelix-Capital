"""
model/ratios.py — Financial ratios: DuPont, ROIC, EVA, CFADS, coverage ratios,
WC days, BVPS/P/B, PEG, incremental ROIC, EBITDAR.

Reference: Architecture Plan Parts 18, 22, 26, 32, 47.2, 52, 54.4, 69, 74.

All monetary values in USD millions.  All rates as decimals.
"""

from __future__ import annotations

from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Safe division helper
# ─────────────────────────────────────────────────────────────────────────────

def _div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den and den != 0 else default


# ─────────────────────────────────────────────────────────────────────────────
# DuPont analysis  (Part 22)
# ─────────────────────────────────────────────────────────────────────────────

def compute_dupont_3factor(
    net_income: float,
    revenue: float,
    total_assets: float,
    total_equity: float,
) -> dict[str, float]:
    """
    3-factor DuPont: ROE = Net_Margin × Asset_Turnover × Equity_Multiplier.

    Returns dict with keys: net_margin, asset_turnover, equity_multiplier, roe.
    Reference: Architecture Plan Part 22.1.
    """
    net_margin       = _div(net_income, revenue)
    asset_turnover   = _div(revenue, total_assets)
    equity_multiplier = _div(total_assets, total_equity, default=float("nan"))
    roe              = net_margin * asset_turnover * equity_multiplier

    return {
        "net_margin":        net_margin,
        "asset_turnover":    asset_turnover,
        "equity_multiplier": equity_multiplier,
        "roe":               roe,
    }


def compute_dupont_5factor(
    net_income: float,
    pretax_income: float,
    ebit: float,
    revenue: float,
    total_assets: float,
    total_equity: float,
) -> dict[str, float]:
    """
    5-factor DuPont:
    ROE = Tax_Burden × Interest_Burden × EBIT_Margin × Asset_Turnover × Leverage.

    Returns dict with all 5 factors + roe.
    Reference: Architecture Plan Part 22.2.
    """
    tax_burden       = _div(net_income, pretax_income)
    interest_burden  = _div(pretax_income, ebit)
    ebit_margin      = _div(ebit, revenue)
    asset_turnover   = _div(revenue, total_assets)
    leverage         = _div(total_assets, total_equity, default=float("nan"))
    roe              = tax_burden * interest_burden * ebit_margin * asset_turnover * leverage

    return {
        "tax_burden":       tax_burden,
        "interest_burden":  interest_burden,
        "ebit_margin":      ebit_margin,
        "asset_turnover":   asset_turnover,
        "leverage":         leverage,
        "roe":              roe,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROIC  (Part 32, 52)
# ─────────────────────────────────────────────────────────────────────────────

def compute_roic(
    nopat: float,
    invested_capital_opening: float,
    invested_capital_closing: float | None = None,
) -> float:
    """
    ROIC = NOPAT / Average Invested Capital.

    If invested_capital_closing is None, uses opening IC only.
    Reference: Architecture Plan Parts 32, 52.
    """
    if invested_capital_closing is not None:
        avg_ic = (invested_capital_opening + invested_capital_closing) / 2.0
    else:
        avg_ic = invested_capital_opening
    return _div(nopat, avg_ic)


# ─────────────────────────────────────────────────────────────────────────────
# Incremental ROIC  (Part 69)
# ─────────────────────────────────────────────────────────────────────────────

def compute_incremental_roic(
    nopat_series: Sequence[float],
    ic_series: Sequence[float],
) -> list[float | None]:
    """
    Incremental ROIC for years y1..yN.

    incremental_roic[i] = (nopat[i] - nopat[i-1]) / (ic[i] - ic[i-1])

    Returns None for any year where ΔIC ≤ 0 (no new investment / disinvestment).
    Reference: Architecture Plan Part 69.
    """
    result: list[float | None] = []
    for i in range(1, len(nopat_series)):
        delta_ic    = ic_series[i] - ic_series[i - 1]
        delta_nopat = nopat_series[i] - nopat_series[i - 1]
        if delta_ic <= 0:
            result.append(None)
        else:
            result.append(delta_nopat / delta_ic)
    return result


def validate_reinvestment_consistency(
    terminal_g: float,
    terminal_roic: float,
    reinvestment_rate: float,
    wacc: float,
    tolerance: float = 0.05,
) -> dict[str, float | bool | str]:
    """
    Cross-check: implied_reinvestment_rate (g/ROIC) vs bottom-up RR.
    Also flags value-destroying growth (ROIC < WACC).

    Returns dict with keys: implied_rr, gap, consistent, value_creating.
    Reference: Architecture Plan Part 69.2.
    """
    implied_rr = _div(terminal_g, terminal_roic) if terminal_roic > 0 else None
    gap        = abs(reinvestment_rate - implied_rr) if implied_rr is not None else None
    consistent = gap is not None and gap <= tolerance
    value_creating = terminal_roic > wacc if terminal_roic is not None else None

    return {
        "implied_rr":     implied_rr,
        "bottom_up_rr":   reinvestment_rate,
        "gap":            gap,
        "consistent":     consistent,
        "value_creating": value_creating,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EVA — Economic Value Added  (Part 74 — Damodaran)
# ─────────────────────────────────────────────────────────────────────────────

def compute_eva(
    nopat_mm: float,
    wacc: float,
    ic_opening_mm: float,
) -> tuple[float, float, float | None]:
    """
    EVA = NOPAT − Capital_Charge = NOPAT − (WACC × IC_opening).

    Also returns (capital_charge_mm, roic_implied).
    Positive EVA → value creation; negative → value destruction.
    Reference: Architecture Plan Part 74.
    """
    capital_charge = wacc * ic_opening_mm
    eva            = nopat_mm - capital_charge
    roic_implied   = _div(nopat_mm, ic_opening_mm) if ic_opening_mm > 0 else None
    return eva, capital_charge, roic_implied


def compute_eva_series(
    nopat_series: Sequence[float],
    wacc: float,
    ic_series: Sequence[float],
) -> list[tuple[float, float, float | None]]:
    """
    Compute EVA for each forecast year.

    ic_series[0] = opening IC (year 0 actuals); ic_series[1..N] = forecast years.
    nopat_series[0..N-1] = forecast NOPAT for years 1..N.

    Returns list of (eva_mm, capital_charge_mm, roic_implied) for each year.
    Reference: Architecture Plan Part 74.
    """
    results = []
    for i, nopat in enumerate(nopat_series):
        ic_opening = ic_series[i]   # ic_series[0] is y0 actuals
        results.append(compute_eva(nopat, wacc, ic_opening))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# EBITDAR  (Part 26 — lease-heavy sectors)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ebitdar(
    ebitda: float,
    operating_lease_expense: float,
) -> float:
    """
    EBITDA + Rent (operating lease expense) = EBITDAR.

    Used for airlines and retail where operating leases are a core cost.
    Reference: Architecture Plan Part 26.
    """
    return ebitda + abs(operating_lease_expense)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage ratios  (Part 18)
# ─────────────────────────────────────────────────────────────────────────────

def compute_coverage_ratios(
    ebit: float,
    ebitda: float,
    interest_expense: float,
    debt_service: float,         # principal + interest
    capex: float,
    dividends: float = 0.0,
    lease_expense: float = 0.0,
) -> dict[str, float | None]:
    """
    Compute 7 coverage / leverage ratios.

    All inputs in USD millions.
    Reference: Architecture Plan Part 18.2.
    """
    # Interest Coverage Ratio (ICR)
    icr = _div(ebit, interest_expense)

    # Debt Service Coverage Ratio (DSCR)
    dscr = _div(ebitda, debt_service)

    # Fixed Charge Coverage Ratio (FCCR) — includes leases and dividends
    fixed_charges = interest_expense + lease_expense + dividends
    fccr = _div(ebitda, fixed_charges) if fixed_charges > 0 else None

    # CFADS / debt service (cash flow available for debt service)
    cfads = ebitda - capex - dividends
    cfads_dscr = _div(cfads, debt_service) if cfads > 0 else None

    # EBITDA / interest
    ebitda_icr = _div(ebitda, interest_expense)

    # EBITDAR / (interest + rent)
    ebitdar = ebitda + abs(lease_expense)
    ebitdar_ir = _div(ebitdar, interest_expense + abs(lease_expense)) if (interest_expense + abs(lease_expense)) > 0 else None

    # Times Interest Earned (TIE) — same as ICR but named separately
    tie = icr

    return {
        "icr":         icr,
        "dscr":        dscr,
        "fccr":        fccr,
        "cfads_dscr":  cfads_dscr,
        "ebitda_icr":  ebitda_icr,
        "ebitdar_ir":  ebitdar_ir,
        "tie":         tie,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CFADS — Cash Flow Available for Debt Service  (Part 18)
# ─────────────────────────────────────────────────────────────────────────────

def compute_cfads(
    ebitda: float,
    capex: float,
    taxes_paid: float,
    working_capital_change: float = 0.0,
) -> float:
    """
    Method 1 (top-down from EBITDA):
    CFADS = EBITDA − CapEx − Taxes_Paid − ΔNOWC

    Reference: Architecture Plan Part 18.
    """
    return ebitda - capex - taxes_paid - working_capital_change


def compute_cfads_from_ufcf(
    ufcf: float,
    interest_tax_shield: float,
) -> float:
    """
    Method 2 (from UFCF):
    CFADS = UFCF + Interest_Tax_Shield (add back ITS as financing item)

    Reference: Architecture Plan Part 18.
    """
    return ufcf + interest_tax_shield


# ─────────────────────────────────────────────────────────────────────────────
# BVPS / P/B  (Part 47.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_bvps(
    total_equity_mm: float,
    basic_shares_mm: float,
) -> float:
    """
    Book Value Per Share = Total Equity / Basic Shares Outstanding.
    Reference: Architecture Plan Part 47.2.
    """
    return _div(total_equity_mm, basic_shares_mm)


def compute_pb_ratio(
    current_price: float,
    bvps: float,
) -> float | None:
    """
    Price-to-Book ratio = Current Price / BVPS.
    Returns None if BVPS ≤ 0.
    """
    if bvps is None or bvps <= 0:
        return None
    return _div(current_price, bvps)


# ─────────────────────────────────────────────────────────────────────────────
# PEG ratio  (Part 54.4)
# ─────────────────────────────────────────────────────────────────────────────

def compute_peg_ratio(
    pe_ratio: float,
    eps_growth_pct: float,
) -> float | None:
    """
    PEG = P/E Ratio / EPS Growth Rate (expressed as a positive %).

    e.g. P/E = 20, EPS growth = 15% → PEG = 20/15 = 1.33×

    Returns None if growth ≤ 0 (PEG is meaningless for negative growth).
    Reference: Architecture Plan Part 54.4.
    """
    if eps_growth_pct is None or eps_growth_pct <= 0:
        return None
    if pe_ratio is None or pe_ratio <= 0:
        return None
    return _div(pe_ratio, eps_growth_pct)


# ─────────────────────────────────────────────────────────────────────────────
# Working capital days  (re-exported convenience functions)
# ─────────────────────────────────────────────────────────────────────────────

def compute_dso(ar_mm: float, revenue_mm: float) -> float:
    """Days Sales Outstanding = AR / (Revenue / 365)."""
    return ar_mm * 365.0 / revenue_mm if revenue_mm > 0 else 0.0


def compute_dio(inventory_mm: float, cogs_mm: float) -> float:
    """Days Inventory Outstanding = Inventory / (COGS / 365)."""
    return inventory_mm * 365.0 / cogs_mm if cogs_mm > 0 else 0.0


def compute_dpo(ap_mm: float, cogs_mm: float) -> float:
    """Days Payable Outstanding = AP / (COGS / 365)."""
    return ap_mm * 365.0 / cogs_mm if cogs_mm > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# FCFE — Free Cash Flow to Equity  (Part 33.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_fcfe(
    net_income: float,
    da: float,
    sbc: float,
    capex: float,
    delta_nowc: float,
    net_borrowings: float,
) -> float:
    """
    FCFE = NI + D&A + SBC − CapEx − ΔNOWC + Net Borrowings

    Net Borrowings = new debt issued − debt repaid.
    Reference: Architecture Plan Part 33.2.
    """
    return net_income + da + sbc - capex - delta_nowc + net_borrowings


# ─────────────────────────────────────────────────────────────────────────────
# NCI-adjusted NOPAT  (Part 67)
# ─────────────────────────────────────────────────────────────────────────────

def compute_nopat_nci_adjusted(
    ebit_consolidated: float,
    tax_rate: float,
    nci_pct: float = 0.0,
    nci_ebit_mm: float | None = None,
) -> tuple[float, float]:
    """
    NCI-adjusted NOPAT = (EBIT_consolidated − NCI_EBIT) × (1 − tax_rate).

    Two methods:
      A. Percentage: nci_ebit = ebit_consolidated × nci_pct
      B. Direct: nci_ebit = nci_ebit_mm (from FMP minorityInterestIncome)

    Returns (nopat_parent, nci_ebit).
    Reference: Architecture Plan Part 67.2.
    """
    if nci_ebit_mm is not None:
        nci_ebit = nci_ebit_mm
    else:
        nci_ebit = ebit_consolidated * nci_pct

    parent_ebit = ebit_consolidated - nci_ebit
    nopat       = parent_ebit * (1.0 - tax_rate)
    return nopat, nci_ebit


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases & helpers (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → compute_coverage_ratios
coverage_ratios = compute_coverage_ratios


def compute_ebitda(
    ebit: float,
    da: float,
) -> float:
    """
    EBITDA = EBIT + Depreciation & Amortization.

    Reference: Architecture Plan Part 5.2.
    """
    return ebit + da
