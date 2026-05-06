"""
webapp/data/eodhd_client.py
───────────────────────────
EODHD (End of Day Historical Data) API client.

Provides up to 20+ years of audited annual financial history — far more
than yfinance's 4-year limit.

API key:  691aca08424c26.36039280  (default; override with EODHD_API_KEY env var)
Base URL: https://eodhd.com/api

Endpoints used:
  GET /real-time/{TICKER}.US?api_token=…&fmt=json     → live price   (5-min  cache)
  GET /fundamentals/{TICKER}.US?api_token=…&fmt=json  → fundamentals (6-hour cache)

Returns the same dict schema as yfinance_client.build_dashboard_data()
so it is a drop-in replacement in the data pipeline.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from statistics import median
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.learning.confidence import build_ranked_confidence_model

# ── API configuration ─────────────────────────────────────────────────────────
_EODHD_KEY_DEFAULT = "691aca08424c26.36039280"
_EODHD_BASE        = "https://eodhd.com/api"

# ── Disk-cache directory ──────────────────────────────────────────────────────
def _resolve_cache_dir() -> Path | None:
    candidates = [
        Path(__file__).parent / "cache",
        Path(tempfile.gettempdir()) / "nelix-capital-cache",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    logger.warning("EODHD disk cache disabled: no writable cache directory available.")
    return None


_CACHE_DIR = _resolve_cache_dir()

# Cache TTLs
# Price: 5 min (needs to stay fresh — users check live quotes)
# Fundamentals: 24 h — income/balance sheets only change at quarterly earnings
# EOD history: 7 days — historical price series barely moves day-to-day
_TTL_PRICE_SEC       = 300          # 5  minutes
_TTL_FUND_SEC        = 86_400       # 24 hours
_TTL_EOD_HISTORY_SEC = 86_400 * 7  # 7  days

# ── In-memory L1 cache (process-level, survives repeated requests) ────────────
# Entries: {name: (data, expires_at_monotonic_sec)}
# Checked before disk; avoids JSON round-trip on repeated page loads.
import time as _time
_MEM_CACHE: dict[str, tuple[Any, float]] = {}


def _macro_regime_from_rf(rf_rate_decimal: float) -> str:
    """Classify the macro regime from the risk-free rate (decimal, e.g. 0.045)."""
    r = float(rf_rate_decimal or 0.0)
    if r >= 0.045:
        return "rising_rates"
    if r <= 0.020:
        return "low_rates"
    return "neutral"

# ── Optional dependency check ─────────────────────────────────────────────────
try:
    import requests as _req
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False
    logger.warning("requests not installed — EODHD client unavailable.")

try:
    from webapp.data.peer_lists import get_peers_for_ticker, fetch_peer_metrics
    _PEERS_AVAILABLE = True
except ImportError:
    try:
        from peer_lists import get_peers_for_ticker, fetch_peer_metrics
        _PEERS_AVAILABLE = True
    except ImportError:
        _PEERS_AVAILABLE = False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _api_key() -> str:
    key = os.environ.get("EODHD_API_KEY") or os.environ.get("EOD_API_KEY") or ""
    if not key:
        if os.environ.get("VERCEL"):
            raise RuntimeError(
                "EODHD_API_KEY environment variable is required in production (Vercel). "
                "Set it in the Vercel project settings."
            )
        return _EODHD_KEY_DEFAULT
    return key


def _sf(val: Any, default: float = 0.0) -> float:
    """Safely convert an EODHD string/number field to float."""
    if val is None or val == "" or val == "None":
        return default
    try:
        f = float(val)
        return default if math.isnan(f) or math.isinf(f) else f
    except (ValueError, TypeError):
        return default


def _cache_path(name: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    return _CACHE_DIR / f"eodhd_{name}.json"


def _cache_read(name: str, ttl_sec: int) -> Any | None:
    # ── L1: in-memory ────────────────────────────────────────────────────
    entry = _MEM_CACHE.get(name)
    if entry is not None:
        data_cached, expires_at = entry
        if _time.monotonic() < expires_at:
            return data_cached
        del _MEM_CACHE[name]

    # ── L2: disk ─────────────────────────────────────────────────────────
    p = _cache_path(name)
    if p is None or not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            obj = json.load(f)
        ts = datetime.fromisoformat(obj.get("_ts", "2000-01-01"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_sec < ttl_sec:
            data = obj.get("data")
            # Promote to L1 for the remaining TTL
            _MEM_CACHE[name] = (data, _time.monotonic() + (ttl_sec - age_sec))
            return data
    except Exception:
        pass
    return None


def _cache_write(name: str, data: Any, ttl_sec: int = 0) -> None:
    # Write to L1 immediately
    if ttl_sec > 0:
        _MEM_CACHE[name] = (data, _time.monotonic() + ttl_sec)
    # Write to L2 (disk)
    p = _cache_path(name)
    if p is None:
        return
    try:
        p.write_text(
            json.dumps({"_ts": datetime.now(timezone.utc).isoformat(), "data": data},
                       ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("EODHD cache write failed: %s", e)


def _shares_to_millions(val: Any) -> float:
    shares = _sf(val)
    return shares / 1e6 if shares > 1e6 else shares


def _statutory_tax_rate(country: str) -> float:
    normalized = (country or "").strip().lower()
    rates = {
        "united states": 25.0,
        "us": 25.0,
        "usa": 25.0,
        "japan": 30.0,
        "united kingdom": 25.0,
        "uk": 25.0,
        "germany": 30.0,
        "france": 25.0,
        "canada": 26.5,
        "china": 25.0,
        "india": 25.0,
        "australia": 30.0,
        "switzerland": 19.0,
        "netherlands": 25.8,
        "ireland": 12.5,
        "singapore": 17.0,
    }
    return rates.get(normalized, 25.0)


def _attributable_earnings_adjustment(
    net_incomes: list[float],
    ebits: list[float],
) -> tuple[float, str | None]:
    """Infer when consolidated operating earnings materially overstate common equity economics.

    Some holding-company or controlled-subsidiary structures report 100% of revenue/EBIT
    while only a minority of that economics belongs to common shareholders. When the gap
    between EBIT and net income is both large and persistent, scale projected UFCF by the
    historical attributable conversion ratio to avoid capitalising non-attributable earnings.
    """

    ratios: list[float] = []
    for net_income, ebit in zip(net_incomes, ebits):
        ebit_value = float(ebit or 0.0)
        net_income_value = float(net_income or 0.0)
        if ebit_value <= 0 or net_income_value <= 0:
            continue
        ratios.append(max(0.05, min(1.0, net_income_value / ebit_value)))

    if len(ratios) < 3:
        return 1.0, None

    recent = ratios[-5:]
    factor = float(median(recent))
    dispersion = max(recent) - min(recent)
    if factor >= 0.45 or dispersion > 0.18:
        return 1.0, None

    return round(max(0.2, min(0.45, factor)), 4), (
        f"Historical net income averages {factor * 100:.1f}% of EBIT, so projected UFCF "
        "is scaled to common-shareholder economics."
    )


def _derive_ebit_margin_target(
    ebit_margin_base_pct: float,
    ebit_margins: list[float],
    gross_margin_base_pct: float,
    industry: str,
    *,
    revenues: list[float] | None = None,
) -> tuple[float, str]:
    current_margin = float(ebit_margin_base_pct or 0.0)
    history = [float(value) for value in ebit_margins if value is not None]
    industry_lower = (industry or "").lower()
    revenue_history = [float(value) for value in (revenues or []) if value is not None and float(value) > 0]

    if history:
        ordered = sorted(history)
        peak_index = max(0, int(len(ordered) * 0.75) - 1)
        historical_anchor = ordered[peak_index]
    else:
        historical_anchor = current_margin

    organic_expansion = round(min(6.0, max(0.0, current_margin) * 0.40), 1)
    recent_revenue_cagr_pct = None
    if len(revenue_history) >= 3 and revenue_history[0] > 0 and revenue_history[-1] > 0:
        recent_revenues = revenue_history[-5:] if len(revenue_history) > 5 else revenue_history
        periods = len(recent_revenues) - 1
        if periods > 0 and recent_revenues[0] > 0:
            recent_revenue_cagr_pct = ((recent_revenues[-1] / recent_revenues[0]) ** (1.0 / periods) - 1.0) * 100.0
    if current_margin >= historical_anchor:
        organic_expansion = min(organic_expansion, 1.0)
    if recent_revenue_cagr_pct is not None and recent_revenue_cagr_pct <= 0:
        organic_expansion = min(organic_expansion, 1.5 if recent_revenue_cagr_pct > -2.0 else 0.8)
    peak_based_target = round(
        min(
            current_margin + 5.0,
            max(
                current_margin,
                current_margin + (historical_anchor - current_margin) * 0.6,
            ),
        ),
        1,
    )
    target = max(peak_based_target, current_margin + organic_expansion)
    source = "Historical peak margin + organic expansion"
    if organic_expansion <= 1.0 and current_margin >= historical_anchor:
        source = "Historical peak margin + restrained expansion"

    recent_window = history[-5:]
    prior_window = history[:-5]
    recent_profitable = [value for value in recent_window if value > 0]
    had_unprofitable_history = any(value <= 0 for value in prior_window)
    if current_margin > 0 and len(recent_profitable) >= 4 and had_unprofitable_history:
        recent_profitable_mean = sum(recent_profitable) / len(recent_profitable)
        regime_lift = min(1.0, max(0.0, recent_profitable_mean - current_margin) * 0.2)
        regime_target = round(recent_profitable_mean + regime_lift, 1)
        target = max(target, regime_target)
        source = "Recent profitable regime + historical anchor"

    if gross_margin_base_pct > 0:
        target = min(target, gross_margin_base_pct)
    if "software" not in industry_lower:
        target = min(45.0, target)

    return round(max(current_margin, target), 1), source


def _historical_ebit_margin_anchor(ebit_margins: list[float], fallback: float) -> float:
    history = [float(value) for value in ebit_margins if value is not None]
    if not history:
        return float(fallback or 0.0)
    ordered = sorted(history)
    peak_index = max(0, int(len(ordered) * 0.75) - 1)
    return round(ordered[peak_index], 1)


_EODHD_SKIP_CODES = frozenset({402, 403, 404})


def _get(endpoint: str, params: dict | None = None) -> Any | None:
    """HTTP GET against EODHD API. Returns parsed JSON or None on failure.
    Returns the sentinel string '__skip__' when the API responds with a
    non-retryable status code (402 Payment Required, 403 Forbidden, 404 Not Found).
    """
    if not _REQUESTS_OK:
        return None
    p = {"api_token": _api_key(), "fmt": "json", **(params or {})}
    url = f"{_EODHD_BASE}/{endpoint}"
    try:
        r = _req.get(url, params=p, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        try:
            import requests as _rmod
            if isinstance(exc, _rmod.HTTPError) and exc.response is not None:
                if exc.response.status_code in _EODHD_SKIP_CODES:
                    logger.warning("EODHD GET %s failed: %s", endpoint, exc)
                    return "__skip__"
        except Exception:
            pass
        logger.warning("EODHD GET %s failed: %s", endpoint, exc)
        return None


def _fetch_price(eodhd_code: str) -> dict | None:
    """Fetch real-time quote. Returns raw dict or None."""
    cache_key = f"price_{eodhd_code.replace('.','_')}"
    cached = _cache_read(cache_key, _TTL_PRICE_SEC)
    if cached:
        return cached
    data = _get(f"real-time/{eodhd_code}")
    if data and isinstance(data, dict) and data.get("close"):
        _cache_write(cache_key, data, ttl_sec=_TTL_PRICE_SEC)
        return data
    return None


_FUND_MISS_SENTINEL = "__not_available__"


def _fetch_fundamentals(eodhd_code: str) -> dict | None:
    """Fetch full fundamentals JSON. Returns raw dict or None.
    Failed 402/403/404 responses are cached for 24 h so they are not retried.
    """
    cache_key = f"fund_{eodhd_code.replace('.','_')}"
    cached = _cache_read(cache_key, _TTL_FUND_SEC)
    if cached == _FUND_MISS_SENTINEL:
        return None
    if cached and isinstance(cached, dict):
        return cached
    data = _get(f"fundamentals/{eodhd_code}")
    if data and isinstance(data, dict) and data.get("General"):
        _cache_write(cache_key, data, ttl_sec=_TTL_FUND_SEC)
        return data
    # Cache the miss so this ticker is not retried for 24 h
    _cache_write(cache_key, _FUND_MISS_SENTINEL, ttl_sec=_TTL_FUND_SEC)
    return None


def fetch_historical_price_series(
    eodhd_code: str,
    *,
    start_date: date | str,
    end_date: date | str,
    use_adjusted_close: bool = True,
) -> list[dict[str, Any]]:
    """Fetch cached daily EOD history for a date range, sorted ascending by date."""
    start_text = str(start_date)[:10]
    end_text = str(end_date)[:10]
    price_field = "adjusted_close" if use_adjusted_close else "close"
    cache_key = (
        f"eod_{eodhd_code.replace('.','_')}_{start_text}_{end_text}_"
        f"{'adj' if use_adjusted_close else 'raw'}"
    )
    cached = _cache_read(cache_key, _TTL_EOD_HISTORY_SEC)
    if isinstance(cached, list):
        return sorted((dict(item) for item in cached if isinstance(item, dict)), key=lambda item: str(item.get("date") or ""))

    try:
        from auto_valuation.learning.background_runner import _EODHD_RATE_LIMITER
        _EODHD_RATE_LIMITER.acquire()
    except Exception:
        pass

    payload = _get(
        f"eod/{eodhd_code}",
        {
            "from": start_text,
            "to": end_text,
            "period": "d",
            "order": "a",
        },
    )
    if not isinstance(payload, list):
        return []

    history: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("date"):
            continue
        close_value = _sf(item.get(price_field), default=float("nan"))
        if math.isnan(close_value) or close_value <= 0:
            close_value = _sf(item.get("close"), default=0.0)
        if close_value <= 0:
            continue
        history.append(
            {
                "date": str(item.get("date"))[:10],
                "close": close_value,
                "raw_close": _sf(item.get("close"), default=close_value),
                "open": _sf(item.get("open"), default=0.0),
                "high": _sf(item.get("high"), default=0.0),
                "low": _sf(item.get("low"), default=0.0),
                "volume": int(_sf(item.get("volume"), default=0.0)),
                "source_field": price_field,
            }
        )

    if history:
        _cache_write(cache_key, history, ttl_sec=_TTL_EOD_HISTORY_SEC)
    return history


# ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S6) — screener-based peer discovery.
_TTL_SCREENER_SEC = 86_400 * 7  # 7 days


def fetch_screener_peers(
    *,
    sector: str | None = None,
    industry: str | None = None,
    exchange: str | None = None,
    min_market_cap_mm: float = 100.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Discover companies that match the given sector/industry filter via EODHD's
    /screener endpoint. Returns a list of {code, name, market_cap, exchange}
    dicts ranked by market cap. Cached for 7 days to conserve API quota."""
    if not (sector or industry):
        return []
    key_bits = [
        f"sector={sector or ''}",
        f"industry={industry or ''}",
        f"exch={exchange or ''}",
        f"cap={int(min_market_cap_mm)}",
        f"lim={int(limit)}",
    ]
    cache_key = "screener_" + "_".join(b.replace("/", "_").replace(" ", "_") for b in key_bits)
    cached = _cache_read(cache_key, _TTL_SCREENER_SEC)
    if cached:
        return list(cached)

    filters: list[list[Any]] = []
    if sector:
        filters.append(["sector", "=", sector])
    if industry:
        filters.append(["industry", "=", industry])
    if exchange:
        filters.append(["exchange", "=", exchange])
    if min_market_cap_mm > 0:
        filters.append(["market_capitalization", ">", int(min_market_cap_mm * 1_000_000)])

    params = {
        "filters": json.dumps(filters),
        "sort": "market_capitalization.desc",
        "limit": int(max(1, min(100, limit))),
    }
    data = _get("screener", params=params)
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        items = data.get("data") or []
        if isinstance(items, list):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                rows.append({
                    "code":       str(entry.get("code") or ""),
                    "name":       str(entry.get("name") or ""),
                    "exchange":   str(entry.get("exchange") or ""),
                    "sector":     str(entry.get("sector") or ""),
                    "industry":   str(entry.get("industry") or ""),
                    "market_cap": _sf(entry.get("market_capitalization")) or 0.0,
                })
    if rows:
        _cache_write(cache_key, rows, ttl_sec=_TTL_SCREENER_SEC)
    return rows


# ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (F1) — news-sentiment signal.
_TTL_SENTIMENT_SEC = 86_400  # 1 day


def fetch_news_sentiment(
    ticker: str,
    *,
    window_days: int = 30,
) -> dict[str, Any]:
    """Return aggregated EODHD news-sentiment for ``ticker`` over the last
    ``window_days`` calendar days.  Schema:
        {
          "sentiment_avg":   float in [-1, 1] or None,
          "article_count":   int,
          "window_days":     int,
          "label":           "bullish" | "neutral" | "bearish" | None,
        }
    Returns an empty dict on API failure so callers can degrade gracefully.
    """
    if not ticker:
        return {}
    code = _eodhd_code(ticker)
    cache_key = f"sent_{code.replace('.', '_')}_{int(window_days)}"
    cached = _cache_read(cache_key, _TTL_SENTIMENT_SEC)
    if cached:
        return cached

    from datetime import date as _date, timedelta as _td
    end = _date.today()
    start = end - _td(days=int(window_days))
    data = _get(
        "sentiments",
        params={"s": code, "from": start.isoformat(), "to": end.isoformat()},
    )
    if not isinstance(data, dict):
        return {}
    series = data.get(code) or next(iter(data.values()), [])
    if not isinstance(series, list) or not series:
        return {}

    scores: list[float] = []
    counts: list[int] = []
    for row in series:
        if not isinstance(row, dict):
            continue
        n = int(_sf(row.get("count"), default=0) or 0)
        s = _sf(row.get("normalized"))
        if s is None or n <= 0:
            continue
        scores.append(float(s) * n)
        counts.append(n)
    total_count = sum(counts)
    if total_count == 0:
        result: dict[str, Any] = {
            "sentiment_avg": None,
            "article_count": 0,
            "window_days":   int(window_days),
            "label":         None,
        }
    else:
        avg = sum(scores) / total_count
        if avg >= 0.15:
            label = "bullish"
        elif avg <= -0.15:
            label = "bearish"
        else:
            label = "neutral"
        result = {
            "sentiment_avg": round(avg, 4),
            "article_count": total_count,
            "window_days":   int(window_days),
            "label":         label,
        }
    _cache_write(cache_key, result, ttl_sec=_TTL_SENTIMENT_SEC)
    return result




def _sorted_yearly(section: dict, n_max: int | None = 10) -> list[dict]:
    """Return up to n_max annual periods from an EODHD Financials sub-section,
    sorted newest-first (so index 0 = most recent year)."""
    yearly = section.get("yearly") or {}
    if not yearly:
        return []
    periods = sorted(yearly.items(), key=lambda kv: kv[0], reverse=True)
    if n_max is None or n_max <= 0:
        return [v for _, v in periods]
    return [v for _, v in periods[:n_max]]


def _extract_array(periods: list[dict], field: str, div: float = 1.0,
                   take_abs: bool = False) -> list[float]:
    """
    From a list of period dicts (newest first), extract *field* values,
    divide by *div*, reverse to oldest-first order.
    Returns a list of floats.
    """
    out = []
    for p in periods:
        raw = _sf(p.get(field, 0))
        if take_abs:
            raw = abs(raw)
        out.append(round(raw / div))
    out.reverse()   # oldest → newest
    return out


def _get_risk_free_rate() -> float:
    """Fetch 10-yr Treasury yield from FRED. Fallback 4.4."""
    try:
        from webapp.data.fmp_client import get_treasury_rate
        rate = get_treasury_rate()
        return float(rate) if rate else 4.4
    except Exception:
        return 4.4


def _get_fx_rate(currency: str) -> float:
    """Return the USD value of 1 unit of *currency*.
    GBX (pence) = 0.01 GBP ≈ 0.0126 USD at ~1.26 GBPUSD.
    Falls back to 1.0 if the pair can't be fetched.
    """
    ccy = (currency or "USD").upper().strip()
    if ccy in ("USD", ""):
        return 1.0
    # Pence (GBX) — always convert via hard ratio to GBP then to USD
    if ccy == "GBX":
        gbp_rate = _get_fx_rate("GBP")
        return round(gbp_rate / 100, 6)
    # Try EODHD forex endpoint
    try:
        pair = f"{ccy}USD"
        data = _get(f"real-time/{pair}.FOREX")
        if data and _sf(data.get("close")):
            return float(data["close"])
    except Exception:
        pass
    # Fallback: 1.0 (no conversion)
    return 1.0


# Exchange-suffix normalisation map: external convention → EODHD convention
_EXCHANGE_SUFFIX_MAP: dict[str, str] = {
    # US exchange venue codes → EODHD uses .US for all US-listed stocks
    ".NASDAQ":  ".US",
    ".NYSE":    ".US",
    ".NYSEARCA": ".US",
    ".NYSEAMERICAN": ".US",
    ".AMEX":    ".US",
    ".BATS":    ".US",
    ".CBOE":    ".US",
    ".OTCMKTS": ".US",
    ".OTCQB":   ".US",
    ".OTCQX":   ".US",
    ".PINK":    ".US",
    ".NMFQS":   ".US",  # mutual-fund codes; will 404 but correct format
    # Additional US venue codes with spaces (MIC-based)
    ".NYSE ARCA":    ".US",
    ".NYSE MKT":     ".US",
    ".NYSE AMERICAN": ".US",
    ".OTCGREY":      ".US",
    ".OTCCE":        ".US",
    # Non-US exchange remaps
    ".KS":  ".KO",       # Korea Stock Exchange (Yahoo/Bloomberg) → EODHD
    ".KQ":  ".KQ",       # KOSDAQ stays the same
    ".AX":  ".AU",       # ASX
    ".L":   ".LSE",      # London Stock Exchange
    ".DE":  ".XETRA",    # XETRA (Germany)
    ".PA":  ".PA",       # Euronext Paris (already correct)
    ".AS":  ".AS",       # Euronext Amsterdam
    ".MC":  ".MC",       # BME Spain
    ".MI":  ".MI",       # Borsa Italiana
    ".SW":  ".SW",       # SIX Swiss
    ".HK":  ".HK",       # Hong Kong Exchanges (already correct)
    ".TSE": ".T",        # Stored legacy Tokyo suffix → EODHD .T
    ".TO":  ".TO",       # Toronto Stock Exchange
    ".V":   ".V",        # TSX Venture
    ".NZ":  ".NZ",       # New Zealand Exchange
    ".SI":  ".SES",      # Singapore Exchange
    ".SS":  ".SHG",      # Shanghai Stock Exchange
    ".SZ":  ".SHE",      # Shenzhen Stock Exchange
    ".NS":  ".NSE",      # National Stock Exchange of India
    ".BO":  ".BSE",      # Bombay Stock Exchange
    ".SA":  ".SA",       # B3 (Brazil)
    ".MX":  ".MX",       # Bolsa Mexicana de Valores
    ".BA":  ".BA",       # Buenos Aires Stock Exchange
}

# Special whole-ticker remaps (dot cannot be used in EODHD code)
_TICKER_REMAP: dict[str, str] = {
    "BRK.A": "BRK-A.US", "BRK.B": "BRK-B.US",
    "BF.A":  "BF-A.US",  "BF.B":  "BF-B.US",
}


def normalize_requested_ticker(ticker: str, exchange: str | None = None) -> str:
    """Normalise a user-supplied ticker to an EODHD-compatible code.

    Handles:
    * Whole-ticker special cases (BRK.B → BRK-B.US)
    * Exchange-suffix remapping (.KS → .KO, .AX → .AU, .L → .LSE, etc.)
    * Explicit exchange hint: normalize_requested_ticker("BHP", exchange="LSE")
      → "BHP.LSE"
    * Plain ticker without suffix → append .US
    """
    t = ticker.upper().strip()

    # 1. Explicit exchange hint overrides everything
    if exchange and exchange.upper() not in ("AUTO", "AUTO-DETECT", ""):
        base = t.split(".")[0]          # strip any existing suffix
        return f"{base}.{exchange.upper()}"

    # 2. Whole-ticker special cases
    if t in _TICKER_REMAP:
        return _TICKER_REMAP[t]

    # 3. Exchange-suffix remapping
    for src, dst in _EXCHANGE_SUFFIX_MAP.items():
        if t.endswith(src):
            return t[: -len(src)] + dst

    # 4. Already has a dot-suffix that isn't a special case → keep as-is
    if "." in t:
        return t

    # 5. No suffix → assume US market
    return f"{t}.US"


def _sensitivity(terminal_ufcf: float, pv_ufcfs: float,
                 net_debt: float, diluted_shares: float,
                 wacc_pcts: list, g_pcts: list,
                 base_wacc: float, base_g: float,
                 forecast_years: int = 7) -> dict:
    values = []
    af_base = sum(1 / (1 + base_wacc / 100) ** (t - 0.5) for t in range(1, forecast_years + 1))
    # Outer loop = g (rows), inner loop = WACC (columns).
    # This matches the dashboard table layout: g labels on rows, WACC labels on columns.
    for g_pct in g_pcts:
        row = []
        for w_pct in wacc_pcts:
            w = w_pct / 100
            g = g_pct / 100
            spread = w - g
            if spread < 0.005:
                row.append(None)
                continue
            tv     = terminal_ufcf * (1 + g) / spread
            pv_tv  = tv / (1 + w) ** forecast_years
            # Mid-year convention: match main DCF (t - 0.5 exponent per year)
            af_new  = sum(1 / (1 + w) ** (t - 0.5) for t in range(1, forecast_years + 1))
            pv_uf   = pv_ufcfs * (af_new / af_base) if af_base > 0 else pv_ufcfs
            ev      = pv_uf + pv_tv
            iv      = max(0, ev - net_debt) / diluted_shares if diluted_shares > 0 else 0
            row.append(round(iv, 1))
        values.append(row)
    return {
        "wacc_labels":    [f"{w:.1f}%" for w in wacc_pcts],
        "g_labels":       [f"{g:.1f}%" for g in g_pcts],
        "iv_grid":        values,
        "base_wacc_idx":  wacc_pcts.index(base_wacc),
        "base_g_idx":     g_pcts.index(base_g),
        # also expose raw arrays for Sensitivity sheet
        "wacc_range":     wacc_pcts,
        "g_range":        g_pcts,
        "grid":           values,
        "iv_min":         min((v for row in values for v in row if v is not None), default=0),
        "iv_max":         max((v for row in values for v in row if v is not None), default=0),
    }


def _live_learning_feedback_enabled() -> bool:
    if os.environ.get("DCF_DISABLE_LIVE_LEARNING_FEEDBACK") == "1":
        return False
    return "PYTEST_CURRENT_TEST" not in os.environ


def _extract_near_term_quarters(earnings: dict[str, Any]) -> dict[str, Any]:
    """Extract analyst consensus for the next 2 upcoming quarters from EODHD Earnings.Trend.

    Returns a dict with 'q1' (soonest upcoming quarter) and 'q2' entries,
    each containing EPS and revenue estimates (base/bull/bear).
    """
    from datetime import date as _date

    today = _date.today()
    trend = dict(earnings.get("Trend") or {})

    upcoming = []
    for date_str, entry in trend.items():
        period = str(entry.get("period") or "")
        if period not in ("0q", "+1q", "+2q"):
            continue
        try:
            q_date = _date.fromisoformat(str(date_str))
        except (ValueError, TypeError):
            continue
        if q_date <= today:
            continue  # Already reported

        eps_avg  = _sf(entry.get("earningsEstimateAvg"),  default=float("nan"))
        eps_high = _sf(entry.get("earningsEstimateHigh"), default=float("nan"))
        eps_low  = _sf(entry.get("earningsEstimateLow"),  default=float("nan"))
        rev_avg  = _sf(entry.get("revenueEstimateAvg"),   default=float("nan"))
        rev_high = _sf(entry.get("revenueEstimateHigh"),  default=float("nan"))
        rev_low  = _sf(entry.get("revenueEstimateLow"),   default=float("nan"))
        n_analysts = int(_sf(entry.get("earningsEstimateNumberOfAnalysts"), default=0))

        import math as _math
        upcoming.append({
            "quarter_end":      date_str,
            "period":           period,
            "eps_base":         round(eps_avg,  4) if not _math.isnan(eps_avg)  else None,
            "eps_bull":         round(eps_high, 4) if not _math.isnan(eps_high) else None,
            "eps_bear":         round(eps_low,  4) if not _math.isnan(eps_low)  else None,
            "revenue_base_mm":  round(rev_avg  / 1e6, 1) if not _math.isnan(rev_avg)  else None,
            "revenue_bull_mm":  round(rev_high / 1e6, 1) if not _math.isnan(rev_high) else None,
            "revenue_bear_mm":  round(rev_low  / 1e6, 1) if not _math.isnan(rev_low)  else None,
            "n_analysts":       n_analysts,
        })

    upcoming.sort(key=lambda x: x["quarter_end"])
    return {
        "q1":    upcoming[0] if len(upcoming) >= 1 else None,
        "q2":    upcoming[1] if len(upcoming) >= 2 else None,
        "count": len(upcoming),
    }


def _extract_consensus_growth(
    earnings: dict[str, Any],
    revenue_base_mm: float,
) -> float | None:
    """Return the +1y analyst consensus revenue growth (%) from EODHD Earnings.Trend.

    Looks for the most forward annual entry (period == '+1y' or '0y') that has a
    non-zero revenueEstimateAvg and computes the implied YoY growth vs the last
    reported annual revenue. Returns None when no reliable estimate is available.
    """
    import math as _math
    if revenue_base_mm <= 0:
        return None
    trend = dict(earnings.get("Trend") or {})
    best_rev_avg: float | None = None
    best_sort_key = ""
    for date_str, entry in trend.items():
        period = str(entry.get("period") or "")
        if period not in ("+1y", "0y"):
            continue
        rev_avg = _sf(entry.get("revenueEstimateAvg"), default=float("nan"))
        if _math.isnan(rev_avg) or rev_avg <= 0:
            continue
        # Prefer the most distant (most forward) date to get true next-year consensus
        if str(date_str) > best_sort_key:
            best_sort_key = str(date_str)
            best_rev_avg = rev_avg / 1e6  # convert to $M
    if best_rev_avg is None:
        return None
    growth = (best_rev_avg / revenue_base_mm - 1.0) * 100.0
    return round(max(-30.0, min(80.0, growth)), 1)


def _extract_analyst_ratings_payload(anal: dict[str, Any], current_price: float) -> dict[str, Any]:
    """Surface the EODHD ``AnalystRatings`` block on the dashboard payload.

    Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (P4).
    """
    if not isinstance(anal, dict):
        return {}
    target = _sf(anal.get("TargetPrice"))
    rating = _sf(anal.get("Rating"))
    strong_buy = int(_sf(anal.get("StrongBuy"), default=0) or 0)
    buy = int(_sf(anal.get("Buy"), default=0) or 0)
    hold = int(_sf(anal.get("Hold"), default=0) or 0)
    sell = int(_sf(anal.get("Sell"), default=0) or 0)
    strong_sell = int(_sf(anal.get("StrongSell"), default=0) or 0)
    total = strong_buy + buy + hold + sell + strong_sell
    pvt: float | None = None
    if target and current_price:
        try:
            pvt = round(float(current_price) / float(target), 4)
        except Exception:
            pvt = None
    return {
        "analyst_target_price":  round(target, 2) if target else None,
        "analyst_rating":        round(rating, 2) if rating else None,
        "analyst_strong_buy":    strong_buy,
        "analyst_buy":           buy,
        "analyst_hold":          hold,
        "analyst_sell":          sell + strong_sell,
        "analyst_total":         total,
        "price_vs_target":       pvt,
    }


def _extract_earnings_surprise(earnings: dict[str, Any]) -> dict[str, Any]:
    """Compute trailing 4Q earnings-surprise statistics from EODHD ``Earnings.History``.

    Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (P5).
    """
    history = (earnings or {}).get("History") or {}
    if not isinstance(history, dict):
        return {}
    rows: list[tuple[str, float]] = []
    for entry in history.values():
        if not isinstance(entry, dict):
            continue
        surprise = _sf(entry.get("surprisePercent"))
        actual = _sf(entry.get("epsActual"))
        if surprise is None or actual is None or actual == 0:
            continue
        date_str = str(entry.get("date") or entry.get("reportDate") or "")
        rows.append((date_str, float(surprise)))
    rows.sort(key=lambda r: r[0], reverse=True)
    recent = rows[:4]
    if not recent:
        return {
            "earnings_surprise_avg_4q": None,
            "earnings_beat_count_4q": 0,
            "earnings_quarters_used": 0,
        }
    surprises = [r[1] for r in recent]
    return {
        "earnings_surprise_avg_4q": round(sum(surprises) / len(surprises), 2),
        "earnings_beat_count_4q":   sum(1 for s in surprises if s > 0),
        "earnings_quarters_used":   len(recent),
    }


def _extract_eps_revision_signal(earnings: dict[str, Any]) -> dict[str, Any]:
    """Pull EPS revision momentum (current vs 30-day-ago consensus) from
    ``Earnings.Trend`` +1y. Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (S4)."""
    trend = (earnings or {}).get("Trend") or {}
    if not isinstance(trend, dict):
        return {}
    plus_1y = next(
        (e for e in trend.values()
         if isinstance(e, dict) and str(e.get("period") or "").lower() == "+1y"),
        None,
    )
    if not plus_1y:
        return {}
    cur = _sf(plus_1y.get("epsTrendCurrent"))
    d30 = _sf(plus_1y.get("epsTrend30daysAgo"))
    rev_up = _sf(plus_1y.get("epsRevisionsUpLast30days"), default=0) or 0
    rev_dn = _sf(plus_1y.get("epsRevisionsDownLast30days"), default=0) or 0
    momentum = None
    if cur is not None and d30 not in (None, 0):
        momentum = round((cur - d30) / abs(d30), 4)
    return {
        "eps_revision_momentum_30d": momentum,
        "eps_revisions_up_30d":      int(rev_up),
        "eps_revisions_down_30d":    int(rev_dn),
    }


def _extract_insider_signal(
    insiders: dict[str, Any] | list[Any],
    *,
    window_days: int = 90,
) -> dict[str, Any]:
    """Aggregate net insider activity over the last ``window_days``.

    Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (M2).
    """
    if isinstance(insiders, dict):
        entries = list(insiders.values())
    elif isinstance(insiders, list):
        entries = insiders
    else:
        return {}
    if not entries:
        return {}

    from datetime import date as _date, timedelta as _td
    cutoff = _date.today() - _td(days=window_days)

    net_shares = 0.0
    buys = 0
    sells = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date_str = str(entry.get("transactionDate") or entry.get("date") or "")
        try:
            d = _date.fromisoformat(date_str[:10])
        except Exception:
            continue
        if d < cutoff:
            continue
        amt = _sf(entry.get("transactionAmount"))
        if amt is None:
            continue
        side = str(entry.get("transactionAcquiredDisposed") or "").upper()
        if side == "A":
            net_shares += amt
            buys += 1
        elif side == "D":
            net_shares -= amt
            sells += 1

    if buys == 0 and sells == 0:
        return {}

    if net_shares > 0:
        signal = "buying"
    elif net_shares < 0:
        signal = "selling"
    else:
        signal = "neutral"

    return {
        "insider_net_shares_90d":   int(net_shares),
        "insider_buy_count_90d":    buys,
        "insider_sell_count_90d":   sells,
        "insider_signal_90d":       signal,
    }


def _forecast_horizon_year(data: dict[str, Any]) -> int:
    forecast = list(data.get("forecast") or [])
    if forecast:
        label = str(forecast[-1].get("year") or "")
        digits = "".join(ch for ch in label if ch.isdigit())
        if len(digits) >= 4:
            try:
                return int(digits[-4:])
            except ValueError:
                pass

    historical_years = list((data.get("historical") or {}).get("years") or [])
    if historical_years and forecast:
        return int(historical_years[-1]) + len(forecast)
    return 0


def _ledger_evidence_summary(ticker: str) -> dict[str, Any]:
    try:
        from auto_valuation.learning.ledger import LedgerReader

        ticker_upper = str(ticker or "").upper()
        reader = LedgerReader()
        predictions = reader.query(ticker=ticker_upper, scenario="base")
        realized = reader.query_realized_outcomes(ticker=ticker_upper)
        postmortems: list[dict[str, Any]] = []
        for record in predictions:
            postmortems.extend(reader.query_postmortems(record_id=record.record_id))
        matured_record_ids = {
            str(item.record_id or "") for item in realized if str(item.record_id or "")
        } | {
            str(item.get("record_id") or "") for item in postmortems if str(item.get("record_id") or "")
        }
        return {
            "enabled": True,
            "matured_records": len(matured_record_ids),
            "updated_records": 0,
            "total_realized_records": len(realized),
            "complete_realized_records": sum(1 for item in realized if item.label_status == "complete"),
            "partial_realized_records": sum(1 for item in realized if item.label_status == "partial"),
            "postmortem_records": len(postmortems),
            "prediction_records": len(predictions),
            "source": "ledger-readonly",
        }
    except Exception:
        pass

    # Fallback: use seeded ledger summary bundled at deploy time
    try:
        from auto_valuation.learning.deployment_seed import seeded_ledger_evidence

        ticker_upper = str(ticker or "").upper()
        summary = seeded_ledger_evidence(ticker_upper)
        if summary:
            return {
                "enabled": True,
                "matured_records": int(summary.get("matured_records") or 0),
                "updated_records": 0,
                "total_realized_records": int(summary.get("matured_records") or 0),
                "complete_realized_records": int(summary.get("matured_records") or 0),
                "partial_realized_records": 0,
                "postmortem_records": 0,
                "prediction_records": int(summary.get("prediction_records") or 0),
                "source": "deployment-ledger-summary",
            }
    except Exception:
        pass

    return {"enabled": False, "matured_records": 0, "updated_records": 0, "reason": "ledger-unavailable"}


def _historical_replay_evidence_summary(ticker: str) -> dict[str, Any]:
    ticker_upper = str(ticker or "").upper()
    if not ticker_upper:
        return {"enabled": False, "records": 0, "reason": "missing-ticker"}

    def _obs_field(observation: Any, name: str, default: Any = None) -> Any:
        if isinstance(observation, dict):
            return observation.get(name, default)
        return getattr(observation, name, default)

    try:
        from auto_valuation.learning.historical_replay import get_all_observations

        observations = [
            observation
            for observation in get_all_observations()
            if str(_obs_field(observation, "ticker", "") or "").upper() == ticker_upper
        ]
        revenue_errors: list[float] = []
        margin_errors: list[float] = []
        for observation in observations:
            predicted_revenue = _maybe_float(_obs_field(observation, "predicted_revenue_growth"))
            actual_revenue = _maybe_float(_obs_field(observation, "actual_revenue_growth"))
            if predicted_revenue is not None and actual_revenue is not None:
                revenue_errors.append((actual_revenue - predicted_revenue) * 100.0)
            predicted_margin = _maybe_float(_obs_field(observation, "predicted_ebit_margin"))
            actual_margin = _maybe_float(_obs_field(observation, "actual_ebit_margin"))
            if predicted_margin is not None and actual_margin is not None:
                margin_errors.append((actual_margin - predicted_margin) * 100.0)
        if observations:
            return {
                "enabled": True,
                "records": len(observations),
                "mean_abs_revenue_error_pct": _mean_abs(revenue_errors),
                "mean_abs_margin_error_pp": _mean_abs(margin_errors),
                "source": "historical-replay",
            }
    except Exception:
        pass

    try:
        from auto_valuation.learning.deployment_seed import historical_replay_summary

        summary = historical_replay_summary(ticker_upper)
        records = int(summary.get("records") or 0)
        if records > 0:
            return {
                "enabled": True,
                "records": records,
                "annual_records": int(summary.get("annual_records") or 0),
                "quarterly_records": int(summary.get("quarterly_records") or 0),
                "first_year": summary.get("first_year"),
                "last_year": summary.get("last_year"),
                "mean_abs_revenue_error_pct": _maybe_float(summary.get("mean_abs_revenue_error_pct")),
                "mean_abs_margin_error_pp": _maybe_float(summary.get("mean_abs_margin_error_pp")),
                "source": "deployment-replay-summary",
            }
        return {
            "enabled": True,
            "records": 0,
            "mean_abs_revenue_error_pct": None,
            "mean_abs_margin_error_pp": None,
            "source": "historical-replay",
        }
    except Exception as exc:
        return {"enabled": False, "records": 0, "reason": str(exc)}


def _latest_maintenance_summary() -> dict[str, Any]:
    try:
        from auto_valuation.learning.ledger import LedgerReader

        runs = LedgerReader().query_maintenance_runs(limit=1)
        if not runs:
            return {"enabled": True, "ran": False, "reason": "no-maintenance-history"}
        latest = runs[0]
        payload = dict(latest.payload or {})
        return {
            "enabled": True,
            "ran": False,
            "reason": "last-run",
            "last_run_at": payload.get("last_run_at") or latest.completed_at,
            "maintenance_run_id": latest.run_id,
            "scanned_tickers": int(payload.get("scanned_tickers") or 0),
            "matured_records": int(payload.get("matured_records") or 0),
            "backfilled_records": int(payload.get("backfilled_records") or 0),
            "annual_postmortems_created": int(payload.get("annual_postmortems_created") or 0),
            "quinquennial_reports_created": int(payload.get("quinquennial_reports_created") or 0),
            "tickers_processed": list(payload.get("tickers_processed") or [])[:8],
        }
    except Exception as exc:
        return {"enabled": False, "ran": False, "reason": str(exc)}


def _read_only_snapshot_summary(data: dict[str, Any]) -> dict[str, Any]:
    horizon_year = _forecast_horizon_year(data)
    ticker = str(data.get("ticker") or data.get("requested_ticker") or "").upper()
    if not _live_learning_feedback_enabled():
        return {"enabled": False, "persisted": False, "reason": "disabled", "horizon_year": horizon_year}
    try:
        from auto_valuation.learning.ledger import LedgerReader

        expected_id = f"{ticker}-{datetime.now(timezone.utc).date()}-FY{horizon_year}-base"
        records = LedgerReader().query(ticker=ticker, horizon_year=horizon_year, scenario="base") if ticker and horizon_year else []
        persisted = any(record.record_id == expected_id for record in records)
        return {
            "enabled": True,
            "persisted": persisted,
            "reason": "already-persisted" if persisted else "read-only",
            "horizon_year": horizon_year,
            "existing_snapshots": len(records),
        }
    except Exception as exc:
        return {"enabled": False, "persisted": False, "reason": str(exc), "horizon_year": horizon_year}


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "" or value == "None":
        return None
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _normalised_pct(value: Any) -> float | None:
    number = _maybe_float(value)
    if number is None:
        return None
    return number * 100.0 if abs(number) <= 1.5 else number


def _pct_error(actual: Any, predicted: Any) -> float | None:
    actual_number = _maybe_float(actual)
    predicted_number = _maybe_float(predicted)
    if actual_number is None or predicted_number is None or abs(predicted_number) <= 1e-9:
        return None
    return (actual_number - predicted_number) / abs(predicted_number) * 100.0


def _mean_abs(values: Iterable[Any]) -> float | None:
    cleaned = [abs(float(value)) for value in values if _maybe_float(value) is not None]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), 2)


def _learned_recommendation_from_upside(
    upside_pct: float,
    *,
    expected_error_pct: float = 0.0,
) -> tuple[str, str, float, float]:
    buy_threshold = max(15.0, min(35.0, expected_error_pct or 0.0))
    sell_threshold = -max(10.0, min(30.0, (expected_error_pct or 0.0) * 0.8))
    if upside_pct >= buy_threshold:
        return "Undervalued", "green", buy_threshold, sell_threshold
    if upside_pct <= sell_threshold:
        return "Overvalued", "red", buy_threshold, sell_threshold
    return "Fairly Valued", "amber", buy_threshold, sell_threshold


def _learning_accuracy_summary(
    ticker: str,
    *,
    sector: str = "",
    industry: str = "",
    knowledge_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    knowledge_model = knowledge_model or {}
    explainability = dict(knowledge_model.get("explainability") or {})
    confidence_model = dict(knowledge_model.get("confidence_model") or {})
    valuation_confidence = dict(confidence_model.get("valuation_confidence") or {})
    expected_error = dict(
        valuation_confidence.get("expected_error_pct")
        or knowledge_model.get("expected_valuation_error_band")
        or {}
    )
    p50_error = float(expected_error.get("p50") or knowledge_model.get("expected_valuation_error_pct") or 0.0)
    p90_error = float(expected_error.get("p90") or p50_error or 0.0)

    revenue_errors: list[float] = []
    margin_errors_pp: list[float] = []
    valuation_errors: list[float] = []
    direct_samples = 0
    historical_summary = _historical_replay_evidence_summary(ticker)
    historical_samples = int(historical_summary.get("records") or 0)

    try:
        from auto_valuation.learning.ledger import LedgerReader

        reader = LedgerReader()
        pairs = reader.query_aligned_pairs(
            ticker=str(ticker or "").upper(),
            scenario="base",
            include_partial=True,
            include_postmortems=True,
        )
        direct_samples = len({pair.prediction.record_id for pair in pairs})
        for pair in pairs:
            for postmortem in pair.postmortems:
                revenue_error = _maybe_float(postmortem.get("revenue_error_pct"))
                if revenue_error is not None:
                    revenue_errors.append(revenue_error)
                margin_error_bps = _maybe_float(postmortem.get("margin_error_bps"))
                if margin_error_bps is not None:
                    margin_errors_pp.append(margin_error_bps / 100.0)
                valuation_error = None
                for error_key in ("ev_error_pct", "price_error_pct", "price_return_error_pct"):
                    error_value = _maybe_float(postmortem.get(error_key))
                    if error_value is not None:
                        valuation_error = error_value
                        break
                if valuation_error is not None:
                    valuation_errors.append(valuation_error)

            outcome = pair.realized_outcome
            prediction = pair.prediction
            if outcome is None:
                continue
            revenue_error = _pct_error(outcome.actual_revenue_mm, prediction.predicted_revenue_mm)
            if revenue_error is not None:
                revenue_errors.append(revenue_error)
            predicted_margin = _normalised_pct(prediction.predicted_ebit_margin)
            actual_margin = _normalised_pct(outcome.actual_ebit_margin)
            if predicted_margin is not None and actual_margin is not None:
                margin_errors_pp.append(actual_margin - predicted_margin)
            valuation_error = _pct_error(outcome.actual_ev_mm, prediction.predicted_ev_mm)
            if valuation_error is None:
                valuation_error = _pct_error(outcome.actual_price_at_horizon, prediction.predicted_price_per_share)
            if valuation_error is not None:
                valuation_errors.append(valuation_error)
    except Exception:
        pass

    priority: dict[str, Any] = {}
    try:
        from auto_valuation.learning.calibration_priority import (
            build_calibration_priority_index,
            calibration_priority_for_symbol,
        )

        priority = calibration_priority_for_symbol(
            str(ticker or "").upper(),
            sector=sector,
            industry=industry,
            index=build_calibration_priority_index(),
        )
    except Exception:
        priority = {}

    revenue_error = _mean_abs(revenue_errors)
    margin_error = _mean_abs(margin_errors_pp)
    historical_revenue_error = _maybe_float(historical_summary.get("mean_abs_revenue_error_pct"))
    historical_margin_error = _maybe_float(historical_summary.get("mean_abs_margin_error_pp"))
    valuation_error = _mean_abs(valuation_errors)
    assumption_error_parts = [
        value for value in (revenue_error, historical_revenue_error)
        if value is not None
    ]
    if margin_error is not None:
        assumption_error_parts.append(margin_error * 3.0)
    if historical_margin_error is not None:
        assumption_error_parts.append(historical_margin_error * 3.0)
    assumption_error = round(sum(assumption_error_parts) / len(assumption_error_parts), 2) if assumption_error_parts else None
    direct_error_parts = [value for value in (assumption_error, valuation_error) if value is not None]
    direct_mean_error = round((assumption_error * 0.85 + valuation_error * 0.15), 2) if assumption_error is not None and valuation_error is not None else (round(sum(direct_error_parts) / len(direct_error_parts), 2) if direct_error_parts else None)
    priority_error = _maybe_float(priority.get("mean_abs_error_pct"))
    effective_error = direct_mean_error if direct_mean_error is not None else (priority_error if priority_error is not None and priority_error > 0 else p50_error)
    accuracy_score = int(round(max(0.0, min(100.0, 100.0 - min(50.0, effective_error or 0.0) * 2.0))))
    assumption_score = int(round(max(0.0, min(100.0, 100.0 - min(50.0, assumption_error or effective_error or 0.0) * 2.0))))
    valuation_score = int(round(max(0.0, min(100.0, 100.0 - min(50.0, valuation_error or effective_error or 0.0) * 2.0))))
    confidence_score = int(
        valuation_confidence.get("score_100")
        or round(float(valuation_confidence.get("score") or knowledge_model.get("valuation_confidence") or 0.0) * 100)
        or accuracy_score
    )

    company_memory = dict(explainability.get("company_memory") or {})
    cohort_memory = dict(explainability.get("cohort_memory") or {})
    global_memory = dict(explainability.get("global_memory") or {})
    global_brain = dict(explainability.get("global_brain") or knowledge_model.get("global_learning") or {})
    global_records = max(int(global_memory.get("records") or 0), int(global_brain.get("cohort_size") or 0))
    cohort_samples = int(priority.get("cohort_samples") or cohort_memory.get("records") or knowledge_model.get("calibration_cohort_size") or 0)
    priority_direct = int(priority.get("direct_samples") or 0)
    direct_samples = max(direct_samples, priority_direct)
    if direct_samples > 0 or historical_samples > 0:
        scope = "ticker"
        source_note = (
            f"{direct_samples} ledger back-test(s) plus {historical_samples} replay observation(s) anchor the ticker view."
            if historical_samples > 0
            else f"{direct_samples} ticker-specific matured forecast sample(s) anchor the back-test."
        )
    elif cohort_samples > 0:
        scope = "cohort"
        source_note = f"No ticker back-test is mature yet, so {cohort_samples} matched cohort sample(s) carry the accuracy estimate."
    elif global_records > 0:
        scope = "global"
        source_note = f"No direct/cohort back-test is mature yet, so {global_records} global memory record(s) carry the estimate."
    else:
        scope = "confidence-model"
        source_note = "No realized back-test is mature yet; accuracy is inferred from the confidence model."

    return {
        "enabled": True,
        "scope": scope,
        "score": accuracy_score,
        "assumption_score": assumption_score,
        "valuation_score": valuation_score,
        "confidence_score": confidence_score,
        "expected_error_pct": {"p50": round(p50_error, 2), "p90": round(p90_error, 2)},
        "direct_samples": direct_samples,
        "historical_replay_samples": historical_samples,
        "cohort_samples": cohort_samples,
        "global_records": global_records,
        "company_weight_pct": int(company_memory.get("weight_pct") or 0),
        "cohort_weight_pct": int(cohort_memory.get("weight_pct") or 0),
        "mean_abs_error_pct": round(effective_error, 2) if effective_error is not None else None,
        "mean_abs_assumption_error_pct": assumption_error,
        "mean_abs_revenue_error_pct": revenue_error,
        "mean_abs_margin_error_pp": margin_error,
        "mean_abs_historical_revenue_error_pct": historical_revenue_error,
        "mean_abs_historical_margin_error_pp": historical_margin_error,
        "mean_abs_valuation_error_pct": valuation_error,
        "priority": priority,
        "source_note": source_note,
        "label": "high" if accuracy_score >= 75 else ("moderate" if accuracy_score >= 55 else "guarded"),
    }


def _learned_scenario_summary(
    dashboard_data: dict[str, Any],
    *,
    knowledge_model: dict[str, Any] | None = None,
    accuracy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    knowledge_model = knowledge_model or {}
    accuracy = accuracy or {}
    scenarios = dict(dashboard_data.get("scenarios") or {})
    base = dict(scenarios.get("base") or {})
    bull = dict(scenarios.get("bull") or {})
    bear = dict(scenarios.get("bear") or {})
    base_upside = float(base.get("upside_pct") if base.get("upside_pct") is not None else base.get("upside") or 0.0)
    bull_upside = float(bull.get("upside_pct") if bull.get("upside_pct") is not None else bull.get("upside") or base_upside)
    bear_upside = float(bear.get("upside_pct") if bear.get("upside_pct") is not None else bear.get("upside") or base_upside)
    probabilities = {
        "base": float(base.get("probability") or 0.50),
        "bull": float(bull.get("probability") or 0.25),
        "bear": float(bear.get("probability") or 0.25),
    }
    total_probability = sum(probabilities.values()) or 1.0
    probabilities = {key: value / total_probability for key, value in probabilities.items()}
    expected_upside = (
        base_upside * probabilities["base"]
        + bull_upside * probabilities["bull"]
        + bear_upside * probabilities["bear"]
    )
    expected_error = float((accuracy.get("expected_error_pct") or {}).get("p50") or knowledge_model.get("expected_valuation_error_pct") or 0.0)
    recommendation, rec_class, buy_threshold, sell_threshold = _learned_recommendation_from_upside(
        expected_upside,
        expected_error_pct=expected_error,
    )
    scenario_basis = dict(base.get("learning_basis") or bull.get("learning_basis") or bear.get("learning_basis") or {})
    return {
        "enabled": bool(scenarios),
        "expected_upside_pct": round(expected_upside, 1),
        "recommendation": recommendation,
        "recommendation_class": rec_class,
        "probabilities": {key: round(value * 100.0) for key, value in probabilities.items()},
        "thresholds": {"buy_upside_pct": round(buy_threshold, 1), "sell_upside_pct": round(sell_threshold, 1)},
        "scenario_width_multiplier": round(float(scenario_basis.get("width_multiplier") or knowledge_model.get("scenario_width_multiplier") or 1.0), 2),
        "growth_bias_pp": scenario_basis.get("growth_bias_pp"),
        "margin_bias_pp": scenario_basis.get("margin_bias_pp"),
        "wacc_bias_pp": scenario_basis.get("wacc_bias_pp"),
        "caution_flags": int(scenario_basis.get("caution_flags") or 0),
        "summary": (
            f"Scenario-weighted expected upside is {expected_upside:.1f}% after applying learned uncertainty and an expected error band near {expected_error:.1f}%."
        ),
    }


def _persist_learning_snapshot(data: dict[str, Any], knowledge_model: dict[str, Any]) -> dict[str, Any]:
    horizon_year = _forecast_horizon_year(data)
    if not _live_learning_feedback_enabled():
        return {
            "enabled": False,
            "persisted": False,
            "reason": "disabled",
            "horizon_year": horizon_year,
        }

    try:
        from auto_valuation.learning.ledger import LedgerWriter, PredictionRecord
    except Exception:
        return {
            "enabled": False,
            "persisted": False,
            "reason": "learning-ledger-unavailable",
            "horizon_year": horizon_year,
        }

    forecast = list(data.get("forecast") or [])
    historical_years = list((data.get("historical") or {}).get("years") or [])
    if not forecast or not historical_years or horizon_year <= 0:
        return {
            "enabled": True,
            "persisted": False,
            "reason": "missing-forecast",
            "horizon_year": horizon_year,
        }

    run_date = str(datetime.now(timezone.utc).date())
    record_id = f"{data.get('ticker', '').upper()}-{run_date}-FY{horizon_year}-base"
    feature_vector = tuple(knowledge_model.get("feature_vector") or ()) or None
    fiscal_year_end_month, fiscal_year_end_day = _fiscal_year_end_components(data)
    macro_backdrop = {
        "rf_rate": round(float(data.get("risk_free_rate") or 0.0) / 100, 4),
        "erp": round(float(data.get("erp") or 0.0) / 100, 4),
        "predicted_wacc": round(float(data.get("wacc") or 0.0) / 100, 4),
        "terminal_growth": round(float(data.get("terminal_growth") or 0.0) / 100, 4),
    }

    try:
        writer = LedgerWriter()
        writer.append(
            PredictionRecord(
                record_id=record_id,
                ticker=str(data.get("ticker") or "").upper(),
                company_name=str(data.get("company_name") or ""),
                sector=str(data.get("sector") or ""),
                industry=str(data.get("industry") or ""),
                run_date=datetime.now(timezone.utc).date(),
                forecast_horizon_year=horizon_year,
                years_since_ipo=len(historical_years),
                data_vintage_years=len(historical_years),
                predicted_revenue_mm=float(forecast[-1].get("revenue") or 0.0),
                predicted_ebit_margin=float(data.get("ebit_margin_target") or 0.0) / 100,
                predicted_ebit_mm=float(forecast[-1].get("ebit") or 0.0),
                predicted_ufcf_mm=float(forecast[-1].get("ufcf") or 0.0),
                predicted_wacc=float(data.get("wacc") or 0.0) / 100,
                predicted_terminal_growth=float(data.get("terminal_growth") or 0.0) / 100,
                predicted_ev_mm=float(data.get("enterprise_value") or 0.0),
                predicted_equity_value_mm=float(data.get("equity_value") or 0.0),
                predicted_price_per_share=float(data.get("intrinsic_value") or 0.0),
                scenario="base",
                near_term_revenue_growth=float(data.get("revenue_growth_near") or 0.0) / 100,
                target_ebit_margin=float(data.get("ebit_margin_target") or 0.0) / 100,
                da_pct_revenue=float(data.get("da_pct") or 0.0) / 100,
                capex_pct_revenue=float(data.get("capex_pct") or 0.0) / 100,
                beta=float(data.get("beta") or 0.0),
                erp=float(data.get("erp") or 0.0) / 100,
                rf_rate=float(data.get("risk_free_rate") or 0.0) / 100,
                actual_price_at_prediction=float(data.get("price") or 0.0),
                actual_ev_at_prediction=float(data.get("market_cap") or 0.0) + float(data.get("net_debt") or 0.0),
                market_cycle_phase="neutral",
                macro_backdrop=macro_backdrop,
                market_cap_regime=str(knowledge_model.get("market_cap_regime") or ""),
                macro_regime=_macro_regime_from_rf(float(data.get("risk_free_rate") or 0.0) / 100),
                feature_vector=feature_vector,
                fiscal_year_end_month=fiscal_year_end_month,
                fiscal_year_end_day=fiscal_year_end_day,
                prediction_context={
                    "source": "webapp_live_dashboard",
                    "price_date": str(data.get("price_date") or run_date),
                    "forecast_years": len(forecast),
                    "data_source": str(data.get("data_source") or "eodhd"),
                    "display_currency": str(data.get("currency") or ""),
                },
            )
        )

        # ── Also record quarterly predictions (Q+1 and Q+2) ─────────────
        near_term = dict(data.get("near_term_forecast") or {})
        ticker_upper = str(data.get("ticker") or "").upper()
        _quarterly_recorded = 0
        for q_key in ("q1", "q2"):
            q = dict(near_term.get(q_key) or {})
            if not q or q.get("quarter_end") is None:
                continue
            rev_base_mm = q.get("revenue_base_mm")
            if not rev_base_mm:
                continue
            q_horizon_year = int(str(q["quarter_end"])[:4])
            q_record_id = f"{ticker_upper}-{run_date}-Q{q['quarter_end']}-base"
            try:
                writer.append(
                    PredictionRecord(
                        record_id=q_record_id,
                        ticker=ticker_upper,
                        company_name=str(data.get("company_name") or ""),
                        sector=str(data.get("sector") or ""),
                        industry=str(data.get("industry") or ""),
                        run_date=datetime.now(timezone.utc).date(),
                        forecast_horizon_year=q_horizon_year,
                        years_since_ipo=len(historical_years),
                        data_vintage_years=len(historical_years),
                        predicted_revenue_mm=float(rev_base_mm),
                        predicted_ebit_margin=None or 0.0,
                        predicted_ebit_mm=0.0,
                        predicted_ufcf_mm=0.0,
                        predicted_wacc=float(data.get("wacc") or 0.0) / 100,
                        predicted_terminal_growth=0.0,
                        predicted_ev_mm=0.0,
                        predicted_equity_value_mm=0.0,
                        predicted_price_per_share=0.0,
                        scenario="base",
                        near_term_revenue_growth=float(data.get("revenue_growth_near") or 0.0) / 100,
                        target_ebit_margin=float(data.get("ebit_margin_target") or 0.0) / 100,
                        da_pct_revenue=0.0,
                        capex_pct_revenue=0.0,
                        beta=float(data.get("beta") or 0.0),
                        erp=float(data.get("erp") or 0.0) / 100,
                        rf_rate=float(data.get("risk_free_rate") or 0.0) / 100,
                        actual_price_at_prediction=float(data.get("price") or 0.0),
                        actual_ev_at_prediction=float(data.get("market_cap") or 0.0) + float(data.get("net_debt") or 0.0),
                        market_cycle_phase="neutral",
                        macro_backdrop=macro_backdrop,
                        market_cap_regime=str(knowledge_model.get("market_cap_regime") or ""),
                        macro_regime=_macro_regime_from_rf(float(data.get("risk_free_rate") or 0.0) / 100),
                        feature_vector=feature_vector,
                        fiscal_year_end_month=fiscal_year_end_month,
                        fiscal_year_end_day=fiscal_year_end_day,
                        prediction_context={
                            "source": "webapp_live_dashboard_quarterly",
                            "price_date": str(data.get("price_date") or run_date),
                            "quarter_end": q["quarter_end"],
                            "quarter_key": q_key,
                            "eps_base": q.get("eps_base"),
                            "eps_bull": q.get("eps_bull"),
                            "eps_bear": q.get("eps_bear"),
                            "revenue_bull_mm": q.get("revenue_bull_mm"),
                            "revenue_bear_mm": q.get("revenue_bear_mm"),
                            "n_analysts": q.get("n_analysts"),
                            "data_source": "eodhd_earnings_trend",
                        },
                    )
                )
                _quarterly_recorded += 1
            except ValueError:
                pass  # Duplicate — already recorded this quarter for today

        return {
            "enabled": True,
            "persisted": True,
            "reason": "appended",
            "record_id": record_id,
            "horizon_year": horizon_year,
            "quarterly_recorded": _quarterly_recorded,
        }
    except ValueError:
        return {
            "enabled": True,
            "persisted": False,
            "reason": "duplicate",
            "record_id": record_id,
            "horizon_year": horizon_year,
        }
    except Exception as exc:
        logger.warning("Learning persistence failed for %s: %s", data.get("ticker"), exc)
        return {
            "enabled": True,
            "persisted": False,
            "reason": "error",
            "detail": str(exc),
            "horizon_year": horizon_year,
        }


def _backfill_learning_actuals(ticker: str, fundamentals: dict[str, Any]) -> dict[str, Any]:
    if not _live_learning_feedback_enabled():
        return {
            "enabled": False,
            "updated_records": 0,
            "matured_records": 0,
            "reason": "disabled",
        }

    try:
        from auto_valuation.learning.ledger import LedgerReader, LedgerWriter
        from auto_valuation.learning.maintenance import (
            align_prediction_record_to_actuals,
            extract_actuals_from_fundamentals,
            extract_quarterly_actuals_from_fundamentals,
        )
    except Exception:
        return {
            "enabled": False,
            "updated_records": 0,
            "matured_records": 0,
            "reason": "learning-ledger-unavailable",
        }

    try:
        actuals_by_year = extract_actuals_from_fundamentals(fundamentals or {})
        quarterly_actuals = extract_quarterly_actuals_from_fundamentals(fundamentals or {})

        if not actuals_by_year and not quarterly_actuals:
            return {
                "enabled": True,
                "updated_records": 0,
                "matured_records": 0,
                "reason": "no-actuals",
            }

        reader = LedgerReader()
        writer = LedgerWriter()
        matured_records = 0
        updated_records = 0
        partial_updated_records = 0
        quarterly_verified = 0

        # ── Annual DCF predictions ────────────────────────────────────────
        for record in reader.query(ticker=ticker.upper(), scenario="base"):
            # Quarterly prediction records have a quarter-specific record_id pattern
            ctx = dict(record.prediction_context or {})
            if ctx.get("source") == "webapp_live_dashboard_quarterly":
                # Verify quarterly prediction against realized EPS
                q_end = str(ctx.get("quarter_end") or "")
                q_actual = quarterly_actuals.get(q_end)
                if q_actual is None:
                    continue
                matured_records += 1
                eps_predicted = ctx.get("eps_base")
                eps_actual_val = q_actual.get("eps_actual")
                if eps_actual_val is None:
                    continue
                # Compute revenue-level proxy: use predicted_revenue_mm vs actual
                # Just backfill with what we have (EPS surprise is in source_payload)
                if writer.backfill_actuals(
                    record.record_id,
                    actual_revenue_mm=record.predicted_revenue_mm,  # Use model revenue as placeholder
                    actual_ebit_margin=None,
                    actual_ufcf_mm=None,
                    actual_ev_mm=None,
                    actual_price_at_horizon=None,
                    postmortem_notes=f"Quarterly EPS actual={eps_actual_val}, predicted={eps_predicted}. Surprise={q_actual.get('eps_surprise_pct')}%.",
                    label_as_of_date=None,
                    aligned_period_end=_parse_iso_date(q_end),
                    source_name="eodhd_earnings_history",
                    source_kind="quarterly_eps",
                    macro_backdrop={},
                    surprise_flags=["eps_surprise"] if q_actual.get("eps_surprise_pct") else [],
                    structural_break_hints=[],
                    unknown_targets=[],
                    source_payload={
                        "quarter_end": q_end,
                        "eps_actual": eps_actual_val,
                        "eps_predicted": eps_predicted,
                        "eps_surprise_pct": q_actual.get("eps_surprise_pct"),
                        "report_date": q_actual.get("report_date"),
                    },
                ):
                    updated_records += 1
                    quarterly_verified += 1
                continue

            # Annual DCF record
            actuals = align_prediction_record_to_actuals(record, actuals_by_year)
            if not actuals:
                continue
            matured_records += 1
            if writer.backfill_actuals(
                record.record_id,
                actual_revenue_mm=actuals.get("actual_revenue_mm"),
                actual_ebit_margin=actuals.get("actual_ebit_margin"),
                actual_ufcf_mm=actuals.get("actual_ufcf_mm"),
                actual_ev_mm=actuals.get("actual_ev_mm"),
                actual_price_at_horizon=actuals.get("actual_price_at_horizon"),
                postmortem_notes=actuals.get("notes"),
                label_as_of_date=_parse_iso_date(actuals.get("label_as_of_date")),
                aligned_period_end=_parse_iso_date(actuals.get("aligned_period_end")),
                source_name=str(actuals.get("source_name") or "eodhd_fundamentals"),
                source_kind=str(actuals.get("source_kind") or "fundamentals"),
                macro_backdrop=dict(actuals.get("macro_backdrop") or {}),
                surprise_flags=list(actuals.get("surprise_flags") or []),
                structural_break_hints=list(actuals.get("structural_break_hints") or []),
                unknown_targets=list(actuals.get("unknown_targets") or []),
                source_payload=dict(actuals.get("source_payload") or {}),
            ):
                updated_records += 1
                if actuals.get("unknown_targets"):
                    partial_updated_records += 1

        return {
            "enabled": True,
            "updated_records": updated_records,
            "partial_updated_records": partial_updated_records,
            "matured_records": matured_records,
            "quarterly_verified": quarterly_verified,
            "available_years": sorted(actuals_by_year),
        }
    except Exception as exc:
        logger.warning("Learning backfill failed for %s: %s", ticker, exc)
        return {
            "enabled": False,
            "updated_records": 0,
            "matured_records": 0,
            "reason": "error",
            "detail": str(exc),
        }


def _run_learning_maintenance(ticker: str, fundamentals: dict[str, Any]) -> dict[str, Any]:
    if not _live_learning_feedback_enabled():
        return {
            "enabled": False,
            "ran": False,
            "reason": "disabled",
        }

    try:
        from auto_valuation.learning.maintenance import run_scheduled_learning_maintenance
    except Exception:
        return {
            "enabled": False,
            "ran": False,
            "reason": "learning-maintenance-unavailable",
        }

    try:
        result = run_scheduled_learning_maintenance(
            fundamentals_provider=lambda symbol: fundamentals if symbol.upper() == ticker.upper() else _fetch_fundamentals(_eodhd_code(symbol)),
            prefetched_fundamentals={ticker.upper(): fundamentals},
        )
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)
    except Exception as exc:
        logger.warning("Learning maintenance failed for %s: %s", ticker, exc)
        return {
            "enabled": False,
            "ran": False,
            "reason": "error",
            "detail": str(exc),
        }


def _safe_symbol_universe_store() -> Any | None:
    if not _live_learning_feedback_enabled() or not LEARNING_CONFIG.get("symbol_universe_enabled", True):
        return None
    try:
        from auto_valuation.learning.universe import SymbolUniverseStore

        return SymbolUniverseStore()
    except Exception:
        return None


def _safe_discovery_store() -> Any | None:
    if not _live_learning_feedback_enabled() or not LEARNING_CONFIG.get("symbol_universe_enabled", True):
        return None
    try:
        from auto_valuation.learning.discovery import DiscoveryStore

        return DiscoveryStore()
    except Exception:
        return None


def _record_peer_learning_signals(
    discovery_store: Any | None,
    *,
    ticker: str,
    company_name: str,
    exchange: str,
    country: str,
    sector: str,
    industry: str,
    peer_items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if discovery_store is None or not peer_items:
        return {}
    try:
        result = discovery_store.record_auto_peer_basket(
            {
                "ticker": ticker,
                "company_name": company_name,
                "exchange": exchange,
                "country": country,
                "sector": sector,
                "industry": industry,
            },
            peer_items,
        )
    except Exception:
        return {}
    relationships = list((result or {}).get("items") or [])
    return {
        str(item.get("peer_ticker") or "").strip().upper(): item
        for item in relationships
        if str(item.get("peer_ticker") or "").strip()
    }


def _merge_peer_learning_relationships(
    peers: list[dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
) -> None:
    if not peers or not relationships:
        return
    for peer in peers:
        ticker_text = str(peer.get("ticker") or peer.get("symbol") or "").strip().upper()
        relationship = relationships.get(ticker_text)
        if relationship is None:
            continue
        peer["pair_strength_score"] = round(float(relationship.get("pair_strength_score") or 0.0), 4)
        peer["pair_hits"] = int(relationship.get("pair_hits") or 0)
        peer["pair_auto_peer_hits"] = int(relationship.get("auto_peer_hits") or 0)
        peer["pair_manual_compare_hits"] = int(relationship.get("manual_compare_hits") or 0)
        peer["pair_last_seen_at"] = str(relationship.get("last_seen_at") or "")
        peer["peer_learning_score"] = round(
            float(peer.get("industry_fit_score") or 0.0)
            + float(peer.get("global_peer_score") or 0.0)
            + float(peer.get("pair_strength_score") or 0.0),
            4,
        )


def _top_learned_peer_edges(
    discovery_store: Any | None,
    ticker: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    ticker_text = str(ticker or "").strip().upper()
    if not ticker_text:
        return []
    try:
        if discovery_store is not None:
            relationships = discovery_store.list_peer_relationships(subject_ticker=ticker_text, limit=limit)
        else:
            from auto_valuation.learning.deployment_seed import peer_relationships as seeded_peer_relationships

            relationships = seeded_peer_relationships(subject_ticker=ticker_text, limit=limit)
    except Exception:
        try:
            from auto_valuation.learning.deployment_seed import peer_relationships as seeded_peer_relationships

            relationships = seeded_peer_relationships(subject_ticker=ticker_text, limit=limit)
        except Exception:
            return []

    items: list[dict[str, Any]] = []
    for relationship in relationships:
        payload = dict(relationship.get("payload") or {})
        peer = dict(payload.get("peer") or {})
        peer_ticker = str(relationship.get("peer_ticker") or peer.get("ticker") or "").strip().upper()
        if not peer_ticker:
            continue
        items.append(
            {
                "ticker": peer_ticker,
                "company_name": str(peer.get("company_name") or peer.get("name") or "").strip(),
                "exchange": str(peer.get("exchange") or "").strip().upper(),
                "sector": str(peer.get("sector") or "").strip(),
                "industry": str(peer.get("industry") or "").strip(),
                "canonical_industry": str(peer.get("canonical_industry") or "").strip(),
                "industry_family": str(peer.get("industry_family") or "").strip(),
                "peer_rank": peer.get("peer_rank"),
                "peer_learning_score": round(float(peer.get("peer_learning_score") or 0.0), 4),
                "base_peer_learning_score": round(float(peer.get("base_peer_learning_score") or 0.0), 4),
                "pair_strength_score": round(float(relationship.get("pair_strength_score") or 0.0), 4),
                "pair_strength_score_raw": round(float(relationship.get("pair_strength_score_raw") or 0.0), 4),
                "pair_decay_multiplier": round(float(relationship.get("pair_decay_multiplier") or 0.0), 4),
                "pair_age_days": round(float(relationship.get("pair_age_days") or 0.0), 2),
                "pair_hits": int(relationship.get("pair_hits") or 0),
                "pair_auto_peer_hits": int(relationship.get("auto_peer_hits") or 0),
                "pair_manual_compare_hits": int(relationship.get("manual_compare_hits") or 0),
                "pair_last_source": str(relationship.get("pair_last_source") or "").strip(),
                "last_seen_at": str(relationship.get("last_seen_at") or "").strip(),
            }
        )
    return items


def _register_global_universe_symbols(
    universe_store: Any | None,
    *,
    ticker: str,
    company_name: str,
    exchange: str,
    country: str,
    sector: str,
    industry: str,
    knowledge_model: dict[str, Any] | None,
    peer_items: list[dict[str, Any]] | None = None,
) -> None:
    if universe_store is None:
        return

    try:
        universe_store.upsert_symbol(
            ticker,
            company_name=company_name,
            exchange=exchange,
            country=country,
            sector=sector,
            industry=industry,
            source="dashboard-live",
            valued=True,
            fundamentals_cached=True,
        )

        analog_items = list(((knowledge_model or {}).get("analogs") or {}).get("items") or [])
        if analog_items:
            universe_store.record_candidates(
                [
                    {
                        **item,
                        "metadata": {
                            "score": item.get("score"),
                            "similarity": item.get("similarity"),
                            "regime_similarity": item.get("regime_similarity"),
                        },
                    }
                    for item in analog_items[:12]
                ],
                source="analog-evidence",
            )

        relationship_nodes = [
            node
            for node in list(((knowledge_model or {}).get("relationship_graph") or {}).get("nodes") or [])
            if str(node.get("ticker") or "").strip().upper() != str(ticker or "").strip().upper()
        ]
        if relationship_nodes:
            universe_store.record_candidates(
                [
                    {
                        **node,
                        "metadata": {
                            "score": node.get("score"),
                            "similarity": node.get("similarity"),
                            "role": node.get("role"),
                        },
                    }
                    for node in relationship_nodes[:16]
                ],
                source="relationship-graph",
            )

        if peer_items:
            universe_store.record_candidates(
                [
                    {
                        **item,
                        "metadata": {
                            "peer_subject_ticker": ticker,
                            "peer_subject_sector": sector,
                            "peer_subject_industry": industry,
                            "peer_learning_score": item.get("peer_learning_score"),
                            "similarity": item.get("industry_similarity"),
                            "score": item.get("peer_learning_score"),
                        },
                        "metadata_increments": {"peer_candidate_hits": 1},
                    }
                    for item in peer_items[:16]
                ],
                source="peer-comps",
            )
    except Exception:
        return


def _global_universe_summary(universe_store: Any | None) -> dict[str, Any]:
    seed_target = int(LEARNING_CONFIG.get("background_runner_seed_target_symbols", 1000) or 0)
    seed_prefix = int(LEARNING_CONFIG.get("background_runner_seed_prefix_per_cycle", 8) or 0)
    seed_pool_limit = int(LEARNING_CONFIG.get("background_runner_seed_pool_limit", 1000) or 0)
    try:
        from auto_valuation.learning.deployment_seed import background_runner_state as seeded_background_runner_state
        from auto_valuation.learning.deployment_seed import universe_summary as seeded_universe_summary

        snapshot_background_runner_state = seeded_background_runner_state()
        snapshot_summary = seeded_universe_summary()
    except Exception:
        snapshot_background_runner_state = {}
        snapshot_summary = {}
    try:
        from auto_valuation.learning.background_runner import read_background_runner_state

        background_runner_state = read_background_runner_state()
    except Exception:
        background_runner_state = {}
    if not background_runner_state and snapshot_background_runner_state:
        background_runner_state = snapshot_background_runner_state
    try:
        from webapp.data.ticker_search import seedable_tickers

        seed_pool_size = len(seedable_tickers(limit=seed_pool_limit if seed_pool_limit > 0 else None, common_stock_only=True))
    except Exception:
        seed_pool_size = 0

    def _effective_seed_pool_size(summary: dict[str, Any]) -> int:
        live_tracked = int(summary.get("tracked_symbols") or 0)
        bootstrapped = int(summary.get("bootstrapped_symbols") or 0)
        bounded_tracked = min(live_tracked, seed_pool_limit) if seed_pool_limit > 0 and live_tracked > 0 else live_tracked
        return max(seed_pool_size, bootstrapped, bounded_tracked)

    def _reconcile_background_runner_state(state: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
        runner_state = dict(state or {})
        live_tracked = int(summary.get("tracked_symbols") or 0)
        state_tracked = int(runner_state.get("tracked_symbols") or 0)
        if live_tracked >= 100 and (state_tracked <= 0 or state_tracked < max(12, live_tracked // 10)):
            runner_state["tracked_symbols"] = live_tracked
            runner_state["seed_pool_size"] = max(int(runner_state.get("seed_pool_size") or 0), _effective_seed_pool_size(summary))
            runner_state["state_reconciled"] = True
            runner_state["reconciled_reason"] = "runner-state-lower-than-live-universe"
            runner_state["requested_tickers"] = []
            runner_state["requested_exchanges"] = []
            runner_state["fetched_exchanges"] = []
            runner_state["bootstrap"] = {**dict(runner_state.get("bootstrap") or {}), "requested_tickers": []}
        return runner_state

    snapshot_payload = {
        **(snapshot_summary or {}),
        "background_target_symbols": seed_target,
        "background_seed_prefix_per_cycle": seed_prefix,
        "background_seed_pool_size": _effective_seed_pool_size(snapshot_summary or {}),
        "background_runner": _reconcile_background_runner_state(background_runner_state, snapshot_summary or {}),
    }

    if universe_store is None:
        if snapshot_summary:
            return {"enabled": True, **snapshot_payload}
        return {
            "enabled": False,
            "tracked_symbols": 0,
            "sector_span": 0,
            "exchange_span": 0,
            "bootstrapped_symbols": 0,
            "cached_fundamentals": 0,
            "recently_valued_symbols": 0,
            "stale_bootstrap_symbols": 0,
            "priority_candidates": [],
            "calibration_priority_candidates": [],
            "top_sectors": [],
            "background_target_symbols": seed_target,
            "background_seed_prefix_per_cycle": seed_prefix,
            "background_seed_pool_size": seed_pool_size,
            "background_runner": background_runner_state,
        }

    try:
        universe_summary_payload = dict(
            universe_store.summary(
                stale_after_hours=int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18)),
                recent_days=int(LEARNING_CONFIG.get("symbol_universe_recent_days", 21)),
            )
        )
        live_summary_base = {
            "enabled": True,
            **universe_summary_payload,
            "background_target_symbols": seed_target,
            "background_seed_prefix_per_cycle": seed_prefix,
            "background_seed_pool_size": _effective_seed_pool_size(universe_summary_payload),
        }
        live_summary = {
            **live_summary_base,
            "background_runner": _reconcile_background_runner_state(background_runner_state, live_summary_base),
        }
        if int(live_summary.get("tracked_symbols") or 0) > 0 or not snapshot_summary:
            return live_summary
        return {"enabled": True, **snapshot_payload}
    except Exception:
        if snapshot_summary:
            return {"enabled": True, **snapshot_payload}
        return {
            "enabled": False,
            "tracked_symbols": 0,
            "sector_span": 0,
            "exchange_span": 0,
            "bootstrapped_symbols": 0,
            "cached_fundamentals": 0,
            "recently_valued_symbols": 0,
            "stale_bootstrap_symbols": 0,
            "priority_candidates": [],
            "calibration_priority_candidates": [],
            "top_sectors": [],
            "background_target_symbols": seed_target,
            "background_seed_prefix_per_cycle": seed_prefix,
            "background_seed_pool_size": seed_pool_size,
            "background_runner": background_runner_state,
        }


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        dt_value = value
    else:
        try:
            dt_value = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def _auto_bootstrap_current_ticker(
    ticker: str,
    fundamentals: dict[str, Any],
    universe_store: Any | None,
) -> dict[str, Any]:
    if universe_store is None:
        return {
            "enabled": False,
            "ran": False,
            "reason": "symbol-universe-unavailable",
        }
    if not LEARNING_CONFIG.get("auto_bootstrap_current_ticker", True):
        return {
            "enabled": False,
            "ran": False,
            "reason": "disabled",
        }

    current = universe_store.get_symbol(ticker)
    stale_after_hours = int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18))
    last_bootstrapped_at = _parse_iso_datetime((current or {}).get("last_bootstrapped_at"))
    if last_bootstrapped_at is not None and datetime.now(timezone.utc) - last_bootstrapped_at < timedelta(hours=max(stale_after_hours, 1)):
        return {
            "enabled": True,
            "ran": False,
            "reason": "fresh",
            "last_bootstrapped_at": last_bootstrapped_at.isoformat(),
            "interval_hours": stale_after_hours,
        }

    try:
        from auto_valuation.learning.maintenance import run_live_evidence_bootstrap
    except Exception:
        return {
            "enabled": False,
            "ran": False,
            "reason": "bootstrap-unavailable",
        }

    try:
        result = run_live_evidence_bootstrap(
            tickers=[ticker.upper()],
            fundamentals_provider=lambda symbol: fundamentals if symbol.upper() == ticker.upper() else _fetch_fundamentals(_eodhd_code(symbol)),
            max_tickers=1,
            max_replay_predictions_per_ticker=int(LEARNING_CONFIG.get("auto_bootstrap_replay_predictions_per_ticker", 5)),
            replay_enabled=True,
        )
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        payload.setdefault("enabled", True)
        return payload
    except Exception as exc:
        logger.warning("Auto-bootstrap failed for %s: %s", ticker, exc)
        return {
            "enabled": True,
            "ran": False,
            "reason": "error",
            "detail": str(exc),
        }


def _parse_iso_date(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


def _fiscal_year_end_components(data: dict[str, Any]) -> tuple[int, int]:
    historical_dates = list((data.get("historical") or {}).get("dates") or [])
    if historical_dates:
        parsed = _parse_iso_date(historical_dates[-1])
        if parsed is not None:
            return parsed.month, parsed.day

    month_label = str(data.get("fiscal_year_end_month") or "December").strip()
    try:
        month = datetime.strptime(month_label, "%B").month
    except ValueError:
        try:
            month = datetime.strptime(month_label[:3], "%b").month
        except ValueError:
            month = 12
    return month, 31


def _augment_learning_explainability(
    knowledge_model: dict[str, Any],
    *,
    sector: str = "",
    industry: str = "",
    ticker: str = "",
    dashboard_data: dict[str, Any] | None = None,
) -> None:
    explainability = dict(knowledge_model.get("explainability") or {})
    if not explainability:
        return

    backfill = dict(knowledge_model.get("learning_backfill") or {})
    bootstrap = dict(knowledge_model.get("learning_bootstrap") or {})
    maintenance = dict(knowledge_model.get("learning_maintenance") or {})
    persistence = dict(knowledge_model.get("learning_persistence") or {})
    ledger_evidence = _ledger_evidence_summary(ticker)
    historical_evidence = _historical_replay_evidence_summary(ticker)
    readonly_maintenance = _latest_maintenance_summary()
    readonly_snapshot = _read_only_snapshot_summary(dashboard_data or {})
    global_universe = dict(knowledge_model.get("global_universe") or {})
    relationship_graph = dict(explainability.get("relationship_graph") or knowledge_model.get("relationship_graph") or {})
    learned_peer_edges = list(knowledge_model.get("learned_peer_edges") or relationship_graph.get("learned_peer_edges") or [])
    if learned_peer_edges:
        relationship_graph["learned_peer_edges"] = learned_peer_edges[:5]
        explainability["learned_peer_edges"] = learned_peer_edges[:5]
    if relationship_graph:
        explainability["relationship_graph"] = relationship_graph
    if global_universe:
        explainability["global_universe"] = global_universe

    backfill_matured = int(backfill.get("matured_records") or 0)
    ledger_matured = int(ledger_evidence.get("matured_records") or 0)
    historical_records = int(historical_evidence.get("records") or 0)
    ledger_matured_records = max(backfill_matured, ledger_matured)
    matured_records = max(ledger_matured_records, historical_records)
    total_realized_records = int(ledger_evidence.get("total_realized_records") or 0)
    postmortem_records = int(ledger_evidence.get("postmortem_records") or 0)
    historical_annual_records = int(historical_evidence.get("annual_records") or 0)
    historical_quarterly_records = int(historical_evidence.get("quarterly_records") or 0)
    historical_mix = (
        f"{historical_records} replay observation(s) ({historical_annual_records} annual, {historical_quarterly_records} quarterly)"
        if historical_annual_records or historical_quarterly_records
        else f"{historical_records} replay observation(s)"
    )
    explainability["realized_evidence"] = {
        "matured_records": matured_records,
        "ledger_matured_records": ledger_matured_records,
        "historical_replay_records": historical_records,
        "historical_replay_annual_records": historical_annual_records,
        "historical_replay_quarterly_records": historical_quarterly_records,
        "historical_replay_first_year": historical_evidence.get("first_year"),
        "historical_replay_last_year": historical_evidence.get("last_year"),
        "updated_records": int(backfill.get("updated_records") or 0),
        "total_realized_records": total_realized_records,
        "complete_realized_records": int(ledger_evidence.get("complete_realized_records") or 0),
        "partial_realized_records": int(ledger_evidence.get("partial_realized_records") or 0),
        "postmortem_records": postmortem_records,
        "prediction_records": int(ledger_evidence.get("prediction_records") or 0),
        "mean_abs_historical_revenue_error_pct": historical_evidence.get("mean_abs_revenue_error_pct"),
        "mean_abs_historical_margin_error_pp": historical_evidence.get("mean_abs_margin_error_pp"),
        "source": "live-backfill" if backfill_matured > 0 else str(ledger_evidence.get("source") or "live-backfill"),
        "note": (
            f"{historical_mix}, {ledger_matured_records} ledger back-test(s), {total_realized_records} realized label(s), and {postmortem_records} postmortem(s) exist for this ticker."
            if matured_records > 0
            else "No matured realized outcomes exist yet for this ticker."
        ),
    }
    explainability["learning_bootstrap"] = {
        "enabled": bool(bootstrap.get("enabled") or False),
        "ran": bool(bootstrap.get("ran") or False),
        "reason": bootstrap.get("reason"),
        "replay_predictions_created": int(bootstrap.get("replay_predictions_created") or 0),
        "realized_outcomes_created": int(bootstrap.get("realized_outcomes_created") or 0),
        "last_bootstrapped_at": bootstrap.get("last_bootstrapped_at"),
    }
    if not maintenance.get("ran") and maintenance.get("reason") == "mutate_learning disabled" and readonly_maintenance.get("last_run_at"):
        maintenance = readonly_maintenance
    explainability["maintenance"] = {
        "ran": bool(maintenance.get("ran")),
        "reason": maintenance.get("reason"),
        "scanned_tickers": int(maintenance.get("scanned_tickers") or 0),
        "matured_records": int(maintenance.get("matured_records") or 0),
        "backfilled_records": int(maintenance.get("backfilled_records") or 0),
        "annual_postmortems_created": int(maintenance.get("annual_postmortems_created") or 0),
        "quinquennial_reports_created": int(maintenance.get("quinquennial_reports_created") or 0),
        "last_run_at": maintenance.get("last_run_at"),
    }
    if not persistence.get("persisted") and persistence.get("reason") == "mutate_learning disabled":
        persistence = readonly_snapshot
    explainability["current_snapshot"] = persistence

    data_gaps = list(explainability.get("data_gaps") or [])
    titles = {str(gap.get("title")) for gap in data_gaps}
    if matured_records == 0 and "No ticker-specific realized evidence" not in titles:
        data_gaps.append(
            {
                "title": "No ticker-specific realized evidence",
                "detail": "This ticker has not yet rolled into matured forecast years, so its own company memory has not been back-tested in the shared ledger.",
                "severity": "amber",
            }
        )
    if not maintenance.get("ran") and maintenance.get("reason") not in ("throttled", "last-run", None) and "Maintenance is behind" not in titles:
        data_gaps.append(
            {
                "title": "Maintenance is behind",
                "detail": "Scheduled postmortems did not complete on this pass, so realized cohort evidence may be less current than the live fundamentals feed.",
                "severity": "amber",
            }
        )
    if global_universe and int(global_universe.get("tracked_symbols") or 0) < 12 and "Global universe is still shallow" not in titles:
        data_gaps.append(
            {
                "title": "Global universe is still shallow",
                "detail": (
                    f"Only {int(global_universe.get('tracked_symbols') or 0)} tracked symbol(s) are enrolled in the shared registry, "
                    "so cross-symbol transfer breadth is still growing."
                ),
                "severity": "amber",
            }
        )
    explainability["data_gaps"] = data_gaps

    if knowledge_model.get("layered_learning"):
        confidence_model = build_ranked_confidence_model(
            {
                **knowledge_model,
                "explainability": explainability,
                "sector": sector,
                "industry": industry,
            }
        )
        explainability["confidence_decomposition"] = {
            "summary": confidence_model["summary"],
            "dominant_risk": confidence_model["dominant_risk"],
            "assumption_confidence": dict(confidence_model["assumption_confidence"]),
            "valuation_confidence": dict(confidence_model["valuation_confidence"]),
            "components": list(confidence_model["components"]),
        }
        knowledge_model["confidence_model"] = confidence_model
        knowledge_model["learning_confidence"] = round(float(confidence_model["assumption_confidence"]["score"] or 0.0), 2)
        knowledge_model["assumption_confidence"] = round(float(confidence_model["assumption_confidence"]["score"] or 0.0), 2)
        knowledge_model["valuation_confidence"] = round(float(confidence_model["valuation_confidence"]["score"] or 0.0), 2)
        knowledge_model["confidence_ranking_signal"] = float(confidence_model["ranking_signal"])
        knowledge_model["expected_valuation_error_pct"] = float(confidence_model["valuation_confidence"]["expected_error_pct"]["p50"])
        knowledge_model["expected_valuation_error_band"] = dict(confidence_model["valuation_confidence"]["expected_error_pct"])
        memory_hierarchy = dict(explainability.get("memory_hierarchy") or knowledge_model.get("memory_hierarchy") or {})
        if memory_hierarchy:
            episodic = dict(memory_hierarchy.get("episodic") or {})
            if episodic:
                episodic["matured_records"] = int(backfill.get("matured_records") or 0)
                episodic["updated_records"] = int(backfill.get("updated_records") or 0)
                if int(backfill.get("matured_records") or 0) > 0:
                    episodic["note"] = (
                        f"Ticker-specific episodic memory now has {int(backfill.get('matured_records') or 0)} matured record(s) "
                        f"and {int(backfill.get('updated_records') or 0)} refreshed label(s)."
                    )
                memory_hierarchy["episodic"] = episodic

            relational = dict(memory_hierarchy.get("relational") or {})
            if relational and relationship_graph:
                relational["connected_tickers"] = list(relationship_graph.get("connected_tickers") or [])[:5]
                relational["note"] = relationship_graph.get("note") or relational.get("note")
                memory_hierarchy["relational"] = relational

            procedural = dict(memory_hierarchy.get("procedural") or {})
            if procedural:
                procedural["maintenance_ran"] = bool(maintenance.get("ran"))
                procedural["maintenance_reason"] = maintenance.get("reason")
                procedural["assumption_confidence_score"] = int(confidence_model["assumption_confidence"]["score_100"])
                procedural["valuation_confidence_score"] = int(confidence_model["valuation_confidence"]["score_100"])
                memory_hierarchy["procedural"] = procedural

            ordered_layers = []
            for key in ("episodic", "semantic", "relational", "procedural"):
                layer = memory_hierarchy.get(key)
                if isinstance(layer, dict) and layer:
                    ordered_layers.append(layer)
            if ordered_layers:
                memory_hierarchy["layers"] = ordered_layers
            explainability["memory_hierarchy"] = memory_hierarchy
            knowledge_model["memory_hierarchy"] = memory_hierarchy

    model_accuracy = _learning_accuracy_summary(
        ticker,
        sector=sector,
        industry=industry,
        knowledge_model={**knowledge_model, "explainability": explainability},
    )
    learned_scenario_engine = _learned_scenario_summary(
        dashboard_data or {},
        knowledge_model=knowledge_model,
        accuracy=model_accuracy,
    )
    explainability["model_accuracy"] = model_accuracy
    explainability["learned_scenario_engine"] = learned_scenario_engine
    knowledge_model["model_accuracy"] = model_accuracy
    knowledge_model["learned_scenario_engine"] = learned_scenario_engine
    if dashboard_data is not None:
        dashboard_data["model_accuracy"] = model_accuracy
        dashboard_data["model_view"] = learned_scenario_engine
        if learned_scenario_engine.get("enabled"):
            dashboard_data["learned_expected_upside_pct"] = learned_scenario_engine.get("expected_upside_pct")
            dashboard_data["learned_recommendation"] = learned_scenario_engine.get("recommendation")
    knowledge_model["explainability"] = explainability


# ── Public interface ──────────────────────────────────────────────────────────

def is_available() -> bool:
    return _REQUESTS_OK


def _eodhd_code(ticker: str) -> str:
    """Map a user-supplied ticker to an EODHD code via normalize_requested_ticker."""
    return normalize_requested_ticker(ticker)


def build_dashboard_data(
    ticker: str,
    overrides: dict | None = None,
    *,
    mutate_learning: bool = True,
    historical_years: int | None = 10,
) -> dict | None:  # noqa: C901
    """
    Fetch live data from EODHD and run a full 7-year DCF.
    Returns the complete dashboard dict (same schema as yfinance_client),
    or None on any failure so the caller can fall back to yfinance.

    *overrides* (optional): dict of user-supplied parameters that replace
    auto-computed assumptions before the DCF is run.  Recognised keys:
        wacc, g (terminal growth), revenue_growth_near, ebit_margin_target,
        da_pct, capex_pct, sbc_pct, tax_rate, beta

    *mutate_learning* controls whether the live request is allowed to write
    bootstrap, peer-learning, and persistence side effects.

    *historical_years* caps annual financial statements; ``None`` uses every
    yearly period available from EODHD.
    """
    if not _REQUESTS_OK:
        return None

    try:
        eodhd_code = _eodhd_code(ticker)

        # ── Fetch price and fundamentals ──────────────────────────────────
        price_data = _fetch_price(eodhd_code)
        fund       = _fetch_fundamentals(eodhd_code)

        if fund is None:
            logger.warning("EODHD: no fundamentals for %s", ticker)
            return None

        price_raw = _sf(price_data.get("close") if price_data else 0)
        if price_raw <= 0:
            # Try Highlights.WallStreetTargetPrice as last resort — but we really
            # need a real price, so return None.
            logger.warning("EODHD: no valid price for %s", ticker)
            return None

        price = round(price_raw, 2)

        # ── Parse company info ────────────────────────────────────────────
        gen   = fund.get("General", {})
        hi    = fund.get("Highlights", {})
        tech  = fund.get("Technicals", {})
        share = fund.get("SharesStats", {})
        anal  = fund.get("AnalystRatings", {})
        fins  = fund.get("Financials", {})
        earn  = fund.get("Earnings", {})

        company_name = gen.get("Name") or ticker
        exchange     = gen.get("Exchange") or "US"
        quote_currency = (gen.get("CurrencyCode") or "USD").upper()

        # Determine the reporting currency from the financial statements
        # (first IS period's currency_symbol, fallback to USD)
        _stmt_ccy: str = "USD"
        for _sec in (fins.get("Income_Statement") or {}, fins.get("Balance_Sheet") or {}):
            for _p in (_sec.get("yearly") or {}).values():
                _c = (_p.get("currency_symbol") or "").upper()
                if _c:
                    _stmt_ccy = _c
                    break
            if _stmt_ccy != "USD":
                break
        reporting_currency = _stmt_ccy

        # Convert the quoted price to reporting currency for consistent DCF arithmetic.
        # E.g. BHP.LSE quotes in GBX (pence) but statements are in USD.
        _fx = _get_fx_rate(quote_currency) / max(_get_fx_rate(reporting_currency), 1e-9)
        price = round(price_raw * _fx, 2)

        # Always expose "currency" as the reporting/statement currency so the DCF
        # figures are comparable to a USD-listed peer.
        currency = reporting_currency

        sector       = gen.get("Sector") or ""
        industry     = gen.get("Industry") or ""
        description  = gen.get("Description") or f"{company_name} is a publicly traded company."
        if len(description) > 400:
            description = description[:397] + "..."

        # ── Parse financial statements ────────────────────────────────────
        is_sec = fins.get("Income_Statement", {})
        bs_sec = fins.get("Balance_Sheet", {})
        cf_sec = fins.get("Cash_Flow", {})

        history_limit = None if historical_years is None else max(1, int(historical_years))
        is_periods = _sorted_yearly(is_sec, history_limit)   # newest-first
        bs_periods = _sorted_yearly(bs_sec, history_limit)
        cf_periods = _sorted_yearly(cf_sec, history_limit)

        # ── Shares outstanding ────────────────────────────────────────────
        shares_raw = _shares_to_millions(
            share.get("SharesOutstanding")
            or share.get("SharesFloat")
            or (bs_periods[0].get("commonStockSharesOutstanding") if bs_periods else 0)
            or 0
        )
        diluted_shares = shares_raw
        if diluted_shares < 1:
            logger.warning("EODHD: invalid shares for %s", ticker)
            return None

        if not is_periods or not bs_periods or not cf_periods:
            logger.warning("EODHD: incomplete financials for %s", ticker)
            return None

        n = min(len(is_periods), len(bs_periods), len(cf_periods))
        if n < 1:
            return None

        # Fiscal year labels — extract year from the "date" field of each period
        fy_years_desc = []
        for p in is_periods[:n]:
            date_str = p.get("date") or ""
            try:
                fy_years_desc.append(int(date_str[:4]))
            except (ValueError, TypeError):
                break
        if not fy_years_desc:
            logger.warning("EODHD: can't parse fiscal years for %s", ticker)
            return None

        fy_years_asc = sorted(set(fy_years_desc))  # oldest first, deduped
        n = len(fy_years_asc)

        # Align periods to deduplicated year list (if same year appears twice, keep first)
        _seen_years: set[int] = set()
        _is_p_aligned, _bs_p_aligned, _cf_p_aligned = [], [], []
        for idx, p in enumerate(is_periods):
            yr = int((p.get("date") or "0000")[:4])
            if yr not in _seen_years and idx < len(bs_periods) and idx < len(cf_periods):
                _seen_years.add(yr)
                _is_p_aligned.append(p)
                _bs_p_aligned.append(bs_periods[idx])
                _cf_p_aligned.append(cf_periods[idx])

        # Reverse to oldest-first for array building
        _is_asc = list(reversed(_is_p_aligned))
        _bs_asc = list(reversed(_bs_p_aligned))
        _cf_asc = list(reversed(_cf_p_aligned))

        M = 1e6   # divisor → local-currency millions

        # ── BDR / Cross-listing mislabeled currency fix ───────────────────
        # EODHD stores the US parent company's USD financial statements for
        # Brazilian BDRs (exchange=SA) and similar cross-listed instruments, but
        # labels the IS currency_symbol as BRL (or ARS for BA).  All the IS/BS/CF
        # figures therefore need to be multiplied by the USD→local FX rate to
        # produce correct local-currency values.
        # Detection: SA/BA exchange AND ticker code follows the BDR naming pattern
        # (e.g. NIKE34, MSFT34, AAPL34 end with "34"; some end with "33" or "11B").
        _bdr_code = eodhd_code.split(".")[0].upper()
        _is_bdr = (
            exchange in {"SA", "BA"}
            and reporting_currency not in {"USD", ""}
            and (
                _bdr_code.endswith("34")
                or _bdr_code.endswith("33")
                or _bdr_code.endswith("11B")
                or _bdr_code.endswith("32")
            )
        )
        if _is_bdr:
            # _get_fx_rate returns USD per 1 local unit (e.g. BRL = 0.2013 USD/BRL).
            # To convert USD_raw → local CCY millions: USD_raw * (1/usd_per_local) / 1e6
            #                                         = USD_raw / (1e6 * usd_per_local)
            # So correct M = 1e6 * usd_per_local
            _usd_per_local = _get_fx_rate(reporting_currency) / max(_get_fx_rate("USD"), 1e-9)
            if _usd_per_local < 0.9:  # Only when local ccy is weaker than USD (BRL≈0.2, ARS≈0.001)
                M = 1e6 * _usd_per_local  # e.g. BRL: M = 1e6 * 0.2013 = 201,300
                logger.debug(
                    "EODHD BDR currency fix: %s exchange=%s USD→%s usd_per_local=%.4f new_M=%.0f",
                    ticker, exchange, reporting_currency, _usd_per_local, M,
                )

        def _is_v(i: int, field: str) -> float:
            return _sf(_is_asc[i].get(field)) / M

        def _bs_v(i: int, field: str) -> float:
            return _sf(_bs_asc[i].get(field)) / M

        def _cf_v(i: int, field: str) -> float:
            return _sf(_cf_asc[i].get(field)) / M

        # ── Historical arrays (oldest → newest, $M) ───────────────────────
        revenues       = [round(_is_v(i, "totalRevenue"))      for i in range(n)]
        gross_profits  = [round(_is_v(i, "grossProfit"))       for i in range(n)]
        ebits          = [round(_is_v(i, "ebit"))              for i in range(n)]
        net_incomes    = [round(_is_v(i, "netIncome"))         for i in range(n)]
        pretax_incomes = [round(_is_v(i, "incomeBeforeTax"))   for i in range(n)]
        tax_provs      = [round(_is_v(i, "taxProvision"))      for i in range(n)]

        op_cfs = [round(_cf_v(i, "totalCashFromOperatingActivities")) for i in range(n)]
        capexes= [round(abs(_sf(_cf_asc[i].get("capitalExpenditures")) / M)) for i in range(n)]

        # Free cash flow: prefer explicit field, else op_cf - capex
        fcfs = []
        for i in range(n):
            fcf_raw = _sf(_cf_asc[i].get("freeCashFlow"))
            fcfs.append(round(fcf_raw / M) if fcf_raw != 0 else op_cfs[i] - capexes[i])

        # D&A: prefer CF depreciation, else IS reconciledDepreciation
        das = []
        for i in range(n):
            da_cf = abs(_sf(_cf_asc[i].get("depreciation")))
            if da_cf == 0:
                da_cf = abs(_sf(_is_asc[i].get("reconciledDepreciation")))
            if da_cf == 0:
                da_cf = abs(_sf(_is_asc[i].get("depreciationAndAmortizationCumulative")))
            das.append(round(da_cf / M))

        sbcs  = [round(abs(_sf(_cf_asc[i].get("stockBasedCompensation"))) / M) for i in range(n)]

        # Balance sheet arrays
        total_assets_h    = [round(_bs_v(i, "totalAssets"))               for i in range(n)]
        total_equity_h    = [round(_bs_v(i, "totalStockholderEquity"))     for i in range(n)]
        total_debts_h     = [round(
            (_sf(_bs_asc[i].get("shortLongTermDebtTotal"))
             or _sf(_bs_asc[i].get("shortLongTermDebt")) + _sf(_bs_asc[i].get("longTermDebtTotal"))
             or _sf(_bs_asc[i].get("longTermDebtTotal"))
            ) / M
        ) for i in range(n)]
        hist_shares_h     = []
        for i in range(n):
            share_count = _shares_to_millions(
                _bs_asc[i].get("commonStockSharesOutstanding")
                or share.get("SharesOutstanding")
                or share.get("SharesFloat")
                or 0
            )
            hist_shares_h.append(round(share_count, 1) if share_count > 0 else round(diluted_shares, 1))

        # Use cashAndEquivalents (excludes short-term investments) to match
        # standard financial statement definitions (e.g. S&P Capital IQ convention).
        # Fall back to broader measures only if narrow field is absent.
        cash_h = [round(
            (   _sf(_bs_asc[i].get("cashAndEquivalents"))
             or _sf(_bs_asc[i].get("cash"))
             or _sf(_bs_asc[i].get("cashAndShortTermInvestments"))
            ) / M
        ) for i in range(n)]

        # Derived: gross margin / EBIT margin per year
        gross_margins = [round(gp / r * 100, 1) if r else 0 for gp, r in zip(gross_profits, revenues)]
        ebit_margins  = [round(e  / r * 100, 1) if r else 0 for e,  r in zip(ebits,        revenues)]
        attributable_earnings_ratio, attributable_earnings_note = _attributable_earnings_adjustment(net_incomes, ebits)

        # ROIC per year: NOPAT / (Equity + LT Debt)
        roics = []
        for i in range(n):
            ebit_i    = _is_v(i, "ebit")
            pre_i     = _is_v(i, "incomeBeforeTax")
            tax_i     = _is_v(i, "taxProvision")
            tr_i      = (tax_i / pre_i) if pre_i > 0 else 0.21
            nopat_i   = ebit_i * (1 - tr_i)
            eq_i      = _bs_v(i, "totalStockholderEquity")
            ltd_i     = _bs_v(i, "longTermDebtTotal")
            inv_cap_i = eq_i + ltd_i
            roics.append(round(nopat_i / inv_cap_i * 100, 1) if inv_cap_i > 0 else 0.0)

        # ── Extended historical arrays for Excel export ───────────────────
        int_exp_h_arr    = [round(abs(_sf(_is_asc[i].get("interestExpense"))) / M) for i in range(n)]
        # Interest income: EODHD does not reliably expose this as a separate line item
        # (it sometimes bundles FX gains and other non-op items into "interestIncome").
        # Keep as a dedicated field lookup only — no fallback derivation.
        int_inc_h_arr    = [round(abs(_sf(_is_asc[i].get("interestIncome") or
                                         _is_asc[i].get("interest_income") or 0)) / M)
                            for i in range(n)]
        acct_recv_h_arr  = [round(abs(_sf(_bs_asc[i].get("netReceivables"))) / M) for i in range(n)]
        inv_h_arr        = [round(abs(_sf(_bs_asc[i].get("inventory"))) / M) for i in range(n)]
        acct_pay_h_arr   = [round(abs(_sf(_bs_asc[i].get("accountsPayable"))) / M) for i in range(n)]
        ca_h_arr         = [round(abs(_sf(_bs_asc[i].get("totalCurrentAssets"))) / M) for i in range(n)]
        cl_h_arr         = [round(abs(_sf(_bs_asc[i].get("totalCurrentLiabilities"))) / M) for i in range(n)]
        net_ppe_h_arr    = [round(abs(_sf(_bs_asc[i].get("propertyPlantEquipment"))) / M) for i in range(n)]
        goodwill_h_arr   = [round(abs(_sf(
                                _bs_asc[i].get("goodwill") or
                                _bs_asc[i].get("Goodwill") or
                                _bs_asc[i].get("goodWill") or 0)) / M) for i in range(n)]
        intang_h_arr     = [round(abs(_sf(_bs_asc[i].get("intangibleAssets") or 0)) / M) for i in range(n)]
        # Gross PP&E and Accumulated Depreciation (EODHD may or may not expose these)
        gross_ppe_h_arr  = [round(abs(_sf(
                                _bs_asc[i].get("propertyPlantEquipmentGross") or
                                _bs_asc[i].get("grossPropertyPlantEquipment") or
                                _bs_asc[i].get("grossPPE") or 0)) / M) for i in range(n)]
        accum_dep_h_arr  = [round(-abs(_sf(
                                _bs_asc[i].get("accumulatedDepreciation") or
                                _bs_asc[i].get("accumulatedAmortization") or 0)) / M) for i in range(n)]
        ret_earn_h_arr   = [round(_sf(_bs_asc[i].get("retainedEarnings") or 0) / M) for i in range(n)]
        div_paid_h_arr   = [round(abs(_sf(_cf_asc[i].get("dividendsPaid") or 0)) / M) for i in range(n)]
        _sps_raw         = [_sf(_cf_asc[i].get("salePurchaseOfStock") or 0) / M for i in range(n)]
        buyback_h_arr    = [round(max(0.0, -v)) for v in _sps_raw]
        # Stock issuances: prefer dedicated EODHD field; fall back to SBC as proxy
        # (Nike is a net repurchaser so salePurchaseOfStock is always negative → gives 0)
        _iss_raw = [_sf(_cf_asc[i].get("issuanceOfCapitalStock")
                        or _cf_asc[i].get("commonStockIssuance")
                        or _cf_asc[i].get("proceedsFromIssuanceOfCommonStock") or 0) / M
                    for i in range(n)]
        stock_iss_h_arr = [round(max(0.0, v)) for v in _iss_raw]
        # If no dedicated issuance field, use SBC as a proxy (option exercise proceeds ≈ SBC)
        if not any(stock_iss_h_arr):
            stock_iss_h_arr = sbcs[:]
        net_borr_h_arr   = [round(_sf(_cf_asc[i].get("netBorrowings") or 0) / M) for i in range(n)]
        # Actual fiscal year end dates from EODHD period data (oldest first)
        _fy_dates: list = []
        for _p in reversed(_is_p_aligned):
            _ds = _p.get("date") or ""
            try:
                _fy_dates.append(datetime.strptime(_ds[:10], "%Y-%m-%d"))
            except (ValueError, TypeError):
                _fy_dates.append(None)
        fy_end_month_str = gen.get("FiscalYearEnd") or "December"

        # ── Most-recent year scalars ──────────────────────────────────────
        # Index -1 of ascending arrays = most recent year
        l_is = _is_asc[-1]
        l_bs = _bs_asc[-1]
        l_cf = _cf_asc[-1]

        revenue_base     = _sf(l_is.get("totalRevenue"))    / M
        gross_profit     = _sf(l_is.get("grossProfit"))     / M
        ebit_base        = _sf(l_is.get("ebit"))            / M
        net_income_base  = _sf(l_is.get("netIncome"))       / M
        pretax_income    = _sf(l_is.get("incomeBeforeTax")) / M
        tax_prov         = _sf(l_is.get("taxProvision"))    / M
        interest_expense = abs(_sf(l_is.get("interestExpense"))) / M

        # ── TTM EBIT margin override ───────────────────────────────────────
        # For companies with improving profitability (e.g., cloud margin expansion),
        # the most-recent-annual EBIT margin can lag the current run-rate.
        # Use OperatingMarginTTM from Highlights when it is HIGHER than the annual
        # figure — this avoids penalising companies with one-time annual charges
        # while capturing genuine, sustained margin improvement.
        _op_margin_ttm = hi.get("OperatingMarginTTM") or 0
        _ebit_m_annual = ebit_base / (abs(_sf(l_is.get("totalRevenue"))) / M)\
            if abs(_sf(l_is.get("totalRevenue"))) > 0 else 0
        if 0.02 < _op_margin_ttm < 0.75 and _op_margin_ttm > _ebit_m_annual and revenue_base > 0:
            ebit_base = revenue_base * _op_margin_ttm

        operating_cf = _sf(l_cf.get("totalCashFromOperatingActivities")) / M
        capex        = abs(_sf(l_cf.get("capitalExpenditures"))) / M
        da           = das[-1]
        sbc          = sbcs[-1]
        buyback_raw  = abs(_sf(l_cf.get("salePurchaseOfStock"))) / M

        total_assets    = total_assets_h[-1]
        total_debt      = total_debts_h[-1]
        cash            = cash_h[-1]
        total_equity    = total_equity_h[-1]

        inventory     = abs(_sf(l_bs.get("inventory")))     / M
        accounts_recv = abs(_sf(l_bs.get("netReceivables"))) / M
        accounts_pay  = abs(_sf(l_bs.get("accountsPayable"))) / M
        cur_assets    = abs(_sf(l_bs.get("totalCurrentAssets"))) / M
        cur_liab      = abs(_sf(l_bs.get("totalCurrentLiabilities"))) / M
        retained_earn = _sf(l_bs.get("retainedEarnings")) / M
        total_liab    = abs(_sf(l_bs.get("totalLiab") or l_bs.get("totalLiabilities"))) / M
        long_term_debt= abs(_sf(l_bs.get("longTermDebtTotal"))) / M
        working_cap   = cur_assets - cur_liab
        stockholders_eq = total_equity

        net_debt   = total_debt - cash
        market_cap = round(price * diluted_shares)   # price * shares_in_M = $M

        # ── WACC ──────────────────────────────────────────────────────────
        beta = _sf(tech.get("Beta") or hi.get("Beta") or 1.0)
        beta = max(0.3, min(3.0, beta if beta != 0 else 1.0))

        rf_rate = _get_risk_free_rate()
        erp     = 5.2
        # Blume (1971) adjustment pulls extreme betas toward 1.0: β_adj = 0.67·β + 0.33
        # Bloomberg and most sell-side models apply this by default.
        beta_adj = 0.67 * beta + 0.33
        ke      = rf_rate + beta_adj * erp

        kd_pre  = (interest_expense / total_debt * 100) if total_debt > 1 and interest_expense > 0 else max(2.0, min(8.0, rf_rate + 1.5))
        kd_pre  = max(2.0, min(12.0, kd_pre))

        statutory_tax_rate = _statutory_tax_rate(gen.get("CountryName") or gen.get("Country") or "")
        if pretax_income > 1 and tax_prov > 0:
            effective_tax_rate = tax_prov / pretax_income * 100
            tax_rate_pct = statutory_tax_rate if effective_tax_rate < 10 else effective_tax_rate
        else:
            tax_rate_pct = statutory_tax_rate
        tax_rate_pct = max(5.0, min(45.0, tax_rate_pct))

        kd_post = kd_pre * (1 - tax_rate_pct / 100)

        total_cap = market_cap + total_debt
        e_wt      = market_cap / total_cap * 100 if total_cap > 0 else 85.0
        d_wt      = 100 - e_wt

        wacc = round(max(5.0, min(20.0, (e_wt / 100) * ke + (d_wt / 100) * kd_post)), 1)

        # ── Operating assumptions ─────────────────────────────────────────
        ebit_margin_base_pct  = ebit_base   / revenue_base * 100 if revenue_base > 0 else 10.0
        gross_margin_base_pct = gross_profit/ revenue_base * 100 if revenue_base > 0 else 40.0
        da_pct    = da    / revenue_base * 100 if revenue_base > 0 else 2.5
        capex_pct = capex / revenue_base * 100 if revenue_base > 0 else 3.0
        sbc_pct   = sbc   / revenue_base * 100 if revenue_base > 0 else 1.5

        # Steady-state capex: heavy investment cycles (AI infra, logistics build-out)
        # should normalise. Net capex (capex − D&A) converges to ≤ 3 pp of revenue.
        capex_steady_pct = min(capex_pct, da_pct + 3.0)
        if capex_pct > da_pct * 1.5:
            capex_steady_pct = max(capex_steady_pct, da_pct * 1.3)

        # Revenue CAGR over full available history, but use a 5-year window for
        # high-growth names so very old low-base years do not depress the run-rate.
        if len(revenues) >= 2 and revenues[0] > 0 and revenues[-1] > 0:
            n_rev_yrs = len(revenues) - 1
            rev_cagr  = (revenues[-1] / revenues[0]) ** (1.0 / n_rev_yrs) - 1
            recent_revenues = revenues[-6:] if len(revenues) > 6 else revenues
            if len(recent_revenues) >= 2 and recent_revenues[0] > 0 and recent_revenues[-1] > 0:
                recent_years = len(recent_revenues) - 1
                recent_rev_cagr = (recent_revenues[-1] / recent_revenues[0]) ** (1.0 / recent_years) - 1
            else:
                recent_rev_cagr = rev_cagr
            last_rev_growth = (revenues[-1] / revenues[-2] - 1) if len(revenues) >= 2 and revenues[-2] > 0 else rev_cagr
            selected_cagr = recent_rev_cagr if max(recent_rev_cagr, last_rev_growth) * 100 > 15 else rev_cagr
            revenue_growth_near = round(max(-15.0, min(50.0, selected_cagr * 100)), 1)
        else:
            revenue_growth_near = 5.0

        # Blend analyst consensus (+1y) into near-term growth.  When analysts
        # have a forward view, their estimate outweighs the backward-looking CAGR
        # (60 / 40 split).  This makes the base-case far more market-realistic
        # and prevents the scenarios from diverging from what analysts actually expect.
        _consensus_growth = _extract_consensus_growth(earn, revenue_base)
        if _consensus_growth is not None:
            revenue_growth_near = round(
                0.4 * revenue_growth_near + 0.6 * _consensus_growth, 1
            )
            revenue_growth_near = max(-15.0, min(50.0, revenue_growth_near))

        # ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (F5) — full anchoring to consensus
        # Year 1 when ≥3 analysts cover the name.  Heavy-coverage names get
        # a 90/10 blend (analysts dominate), thin-coverage stays at 60/40.
        _n_analysts_rev = 0
        try:
            _trend = (earn or {}).get("Trend") or {}
            _plus1 = next(
                (e for e in _trend.values()
                 if isinstance(e, dict) and str(e.get("period") or "").lower() == "+1y"),
                None,
            )
            _n_analysts_rev = int(_sf((_plus1 or {}).get("revenueNumberOfAnalysts"), default=0) or 0)
            if _n_analysts_rev >= 3 and _consensus_growth is not None:
                revenue_growth_near = round(
                    0.10 * revenue_growth_near + 0.90 * _consensus_growth, 1
                )
                revenue_growth_near = max(-15.0, min(50.0, revenue_growth_near))
        except Exception:
            pass

        # Sector-aware terminal growth: Technology companies (cloud, AI, software,
        # semiconductors) have structural advantages — scale, R&D compounding, and
        # secular AI tailwinds — that sustain above-GDP nominal growth long-term.
        # Non-tech industries default to nominal GDP proxy (2.5%).
        terminal_growth = 3.0 if sector.lower() == "technology" else 2.5

        # EBIT margin target
        hist_peak = _historical_ebit_margin_anchor(ebit_margins, ebit_margin_base_pct)
        ebit_margin_target, ebit_margin_target_source = _derive_ebit_margin_target(
            ebit_margin_base_pct,
            ebit_margins,
            gross_margin_base_pct,
            industry,
            revenues=revenues,
        )

        # Working-capital days
        cogs = revenue_base - gross_profit
        dso  = round(accounts_recv / revenue_base * 365, 1) if accounts_recv > 0 and revenue_base > 0 else 30.0
        dio  = round(inventory     / cogs * 365, 1)         if inventory > 0 and cogs > 0 else 60.0
        dpo  = round(accounts_pay  / cogs * 365, 1)         if accounts_pay > 0 and cogs > 0 else 40.0

        knowledge_model_payload: dict[str, Any] | None = None
        scenario_width_multiplier = 1.0
        try:
            from webapp.data.knowledge_model import refine_live_assumptions

            knowledge_model_payload = refine_live_assumptions(
                ticker=ticker,
                company_name=company_name,
                sector=sector,
                industry=industry,
                market_cap=market_cap,
                revenues=revenues,
                ebit_margins=ebit_margins,
                gross_margin_base_pct=gross_margin_base_pct,
                revenue_growth_near=revenue_growth_near,
                terminal_growth=terminal_growth,
                ebit_margin_base_pct=ebit_margin_base_pct,
                ebit_margin_target=ebit_margin_target,
                beta=beta,
                wacc=wacc,
                rf_rate=rf_rate,
                erp=erp,
                kd_post=kd_post,
                e_wt=e_wt,
                d_wt=d_wt,
                total_assets=total_assets,
                total_debt=total_debt,
                revenue_base=revenue_base,
                operating_cf=operating_cf,
                fcf=fcfs[-1] if fcfs else 0.0,
                capex_pct=capex_pct,
                capexes=capexes,
                da_pct=da_pct,
                das=das,
                sbc_pct=sbc_pct,
                sbcs=sbcs,
                tax_rate_pct=tax_rate_pct,
                pretax_incomes=pretax_incomes,
                tax_provisions=tax_provs,
                dso=dso,
                dio=dio,
                dpo=dpo,
                # Layer F Tier 3 — pass raw NTM consensus and analyst count so
                # the knowledge model can weight the consensus estimate correctly.
                ntm_growth=_consensus_growth,
                analyst_count=_n_analysts_rev,
            )

            revenue_growth_near = float(knowledge_model_payload.get("revenue_growth_near", revenue_growth_near))
            terminal_growth = float(knowledge_model_payload.get("terminal_growth", terminal_growth))
            ebit_margin_target = float(knowledge_model_payload.get("ebit_margin_target", ebit_margin_target))
            beta = float(knowledge_model_payload.get("beta", beta))
            wacc = float(knowledge_model_payload.get("wacc", wacc))
            tax_rate_pct = float(knowledge_model_payload.get("tax_rate_pct", tax_rate_pct))
            da_pct = float(knowledge_model_payload.get("da_pct", da_pct))
            capex_pct = float(knowledge_model_payload.get("capex_pct", capex_pct))
            sbc_pct = float(knowledge_model_payload.get("sbc_pct", sbc_pct))
            dso = float(knowledge_model_payload.get("dso", dso))
            dio = float(knowledge_model_payload.get("dio", dio))
            dpo = float(knowledge_model_payload.get("dpo", dpo))
            scenario_width_multiplier = max(1.0, float(knowledge_model_payload.get("scenario_width_multiplier") or 1.0))

            beta_adj = 0.67 * beta + 0.33
            ke = rf_rate + beta_adj * erp
            kd_post = kd_pre * (1 - tax_rate_pct / 100)
            capex_steady_pct = min(capex_pct, da_pct + 3.0)
            if capex_pct > da_pct * 1.5:
                capex_steady_pct = max(capex_steady_pct, da_pct * 1.3)
        except Exception as exc:
            logger.warning("Knowledge model refinement failed for %s: %s", ticker, exc)
            knowledge_model_payload = None

        def _run_projection(near_growth: float, target_margin: float,
                            scenario_wacc: float, scenario_g: float) -> dict:
            scenario_forecast = []
            scenario_pv_ufcfs = 0.0
            prev_rev_local = revenue_base
            prev_ar_local = accounts_recv
            prev_inv_local = inventory
            prev_ap_local = accounts_pay

            for n_yr in range(1, FORECAST_YEARS + 1):
                alpha = (n_yr - 1) / max(FORECAST_YEARS - 1, 1)
                g_yr = near_growth * (1 - alpha) + scenario_g * alpha
                rev_n = prev_rev_local * (1 + g_yr / 100)
                margin_n = ebit_margin_base_pct + (target_margin - ebit_margin_base_pct) * n_yr / FORECAST_YEARS
                ebit_n = rev_n * margin_n / 100
                nopat_n = ebit_n * (1 - tax_rate_pct / 100)
                da_n = rev_n * da_pct / 100
                sbc_n = rev_n * sbc_pct / 100
                capex_yr_pct = capex_pct + (capex_steady_pct - capex_pct) * alpha
                capex_n = rev_n * capex_yr_pct / 100

                cogs_n = rev_n * (1 - gross_margin_base_pct / 100)
                ar_n = rev_n * dso / 365
                if cogs_n > 0:
                    inv_n = cogs_n * dio / 365
                    ap_n = cogs_n * dpo / 365
                else:
                    inv_n = 0.0
                    ap_n = max(rev_n - ebit_n, 0.0) * dpo / 365
                d_nwc = (ar_n - prev_ar_local) + (inv_n - prev_inv_local) - (ap_n - prev_ap_local)

                ufcf_n = nopat_n + da_n + sbc_n - capex_n - d_nwc
                ufcf_n *= attributable_earnings_ratio
                df_n = 1 / (1 + scenario_wacc / 100) ** (n_yr - 0.5)
                pv_n = ufcf_n * df_n
                scenario_pv_ufcfs += pv_n

                scenario_forecast.append({
                    "year": f"FY{fy_years_asc[-1] + n_yr}",
                    "n": n_yr,
                    "revenue": round(rev_n),
                    "ebit_m": round(margin_n, 1),
                    "ebit": round(ebit_n),
                    "nopat": round(nopat_n),
                    "da": round(da_n),
                    "sbc": round(sbc_n),
                    "capex": round(capex_n),
                    "d_nowc": round(d_nwc),
                    "ufcf": round(ufcf_n),
                    "df": round(df_n, 4),
                    "pv": round(pv_n),
                })
                prev_rev_local = rev_n
                prev_ar_local, prev_inv_local, prev_ap_local = ar_n, inv_n, ap_n

            scenario_pv_ufcfs = round(scenario_pv_ufcfs)
            scenario_terminal_ufcf = scenario_forecast[-1]["ufcf"] if scenario_forecast else 0
            scenario_spread = max(scenario_wacc / 100 - scenario_g / 100, 0.005)
            scenario_tv = scenario_terminal_ufcf * (1 + scenario_g / 100) / scenario_spread
            scenario_pv_tv = round(scenario_tv / (1 + scenario_wacc / 100) ** FORECAST_YEARS)
            return {
                "forecast": scenario_forecast,
                "pv_ufcfs": scenario_pv_ufcfs,
                "terminal_ufcf": scenario_terminal_ufcf,
                "pv_terminal": scenario_pv_tv,
                "enterprise_value": scenario_pv_ufcfs + scenario_pv_tv,
            }

        # ── 7-year DCF forecast ───────────────────────────────────────────
        FORECAST_YEARS = 7

        # Apply user overrides to DCF assumptions before running the projection.
        # This allows /api/recompute to produce a fully re-forecast IV rather than
        # just scaling the old UFCF stream by a discount-factor ratio.
        if overrides:
            _ov = overrides  # shorthand
            if "wacc" in _ov and _ov["wacc"] is not None:
                wacc = float(_ov["wacc"])
            if "g" in _ov and _ov["g"] is not None:
                terminal_growth = float(_ov["g"])
            if "revenue_growth_near" in _ov and _ov["revenue_growth_near"] is not None:
                revenue_growth_near = float(_ov["revenue_growth_near"])
            if "ebit_margin_target" in _ov and _ov["ebit_margin_target"] is not None:
                ebit_margin_target = float(_ov["ebit_margin_target"])
            if "da_pct" in _ov and _ov["da_pct"] is not None:
                da_pct = float(_ov["da_pct"])
            if "capex_pct" in _ov and _ov["capex_pct"] is not None:
                capex_pct = float(_ov["capex_pct"])
                capex_steady_pct = min(capex_pct, da_pct + 3.0)
                if capex_pct > da_pct * 1.5:
                    capex_steady_pct = max(capex_steady_pct, da_pct * 1.3)
            if "sbc_pct" in _ov and _ov["sbc_pct"] is not None:
                sbc_pct = float(_ov["sbc_pct"])
            if "tax_rate" in _ov and _ov["tax_rate"] is not None:
                tax_rate_pct = float(_ov["tax_rate"])
            if "beta" in _ov and _ov["beta"] is not None:
                beta = max(0.3, min(3.0, float(_ov["beta"])))
                beta_adj = 0.67 * beta + 0.33
                ke = rf_rate + beta_adj * erp
                kd_post = kd_pre * (1 - tax_rate_pct / 100)
                total_cap = market_cap + total_debt
                e_wt = market_cap / total_cap * 100 if total_cap > 0 else 85.0
                d_wt = 100 - e_wt
                wacc = round(max(5.0, min(20.0, (e_wt / 100) * ke + (d_wt / 100) * kd_post)), 1)

        knowledge_weights = dict((knowledge_model_payload or {}).get("assumption_weights") or {})

        def _assumption_meta(name: str, default_source: str, default_warn: str | None = None) -> tuple[str, str | None]:
            payload = dict(knowledge_weights.get(name) or {})
            return str(payload.get("source") or default_source), payload.get("warn", default_warn)

        base_projection = _run_projection(revenue_growth_near, ebit_margin_target, wacc, terminal_growth)
        forecast = base_projection["forecast"]
        pv_ufcfs = base_projection["pv_ufcfs"]
        terminal_ufcf = base_projection["terminal_ufcf"]
        pv_tv = base_projection["pv_terminal"]

        ev           = pv_ufcfs + pv_tv
        equity_value = max(0, ev - net_debt)
        iv           = equity_value / diluted_shares if diluted_shares > 0 else 0
        upside       = (iv - price) / price * 100 if price > 0 else 0
        base_dcf_ev = ev
        base_dcf_equity_value = equity_value
        base_dcf_iv = iv
        base_dcf_upside = upside
        market_residual_overlay = dict((knowledge_model_payload or {}).get("market_residual_overlay") or {})
        market_residual_applied = False
        market_residual_adjustment_pct = 0.0
        if market_residual_overlay.get("enabled"):
            adjustment_decimal = float(market_residual_overlay.get("applied_adjustment_decimal") or 0.0)
            if abs(adjustment_decimal) >= 0.001:
                adjusted_ev = max(0.0, ev * (1.0 + adjustment_decimal))
                ev = round(adjusted_ev)
                equity_value = max(0, ev - net_debt)
                iv = equity_value / diluted_shares if diluted_shares > 0 else 0
                upside = (iv - price) / price * 100 if price > 0 else 0
                market_residual_applied = True
                market_residual_adjustment_pct = round(adjustment_decimal * 100.0, 1)
                market_residual_overlay["applied_to_dashboard_value"] = True
                market_residual_overlay["base_dcf_enterprise_value_m"] = round(base_dcf_ev)
                market_residual_overlay["hybrid_enterprise_value_m"] = round(ev)
                market_residual_overlay["base_dcf_intrinsic_value"] = round(base_dcf_iv, 2)
                market_residual_overlay["hybrid_intrinsic_value"] = round(iv, 2)
                if knowledge_model_payload is not None:
                    knowledge_model_payload["market_residual_overlay"] = market_residual_overlay
                    layered_payload = dict(knowledge_model_payload.get("layered_learning") or {})
                    layered_payload["market_residual_overlay"] = market_residual_overlay
                    knowledge_model_payload["layered_learning"] = layered_payload
        tv_pct       = round(pv_tv / ev * 100, 1) if ev > 0 else 0

        expected_error_band_for_rec = dict((knowledge_model_payload or {}).get("expected_valuation_error_band") or {})
        expected_error_for_rec = float(
            expected_error_band_for_rec.get("p50")
            or (knowledge_model_payload or {}).get("expected_valuation_error_pct")
            or 0.0
        )
        rec, rec_class, buy_threshold, sell_threshold = _learned_recommendation_from_upside(
            upside,
            expected_error_pct=expected_error_for_rec,
        )
        recommendation_basis = {
            "method": "learned-error-adjusted-base-case",
            "base_upside_pct": round(upside, 1),
            "expected_error_pct": round(expected_error_for_rec, 1),
            "buy_threshold_pct": round(buy_threshold, 1),
            "sell_threshold_pct": round(sell_threshold, 1),
        }

        # ── 52-week range ─────────────────────────────────────────────────
        year_high = _sf(tech.get("52WeekHigh")) or _sf(hi.get("52WeekHigh")) or price * 1.2
        year_low  = _sf(tech.get("52WeekLow"))  or _sf(hi.get("52WeekLow"))  or price * 0.8

        # ── Analyst targets ───────────────────────────────────────────────
        analyst_median = _sf(anal.get("TargetPrice")) or _sf(hi.get("WallStreetTargetPrice"))
        n_analysts = int(_sf(anal.get("StrongBuy", 0)) + _sf(anal.get("Buy", 0)) +
                        _sf(anal.get("Hold", 0))       + _sf(anal.get("Sell", 0)) +
                        _sf(anal.get("StrongSell", 0)))
        analyst_low  = analyst_median * 0.85
        analyst_high = analyst_median * 1.15

        fwd_eps  = _sf(hi.get("EPSEstimateNextYear") or hi.get("EarningsShare"))
        div_yield= _sf(hi.get("DividendYield"))
        dividend_yield = round(div_yield * 100 if div_yield < 0.5 else div_yield, 2)
        buyback_yield  = round(buyback_raw / market_cap * 100, 1) if market_cap > 0 else 0.0

        # ── Sensitivity table ─────────────────────────────────────────────
        wacc_pcts = [round(wacc - 1.0, 1), round(wacc - 0.5, 1), round(wacc, 1),
                     round(wacc + 0.5, 1), round(wacc + 1.0, 1)]
        g_pcts    = [round(terminal_growth - 1.0, 1), round(terminal_growth - 0.5, 1),
                     round(terminal_growth, 1), round(terminal_growth + 0.5, 1),
                     round(terminal_growth + 1.0, 1)]
        sens = _sensitivity(
            terminal_ufcf=terminal_ufcf, pv_ufcfs=pv_ufcfs,
            net_debt=net_debt, diluted_shares=diluted_shares,
            wacc_pcts=wacc_pcts, g_pcts=g_pcts,
            base_wacc=wacc, base_g=terminal_growth,
        )

        # ── Scenarios ─────────────────────────────────────────────────────
        def _quick_iv(w_p: float, g_p: float, near_growth: float,
                      target_margin: float) -> tuple:
            scenario_projection = _run_projection(near_growth, target_margin, w_p, g_p)
            ev_ = scenario_projection["enterprise_value"]
            eq_  = max(0, ev_ - net_debt)
            iv_  = round(eq_ / diluted_shares, 2) if diluted_shares > 0 else 0
            up_  = round((iv_ - price) / price * 100, 1) if price > 0 else 0
            return iv_, up_, round(ev_)

        learning_explainability = dict((knowledge_model_payload or {}).get("explainability") or {})
        forecast_layers_payload = list(learning_explainability.get("forecast_layers") or [])

        def _forecast_layer(prefix: str) -> dict[str, Any]:
            prefix_lower = prefix.lower()
            for layer in forecast_layers_payload:
                if str(layer.get("driver") or "").lower().startswith(prefix_lower):
                    return dict(layer)
            return {}

        def _bounded(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        revenue_layer = _forecast_layer("Revenue")
        margin_layer = _forecast_layer("EBIT")
        wacc_layer = _forecast_layer("WACC")
        revenue_final = _maybe_float(revenue_layer.get("final_value"))
        revenue_learned = _maybe_float(revenue_layer.get("learned_adjustment"))
        growth_bias_pp = _bounded(
            (revenue_learned - revenue_final) if revenue_learned is not None and revenue_final is not None else 0.0,
            -5.0,
            5.0,
        )
        margin_final = _maybe_float(margin_layer.get("final_value"))
        margin_company = _maybe_float(margin_layer.get("company_anchor"))
        margin_bias_pp = _bounded(
            (margin_final - margin_company) if margin_final is not None and margin_company is not None else 0.0,
            -4.0,
            4.0,
        )
        wacc_final = _maybe_float(wacc_layer.get("final_value"))
        wacc_company = _maybe_float(wacc_layer.get("company_anchor"))
        wacc_bias_pp = _bounded(
            (wacc_final - wacc_company) if wacc_final is not None and wacc_company is not None else 0.0,
            -3.0,
            3.0,
        )
        caution_flags = sum(1 for layer in forecast_layers_payload if layer.get("warn"))
        learned_caution = _bounded(
            caution_flags * 0.25 + max(0.0, -growth_bias_pp) * 0.15 + max(0.0, wacc_bias_pp) * 0.30,
            0.0,
            1.5,
        )
        learned_support = _bounded(
            max(0.0, growth_bias_pp) * 0.15 + max(0.0, margin_bias_pp) * 0.12 + max(0.0, -wacc_bias_pp) * 0.20,
            0.0,
            1.0,
        )
        bull_wacc_reduction = _bounded(1.0 * scenario_width_multiplier + learned_support * 0.35 - learned_caution * 0.40, 0.4, 1.5)
        bear_wacc_add = _bounded(1.5 * scenario_width_multiplier + learned_caution * 0.40 + max(0.0, wacc_bias_pp) * 0.50, 0.8, 5.0)

        # Cap WACC reduction at 1.5pp in bull case to prevent terminal-value explosion
        bull_wacc = round(max(wacc - 1.5, max(4.0, wacc - bull_wacc_reduction)), 1)
        bull_g = round(min(5.0, terminal_growth + min(1.2, 0.5 * scenario_width_multiplier + learned_support * 0.2)), 1)
        # Enforce minimum WACC-g spread of 2.0pp; without this, a small spread causes TV to go infinite
        bull_g = min(bull_g, round(bull_wacc - 2.0, 1))
        bear_wacc = round(min(25.0, wacc + bear_wacc_add), 1)
        bear_g = round(max(-1.0, terminal_growth - min(1.8, 1.0 * scenario_width_multiplier + learned_caution * 0.2)), 1)
        # Scenarios diverge around the analyst consensus anchor (not the blended
        # base-case growth) so bull and bear stay grounded in forward expectations.
        _cons_g_scenario = _extract_consensus_growth(earn, revenue_base)
        _scenario_anchor = _cons_g_scenario if _cons_g_scenario is not None else revenue_growth_near
        bull_growth_step = _bounded(
            2.0 * scenario_width_multiplier
            + max(0.0, growth_bias_pp) * 0.50
            - max(0.0, -growth_bias_pp) * 0.35
            - learned_caution * 0.20,
            0.5,
            10.0,
        )
        bear_growth_step = _bounded(
            3.0 * scenario_width_multiplier
            + max(0.0, -growth_bias_pp) * 0.80
            + max(0.0, wacc_bias_pp) * 0.50
            + learned_caution * 0.40,
            1.0,
            14.0,
        )
        bull_margin_lift = _bounded(
            2.0 * scenario_width_multiplier
            + max(0.0, margin_bias_pp) * 0.60
            - max(0.0, -margin_bias_pp) * 0.25,
            0.5,
            8.0,
        )
        bear_margin_drop = _bounded(
            max(1.0, scenario_width_multiplier)
            + max(0.0, -margin_bias_pp) * 0.70
            + learned_caution * 0.30,
            0.8,
            8.0,
        )
        bull_growth = round(min(60.0, _scenario_anchor + bull_growth_step), 1)
        bear_growth = round(max(-15.0, _scenario_anchor - bear_growth_step), 1)
        bull_margin = round(max(ebit_margin_target, min(80.0, ebit_margin_target + bull_margin_lift)), 1)
        bear_margin = round(max(-10.0, ebit_margin_base_pct - bear_margin_drop), 1)
        bull_iv, bull_up, bull_ev = _quick_iv(bull_wacc, bull_g, bull_growth, bull_margin)
        bear_iv, bear_up, bear_ev = _quick_iv(bear_wacc, bear_g, bear_growth, bear_margin)
        bull_rec = _learned_recommendation_from_upside(bull_up, expected_error_pct=expected_error_for_rec)[0]
        bear_rec = _learned_recommendation_from_upside(bear_up, expected_error_pct=expected_error_for_rec)[0]
        bull_probability = _bounded(0.25 + learned_support * 0.06 - learned_caution * 0.04, 0.12, 0.38)
        bear_probability = _bounded(0.25 + learned_caution * 0.06 - learned_support * 0.04, 0.12, 0.38)
        base_probability = max(0.30, 1.0 - bull_probability - bear_probability)
        probability_total = base_probability + bull_probability + bear_probability
        base_probability /= probability_total
        bull_probability /= probability_total
        bear_probability /= probability_total
        learned_expected_upside = round(
            upside * base_probability + bull_up * bull_probability + bear_up * bear_probability,
            1,
        )
        learned_rec, learned_rec_class, learned_buy_threshold, learned_sell_threshold = _learned_recommendation_from_upside(
            learned_expected_upside,
            expected_error_pct=expected_error_for_rec,
        )
        rec = learned_rec
        rec_class = learned_rec_class
        recommendation_basis.update(
            {
                "method": "learned-scenario-weighted-expected-upside",
                "expected_upside_pct": learned_expected_upside,
                "buy_threshold_pct": round(learned_buy_threshold, 1),
                "sell_threshold_pct": round(learned_sell_threshold, 1),
                "scenario_probabilities": {
                    "base": round(base_probability * 100),
                    "bull": round(bull_probability * 100),
                    "bear": round(bear_probability * 100),
                },
            }
        )
        learning_scenario_basis = {
            "source": "forecast_layers",
            "width_multiplier": round(scenario_width_multiplier, 2),
            "growth_bias_pp": round(growth_bias_pp, 1),
            "margin_bias_pp": round(margin_bias_pp, 1),
            "wacc_bias_pp": round(wacc_bias_pp, 1),
            "caution_flags": int(caution_flags),
            "learned_caution": round(learned_caution, 2),
            "learned_support": round(learned_support, 2),
        }

        # ── Financial scores ──────────────────────────────────────────────
        financial_scores   = None
        dupont             = None
        earnings_quality   = None
        try:
            from webapp.data.financial_scores import (
                compute_altman_z, compute_piotroski_f,
                compute_dupont, compute_earnings_quality,
            )
            # Prev-year data (index -2 of ascending lists)
            ni_prev     = net_incomes[-2]    if len(net_incomes)   >= 2 else net_income_base
            ta_prev     = total_assets_h[-2] if len(total_assets_h)>= 2 else total_assets
            ltd_prev    = total_debts_h[-2]  if len(total_debts_h) >= 2 else total_debt
            ca_prev     = ca_h_arr[-2] if len(ca_h_arr) >= 2 else cur_assets
            cl_prev     = cl_h_arr[-2] if len(cl_h_arr) >= 2 else cur_liab
            sh_prev     = hist_shares_h[-2] if len(hist_shares_h) >= 2 else diluted_shares
            gp_prev     = gross_profits[-2]  if len(gross_profits) >= 2 else gross_profit
            rev_prev    = revenues[-2]       if len(revenues)      >= 2 else revenue_base

            financial_scores = {
                "altman_z": compute_altman_z(
                    working_capital   = round(working_cap),
                    total_assets      = round(total_assets),
                    retained_earnings = round(retained_earn),
                    ebit              = round(ebit_base),
                    market_cap        = round(market_cap),
                    total_liabilities = round(total_liab),
                    revenue           = round(revenue_base),
                ),
                "piotroski_f": compute_piotroski_f(
                    net_income               = round(net_income_base),
                    total_assets             = round(total_assets),
                    operating_cash_flow      = round(operating_cf),
                    long_term_debt           = round(long_term_debt),
                    current_assets           = round(cur_assets),
                    current_liabilities      = round(cur_liab),
                    shares_outstanding       = round(diluted_shares),
                    gross_profit             = round(gross_profit),
                    revenue                  = round(revenue_base),
                    net_income_prev          = round(ni_prev),
                    total_assets_prev        = round(ta_prev),
                    long_term_debt_prev      = round(ltd_prev),
                    current_assets_prev      = round(ca_prev),
                    current_liabilities_prev = round(cl_prev),
                    shares_prev              = round(sh_prev),
                    gross_profit_prev        = round(gp_prev),
                    revenue_prev             = round(rev_prev),
                ),
            }

            dupont = compute_dupont(
                years        = fy_years_asc,
                net_income   = net_incomes,
                revenue      = revenues,
                total_assets = total_assets_h,
                equity       = total_equity_h,
            )
            earnings_quality = compute_earnings_quality(
                years        = fy_years_asc,
                net_income   = net_incomes,
                operating_cf = op_cfs,
                fcf          = fcfs,
            )
        except Exception as exc:
            logger.warning("financial_scores failed for %s: %s", ticker, exc)

        # ── Assumptions table ─────────────────────────────────────────────
        revenue_source, revenue_warn = _assumption_meta("revenue_growth_near", f"{n}-yr CAGR (EODHD)")
        terminal_source, terminal_warn = _assumption_meta(
            "terminal_growth",
            f"{'Tech sector long-run growth' if sector.lower() == 'technology' else 'Long-run nominal GDP proxy'}",
        )
        margin_source, margin_warn = _assumption_meta("ebit_margin_target", ebit_margin_target_source)
        beta_source, beta_warn = _assumption_meta("beta", "EODHD Technicals")
        wacc_source, wacc_warn = _assumption_meta("wacc", f"CAPM: Rf {round(rf_rate,1)}% + β {round(beta,2)} × ERP {erp}%")
        tax_source, tax_warn = _assumption_meta("tax_rate_pct", "LTM effective tax rate")
        da_source, da_warn = _assumption_meta("da_pct", "LTM D&A / Revenue")
        capex_source, capex_warn = _assumption_meta("capex_pct", "LTM CapEx / Revenue")
        sbc_source, sbc_warn = _assumption_meta("sbc_pct", "LTM SBC / Revenue")
        dso_source, dso_warn = _assumption_meta("dso", "LTM AR / (Rev/365)")
        dio_source, dio_warn = _assumption_meta("dio", "LTM Inventory / (COGS/365)")
        dpo_source, dpo_warn = _assumption_meta("dpo", "LTM AP / (COGS/365)")

        assumptions = [
            {"driver": "Revenue Growth (Near-Term)", "auto": revenue_growth_near, "active": revenue_growth_near, "unit": "%",    "mode": "AUTO", "source": revenue_source, "warn": revenue_warn},
            {"driver": "Revenue Growth (Terminal)",  "auto": terminal_growth,     "active": terminal_growth,     "unit": "%",    "mode": "AUTO", "source": terminal_source, "warn": terminal_warn},
            {"driver": "EBIT Margin (Base)",         "auto": round(ebit_margin_base_pct, 1),  "active": round(ebit_margin_base_pct, 1),  "unit": "%", "mode": "AUTO", "source": "LTM EBIT / Revenue", "warn": None},
            {"driver": "EBIT Margin (Target Y7)",    "auto": round(ebit_margin_target, 1),    "active": round(ebit_margin_target, 1),    "unit": "%", "mode": "AUTO", "source": margin_source, "warn": margin_warn},
            {"driver": "WACC",                       "auto": wacc,  "active": wacc,  "unit": "%", "mode": "AUTO", "source": wacc_source, "warn": wacc_warn},
            {"driver": "Cost of Debt (Pre-Tax)",     "auto": round(kd_pre, 1), "active": round(kd_pre, 1), "unit": "%", "mode": "AUTO", "source": "Interest expense / total debt", "warn": None},
            {"driver": "Beta (Levered)",             "auto": round(beta, 2),   "active": round(beta, 2),   "unit": "×",  "mode": "AUTO", "source": beta_source, "warn": beta_warn},
            {"driver": "Tax Rate",                   "auto": round(tax_rate_pct, 1), "active": round(tax_rate_pct, 1), "unit": "%", "mode": "AUTO", "source": tax_source, "warn": tax_warn},
            {"driver": "D&A % Revenue",              "auto": round(da_pct, 1),    "active": round(da_pct, 1),    "unit": "%", "mode": "AUTO", "source": da_source, "warn": da_warn},
            {"driver": "CapEx % Revenue",            "auto": round(capex_pct, 1), "active": round(capex_pct, 1), "unit": "%", "mode": "AUTO", "source": capex_source, "warn": capex_warn},
            {"driver": "SBC % Revenue",              "auto": round(sbc_pct, 1),   "active": round(sbc_pct, 1),   "unit": "%", "mode": "AUTO", "source": sbc_source, "warn": sbc_warn},
            {"driver": "DSO (Days Sales Outstanding)", "auto": round(dso, 1), "active": round(dso, 1), "unit": "days", "mode": "AUTO", "source": dso_source, "warn": dso_warn},
            {"driver": "DIO (Days Inventory Outst.)", "auto": round(dio, 1),  "active": round(dio, 1),  "unit": "days", "mode": "AUTO", "source": dio_source, "warn": dio_warn},
            {"driver": "DPO (Days Payable Outst.)",  "auto": round(dpo, 1),   "active": round(dpo, 1),   "unit": "days", "mode": "AUTO", "source": dpo_source, "warn": dpo_warn},
            {"driver": "Buyback Yield",              "auto": buyback_yield,   "active": buyback_yield,   "unit": "%", "mode": "AUTO", "source": "LTM buybacks / market cap", "warn": None},
            {"driver": "Dividend Yield",             "auto": dividend_yield,  "active": dividend_yield,  "unit": "%", "mode": "AUTO", "source": "EODHD Highlights.DividendYield", "warn": None},
        ]

        # ── Validation flags ──────────────────────────────────────────────
        flags = [
            {"name": "Data Freshness",  "status": "pass", "message": f"EODHD: {n} years of annual financials ({fy_years_asc[0]}–{fy_years_asc[-1]})."},
            {"name": "Revenue Sanity",  "status": "pass" if revenue_base > 10 else "warn", "message": f"Latest annual revenue: ${revenue_base:,.0f}M."},
            {"name": "WACC Range",      "status": "pass", "message": f"WACC {wacc}% (β={beta:.2f}, Rf={rf_rate:.1f}%, ERP={erp}%)."},
            {"name": "WACC–g Spread",   "status": "pass" if wacc - terminal_growth >= 0.5 else "fail",
             "message": f"Spread {wacc - terminal_growth:.1f}pp {'above' if wacc - terminal_growth >= 0.5 else 'below'} 50bp minimum."},
            {"name": "TV % of EV",      "status": "warn" if tv_pct > 70 else "pass", "message": f"Terminal value = {tv_pct}% of EV."},
            {"name": "Net Debt Sign",   "status": "pass" if net_debt < revenue_base * 3 else "warn",
             "message": f"Net debt ${net_debt:,.0f}M vs revenue ${revenue_base:,.0f}M."},
        ]
        if attributable_earnings_ratio < 1.0 and attributable_earnings_note:
            flags.append(
                {
                    "name": "Attributable Earnings",
                    "status": "warn",
                    "message": attributable_earnings_note,
                }
            )

        analyst_consensus = {
            "revenue_y1_consensus": round(revenue_base * (1 + revenue_growth_near / 100)),  # model-implied, not third-party
            "revenue_y1_model":     forecast[0]["revenue"] if forecast else 0,
            "eps_y1_consensus":     round(fwd_eps, 2),
            "buy_count":            int(_sf(anal.get("StrongBuy", 0)) + _sf(anal.get("Buy", 0))),
            "hold_count":           int(_sf(anal.get("Hold", 0))),
            "sell_count":           int(_sf(anal.get("Sell", 0))   + _sf(anal.get("StrongSell", 0))),
            "total_analysts":       n_analysts,
            "mean_target":          round(analyst_median, 2),
        }

        # Plan P4/P5/S4/M2 — extract richer signals from the EODHD fund file.
        analyst_ratings_block = _extract_analyst_ratings_payload(anal, price)
        earnings_surprise_block = _extract_earnings_surprise(earn)
        eps_revision_block = _extract_eps_revision_signal(earn)
        insider_block = _extract_insider_signal(fund.get("InsiderTransactions") or {})

        # ── Build result dict ─────────────────────────────────────────────
        _result = {
            # Identity
            "ticker":             ticker.upper(),
            "company_name":       company_name,
            "exchange":           exchange,
            "currency":           currency,
            "quote_currency":     quote_currency,
            "reporting_currency": reporting_currency,
            "sector":        sector,
            "industry":      industry,
            "description":   description,

            # Market data
            "price":              price,
            "price_date":         str(datetime.now(timezone.utc).date()),
            "market_cap":         market_cap,
            "market_cap_m":       market_cap,
            "fifty_two_week_low": round(year_low,  2),
            "fifty_two_week_high":round(year_high, 2),
            "week52_low":         round(year_low,  2),
            "week52_high":        round(year_high, 2),
            "analyst_low":        round(analyst_low,    2),
            "analyst_high":       round(analyst_high,   2),
            "analyst_median":     round(analyst_median, 2),

            # Valuation output
            "intrinsic_value":     round(iv, 2),
            "upside_pct":          round(upside, 1),
            "recommendation":      rec,
            "recommendation_class":rec_class,
            "recommendation_basis": recommendation_basis,
            "learned_expected_upside_pct": learned_expected_upside,
            "learned_recommendation": rec,
            "confidence_score":    70,
            "data_freshness":      f"Live (EODHD — {n} years)",

            # DCF bridge
            "enterprise_value":    round(ev),
            "enterprise_value_m":  round(ev),
            "equity_value":        round(equity_value),
            "base_dcf_enterprise_value": round(base_dcf_ev),
            "base_dcf_enterprise_value_m": round(base_dcf_ev),
            "base_dcf_equity_value": round(base_dcf_equity_value),
            "base_dcf_intrinsic_value": round(base_dcf_iv, 2),
            "base_dcf_upside_pct": round(base_dcf_upside, 1),
            "hybrid_value_applied": market_residual_applied,
            "market_residual_adjustment_pct": market_residual_adjustment_pct,
            "market_residual_overlay": market_residual_overlay,
            "pv_ufcfs":            pv_ufcfs,
            "pv_terminal":         pv_tv,
            "tv_pct":              tv_pct,
            "diluted_shares":      round(diluted_shares, 1),
            "terminal_ufcf":       terminal_ufcf,
            "attributable_earnings_ratio": attributable_earnings_ratio,
            "attributable_earnings_adjustment_applied": attributable_earnings_ratio < 1.0,

            # WACC
            "wacc":              wacc,
            "cost_of_equity":    round(ke, 1),
            "cost_of_debt_pre":  round(kd_pre, 1),
            "cost_of_debt_post": round(kd_post, 1),
            "terminal_growth":   terminal_growth,
            "tax_rate":          round(tax_rate_pct, 1),
            "beta":              round(beta, 2),
            "risk_free_rate":    round(rf_rate, 1),
            "erp":               erp,
            "size_premium":      0.0,
            "equity_weight":     round(e_wt, 1),
            "debt_weight":       round(d_wt, 1),
            "equity_weight_pct": round(e_wt, 1),
            "debt_weight_pct":   round(d_wt, 1),

            # Capital structure
            "total_debt":  round(total_debt),
            "cash_equiv":  round(cash),
            "net_debt":    round(net_debt),
            "minority_interest_m": 0.0,
            "preferred_equity_m":  0.0,

            # Operating assumptions
            "revenue_growth_near":  revenue_growth_near,
            "revenue_growth_term":  terminal_growth,
            "revenue_growth_far":   terminal_growth,
            "ebit_margin_base":     round(ebit_margin_base_pct, 1),
            "ebit_margin_target":   round(ebit_margin_target, 1),
            "da_pct":               round(da_pct, 1),
            "capex_pct":            round(capex_pct, 1),
            "sbc_pct":              round(sbc_pct, 1),
            "dso":                  round(dso, 1),
            "dio":                  round(dio, 1),
            "dpo":                  round(dpo, 1),
            "buyback_yield":        buyback_yield,
            "dividend_yield":       dividend_yield,

            # Extra fields for Excel/Comps
            "revenue_base":         round(revenue_base),
            "ebitda_ltm":           round(_sf(l_is.get("ebitda")) / M or ebit_base + da),

            # Historical
            "historical": {
                "years":        fy_years_asc,
                "revenue":      revenues,
                "gross_profit": gross_profits,
                "gross_margin": gross_margins,
                "ebit":         ebits,
                "ebit_margin":  ebit_margins,
                "net_income":   net_incomes,
                "fcf":          fcfs,
                "op_cf":        op_cfs,
                "capex":        capexes,
                "da":           das,
                "sbc":          sbcs,
                "total_assets": total_assets_h,
                "equity":       total_equity_h,
                "debt":         total_debts_h,
                "total_debt":   total_debts_h,
                "cash":         cash_h,
                "roic":         roics,
                "shares":       hist_shares_h,
                "tax":          tax_provs,
                "pretax_income":pretax_incomes,
                # Extended arrays for Excel export (EODHD actual BS/IS/CF per year)
                "interest_expense":          int_exp_h_arr,
                "interest_income":           int_inc_h_arr,
                "accounts_receivable":       acct_recv_h_arr,
                "inventory_bs":              inv_h_arr,
                "accounts_payable":          acct_pay_h_arr,
                "total_current_assets":      ca_h_arr,
                "total_current_liabilities": cl_h_arr,
                "net_ppe":                   net_ppe_h_arr,
                "goodwill":                  goodwill_h_arr,
                "intangibles":               intang_h_arr,
                "gross_ppe":                 gross_ppe_h_arr,
                "accum_dep":                 accum_dep_h_arr,
                "retained_earnings":         ret_earn_h_arr,
                "dividends_paid":            div_paid_h_arr,
                "buybacks":                  buyback_h_arr,
                "stock_issued":              stock_iss_h_arr,
                "net_borrowings":            net_borr_h_arr,
                "dates":                     _fy_dates,
            },

            # DCF schedule
            "forecast":    forecast,
            "sensitivity": sens,

            # Comps — populated after
            "peers":       [],
            "peer_median": {},

            # Flags
            "flags": flags,

            # Assumptions table
            "assumptions": assumptions,

            # Insights
            "insights": [
                {
                    "icon": "📊", "category": "Revenue Growth", "status": "neutral",
                    "headline": f"Revenue growth {revenue_growth_near:.1f}% ({n}-yr CAGR, EODHD)",
                    "body": (f"Latest annual revenue: ${revenue_base:,.0f}M. "
                             f"EODHD provides {n} years of audited annual data ({fy_years_asc[0]}–{fy_years_asc[-1]}). "
                             f"Near-term growth {revenue_growth_near:.1f}%, tapering to {terminal_growth}%."),
                },
                {
                    "icon": "📈", "category": "Margin Trajectory", "status": "neutral",
                    "headline": f"EBIT margin {ebit_margin_base_pct:.1f}% → target {ebit_margin_target:.1f}%",
                    "body": (f"Gross margin: {gross_margin_base_pct:.1f}%. "
                             f"Historical EBIT margin reference: {hist_peak:.1f}%. "
                             f"Model forecasts {ebit_margin_target - ebit_margin_base_pct:.1f}pp improvement over 7 years."),
                },
                {
                    "icon": "🏛️", "category": "WACC", "status": "neutral",
                    "headline": f"WACC {wacc}% (β={beta:.2f}, Rf={rf_rate:.1f}%, ERP={erp}%)",
                    "body": f"Ke={ke:.1f}%, Kd(pre-tax)={kd_pre:.1f}%, equity weight={e_wt:.1f}%. Spread vs terminal growth: {wacc - terminal_growth:.1f}pp.",
                },
                {
                    "icon": "⚡", "category": "Terminal Value",
                    "status": "warn" if tv_pct > 70 else "neutral",
                    "headline": f"{tv_pct}% of EV in terminal value",
                    "body": (f"TV/EV = {tv_pct}%. "
                             f"{'Elevated — model is sensitive to terminal assumptions.' if tv_pct > 70 else 'Within a normal range.'}"),
                },
            ],

            # Scenarios
            "scenarios": {
                "base": {
                    "label": "Base Case", "wacc": wacc, "g": terminal_growth,
                    "margin_target": round(ebit_margin_target, 1), "rev_growth": revenue_growth_near,
                    "iv": round(iv, 2), "upside": round(upside, 1), "ev": round(ev),
                    "recommendation": rec,
                    "revenue_cagr": revenue_growth_near,
                    "ebit_margin": round(ebit_margin_target, 1),
                    "terminal_growth": terminal_growth,
                    "intrinsic_value": round(iv, 2),
                    "upside_pct": round(upside, 1),
                    "recommendation": rec,
                    "probability": round(base_probability, 3),
                    "learning_basis": learning_scenario_basis,
                    "narrative": "Learned expected case after company memory, cohort calibration, global memory, and confidence penalties are applied.",
                },
                "bull": {
                    "label": "Bull Case", "wacc": bull_wacc, "g": bull_g,
                    "margin_target": bull_margin,
                    "rev_growth": bull_growth,
                    "iv": bull_iv, "upside": bull_up, "ev": bull_ev,
                    "recommendation": bull_rec,
                    "revenue_cagr": bull_growth,
                    "ebit_margin": bull_margin,
                    "wacc": bull_wacc,
                    "terminal_growth": bull_g,
                    "intrinsic_value": bull_iv,
                    "upside_pct": bull_up,
                    "probability": round(bull_probability, 3),
                    "learning_basis": learning_scenario_basis,
                    "narrative": "Upside case gives learned support more room while respecting the current confidence and scenario-width penalty.",
                },
                "bear": {
                    "label": "Bear Case", "wacc": bear_wacc, "g": bear_g,
                    "margin_target": bear_margin,
                    "rev_growth": bear_growth,
                    "iv": bear_iv, "upside": bear_up, "ev": bear_ev,
                    "recommendation": bear_rec,
                    "revenue_cagr": bear_growth,
                    "ebit_margin": bear_margin,
                    "wacc": bear_wacc,
                    "terminal_growth": bear_g,
                    "intrinsic_value": bear_iv,
                    "upside_pct": bear_up,
                    "probability": round(bear_probability, 3),
                    "learning_basis": learning_scenario_basis,
                    "narrative": "Downside case reflects learned caution, warning flags, layer disagreement, and higher discount-rate risk.",
                },
            },

            # Analyst view
            "analyst_view": {
                "valuation_says": (
                    f"Live DCF from EODHD ({n} years of history). "
                    f"IV=${iv:.2f} vs current price=${price:.2f} "
                    f"({'+' if upside >= 0 else ''}{upside:.1f}% upside). "
                    f"Analyst consensus target: ${analyst_median:.2f}."
                ),
                "key_assumptions": (
                    f"WACC {wacc}%, terminal growth {terminal_growth}%, "
                    f"EBIT margin {ebit_margin_base_pct:.1f}%→{ebit_margin_target:.1f}%, "
                    f"revenue growth {revenue_growth_near:.1f}% near-term."
                ),
                "model_risks": (
                    f"Model uses {n} years of EODHD data ({fy_years_asc[0]}–{fy_years_asc[-1]}). "
                    "Assumptions are auto-derived from historical averages."
                ),
                "verify_before_use": [
                    "Review latest earnings report and forward guidance",
                    "Check analyst consensus estimates vs model",
                    f"Verify beta ({beta:.2f}) for current conditions",
                    "Confirm no major acquisitions distort historical averages",
                ],
            },

            # Enriched fields
            "knowledge_model":    knowledge_model_payload,
            "financial_scores":  financial_scores,
            "dupont":            dupont,
            "earnings_quality":  earnings_quality,
            "analyst_consensus": analyst_consensus,
            **analyst_ratings_block,
            **earnings_surprise_block,
            **eps_revision_block,
            **insider_block,

            "is_demo": False,
            "data_source": "eodhd",
            "fiscal_year_end_month": fy_end_month_str,
            "is_live": True,

            # Near-term quarterly forecast (analyst consensus Q+1, Q+2)
            "near_term_forecast": _extract_near_term_quarters(earn),

            # Data quality
            "data_quality": {
                "annual_years":        n,
                "quarterly_periods":   0,
                "has_quarterly_recon": False,
                "reconstructed_years": 0,
                "source":              f"EODHD ({n} annual years, {fy_years_asc[0]}–{fy_years_asc[-1]})",
            },
        }

        # ── Live peer / comps ─────────────────────────────────────────────
        _peer_tickers: list[str] = []
        _peers: list[dict[str, Any]] = []
        _peer_median: dict[str, Any] = {}
        if _PEERS_AVAILABLE:
            try:
                _peer_tickers = get_peers_for_ticker(ticker, sector, industry)
                _peers, _peer_median = fetch_peer_metrics(
                    _peer_tickers,
                    ticker,
                    target_sector=sector,
                    target_industry=industry,
                )
                _result["peers"]       = _peers
                _result["peer_median"] = _peer_median
            except Exception as _pe:
                logger.debug("Peer fetch failed for %s: %s", ticker, _pe)

        universe_store = _safe_symbol_universe_store()
        discovery_store = _safe_discovery_store()
        peer_candidates = [
            {
                "ticker": peer.get("ticker") or peer.get("symbol"),
                "company_name": peer.get("name") or peer.get("company_name") or "",
                "exchange": str(peer.get("exchange") or ""),
                "sector": str(peer.get("sector") or sector),
                "industry": str(peer.get("industry") or industry),
                "canonical_industry": str(peer.get("canonical_industry") or ""),
                "industry_family": str(peer.get("industry_family") or ""),
                "peer_learning_score": float(peer.get("base_peer_learning_score") or peer.get("peer_learning_score") or 0.0),
                "base_peer_learning_score": float(peer.get("base_peer_learning_score") or peer.get("peer_learning_score") or 0.0),
                "industry_similarity": float(peer.get("industry_similarity") or 0.0),
                "pair_strength_score": float(peer.get("pair_strength_score") or 0.0),
            }
            for peer in _peers
            if str(peer.get("ticker") or peer.get("symbol") or "").strip()
        ]
        if mutate_learning:
            _register_global_universe_symbols(
                universe_store,
                ticker=ticker,
                company_name=company_name,
                exchange=exchange,
                country=str(gen.get("CountryName") or gen.get("CountryISO") or ""),
                sector=sector,
                industry=industry,
                knowledge_model=knowledge_model_payload,
                peer_items=peer_candidates,
            )
            peer_relationships = _record_peer_learning_signals(
                discovery_store,
                ticker=ticker,
                company_name=company_name,
                exchange=exchange,
                country=str(gen.get("CountryName") or gen.get("CountryISO") or ""),
                sector=sector,
                industry=industry,
                peer_items=peer_candidates,
            )
        else:
            peer_relationships = []
        _merge_peer_learning_relationships(_peers, peer_relationships)
        learning_bootstrap = (
            _auto_bootstrap_current_ticker(ticker, fund, universe_store)
            if mutate_learning
            else {"executed": False, "reason": "mutate_learning disabled"}
        )
        global_universe = _global_universe_summary(universe_store)
        learned_peer_edges = _top_learned_peer_edges(discovery_store, ticker, limit=5)

        if knowledge_model_payload:
            knowledge_model_payload["learning_bootstrap"] = learning_bootstrap
            knowledge_model_payload["global_universe"] = global_universe
            knowledge_model_payload["learned_peer_edges"] = learned_peer_edges
            if mutate_learning:
                knowledge_model_payload["learning_backfill"] = _backfill_learning_actuals(ticker, fund)
                knowledge_model_payload["learning_maintenance"] = _run_learning_maintenance(ticker, fund)
                knowledge_model_payload["learning_persistence"] = _persist_learning_snapshot(_result, knowledge_model_payload)
            else:
                disabled_note = {"executed": False, "reason": "mutate_learning disabled"}
                knowledge_model_payload["learning_backfill"] = disabled_note
                knowledge_model_payload["learning_maintenance"] = disabled_note
                knowledge_model_payload["learning_persistence"] = disabled_note
            _augment_learning_explainability(knowledge_model_payload, sector=sector, industry=industry, ticker=ticker, dashboard_data=_result)
            confidence_model = dict(knowledge_model_payload.get("confidence_model") or {})
            dashboard_breakdown = dict(confidence_model.get("dashboard_breakdown") or {})
            if dashboard_breakdown:
                _result["confidence_breakdown"] = dashboard_breakdown
                _result["confidence_score"] = int(dashboard_breakdown.get("total") or _result.get("confidence_score") or 0)
                _result["model_confidence_score"] = _result["confidence_score"]
            _result["analyst_view"]["key_assumptions"] += " Shared-brain learning is active; the Everything Knows Model panel shows the layer mix, analog evidence, and remaining weak spots."
            _result["analyst_view"]["model_risks"] = (
                f"Model uses {n} years of EODHD data ({fy_years_asc[0]}–{fy_years_asc[-1]}). "
                "Shared-brain overlays only move the forecast when the evidence is strong enough, and current gaps are surfaced in the assumptions tab."
            )
            _result["knowledge_model"] = knowledge_model_payload

        return _result

    except Exception as exc:
        logger.warning("EODHD build_dashboard_data(%s) failed: %s", ticker, exc, exc_info=True)
        return None
