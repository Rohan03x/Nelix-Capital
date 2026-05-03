"""
validation/checks.py — Data-layer and model validation checks.

Reference: Architecture Plan Parts 7, 24, 32, 40, 41, 46.1, 55.1, 61, 76.

All check functions return a list of ValidationResult objects.
A ValidationResult has: name, status ("PASS" / "WARN" / "FAIL"), value, threshold, message.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import Any

from auto_valuation.utils.error import DataQualityError, ValuationWarning


@dataclass
class ValidationResult:
    name:      str
    status:    str          # "PASS" | "WARN" | "FAIL"
    value:     Any = None
    threshold: Any = None
    message:   str = ""

    def is_ok(self) -> bool:
        return self.status in ("PASS", "WARN")


# ─────────────────────────────────────────────────────────────────────────────
# Data-layer validation  (Part 61)
# ─────────────────────────────────────────────────────────────────────────────

_CRITICAL_IS_FIELDS = ["revenue", "ebit", "net_income", "da"]
_CRITICAL_BS_FIELDS = ["total_assets", "total_equity", "total_liabilities"]
_CRITICAL_CF_FIELDS = ["cfo", "capex"]


def validate_fmp_data(
    income_stmts: list[dict],
    balance_sheets: list[dict],
    cash_flows: list[dict],
) -> list[ValidationResult]:
    """
    Check all critical FMP fields are non-None for at least 3 years.
    Returns a list of ValidationResults (one per critical field group).
    Reference: Part 61.1.
    """
    results: list[ValidationResult] = []

    def _check_fields(stmts: list[dict], fields: list[str], label: str) -> None:
        for field in fields:
            populated = sum(
                1 for s in stmts[:5]
                if s.get(field) is not None
            )
            if populated == 0:
                results.append(ValidationResult(
                    name=f"FMP_{label}_{field}",
                    status="FAIL",
                    value=0,
                    threshold=3,
                    message=f"Field '{field}' is None in all available {label} records. Cannot model.",
                ))
            elif populated < 3:
                results.append(ValidationResult(
                    name=f"FMP_{label}_{field}",
                    status="WARN",
                    value=populated,
                    threshold=3,
                    message=f"Field '{field}' only available in {populated} of 5 {label} years.",
                ))
            else:
                results.append(ValidationResult(
                    name=f"FMP_{label}_{field}",
                    status="PASS",
                    value=populated,
                    threshold=3,
                ))

    _check_fields(income_stmts,  _CRITICAL_IS_FIELDS, "IS")
    _check_fields(balance_sheets, _CRITICAL_BS_FIELDS, "BS")
    _check_fields(cash_flows,     _CRITICAL_CF_FIELDS, "CF")

    return results


def check_revenue_sanity(income_stmts: list[dict]) -> list[ValidationResult]:
    """
    Halt if revenue is negative. Warn if YoY growth > 200%.
    Reference: Part 61.2.
    """
    results: list[ValidationResult] = []
    if not income_stmts:
        results.append(ValidationResult(
            name="REVENUE_PRESENT", status="FAIL",
            message="No income statement data available.",
        ))
        return results

    most_recent_rev = income_stmts[0].get("revenue") or 0
    if most_recent_rev < 0:
        results.append(ValidationResult(
            name="REVENUE_POSITIVE", status="FAIL",
            value=most_recent_rev, threshold=0,
            message=f"Revenue is negative ({most_recent_rev:,.0f}M). Cannot model.",
        ))
        return results

    results.append(ValidationResult(
        name="REVENUE_POSITIVE", status="PASS", value=most_recent_rev,
    ))

    # YoY growth check
    for i in range(min(3, len(income_stmts) - 1)):
        curr_rev = income_stmts[i].get("revenue") or 0
        prev_rev = income_stmts[i + 1].get("revenue") or 0
        if prev_rev and prev_rev > 0:
            growth = (curr_rev - prev_rev) / prev_rev
            if growth > 2.0:
                year = income_stmts[i].get("calendarYear", "")
                results.append(ValidationResult(
                    name=f"REVENUE_GROWTH_SANITY_{year}",
                    status="WARN",
                    value=growth,
                    threshold=2.0,
                    message=(
                        f"Revenue growth of {growth:.0%} in {year} exceeds 200%. "
                        "Possible acquisition year — check for M&A distortion."
                    ),
                ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Model-layer validation  (Parts 7, 24, 32, 40, 41)
# ─────────────────────────────────────────────────────────────────────────────

def check_wacc_range(
    wacc: float,
    warn_low:  float = 0.06,
    warn_high: float = 0.15,
    hard_min:  float = 0.03,
    hard_max:  float = 0.30,
) -> ValidationResult:
    """Reference: Parts 7, 33.1."""
    if wacc < hard_min or wacc > hard_max:
        return ValidationResult(
            name="WACC_RANGE", status="FAIL",
            value=wacc, threshold=(hard_min, hard_max),
            message=f"WACC {wacc:.2%} is outside hard bounds [{hard_min:.0%}–{hard_max:.0%}].",
        )
    if wacc < warn_low or wacc > warn_high:
        return ValidationResult(
            name="WACC_RANGE", status="WARN",
            value=wacc, threshold=(warn_low, warn_high),
            message=f"WACC {wacc:.2%} is outside typical range [{warn_low:.0%}–{warn_high:.0%}]. Review inputs.",
        )
    return ValidationResult(name="WACC_RANGE", status="PASS", value=wacc)


def check_wacc_terminal_growth_spread(
    wacc: float,
    terminal_growth: float,
    min_spread: float = 0.005,
) -> ValidationResult:
    """
    Enforce a minimum spread between WACC and terminal growth rate.

    As WACC approaches terminal_growth, the Gordon Growth Model TV denominator
    (WACC − g) shrinks toward zero and the terminal value explodes to infinity,
    making the model extremely sensitive to small changes in either assumption.

    Standard IB practice: maintain at least 50 basis points (0.50%) spread.
    Many desks enforce 100–200bp.

    Args:
        wacc          : WACC (decimal).
        terminal_growth: Perpetuity growth rate (decimal).
        min_spread    : Minimum required spread (default 0.005 = 50bp).

    Returns:
        ValidationResult with FAIL if spread < 0 (GGM breaks), WARN if spread
        is less than min_spread, PASS otherwise.

    Reference: Architecture Plan Part 3.3; IB best practice.
    """
    spread = wacc - terminal_growth
    if spread <= 0:
        return ValidationResult(
            name="WACC_TG_SPREAD", status="FAIL",
            value=spread, threshold=min_spread,
            message=(
                f"WACC ({wacc:.2%}) ≤ terminal growth ({terminal_growth:.2%}). "
                "Gordon Growth Model denominator is zero or negative — model is invalid."
            ),
        )
    if spread < min_spread:
        return ValidationResult(
            name="WACC_TG_SPREAD", status="WARN",
            value=spread, threshold=min_spread,
            message=(
                f"WACC–g spread is {spread:.2%} (< {min_spread:.2%} minimum). "
                "Terminal value is highly sensitive to assumption changes. "
                "Consider widening the spread to at least 50bp."
            ),
        )
    return ValidationResult(name="WACC_TG_SPREAD", status="PASS", value=spread)


def check_terminal_growth_ceiling(
    terminal_growth: float,
    gdp_growth_ceiling: float = 0.04,
) -> ValidationResult:
    """Reference: Parts 3.3, 41.1."""
    if terminal_growth >= gdp_growth_ceiling:
        return ValidationResult(
            name="TERMINAL_GROWTH_CEILING", status="FAIL",
            value=terminal_growth, threshold=gdp_growth_ceiling,
            message=(
                f"Terminal growth {terminal_growth:.2%} ≥ nominal GDP ceiling "
                f"{gdp_growth_ceiling:.2%}. Implies perpetual outperformance of the economy."
            ),
        )
    if terminal_growth < 0.005:
        return ValidationResult(
            name="TERMINAL_GROWTH_CEILING", status="WARN",
            value=terminal_growth, threshold=0.005,
            message=f"Terminal growth {terminal_growth:.2%} is very low. Consider 2–3% for US companies.",
        )
    return ValidationResult(name="TERMINAL_GROWTH_CEILING", status="PASS", value=terminal_growth)


def check_tv_pct_of_ev(
    pv_tv: float,
    total_ev: float,
    warn_threshold: float = 0.80,
) -> ValidationResult:
    """Reference: Part 41.1."""
    if total_ev <= 0:
        return ValidationResult(
            name="TV_PCT_EV", status="FAIL",
            message="Total EV is zero or negative — cannot compute TV%.",
        )
    pct = pv_tv / total_ev
    if pct > warn_threshold:
        return ValidationResult(
            name="TV_PCT_EV", status="WARN",
            value=pct, threshold=warn_threshold,
            message=(
                f"Terminal value is {pct:.0%} of total EV "
                f"(> {warn_threshold:.0%} threshold). "
                "Model is highly sensitive to terminal assumptions."
            ),
        )
    return ValidationResult(name="TV_PCT_EV", status="PASS", value=pct)


def check_terminal_roic_vs_wacc(
    terminal_roic: float,
    wacc: float,
) -> ValidationResult:
    """
    Terminal ROIC should exceed WACC — otherwise the company is value-destroying
    in perpetuity (NPV of growth < 0).
    Reference: Part 32.1.
    """
    if terminal_roic < wacc:
        return ValidationResult(
            name="TERMINAL_ROIC_VS_WACC", status="WARN",
            value=terminal_roic, threshold=wacc,
            message=(
                f"Terminal ROIC {terminal_roic:.2%} < WACC {wacc:.2%}. "
                "Perpetual growth creates negative NPV — reduce terminal growth or increase ROIC."
            ),
        )
    return ValidationResult(name="TERMINAL_ROIC_VS_WACC", status="PASS", value=terminal_roic)


def check_balance_sheet_closes(
    total_assets: float,
    total_liabilities: float,
    total_equity: float,
    year: str = "",
    tolerance_mm: float = 1.0,
) -> ValidationResult:
    """
    Assets = Liabilities + Equity within tolerance.
    Reference: Part 76.
    """
    diff = abs(total_assets - (total_liabilities + total_equity))
    label = f"BS_CLOSES_{year}" if year else "BS_CLOSES"
    if diff > tolerance_mm:
        return ValidationResult(
            name=label, status="FAIL",
            value=diff, threshold=tolerance_mm,
            message=f"Balance sheet out of balance by ${diff:,.1f}M in year {year}.",
        )
    return ValidationResult(name=label, status="PASS", value=diff)


def check_negative_ev(enterprise_value: float) -> ValidationResult:
    """Reference: Part 7."""
    if enterprise_value < 0:
        return ValidationResult(
            name="NEGATIVE_EV", status="WARN",
            value=enterprise_value,
            message=(
                f"Implied EV is negative (${enterprise_value:,.0f}M). "
                "This may mean the company holds more net cash than its operating value. "
                "Equity value = net cash position."
            ),
        )
    return ValidationResult(name="NEGATIVE_EV", status="PASS", value=enterprise_value)


def check_capex_vs_da(capex: float, da: float, year: str = "") -> ValidationResult:
    """
    Flag if CapEx < 50% of D&A (possible under-investment) or > 5× D&A (anomaly).
    Reference: Phase 8 checklist.
    """
    if da <= 0:
        return ValidationResult(name=f"CAPEX_VS_DA_{year}", status="PASS")
    ratio = capex / da
    label = f"CAPEX_VS_DA_{year}" if year else "CAPEX_VS_DA"
    if ratio < 0.50:
        return ValidationResult(
            name=label, status="WARN", value=ratio, threshold=0.50,
            message=f"CapEx is {ratio:.1f}× D&A in {year} — potential under-investment.",
        )
    if ratio > 5.0:
        return ValidationResult(
            name=label, status="WARN", value=ratio, threshold=5.0,
            message=f"CapEx is {ratio:.1f}× D&A in {year} — unusually high; check for anomaly.",
        )
    return ValidationResult(name=label, status="PASS", value=ratio)


def check_nowc_sign(nowc: float) -> ValidationResult:
    """
    Negative NWC is VALID for Amazon/Costco/retailer pattern.
    Do NOT flag as error — only note it.
    Reference: Part 40.1.
    """
    if nowc < 0:
        return ValidationResult(
            name="NOWC_NEGATIVE", status="PASS",
            value=nowc,
            message=f"NOWC is negative (${nowc:,.0f}M) — valid for float-funded business models.",
        )
    return ValidationResult(name="NOWC_NEGATIVE", status="PASS", value=nowc)


def check_net_debt_sign(
    net_debt: float,
    cash: float | None = None,
) -> ValidationResult:
    """
    Flag unusually large net-cash positions that could distort equity value.
    Net debt = total_debt - cash.  Negative net_debt = net cash position.
    Reference: Part 55.
    """
    if net_debt < -5_000:
        return ValidationResult(
            name="NET_DEBT_SIGN", status="WARN",
            value=net_debt,
            message=(
                f"Net debt is ${net_debt:,.0f}M (large net cash). "
                "Verify cash is unrestricted and correctly excluded from enterprise value."
            ),
        )
    if net_debt < 0:
        return ValidationResult(
            name="NET_DEBT_SIGN", status="PASS",
            value=net_debt,
            message=f"Net cash position of ${abs(net_debt):,.0f}M — adds to equity value.",
        )
    return ValidationResult(name="NET_DEBT_SIGN", status="PASS", value=net_debt)


def check_sbc_terminal_dilution(
    sbc_mm: float,
    revenue_mm: float,
    warn_threshold: float = 0.05,
) -> ValidationResult:
    """
    If SBC exceeds warn_threshold (5%) of revenue, terminal value is understated
    because free cash flow ignores the dilution cost.
    Reference: Part 44.
    """
    if revenue_mm <= 0:
        return ValidationResult(
            name="SBC_DILUTION", status="PASS",
            message="Revenue is zero — skipping SBC check.",
        )
    ratio = sbc_mm / revenue_mm
    if ratio >= warn_threshold:
        return ValidationResult(
            name="SBC_DILUTION", status="WARN",
            value=ratio, threshold=warn_threshold,
            message=(
                f"SBC is {ratio:.1%} of revenue (> {warn_threshold:.0%} threshold). "
                "Terminal FCF may overstate true shareholder returns — consider SBC as a cash cost."
            ),
        )
    return ValidationResult(name="SBC_DILUTION", status="PASS", value=ratio)


def check_revenue_growth_vs_margins(
    income_stmts: list[dict],
) -> list[ValidationResult]:
    """
    Detect the pattern of declining revenue growth coinciding with rising EBIT margins
    across consecutive years — a classic revenue-management signal.
    Reference: Part 43.
    """
    results: list[ValidationResult] = []
    if len(income_stmts) < 3:
        return results

    flags = 0
    for i in range(min(3, len(income_stmts) - 1)):
        curr = income_stmts[i]
        prev = income_stmts[i + 1]
        c_rev  = curr.get("revenue") or 0
        p_rev  = prev.get("revenue") or 0
        c_ebit = curr.get("ebit") or 0
        p_ebit = prev.get("ebit") or 0

        if p_rev <= 0:
            continue

        growth      = (c_rev - p_rev) / p_rev
        curr_margin = c_ebit / c_rev if c_rev > 0 else 0
        prev_margin = p_ebit / p_rev if p_rev > 0 else 0

        if growth < 0 and curr_margin > prev_margin:
            flags += 1

    if flags >= 2:
        results.append(ValidationResult(
            name="REVENUE_MGMT_SIGNAL", status="WARN",
            value=flags,
            message=(
                f"Revenue declined while EBIT margins expanded in {flags} consecutive years. "
                "Possible revenue management / cost-cutting masking top-line deterioration."
            ),
        ))
    return results


def check_nci_materiality(
    minority_interest_mm: float,
    total_equity_mm: float,
    warn_threshold: float = 0.05,
) -> ValidationResult:
    """
    Minority interest (NCI) should be deducted from EV to get common equity value
    if it represents more than warn_threshold of total equity.
    Reference: Part 69.
    """
    if total_equity_mm <= 0:
        return ValidationResult(
            name="NCI_MATERIALITY", status="PASS",
            message="Total equity is non-positive — skipping NCI check.",
        )
    ratio = abs(minority_interest_mm) / total_equity_mm
    if ratio >= warn_threshold:
        return ValidationResult(
            name="NCI_MATERIALITY", status="WARN",
            value=ratio, threshold=warn_threshold,
            message=(
                f"Minority interest is {ratio:.1%} of total equity "
                f"(> {warn_threshold:.0%} threshold). "
                "Deduct NCI from enterprise value when computing equity value."
            ),
        )
    return ValidationResult(name="NCI_MATERIALITY", status="PASS", value=ratio)


def check_pension_materiality(
    pension_obligation_mm: float,
    total_assets_mm: float,
    warn_threshold: float = 0.05,
) -> ValidationResult:
    """
    Unfunded pension / OPEB obligations should be treated as debt if material.
    Reference: Part 70.
    """
    if total_assets_mm <= 0:
        return ValidationResult(
            name="PENSION_MATERIALITY", status="PASS",
            message="Total assets is non-positive — skipping pension check.",
        )
    ratio = abs(pension_obligation_mm) / total_assets_mm
    if ratio >= warn_threshold:
        return ValidationResult(
            name="PENSION_MATERIALITY", status="WARN",
            value=ratio, threshold=warn_threshold,
            message=(
                f"Pension/OPEB obligation is {ratio:.1%} of total assets "
                f"(> {warn_threshold:.0%} threshold). "
                "Add to net debt in bridge calculation."
            ),
        )
    return ValidationResult(name="PENSION_MATERIALITY", status="PASS", value=ratio)


def check_lease_wacc_materiality(
    finance_leases_mm: float,
    total_debt_mm: float,
    warn_threshold: float = 0.10,
) -> ValidationResult:
    """
    If finance lease obligations exceed warn_threshold of total debt (10% default),
    warn that lease obligations should be included in the debt portion of WACC.
    Operating leases are capitalised under IFRS 16 / ASC 842 but are not
    always included in FMP's total_debt figure — verify the bridge.
    Reference: Architecture Plan Part 70.1.
    """
    if total_debt_mm <= 0:
        return ValidationResult(
            name="LEASE_WACC_MATERIALITY", status="PASS",
            message="Total debt is non-positive — skipping lease WACC check.",
        )
    ratio = abs(finance_leases_mm) / total_debt_mm
    if ratio >= warn_threshold:
        return ValidationResult(
            name="LEASE_WACC_MATERIALITY", status="WARN",
            value=ratio, threshold=warn_threshold,
            message=(
                f"Finance leases are {ratio:.1%} of total debt "
                f"(> {warn_threshold:.0%} threshold). "
                "Ensure lease obligations are included in the WACC debt weight "
                "and deducted from enterprise value in the equity bridge."
            ),
        )
    return ValidationResult(name="LEASE_WACC_MATERIALITY", status="PASS", value=ratio)


def validate_reinvestment_consistency(
    reinvestment_rate: float,
    roic: float,
    growth_rate: float,
    tolerance: float = 0.01,
) -> ValidationResult:
    """
    Fundamental valuation identity: g = ROIC × reinvestment_rate.
    If the implied growth rate from ROIC × RR diverges from modelled growth by
    more than tolerance (1%), warn of potential internal inconsistency.
    Reference: Architecture Plan Part 32.1.
    """
    implied_g = roic * reinvestment_rate
    delta = abs(implied_g - growth_rate)
    if delta > tolerance:
        return ValidationResult(
            name="REINVESTMENT_CONSISTENCY", status="WARN",
            value=delta, threshold=tolerance,
            message=(
                f"ROIC ({roic:.2%}) × reinvestment rate ({reinvestment_rate:.2%}) = "
                f"implied growth {implied_g:.2%}, but modelled growth = {growth_rate:.2%} "
                f"(gap {delta:.2%} > {tolerance:.0%} tolerance). "
                "Adjust reinvestment rate or terminal growth to maintain internal consistency."
            ),
        )
    return ValidationResult(name="REINVESTMENT_CONSISTENCY", status="PASS", value=delta)


def check_restatement_detection(
    income_stmts: list[dict],
    revenue_jump_threshold: float = 0.30,
) -> list[ValidationResult]:
    """
    Detect possible restatement by flagging large unexplained step-changes in reported
    revenue between adjacent years that exceed the threshold.
    Reference: Part 24.
    """
    results: list[ValidationResult] = []
    for i in range(min(4, len(income_stmts) - 1)):
        curr = income_stmts[i]
        prev = income_stmts[i + 1]
        c_rev = curr.get("revenue") or 0
        p_rev = prev.get("revenue") or 0
        year  = curr.get("calendarYear", str(i))

        if p_rev <= 0:
            continue

        change = abs(c_rev - p_rev) / p_rev
        if change > revenue_jump_threshold:
            results.append(ValidationResult(
                name=f"RESTATEMENT_SIGNAL_{year}",
                status="WARN",
                value=change,
                threshold=revenue_jump_threshold,
                message=(
                    f"Revenue changed by {change:.0%} in {year} "
                    f"(> {revenue_jump_threshold:.0%} threshold). "
                    "Possible restatement, acquisition, or divestiture — verify comparability."
                ),
            ))
    return results


def check_price_freshness(
    price_date: str,
    today: str | None = None,
    stale_days: int = 5,
) -> ValidationResult:
    """
    Warn if the market price used for WACC/equity-value is more than stale_days old.
    price_date: ISO-8601 string (YYYY-MM-DD).
    Reference: Part 76.
    """
    import datetime

    try:
        pd_dt = datetime.date.fromisoformat(price_date)
    except (ValueError, TypeError):
        return ValidationResult(
            name="PRICE_FRESHNESS", status="WARN",
            message=f"Cannot parse price_date '{price_date}'. Verify market data is current.",
        )

    if today is None:
        ref = datetime.date.today()
    else:
        try:
            ref = datetime.date.fromisoformat(today)
        except ValueError:
            ref = datetime.date.today()

    delta = (ref - pd_dt).days
    if delta < 0:
        return ValidationResult(
            name="PRICE_FRESHNESS", status="WARN",
            value=delta,
            message=f"Price date '{price_date}' is in the future. Check data source.",
        )
    if delta > stale_days:
        return ValidationResult(
            name="PRICE_FRESHNESS", status="WARN",
            value=delta, threshold=stale_days,
            message=(
                f"Market price is {delta} calendar days old (> {stale_days}-day threshold). "
                "Re-fetch price before finalising WACC and equity value."
            ),
        )
    return ValidationResult(name="PRICE_FRESHNESS", status="PASS", value=delta)


# ─────────────────────────────────────────────────────────────────────────────
# NOWC sign check  (Part 40.1)
# ─────────────────────────────────────────────────────────────────────────────

def check_nowc_sign(
    historical_nowc: list[float],
    historical_revenue: list[float],
    warn_low: float = -0.30,
    warn_high: float = 0.20,
) -> ValidationResult:
    """
    Negative NOWC is VALID for AP-heavy retailers and subscription businesses
    (Amazon, Costco pattern). Only flag if the magnitude is outside the
    normal range of −30% to +20% of revenue.

    Returns PASS for the normal range; WARN for extreme values.
    Reference: Architecture Plan Part 40.1.
    """
    if not historical_nowc or not historical_revenue:
        return ValidationResult(
            name="NOWC_SIGN", status="PASS",
            message="No NOWC data to check.",
        )

    last_nowc    = historical_nowc[-1]
    last_revenue = historical_revenue[-1]

    if last_revenue <= 0:
        return ValidationResult(
            name="NOWC_SIGN", status="PASS",
            message="Revenue is non-positive — skipping NOWC check.",
        )

    nowc_pct = last_nowc / last_revenue

    if warn_low <= nowc_pct <= warn_high:
        return ValidationResult(
            name="NOWC_SIGN", status="PASS",
            value=nowc_pct,
            message=f"NOWC = {nowc_pct:.1%} of revenue. Normal operating range.",
        )
    elif nowc_pct < warn_low:
        return ValidationResult(
            name="NOWC_SIGN", status="WARN",
            value=nowc_pct, threshold=warn_low,
            message=(
                f"NOWC = {nowc_pct:.1%} of revenue (< {warn_low:.0%}). "
                "Unusually large negative WC. Verify AP includes supplier financing "
                "or securitised payables — this may inflate modelled UFCF."
            ),
        )
    else:
        return ValidationResult(
            name="NOWC_SIGN", status="WARN",
            value=nowc_pct, threshold=warn_high,
            message=(
                f"NOWC = {nowc_pct:.1%} of revenue (> {warn_high:.0%}). "
                "High WC intensity — verify AR and inventory are not unusually bloated."
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# D&A / CapEx ratio check  (Part 40.2)
# ─────────────────────────────────────────────────────────────────────────────

def check_da_capex_ratio(
    da: float,
    capex: float,
    warn_threshold: float = 3.0,
) -> ValidationResult:
    """
    D&A should not far exceed CapEx for a going-concern company. A D&A/CapEx
    ratio > 3× may indicate under-investment in maintaining the asset base.
    Reference: Architecture Plan Part 40.2.
    """
    if capex <= 0:
        return ValidationResult(
            name="DA_CAPEX_RATIO", status="PASS",
            message="CapEx is zero — skipping D&A/CapEx ratio check.",
        )
    ratio = da / capex
    if ratio > warn_threshold:
        return ValidationResult(
            name="DA_CAPEX_RATIO", status="WARN",
            value=ratio, threshold=warn_threshold,
            message=(
                f"D&A/CapEx = {ratio:.1f}× (> {warn_threshold:.0f}× threshold). "
                "Possible under-investment in asset base or end-of-life asset depreciation."
            ),
        )
    return ValidationResult(
        name="DA_CAPEX_RATIO", status="PASS",
        value=ratio,
    )


# ─────────────────────────────────────────────────────────────────────────────
# run_all_data_checks
# ─────────────────────────────────────────────────────────────────────────────

def run_all_data_checks(
    income_stmts: list[dict],
    balance_sheets: list[dict],
    cash_flows: list[dict],
) -> list[ValidationResult]:
    """
    Run all data-layer checks and return a combined list.
    Raises DataQualityError if any FAIL-status check is critical.
    """
    results: list[ValidationResult] = []
    results.extend(validate_fmp_data(income_stmts, balance_sheets, cash_flows))
    results.extend(check_revenue_sanity(income_stmts))

    # Halt on hard FAILs
    fatal = [r for r in results if r.status == "FAIL"]
    if fatal:
        msgs = "; ".join(r.message for r in fatal)
        raise DataQualityError(f"Data validation failed: {msgs}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Additional checks and aliases  (Parts N17, N18)
# ─────────────────────────────────────────────────────────────────────────────

def check_tv_pct_ev(
    pv_tv: float,
    total_ev: float,
    warn_threshold: float = 0.80,
) -> ValidationResult:
    """
    Alias for check_tv_pct_of_ev().
    Check that the terminal value does not dominate the enterprise value.
    Reference: Architecture Plan Part N17.
    """
    return check_tv_pct_of_ev(pv_tv, total_ev, warn_threshold)


def check_roic_growth_consistency(
    roic: float,
    reinvestment_rate: float,
    terminal_g: float,
    tolerance: float = 0.02,
) -> ValidationResult:
    """
    Check that the implied terminal growth implied by ROIC × reinvestment_rate
    is consistent with the explicit terminal_g assumption.

    Implied g = ROIC × reinvestment_rate.
    A warning is issued if |implied_g − terminal_g| > tolerance.

    Reference: Architecture Plan Part N18.
    """
    implied_g = roic * reinvestment_rate
    diff = abs(implied_g - terminal_g)
    name = "ROIC-growth-consistency"
    if diff <= tolerance:
        return ValidationResult(
            name=name,
            status="OK",
            message=(
                f"ROIC×reinv ({implied_g:.2%}) consistent with terminal_g ({terminal_g:.2%})."
            ),
        )
    return ValidationResult(
        name=name,
        status="WARN",
        message=(
            f"ROIC×reinv ({implied_g:.2%}) deviates from terminal_g ({terminal_g:.2%}) "
            f"by {diff:.2%} (>{tolerance:.0%} tolerance). "
            "Verify reinvestment rate or terminal growth assumption."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → check_terminal_roic_vs_wacc
validate_terminal_roic = check_terminal_roic_vs_wacc

#: Canonical checklist name → check_tv_pct_of_ev
check_tv_pct_ev = check_tv_pct_of_ev


def check_ufcf_sign(
    ufcf_series: list[float],
    name: str = "check_ufcf_sign",
) -> "ValidationResult":
    """
    UFCF may be negative for early-stage companies; do NOT auto-clip.
    This check is informational — always returns PASS.

    Reference: Architecture Plan Phase 8.
    """
    any_negative = any(v < 0 for v in (ufcf_series or []))
    return ValidationResult(
        name=name,
        status="PASS",
        message=(
            "UFCF contains negative values (early-stage pattern; not clipped)."
            if any_negative else "UFCF non-negative across all forecast years."
        ),
    )
