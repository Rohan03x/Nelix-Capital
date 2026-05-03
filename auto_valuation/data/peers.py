"""
data/peers.py — Peer group discovery via FMP screener API.

Uses the FMP /stock-screener endpoint to find comparable companies
by sector, industry, market-cap range, and geography.

Reference: Architecture Plan Parts 44, 46.

All monetary values in USD millions.
"""

from __future__ import annotations

import logging
from typing import Any

from auto_valuation.learning.cross_industry import AnalogObservation, find_analogs
from auto_valuation.learning.feature_space import SymbolFeatures, coerce_symbol_features

logger = logging.getLogger(__name__)

# Maximum peers to return when auto-discovering
MAX_PEERS_AUTO = 15

# Market-cap bracket multipliers for peer range (subject ×/÷ these factors)
MC_LOWER_FACTOR = 0.20   # floor = subject_mc × 0.20
MC_UPPER_FACTOR = 5.0    # ceiling = subject_mc × 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Main peer discovery function
# ─────────────────────────────────────────────────────────────────────────────

def find_peer_group(
    ticker:              str,
    sector:              str,
    industry:            str,
    subject_market_cap_mm: float,
    fmp_api_key:         str,
    country:             str = "US",
    max_peers:           int = MAX_PEERS_AUTO,
    exclude_tickers:     list[str] | None = None,
    exchange:            str | None = None,
) -> list[str]:
    """
    Use FMP /stock-screener to find peers matching sector / industry
    within a market-cap band around the subject company.

    Args:
        ticker:                 Subject ticker (excluded from results).
        sector:                 FMP sector string (e.g. "Technology").
        industry:               FMP industry string (e.g. "Software - Application").
        subject_market_cap_mm:  Subject market cap in USD millions.
        fmp_api_key:            FMP API key.
        country:                ISO2 country code filter (default "US").
        max_peers:              Maximum number of peers to return.
        exclude_tickers:        Additional tickers to exclude.
        exchange:               Optional exchange filter (e.g. "NASDAQ").

    Returns:
        List of ticker strings (up to max_peers), sorted by market cap desc.
    Reference: Architecture Plan Part 46.
    """
    import requests

    excluded = {ticker.upper()}
    if exclude_tickers:
        excluded.update(t.upper() for t in exclude_tickers)

    mc_low  = subject_market_cap_mm * MC_LOWER_FACTOR
    mc_high = subject_market_cap_mm * MC_UPPER_FACTOR

    params: dict[str, Any] = {
        "apikey":           fmp_api_key,
        "sector":           sector,
        "industry":         industry,
        "country":          country,
        "marketCapMoreThan": int(mc_low * 1_000_000),
        "marketCapLowerThan": int(mc_high * 1_000_000),
        "limit":            100,
        "isActivelyTrading": "true",
    }
    if exchange:
        params["exchange"] = exchange

    url = "https://financialmodelingprep.com/api/v3/stock-screener"
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        logger.warning("FMP screener failed for %s: %s", ticker, exc)
        return []

    if not isinstance(rows, list):
        return []

    peers: list[str] = []
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if sym and sym not in excluded:
            peers.append(sym)
        if len(peers) >= max_peers:
            break

    return peers


# ─────────────────────────────────────────────────────────────────────────────
# Peer validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def validate_peer_list(
    peer_tickers: list[str],
    min_peers:    int = 3,
    max_peers:    int = MAX_PEERS_AUTO,
) -> list[str]:
    """
    Validate and deduplicate a peer list.

    Raises ValueError if fewer than min_peers remain after deduplication.
    Truncates to max_peers.
    Reference: Architecture Plan Part 46.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for t in peer_tickers:
        upper = t.strip().upper()
        if upper and upper not in seen:
            seen.add(upper)
            cleaned.append(upper)

    if len(cleaned) < min_peers:
        logger.warning(
            "Only %d valid peers found (minimum %d recommended). "
            "Results may be less reliable.",
            len(cleaned),
            min_peers,
        )

    return cleaned[:max_peers]


def rank_peer_candidates(
    ticker: str,
    subject_features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    peer_observations: list[AnalogObservation],
    *,
    subject_sector: str = "",
    subject_industry: str = "",
    subject_vintage_year: int = 0,
    subject_market_cap_regime: str = "",
    subject_macro_regime: str = "neutral",
    max_peers: int = MAX_PEERS_AUTO,
    cross_sector_only: bool = False,
) -> dict[str, Any]:
    """Rank peer candidates using the shared symbol-brain analog engine."""
    subject = coerce_symbol_features(
        subject_features,
        ticker=ticker,
        sector=subject_sector,
        industry=subject_industry,
        market_cap_regime=subject_market_cap_regime or "mid",
        macro_regime=subject_macro_regime,
        sample_size=max(subject_vintage_year, 1),
    )
    analog_set = find_analogs(
        ticker,
        subject,
        peer_observations,
        subject_sector=subject_sector,
        subject_industry=subject_industry,
        subject_vintage_year=subject_vintage_year,
        subject_market_cap_regime=subject.market_cap_regime,
        subject_macro_regime=subject_macro_regime,
        max_results=max_peers,
        cross_sector_only=cross_sector_only,
    )
    return {
        "subject_summary": subject.summary,
        "cohorts": [
            {
                "label": cohort.label,
                "score": round(cohort.score, 3),
                "members": list(cohort.members),
                "explanation": cohort.explanation,
            }
            for cohort in analog_set.cohorts
        ],
        "peers": [
            {
                "ticker": match.analog.ticker,
                "score": round(match.analog_score, 3),
                "similarity": round(match.similarity_score, 3),
                "sector": match.analog.sector,
                "industry": match.analog.industry,
                "same_sector": match.analog.sector == subject_sector,
                "maturity_stage": match.analog.maturity_stage,
                "valuation_regime": match.analog.valuation_regime,
                "volatility_regime": match.analog.volatility_regime,
                "weights": {
                    "recency": round(match.recency_weight, 2),
                    "data_quality": round(match.quality_weight, 2),
                    "sample": round(match.sample_weight, 2),
                    "usefulness": round(match.usefulness_weight, 2),
                },
                "evidence": list(match.evidence),
            }
            for match in analog_set.analogs
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Peer group from override file
# ─────────────────────────────────────────────────────────────────────────────

def get_peers_from_overrides(
    overrides: dict[str, Any],
    exclude_tickers: list[str] | None = None,
) -> list[str] | None:
    """
    Return peer tickers from the analyst override file if present.

    Override keys: `peer_tickers` (list), `exclude_peers` (list).
    Returns None if no peer override exists.
    Reference: Architecture Plan Part 46.
    """
    peers = overrides.get("peer_tickers")
    if not peers:
        return None

    exclude = set(t.upper() for t in (overrides.get("exclude_peers") or []))
    if exclude_tickers:
        exclude.update(t.upper() for t in exclude_tickers)

    return [t.upper() for t in peers if t.upper() not in exclude]


# ─────────────────────────────────────────────────────────────────────────────
# Fetch LTM and NTM financials for a list of peers  (Part 5.2, 37)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_peer_financials(
    peer_tickers: list[str],
    fmp_api_key: str,
    include_ntm: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    Fetch LTM (and optionally NTM) financial metrics for a peer set.

    For each peer ticker, fetches:
      - Profile: market cap, current price, sector, industry
      - LTM income statement: revenue, EBITDA, EBIT, net income
      - LTM cash flow: FCF
      - LTM balance sheet: net debt (for EV calculation)
      - NTM estimates: consensus revenue and EBITDA (if include_ntm=True)

    Returns:
        dict {ticker: {metric_name: value_mm, ...}}
        Missing / error tickers are included with None values for all metrics.

    Reference: Architecture Plan Parts 5.2, 37.
    """
    import requests

    BASE = "https://financialmodelingprep.com/api/v3"
    results: dict[str, dict[str, Any]] = {}

    for ticker in peer_tickers:
        data: dict[str, Any] = {"ticker": ticker}
        try:
            # --- Income statement (annual, limit 1 = most recent) ---
            r = requests.get(
                f"{BASE}/income-statement/{ticker}",
                params={"apikey": fmp_api_key, "limit": 1},
                timeout=15,
            )
            r.raise_for_status()
            is_rows = r.json()
            if is_rows and isinstance(is_rows, list):
                is_ = is_rows[0]
                rev = is_.get("revenue") or 0
                ebit = is_.get("operatingIncome") or 0
                ni = is_.get("netIncome") or 0
                da = abs(is_.get("depreciationAndAmortization") or 0)
                data["revenue_ltm"]     = rev / 1e6
                data["ebitda_ltm"]      = (ebit + da) / 1e6
                data["ebit_ltm"]        = ebit / 1e6
                data["net_income_ltm"]  = ni / 1e6

            # --- Cash flow (annual, limit 1) ---
            r2 = requests.get(
                f"{BASE}/cash-flow-statement/{ticker}",
                params={"apikey": fmp_api_key, "limit": 1},
                timeout=15,
            )
            r2.raise_for_status()
            cf_rows = r2.json()
            if cf_rows and isinstance(cf_rows, list):
                cf = cf_rows[0]
                ocf = cf.get("operatingCashFlow") or 0
                capex = abs(cf.get("capitalExpenditure") or 0)
                data["fcf_ltm"] = (ocf - capex) / 1e6

            # --- Balance sheet (annual, limit 1) ---
            r3 = requests.get(
                f"{BASE}/balance-sheet-statement/{ticker}",
                params={"apikey": fmp_api_key, "limit": 1},
                timeout=15,
            )
            r3.raise_for_status()
            bs_rows = r3.json()
            if bs_rows and isinstance(bs_rows, list):
                bs = bs_rows[0]
                cash = (bs.get("cashAndCashEquivalents") or 0)
                st_inv = (bs.get("shortTermInvestments") or 0)
                lt_debt = (bs.get("longTermDebt") or 0)
                st_debt = (bs.get("shortTermDebt") or 0)
                data["cash_mm"]     = (cash + st_inv) / 1e6
                data["ibd_mm"]      = (lt_debt + st_debt) / 1e6
                data["net_debt_mm"] = (lt_debt + st_debt - cash - st_inv) / 1e6

            # --- Profile (market cap) ---
            r4 = requests.get(
                f"{BASE}/profile/{ticker}",
                params={"apikey": fmp_api_key},
                timeout=15,
            )
            r4.raise_for_status()
            prof = r4.json()
            if prof and isinstance(prof, list):
                p = prof[0]
                mc = p.get("mktCap") or 0
                data["market_cap_mm"] = mc / 1e6
                data["price"]         = p.get("price")
                data["sector"]        = p.get("sector")
                data["industry"]      = p.get("industry")

            # --- NTM estimates ---
            if include_ntm:
                r5 = requests.get(
                    f"{BASE}/analyst-estimates/{ticker}",
                    params={"apikey": fmp_api_key, "limit": 4, "period": "annual"},
                    timeout=15,
                )
                r5.raise_for_status()
                est_rows = r5.json()
                if est_rows and isinstance(est_rows, list):
                    est = est_rows[0]
                    data["revenue_ntm"]  = (est.get("estimatedRevenueAvg") or 0) / 1e6
                    data["ebitda_ntm"]   = (est.get("estimatedEbitdaAvg") or 0) / 1e6

        except Exception as exc:
            logger.warning("fetch_peer_financials failed for %s: %s", ticker, exc)
            # Fill with Nones so callers don't crash
            for key in ["revenue_ltm", "ebitda_ltm", "ebit_ltm", "net_income_ltm",
                        "fcf_ltm", "cash_mm", "ibd_mm", "net_debt_mm",
                        "market_cap_mm", "revenue_ntm", "ebitda_ntm"]:
                data.setdefault(key, None)

        results[ticker] = data

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical alias (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → find_peer_group
select_peer_group = find_peer_group
