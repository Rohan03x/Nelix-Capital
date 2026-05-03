"""
model/debt.py — Debt rollforward, interest schedule, cost of debt.

Reference: Architecture Plan Parts 18, 33.2.

All monetary values in USD millions.
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# Historical cost of debt  (Part 33.2)
# ─────────────────────────────────────────────────────────────────────────────

def historical_cost_of_debt(
    income_stmts: list[dict],
    balance_sheets: list[dict],
    years: int = 3,
    fallback: float = 0.05,
) -> float:
    """
    Estimate pre-tax cost of debt = Interest Expense / Average Total Debt.
    Averages over last `years` years.
    Reference: Part 33.2.
    """
    bs_map = {b.get("calendarYear", ""): b for b in (balance_sheets or [])}
    rates: list[float] = []

    for s in income_stmts[:years]:
        yr        = s.get("calendarYear", "")
        int_exp   = abs(s.get("interest_expense") or s.get("interestExpense") or 0)
        bs        = bs_map.get(yr, {})
        lt_debt   = bs.get("long_term_debt")  or bs.get("longTermDebt")  or 0
        st_debt   = bs.get("short_term_debt") or bs.get("shortTermDebt") or 0
        total_debt = lt_debt + st_debt
        if total_debt > 0 and int_exp > 0:
            rates.append(int_exp / total_debt)

    if not rates:
        return fallback

    avg = sum(rates) / len(rates)
    # Clamp to reasonable range
    return max(0.02, min(0.15, avg))


# ─────────────────────────────────────────────────────────────────────────────
# Debt rollforward  (Part 18)
# ─────────────────────────────────────────────────────────────────────────────

def build_debt_schedule(
    opening_total_debt: float,
    interest_rate: float,
    repayment_schedule: list[float] | None = None,
    forecast_years: int = 10,
) -> list[dict]:
    """
    Simplified debt rollforward assuming no new borrowings and a straight-line
    optional repayment schedule.

    Each year: Interest = opening_debt × interest_rate (before repayment)

    repayment_schedule: optional list of annual principal repayments.
    If None, assumes debt stays flat (bullet structure).

    Returns list of dicts per year:
      year_index, opening_debt, interest_expense, repayment, closing_debt

    Reference: Part 18.
    """
    records: list[dict] = []
    debt = opening_total_debt

    for i in range(forecast_years):
        interest   = debt * interest_rate
        repayment  = (repayment_schedule[i]
                      if repayment_schedule and i < len(repayment_schedule)
                      else 0.0)
        closing    = max(0.0, debt - repayment)
        records.append({
            "year_index":       i + 1,
            "opening_debt":     debt,
            "interest_expense": interest,
            "repayment":        repayment,
            "closing_debt":     closing,
        })
        debt = closing

    return records


# ─────────────────────────────────────────────────────────────────────────────
# Interest income on cash  (Part 18)
# ─────────────────────────────────────────────────────────────────────────────

def compute_interest_income(
    cash_and_st_investments: float,
    risk_free_rate: float = 0.045,
) -> float:
    """
    Estimate annual interest income on cash / short-term investments.
    Uses risk-free rate as a conservative proxy.
    Note: Interest income is NOT included in UFCF (unlevered model).
    It is only used for the levered net income bridge if needed.
    Reference: Part 18.
    """
    return cash_and_st_investments * risk_free_rate


# ─────────────────────────────────────────────────────────────────────────────
# Leverage metrics  (Part 33.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_debt_to_equity(total_debt: float, total_equity: float) -> float:
    """D/E ratio (market or book). Returns 0.0 if equity ≤ 0."""
    return safe_divide(total_debt, total_equity, 0.0)


def compute_net_debt_to_ebitda(net_debt: float, ebitda: float) -> float:
    """Net leverage ratio. Returns 0.0 if EBITDA ≤ 0."""
    return safe_divide(net_debt, ebitda, 0.0)


def compute_interest_coverage(ebit: float, interest_expense: float) -> float:
    """EBIT / Interest Expense. Returns inf if interest = 0."""
    if interest_expense <= 0:
        return float("inf")
    return safe_divide(ebit, interest_expense, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Debt maturity rollforward  (Part 58)
# ─────────────────────────────────────────────────────────────────────────────

def rollforward_debt_schedule(
    ibd_opening: float,
    scheduled_repayments: list[float],
    new_debt_issuance: list[float] | None = None,
    kd_pretax: float | list[float] = 0.05,
) -> list[dict]:
    """
    Year-by-year debt balance and interest expense schedule.

    Interest expense for year t = average of opening and closing IBD × kd_pretax.
    Average method avoids step-function interest if a large repayment occurs mid-year.

    Args:
        ibd_opening:          Total IBD at start of year 1 ($M).
        scheduled_repayments: Annual principal repayments ($M), len = forecast_years.
        new_debt_issuance:    New debt issued per year ($M); defaults to all zeros.
        kd_pretax:            Pre-tax cost of debt (constant float or per-year list).

    Returns list of dicts per year:
      year, ibd_opening, repayment, new_issuance, ibd_closing, interest_expense

    Reference: Architecture Plan Part 58.
    """
    n = len(scheduled_repayments)
    if new_debt_issuance is None:
        new_debt_issuance = [0.0] * n

    schedule: list[dict] = []
    ibd = float(ibd_opening)

    for yr in range(n):
        repay     = float(scheduled_repayments[yr])
        new_issue = float(new_debt_issuance[yr]) if yr < len(new_debt_issuance) else 0.0
        kd        = float(kd_pretax[yr]) if isinstance(kd_pretax, list) else float(kd_pretax)

        ibd_close = max(0.0, ibd - repay + new_issue)
        avg_ibd   = (ibd + ibd_close) / 2.0
        int_exp   = avg_ibd * kd

        schedule.append({
            "year":             yr + 1,
            "ibd_opening":      ibd,
            "repayment":        repay,
            "new_issuance":     new_issue,
            "ibd_closing":      ibd_close,
            "interest_expense": int_exp,
        })
        ibd = ibd_close

    return schedule


def validate_debt_schedule(
    ibd_total: float,
    scheduled_repayments: list[float],
    tolerance_pct: float = 0.05,
) -> None:
    """
    Validate that total scheduled repayments do not exceed IBD balance.

    Raises ValueError if total_repayments > ibd_total × (1 + tolerance_pct).
    Reference: Architecture Plan Part 58.
    """
    total_repay = sum(scheduled_repayments)
    if total_repay > ibd_total * (1.0 + tolerance_pct):
        raise ValueError(
            f"Debt repayments (${total_repay:.0f}m) exceed IBD balance "
            f"(${ibd_total:.0f}m). Check debt_schedule in overrides."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases & new functions (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → historical_cost_of_debt
compute_cost_of_debt = historical_cost_of_debt


def compute_interest_expense(
    avg_ibd: float,
    cost_of_debt: float,
) -> float:
    """
    Forecast interest expense = average interest-bearing debt × pre-tax cost of debt.

    For circular reference resolution, use average of opening and closing IBD.
    Reference: Architecture Plan Parts 3.5, C.2.
    """
    return avg_ibd * cost_of_debt
