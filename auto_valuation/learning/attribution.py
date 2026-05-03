"""Error attribution for learning-system postmortems."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .postmortem import PostmortemRecord


class ErrorDriver(Enum):
    REVENUE_SURPRISE = "revenue_surprise"
    MARGIN_SURPRISE = "margin_surprise"
    CAPEX_CYCLE = "capex_cycle"
    MULTIPLE_RERATING = "multiple_rerating"
    MACRO_RATE_SHIFT = "macro_rate_shift"
    STRUCTURAL_DISRUPTION = "structural_disruption"
    ACQUISITION_DILUTION = "acquisition_dilution"
    CURRENCY_IMPACT = "currency_impact"
    ONE_TIME_ITEM = "one_time_item"
    MANAGEMENT_CHANGE = "management_change"
    MACRO_CYCLE = "macro_cycle"
    SECTOR_ROTATION = "sector_rotation"
    MODEL_BIAS = "model_bias"


@dataclass(frozen=True)
class AttributionContribution:
    driver: ErrorDriver
    contribution_pct: float


def _bias_signal_from_history(bias_history: list[float] | None) -> bool:
    if not bias_history or len(bias_history) < 3:
        return False
    trailing = bias_history[-3:]
    return all(abs(value) > 10.0 for value in trailing) and (max(trailing) < 0 or min(trailing) > 0)


def _normalise(scores: dict[ErrorDriver, float]) -> list[tuple[ErrorDriver, float]]:
    filtered = {driver: score for driver, score in scores.items() if score > 0}
    if not filtered:
        return [(ErrorDriver.ONE_TIME_ITEM, 100.0)]
    total = sum(filtered.values())
    ranked = sorted(
        ((driver, (score / total) * 100.0) for driver, score in filtered.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    rounded: list[tuple[ErrorDriver, float]] = []
    running = 0.0
    for index, (driver, value) in enumerate(ranked):
        if index == len(ranked) - 1:
            rounded_value = round(100.0 - running, 2)
        else:
            rounded_value = round(value, 2)
            running += rounded_value
        rounded.append((driver, rounded_value))
    return rounded


def attribute_postmortem(
    postmortem: PostmortemRecord,
    *,
    peer_miss_fraction: float = 0.0,
    bias_history: list[float] | None = None,
) -> list[tuple[ErrorDriver, float]]:
    """Rank the most likely drivers of a postmortem miss and normalise to 100%."""
    scores: dict[ErrorDriver, float] = {}

    revenue_error = abs(postmortem.revenue_error_pct)
    margin_error_pct = abs(postmortem.margin_error_bps) / 100.0
    ev_error = abs(postmortem.ev_error_pct)
    price_error = abs(postmortem.price_return_error_pct)

    if revenue_error > 0:
        scores[ErrorDriver.REVENUE_SURPRISE] = revenue_error * 1.25
    if margin_error_pct > 0:
        scores[ErrorDriver.MARGIN_SURPRISE] = margin_error_pct * 1.10
    if ev_error > 0:
        scores[ErrorDriver.MULTIPLE_RERATING] = ev_error * 0.55

    rate_delta = abs(postmortem.macro_backdrop_at_horizon.get("10y_yield", 0.0) - postmortem.macro_backdrop_at_prediction.get("10y_yield", 0.0))
    if rate_delta > 0.015:
        scores[ErrorDriver.MACRO_RATE_SHIFT] = rate_delta * 10_000

    cpi_delta = abs(postmortem.macro_backdrop_at_horizon.get("cpi_yoy", 0.0) - postmortem.macro_backdrop_at_prediction.get("cpi_yoy", 0.0))
    gdp_delta = abs(postmortem.macro_backdrop_at_horizon.get("gdp_growth", 0.0) - postmortem.macro_backdrop_at_prediction.get("gdp_growth", 0.0))
    if max(cpi_delta, gdp_delta) > 0.02:
        scores[ErrorDriver.MACRO_CYCLE] = (cpi_delta + gdp_delta) * 2_500

    if postmortem.structural_break_detected:
        scores[ErrorDriver.STRUCTURAL_DISRUPTION] = max(revenue_error, ev_error, 25.0)

    for flag in postmortem.surprise_flags:
        flag_lower = flag.lower()
        if "acquisition" in flag_lower or "m&a" in flag_lower:
            scores[ErrorDriver.ACQUISITION_DILUTION] = scores.get(ErrorDriver.ACQUISITION_DILUTION, 0.0) + 25.0
        if "currency" in flag_lower or "fx" in flag_lower:
            scores[ErrorDriver.CURRENCY_IMPACT] = scores.get(ErrorDriver.CURRENCY_IMPACT, 0.0) + 18.0
        if "management" in flag_lower or "ceo" in flag_lower or "cfo" in flag_lower:
            scores[ErrorDriver.MANAGEMENT_CHANGE] = scores.get(ErrorDriver.MANAGEMENT_CHANGE, 0.0) + 16.0
        if "regulatory" in flag_lower or "technology" in flag_lower or "competitor" in flag_lower:
            scores[ErrorDriver.STRUCTURAL_DISRUPTION] = scores.get(ErrorDriver.STRUCTURAL_DISRUPTION, 0.0) + 20.0
        if "one-time" in flag_lower or "non-recurring" in flag_lower:
            scores[ErrorDriver.ONE_TIME_ITEM] = scores.get(ErrorDriver.ONE_TIME_ITEM, 0.0) + 12.0
        if "capex" in flag_lower or "investment" in flag_lower:
            scores[ErrorDriver.CAPEX_CYCLE] = scores.get(ErrorDriver.CAPEX_CYCLE, 0.0) + 14.0

    if peer_miss_fraction >= 0.5:
        scores[ErrorDriver.SECTOR_ROTATION] = peer_miss_fraction * 100.0

    if _bias_signal_from_history(bias_history):
        scores[ErrorDriver.MODEL_BIAS] = max(scores.get(ErrorDriver.MODEL_BIAS, 0.0), 20.0)

    if price_error > 0 and ErrorDriver.MULTIPLE_RERATING in scores:
        scores[ErrorDriver.MULTIPLE_RERATING] += price_error * 0.25

    return _normalise(scores)


def aggregate_attributions(postmortems: list[PostmortemRecord]) -> list[tuple[ErrorDriver, float]]:
    """Aggregate annual attributions into a normalised quinquennial blend."""
    scores: dict[ErrorDriver, float] = {}
    for postmortem in postmortems:
        attributions = postmortem.error_attribution or attribute_postmortem(postmortem)
        for driver, contribution in attributions:
            enum_driver = driver if isinstance(driver, ErrorDriver) else ErrorDriver(driver)
            scores[enum_driver] = scores.get(enum_driver, 0.0) + float(contribution)
    return _normalise(scores)