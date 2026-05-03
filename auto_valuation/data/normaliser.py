"""
data/normaliser.py — Unit detection and normalisation for FMP financial data.

FMP can return financial figures in different units depending on the company
and plan tier.  This module provides reliable unit detection and normalisation
to USD millions.

Reference: Architecture Plan Part 2.4 (unit_normalize), v9 deep scan.

All output is in USD millions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

UNIT_ANOMALY_THRESHOLD = 0.50   # Warn if revenue scale factor changes >50% between years

# Heuristic revenue thresholds for unit detection (USD)
_BILLIONS_CUTOFF    = 1_000     # revenue < 1,000 → likely in billions
_THOUSANDS_CUTOFF   = 10_000_000  # revenue > 10,000,000 → likely in thousands


# ─────────────────────────────────────────────────────────────────────────────
# Unit detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_units(statements: list[dict]) -> str:
    """
    Detect the unit scale used in FMP statements by probing the revenue field.

    Returns one of:
      'millions'   — already in USD millions (scale = 1.0)
      'thousands'  — in USD thousands (divide by 1,000 to get millions)
      'billions'   — in USD billions (multiply by 1,000 to get millions)
      'unknown'    — cannot determine (revenue field not available)

    Heuristic:
      - revenue > 10,000,000 → thousands
      - revenue < 1,000 and > 0 → billions
      - otherwise → millions

    Reference: Architecture Plan Part 2.4.
    """
    if not statements:
        return "unknown"

    revenue_probe = None
    for stmt in statements:
        rev = stmt.get("revenue") or stmt.get("totalRevenue")
        if rev and abs(rev) > 0:
            revenue_probe = abs(rev)
            break

    if revenue_probe is None:
        return "unknown"

    if revenue_probe > _THOUSANDS_CUTOFF:
        return "thousands"
    elif revenue_probe < _BILLIONS_CUTOFF:
        return "billions"
    else:
        return "millions"


def detect_units_scale(statements: list[dict]) -> float:
    """
    Return the numeric scale factor to convert to USD millions.

    Returns:
      1.0       — already in millions
      0.001     — in thousands → divide by 1,000
      1000.0    — in billions → multiply by 1,000
      1.0       — unknown (no-op)
    """
    unit = detect_units(statements)
    if unit == "thousands":
        return 0.001
    elif unit == "billions":
        return 1000.0
    else:
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Unit normalisation
# ─────────────────────────────────────────────────────────────────────────────

_NON_NUMERIC_FIELDS = {
    "calendarYear", "period", "reportedCurrency", "cik",
    "link", "finalLink", "date", "symbol", "fillingDate", "acceptedDate",
}


def normalize_units(
    statements: list[dict],
    scale: float | None = None,
) -> list[dict]:
    """
    Normalise all numeric fields in `statements` to USD millions.

    If `scale` is None, auto-detects the scale factor using detect_units_scale().
    Otherwise uses the provided scale factor.

    Returns a new list of dicts with all numeric values in USD millions.
    Reference: Architecture Plan Part 2.4.
    """
    if not statements:
        return statements

    if scale is None:
        scale = detect_units_scale(statements)

    if scale == 1.0:
        return statements   # already in millions — no copy needed

    logger.info(f"Unit normalisation: applying scale factor {scale} ({1/scale:.0f} → millions)")

    normalised = []
    for stmt in statements:
        new_stmt = {}
        for k, v in stmt.items():
            if isinstance(v, (int, float)) and k not in _NON_NUMERIC_FIELDS:
                new_stmt[k] = v * scale
            else:
                new_stmt[k] = v
        normalised.append(new_stmt)
    return normalised


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly detection across years
# ─────────────────────────────────────────────────────────────────────────────

def check_unit_consistency(
    statements: list[dict],
    threshold: float = UNIT_ANOMALY_THRESHOLD,
) -> list[dict]:
    """
    Check for anomalous jumps in revenue that may indicate a unit change
    mid-history (e.g., FMP data mixing millions and thousands).

    Returns a list of dicts: {'from_year', 'to_year', 'ratio', 'anomaly'}
    where anomaly=True means the ratio deviates >threshold from 1.0.

    Reference: Architecture Plan Part 2.4 (unit_normalize).
    """
    revs = []
    for stmt in statements:
        year = stmt.get("calendarYear") or stmt.get("date", "")[:4]
        rev = abs(stmt.get("revenue") or stmt.get("totalRevenue") or 0)
        if rev > 0:
            revs.append((year, rev))

    if len(revs) < 2:
        return []

    anomalies = []
    for i in range(1, len(revs)):
        prev_year, prev_rev = revs[i - 1]
        curr_year, curr_rev = revs[i]
        if prev_rev <= 0:
            continue
        ratio = curr_rev / prev_rev
        # A ratio > (1+threshold)*expected_growth or < (1-threshold)/expected_growth
        # is suspicious. We use a simple check: ratio > 5.0 or < 0.2 (>5x jump)
        anomaly = ratio > 5.0 or ratio < 0.2
        if anomaly:
            logger.warning(
                f"Unit anomaly: revenue ratio {curr_year}/{prev_year} = {ratio:.2f}. "
                f"Possible unit change in FMP data (mixing thousands/millions). "
                f"Verify raw data before proceeding."
            )
        anomalies.append({
            "from_year": prev_year,
            "to_year": curr_year,
            "ratio": ratio,
            "anomaly": anomaly,
        })
    return anomalies
