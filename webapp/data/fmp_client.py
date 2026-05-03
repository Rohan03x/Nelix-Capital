"""
webapp/data/fmp_client.py
─────────────────────────
Financial Modeling Prep (FMP) API client.

Falls back gracefully to sample data when:
  - FMP_API_KEY is not set in environment
  - Any API call fails (network error, rate limit, etc.)
  - Ticker not found

Free tier: 250 requests/day at https://financialmodelingprep.com/stable

FRED API (no key required):
  - 10-year Treasury yield: https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to import requests; if unavailable, FMP stays disabled
try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False
    logger.warning("requests not installed — FMP client will use sample data only.")

FMP_BASE  = "https://financialmodelingprep.com/stable"
FRED_DGS10 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"

# Simple in-memory cache: {cache_key: (timestamp, data)}
_CACHE: dict[str, tuple[datetime, Any]] = {}
_CACHE_TTL_MINUTES = 60
_CACHE_DIR = Path(__file__).parent / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(key: str) -> Path:
    safe_key = key.replace(":", "_").replace("/", "_")
    return _CACHE_DIR / f"fmp_{safe_key}.json"


def _load_disk_cache(key: str) -> Any | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        cached_at = datetime.fromisoformat(payload.get("_ts", "2000-01-01T00:00:00"))
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - cached_at < timedelta(minutes=_CACHE_TTL_MINUTES):
            return payload.get("data")
    except Exception:
        return None
    return None


def _save_disk_cache(key: str, value: Any) -> None:
    try:
        with _cache_path(key).open("w", encoding="utf-8") as fh:
            json.dump({"_ts": datetime.now(timezone.utc).isoformat(), "data": value}, fh)
    except Exception:
        logger.debug("FMP disk cache write failed for %s", key, exc_info=True)


def _cached(key: str, fetch_fn) -> Any | None:
    """Return cached value if fresh, else call fetch_fn(), cache, and return."""
    if key in _CACHE:
        ts, val = _CACHE[key]
        if datetime.now(timezone.utc) - ts < timedelta(minutes=_CACHE_TTL_MINUTES):
            return val
    disk_val = _load_disk_cache(key)
    if disk_val is not None:
        _CACHE[key] = (datetime.now(timezone.utc), disk_val)
        return disk_val
    try:
        val = fetch_fn()
        if val is not None:
            _CACHE[key] = (datetime.now(timezone.utc), val)
            _save_disk_cache(key, val)
        return val
    except Exception as exc:
        logger.warning("FMP cache miss for %s: %s", key, exc)
        return None


def _get(path: str, params: dict | None = None) -> Any | None:
    """HTTP GET against FMP stable API. Returns parsed JSON or None on failure."""
    if not _REQUESTS_AVAILABLE:
        return None
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        return None
    p = {"apikey": api_key, **(params or {})}
    try:
        resp = _requests.get(f"{FMP_BASE}/{path}", params=p, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("Error Message"):
            logger.warning("FMP API error: %s", data["Error Message"])
            return None
        return data
    except Exception as exc:
        logger.warning("FMP GET %s failed: %s", path, exc)
        return None


# ─── Public helpers ───────────────────────────────────────────────────────────

def is_available() -> bool:
    """True if FMP_API_KEY is set and requests is installed."""
    return _REQUESTS_AVAILABLE and bool(os.environ.get("FMP_API_KEY", ""))


def get_profile(ticker: str) -> dict | None:
    """Fetch company profile (price, market cap, sector, beta, etc.)."""
    def _fetch():
        rows = _get("profile", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else None
    return _cached(f"profile:{ticker}", _fetch)


def get_income_statements(ticker: str, limit: int = 10) -> list[dict]:
    """Annual income statements, most-recent first."""
    def _fetch():
        rows = _get("income-statement", {"symbol": ticker, "limit": limit})
        return rows if isinstance(rows, list) else []
    return _cached(f"is:{ticker}", _fetch) or []


def get_balance_sheets(ticker: str, limit: int = 10) -> list[dict]:
    """Annual balance sheets, most-recent first."""
    def _fetch():
        rows = _get("balance-sheet-statement", {"symbol": ticker, "limit": limit})
        return rows if isinstance(rows, list) else []
    return _cached(f"bs:{ticker}", _fetch) or []


def get_cash_flows(ticker: str, limit: int = 10) -> list[dict]:
    """Annual cash flow statements, most-recent first."""
    def _fetch():
        rows = _get("cash-flow-statement", {"symbol": ticker, "limit": limit})
        return rows if isinstance(rows, list) else []
    return _cached(f"cf:{ticker}", _fetch) or []


def get_key_metrics_ttm(ticker: str) -> dict | None:
    """TTM key metrics (EV/EBITDA, P/E, FCF yield, ROIC, etc.)."""
    def _fetch():
        rows = _get("key-metrics-ttm", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else None
    return _cached(f"metrics_ttm:{ticker}", _fetch)


def get_analyst_estimates(ticker: str) -> list[dict]:
    """Analyst consensus EPS/Revenue estimates."""
    def _fetch():
        rows = _get("analyst-estimates", {"symbol": ticker})
        return rows if isinstance(rows, list) else []
    return _cached(f"est:{ticker}", _fetch) or []


def get_price_target_consensus(ticker: str) -> dict | None:
    """Analyst price target consensus (low, high, median)."""
    def _fetch():
        rows = _get("price-target-consensus", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else None
    return _cached(f"pt:{ticker}", _fetch)


def get_stock_peers(ticker: str) -> list[str]:
    """Return list of peer ticker symbols."""
    def _fetch():
        rows = _get("stock-peers", {"symbol": ticker})
        if isinstance(rows, list) and rows:
            return rows[0].get("peersList", [])
        return []
    return _cached(f"peers:{ticker}", _fetch) or []


def get_treasury_rate() -> float | None:
    """
    Fetch the latest 10-year US Treasury yield from FRED (no API key needed).
    Returns the rate as a float percentage (e.g. 4.45 for 4.45%).
    """
    if not _REQUESTS_AVAILABLE:
        return None

    def _fetch():
        try:
            resp = _requests.get(FRED_DGS10, timeout=8)
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
            # CSV has header "DATE,DGS10" then data rows newest last
            for line in reversed(lines):
                if line.startswith("DATE") or not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) == 2:
                    try:
                        rate = float(parts[1])
                        return rate
                    except ValueError:
                        continue
        except Exception as exc:
            logger.warning("FRED DGS10 fetch failed: %s", exc)
        return None

    return _cached("fred:dgs10", _fetch)


def get_financial_scores(ticker: str) -> dict | None:
    """FMP /financial-scores?symbol=X — Altman Z and Piotroski F pre-computed."""
    def _fetch():
        rows = _get("financial-scores", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
    return _cached(f"fscores:{ticker}", _fetch)


def get_market_risk_premium() -> dict | None:
    """FMP /market-risk-premium — returns list of country ERP estimates."""
    def _fetch():
        rows = _get("market-risk-premium", {})
        if isinstance(rows, list):
            for row in rows:
                if str(row.get("country", "")).upper() in ("US", "UNITED STATES"):
                    return row
            return rows[0] if rows else None
        return None
    return _cached("mrp:us", _fetch)


def get_grades_consensus(ticker: str) -> dict | None:
    """FMP /grades-consensus?symbol=X — analyst grades (Strong Buy/Buy/Hold/Sell)."""
    def _fetch():
        rows = _get("grades-consensus", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
    return _cached(f"grades:{ticker}", _fetch)


def get_insider_trade_stats(ticker: str) -> dict | None:
    """FMP /insider-trading/statistics?symbol=X — net insider buying/selling."""
    def _fetch():
        rows = _get("insider-trading/statistics", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
    return _cached(f"insider:{ticker}", _fetch)


def get_esg_ratings(ticker: str) -> dict | None:
    """FMP /esg-ratings?symbol=X — ESG composite score."""
    def _fetch():
        rows = _get("esg-ratings", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
    return _cached(f"esg:{ticker}", _fetch)


def get_ratios_ttm(ticker: str) -> dict | None:
    """FMP /ratios-ttm?symbol=X — TTM financial ratios."""
    def _fetch():
        rows = _get("ratios-ttm", {"symbol": ticker})
        return rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
    return _cached(f"ratiosttm:{ticker}", _fetch)


def build_dashboard_data(ticker: str) -> dict | None:
    """
    Build a dashboard data dict from live FMP data.
    Returns None if FMP is unavailable or the ticker is not found.

    Only called when is_available() is True.
    """
    if not is_available():
        return None

    profile = get_profile(ticker)
    if not profile:
        return None

    is_stmts  = get_income_statements(ticker)
    bs_stmts  = get_balance_sheets(ticker)
    cf_stmts  = get_cash_flows(ticker)
    metrics   = get_key_metrics_ttm(ticker)
    pt        = get_price_target_consensus(ticker)

    if not is_stmts or not bs_stmts or not cf_stmts:
        return None

    # Sort oldest-first for historical arrays
    is_sorted = sorted(is_stmts, key=lambda r: r.get("date", ""))
    bs_sorted = sorted(bs_stmts, key=lambda r: r.get("date", ""))
    cf_sorted = sorted(cf_stmts, key=lambda r: r.get("date", ""))

    def _safe(row: dict, key: str, default=0) -> float:
        val = row.get(key, default)
        try:
            return float(val) if val is not None else float(default)
        except (ValueError, TypeError):
            return float(default)

    years           = [int(r.get("calendarYear", r.get("date", "2015")[:4])) for r in is_sorted]
    revenue_list    = [_safe(r, "revenue") / 1e6 for r in is_sorted]          # $M
    gross_margin_l  = [_safe(r, "grossProfitRatio") * 100 for r in is_sorted]
    ebit_margin_l   = [_safe(r, "operatingIncomeRatio") * 100 for r in is_sorted]
    net_income_l    = [_safe(r, "netIncome") / 1e6 for r in is_sorted]

    # FCF = Operating CF − CapEx
    capex_list  = [abs(_safe(r, "capitalExpenditure")) / 1e6 for r in cf_sorted[-len(years):]]
    op_cf_list  = [_safe(r, "operatingCashFlow") / 1e6 for r in cf_sorted[-len(years):]]
    fcf_list    = [oc - cap for oc, cap in zip(op_cf_list, capex_list)]

    debt_list   = [_safe(r, "totalDebt") / 1e6 for r in bs_sorted[-len(years):]]
    shares_list = [_safe(r, "weightedAverageShsOutDil") / 1e6 for r in is_sorted]  # M shares

    # ROIC from key metrics history (fall back to 0 if missing)
    roic_list   = [0.0] * len(years)

    # Most-recent row for live inputs
    latest_is = is_stmts[0]
    latest_bs = bs_stmts[0]
    latest_cf = cf_stmts[0]

    price       = _safe(profile, "price")
    market_cap  = _safe(profile, "mktCap") / 1e6
    beta        = _safe(profile, "beta", 1.0)
    total_debt  = _safe(latest_bs, "totalDebt") / 1e6
    cash        = _safe(latest_bs, "cashAndCashEquivalentsAndShortTermInvestments") / 1e6
    if cash == 0:
        cash    = _safe(latest_bs, "cashAndShortTermInvestments") / 1e6
    net_debt    = total_debt - cash

    ebit_margin_base = _safe(latest_is, "operatingIncomeRatio") * 100
    revenue_base     = _safe(latest_is, "revenue") / 1e6
    diluted_shares   = _safe(latest_is, "weightedAverageShsOutDil") / 1e6

    # WACC inputs
    rf_rate = get_treasury_rate() or 4.4
    erp     = 5.2  # market risk premium — our standard assumption
    ke      = rf_rate + beta * erp
    # Simple capital structure
    equity_val   = price * diluted_shares * 1e6 / 1e6  # $M
    total_cap    = equity_val + total_debt
    e_weight     = equity_val / total_cap * 100 if total_cap > 0 else 100
    d_weight     = 100 - e_weight
    kd_pre       = _safe(latest_is, "interestExpense") / max(_safe(latest_bs, "totalDebt"), 1) * 100 if total_debt > 0 else 4.0
    kd_pre       = max(2.0, min(kd_pre, 12.0))
    tax_rate     = max(0, _safe(latest_is, "incomeTaxExpense") / max(_safe(latest_is, "incomeBeforeTax"), 1) * 100)
    tax_rate     = min(35.0, max(0.0, tax_rate))
    kd_post      = kd_pre * (1 - tax_rate / 100)
    wacc         = (e_weight / 100) * ke + (d_weight / 100) * kd_post

    wacc = round(max(6.0, min(20.0, wacc)), 1)

    # Analyst targets
    analyst_low    = _safe(pt, "targetLow") if pt else 0
    analyst_high   = _safe(pt, "targetHigh") if pt else 0
    analyst_median = _safe(pt, "targetConsensus") if pt else 0

    # Very simple IV estimate: use trailing FCF capitalised at wacc–g
    last_fcf   = fcf_list[-1] if fcf_list else 0
    g_terminal = 2.5
    if wacc / 100 > g_terminal / 100:
        tv    = last_fcf / (wacc / 100 - g_terminal / 100)
        ev    = sum(fcf_list[-5:]) * 0.8 + tv / (1 + wacc / 100) ** 5  # rough estimate
    else:
        ev    = market_cap
    equity_est     = max(0, ev - net_debt)
    iv_simple      = equity_est / diluted_shares if diluted_shares > 0 else price
    upside         = (iv_simple - price) / price * 100 if price > 0 else 0

    if upside >= 15:
        rec, rec_class = "Undervalued", "green"
    elif upside >= -10:
        rec, rec_class = "Fairly Valued", "amber"
    else:
        rec, rec_class = "Overvalued", "red"

    return {
        "ticker":           ticker.upper(),
        "company_name":     profile.get("companyName", ticker),
        "exchange":         profile.get("exchangeShortName", ""),
        "currency":         profile.get("currency", "USD"),
        "sector":           profile.get("sector", ""),
        "industry":         profile.get("industry", ""),
        "description":      profile.get("description", ""),

        "price":            round(price, 2),
        "price_date":       str(datetime.utcnow().date()),
        "market_cap":       round(market_cap),
        "fifty_two_week_low":  _safe(profile, "range", "0-0").split("-")[0] if isinstance(profile.get("range"), str) else 0,
        "fifty_two_week_high": _safe(profile, "range", "0-0").split("-")[1] if isinstance(profile.get("range"), str) else 0,
        "analyst_low":      round(analyst_low, 2),
        "analyst_high":     round(analyst_high, 2),
        "analyst_median":   round(analyst_median, 2),

        "intrinsic_value":  round(iv_simple, 2),
        "upside_pct":       round(upside, 1),
        "recommendation":   rec,
        "recommendation_class": rec_class,
        "confidence_score": 50,  # Will be computed by confidence engine
        "data_freshness":   "Live (FMP)",

        "enterprise_value": round(ev),
        "equity_value":     round(equity_est),
        "pv_ufcfs":         round(sum(fcf_list[-7:]) * 0.7),  # rough present value
        "pv_terminal":      round(ev * 0.65),                  # rough terminal component
        "tv_pct":           65.0,
        "diluted_shares":   round(diluted_shares, 1),

        "wacc":             wacc,
        "cost_of_equity":   round(ke, 1),
        "cost_of_debt_pre": round(kd_pre, 1),
        "cost_of_debt_post": round(kd_post, 1),
        "terminal_growth":  g_terminal,
        "tax_rate":         round(tax_rate, 1),
        "beta":             round(beta, 2),
        "risk_free_rate":   round(rf_rate, 1),
        "erp":              erp,
        "size_premium":     0.0,
        "equity_weight":    round(e_weight, 1),
        "debt_weight":      round(d_weight, 1),

        "total_debt":       round(total_debt),
        "cash_equiv":       round(cash),
        "net_debt":         round(net_debt),

        "revenue_growth_near":  5.0,
        "revenue_growth_term":  g_terminal,
        "ebit_margin_base":     round(ebit_margin_base, 1),
        "ebit_margin_target":   round(ebit_margin_base * 1.1, 1),
        "da_pct":               2.0,
        "capex_pct":            round(abs(_safe(latest_cf, "capitalExpenditure")) / max(revenue_base, 1) * 100, 1),
        "sbc_pct":              round(_safe(latest_cf, "stockBasedCompensation") / max(revenue_base * 1e6, 1) * 100, 1),
        "dso": 30.0, "dio": 60.0, "dpo": 40.0,
        "buyback_yield": 0.0, "dividend_yield": 0.0,

        "historical": {
            "years":         years,
            "revenue":       [round(r) for r in revenue_list],
            "gross_margin":  [round(m, 1) for m in gross_margin_l],
            "ebit_margin":   [round(m, 1) for m in ebit_margin_l],
            "net_income":    [round(n) for n in net_income_l],
            "fcf":           [round(f) for f in fcf_list],
            "capex":         [round(c) for c in capex_list],
            "debt":          [round(d) for d in debt_list],
            "roic":          roic_list,
            "shares":        [round(s) for s in shares_list],
        },

        "forecast":   [],  # placeholder — full DCF engine would populate this
        "sensitivity": None,
        "peers":       [],
        "peer_median": {},
        "flags":       [{"name": "Live Data", "status": "pass", "message": "Data sourced from FMP API in real-time."}],
        "assumptions": [],
        "insights":    [],
        "scenarios": {
            "base": {"label": "Base Case", "wacc": wacc, "g": g_terminal, "margin_target": round(ebit_margin_base * 1.1, 1), "rev_growth": 5.0, "iv": round(iv_simple, 2), "upside": round(upside, 1), "ev": round(ev), "recommendation": rec},
        },
        "analyst_view": {
            "valuation_says": f"Live data sourced from FMP API. Full DCF requires setting FMP_API_KEY. Simplified IV estimate: ${iv_simple:.2f}.",
            "key_assumptions": f"WACC: {wacc}%, terminal growth: {g_terminal}%, last FCF: ${last_fcf:.0f}M.",
            "model_risks": "This is a simplified real-time estimate. For a full DCF analysis, use the sample tickers (NKE, AAPL, TSLA).",
            "verify_before_use": ["Review latest earnings release", "Check analyst consensus estimates"],
        },
        "is_demo": False,
        "is_live": True,
    }
