"""
assumptions/wacc.py — WACC computation via CAPM + Hamada equation.

Reference: Architecture Plan Parts 7, 12, 19, 33, 34.

All rates as decimals (0.09 = 9%).
"""

from __future__ import annotations

import math

from auto_valuation.utils.error import safe_divide, ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Beta (Hamada unlevering / re-levering)  (Part 12)
# ─────────────────────────────────────────────────────────────────────────────

def unlever_beta(
    levered_beta: float,
    debt_to_equity: float,
    tax_rate: float,
) -> float:
    """
    Hamada equation:
        β_unlevered = β_levered / [1 + (1 − t) × (D/E)]

    Reference: Part 12.
    """
    return levered_beta / (1.0 + (1.0 - tax_rate) * debt_to_equity)


def relever_beta(
    unlevered_beta: float,
    target_debt_to_equity: float,
    tax_rate: float,
) -> float:
    """
    Re-lever beta to the subject company's capital structure:
        β_levered = β_unlevered × [1 + (1 − t) × (D/E)]

    Reference: Part 12.
    """
    return unlevered_beta * (1.0 + (1.0 - tax_rate) * target_debt_to_equity)


def blended_beta(
    company_beta: float,
    industry_beta: float,
    industry_weight: float = 0.33,
) -> float:
    """
    Blend company-specific beta with industry beta (Damodaran proxy) to
    reduce noise in thinly-traded or volatile stocks.

    Default: 1/3 industry, 2/3 company.
    Reference: Part 12.
    """
    w = max(0.0, min(1.0, industry_weight))
    return (1.0 - w) * company_beta + w * industry_beta


# ─────────────────────────────────────────────────────────────────────────────
# Cost of Equity — CAPM  (Part 7, 33)
# ─────────────────────────────────────────────────────────────────────────────

def cost_of_equity_capm(
    risk_free_rate: float,
    beta: float,
    equity_risk_premium: float,
    size_premium: float = 0.0,
    country_risk_premium: float = 0.0,
) -> float:
    """
    CAPM cost of equity:
        Ke = Rf + β × ERP + size_premium + country_risk_premium

    size_premium: additional return demanded for small-cap stocks (Duff & Phelps)
    country_risk_premium: for ADRs / foreign-domiciled companies

    Reference: Parts 7, 33.
    """
    return risk_free_rate + beta * equity_risk_premium + size_premium + country_risk_premium


def size_premium_for_market_cap(market_cap_usd_mm: float) -> float:
    """Return the Kroll/Duff & Phelps style size premium for market cap."""
    from auto_valuation.data.macro import compute_size_premium

    return compute_size_premium(market_cap_usd_mm)


def country_risk_premium_for_country(
    country_iso2: str | None = None,
    exchange_country: str | None = None,
) -> float:
    """Return the Damodaran-style country risk premium for a country."""
    from auto_valuation.data.macro import compute_crp

    return compute_crp(country_iso2=country_iso2, exchange_country=exchange_country)


def country_adjusted_erp(
    base_equity_risk_premium: float,
    country_risk_premium: float = 0.0,
) -> float:
    """Total ERP = mature-market ERP + country risk premium."""
    return max(0.0, float(base_equity_risk_premium or 0.0)) + max(0.0, float(country_risk_premium or 0.0))


def compute_pre_tax_cost_of_debt(
    interest_expense: float | None,
    total_debt: float | None,
    risk_free_rate: float,
    credit_spread: float = 0.015,
    min_cost: float = 0.02,
    max_cost: float = 0.12,
) -> float:
    """
    Estimate the pre-tax cost of debt from reported interest expense.

    Falls back to risk-free rate plus a conservative credit spread when debt
    or interest expense is unavailable. All rates are decimals.
    """
    debt = abs(float(total_debt or 0.0))
    interest = abs(float(interest_expense or 0.0))
    if debt > 0 and interest > 0:
        observed = interest / debt
    else:
        observed = float(risk_free_rate or 0.0) + float(credit_spread or 0.0)
    return max(min_cost, min(max_cost, observed))


def cost_of_equity_dividend_growth(
    dps_next: float,
    current_price: float,
    growth_rate: float,
) -> float:
    """
    Dividend Capitalization Model (Dividend Growth Model) cost of equity:

        Re = D1 / P0 + g

    This is the second standard method for estimating the cost of equity
    (after CAPM), applicable to dividend-paying companies with predictable
    payout growth.

    Limitations vs CAPM:
    - Only valid for companies that pay dividends
    - Assumes dividends grow at a constant rate in perpetuity
    - Does not account for investment risk (no beta)

    Args:
        dps_next      : Expected dividend per share next period (D1, $).
        current_price : Current share price (P0, $). Must be > 0.
        growth_rate   : Constant dividend / earnings growth rate (decimal, e.g. 0.03).

    Returns:
        float: Cost of equity (decimal, e.g. 0.12 = 12%).

    Raises:
        ValueError: if current_price ≤ 0.

    Reference: CFI "Cost of Equity" guide; Re = D1/P0 + g.
    """
    if current_price <= 0:
        raise ValueError(
            f"current_price must be positive for Dividend Growth Model "
            f"(got {current_price})."
        )
    return dps_next / current_price + growth_rate


# ─────────────────────────────────────────────────────────────────────────────
# Capital structure weights  (Part 19)
# ─────────────────────────────────────────────────────────────────────────────

def compute_capital_structure(
    market_cap: float,
    total_debt: float,
    preferred_stock: float = 0.0,
) -> dict[str, float]:
    """
    Compute market-value weights for WACC:
        E% = market_cap / (market_cap + debt + preferred)
        D% = debt / (...)
        P% = preferred / (...)

    Reference: Part 19.
    """
    total = market_cap + total_debt + preferred_stock
    if total <= 0:
        return {"equity_weight": 1.0, "debt_weight": 0.0, "preferred_weight": 0.0, "total_cap": 0.0}
    return {
        "equity_weight":    market_cap      / total,
        "debt_weight":      total_debt      / total,
        "preferred_weight": preferred_stock / total,
        "total_cap":        total,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WACC  (Part 7, 33)
# ─────────────────────────────────────────────────────────────────────────────

def compute_wacc(
    equity_weight: float,
    cost_of_equity: float,
    debt_weight: float,
    pre_tax_cost_of_debt: float,
    tax_rate: float,
    preferred_weight: float = 0.0,
    cost_of_preferred: float = 0.0,
) -> float:
    """
    WACC = E% × Ke + D% × Kd × (1 − t) + P% × Kp

    All weights should sum to 1.0.
    Reference: Part 7.
    """
    after_tax_kd = pre_tax_cost_of_debt * (1.0 - tax_rate)
    return (
        equity_weight    * cost_of_equity
        + debt_weight    * after_tax_kd
        + preferred_weight * cost_of_preferred
    )


def build_wacc(
    market_cap: float,
    total_debt: float,
    preferred_stock: float,
    basic_shares_mm: float,
    current_price: float,
    risk_free_rate: float,
    equity_risk_premium: float,
    beta: float,
    pre_tax_cost_of_debt: float,
    tax_rate: float,
    size_premium: float = 0.0,
    country_risk_premium: float = 0.0,
    cost_of_preferred: float = 0.06,
) -> dict[str, float]:
    """
    End-to-end WACC calculation.

    Returns a dict containing all intermediate and final values:
      equity_weight, debt_weight, preferred_weight,
      cost_of_equity, after_tax_cost_of_debt, cost_of_preferred,
      wacc, market_cap, total_cap

    Reference: Part 7.
    """
    cap_struct = compute_capital_structure(market_cap, total_debt, preferred_stock)
    ke = cost_of_equity_capm(risk_free_rate, beta, equity_risk_premium, size_premium, country_risk_premium)
    kd_after = pre_tax_cost_of_debt * (1.0 - tax_rate)
    kp = cost_of_preferred

    wacc = compute_wacc(
        cap_struct["equity_weight"],
        ke,
        cap_struct["debt_weight"],
        pre_tax_cost_of_debt,
        tax_rate,
        cap_struct["preferred_weight"],
        kp,
    )

    return {
        "wacc":                   wacc,
        "cost_of_equity":         ke,
        "after_tax_cost_of_debt": kd_after,
        "cost_of_preferred":      kp,
        "equity_weight":          cap_struct["equity_weight"],
        "debt_weight":            cap_struct["debt_weight"],
        "preferred_weight":       cap_struct["preferred_weight"],
        "total_cap":              cap_struct["total_cap"],
        "market_cap":             market_cap,
        "pre_tax_cost_of_debt":   pre_tax_cost_of_debt,
        "beta":                   beta,
        "risk_free_rate":         risk_free_rate,
        "equity_risk_premium":    equity_risk_premium,
        "size_premium":           size_premium,
        "country_risk_premium":   country_risk_premium,
        "tax_rate":               tax_rate,
    }


# ─────────────────────────────────────────────────────────────────────────────
# IFRS 16 / ASC 842 three-component WACC  (Part 75)
# ─────────────────────────────────────────────────────────────────────────────

def compute_wacc_with_leases(
    ke: float,
    kd_after_tax: float,
    k_lease: float,
    equity_mv_m: float,
    debt_mv_m: float,
    lease_liability_m: float,
    tax_rate: float,
    lease_tax_deductible: bool = True,
) -> tuple[float, dict]:
    """
    Three-component WACC including IFRS 16 / ASC 842 lease liability:

        WACC = ke × (E/V) + kd_at × (D/V) + k_lease_at × (L/V)

    where V = E + D + L.

    Args:
        ke:                 Cost of equity (decimal).
        kd_after_tax:       After-tax cost of financial debt (decimal).
        k_lease:            Implicit / incremental borrowing rate on leases (decimal).
                            Use the company's disclosed rate, or kd_pretax as proxy.
        equity_mv_m:        Market value of equity ($M).
        debt_mv_m:          Market value of financial IBD ($M).
        lease_liability_m:  IFRS 16 lease liability on balance sheet ($M).
        tax_rate:           Effective tax rate (decimal).
        lease_tax_deductible: True if lease interest creates a tax shield (most jurisdictions).

    Returns:
        (wacc, weights_dict)

    Materiality trigger: use this function when lease_liability_m ≥ 5 % of
    total capital (E + D + L).  Otherwise, standard 2-component WACC is adequate.

    Reference: Architecture Plan Part 75.
    """
    E = float(equity_mv_m)
    D = float(debt_mv_m)
    L = float(lease_liability_m)
    V = E + D + L

    if V <= 0:
        raise ValueError("Total capital (E + D + L) must be positive for 3-component WACC.")

    k_lease_at = k_lease * (1.0 - tax_rate) if lease_tax_deductible else k_lease

    wacc = (
        ke           * (E / V)
        + kd_after_tax * (D / V)
        + k_lease_at   * (L / V)
    )

    weights: dict = {
        "E_pct":      E / V,
        "D_pct":      D / V,
        "L_pct":      L / V,
        "ke":         ke,
        "kd_at":      kd_after_tax,
        "k_lease_at": k_lease_at,
        "k_lease":    k_lease,
        "total_capital_m": V,
    }

    return wacc, weights


# ─────────────────────────────────────────────────────────────────────────────
# Blume beta adjustment  (Part 7 / Macabacus WACC)
# ─────────────────────────────────────────────────────────────────────────────

def compute_predicted_beta_blume(raw_beta: float) -> float:
    """
    Blume (1975) mean-reversion adjustment:
        β_adj = 0.67 × β_raw + 0.33 × 1.0

    All betas revert toward the market mean (1.0) over time.
    High-beta stocks tend to decline; low-beta stocks tend to rise.

    This is the standard Macabacus / Kroll adjustment applied BEFORE
    levering / unlevering.

    Args:
        raw_beta: Historical OLS beta from market regression.

    Returns:
        Adjusted (predicted) beta.

    Reference: Blume (1975), Macabacus WACC reference.
    """
    return 0.67 * raw_beta + 0.33 * 1.0


# ─────────────────────────────────────────────────────────────────────────────
# 4-component WACC with preferred stock  (Part 75 / Macabacus)
# ─────────────────────────────────────────────────────────────────────────────

def compute_wacc_with_preferred(
    ke: float,
    kd_after_tax: float,
    k_preferred: float,
    k_lease_at: float,
    equity_mv_m: float,
    debt_mv_m: float,
    preferred_mv_m: float,
    lease_liability_m: float,
) -> tuple[float, dict]:
    """
    Four-component WACC including equity, debt, preferred stock, and leases:

        WACC = ke×(E/V) + kd_at×(D/V) + kp×(P/V) + k_lease_at×(L/V)

    where V = E + D + P + L.

    Preferred stock dividends are not tax-deductible in most jurisdictions
    (unlike debt interest), so k_preferred is already an after-tax rate.

    Args:
        ke:              Cost of equity (decimal).
        kd_after_tax:    After-tax cost of financial debt (decimal).
        k_preferred:     Cost of preferred stock — dividend / liquidation value (decimal).
        k_lease_at:      After-tax implicit rate on operating lease liabilities (decimal).
        equity_mv_m:     Market value of common equity ($M).
        debt_mv_m:       Market value of interest-bearing debt ($M).
        preferred_mv_m:  Liquidation / market value of preferred stock ($M).
        lease_liability_m: IFRS 16 / ASC 842 operating lease liability ($M).

    Returns:
        (wacc, weights_dict)

    Reference: Architecture Plan Parts 75, Macabacus WACC guide.
    """
    E = float(equity_mv_m)
    D = float(debt_mv_m)
    P = float(preferred_mv_m)
    L = float(lease_liability_m)
    V = E + D + P + L

    if V <= 0:
        raise ValueError("Total capital (E + D + P + L) must be positive for 4-component WACC.")

    wacc = (
        ke           * (E / V)
        + kd_after_tax * (D / V)
        + k_preferred  * (P / V)
        + k_lease_at   * (L / V)
    )

    weights: dict = {
        "E_pct": E / V,
        "D_pct": D / V,
        "P_pct": P / V,
        "L_pct": L / V,
        "ke":         ke,
        "kd_at":      kd_after_tax,
        "k_preferred": k_preferred,
        "k_lease_at": k_lease_at,
        "total_capital_m": V,
    }

    return wacc, weights


# ─────────────────────────────────────────────────────────────────────────────
# Cross-currency WACC validation  (Part 46.3)
# ─────────────────────────────────────────────────────────────────────────────

def validate_wacc_currency_consistency(
    rf_currency: str,
    erp_currency: str,
    company_currency: str,
) -> None:
    """
    Ensure the risk-free rate, ERP, and company reporting currency all match.

    A common error: using a USD risk-free rate (4.3%) for a EUR company (Adidas)
    when EUR Rf is ~2.5%.  This overstates Ke by ~150bp.

    Raises:
        ValueError: if any of the three currencies differ.

    Args:
        rf_currency:      ISO 3-letter currency of the risk-free rate used (e.g. "USD").
        erp_currency:     ISO 3-letter currency of the ERP used (e.g. "USD").
        company_currency: Company's functional / reporting currency (from FMP profile).

    Reference: Architecture Plan Part 46.3.
    """
    rf_c = rf_currency.upper()
    erp_c = erp_currency.upper()
    co_c = company_currency.upper()

    if not (rf_c == erp_c == co_c):
        raise ValueError(
            f"WACC currency mismatch: Rf currency='{rf_c}', "
            f"ERP currency='{erp_c}', company reports in '{co_c}'. "
            f"All three must match. Update rf_rate to use the {co_c} 10-year "
            f"government bond yield (see data/macro.py FRED_RF_SERIES)."
        )


def compute_cross_currency_wacc(
    ke: float,
    kd_after_tax: float,
    equity_weight: float,
    debt_weight: float,
    rf_currency: str,
    erp_currency: str,
    company_currency: str,
    country_risk_premium: float = 0.0,
) -> float:
    """
    WACC for non-USD companies, validated for currency consistency.

    Validates that Rf and ERP are denominated in the same currency as the
    company reports, then computes the standard two-component WACC.

    Args:
        ke:               Cost of equity (already computed in company's home currency).
        kd_after_tax:     After-tax cost of debt in company's home currency.
        equity_weight:    E / (E + D).
        debt_weight:      D / (E + D).
        rf_currency:      Currency of the Rf used to compute ke.
        erp_currency:     Currency of the ERP used to compute ke.
        company_currency: Company's functional / reporting currency.
        country_risk_premium: Additional CRP already included in ke (informational).

    Returns:
        float: WACC in the company's home currency.

    Reference: Architecture Plan Part 46.3.
    """
    validate_wacc_currency_consistency(rf_currency, erp_currency, company_currency)
    return equity_weight * ke + debt_weight * kd_after_tax


# ─────────────────────────────────────────────────────────────────────────────
# WACC mean reversion / step-down for distressed companies  (Part 48.1)
# ─────────────────────────────────────────────────────────────────────────────

def apply_wacc_step_down(
    base_wacc: float,
    target_long_run_wacc: float,
    forecast_years: int,
    transition_years: int = 3,
) -> list[float]:
    """
    OPTIONAL: Fades WACC from a high distressed level toward the long-run target.

    This is used ONLY for highly leveraged companies (D/TA > 60%) that have
    an explicit deleveraging path.  For all other companies, use a constant
    WACC equal to target_long_run_wacc across all forecast years.

    The standard system default: constant WACC = target_long_run_wacc.

    Args:
        base_wacc:             Starting WACC (distressed / current level).
        target_long_run_wacc:  Target steady-state WACC after deleveraging.
        forecast_years:        Number of forecast years (e.g. 7).
        transition_years:      Years over which WACC fades to long-run target (default 3).

    Returns:
        List of WACC values, one per forecast year (length = forecast_years).

    Reference: Architecture Plan Part 48.1.
    """
    waccs: list[float] = []
    for yr in range(1, forecast_years + 1):
        if yr <= transition_years:
            progress = yr / transition_years
            w = base_wacc + progress * (target_long_run_wacc - base_wacc)
        else:
            w = target_long_run_wacc
        waccs.append(w)
    return waccs


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → unlever_beta
compute_unlevered_beta = unlever_beta

#: Canonical checklist name → relever_beta
compute_relevered_beta = relever_beta

#: Canonical checklist name → cost_of_equity_capm
compute_cost_of_equity = cost_of_equity_capm

#: Canonical checklist name → compute_pre_tax_cost_of_debt
compute_cost_of_debt = compute_pre_tax_cost_of_debt

#: Canonical checklist name → apply_wacc_step_down
wacc_mean_reversion_schedule = apply_wacc_step_down
