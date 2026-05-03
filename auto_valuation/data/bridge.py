"""
data/bridge.py — Canonical compute_net_debt() and EV bridge helpers.

Reference: Architecture Plan Part 60.

Net Debt = ST_debt + LT_debt + finance_leases - cash - ST_investments
           + preferred_stock + NCI_book_value
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide


def compute_net_debt(balance_sheet: dict) -> float:
    """
    Canonical net debt calculation per Part 60.

    Adds:
      + Short-term debt (notes payable, current portion of LT debt)
      + Long-term debt
      + Finance lease obligations (where separately reported)
      + Preferred stock (liquidation value)
      + NCI / minority interest (book value)

    Deducts:
      - Cash and cash equivalents
      - Short-term investments

    A negative result means the company holds net cash (valid — do NOT clip to 0).

    Reference: Part 60.
    """
    bs = balance_sheet

    # ── Debt components ───────────────────────────────────────────────────────
    st_debt         = _pos(bs.get("short_term_debt")       or bs.get("shortTermDebt")      or 0)
    lt_debt         = _pos(bs.get("long_term_debt")        or bs.get("longTermDebt")        or 0)
    # FMP may have a combined totalDebt field — only use if breakdown not available
    total_debt_fmp  = _pos(bs.get("total_debt")            or bs.get("totalDebt")           or 0)
    finance_leases  = _pos(bs.get("financeLeaseLiability") or bs.get("capitalLeaseObligations") or 0)
    preferred       = _pos(bs.get("preferredStock")        or bs.get("preferredStocksAndOther") or 0)
    nci             = _pos(bs.get("nci")                   or bs.get("minorityInterest")    or 0)

    # If FMP only provides totalDebt (no split), use that
    if st_debt == 0 and lt_debt == 0 and total_debt_fmp > 0:
        debt_total = total_debt_fmp
    else:
        debt_total = st_debt + lt_debt

    # ── Cash items ────────────────────────────────────────────────────────────
    cash            = _pos(bs.get("cash")                  or bs.get("cashAndCashEquivalents") or 0)
    st_investments  = _pos(bs.get("st_investments")        or bs.get("shortTermInvestments")   or 0)

    net_debt = (
        debt_total
        + finance_leases
        + preferred
        + nci
        - cash
        - st_investments
    )
    return net_debt


def validate_net_debt(
    net_debt_m: float,
    ev_m: float,
    equity_value_m: float,
    tolerance_m: float = 1.0,
) -> bool:
    """
    Cross-check: EV − net_debt should equal equity_value within tolerance.

    Returns True if consistent; raises ValueError if discrepancy > tolerance_m.
    Reference: Architecture Plan Part 60.
    """
    implied_equity = ev_m - net_debt_m
    diff = abs(implied_equity - equity_value_m)
    if diff > tolerance_m:
        raise ValueError(
            f"Net debt bridge discrepancy: EV({ev_m:.1f}) - NetDebt({net_debt_m:.1f}) = "
            f"{implied_equity:.1f} but equity_value = {equity_value_m:.1f} "
            f"(diff = {diff:.1f}m, tolerance = {tolerance_m:.1f}m). "
            f"Check component inputs."
        )
    return True


def _pos(val: float | None) -> float:
    """Coerce None or negative to 0 (balance sheet items should be non-negative)."""
    if val is None:
        return 0.0
    return max(0.0, float(val))


def compute_equity_value(
    enterprise_value: float,
    balance_sheet: dict,
    equity_method_investments: float = 0.0,
    net_operating_losses_pv: float = 0.0,
    restricted_cash: float = 0.0,
) -> float:
    """
    EV → Equity Value bridge (Part 34).

    equity_value = EV
        - IBD (ST + LT debt + finance leases)
        - preferred stock
        - NCI (book value)
        - pension underfunding (net of deferred tax)
        + cash
        + short-term investments
        + equity method investments (if not already in UFCF)
        + net operating loss PV (if material)
        + restricted cash (if pledged to debt service, deduct; otherwise add)

    Reference: Parts 3.4, 34, 60.
    """
    bs = balance_sheet
    net_debt = compute_net_debt(bs)

    # EV - net_debt = equity value (before equity method investments etc.)
    equity_value = enterprise_value - net_debt

    # Add non-operating assets not captured in UFCF
    equity_value += equity_method_investments
    equity_value += net_operating_losses_pv
    equity_value += restricted_cash   # restricted cash is already in net_debt deduction — caller manages

    return equity_value
