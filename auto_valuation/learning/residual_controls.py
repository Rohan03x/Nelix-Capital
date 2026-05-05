"""Shared residual bounds for learning calibration and valuation overlays."""

from __future__ import annotations

import math
import statistics
from typing import Iterable


ASSUMPTION_RESIDUAL_BOUNDS: dict[str, tuple[float, float]] = {
    "revenue_growth": (-0.30, 0.30),
    "ebit_margin": (-0.15, 0.15),
    "ufcf_margin": (-0.20, 0.20),
    "reinvestment_rate": (-0.15, 0.15),
    "wacc": (-0.04, 0.04),
    "terminal_growth": (-0.01, 0.01),
    "beta": (-0.75, 0.75),
}

MARKET_VALUATION_RESIDUAL_BOUNDS: tuple[float, float] = (-0.85, 1.50)
MARKET_VALUATION_EXTREME_BOUNDS: tuple[float, float] = (-0.95, 5.00)
MARKET_APPLIED_ADJUSTMENT_BOUNDS: tuple[float, float] = (-0.45, 0.22)


def finite_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def assumption_residual_bounds(assumption_name: str) -> tuple[float, float]:
    base_name = str(assumption_name or "").split("@", 1)[0]
    return ASSUMPTION_RESIDUAL_BOUNDS.get(base_name, (-1.0, 1.0))


def clamp_assumption_residual(assumption_name: str, value: object) -> float | None:
    number = finite_float(value)
    if number is None:
        return None
    low, high = assumption_residual_bounds(assumption_name)
    return clamp(number, low, high)


def bounded_residuals(assumption_name: str, values: Iterable[object]) -> list[float]:
    bounded: list[float] = []
    for value in values:
        residual = clamp_assumption_residual(assumption_name, value)
        if residual is not None:
            bounded.append(residual)
    return bounded


def robust_bounded_mean(assumption_name: str, values: Iterable[object]) -> float:
    clean = sorted(bounded_residuals(assumption_name, values))
    if not clean:
        return 0.0
    if len(clean) <= 2:
        return sum(clean) / len(clean)
    trim = max(1, int(len(clean) * 0.10))
    if len(clean) - (trim * 2) < 2:
        return statistics.median(clean)
    trimmed = clean[trim:-trim]
    return sum(trimmed) / len(trimmed)


def robust_bounded_std(assumption_name: str, values: Iterable[object]) -> float:
    clean = bounded_residuals(assumption_name, values)
    if len(clean) < 2:
        return 0.0
    center = statistics.median(clean)
    deviations = [abs(value - center) for value in clean]
    mad = statistics.median(deviations)
    if mad > 0:
        return 1.4826 * mad
    mean_value = sum(clean) / len(clean)
    variance = sum((value - mean_value) ** 2 for value in clean) / len(clean)
    return math.sqrt(max(variance, 0.0))


def market_residual_is_extreme(value: object) -> bool:
    number = finite_float(value)
    if number is None:
        return True
    low, high = MARKET_VALUATION_EXTREME_BOUNDS
    return number < low or number > high


def clamp_market_residual(value: object) -> float | None:
    number = finite_float(value)
    if number is None:
        return None
    low, high = MARKET_VALUATION_RESIDUAL_BOUNDS
    return clamp(number, low, high)


def clamp_market_applied_adjustment(value: object) -> float:
    number = finite_float(value) or 0.0
    low, high = MARKET_APPLIED_ADJUSTMENT_BOUNDS
    return clamp(number, low, high)
