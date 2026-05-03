"""
model/working_capital.py — Net Operating Working Capital (NOWC) and ΔNOWC.

Reference: Architecture Plan Parts 21, 40.

NOWC = Accounts Receivable + Inventory − Accounts Payable
     (excludes cash, short-term investments, short-term debt — those are
      financing items captured in the EV → equity value bridge)

Negative NOWC is VALID and common for Amazon/Costco/retailer pattern.
A persistently negative NOWC reduces the UFCF model's reinvestment drag.
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# WC days from historical data  (Part 21)
# ─────────────────────────────────────────────────────────────────────────────

def compute_dso(accounts_receivable: float, revenue: float) -> float:
    """Days Sales Outstanding = AR / (Revenue / 365)."""
    return safe_divide(accounts_receivable * 365, revenue, 0.0)


def compute_dio(inventory: float, cogs: float) -> float:
    """Days Inventory Outstanding = Inventory / (COGS / 365)."""
    return safe_divide(inventory * 365, cogs, 0.0)


def compute_dpo(accounts_payable: float, cogs: float) -> float:
    """Days Payable Outstanding = AP / (COGS / 365)."""
    return safe_divide(accounts_payable * 365, cogs, 0.0)


def compute_cwc_days(dso: float, dio: float, dpo: float) -> float:
    """Cash WC cycle = DSO + DIO − DPO."""
    return dso + dio - dpo


def historical_wc_days(
    income_stmts: list[dict],
    balance_sheets: list[dict],
    years: int = 3,
) -> dict[str, float]:
    """
    Average DSO / DIO / DPO over last `years` years.
    Returns dict with keys: dso, dio, dpo, cwc_days.
    Reference: Part 21.
    """
    bs_map = {
        b.get("calendarYear", ""): b
        for b in (balance_sheets or [])
    }
    dsos, dios, dpos = [], [], []

    for stmt in income_stmts[:years]:
        yr   = stmt.get("calendarYear", "")
        rev  = stmt.get("revenue") or 0
        cogs = abs(stmt.get("cogs") or stmt.get("costOfRevenue") or 0)
        bs   = bs_map.get(yr, {})

        ar  = bs.get("accounts_receivable") or bs.get("netReceivables") or 0
        inv = bs.get("inventory")           or bs.get("inventory")      or 0
        ap  = bs.get("accounts_payable")    or bs.get("accountPayables") or 0

        if rev > 0 and ar >= 0:
            dsos.append(compute_dso(ar, rev))
        if cogs > 0 and inv >= 0:
            dios.append(compute_dio(inv, cogs))
        if cogs > 0 and ap >= 0:
            dpos.append(compute_dpo(ap, cogs))

    avg_dso = sum(dsos) / len(dsos) if dsos else 0.0
    avg_dio = sum(dios) / len(dios) if dios else 0.0
    avg_dpo = sum(dpos) / len(dpos) if dpos else 0.0

    return {
        "dso":      avg_dso,
        "dio":      avg_dio,
        "dpo":      avg_dpo,
        "cwc_days": avg_dso + avg_dio - avg_dpo,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NOWC computation  (Part 21, 40)
# ─────────────────────────────────────────────────────────────────────────────

def compute_nowc_from_bs(balance_sheet: dict) -> float:
    """
    NOWC = AR + Inventory − AP (point-in-time from balance sheet).
    Reference: Part 21.
    """
    ar  = balance_sheet.get("accounts_receivable") or balance_sheet.get("netReceivables")       or 0
    inv = balance_sheet.get("inventory")           or balance_sheet.get("inventory")            or 0
    ap  = balance_sheet.get("accounts_payable")    or balance_sheet.get("accountPayables")      or 0
    return ar + inv - ap


def compute_nowc_from_days(
    revenue: float,
    cogs: float,
    dso: float,
    dio: float,
    dpo: float,
) -> float:
    """
    Project NOWC from WC days (used in forecast years).

    AR  = DSO  × Revenue / 365
    Inv = DIO  × COGS    / 365
    AP  = DPO  × COGS    / 365

    Reference: Part 21.
    """
    ar  = dso * revenue / 365.0
    inv = dio * cogs    / 365.0
    ap  = dpo * cogs    / 365.0
    return ar + inv - ap


def compute_delta_nowc(nowc_current: float, nowc_prior: float) -> float:
    """
    ΔNOWC = NOWC_t − NOWC_{t-1}
    Positive ΔNOWC = cash consumed (increases reinvestment in the UFCF formula).
    Negative ΔNOWC = cash released (reduces reinvestment — valid for float-funded models).
    """
    return nowc_current - nowc_prior


# ─────────────────────────────────────────────────────────────────────────────
# Forecast NOWC schedule  (Part 40)
# ─────────────────────────────────────────────────────────────────────────────

def build_nowc_forecast(
    revenues: list[float],
    cogs_pct_rev: float,
    dso: float,
    dio: float,
    dpo: float,
    base_nowc: float,
) -> tuple[list[float], list[float]]:
    """
    Build NOWC and ΔNOWC for each forecast year.

    Inputs:
      revenues       — forecast revenue list (length = forecast_years)
      cogs_pct_rev   — COGS as % of revenue (historical average)
      dso/dio/dpo    — projected WC days (held constant)
      base_nowc      — NOWC at end of the last historical year (year 0)

    Returns:
      (nowc_schedule, delta_nowc_schedule) — both length = forecast_years

    Reference: Part 40.
    """
    nowc_schedule:       list[float] = []
    delta_nowc_schedule: list[float] = []
    prev_nowc = base_nowc

    for rev in revenues:
        cogs    = rev * cogs_pct_rev
        nowc_t  = compute_nowc_from_days(rev, cogs, dso, dio, dpo)
        delta   = compute_delta_nowc(nowc_t, prev_nowc)
        nowc_schedule.append(nowc_t)
        delta_nowc_schedule.append(delta)
        prev_nowc = nowc_t

    return nowc_schedule, delta_nowc_schedule


def historical_cogs_pct(income_stmts: list[dict], years: int = 3) -> float:
    """COGS as % of revenue — median over last `years` years."""
    pcts: list[float] = []
    for s in income_stmts[:years]:
        rev  = s.get("revenue") or 0
        cogs = abs(s.get("cogs") or s.get("costOfRevenue") or 0)
        if rev > 0 and cogs > 0:
            pcts.append(cogs / rev)
    if not pcts:
        return 0.60   # 60% fallback
    return sorted(pcts)[len(pcts) // 2]


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → compute_cwc_days
compute_working_capital_days = compute_cwc_days

#: Canonical checklist name → build_nowc_forecast
forecast_nowc = build_nowc_forecast


def check_wc_seasonality_flag(
    quarterly_balance_sheets: list[dict],
    revenue_threshold: float = 0.30,
) -> bool:
    """
    Detect high seasonal swing in working capital (>30% of revenue difference
    between Q1 and Q3 NOWC balances).

    Returns True if high seasonality is detected (flag triggered).
    Reference: Architecture Plan Part 48.2.
    """
    if not quarterly_balance_sheets or len(quarterly_balance_sheets) < 3:
        return False

    def _nowc(bs: dict) -> float:
        ar  = bs.get("net_receivables") or bs.get("netReceivables") or 0
        inv = bs.get("inventory") or 0
        ap  = bs.get("accounts_payable") or bs.get("accountPayables") or 0
        return ar + inv - ap

    # Group by quarter (period label: Q1, Q2, Q3, Q4)
    q_nowc: dict[str, list[float]] = {}
    for bs in quarterly_balance_sheets:
        period = bs.get("period") or ""
        if period in ("Q1", "Q2", "Q3", "Q4"):
            q_nowc.setdefault(period, []).append(_nowc(bs))

    q1_vals = q_nowc.get("Q1", [])
    q3_vals = q_nowc.get("Q3", [])
    if not q1_vals or not q3_vals:
        return False

    q1_avg = sum(q1_vals) / len(q1_vals)
    q3_avg = sum(q3_vals) / len(q3_vals)
    swing  = abs(q1_avg - q3_avg)

    # Use max NOWC as rough revenue proxy normaliser
    max_nowc = max(abs(q1_avg), abs(q3_avg))
    if max_nowc == 0:
        return False

    return (swing / max_nowc) > revenue_threshold
