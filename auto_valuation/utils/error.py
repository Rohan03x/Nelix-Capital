"""
utils/error.py — Error recovery helpers and custom exception types.

Reference: Architecture Plan Part 39.3.
"""
from __future__ import annotations
from typing import Any


# ── Custom exceptions ──────────────────────────────────────────────────────────

class ValuationError(Exception):
    """Base class for all valuation system errors. Always fatal — stops the run."""
    exit_code: int = 1


class DataFetchError(ValuationError):
    """API call failed and no fallback is available."""
    exit_code = 2


class DataQualityError(ValuationError):
    """Data passed validation but is so unreliable the model cannot run."""
    exit_code = 3


class UnsupportedCompanyError(ValuationError):
    """Company type is not supported (Financials, Mining, etc.)."""
    exit_code = 4


class ConfigError(ValuationError):
    """Invalid or missing configuration."""
    exit_code = 5


# ── Soft warnings (non-fatal) ──────────────────────────────────────────────────

class ValuationWarning(UserWarning):
    """Raised (via warnings.warn) for non-fatal data quality issues."""


# ── Recovery helpers ───────────────────────────────────────────────────────────

def safe_divide(numerator: float, denominator: float, fallback: float = 0.0) -> float:
    """Return numerator / denominator; return fallback if denominator is 0 or None."""
    if denominator is None or denominator == 0:
        return fallback
    return numerator / denominator


def coerce_positive(value: float | None, fallback: float, label: str = "") -> float:
    """
    Return value if it is a positive finite number.
    Return fallback and note in error log otherwise.
    Does NOT raise — caller decides if missing data is fatal.
    """
    import math
    if value is None or not math.isfinite(value) or value <= 0:
        return fallback
    return value


def require_field(data: dict, field: str, label: str = "") -> object:
    """
    Return data[field].  Raise DataQualityError if field is missing or None.
    Used at model boundaries where a missing value cannot be recovered.
    """
    val = data.get(field)
    if val is None:
        context = f" ({label})" if label else ""
        raise DataQualityError(f"Required field '{field}' is None or missing{context}.")
    return val


def error_recovery(
    exc: Exception,
    context: str = "",
    fallback: object = None,
    logger: "Any | None" = None,
) -> object:
    """
    Handle a partial API or data failure gracefully.

    Logs the error with optional ``context`` description and returns
    ``fallback`` so the caller can continue with cached/default data.

    Reference: Architecture Plan Part 39.3.
    """
    import logging as _logging

    _log = logger or _logging.getLogger(__name__)
    ctx  = f" [{context}]" if context else ""
    _log.warning("error_recovery%s: %s — using fallback %r", ctx, exc, fallback)
    return fallback
