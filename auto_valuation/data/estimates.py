"""
data/estimates.py — Fetch and process NTM (Next Twelve Months) consensus estimates.

Priority order for NTM revenue / EBITDA / EPS:
  1. FMP /analyst-estimates endpoint  (requires FMP API key)
  2. yfinance `.info` / `.financials` (free, but limited)
  3. Manual overrides from overrides/{TICKER}.json (ntm_revenue_mm, etc.)

Reference: Architecture Plan Part 44 (NTM multiples).

All monetary values in USD millions.  Dates as ISO strings.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.revenue_mm    = revenue_mm
        self.ebitda_mm     = ebitda_mm
        self.ebit_mm       = ebit_mm
        self.net_income_mm = net_income_mm
        self.eps           = eps
        self.source        = source

    def to_dict(self) -> dict[str, Any]:
        return {
            "ntm_revenue_mm":    self.revenue_mm,
            "ntm_ebitda_mm":     self.ebitda_mm,
            "ntm_ebit_mm":       self.ebit_mm,
            "ntm_net_income_mm": self.net_income_mm,
            "ntm_eps":           self.eps,
            "source":            self.source,
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
# yfinance NTM estimates
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ntm_estimates_yfinance(
    ticker: str,
) -> NTMEstimates:
    """
    Fetch NTM estimates from yfinance `.info` fields.

    yfinance provides `forwardEps`, `forwardPE`, and some revenue estimates.
    Reference: Architecture Plan Part 44.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        logger.warning("yfinance estimate fetch failed for %s: %s", ticker, exc)
        return NTMEstimates(source="yf_error")

    # Revenue estimate from totalRevenue (TTM only in yfinance — rough proxy)
    revenue_mm    = None
    rev_raw       = info.get("totalRevenue") or info.get("revenueEstimateAvg")
    if rev_raw:
        revenue_mm = float(rev_raw) / 1e6

    # yfinance sometimes exposes forward estimates
    eps           = info.get("forwardEps")
    forward_pe    = info.get("forwardPE")
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")

    ebitda_mm = None
    ebitda_raw = info.get("ebitda")
    if ebitda_raw:
        ebitda_mm = float(ebitda_raw) / 1e6

    return NTMEstimates(
        revenue_mm = revenue_mm,
        ebitda_mm  = ebitda_mm,
        eps        = float(eps) if eps else None,
        source     = "yfinance",
    )


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
) -> NTMEstimates:
    """
    Fetch NTM estimates using the priority chain:
      1. FMP (if api key available)
      2. yfinance fallback
      3. Override file values applied on top

    Reference: Architecture Plan Part 44.
    """
    estimates = NTMEstimates(source="none")

    if fmp_api_key:
        estimates = fetch_ntm_estimates_fmp(ticker, fmp_api_key, currency_to_usd_rate)

    if not estimates.revenue_mm:
        yf_est = fetch_ntm_estimates_yfinance(ticker)
        if yf_est.revenue_mm:
            estimates.revenue_mm = yf_est.revenue_mm
            if estimates.source == "none":
                estimates.source = "yfinance"

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
