"""
data/fetcher.py — All market data fetching: FMP, EODHD, FRED, Damodaran.

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
# 1B — EODHD market data (market price, beta, 52-week range)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_market_info(ticker: str) -> dict:
    """Return basic market data from EODHD fund cache + real-time endpoint."""
    info = fetch_eodhd_info(ticker)
    if info:
        return info
    raise DataFetchError(f"No EODHD market data available for {ticker}")


def fetch_eodhd_info(ticker: str) -> dict:
    """Build a market-info dict from EODHD fund cache + real-time price."""
    try:
        from webapp.data import eodhd_client as _eod
        code = _eod._eodhd_code(ticker)
        fund = _eod._fetch_fundamentals(code) or {}
        price = _eod._fetch_price(code) or {}
    except Exception:
        return {}

    if not fund:
        return {}

    gen = fund.get("General", {}) or {}
    hi = fund.get("Highlights", {}) or {}
    tech = fund.get("Technicals", {}) or {}
    share = fund.get("SharesStats", {}) or {}

    info: dict = {
        "shortName": gen.get("Name"),
        "longName": gen.get("Name"),
        "sector": gen.get("Sector"),
        "industry": gen.get("Industry"),
        "country": gen.get("CountryName") or gen.get("Country"),
        "currency": gen.get("CurrencyCode"),
        "exchange": gen.get("Exchange"),
        "marketCap": hi.get("MarketCapitalization"),
        "beta": tech.get("Beta"),
        "fiftyTwoWeekHigh": tech.get("52WeekHigh"),
        "fiftyTwoWeekLow": tech.get("52WeekLow"),
        "sharesOutstanding": share.get("SharesOutstanding"),
        "floatShares": share.get("SharesFloat"),
        "trailingPE": hi.get("PERatio"),
        "forwardPE": hi.get("ForwardPE"),
        "dividendYield": hi.get("DividendYield"),
        "ebitda": hi.get("EBITDA"),
        "totalRevenue": hi.get("RevenueTTM"),
        "profitMargins": hi.get("ProfitMargin"),
        "ipoDate": gen.get("IPODate"),
    }
    if isinstance(price, dict):
        close = price.get("close")
        if close not in (None, "NA"):
            info["currentPrice"] = close
            info["regularMarketPreviousClose"] = price.get("previousClose") or close
    return {k: v for k, v in info.items() if v is not None}


def fetch_52wk_range(ticker: str) -> dict[str, float | None]:
    """Return ``{high_52wk, low_52wk, current_price}`` via EODHD."""
    info = fetch_eodhd_info(ticker)
    if info:
        high = info.get("fiftyTwoWeekHigh")
        low = info.get("fiftyTwoWeekLow")
        price = info.get("currentPrice") or info.get("regularMarketPreviousClose")

        if high is None or low is None:
            try:
                from datetime import date as _date, timedelta as _td
                from webapp.data import eodhd_client as _eod
                code = _eod._eodhd_code(ticker)
                series = _eod.fetch_historical_price_series(
                    code,
                    start_date=_date.today() - _td(days=400),
                    end_date=_date.today(),
                )
                closes = [float(p["adjusted_close"]) for p in series if p.get("adjusted_close")]
                if closes:
                    high = high or max(closes)
                    low = low or min(closes)
                    price = price or closes[-1]
            except Exception:
                pass

        if high is not None and low is not None:
            return {"high_52wk": high, "low_52wk": low, "current_price": price}
    raise DataFetchError(f"52-week range fetch failed for {ticker}: no EODHD data")


def check_price_freshness(ticker: str, max_stale_days: int = PRICE_STALENESS_DAYS) -> dict:
    """Check price freshness via EODHD real-time endpoint."""
    try:
        from webapp.data import eodhd_client as _eod
        code = _eod._eodhd_code(ticker)
        price = _eod._fetch_price(code) or {}
        ts = price.get("timestamp")
        if ts:
            last_date = datetime.utcfromtimestamp(int(ts)).date()
            delta = (date.today() - last_date).days
            return {
                "fresh": delta <= max_stale_days,
                "last_date": last_date.isoformat(),
                "days_stale": delta,
            }
    except Exception:
        pass
    return {"fresh": False, "last_date": None, "days_stale": 999}


# ─────────────────────────────────────────────────────────────────────────────
# 1C — FRED (risk-free rate, GDP growth)
# ─────────────────────────────────────────────────────────────────────────────

# EODHD macro context (cached for 30 days; replaces FRED for non-US tickers).
_MACRO_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_MACRO_TTL_SEC = 60 * 60 * 24 * 30  # 30 days


def fetch_macro_context_eodhd(country_iso3: str = "USA") -> dict[str, float]:
    """Country-specific macro context from EODHD ``/macro-indicator``.

    Returns dict with possible keys:
      ``risk_free_rate``      — real_interest_rate (decimal, e.g. 0.025)
      ``gdp_growth_real``     — real GDP growth (decimal)
      ``gdp_growth_nominal``  — real + inflation_consumer_prices_annual
      ``inflation``           — CPI inflation (decimal)

    Cached in-process for 30 days. Returns ``{}`` on any failure.
    """
    api_key = (
        os.getenv("EODHD_API_KEY", "").strip()
        or os.getenv("EOD_API_KEY", "").strip()
    )
    if not api_key:
        return {}

    iso = (country_iso3 or "USA").upper()
    cached = _MACRO_CACHE.get(iso)
    if cached and (time.time() - cached[0]) < _MACRO_TTL_SEC:
        return dict(cached[1])

    def _latest(indicator: str) -> float | None:
        try:
            r = requests.get(
                f"https://eodhistoricaldata.com/api/macro-indicator/{iso}",
                params={"indicator": indicator, "api_token": api_key, "fmt": "json"},
                timeout=12,
            )
            r.raise_for_status()
            data = r.json() or []
            if not isinstance(data, list) or not data:
                return None
            sorted_rows = sorted(
                (row for row in data if isinstance(row, dict) and row.get("Value") not in (None, "")),
                key=lambda row: str(row.get("Date") or ""),
                reverse=True,
            )
            if not sorted_rows:
                return None
            return float(sorted_rows[0]["Value"])
        except Exception:
            return None

    real_rate_pct = _latest("real_interest_rate")
    gdp_real_pct = _latest("gdp_growth_annual")
    cpi_pct = _latest("inflation_consumer_prices_annual")

    out: dict[str, float] = {}
    if real_rate_pct is not None:
        out["risk_free_rate"] = real_rate_pct / 100.0
    if gdp_real_pct is not None:
        out["gdp_growth_real"] = gdp_real_pct / 100.0
    if cpi_pct is not None:
        out["inflation"] = cpi_pct / 100.0
    if "gdp_growth_real" in out:
        out["gdp_growth_nominal"] = out["gdp_growth_real"] + out.get("inflation", 0.02)

    if out:
        _MACRO_CACHE[iso] = (time.time(), out)
    return out


def fetch_risk_free_rate(series: str = RF_FRED_SERIES) -> float:
    """Risk-free rate. Priority: EODHD macro (any country) → FRED → fallback.
    Reference: Parts 4.3, A.3 + ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S1).
    """
    try:
        ctx = fetch_macro_context_eodhd("USA")
        rf = ctx.get("risk_free_rate")
        if rf is not None:
            return float(rf)
    except Exception:
        pass

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
    """Trailing nominal GDP growth (US default).
    Priority: EODHD macro indicator → FRED → 4.0% fallback.
    Reference: Architecture Plan + ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S1).
    """
    try:
        ctx = fetch_macro_context_eodhd("USA")
        g = ctx.get("gdp_growth_nominal")
        if g is not None:
            return float(g)
    except Exception:
        pass

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


def fetch_eodhd_beta(
    ticker: str,
    *,
    benchmark: str = "GSPC.INDX",
    lookback_days: int = 365 * 2,
) -> float | None:
    """Compute equity beta by regressing ticker daily returns against a benchmark.

    Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S5).
    Uses cached EODHD daily history; returns ``None`` on insufficient data.
    """
    try:
        from datetime import date as _date, timedelta as _td
        from webapp.data import eodhd_client as _eod
    except Exception:
        return None
    try:
        code = _eod._eodhd_code(ticker)
        end = _date.today()
        start = end - _td(days=lookback_days)
        stock = _eod.fetch_historical_price_series(code, start_date=start, end_date=end) or []
        bench = _eod.fetch_historical_price_series(benchmark, start_date=start, end_date=end) or []
    except Exception:
        return None

    def _series(series: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in series:
            d = str(row.get("date") or "")
            c = row.get("adjusted_close")
            if d and c not in (None, ""):
                try:
                    out[d] = float(c)
                except (TypeError, ValueError):
                    continue
        return out

    s_map = _series(stock)
    b_map = _series(bench)
    common = sorted(set(s_map) & set(b_map))
    if len(common) < 60:
        return None
    s_returns: list[float] = []
    b_returns: list[float] = []
    for prev, curr in zip(common[:-1], common[1:]):
        sp, sc = s_map[prev], s_map[curr]
        bp, bc = b_map[prev], b_map[curr]
        if sp <= 0 or bp <= 0:
            continue
        s_returns.append((sc / sp) - 1.0)
        b_returns.append((bc / bp) - 1.0)
    n = len(s_returns)
    if n < 60:
        return None
    mean_b = sum(b_returns) / n
    mean_s = sum(s_returns) / n
    cov = sum((b - mean_b) * (s - mean_s) for b, s in zip(b_returns, s_returns)) / n
    var = sum((b - mean_b) ** 2 for b in b_returns) / n
    if var <= 0:
        return None
    beta = cov / var
    return round(max(0.1, min(5.0, beta)), 3)


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
    """Return the current market price via EODHD real-time."""
    try:
        from webapp.data import eodhd_client as _eod
        code = _eod._eodhd_code(ticker)
        price = _eod._fetch_price(code) or {}
        close = price.get("close")
        if close not in (None, "NA"):
            return float(close)
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
