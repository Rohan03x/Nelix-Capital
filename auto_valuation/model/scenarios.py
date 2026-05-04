"""
model/scenarios.py — Bear / Base / Bull scenario framework.

Each scenario applies a signed delta to the base AssumptionSet and re-runs
the full DCF, returning a standardised summary dict.

Reference: Architecture Plan Parts 35, 55.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ─────────────────────────────────────────────────────────────────────────────
# Default scenario delta table  (Part 35)
# ─────────────────────────────────────────────────────────────────────────────

# Each scenario delta is a dict of {assumption_key: additive_delta}.
# Multiplicative fields use "×" prefix convention — handled by apply_scenario().
#
# Units:  growth rates / margins / wacc / terminal_g all in decimal form.
#         near_term_growth:  ±0.02 = ±200bps on top of base near-term growth
#         ebit_margin:       ±0.02 = ±200bps on current/terminal margin
#         wacc:              ±0.005 = ±50bps
#         terminal_g:        ±0.005 = ±50bps

SCENARIO_DELTAS: dict[str, dict[str, float]] = {
    "bull": {
        "near_term_growth":    +0.02,
        "ebit_margin_current": +0.02,
        "ebit_margin_terminal":+0.02,
        "wacc":                -0.005,
        "terminal_g":          +0.005,
    },
    "base": {
        "near_term_growth":     0.0,
        "ebit_margin_current":  0.0,
        "ebit_margin_terminal": 0.0,
        "wacc":                  0.0,
        "terminal_g":            0.0,
    },
    "bear": {
        "near_term_growth":    -0.02,
        "ebit_margin_current": -0.02,
        "ebit_margin_terminal":-0.02,
        "wacc":                +0.005,
        "terminal_g":          -0.005,
    },
}

VALID_SCENARIOS = {"bull", "base", "bear"}


# ─────────────────────────────────────────────────────────────────────────────
# Analyst-derived scenario deltas (M1 — drive scenarios from real consensus)
# ─────────────────────────────────────────────────────────────────────────────

def deltas_from_analyst_estimates(
    revenue_avg_mm: float | None,
    revenue_low_mm: float | None,
    revenue_high_mm: float | None,
    base_revenue_mm: float | None,
    base_near_term_growth: float | None,
    *,
    margin_swing: float = 0.02,
    wacc_swing: float = 0.005,
    terminal_swing: float = 0.005,
) -> dict[str, dict[str, float]] | None:
    """Derive bull/base/bear deltas from analyst Low/Avg/High revenue estimates.

    Returns ``None`` when inputs are insufficient — callers should fall back to
    the static ``SCENARIO_DELTAS`` table. Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (M1).
    """
    if not (revenue_avg_mm and revenue_low_mm and revenue_high_mm and base_revenue_mm and base_revenue_mm > 0):
        return None
    base_g = float(base_near_term_growth) if base_near_term_growth is not None else 0.0
    g_avg = float(revenue_avg_mm) / float(base_revenue_mm) - 1.0
    g_low = float(revenue_low_mm) / float(base_revenue_mm) - 1.0
    g_high = float(revenue_high_mm) / float(base_revenue_mm) - 1.0
    # Center deltas on consensus average so that "base" matches analyst mean.
    return {
        "bull": {
            "near_term_growth":     (g_high - base_g),
            "ebit_margin_current": +margin_swing,
            "ebit_margin_terminal":+margin_swing,
            "wacc":                -wacc_swing,
            "terminal_g":          +terminal_swing,
        },
        "base": {
            "near_term_growth":     (g_avg - base_g),
            "ebit_margin_current":  0.0,
            "ebit_margin_terminal": 0.0,
            "wacc":                  0.0,
            "terminal_g":            0.0,
        },
        "bear": {
            "near_term_growth":     (g_low - base_g),
            "ebit_margin_current": -margin_swing,
            "ebit_margin_terminal":-margin_swing,
            "wacc":                +wacc_swing,
            "terminal_g":          -terminal_swing,
        },
    }



# ─────────────────────────────────────────────────────────────────────────────
# Apply a scenario delta to a base assumptions dict
# ─────────────────────────────────────────────────────────────────────────────

def apply_scenario(
    base_assumptions: dict[str, Any],
    scenario: str,
    custom_deltas: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """
    Apply scenario deltas to a copy of base_assumptions.

    Args:
        base_assumptions: Dict of computed assumptions (from AssumptionsEngine).
        scenario:         "bull", "base", or "bear".
        custom_deltas:    Override SCENARIO_DELTAS if provided.

    Returns:
        A new dict with scenario adjustments applied (base is not mutated).

    Reference: Architecture Plan Part 35.
    """
    deltas_map = custom_deltas if custom_deltas is not None else SCENARIO_DELTAS
    if scenario not in deltas_map:
        raise ValueError(
            f"Unknown scenario '{scenario}'. Valid: {sorted(deltas_map.keys())}."
        )

    import copy
    result = copy.deepcopy(base_assumptions)
    deltas = deltas_map[scenario]

    # Apply additive adjustments
    for key, delta in deltas.items():
        if delta == 0.0:
            continue

        if key in result:
            current = result[key]
            if isinstance(current, (int, float)):
                result[key] = current + delta
            # Lists (e.g. revenue_growth_rates) — add delta to each element
            elif isinstance(current, list):
                result[key] = [v + delta if isinstance(v, (int, float)) else v for v in current]
        # Keys not yet in result get initialised to the delta itself
        else:
            result[key] = delta

    # Enforce physical bounds after application
    _clip_bounds(result)

    return result


def _clip_bounds(d: dict[str, Any]) -> None:
    """Enforce hard lower bounds after scenario shift (in-place)."""
    # WACC must stay ≥ 1% and ≤ 80%
    if "wacc" in d and isinstance(d["wacc"], (int, float)):
        d["wacc"] = max(0.01, min(0.80, d["wacc"]))
    # WACC must exceed terminal growth
    if "wacc" in d and "terminal_g" in d:
        if isinstance(d["wacc"], (int, float)) and isinstance(d["terminal_g"], (int, float)):
            if d["terminal_g"] >= d["wacc"]:
                d["terminal_g"] = d["wacc"] - 0.005
    # Margins must be in [-1, 1]
    for key in ("ebit_margin_current", "ebit_margin_terminal", "ebitda_margin_override"):
        if key in d and isinstance(d[key], (int, float)):
            d[key] = max(-1.0, min(1.0, d[key]))


# ─────────────────────────────────────────────────────────────────────────────
# Run all 3 scenarios
# ─────────────────────────────────────────────────────────────────────────────

def run_all_scenarios(
    base_assumptions: dict[str, Any],
    dcf_fn: Callable[[dict[str, Any]], Any],
    custom_deltas: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """
    Run bear / base / bull scenarios by mutating assumptions and calling dcf_fn.

    Args:
        base_assumptions: Base-case assumptions dict.
        dcf_fn:           Callable(assumptions_dict) → DCFResult (or any result).
        custom_deltas:    Optional custom delta table.

    Returns:
        Dict with keys {"bull", "base", "bear"} each containing the dcf_fn result.

    Reference: Architecture Plan Part 55.
    """
    results: dict[str, Any] = {}
    for scenario in ("bull", "base", "bear"):
        scenario_assum = apply_scenario(base_assumptions, scenario, custom_deltas)
        try:
            results[scenario] = dcf_fn(scenario_assum)
        except Exception as exc:
            results[scenario] = {"error": str(exc)}
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Scenario summary table  (Part 55.2)
# ─────────────────────────────────────────────────────────────────────────────

def scenario_summary_table(
    scenario_results: dict[str, Any],
    equity_value_key: str = "equity_value_mm",
    share_price_key: str = "implied_share_price",
) -> list[dict[str, Any]]:
    """
    Build a list of summary rows for bear / base / bull.

    Each row: {"scenario", equity_value_key value, share_price_key value}.
    Reference: Architecture Plan Part 55.2.
    """
    rows = []
    for label in ("bear", "base", "bull"):
        res = scenario_results.get(label, {})
        if isinstance(res, dict):
            row = {
                "scenario":        label,
                equity_value_key:  res.get(equity_value_key),
                share_price_key:   res.get(share_price_key),
            }
        else:
            # DCFResult dataclass or similar — use getattr
            row = {
                "scenario":        label,
                equity_value_key:  getattr(res, equity_value_key, None),
                share_price_key:   getattr(res, share_price_key, None),
            }
        rows.append(row)
    return rows
