"""
data/macro.py — Macro data fetching: FRED risk-free rate, Damodaran ERP/beta/CRP,
size premium lookups.

Reference: Architecture Plan Parts 4.3, 38, 46.3, 71.

All rates as decimals (0.045 = 4.5%).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── FRED series mapping by currency (Part 46.3) ────────────────────────────────
FRED_RF_SERIES: dict[str, str] = {
    "USD": "GS10",    # 10-year US Treasury constant maturity
    "EUR": "IRLTLT01EZM156N",   # 10-year Euro area govt bond
    "GBP": "IRLTLT01GBM156N",   # 10-year UK gilt
    "JPY": "IRLTLT01JPM156N",   # 10-year Japan govt bond
    "CAD": "IRLTLT01CAM156N",   # 10-year Canada govt bond
    "AUD": "IRLTLT01AUM156N",   # 10-year Australia govt bond
    "CHF": "IRLTLT01CHM156N",   # 10-year Switzerland govt bond
    "CNY": "IRLTLT01CNM156N",   # 10-year China govt bond
}

# Fallback risk-free rates if FRED API unavailable
RF_FALLBACKS: dict[str, float] = {
    "USD": 0.045,
    "EUR": 0.030,
    "GBP": 0.040,
    "JPY": 0.010,
    "CAD": 0.038,
    "AUD": 0.042,
    "CHF": 0.010,
    "CNY": 0.028,
}

# Size premium table: Duff & Phelps / Kroll CRSP decile lookup (approximate)
# Market cap in USD millions → size premium in decimal
_SIZE_PREMIUM_TABLE: list[tuple[float, float]] = [
    (200_000, 0.000),   # mega-cap (Decile 1) — no size premium
    (50_000,  0.006),   # large-cap (Decile 2)
    (10_000,  0.012),   # mid-cap (Deciles 3-5)
    (2_000,   0.022),   # small-cap (Deciles 6-8)
    (500,     0.040),   # micro-cap (Decile 9)
    (0,       0.060),   # nano-cap (Decile 10)
]

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".damodaran_cache"
_DAMODARAN_MAX_AGE_DAYS = 90


# ─────────────────────────────────────────────────────────────────────────────
# FRED risk-free rate fetch  (Part 4.3, 38)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_rf_rate_for_currency(
    currency: str = "USD",
    fred_api_key: str | None = None,
) -> float:
    """
    Fetch the current risk-free rate for the given currency from FRED.

    Returns the most recent yield as a decimal (e.g. 0.045 for 4.5%).
    Falls back to hard-coded values if FRED API key is absent or call fails.

    Reference: Architecture Plan Parts 4.3, 38, 46.3.
    """
    currency = currency.upper()
    series_id = FRED_RF_SERIES.get(currency, "GS10")
    fallback = RF_FALLBACKS.get(currency, 0.045)

    if not fred_api_key:
        fred_api_key = os.getenv("FRED_API_KEY", "")

    if not fred_api_key:
        return fallback

    try:
        import requests
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={fred_api_key}"
            f"&file_type=json&sort_order=desc&limit=1"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        obs = data.get("observations", [])
        if obs:
            value_str = obs[0].get("value", "")
            if value_str and value_str != ".":
                return float(value_str) / 100.0   # FRED returns percentages
    except Exception:
        pass

    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Size premium lookup  (Part 38.1)
# ─────────────────────────────────────────────────────────────────────────────

def compute_size_premium(market_cap_usd_mm: float) -> float:
    """
    Duff & Phelps / Kroll CRSP decile size premium lookup.

    market_cap_usd_mm: market capitalisation in USD millions.
    Returns size premium as decimal (e.g. 0.012 for 1.2%).

    Reference: Architecture Plan Part 38.1.
    """
    if market_cap_usd_mm is None or market_cap_usd_mm <= 0:
        return 0.0
    for threshold, premium in _SIZE_PREMIUM_TABLE:
        if market_cap_usd_mm >= threshold:
            return premium
    return _SIZE_PREMIUM_TABLE[-1][1]


# ─────────────────────────────────────────────────────────────────────────────
# Country Risk Premium  (Part 38.2)
# ─────────────────────────────────────────────────────────────────────────────

# Approximate CRP table from Damodaran (January 2024 update)
# Country ISO code → CRP decimal
_CRP_TABLE: dict[str, float] = {
    "US": 0.000, "GB": 0.000, "DE": 0.000, "FR": 0.001, "JP": 0.001,
    "CA": 0.000, "AU": 0.000, "CH": 0.000, "NL": 0.000, "SE": 0.000,
    "KR": 0.005, "TW": 0.005, "SG": 0.000, "HK": 0.005,
    "CN": 0.008, "IN": 0.012, "BR": 0.020, "MX": 0.015, "ZA": 0.025,
    "RU": 0.045, "TR": 0.040, "AR": 0.060, "NG": 0.050, "EG": 0.040,
    "ID": 0.018, "MY": 0.010, "TH": 0.012, "PH": 0.018, "VN": 0.025,
    "CL": 0.012, "CO": 0.020, "PE": 0.015, "PL": 0.005, "CZ": 0.003,
    "HU": 0.008, "RO": 0.010, "GR": 0.015, "IT": 0.006, "ES": 0.003,
    "PT": 0.005, "IE": 0.001, "BE": 0.001, "AT": 0.000, "NO": 0.000,
    "DK": 0.000, "FI": 0.000,
}


def compute_crp(
    country_iso2: str | None = None,
    exchange_country: str | None = None,
) -> float:
    """
    Damodaran country risk premium lookup.

    country_iso2: 2-letter ISO country code (e.g. 'BR' for Brazil).
    exchange_country: exchange country from FMP profile as fallback.

    Returns CRP as decimal (0.0 for developed markets, positive for EM).
    Reference: Architecture Plan Part 38.2.
    """
    code = (country_iso2 or exchange_country or "US").upper()
    # Handle some common exchange country strings from FMP
    _country_map = {
        "UNITED STATES": "US",
        "USA": "US",
        "UNITED KINGDOM": "GB",
        "UK": "GB",
        "GERMANY": "DE",
        "FRANCE": "FR",
        "JAPAN": "JP",
        "CANADA": "CA",
        "AUSTRALIA": "AU",
        "CHINA": "CN",
        "INDIA": "IN",
        "BRAZIL": "BR",
        "SOUTH AFRICA": "ZA",
        "SOUTH KOREA": "KR",
    }
    code = _country_map.get(code, code)
    return _CRP_TABLE.get(code, 0.000)


# ─────────────────────────────────────────────────────────────────────────────
# Damodaran ERP and industry beta  (Part 4.3, A.4)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_damodaran_erp(
    max_age_days: int = _DAMODARAN_MAX_AGE_DAYS,
) -> float:
    """
    Return the current Damodaran implied ERP (US market).

    Tries to load from local cache first (max_age_days TTL).
    Falls back to default ERP_DEFAULT = 5.5% if unavailable.

    Reference: Architecture Plan Part 4.3.
    """
    cache_path = _CACHE_DIR / "damodaran_erp.json"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Check cache
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            fetched = datetime.fromisoformat(cached.get("fetched", "2000-01-01"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched < timedelta(days=max_age_days):
                return float(cached["erp"])
        except Exception:
            pass

    # Damodaran's public ERP data — direct CSV not always available via API
    # Return module-level default; callers may override via env/config
    erp_default = 0.055
    try:
        erp_env = os.getenv("DAMODARAN_ERP", "")
        if erp_env:
            erp_default = float(erp_env)
    except (ValueError, TypeError):
        pass

    # Persist to cache
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({"erp": erp_default, "fetched": datetime.now(timezone.utc).isoformat()}, fh)
    except Exception:
        pass

    return erp_default


def fetch_damodaran_industry_beta(
    sector: str,
    max_age_days: int = _DAMODARAN_MAX_AGE_DAYS,
) -> float | None:
    """
    Return the unlevered industry beta for `sector` from the Damodaran
    industry beta table.  Returns None if sector not found.

    The table is cached locally; falls back to None if unavailable.
    Reference: Architecture Plan Part 4.3, A.4.
    """
    # Approximate Damodaran industry unlevered betas (January 2024)
    _INDUSTRY_UNLEV_BETA: dict[str, float] = {
        "Information Technology": 0.92,
        "Technology":             0.92,
        "Software":               1.02,
        "Semiconductors":         1.15,
        "Health Care":            0.78,
        "Pharmaceuticals":        0.72,
        "Biotechnology":          1.10,
        "Consumer Discretionary": 0.85,
        "Consumer Staples":       0.52,
        "Financials":             0.40,
        "Industrials":            0.72,
        "Materials":              0.80,
        "Energy":                 0.75,
        "Utilities":              0.30,
        "Real Estate":            0.55,
        "Communication Services": 0.85,
        "Retail":                 0.75,
        "Airlines":               0.90,
        "Automotive":             0.80,
    }

    cache_path = _CACHE_DIR / "damodaran_industry_betas.json"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Check cache for full table
    table = {}
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            fetched = datetime.fromisoformat(cached.get("fetched", "2000-01-01"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched < timedelta(days=max_age_days):
                table = cached.get("betas", {})
        except Exception:
            pass

    if not table:
        table = _INDUSTRY_UNLEV_BETA
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"betas": table, "fetched": datetime.now(timezone.utc).isoformat()}, fh)
        except Exception:
            pass

    # Fuzzy sector lookup
    sector_clean = (sector or "").strip()
    if sector_clean in table:
        return float(table[sector_clean])

    # Partial match
    for key, val in table.items():
        if sector_clean.lower() in key.lower() or key.lower() in sector_clean.lower():
            return float(val)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cash-adjusted unlevered beta  (Part 71 — Damodaran)
# ─────────────────────────────────────────────────────────────────────────────

def compute_unlevered_beta_cash_adjusted(
    unlevered_beta: float,
    cash_mm: float,
    firm_value_mm: float,
) -> tuple[float, float]:
    """
    Damodaran cash-adjusted unlevered beta.

    Unlev_adj = Unlev_beta / (1 - Cash / Firm_Value)

    Corrects for the fact that cash has beta ≈ 0, so the operating-asset
    beta is higher than the raw unlevered beta.

    Returns (unlev_beta_adj, cash_pct_of_firm_value).
    Reference: Architecture Plan Part 71.
    """
    if firm_value_mm is None or firm_value_mm <= 0:
        return unlevered_beta, 0.0

    cash_pct = min(cash_mm / firm_value_mm, 0.90)
    denominator = 1.0 - cash_pct

    if denominator < 0.10:
        return unlevered_beta, cash_pct

    return unlevered_beta / denominator, cash_pct


# ─────────────────────────────────────────────────────────────────────────────
# Total beta for private company valuation  (Part 73 — Damodaran)
# ─────────────────────────────────────────────────────────────────────────────

def compute_total_beta(
    market_beta: float,
    correlation_with_market: float,
) -> float:
    """
    Damodaran total beta = Market_Beta / Correlation.

    For an undiversified private-company owner, total beta is a better
    measure of risk than standard market beta.

    correlation_with_market: R (not R²), in range (0, 1].
    Reference: Architecture Plan Part 73.
    """
    if not (0 < correlation_with_market <= 1):
        raise ValueError(
            f"correlation_with_market must be in (0, 1], got {correlation_with_market}"
        )
    return market_beta / correlation_with_market


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → compute_size_premium
fetch_size_premium = compute_size_premium

#: Canonical checklist name → compute_crp
fetch_crp = compute_crp
