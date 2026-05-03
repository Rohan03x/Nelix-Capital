"""
data/fx.py — FX conversion for non-USD reporting companies.

Reference: Architecture Plan Part 29.

Rules:
  - Income statement / cash flow: use historical AVERAGE rate for the fiscal year
  - Balance sheet: use CLOSING rate (period-end)
  - Forecast: leave in original reporting currency; convert at spot for display
  - Source: FMP exchange rates or ECB/FRED as fallback
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests

from auto_valuation.utils.error import DataFetchError


def _get(url: str, params: dict | None = None, timeout: int = 15) -> Any:
    resp = requests.get(url, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_fx_rate(
    from_currency: str,
    to_currency: str = "USD",
    fiscal_year: int | None = None,
    rate_type: str = "average",   # "average" or "closing"
) -> float:
    """
    Return the FX rate (from_currency → to_currency) for a given fiscal year.

    rate_type:
      "average"  — annual average rate (for income statement / cash flow)
      "closing"  — year-end closing rate (for balance sheet)

    Falls back to 1.0 if the rate cannot be fetched (caller should warn).
    Reference: Part 29.
    """
    if from_currency.upper() == to_currency.upper():
        return 1.0

    if fiscal_year is None:
        fiscal_year = date.today().year - 1

    api_key = os.getenv("FMP_API_KEY", "")
    if not api_key:
        return _ecb_fallback(from_currency, to_currency, fiscal_year, rate_type)

    pair = f"{from_currency.upper()}{to_currency.upper()}"
    try:
        if rate_type == "closing":
            # Use Dec 31 of the fiscal year
            target_date = f"{fiscal_year}-12-31"
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/forex/{pair}"
            data = _get(url, {"from": target_date, "to": target_date, "apikey": api_key})
            hist = (data.get("historical") or [])
            if hist:
                return float(hist[0].get("close", 1.0))
        else:
            # Average: fetch full year and average daily closes
            start = f"{fiscal_year}-01-01"
            end   = f"{fiscal_year}-12-31"
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/forex/{pair}"
            data = _get(url, {"from": start, "to": end, "apikey": api_key})
            hist = data.get("historical") or []
            if hist:
                closes = [float(r.get("close", 1.0)) for r in hist if r.get("close")]
                if closes:
                    return sum(closes) / len(closes)
    except Exception:
        pass

    return _ecb_fallback(from_currency, to_currency, fiscal_year, rate_type)


def _ecb_fallback(
    from_currency: str,
    to_currency: str,
    fiscal_year: int,
    rate_type: str,
) -> float:
    """
    ECB reference rates fallback (EUR-based pairs only).
    For non-EUR pairs this will return 1.0 as a last resort.
    """
    if to_currency.upper() != "USD" and from_currency.upper() != "USD":
        return 1.0

    try:
        # ECB data portal — only covers EUR/XXX pairs
        currency = from_currency if to_currency.upper() == "USD" else to_currency
        url = (
            f"https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.USD.SP00.A"
            f"?startPeriod={fiscal_year}-01-01&endPeriod={fiscal_year}-12-31"
            f"&format=jsondata"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            observations = (
                data.get("dataSets", [{}])[0]
                    .get("series", {})
                    .get("0:0:0:0:0", {})
                    .get("observations", {})
            )
            values = [v[0] for v in observations.values() if v and v[0] is not None]
            if values:
                if rate_type == "closing":
                    return float(values[-1])
                return sum(values) / len(values)
    except Exception:
        pass

    return 1.0   # absolute fallback — caller should warn


def apply_fx_conversion(
    statements: list[dict],
    from_currency: str,
    to_currency: str = "USD",
    statement_type: str = "income",   # "income" | "balance" | "cashflow"
) -> list[dict]:
    """
    Apply FX conversion to all monetary fields in a list of statements.

    statement_type:
      "income" / "cashflow" → use AVERAGE rate for each fiscal year
      "balance"             → use CLOSING rate for each fiscal year

    Reference: Part 29.
    """
    if from_currency.upper() == to_currency.upper():
        return statements

    rate_type = "closing" if statement_type == "balance" else "average"
    _SKIP = {"calendarYear", "period", "reportedCurrency", "cik", "link", "date", "symbol"}

    result = []
    for stmt in statements:
        stmt = dict(stmt)
        year = int(str(stmt.get("calendarYear") or stmt.get("date", "2000")[:4]))
        rate = fetch_fx_rate(from_currency, to_currency, year, rate_type)
        for k, v in stmt.items():
            if isinstance(v, (int, float)) and k not in _SKIP and v != 0:
                stmt[k] = v * rate
        stmt["fx_rate_applied"]    = rate
        stmt["fx_from_currency"]   = from_currency
        stmt["fx_to_currency"]     = to_currency
        result.append(stmt)
    return result
