"""
data/transactions.py — Precedent M&A transaction comps loader and analyser.

Reference: Architecture Plan Part 77.

Transactions are loaded from overrides/{TICKER}.json under the
"precedent_transactions" key. Each entry has:
  {
    "target":       "Company Name",
    "acquirer":     "Acquirer Name",
    "date":         "YYYY-MM-DD",
    "ev_mm":        12000,
    "ebitda_mm":    600,
    "revenue_mm":   3000,
    "control_premium_pct": 0.25    (optional)
  }

All monetary values in USD millions.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses  (Part 77)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PrecedentDeal:
    """Single M&A precedent transaction record (Part 77)."""
    target_name:       str
    acquirer_name:     str
    announcement_date: str                  # ISO date string 'YYYY-MM-DD'
    enterprise_value:  float                # Deal EV in USD millions
    equity_value:      float                # Implied equity value paid
    ltm_revenue:       Optional[float] = None
    ltm_ebitda:        Optional[float] = None
    ltm_ebit:          Optional[float] = None
    ltm_net_income:    Optional[float] = None
    sector:            Optional[str]   = None
    status:            str             = "closed"   # 'closed' | 'pending' | 'terminated'
    notes:             str             = ""


@dataclass
class TransactionCompsResult:
    """Output of precedent transactions analysis (Part 77)."""
    deals:             list             = field(default_factory=list)
    ev_revenue_25th:   Optional[float] = None
    ev_revenue_median: Optional[float] = None
    ev_revenue_75th:   Optional[float] = None
    ev_ebitda_25th:    Optional[float] = None
    ev_ebitda_median:  Optional[float] = None
    ev_ebitda_75th:    Optional[float] = None
    implied_ev_low:    Optional[float] = None
    implied_ev_high:   Optional[float] = None
    is_estimated:      bool            = False  # True if derived from control-premium fallback
    source:            str             = ""     # 'user_json' | 'control_premium_fallback'


# ─────────────────────────────────────────────────────────────────────────────
# Sector-level transaction multiple ranges  (used by output/metrics.py)
# Source: Damodaran sector data + GS/MS precedent transaction surveys
# ─────────────────────────────────────────────────────────────────────────────

_SECTOR_TRANSACTION_MULTIPLES: dict[str, dict[str, float]] = {
    "Technology":           {"ev_ebitda_low": 12.0, "ev_ebitda_high": 22.0, "ev_revenue_low": 3.0,  "ev_revenue_high": 8.0},
    "Healthcare":           {"ev_ebitda_low": 10.0, "ev_ebitda_high": 18.0, "ev_revenue_low": 2.5,  "ev_revenue_high": 6.0},
    "Financial Services":   {"ev_ebitda_low":  8.0, "ev_ebitda_high": 14.0, "ev_revenue_low": 1.5,  "ev_revenue_high": 4.0},
    "Consumer Discretionary":{"ev_ebitda_low": 8.0, "ev_ebitda_high": 14.0, "ev_revenue_low": 1.0,  "ev_revenue_high": 3.0},
    "Consumer Staples":     {"ev_ebitda_low":  9.0, "ev_ebitda_high": 15.0, "ev_revenue_low": 1.5,  "ev_revenue_high": 3.5},
    "Industrials":          {"ev_ebitda_low":  8.0, "ev_ebitda_high": 13.0, "ev_revenue_low": 1.0,  "ev_revenue_high": 2.5},
    "Energy":               {"ev_ebitda_low":  5.0, "ev_ebitda_high": 10.0, "ev_revenue_low": 1.0,  "ev_revenue_high": 3.0},
    "Materials":            {"ev_ebitda_low":  7.0, "ev_ebitda_high": 12.0, "ev_revenue_low": 1.0,  "ev_revenue_high": 2.5},
    "Real Estate":          {"ev_ebitda_low": 14.0, "ev_ebitda_high": 22.0, "ev_revenue_low": 5.0,  "ev_revenue_high": 12.0},
    "Utilities":            {"ev_ebitda_low": 10.0, "ev_ebitda_high": 16.0, "ev_revenue_low": 2.0,  "ev_revenue_high": 4.0},
    "Communication Services":{"ev_ebitda_low":8.0,  "ev_ebitda_high": 14.0, "ev_revenue_low": 1.5,  "ev_revenue_high": 4.0},
    "Default":              {"ev_ebitda_low":  8.0, "ev_ebitda_high": 14.0, "ev_revenue_low": 1.5,  "ev_revenue_high": 3.5},
}


# ─────────────────────────────────────────────────────────────────────────────
# Load from overrides file  (Part 77)
# ─────────────────────────────────────────────────────────────────────────────

def load_precedent_transactions(
    ticker: str,
    overrides_dir: str = "overrides",
) -> list[dict]:
    """
    Load precedent transactions from overrides/{TICKER}.json.

    Returns an empty list if no override file or no "precedent_transactions" key.
    Reference: Part 77.
    """
    path = Path(overrides_dir) / f"{ticker.upper()}.json"
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []

    return data.get("precedent_transactions", [])


# ─────────────────────────────────────────────────────────────────────────────
# Compute multiples  (Part 77)
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(data: list[float], pct: float) -> float:
    """Return the p-th percentile of a sorted list (linear interpolation)."""
    if not data:
        return 0.0
    s = sorted(data)
    n = len(s)
    idx = (n - 1) * pct / 100.0
    lo  = int(idx)
    hi  = lo + 1
    if hi >= n:
        return s[-1]
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def compute_transaction_multiples(
    transactions: list[dict],
) -> dict[str, Any]:
    """
    Compute EV/EBITDA and EV/Revenue statistics across a list of deals.

    Returns a dict:
      {
        "ev_ebitda": {"p25": .., "median": .., "p75": .., "mean": .., "n": ..},
        "ev_revenue": { ... },
        "control_premium": { ... },
      }

    Deals with missing or zero denominators are excluded per-multiple.
    Reference: Part 77.
    """
    ev_ebitda_vals:  list[float] = []
    ev_revenue_vals: list[float] = []
    premium_vals:    list[float] = []

    for t in transactions:
        ev = t.get("ev_mm") or 0

        ebitda = t.get("ebitda_mm") or 0
        if ev > 0 and ebitda > 0:
            ev_ebitda_vals.append(ev / ebitda)

        revenue = t.get("revenue_mm") or 0
        if ev > 0 and revenue > 0:
            ev_revenue_vals.append(ev / revenue)

        prem = t.get("control_premium_pct")
        if prem is not None and prem > 0:
            premium_vals.append(float(prem))

    def _stats(vals: list[float]) -> dict[str, Any]:
        if not vals:
            return {"p25": None, "median": None, "p75": None, "mean": None, "n": 0}
        return {
            "p25":    _percentile(vals, 25),
            "median": _percentile(vals, 50),
            "p75":    _percentile(vals, 75),
            "mean":   sum(vals) / len(vals),
            "n":      len(vals),
        }

    return {
        "ev_ebitda":       _stats(ev_ebitda_vals),
        "ev_revenue":      _stats(ev_revenue_vals),
        "control_premium": _stats(premium_vals),
        "deal_count":      len(transactions),
    }


def compute_transaction_comps_result(
    subject_ebitda_mm: float,
    subject_revenue_mm: float,
    multiples: dict[str, Any],
) -> dict[str, Any]:
    """
    Apply precedent transaction multiples to the subject company to derive
    an implied EV range.

    Returns a dict:
      {
        "ev_from_ebitda": {"low": .., "mid": .., "high": ..},
        "ev_from_revenue": { ... },
        "blended_ev_range": {"low": .., "high": ..},
      }

    Reference: Part 77.
    """
    result: dict[str, Any] = {}

    # EV/EBITDA
    ee = multiples.get("ev_ebitda", {})
    if subject_ebitda_mm > 0 and ee.get("n", 0) > 0 and ee.get("p25") is not None:
        result["ev_from_ebitda"] = {
            "low":  subject_ebitda_mm * ee["p25"],
            "mid":  subject_ebitda_mm * ee["median"],
            "high": subject_ebitda_mm * ee["p75"],
        }

    # EV/Revenue
    er = multiples.get("ev_revenue", {})
    if subject_revenue_mm > 0 and er.get("n", 0) > 0 and er.get("p25") is not None:
        result["ev_from_revenue"] = {
            "low":  subject_revenue_mm * er["p25"],
            "mid":  subject_revenue_mm * er["median"],
            "high": subject_revenue_mm * er["p75"],
        }

    # Blended range: low = min of p25s, high = max of p75s
    lows  = [v["low"]  for v in result.values() if isinstance(v, dict) and "low"  in v]
    highs = [v["high"] for v in result.values() if isinstance(v, dict) and "high" in v]
    if lows and highs:
        result["blended_ev_range"] = {"low": min(lows), "high": max(highs)}

    result["multiples_used"] = multiples
    return result
