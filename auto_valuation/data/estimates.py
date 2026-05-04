"""
data/estimates.py — Fetch and process NTM (Next Twelve Months) consensus estimates.

Priority order for NTM revenue / EBITDA / EPS:
  1. EODHD fund cache `Earnings.Trend` (+1y consensus, zero extra API calls)
  2. EODHD `/calendar/trends` endpoint (live fallback when not in fund cache)
  3. FMP `/analyst-estimates` endpoint (requires FMP API key)
  4. Manual overrides from overrides/{TICKER}.json (ntm_revenue_mm, etc.)

The legacy public-web fallback was removed because it used stale, trailing,
non-consensus fields.

Reference: Architecture Plan Part 44 (NTM multiples).

All monetary values in USD millions.  Dates as ISO strings.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _sf(v: Any, default: float | None = None) -> float | None:
    """Safe float conversion."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# NTM estimates data container
# ─────────────────────────────────────────────────────────────────────────────

class NTMEstimates:
    """Container for Next Twelve Months consensus estimates."""

    def __init__(
        self,
        revenue_mm:    float | None = None,
        ebitda_mm:     float | None = None,
        ebit_mm:       float | None = None,
        net_income_mm: float | None = None,
        eps:           float | None = None,
        source:        str          = "unknown",
        analyst_count: int          = 0,
        eps_revision_momentum_30d: float | None = None,
        revenue_growth_consensus: float | None = None,
    ) -> None:
        self.revenue_mm    = revenue_mm
        self.ebitda_mm     = ebitda_mm
        self.ebit_mm       = ebit_mm
        self.net_income_mm = net_income_mm
        self.eps           = eps
        self.source        = source
        self.analyst_count = analyst_count
        self.eps_revision_momentum_30d = eps_revision_momentum_30d
        self.revenue_growth_consensus = revenue_growth_consensus

    def to_dict(self) -> dict[str, Any]:
        return {
            "ntm_revenue_mm":    self.revenue_mm,
            "ntm_ebitda_mm":     self.ebitda_mm,
            "ntm_ebit_mm":       self.ebit_mm,
            "ntm_net_income_mm": self.net_income_mm,
            "ntm_eps":           self.eps,
            "source":            self.source,
            "analyst_count":     self.analyst_count,
            "eps_revision_momentum_30d": self.eps_revision_momentum_30d,
            "revenue_growth_consensus": self.revenue_growth_consensus,
        }


# ─────────────────────────────────────────────────────────────────────────────
# FMP analyst estimates
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ntm_estimates_fmp(
    ticker: str,
    fmp_api_key: str,
    currency_to_usd_rate: float = 1.0,
) -> NTMEstimates:
    """
    Fetch NTM estimates from FMP /analyst-estimates/{ticker}.

    Returns the first (most near-term) annual estimate row.
    Values are converted to USD millions using currency_to_usd_rate.

    Reference: Architecture Plan Part 44.
    """
    import requests
    url = f"https://financialmodelingprep.com/api/v3/analyst-estimates/{ticker.upper()}"
    try:
        resp = requests.get(
            url,
            params={"apikey": fmp_api_key, "limit": 4},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("FMP analyst-estimates failed for %s: %s", ticker, exc)
        return NTMEstimates(source="fmp_error")

    if not data or not isinstance(data, list):
        return NTMEstimates(source="fmp_empty")

    # Use the first (upcoming) estimate
    row = data[0]
    factor = currency_to_usd_rate / 1_000_000 if currency_to_usd_rate != 1.0 else 1e-6

    def _get(key: str) -> float | None:
        val = row.get(key) or row.get(f"estimated{key[0].upper()}{key[1:]}")
        if val and val != 0:
            return float(val) * (factor if abs(float(val)) > 1000 else 1.0)
        return None

    return NTMEstimates(
        revenue_mm    = _get("revenueAvg"),
        ebitda_mm     = _get("ebitdaAvg"),
        ebit_mm       = _get("ebitAvg"),
        net_income_mm = _get("netIncomeAvg"),
        eps           = row.get("epsAvg") or row.get("estimatedEpsAvg"),
        source        = "fmp",
    )


# ─────────────────────────────────────────────────────────────────────────────
# EODHD NTM estimates — primary source
# ─────────────────────────────────────────────────────────────────────────────

def _extract_ntm_from_trend(earnings: dict[str, Any]) -> dict[str, Any]:
    """Extract +1y consensus from EODHD ``Earnings.Trend`` (zero API calls).

    Returns dict with keys:
      ntm_revenue_mm, ntm_eps, analyst_count,
      revenue_growth_consensus, eps_revision_momentum_30d
    """
    trend = (earnings or {}).get("Trend") or {}
    if not isinstance(trend, dict):
        return {}

    plus_1y: dict[str, Any] | None = None
    for entry in trend.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("period") or "").lower() == "+1y":
            plus_1y = entry
            break
    if plus_1y is None:
        return {}

    rev_avg = _sf(plus_1y.get("revenueEstimateAvg"))
    eps_avg = _sf(plus_1y.get("earningsEstimateAvg"))
    n_eps = int(_sf(plus_1y.get("earningsEstimateNumberOfAnalysts"), default=0) or 0)
    n_rev = int(_sf(plus_1y.get("revenueEstimateNumberOfAnalysts"), default=0) or 0)
    rev_growth = _sf(plus_1y.get("revenueEstimateGrowth"))

    eps_now = _sf(plus_1y.get("epsTrendCurrent"))
    eps_30d = _sf(plus_1y.get("epsTrend30daysAgo"))
    revision = None
    if eps_now is not None and eps_30d not in (None, 0):
        revision = (eps_now - eps_30d) / abs(eps_30d)

    return {
        "ntm_revenue_mm": rev_avg / 1e6 if rev_avg else None,
        "ntm_eps": eps_avg,
        "analyst_count": max(n_eps, n_rev),
        "revenue_growth_consensus": rev_growth,
        "eps_revision_momentum_30d": revision,
    }


def fetch_ntm_estimates_eodhd(
    ticker: str,
    *,
    fund: dict[str, Any] | None = None,
) -> NTMEstimates:
    """Fetch NTM estimates from EODHD.

    Priority:
      1. If ``fund`` is provided (already loaded fundamentals dict), parse Trend.
      2. Try the local EODHD fund cache via webapp.data.eodhd_client._fetch_fundamentals.
      3. Live call to ``/calendar/trends?symbols={TICKER}``.
    """
    parsed: dict[str, Any] = {}

    if fund:
        parsed = _extract_ntm_from_trend(fund.get("Earnings") or {})

    if not parsed.get("ntm_revenue_mm") and not parsed.get("ntm_eps"):
        try:
            from webapp.data import eodhd_client as _eod
            code = _eod._eodhd_code(ticker)
            cached = _eod._fetch_fundamentals(code)
            if cached:
                parsed = _extract_ntm_from_trend(cached.get("Earnings") or {})
        except Exception as exc:
            logger.debug("EODHD fund cache lookup failed for %s: %s", ticker, exc)

    if not parsed.get("ntm_revenue_mm") and not parsed.get("ntm_eps"):
        api_key = os.getenv("EODHD_API_KEY", "").strip() or os.getenv("EOD_API_KEY", "").strip()
        if api_key:
            try:
                import requests
                code = ticker if "." in ticker else f"{ticker}.US"
                r = requests.get(
                    "https://eodhistoricaldata.com/api/calendar/trends",
                    params={"symbols": code, "api_token": api_key, "fmt": "json"},
                    timeout=12,
                )
                r.raise_for_status()
                data = r.json() or {}
                trends = data.get("trends") if isinstance(data, dict) else data
                if isinstance(trends, list) and trends:
                    plus_1y = next(
                        (e for e in trends if isinstance(e, dict) and str(e.get("period") or "").lower() == "+1y"),
                        trends[0] if isinstance(trends[0], dict) else None,
                    )
                    if isinstance(plus_1y, dict):
                        parsed = _extract_ntm_from_trend({"Trend": {"x": plus_1y}})
            except Exception as exc:
                logger.debug("EODHD calendar/trends failed for %s: %s", ticker, exc)

    if not parsed.get("ntm_revenue_mm") and not parsed.get("ntm_eps"):
        return NTMEstimates(source="eodhd_empty")

    return NTMEstimates(
        revenue_mm=parsed.get("ntm_revenue_mm"),
        eps=parsed.get("ntm_eps"),
        source="eodhd_trend",
        analyst_count=int(parsed.get("analyst_count") or 0),
        eps_revision_momentum_30d=parsed.get("eps_revision_momentum_30d"),
        revenue_growth_consensus=parsed.get("revenue_growth_consensus"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy public-web NTM estimates — disabled
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ntm_estimates_legacy_disabled(
    ticker: str,
) -> NTMEstimates:
    """Disabled placeholder for the removed public-web estimates source."""
    logger.debug("legacy NTM estimate source disabled for %s", ticker)
    return NTMEstimates(source="legacy_disabled")


# ─────────────────────────────────────────────────────────────────────────────
# Overrides — manual NTM estimates from the override file
# ─────────────────────────────────────────────────────────────────────────────

def apply_ntm_overrides(
    estimates: NTMEstimates,
    overrides: dict[str, Any],
) -> NTMEstimates:
    """
    Override NTM estimate values from the analyst override file.

    Keys checked: ntm_revenue_mm, ntm_ebitda_mm.
    Reference: Architecture Plan Part 44.
    """
    if "ntm_revenue_mm" in overrides and overrides["ntm_revenue_mm"]:
        estimates.revenue_mm = float(overrides["ntm_revenue_mm"])
        estimates.source = "override"
    if "ntm_ebitda_mm" in overrides and overrides["ntm_ebitda_mm"]:
        estimates.ebitda_mm = float(overrides["ntm_ebitda_mm"])
        estimates.source = "override"
    return estimates


# ─────────────────────────────────────────────────────────────────────────────
# Aggregated NTM fetch with fallback chain
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ntm_estimates(
    ticker:               str,
    fmp_api_key:          str | None = None,
    overrides:            dict[str, Any] | None = None,
    currency_to_usd_rate: float = 1.0,
    *,
    fund:                 dict[str, Any] | None = None,
) -> NTMEstimates:
    """
    Fetch NTM estimates using the priority chain:
      1. EODHD ``Earnings.Trend`` from fund cache (zero extra API calls)
      2. EODHD ``/calendar/trends`` live fallback (1 API call)
      3. FMP ``/analyst-estimates`` (if FMP_API_KEY available)
      4. Override file values applied on top

    Legacy public-web fallback removed — see ADAPTIVE_DCF_IMPROVEMENT_PLAN.md.
    """
    # 1+2) EODHD primary
    estimates = fetch_ntm_estimates_eodhd(ticker, fund=fund)

    # 3) FMP supplement when EODHD gave nothing useful
    if (not estimates.revenue_mm) and fmp_api_key:
        fmp_est = fetch_ntm_estimates_fmp(ticker, fmp_api_key, currency_to_usd_rate)
        if fmp_est.revenue_mm:
            estimates.revenue_mm = fmp_est.revenue_mm
            estimates.ebitda_mm = fmp_est.ebitda_mm or estimates.ebitda_mm
            estimates.ebit_mm = fmp_est.ebit_mm or estimates.ebit_mm
            estimates.net_income_mm = fmp_est.net_income_mm or estimates.net_income_mm
            estimates.eps = fmp_est.eps or estimates.eps
            if estimates.source in ("eodhd_empty", "unknown", "none"):
                estimates.source = "fmp"

    if overrides:
        estimates = apply_ntm_overrides(estimates, overrides)

    return estimates


# ─────────────────────────────────────────────────────────────────────────────
# NTM multiples from estimates
# ─────────────────────────────────────────────────────────────────────────────

def compute_ntm_multiples(
    enterprise_value_mm: float,
    equity_value_mm:     float,
    estimates:           NTMEstimates,
    diluted_shares_mm:   float = 0.0,
) -> dict[str, float | None]:
    """
    Compute forward (NTM) trading multiples.

    Reference: Architecture Plan Part 44.
    """
    def _div(num: float | None, den: float | None) -> float | None:
        if num is None or den is None or den == 0:
            return None
        return num / den

    ntm_pe = None
    if estimates.eps and estimates.eps > 0 and diluted_shares_mm > 0:
        price_per_share = equity_value_mm / diluted_shares_mm
        ntm_pe = price_per_share / estimates.eps

    return {
        "ntm_ev_revenue":  _div(enterprise_value_mm, estimates.revenue_mm),
        "ntm_ev_ebitda":   _div(enterprise_value_mm, estimates.ebitda_mm),
        "ntm_ev_ebit":     _div(enterprise_value_mm, estimates.ebit_mm),
        "ntm_pe":          ntm_pe,
    }
