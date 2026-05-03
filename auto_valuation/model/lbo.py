"""
model/lbo.py — Leveraged Buyout (LBO) analysis module.

Implements a simplified but IB-grade LBO model:
  - Entry: EV = EBITDA × entry_multiple; equity = EV − total_debt
  - Forecast: EBITDA growth + mandatory debt amortization
  - Exit: EV = EBITDA × exit_multiple; equity at exit = EV − net_debt_exit
  - Returns: IRR (bisection), cash-on-cash (MoM)

IRR computation uses bisection (no scipy dependency).

Reference: Macabacus LBO guide; Architecture Plan Part 17.

All monetary values in USD millions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────────────

def compute_lbo_entry_ev(ebitda_entry: float, entry_multiple: float) -> float:
    """
    Entry enterprise value = EBITDA at entry × entry EV/EBITDA multiple.

    Args:
        ebitda_entry   : LTM EBITDA at acquisition ($M).
        entry_multiple : EV/EBITDA purchase multiple (e.g. 10.0).

    Returns:
        Entry EV ($M).
    """
    if entry_multiple <= 0:
        raise ValueError(f"entry_multiple must be positive, got {entry_multiple}")
    return ebitda_entry * entry_multiple


def compute_lbo_equity_investment(
    entry_ev: float,
    total_debt_mm: float,
) -> float:
    """
    Equity check written at close = Entry EV − total debt raised.

    Args:
        entry_ev       : entry enterprise value ($M).
        total_debt_mm  : total debt raised at close (senior + subordinated) ($M).

    Returns:
        Sponsor equity investment ($M). Raises if result ≤ 0.
    """
    equity = entry_ev - total_debt_mm
    if equity <= 0:
        raise ValueError(
            f"Equity investment must be positive: EV={entry_ev:.1f}, debt={total_debt_mm:.1f}"
        )
    return equity


# ─────────────────────────────────────────────────────────────────────────────
# Debt schedule
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LBODebtYear:
    year: int
    opening_debt: float
    interest_expense: float     # pre-tax
    mandatory_amort: float      # scheduled principal repayment
    cash_sweep: float           # excess cash used to repay debt (optional)
    closing_debt: float
    ebitda: float
    net_leverage: float         # closing_debt / ebitda


def build_lbo_debt_schedule(
    opening_debt: float,
    ebitda_schedule: list[float],
    interest_rate: float,
    mandatory_amort_pct: float = 0.05,
    cash_sweep_pct: float = 0.50,
    cash_tax_rate: float = 0.25,
    capex_pct_ebitda: float = 0.10,
    nwc_change_pct_revenue: float = 0.0,
    revenue_schedule: list[float] | None = None,
) -> list[LBODebtYear]:
    """
    Build an LBO debt repayment schedule incorporating mandatory amortisation
    and an optional cash sweep (excess cash flow applied to debt).

    Cash available for sweep each year:
        EBITDA − Interest (after-tax) − Mandatory amort − CapEx − ΔNWC

    Args:
        opening_debt          : Total debt at close ($M).
        ebitda_schedule       : Projected EBITDA for years 1..N ($M).
        interest_rate         : Annual interest rate on all debt (blended, pre-tax).
        mandatory_amort_pct   : Annual mandatory amortisation as % of opening debt.
        cash_sweep_pct        : Fraction of remaining FCF applied as additional debt sweep.
        cash_tax_rate         : Cash tax rate for after-tax FCF calculation.
        capex_pct_ebitda      : CapEx as % of EBITDA.
        nwc_change_pct_revenue: ΔNWC as % of revenue (typically small, positive = use).
        revenue_schedule      : Revenue for each year (only used if nwc_change_pct_revenue > 0).

    Returns:
        List of LBODebtYear dataclasses, length = len(ebitda_schedule).
    """
    if interest_rate < 0 or interest_rate > 1:
        raise ValueError(f"interest_rate must be in [0, 1], got {interest_rate}")
    if not (0.0 <= mandatory_amort_pct <= 1.0):
        raise ValueError(f"mandatory_amort_pct must be in [0, 1], got {mandatory_amort_pct}")

    debt = opening_debt
    schedule: list[LBODebtYear] = []

    for i, ebitda in enumerate(ebitda_schedule, start=1):
        opening = debt
        # Interest on average balance (opening; will be refined below)
        interest = opening * interest_rate
        mandatory = min(opening * mandatory_amort_pct, opening)

        # After-tax FCF available for sweep
        ebit_approx = ebitda * 0.70          # rough approximation; margins vary
        nopat = ebit_approx * (1.0 - cash_tax_rate)
        capex = ebitda * capex_pct_ebitda
        rev_t = (revenue_schedule[i - 1] if revenue_schedule and i <= len(revenue_schedule) else 0.0)
        delta_nwc = rev_t * nwc_change_pct_revenue

        fcf_before_sweep = nopat - capex - delta_nwc - mandatory
        cash_sweep = max(0.0, fcf_before_sweep * cash_sweep_pct)
        cash_sweep = min(cash_sweep, max(0.0, opening - mandatory))  # can't repay more than balance

        closing = max(0.0, opening - mandatory - cash_sweep)
        net_lev = closing / ebitda if ebitda > 0 else float("inf")

        schedule.append(LBODebtYear(
            year=i,
            opening_debt=opening,
            interest_expense=interest,
            mandatory_amort=mandatory,
            cash_sweep=cash_sweep,
            closing_debt=closing,
            ebitda=ebitda,
            net_leverage=net_lev,
        ))
        debt = closing

    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Exit
# ─────────────────────────────────────────────────────────────────────────────

def compute_lbo_exit_ev(ebitda_exit: float, exit_multiple: float) -> float:
    """
    Exit enterprise value = EBITDA at exit × exit EV/EBITDA multiple.

    Args:
        ebitda_exit    : EBITDA in exit year ($M).
        exit_multiple  : EV/EBITDA exit multiple (e.g. 9.0).

    Returns:
        Exit EV ($M).
    """
    if exit_multiple <= 0:
        raise ValueError(f"exit_multiple must be positive, got {exit_multiple}")
    return ebitda_exit * exit_multiple


def compute_lbo_equity_at_exit(
    exit_ev: float,
    net_debt_exit: float,
) -> float:
    """
    Equity value at exit = Exit EV − Net debt at exit.

    Net debt at exit should be total IBD (after all sweeps / amort) minus
    any cash on the balance sheet.

    Returns:
        Equity value at exit ($M).  May be negative if deeply distressed.
    """
    return exit_ev - net_debt_exit


# ─────────────────────────────────────────────────────────────────────────────
# Returns
# ─────────────────────────────────────────────────────────────────────────────

def compute_cash_on_cash(
    equity_exit: float,
    equity_entry: float,
) -> float:
    """
    Cash-on-cash return (Money-on-Money, MoM):
        MoM = Equity at exit / Equity invested at entry

    Args:
        equity_exit  : Proceeds to equity at exit ($M).
        equity_entry : Sponsor equity check at close ($M, positive).

    Returns:
        MoM multiple (e.g. 2.5 = 2.5×).  Returns 0.0 if entry ≤ 0.
    """
    if equity_entry <= 0:
        return 0.0
    return equity_exit / equity_entry


def compute_lbo_irr(
    equity_entry: float,
    equity_exit: float,
    years: float,
    tol: float = 1e-8,
    max_iter: int = 200,
) -> float:
    """
    Sponsor IRR from a simple 2-cash-flow LBO (entry and exit only).

    Solves for r in:  equity_exit / (1 + r)^years  =  equity_entry
    Equivalently:     r = (equity_exit / equity_entry)^(1/years) − 1

    When intermediate cash flows exist (dividends, recaps), use
    ``compute_lbo_irr_cashflows`` instead.

    Args:
        equity_entry : Equity invested at t=0 ($M, positive).
        equity_exit  : Equity proceeds at exit ($M).
        years        : Holding period in years (float for partial years).
        tol          : Convergence tolerance for bisection (fallback only).
        max_iter     : Max bisection iterations (fallback only).

    Returns:
        IRR as a decimal (e.g. 0.20 = 20%).  Raises ValueError if equity_entry ≤ 0.
    """
    if equity_entry <= 0:
        raise ValueError(f"equity_entry must be positive, got {equity_entry}")
    if years <= 0:
        raise ValueError(f"years must be positive, got {years}")
    if equity_exit <= 0:
        return -1.0  # total loss

    # Closed-form for 2 cash-flow IRR: r = (exit/entry)^(1/years) - 1
    return (equity_exit / equity_entry) ** (1.0 / years) - 1.0


def compute_lbo_irr_cashflows(
    cashflows: list[float],
    tol: float = 1e-9,
    max_iter: int = 300,
) -> float:
    """
    Compute IRR for an arbitrary cash flow series using bisection.
    No scipy used.

    cashflows[0] is the initial outflow (negative, e.g. -100.0).
    cashflows[1:] are the inflows.  The last element is the exit proceeds.

    Raises ValueError if no sign change is found (degenerate cash flows).

    Args:
        cashflows : List of cash flows with at least one sign change.
        tol       : Convergence tolerance.
        max_iter  : Maximum bisection iterations.

    Returns:
        IRR as a decimal.
    """
    if len(cashflows) < 2:
        raise ValueError("Need at least 2 cash flows for IRR")

    def npv(r: float) -> float:
        total = 0.0
        for t, cf in enumerate(cashflows):
            total += cf / (1.0 + r) ** t
        return total

    # Find bracket
    lo, hi = -0.999, 10.0
    npv_lo = npv(lo)
    npv_hi = npv(hi)

    if npv_lo * npv_hi > 0:
        # Try to widen bracket
        for hi_try in [50.0, 200.0, 1000.0]:
            if npv_lo * npv(hi_try) < 0:
                hi = hi_try
                npv_hi = npv(hi_try)
                break
        else:
            raise ValueError(
                "No sign change found in NPV — cannot compute IRR. "
                "Check that cash flows have at least one sign change."
            )

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        npv_mid = npv(mid)
        if abs(npv_mid) < tol or (hi - lo) / 2.0 < tol:
            return mid
        if npv_lo * npv_mid < 0:
            hi = mid
            npv_hi = npv_mid
        else:
            lo = mid
            npv_lo = npv_mid

    return (lo + hi) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Full LBO Analysis
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LBOResult:
    # Entry
    entry_ev: float
    equity_entry: float
    total_debt_entry: float
    entry_leverage: float           # total_debt / ebitda_entry
    entry_multiple: float

    # Exit
    exit_ev: float
    equity_exit: float
    net_debt_exit: float
    exit_multiple: float
    exit_ebitda: float

    # Returns
    irr: float
    cash_on_cash: float             # MoM
    holding_years: int

    # Schedule
    debt_schedule: list[LBODebtYear] = field(default_factory=list)

    # Warnings
    warnings: list[str] = field(default_factory=list)


def run_lbo_analysis(
    ebitda_entry: float,
    entry_multiple: float,
    debt_pct_ev: float,
    ebitda_growth_rate: float,
    exit_multiple: float,
    holding_years: int = 5,
    interest_rate: float = 0.065,
    mandatory_amort_pct: float = 0.05,
    cash_sweep_pct: float = 0.50,
    cash_tax_rate: float = 0.25,
    capex_pct_ebitda: float = 0.10,
) -> LBOResult:
    """
    End-to-end simplified LBO analysis.

    Args:
        ebitda_entry       : LTM EBITDA at acquisition ($M).
        entry_multiple     : EV/EBITDA purchase multiple.
        debt_pct_ev        : Leverage as % of entry EV (e.g. 0.60 = 60%).
        ebitda_growth_rate : Constant annual EBITDA growth rate during holding period.
        exit_multiple      : EV/EBITDA exit multiple.
        holding_years      : Holding period in years.
        interest_rate      : Blended pre-tax cost of debt.
        mandatory_amort_pct: Annual mandatory amort as % of original debt principal.
        cash_sweep_pct     : Fraction of FCF applied to additional debt repayment.
        cash_tax_rate      : Cash tax rate for FCF computation.
        capex_pct_ebitda   : CapEx as % of EBITDA.

    Returns:
        LBOResult dataclass.
    """
    warnings: list[str] = []

    # ── Entry ──────────────────────────────────────────────────────────────
    if not (0.0 < debt_pct_ev < 1.0):
        raise ValueError(f"debt_pct_ev must be in (0, 1), got {debt_pct_ev}")

    entry_ev = compute_lbo_entry_ev(ebitda_entry, entry_multiple)
    total_debt_entry = entry_ev * debt_pct_ev
    equity_entry = compute_lbo_equity_investment(entry_ev, total_debt_entry)
    entry_leverage = total_debt_entry / ebitda_entry if ebitda_entry > 0 else float("inf")

    if entry_leverage > 8.0:
        warnings.append(f"Entry leverage {entry_leverage:.1f}× > 8.0× — unusually high.")

    # ── EBITDA forecast ────────────────────────────────────────────────────
    ebitda_schedule: list[float] = []
    ebitda_t = ebitda_entry
    for _ in range(holding_years):
        ebitda_t = ebitda_t * (1.0 + ebitda_growth_rate)
        ebitda_schedule.append(ebitda_t)

    # ── Debt schedule ──────────────────────────────────────────────────────
    debt_schedule = build_lbo_debt_schedule(
        opening_debt=total_debt_entry,
        ebitda_schedule=ebitda_schedule,
        interest_rate=interest_rate,
        mandatory_amort_pct=mandatory_amort_pct,
        cash_sweep_pct=cash_sweep_pct,
        cash_tax_rate=cash_tax_rate,
        capex_pct_ebitda=capex_pct_ebitda,
    )

    # ── Exit ───────────────────────────────────────────────────────────────
    ebitda_exit = ebitda_schedule[-1]
    net_debt_exit = debt_schedule[-1].closing_debt  # assume no cash accumulation (swept)
    exit_ev = compute_lbo_exit_ev(ebitda_exit, exit_multiple)
    equity_exit = compute_lbo_equity_at_exit(exit_ev, net_debt_exit)

    if equity_exit <= 0:
        warnings.append("Equity at exit is non-positive — deal underwater.")

    # ── Returns ────────────────────────────────────────────────────────────
    irr = compute_lbo_irr(equity_entry, max(equity_exit, 0.0), holding_years)
    mom = compute_cash_on_cash(equity_exit, equity_entry)

    if irr < 0.15:
        warnings.append(f"IRR {irr:.1%} < 15% — below typical PE hurdle rate.")
    if mom < 2.0:
        warnings.append(f"MoM {mom:.2f}× < 2.0× — below typical PE target.")

    return LBOResult(
        entry_ev=entry_ev,
        equity_entry=equity_entry,
        total_debt_entry=total_debt_entry,
        entry_leverage=entry_leverage,
        entry_multiple=entry_multiple,
        exit_ev=exit_ev,
        equity_exit=equity_exit,
        net_debt_exit=net_debt_exit,
        exit_multiple=exit_multiple,
        exit_ebitda=ebitda_exit,
        irr=irr,
        cash_on_cash=mom,
        holding_years=holding_years,
        debt_schedule=debt_schedule,
        warnings=warnings,
    )
