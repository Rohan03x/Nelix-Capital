"""
data/fetcher.py — All market data fetching: FMP, yfinance, FRED, Damodaran.

Reference: Architecture Plan Parts 2.1, A.1-A.4, 28, 37.1, 45, 55.1, 78.2, 79.2.

All monetary values are returned in the units reported by FMP (USD millions for
US tickers). Unit normalisation happens in cleaner.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yfinance as yf

from auto_valuation.config import (
    FMP_BASE_URL,
    API_RATE_LIMIT_SLEEP,
    MAX_RETRIES,
    RETRY_BACKOFF,
    PRICE_STALENESS_DAYS,
    CACHE_DIR,
    RF_FRED_SERIES,
    RF_DEFAULT_FALLBACK,
)
from auto_valuation.utils.error import DataFetchError


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmp_key() -> str:
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        raise DataFetchError(
            "FMP_API_KEY not set. Add it to your .env file."
        )
    return key


def _get(url: str, params: dict | None = None, timeout: int = 20) -> Any:
    """HTTP GET with retry + exponential back-off. Returns parsed JSON."""
    params = params or {}
    last_exc: Exception | None = None
    wait = API_RATE_LIMIT_SLEEP
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            time.sleep(API_RATE_LIMIT_SLEEP)
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(wait)
            wait *= RETRY_BACKOFF
    raise DataFetchError(f"GET {url} failed after {MAX_RETRIES} attempts: {last_exc}")


def _fmp(path: str, **params) -> Any:
    """FMP API call. Injects apikey automatically."""
    params["apikey"] = _fmp_key()
    return _get(f"{FMP_BASE_URL}{path}", params)


# ─────────────────────────────────────────────────────────────────────────────
# 1A — FMP financial statement endpoints
# ─────────────────────────────────────────────────────────────────────────────

def fetch_income_statement(ticker: str, limit: int = 10) -> list[dict]:
    """Annual income statements, most-recent-first. Returns list of dicts."""
    data = _fmp(f"/v3/income-statement/{ticker}", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected income statement response for {ticker}: {type(data)}")
    return data


def fetch_balance_sheet(ticker: str, limit: int = 10) -> list[dict]:
    data = _fmp(f"/v3/balance-sheet-statement/{ticker}", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected balance sheet response for {ticker}: {type(data)}")
    return data


def fetch_cash_flow(ticker: str, limit: int = 10) -> list[dict]:
    data = _fmp(f"/v3/cash-flow-statement/{ticker}", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected cash flow response for {ticker}: {type(data)}")
    return data


def fetch_income_quarterly(ticker: str, limit: int = 8) -> list[dict]:
    """Last 8 quarters of income data — used for TTM computation (Part 28)."""
    data = _fmp(f"/v3/income-statement/{ticker}", period="quarter", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected quarterly income response for {ticker}")
    return data


def fetch_balance_quarterly(ticker: str, limit: int = 4) -> list[dict]:
    data = _fmp(f"/v3/balance-sheet-statement/{ticker}", period="quarter", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected quarterly balance sheet response for {ticker}")
    return data


def fetch_cashflow_quarterly(ticker: str, limit: int = 8) -> list[dict]:
    data = _fmp(f"/v3/cash-flow-statement/{ticker}", period="quarter", limit=limit)
    if not isinstance(data, list):
        raise DataFetchError(f"Unexpected quarterly cash flow response for {ticker}")
    return data


def fetch_profile(ticker: str) -> dict:
    """Company profile: sector, currency, exchange, market cap, price, beta."""
    data = _fmp(f"/v3/profile/{ticker}")
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    raise DataFetchError(f"Empty profile response for {ticker}. Check the ticker symbol.")


def fetch_ntm_estimates(ticker: str) -> list[dict]:
    """Forward consensus analyst estimates for NTM multiples (Part 37.1)."""
    data = _fmp(f"/v3/analyst-estimates/{ticker}", limit=4)
    return data if isinstance(data, list) else []


def fetch_segment_data(ticker: str) -> dict[str, list[dict]]:
    """
    Product and geographic revenue segments (Part 45.1).
    Returns {"product": [...], "geographic": [...]}.
    """
    product = _fmp(f"/v4/revenue-product-segmentation", symbol=ticker)
    geo     = _fmp(f"/v4/revenue-geographic-segmentation", symbol=ticker)
    return {
        "product":    product    if isinstance(product, list) else [],
        "geographic": geo        if isinstance(geo, list)     else [],
    }


def fetch_sec_filings_8k(ticker: str, limit: int = 20) -> list[dict]:
    """
    Recent SEC 8-K filings — used for pro forma event detection (Part 78.2).
    Returns list of {date, type, url, title}.
    """
    data = _fmp(f"/v3/sec_filings/{ticker}", type="8-K", limit=limit)
    return data if isinstance(data, list) else []


# ─────────────────────────────────────────────────────────────────────────────
# 1B — yfinance data (market price, beta, 52-week range)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yfinance_info(ticker: str) -> dict:
    """
    yfinance .info dict: marketCap, currentPrice, beta,
    fiftyTwoWeekHigh, fiftyTwoWeekLow, regularMarketPreviousClose.
    Reference: Parts 79.2, A.2.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return info
    except Exception as exc:
        raise DataFetchError(f"yfinance fetch failed for {ticker}: {exc}") from exc


def fetch_52wk_range(ticker: str) -> dict[str, float | None]:
    """
    Return {"high_52wk": float, "low_52wk": float, "current_price": float}.
    Primary: yfinance .info. Fallback: 1-year history max/min.
    Reference: Part 79.2.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        high = info.get("fiftyTwoWeekHigh")
        low  = info.get("fiftyTwoWeekLow")
        price = info.get("currentPrice") or info.get("regularMarketPreviousClose")

        # Fallback: compute from 1-year price history
        if high is None or low is None:
            hist = t.history(period="1y")
            if not hist.empty:
                high = float(hist["High"].max())
                low  = float(hist["Low"].min())
                if price is None and not hist.empty:
                    price = float(hist["Close"].iloc[-1])

        return {"high_52wk": high, "low_52wk": low, "current_price": price}
    except Exception as exc:
        raise DataFetchError(f"52-week range fetch failed for {ticker}: {exc}") from exc


def check_price_freshness(ticker: str, max_stale_days: int = PRICE_STALENESS_DAYS) -> dict:
    """
    Verify that yfinance price data is fresh (< max_stale_days trading days old).
    Returns {"fresh": bool, "last_date": str, "days_stale": int}.
    Reference: Part 55.1.
    """
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return {"fresh": False, "last_date": None, "days_stale": 999}
        last_dt = hist.index[-1]
        # Convert to date (handles timezone-aware index)
        if hasattr(last_dt, "date"):
            last_date = last_dt.date()
        else:
            last_date = datetime.utcfromtimestamp(last_dt.timestamp()).date()
        today = date.today()
        delta = (today - last_date).days
        return {
            "fresh":      delta <= max_stale_days,
            "last_date":  last_date.isoformat(),
            "days_stale": delta,
        }
    except Exception:
        return {"fresh": False, "last_date": None, "days_stale": 999}


# ─────────────────────────────────────────────────────────────────────────────
# 1C — FRED (risk-free rate, GDP growth)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_risk_free_rate(series: str = RF_FRED_SERIES) -> float:
    """
    Fetch the latest 10-year Treasury yield from FRED as a decimal.
    Falls back to RF_DEFAULT_FALLBACK if FRED_API_KEY is not set or call fails.
    Reference: Parts 4.3, A.3.
    """
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return RF_DEFAULT_FALLBACK

    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id":      series,
            "api_key":        fred_key,
            "file_type":      "json",
            "sort_order":     "desc",
            "limit":          1,
            "observation_start": (date.today() - timedelta(days=30)).isoformat(),
        }
        data = _get(url, params)
        obs = data.get("observations", [])
        if obs:
            val = float(obs[0]["value"])
            return val / 100.0   # FRED reports in percent
        return RF_DEFAULT_FALLBACK
    except Exception:
        return RF_DEFAULT_FALLBACK


def fetch_gdp_growth_estimate() -> float:
    """
    Fetch trailing nominal US GDP growth rate from FRED (GDP series, annual % change).
    Used as a ceiling for terminal growth rate validation.
    Falls back to 0.04 (4%) if unavailable.
    """
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return 0.04

    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id":  "A191RL1Q225SBEA",  # Real GDP growth (quarterly, SAAR)
            "api_key":    fred_key,
            "file_type":  "json",
            "sort_order": "desc",
            "limit":      4,
        }
        data = _get(url, params)
        obs = data.get("observations", [])
        values = [float(o["value"]) for o in obs if o["value"] != "."]
        if values:
            avg_real = sum(values) / len(values) / 100.0
            # Add ~2% inflation assumption for nominal GDP cap
            return avg_real + 0.02
        return 0.04
    except Exception:
        return 0.04


# ─────────────────────────────────────────────────────────────────────────────
# 1D — Damodaran static data (cached locally)
# ─────────────────────────────────────────────────────────────────────────────

_DAMODARAN_BETA_URL = (
    "http://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/betaUS.csv"
)
_DAMODARAN_ERP_URL = (
    "http://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ctryprem.csv"
)

_DAMODARAN_BETA_FALLBACK: dict[str, float] = {
    "Information Technology":    1.15,
    "Health Care":               0.85,
    "Consumer Discretionary":    0.95,
    "Consumer Staples":          0.65,
    "Industrials":               1.00,
    "Energy":                    1.10,
    "Materials":                 1.05,
    "Utilities":                 0.50,
    "Real Estate":               0.80,
    "Communication Services":    0.90,
    "Financials":                1.00,
    "":                          1.00,   # default
}

_DAMODARAN_ERP_FALLBACK: float = 0.055


def fetch_damodaran_industry_beta(sector: str) -> float:
    """
    Return unlevered beta for the given sector from Damodaran's table.
    Downloads and caches the CSV. Falls back to hardcoded values on failure.
    Reference: Parts 4.3, A.4.
    """
    cache_path = CACHE_DIR / "damodaran_beta.json"
    # Use cache if it exists and is < 365 days old
    if cache_path.exists():
        age_days = (date.today() - date.fromtimestamp(cache_path.stat().st_mtime)).days
        if age_days < 365:
            try:
                with open(cache_path) as fh:
                    table: dict = json.load(fh)
                return _lookup_sector(table, sector)
            except Exception:
                pass

    # Download fresh data
    try:
        import csv, io
        resp = requests.get(_DAMODARAN_BETA_URL, timeout=15)
        resp.raise_for_status()
        table = {}
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            # Column names vary by year — look for "Unlevered beta" or similar
            industry = (row.get("Industry Name") or row.get("Industry") or "").strip()
            beta_raw = None
            for col in row:
                if "unlevered" in col.lower() and "corrected" not in col.lower():
                    try:
                        beta_raw = float(row[col])
                        break
                    except (ValueError, TypeError):
                        pass
            if industry and beta_raw is not None:
                table[industry] = beta_raw
        if table:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as fh:
                json.dump(table, fh)
            return _lookup_sector(table, sector)
    except Exception:
        pass

    return _DAMODARAN_BETA_FALLBACK.get(sector, 1.00)


def _lookup_sector(table: dict[str, float], sector: str) -> float:
    """Fuzzy match sector name → industry beta entry."""
    sector_lower = sector.lower()
    # Direct match
    for key, val in table.items():
        if key.lower() == sector_lower:
            return val
    # Partial match
    for key, val in table.items():
        if sector_lower in key.lower() or key.lower() in sector_lower:
            return val
    return _DAMODARAN_BETA_FALLBACK.get(sector, 1.00)


def fetch_damodaran_erp(country: str = "United States") -> float:
    """
    Return the Equity Risk Premium (and Country Risk Premium if non-US)
    from Damodaran's country premium table.
    Reference: Parts 38, A.4.
    Falls back to ERP_DEFAULT_FALLBACK = 5.5% on failure.
    """
    cache_path = CACHE_DIR / "damodaran_erp.json"
    if cache_path.exists():
        age_days = (date.today() - date.fromtimestamp(cache_path.stat().st_mtime)).days
        if age_days < 365:
            try:
                with open(cache_path) as fh:
                    table: dict = json.load(fh)
                return _lookup_erp(table, country)
            except Exception:
                pass

    try:
        import csv, io
        resp = requests.get(_DAMODARAN_ERP_URL, timeout=15)
        resp.raise_for_status()
        table = {}
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            country_name = (row.get("Country") or "").strip()
            erp_raw = None
            for col in row:
                if "equity risk premium" in col.lower() or "erp" in col.lower():
                    try:
                        raw = row[col].replace("%", "").strip()
                        erp_raw = float(raw)
                        break
                    except (ValueError, TypeError):
                        pass
            if country_name and erp_raw is not None:
                table[country_name] = erp_raw / 100.0
        if table:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as fh:
                json.dump(table, fh)
            return _lookup_erp(table, country)
    except Exception:
        pass

    return _DAMODARAN_ERP_FALLBACK


def _lookup_erp(table: dict[str, float], country: str) -> float:
    country_lower = country.lower()
    for key, val in table.items():
        if key.lower() == country_lower:
            return val
    for key, val in table.items():
        if country_lower in key.lower():
            return val
    return _DAMODARAN_ERP_FALLBACK


# ─────────────────────────────────────────────────────────────────────────────
# Current price (Part O8)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_current_price(ticker: str) -> float | None:
    """
    Return the current market price for *ticker* from yfinance.
    Returns None if unavailable.
    Reference: Architecture Plan Part O8.
    """
    try:
        info = yf.Ticker(ticker).info or {}
        price = info.get("currentPrice") or info.get("regularMarketPreviousClose")
        if price:
            return float(price)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Free cash (unrestricted)  (Part M2)
# ─────────────────────────────────────────────────────────────────────────────

def get_free_cash(
    balance_sheet: dict,
    exclude_restricted: bool = True,
) -> float:
    """
    Return unrestricted cash from a balance sheet dict.

    If exclude_restricted is True, subtracts 'restrictedCash' from
    'cashAndCashEquivalents' to get truly free cash.

    Reference: Architecture Plan Part M2.
    """
    cash = abs(balance_sheet.get("cashAndCashEquivalents") or
               balance_sheet.get("cash") or 0)
    if exclude_restricted:
        restricted = abs(balance_sheet.get("restrictedCash") or 0)
        cash = max(0.0, cash - restricted)
    st_inv = abs(balance_sheet.get("shortTermInvestments") or
                 balance_sheet.get("st_investments") or 0)
    return cash + st_inv


# ─────────────────────────────────────────────────────────────────────────────
# Company-type gating  (Parts M5, 35, 59.2)
# ─────────────────────────────────────────────────────────────────────────────

def is_financial_company(profile: dict) -> bool:
    """
    Return True if the company's GICS sector is 'Financials' or 'Financial Services'.
    UFCF-DCF is not applicable to financial companies.
    Reference: Architecture Plan Part M5.
    """
    sector = (profile.get("sector") or "").strip()
    return sector in ("Financials", "Financial Services")


def gate_company_type(profile: dict) -> str:
    """
    Inspect the company profile and return the company type gate result.

    Returns one of:
      'ok'         — standard UFCF-DCF applies
      'financial'  — Financials/Banks; UFCF-DCF not applicable
      'reit'       — Real Estate; use FFO/AFFO model
      'mining'     — Mining/Resources; NAV model preferred

    Reference: Architecture Plan Parts M5, 59.
    """
    sector   = (profile.get("sector") or "").strip()
    industry = (profile.get("industry") or "").strip().lower()

    if sector in ("Financials", "Financial Services"):
        return "financial"
    if sector == "Real Estate":
        return "reit"
    if sector in ("Energy", "Materials") and any(
        kw in industry for kw in ("mining", "gold", "silver", "copper", "coal", "iron")
    ):
        return "mining"
    return "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Revenue segments  (Part N8)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_revenue_segments(ticker: str) -> dict[str, list[dict]]:
    """
    Fetch product and geographic revenue segments from FMP.
    Alias for fetch_segment_data() with a standardised name.
    Reference: Architecture Plan Part N8.
    """
    return fetch_segment_data(ticker)


# ─────────────────────────────────────────────────────────────────────────────
# IPO recency check  (Part M12d)
# ─────────────────────────────────────────────────────────────────────────────

def check_ipo_recency(
    profile: dict,
    min_years: int = 3,
) -> dict:
    """
    Check whether the company has at least *min_years* of public history.

    Returns {'recent_ipo': bool, 'ipo_date': str, 'years_public': float}.
    'recent_ipo' is True if the company went public fewer than min_years ago.

    FMP profile field: 'ipoDate' (ISO date string 'YYYY-MM-DD').
    Reference: Architecture Plan Part M12d.
    """
    ipo_str = profile.get("ipoDate") or ""
    if not ipo_str:
        return {"recent_ipo": False, "ipo_date": None, "years_public": None}

    try:
        from datetime import date
        ipo_dt = date.fromisoformat(ipo_str[:10])
        years_public = (date.today() - ipo_dt).days / 365.25
        recent = years_public < min_years
        return {
            "recent_ipo":   recent,
            "ipo_date":     ipo_str[:10],
            "years_public": round(years_public, 1),
        }
    except (ValueError, TypeError):
        return {"recent_ipo": False, "ipo_date": ipo_str, "years_public": None}


# ─────────────────────────────────────────────────────────────────────────────
# Ticker input parsing — batch mode  (Part N13)
# ─────────────────────────────────────────────────────────────────────────────

def parse_ticker_input(ticker_str: str) -> list[str]:
    """
    Parse a ticker input string into a list of clean ticker symbols.

    Accepts:
      - Single ticker:  "AAPL"            → ["AAPL"]
      - CSV string:     "AAPL,MSFT,NKE"   → ["AAPL", "MSFT", "NKE"]
      - Path to CSV file: "/path/to/tickers.csv" — reads first column, skips header

    Whitespace and empty entries are stripped.  Duplicates are removed while
    preserving order.

    Reference: Architecture Plan Part N13.
    """
    import os

    # Check if it looks like a file path
    if os.path.isfile(ticker_str):
        tickers: list[str] = []
        with open(ticker_str, encoding="utf-8") as fh:
            for line in fh:
                cell = line.split(",")[0].strip().upper()
                if cell and not cell.startswith("#"):
                    tickers.append(cell)
        # Remove header if first entry looks like a label
        if tickers and tickers[0] in ("TICKER", "SYMBOL", "NAME"):
            tickers = tickers[1:]
    else:
        tickers = [t.strip().upper() for t in ticker_str.split(",") if t.strip()]

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → fetch_income_quarterly
fetch_quarterly_income_statement = fetch_income_quarterly

#: Canonical checklist name → fetch_balance_quarterly
fetch_quarterly_balance_sheet = fetch_balance_quarterly

#: Canonical checklist name → fetch_cashflow_quarterly
fetch_quarterly_cash_flow = fetch_cashflow_quarterly
