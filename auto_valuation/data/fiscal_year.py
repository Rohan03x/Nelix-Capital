"""
data/fiscal_year.py — Fiscal year alignment, TTM, stub-period, calendarisation.

Reference: Architecture Plan Parts 2.3, 4.5, 28, 37.2.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Fiscal year alignment  (Part 2.3)
# ─────────────────────────────────────────────────────────────────────────────

def get_fiscal_year_end_month(statements: list[dict]) -> int:
    """
    Infer the fiscal year-end month from the most-recent annual report date.
    Returns integer month (1-12). Defaults to 12 if unknown.
    """
    for stmt in statements:
        date_str = stmt.get("date") or stmt.get("fillingDate") or ""
        if date_str:
            try:
                return datetime.strptime(date_str[:10], "%Y-%m-%d").month
            except ValueError:
                pass
    return 12


def stub_period_weight(fiscal_year_end_month: int) -> float:
    """
    Fraction of the current calendar year already elapsed for a Dec-YE model.
    Used for mid-year discounting stub adjustment (Part 4.5).

    Example: fiscal year ends May → stub = 5/12 of current year remaining.
    Returns a float in [0.0, 1.0].
    """
    today_month = date.today().month
    # Months until next fiscal year-end
    if fiscal_year_end_month >= today_month:
        months_remaining = fiscal_year_end_month - today_month
    else:
        months_remaining = 12 - today_month + fiscal_year_end_month
    return months_remaining / 12.0


def align_to_calendar_year(statements: list[dict]) -> list[dict]:
    """
    Add a 'calendar_year' field to each statement based on the report date.
    FMP already provides calendarYear for most tickers; this fills gaps.
    """
    result = []
    for stmt in statements:
        stmt = dict(stmt)
        if not stmt.get("calendarYear"):
            date_str = stmt.get("date") or ""
            if date_str:
                stmt["calendarYear"] = date_str[:4]
        result.append(stmt)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# TTM computation  (Part 28)
# ─────────────────────────────────────────────────────────────────────────────

# Fields that should be SUMMED for TTM (flow items)
_TTM_SUM_FIELDS = {
    "revenue", "cogs", "gross_profit", "ebit", "ebitda", "da",
    "rd_expense", "sga", "interest_expense", "tax_expense", "net_income",
    "cfo", "capex", "fcf_reported", "sbc", "delta_wc_reported",
    # Raw FMP names (before standardisation)
    "operatingIncome", "depreciationAndAmortization", "incomeTaxExpense",
    "netIncome", "capitalExpenditure", "operatingCashFlow", "freeCashFlow",
    "stockBasedCompensation",
}

# Fields that should use the LATEST quarter value (balance sheet / point-in-time)
_TTM_LATEST_FIELDS = {
    "cash", "st_investments", "accounts_receivable", "inventory",
    "current_assets", "ppe_net", "goodwill", "intangibles", "total_assets",
    "accounts_payable", "short_term_debt", "current_liabilities",
    "long_term_debt", "total_debt", "deferred_tax_liability", "total_liabilities",
    "retained_earnings", "shareholders_equity", "total_equity", "nci", "apic", "aoci",
}


def compute_ttm(
    quarterly_is: list[dict],
    quarterly_cf: list[dict],
    quarterly_bs: list[dict],
) -> dict[str, Any]:
    """
    Compute a Trailing-Twelve-Month snapshot by:
      - Summing the most-recent 4 quarters for income statement and cash flow fields
      - Taking the most-recent quarter for balance sheet fields

    All inputs are most-recent-first (FMP convention).
    Returns a single dict with key 'period' = 'TTM' and 'date' = latest quarter date.

    Reference: Part 28.
    """
    ttm: dict[str, Any] = {"period": "TTM", "calendarYear": "TTM"}

    # Latest date
    if quarterly_is:
        ttm["date"] = quarterly_is[0].get("date", "")

    # Sum IS fields over 4 quarters
    is_q4 = quarterly_is[:4]
    for field in _TTM_SUM_FIELDS:
        values = [q.get(field) for q in is_q4 if q.get(field) is not None]
        if values:
            ttm[field] = sum(values)

    # Sum CF fields over 4 quarters
    cf_q4 = quarterly_cf[:4]
    for field in _TTM_SUM_FIELDS:
        if field not in ttm:   # don't overwrite IS data
            values = [q.get(field) for q in cf_q4 if q.get(field) is not None]
            if values:
                ttm[field] = sum(values)

    # Latest BS (point-in-time)
    if quarterly_bs:
        latest_bs = quarterly_bs[0]
        for field in _TTM_LATEST_FIELDS:
            val = latest_bs.get(field)
            if val is not None:
                ttm[field] = val

    return ttm


# ─────────────────────────────────────────────────────────────────────────────
# Peer calendarisation  (Part 37.2)
# ─────────────────────────────────────────────────────────────────────────────

def calendarize_peer_data(
    peer_statements: list[dict],
    target_fiscal_month: int = 12,
) -> list[dict]:
    """
    Adjust peer company LTM metrics so all peers share the same
    fiscal year-end month as the subject company (target_fiscal_month).

    Implementation: for peers whose fiscal year ends in a different month,
    use their TTM data instead of their last annual data to align endpoints.
    This is a best-effort approximation; a full stub-period interpolation
    would require quarterly data per peer.

    Reference: Part 37.2.
    """
    result = []
    for stmt in peer_statements:
        stmt = dict(stmt)
        peer_fy_month = get_fiscal_year_end_month([stmt])
        if peer_fy_month != target_fiscal_month:
            # Flag that this peer should use TTM data for comps
            stmt["use_ttm_for_comps"] = True
            stmt["calendarization_note"] = (
                f"FY ends month {peer_fy_month}; "
                f"subject FY ends month {target_fiscal_month} — "
                "using TTM for alignment."
            )
        else:
            stmt["use_ttm_for_comps"] = False
        result.append(stmt)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# LTM calendarization and peer table builder  (Parts 48, N11)
# ─────────────────────────────────────────────────────────────────────────────

def calendarize_ltm(
    ltm_data: dict,
    fiscal_year_end_month: int,
    reference_month: int = 12,
) -> dict:
    """
    Adjust LTM (last-twelve-months) data for a non-December fiscal year end
    to make it comparable to a December year-end reference.

    When *fiscal_year_end_month* == *reference_month* no adjustment is needed.
    Otherwise a note is appended flagging the mismatch for manual review.

    Returns a copy of *ltm_data* with a 'calendarized' flag and note.

    Reference: Architecture Plan Parts 48, N11.
    """
    ltm = dict(ltm_data)
    ltm["calendarized"] = True
    ltm["fiscal_year_end_month"] = fiscal_year_end_month
    ltm["reference_month"]       = reference_month

    if fiscal_year_end_month == reference_month:
        ltm["calendarization_note"] = "FY end matches reference month — no adjustment needed."
    else:
        offset = reference_month - fiscal_year_end_month
        ltm["calendarization_offset_months"] = offset
        ltm["calendarization_note"] = (
            f"FY ends month {fiscal_year_end_month}; reference month {reference_month} — "
            f"offset {offset:+d} months flagged for LTM alignment."
        )
    return ltm


def build_calendarized_peer_table(
    peers_data: list[dict],
    reference_month: int = 12,
    ltm_field: str = "ltm",
) -> list[dict]:
    """
    Build a list of calendarized peer data dicts ready for comps analysis.

    Each peer entry should contain:
      - 'ticker':              peer ticker symbol
      - 'income_stmts':        list of annual statements
      - 'fiscal_year_end_month': integer (1-12)
      - 'ltm':                 LTM income statement dict (optional)

    Returns a list of dicts, each with the calendarized LTM data and
    alignment metadata.

    Reference: Architecture Plan Part N11.
    """
    from auto_valuation.data.fiscal_year import get_fiscal_year_end_month

    result = []
    for peer in peers_data:
        peer_out    = dict(peer)
        stmts       = peer.get("income_stmts", [])
        fy_month    = peer.get("fiscal_year_end_month") or get_fiscal_year_end_month(stmts)
        ltm         = peer.get(ltm_field) or (stmts[0] if stmts else {})
        peer_out["calendarized_ltm"] = calendarize_ltm(ltm, fy_month, reference_month)
        result.append(peer_out)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical alias (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → align_to_calendar_year
align_fiscal_year = align_to_calendar_year
