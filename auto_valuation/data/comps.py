"""
data/comps.py — Trading-comps engine and peer proforma screening.

Reference: Architecture Plan Parts 37, 37.2, 38, 78.

All monetary values in USD millions.
Multiples computed on LTM (last twelve months) or NTM (next twelve months) basis.
"""

from __future__ import annotations

import statistics
from typing import Any

from auto_valuation.utils.error import safe_divide
from auto_valuation.data.transactions import _percentile


# ─────────────────────────────────────────────────────────────────────────────
# Trading multiples for a single peer  (Part 37)
# ─────────────────────────────────────────────────────────────────────────────

def compute_peer_multiples(
    peer_ticker: str,
    market_cap_mm: float,
    net_debt_mm: float,
    revenue_ltm: float,
    ebitda_ltm: float,
    ebit_ltm:   float,
    fcf_ltm:    float,
    net_income_ltm: float,
    revenue_ntm: float | None = None,
    ebitda_ntm:  float | None = None,
    ebit_ntm:    float | None = None,
    ufcf_ltm:   float | None = None,
) -> dict[str, Any]:
    """
    Compute a full set of trading multiples for one peer.

    Returned dict keys:
      ev_revenue_ltm, ev_ebitda_ltm, ev_ebit_ltm,
      p_fcf_ltm, p_e_ltm, ev_ufcf_ltm (if ufcf_ltm provided),
      ev_revenue_ntm (if ntm_data provided), ev_ebitda_ntm, ev_ebit_ntm

    ufcf_ltm: Unlevered Free Cash Flow (LTM) — enables EV/UFCF multiple per
              Macabacus Valuation Multiples standard (Session 14 gap G).

    Reference: Part 37; Macabacus Valuation Multiples.
    """
    ev = market_cap_mm + net_debt_mm

    multiples: dict[str, Any] = {
        "ticker":       peer_ticker,
        "market_cap":   market_cap_mm,
        "net_debt":     net_debt_mm,
        "ev":           ev,
        # LTM
        "ev_revenue_ltm": safe_divide(ev,          revenue_ltm,    None),
        "ev_ebitda_ltm":  safe_divide(ev,          ebitda_ltm,     None),
        "ev_ebit_ltm":    safe_divide(ev,          ebit_ltm,       None),
        "p_fcf_ltm":      safe_divide(market_cap_mm, fcf_ltm,      None),
        "p_e_ltm":        safe_divide(market_cap_mm, net_income_ltm, None),
    }

    # EV/UFCF — unlevered FCF multiple (Macabacus standard)
    if ufcf_ltm is not None and ufcf_ltm > 0:
        multiples["ev_ufcf_ltm"] = safe_divide(ev, ufcf_ltm, None)

    # NTM multiples (if provided and positive)
    if revenue_ntm and revenue_ntm > 0:
        multiples["ev_revenue_ntm"] = safe_divide(ev, revenue_ntm, None)
    if ebitda_ntm and ebitda_ntm > 0:
        multiples["ev_ebitda_ntm"] = safe_divide(ev, ebitda_ntm, None)
    if ebit_ntm and ebit_ntm > 0:
        multiples["ev_ebit_ntm"] = safe_divide(ev, ebit_ntm, None)

    return multiples


# ─────────────────────────────────────────────────────────────────────────────
# Peer set statistics  (Part 38)
# ─────────────────────────────────────────────────────────────────────────────

_MULTIPLE_KEYS = [
    "ev_revenue_ltm", "ev_ebitda_ltm", "ev_ebit_ltm",
    "p_fcf_ltm", "p_e_ltm", "ev_ufcf_ltm",
    "ev_revenue_ntm", "ev_ebitda_ntm",
]


def _weighted_percentile(values: list[tuple[float, float]], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted((value, max(weight, 0.0)) for value, weight in values if weight > 0)
    if not ordered:
        return None
    total_weight = sum(weight for _, weight in ordered)
    threshold = total_weight * (percentile / 100.0)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def compute_peer_set_stats(
    peer_multiples: list[dict],
) -> dict[str, dict[str, Any]]:
    """
    Compute p25 / median / p75 / mean across the peer set for each multiple.

    Excludes None values (peers where the denominator was zero/unavailable).
    Reference: Part 38.
    """
    stats: dict[str, dict[str, Any]] = {}
    for key in _MULTIPLE_KEYS:
        vals = [
            p[key] for p in peer_multiples
            if p.get(key) is not None and p[key] > 0
        ]
        if not vals:
            stats[key] = {"p25": None, "median": None, "p75": None, "mean": None, "n": 0}
        else:
            stats[key] = {
                "p25":    _percentile(vals, 25),
                "median": _percentile(vals, 50),
                "p75":    _percentile(vals, 75),
                "mean":   sum(vals) / len(vals),
                "n":      len(vals),
            }
    return stats


def compute_weighted_peer_set_stats(
    peer_multiples: list[dict],
    peer_weights: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute analog-weighted peer statistics for comps transfer logic."""
    peer_weights = peer_weights or {}
    stats: dict[str, dict[str, Any]] = {}
    for key in _MULTIPLE_KEYS:
        values = [
            (float(peer[key]), float(peer_weights.get(str(peer.get("ticker") or ""), 1.0)))
            for peer in peer_multiples
            if peer.get(key) is not None and peer[key] > 0
        ]
        total_weight = sum(weight for _, weight in values if weight > 0)
        if not values or total_weight <= 0:
            stats[key] = {
                "weighted_p25": None,
                "weighted_median": None,
                "weighted_p75": None,
                "weighted_mean": None,
                "weight_sum": 0.0,
                "n": 0,
            }
            continue

        stats[key] = {
            "weighted_p25": _weighted_percentile(values, 25),
            "weighted_median": _weighted_percentile(values, 50),
            "weighted_p75": _weighted_percentile(values, 75),
            "weighted_mean": sum(value * weight for value, weight in values) / total_weight,
            "weight_sum": round(total_weight, 3),
            "n": len(values),
        }
    return stats


def build_cross_symbol_comps_view(
    peer_multiples: list[dict],
    peer_rankings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Combine analog peer rankings with weighted comps statistics for downstream consumers."""
    peer_rankings = peer_rankings or []
    peer_weights = {str(row.get("ticker") or ""): float(row.get("score") or 0.0) for row in peer_rankings}
    weighted_stats = compute_weighted_peer_set_stats(peer_multiples, peer_weights)
    return {
        "weighted_stats": weighted_stats,
        "top_ranked_peers": [
            {
                "ticker": row.get("ticker"),
                "score": round(float(row.get("score") or 0.0), 3),
                "similarity": round(float(row.get("similarity") or 0.0), 3),
                "evidence": row.get("evidence") or [],
            }
            for row in sorted(peer_rankings, key=lambda item: float(item.get("score") or 0.0), reverse=True)[:5]
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Apply multiples to subject company  (Part 37.1)
# ─────────────────────────────────────────────────────────────────────────────

def apply_comps_to_subject(
    peer_stats: dict[str, dict[str, Any]],
    subject_revenue_ltm: float,
    subject_ebitda_ltm:  float,
    subject_ebit_ltm:    float,
    subject_fcf_ltm:     float,
    subject_ni_ltm:      float,
    subject_revenue_ntm: float | None = None,
    subject_ebitda_ntm:  float | None = None,
) -> dict[str, Any]:
    """
    Derive implied EV (and market cap) ranges for the subject company
    by applying peer-set median multiples.

    Returns dict with per-multiple implied EVs and a summary range.
    Reference: Part 37.1.
    """
    result: dict[str, Any] = {}
    implied_evs: list[float] = []   # collect p25–p75 midpoints for summary

    def _apply(key: str, denominator: float | None, is_ev: bool = True) -> None:
        if denominator is None or denominator <= 0:
            return
        s = peer_stats.get(key, {})
        if s.get("n", 0) == 0 or s.get("median") is None:
            return
        mid   = denominator * s["median"]
        low   = denominator * s["p25"]
        high  = denominator * s["p75"]
        result[f"implied_ev_from_{key}"] = {"low": low, "mid": mid, "high": high}
        implied_evs.append(mid)

    _apply("ev_revenue_ltm", subject_revenue_ltm)
    _apply("ev_ebitda_ltm",  subject_ebitda_ltm)
    _apply("ev_ebit_ltm",    subject_ebit_ltm)
    _apply("ev_revenue_ntm", subject_revenue_ntm)
    _apply("ev_ebitda_ntm",  subject_ebitda_ntm)

    # Summary: min of p25 lows, max of p75 highs across all methods
    lows  = [v["low"]  for v in result.values() if isinstance(v, dict) and "low"  in v]
    highs = [v["high"] for v in result.values() if isinstance(v, dict) and "high" in v]
    if lows and highs:
        result["comps_ev_low_mm"]  = min(lows)
        result["comps_ev_high_mm"] = max(highs)
        result["comps_ev_mid_mm"]  = sum(implied_evs) / len(implied_evs) if implied_evs else None

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Peer proforma screening  (Part 78)
# ─────────────────────────────────────────────────────────────────────────────

def check_peer_proforma_events(
    peer_tickers: list[str],
    recent_8k_data: dict[str, list[dict]] | None = None,
    lookback_days: int = 365,
) -> dict[str, list[str]]:
    """
    Screen peers for material corporate events (M&A, spin-offs, restatements)
    that would distort LTM multiples.

    recent_8k_data: pre-fetched dict {ticker: [8k_filings]} from fetch_sec_filings_8k().
    If not provided, returns empty dict (caller should fetch data first).

    Returns {ticker: [event_descriptions]} for flagged peers only.
    Reference: Part 78.
    """
    if not recent_8k_data:
        return {}

    _MA_KEYWORDS = {
        "merger", "acquisition", "acqui", "spinoff", "spin-off",
        "divest", "divestiture", "restatement", "restate", "impairment",
        "restructuring", "strategic review",
    }

    flagged: dict[str, list[str]] = {}
    for ticker in peer_tickers:
        filings = recent_8k_data.get(ticker, [])
        events: list[str] = []
        for filing in filings:
            title = (filing.get("title") or filing.get("type") or "").lower()
            desc  = (filing.get("description") or "").lower()
            if any(kw in title or kw in desc for kw in _MA_KEYWORDS):
                events.append(
                    filing.get("title") or filing.get("type") or "Unknown event"
                )
        if events:
            flagged[ticker] = events

    return flagged


def apply_manual_proforma_adjustments(
    peer_data: list[dict],
    adjustments: dict[str, dict],
) -> list[dict]:
    """
    Apply manual proforma adjustments to peer financial data.

    adjustments dict format (from overrides/{TICKER}.json peer_proforma_adjustments):
      {
        "AAPL": {
          "revenue_adjustment_mm": -2000,
          "ebitda_adjustment_mm":  -300,
          "note": "Exclude divested segment"
        }
      }

    Reference: Part 78.
    """
    result: list[dict] = []
    for peer in peer_data:
        peer = dict(peer)
        ticker = peer.get("ticker", "")
        adj = adjustments.get(ticker, {})
        if adj:
            for field, delta in adj.items():
                if field.endswith("_adjustment_mm") and isinstance(delta, (int, float)):
                    base_field = field.replace("_adjustment_mm", "")
                    if base_field in peer and peer[base_field] is not None:
                        peer[base_field] = peer[base_field] + delta
                        peer.setdefault("proforma_notes", [])
                        peer["proforma_notes"].append(
                            f"{base_field} adjusted by {delta:+,.0f}M: {adj.get('note', '')}"
                        )
        result.append(peer)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Football field summary  (Part 37.2)
# ─────────────────────────────────────────────────────────────────────────────

def build_football_field(
    dcf_ev_low:   float,
    dcf_ev_high:  float,
    comps_ev_low: float,
    comps_ev_high: float,
    transactions_ev_low: float | None = None,
    transactions_ev_high: float | None = None,
    net_debt:     float = 0.0,
    shares_mm:    float = 1.0,
    current_price: float | None = None,
) -> list[dict]:
    """
    Assemble a football field valuation summary converting EV ranges to
    per-share equity value ranges.

    Returns a list of dicts (one per method) for use in Excel charts.
    Reference: Part 37.2.
    """
    def _to_price(ev: float) -> float:
        equity = ev - net_debt
        return safe_divide(equity, shares_mm, 0.0)

    rows: list[dict] = [
        {
            "method":     "DCF (WACC/g sensitivity)",
            "ev_low_mm":  dcf_ev_low,
            "ev_high_mm": dcf_ev_high,
            "price_low":  _to_price(dcf_ev_low),
            "price_high": _to_price(dcf_ev_high),
        },
        {
            "method":     "Trading Comps",
            "ev_low_mm":  comps_ev_low,
            "ev_high_mm": comps_ev_high,
            "price_low":  _to_price(comps_ev_low),
            "price_high": _to_price(comps_ev_high),
        },
    ]

    if transactions_ev_low is not None and transactions_ev_high is not None:
        rows.append({
            "method":     "Precedent Transactions",
            "ev_low_mm":  transactions_ev_low,
            "ev_high_mm": transactions_ev_high,
            "price_low":  _to_price(transactions_ev_low),
            "price_high": _to_price(transactions_ev_high),
        })

    if current_price is not None:
        rows.append({
            "method":     "Current Price",
            "ev_low_mm":  current_price * shares_mm + net_debt,
            "ev_high_mm": current_price * shares_mm + net_debt,
            "price_low":  current_price,
            "price_high": current_price,
        })

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Pro-forma peer filter  (Part 78)
# ─────────────────────────────────────────────────────────────────────────────

def filter_peers_with_events(
    peer_tickers: list[str],
    pf_warnings: dict[str, list],
    exclude_flagged: bool = False,
) -> list[str]:
    """
    Filter peers that have outstanding pro-forma event warnings.

    If *exclude_flagged* is True, any ticker that appears as a key in
    *pf_warnings* (i.e. has at least one warning) is removed from the
    returned list.

    If *exclude_flagged* is False (the default), all peers are returned
    unchanged — the caller is responsible for logging or surfacing the
    warnings to the analyst.

    Args:
        peer_tickers:    Full list of peer ticker symbols.
        pf_warnings:     Dict mapping ticker → list-of-warning-strings.
                         Populated by check_peer_proforma_events().
        exclude_flagged: When True, remove flagged peers from comps.

    Returns:
        Filtered (or original) list of ticker symbols.

    Reference: Architecture Plan Part 78.
    """
    if not exclude_flagged:
        return list(peer_tickers)
    return [t for t in peer_tickers if t not in pf_warnings]


# ─────────────────────────────────────────────────────────────────────────────
# Peer enterprise value  (Part 5.2 / Macabacus)
# ─────────────────────────────────────────────────────────────────────────────

def compute_peer_ev(
    market_cap_mm: float,
    ibd_mm: float,
    cash_mm: float,
    st_investments_mm: float = 0.0,
    nci_mm: float = 0.0,
    preferred_mm: float = 0.0,
) -> float:
    """
    Compute enterprise value for a peer company using the Macabacus convention:

        EV = market_cap + IBD − cash − ST_investments + NCI + preferred

    IBD = interest-bearing debt (short-term + long-term financial debt).
    NCI (non-controlling interest) and preferred stock are added because EV
    represents the value of the ENTIRE firm, not just common equity holders.

    Args:
        market_cap_mm:     Market capitalisation ($M).
        ibd_mm:            Total interest-bearing debt: ST debt + LT debt + finance leases ($M).
        cash_mm:           Cash and cash equivalents ($M).
        st_investments_mm: Short-term investments / marketable securities ($M).
        nci_mm:            Non-controlling interest at book value ($M).
        preferred_mm:      Preferred stock at liquidation value ($M).

    Returns:
        float: Enterprise Value ($M).

    Reference: Architecture Plan Part 5.2, Macabacus EV bridge.
    """
    return (
        market_cap_mm
        + ibd_mm
        - cash_mm
        - st_investments_mm
        + nci_mm
        + preferred_mm
    )


# ─────────────────────────────────────────────────────────────────────────────
# NM (not meaningful) multiple exclusion  (Part 21.2)
# ─────────────────────────────────────────────────────────────────────────────

def exclude_nm_multiples(
    multiples: list[float | None],
    iqr_threshold: float = 3.0,
) -> list[float]:
    """
    Exclude NM (not meaningful) multiples from a peer list.

    A multiple is excluded when:
      1. It is None (denominator was zero or unavailable).
      2. It is ≤ 0 (negative denominator — economically meaningless).
      3. It is an outlier: multiple > median ± iqr_threshold × IQR.

    Outlier detection uses the IQR method (Tukey fences):
        fence = median ± iqr_threshold × IQR
    Default iqr_threshold = 3.0 (wide fence, only removes extreme outliers).

    Args:
        multiples:      Raw list of computed multiples, may contain None.
        iqr_threshold:  Fence multiplier for IQR outlier detection (default 3.0).

    Returns:
        Cleaned list of valid, non-outlier multiples.

    Reference: Architecture Plan Part 21.2.
    """
    # Step 1: Remove None and non-positive
    clean = [m for m in multiples if m is not None and m > 0]
    if len(clean) < 3:
        return clean  # not enough data for IQR test

    # Step 2: IQR outlier removal
    sorted_vals = sorted(clean)
    n = len(sorted_vals)
    q1 = sorted_vals[n // 4]
    q3 = sorted_vals[(3 * n) // 4]
    iqr = q3 - q1

    if iqr == 0:
        return clean  # all same value, nothing to remove

    lower_fence = q1 - iqr_threshold * iqr
    upper_fence = q3 + iqr_threshold * iqr

    return [m for m in clean if lower_fence <= m <= upper_fence]


# ─────────────────────────────────────────────────────────────────────────────
# Forward multiples  (Parts 48, N11)
# ─────────────────────────────────────────────────────────────────────────────

def compute_forward_multiples(
    peer_ev: dict,
    ntm_estimates: dict,
) -> dict:
    """
    Compute NTM (next-twelve-months) EV multiples for one peer.

    *peer_ev*       — dict output of compute_peer_ev() containing 'ev_mm'.
    *ntm_estimates* — dict from fetch_ntm_estimates(), expected keys:
                        estimatedRevenueAvg, estimatedEbitdaAvg,
                        estimatedEbitAvg, estimatedEpsAvg.

    Returns a dict with:
      ntm_ev_revenue, ntm_ev_ebitda, ntm_ev_ebit, ntm_pe
      (None if denominator is zero or missing).

    Reference: Architecture Plan Parts 48, N11.
    """
    ev_mm = peer_ev.get("ev_mm") or 0.0

    def _safe_div(num, denom):
        if denom and denom != 0:
            return round(num / denom, 4)
        return None

    ntm_rev    = ntm_estimates.get("estimatedRevenueAvg")  or 0
    ntm_ebitda = ntm_estimates.get("estimatedEbitdaAvg")   or 0
    ntm_ebit   = ntm_estimates.get("estimatedEbitAvg")     or 0
    ntm_eps    = ntm_estimates.get("estimatedEpsAvg")      or 0

    # Price for P/E — use market cap from peer_ev if available
    mkt_cap_mm = peer_ev.get("market_cap_mm") or peer_ev.get("equity_value_mm") or 0
    shares_mm  = peer_ev.get("diluted_shares_mm") or 1.0
    price      = mkt_cap_mm / shares_mm if shares_mm else 0

    return {
        "ntm_ev_revenue": _safe_div(ev_mm, ntm_rev / 1e6) if ntm_rev else None,
        "ntm_ev_ebitda":  _safe_div(ev_mm, ntm_ebitda / 1e6) if ntm_ebitda else None,
        "ntm_ev_ebit":    _safe_div(ev_mm, ntm_ebit / 1e6) if ntm_ebit else None,
        "ntm_pe":         _safe_div(price, ntm_eps) if ntm_eps else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → compute_peer_set_stats
compute_comps_summary_stats = compute_peer_set_stats

#: Canonical checklist name → apply_comps_to_subject
apply_multiples_to_subject = apply_comps_to_subject
