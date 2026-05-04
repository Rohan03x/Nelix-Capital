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
from typing import Any

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
_TTL_PRICE_SEC  = 300       # 5  minutes for real-time price
_TTL_FUND_SEC   = 21_600    # 6  hours   for fundamentals
_TTL_EOD_HISTORY_SEC = 86_400

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
    return os.environ.get("EODHD_API_KEY", _EODHD_KEY_DEFAULT)


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
    p = _cache_path(name)
    if p is None or not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            obj = json.load(f)
        ts = datetime.fromisoformat(obj.get("_ts", "2000-01-01"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - ts).total_seconds() < ttl_sec:
            return obj.get("data")
    except Exception:
        pass
    return None


def _cache_write(name: str, data: Any) -> None:
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


def _get(endpoint: str, params: dict | None = None) -> Any | None:
    """HTTP GET against EODHD API. Returns parsed JSON or None on failure."""
    if not _REQUESTS_OK:
        return None
    p = {"api_token": _api_key(), "fmt": "json", **(params or {})}
    url = f"{_EODHD_BASE}/{endpoint}"
    try:
        r = _req.get(url, params=p, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
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
        _cache_write(cache_key, data)
        return data
    return None


def _fetch_fundamentals(eodhd_code: str) -> dict | None:
    """Fetch full fundamentals JSON. Returns raw dict or None."""
    cache_key = f"fund_{eodhd_code.replace('.','_')}"
    cached = _cache_read(cache_key, _TTL_FUND_SEC)
    if cached:
        return cached
    data = _get(f"fundamentals/{eodhd_code}")
    if data and isinstance(data, dict) and data.get("General"):
        _cache_write(cache_key, data)
        return data
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
        _cache_write(cache_key, history)
    return history


def _sorted_yearly(section: dict, n_max: int = 10) -> list[dict]:
    """Return up to n_max annual periods from an EODHD Financials sub-section,
    sorted newest-first (so index 0 = most recent year)."""
    yearly = section.get("yearly") or {}
    if not yearly:
        return []
    periods = sorted(yearly.items(), key=lambda kv: kv[0], reverse=True)
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
    ".T":   ".TSE",      # Tokyo Stock Exchange
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
                macro_regime="neutral",
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
        return {
            "enabled": True,
            "persisted": True,
            "reason": "appended",
            "record_id": record_id,
            "horizon_year": horizon_year,
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
        from auto_valuation.learning.maintenance import align_prediction_record_to_actuals, extract_actuals_from_fundamentals
    except Exception:
        return {
            "enabled": False,
            "updated_records": 0,
            "matured_records": 0,
            "reason": "learning-ledger-unavailable",
        }

    try:
        actuals_by_year = extract_actuals_from_fundamentals(fundamentals or {})
        if not actuals_by_year:
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
        for record in reader.query(ticker=ticker.upper(), scenario="base"):
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

    snapshot_payload = {
        **(snapshot_summary or {}),
        "background_target_symbols": seed_target,
        "background_seed_prefix_per_cycle": seed_prefix,
        "background_seed_pool_size": seed_pool_size,
        "background_runner": background_runner_state,
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
        live_summary = {
            "enabled": True,
            **universe_store.summary(
                stale_after_hours=int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18)),
                recent_days=int(LEARNING_CONFIG.get("symbol_universe_recent_days", 21)),
            ),
            "background_target_symbols": seed_target,
            "background_seed_prefix_per_cycle": seed_prefix,
            "background_seed_pool_size": seed_pool_size,
            "background_runner": background_runner_state,
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
) -> None:
    explainability = dict(knowledge_model.get("explainability") or {})
    if not explainability:
        return

    backfill = dict(knowledge_model.get("learning_backfill") or {})
    bootstrap = dict(knowledge_model.get("learning_bootstrap") or {})
    maintenance = dict(knowledge_model.get("learning_maintenance") or {})
    persistence = dict(knowledge_model.get("learning_persistence") or {})
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

    explainability["realized_evidence"] = {
        "matured_records": int(backfill.get("matured_records") or 0),
        "updated_records": int(backfill.get("updated_records") or 0),
        "note": (
            f"{int(backfill.get('matured_records') or 0)} matured prediction(s) exist for this ticker."
            if int(backfill.get("matured_records") or 0) > 0
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
    explainability["maintenance"] = {
        "ran": bool(maintenance.get("ran")),
        "reason": maintenance.get("reason"),
        "scanned_tickers": int(maintenance.get("scanned_tickers") or 0),
        "annual_postmortems_created": int(maintenance.get("annual_postmortems_created") or 0),
        "quinquennial_reports_created": int(maintenance.get("quinquennial_reports_created") or 0),
        "last_run_at": maintenance.get("last_run_at"),
    }
    explainability["current_snapshot"] = persistence

    data_gaps = list(explainability.get("data_gaps") or [])
    titles = {str(gap.get("title")) for gap in data_gaps}
    if int(backfill.get("matured_records") or 0) == 0 and "No ticker-specific realized evidence" not in titles:
        data_gaps.append(
            {
                "title": "No ticker-specific realized evidence",
                "detail": "This ticker has not yet rolled into matured forecast years, so its own company memory has not been back-tested in the shared ledger.",
                "severity": "amber",
            }
        )
    if not maintenance.get("ran") and maintenance.get("reason") not in ("throttled", None) and "Maintenance is behind" not in titles:
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

        is_periods = _sorted_yearly(is_sec, 10)   # newest-first
        bs_periods = _sorted_yearly(bs_sec, 10)
        cf_periods = _sorted_yearly(cf_sec, 10)

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

        M = 1e6   # divisor → $M

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
        tv_pct       = round(pv_tv / ev * 100, 1) if ev > 0 else 0

        rec       = "Undervalued" if upside >= 15 else ("Fairly Valued" if upside >= -10 else "Overvalued")
        rec_class = "green"       if upside >= 15 else ("amber"        if upside >= -10 else "red")

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

        bull_wacc = round(max(4.0, wacc - (1.0 * scenario_width_multiplier)), 1)
        bull_g = round(min(5.0, terminal_growth + min(1.2, 0.5 * scenario_width_multiplier)), 1)
        bear_wacc = round(min(25.0, wacc + (1.5 * scenario_width_multiplier)), 1)
        bear_g = round(max(-1.0, terminal_growth - min(1.6, 1.0 * scenario_width_multiplier)), 1)
        bull_growth = round(min(60.0, revenue_growth_near + (2.0 * scenario_width_multiplier)), 1)
        bear_growth = round(max(-15.0, revenue_growth_near - (3.0 * scenario_width_multiplier)), 1)
        bull_margin = round(max(ebit_margin_target, min(80.0, ebit_margin_target + (2.0 * scenario_width_multiplier))), 1)
        bear_margin = round(max(-10.0, ebit_margin_base_pct - max(1.0, scenario_width_multiplier)), 1)
        bull_iv, bull_up, bull_ev = _quick_iv(bull_wacc, bull_g, bull_growth, bull_margin)
        bear_iv, bear_up, bear_ev = _quick_iv(bear_wacc, bear_g, bear_growth, bear_margin)
        bull_rec = "Undervalued" if bull_up >= 15 else "Fairly Valued"
        bear_rec = "Overvalued"  if bear_up < -10 else "Fairly Valued"

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
            "confidence_score":    70,
            "data_freshness":      f"Live (EODHD — {n} years)",

            # DCF bridge
            "enterprise_value":    round(ev),
            "enterprise_value_m":  round(ev),
            "equity_value":        round(equity_value),
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
                    "narrative": "Accelerated revenue growth, margin expansion ahead of plan.",
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
                    "narrative": "Margin compression, slowing top-line growth, higher discount rate.",
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

            "is_demo": False,
            "data_source": "eodhd",
            "fiscal_year_end_month": fy_end_month_str,
            "is_live": True,

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
            _augment_learning_explainability(knowledge_model_payload, sector=sector, industry=industry)
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
