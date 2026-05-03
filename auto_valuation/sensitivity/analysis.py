"""
sensitivity/analysis.py — Sensitivity tables, tornado chart data, scenario engine,
                           IRR / implied-WACC solver, Monte Carlo simulation.

Reference: Architecture Plan Parts 16, 36, 46, 47, 48, 49, 50, 51, 62, 65.

All monetary values in USD millions. Rates as decimals.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from auto_valuation.forecast.dcf import run_dcf, DCFResult
from auto_valuation.data.bridge import compute_equity_value
from auto_valuation.model.dilution import compute_price_per_share
from auto_valuation.utils.error import safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# WACC × Terminal Growth sensitivity table  (Part 46)
# ─────────────────────────────────────────────────────────────────────────────

def wacc_growth_sensitivity(
    base_dcf_kwargs: dict,
    wacc_range:   list[float] | None = None,
    growth_range: list[float] | None = None,
    net_debt:     float = 0.0,
    shares_mm:    float = 1.0,
) -> dict[str, Any]:
    """
    Re-run DCF across a grid of (WACC, terminal_growth) combinations.

    Returns:
      {
        "wacc_range": [...],
        "growth_range": [...],
        "ev_table":    {(wacc, g): ev},         # enterprise values
        "price_table": {(wacc, g): price},      # per-share prices
      }

    Reference: Part 46.
    """
    if wacc_range is None:
        wacc_range = [round(w, 3) for w in _frange(0.07, 0.13, 0.01)]
    if growth_range is None:
        growth_range = [round(g, 3) for g in _frange(0.015, 0.040, 0.005)]

    ev_table:    dict[tuple, float] = {}
    price_table: dict[tuple, float] = {}

    for wacc in wacc_range:
        for g in growth_range:
            if wacc <= g:
                continue
            kwargs = dict(base_dcf_kwargs)
            kwargs["wacc"]            = wacc
            kwargs["terminal_growth"] = g
            try:
                res = run_dcf(**kwargs)
                ev    = res.enterprise_value
                eq    = ev - net_debt
                price = safe_divide(eq, shares_mm, 0.0)
                ev_table[(round(wacc, 4), round(g, 4))]    = ev
                price_table[(round(wacc, 4), round(g, 4))] = price
            except Exception:
                pass   # invalid combination (WACC ≤ g) — skip

    return {
        "wacc_range":   wacc_range,
        "growth_range": growth_range,
        "ev_table":     ev_table,
        "price_table":  price_table,
    }


def _frange(start: float, stop: float, step: float) -> list[float]:
    """Inclusive float range generator."""
    result = []
    x = start
    while x <= stop + 1e-9:
        result.append(round(x, 6))
        x += step
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Revenue growth × EBIT margin sensitivity  (Part 47)
# ─────────────────────────────────────────────────────────────────────────────

def growth_margin_sensitivity(
    base_dcf_kwargs: dict,
    growth_range:   list[float] | None = None,
    margin_range:   list[float] | None = None,
    net_debt:       float = 0.0,
    shares_mm:      float = 1.0,
) -> dict[str, Any]:
    """
    Re-run DCF across a grid of (near_term_growth, target_ebit_margin).
    Reference: Part 47.
    """
    if growth_range is None:
        growth_range = [round(g, 3) for g in _frange(0.03, 0.15, 0.02)]
    if margin_range is None:
        margin_range = [round(m, 3) for m in _frange(0.08, 0.22, 0.02)]

    ev_table:    dict[tuple, float] = {}
    price_table: dict[tuple, float] = {}

    for g in growth_range:
        for m in margin_range:
            kwargs = dict(base_dcf_kwargs)
            kwargs["near_term_growth"]    = g
            kwargs["target_ebit_margin"]  = m
            try:
                res = run_dcf(**kwargs)
                ev    = res.enterprise_value
                eq    = ev - net_debt
                price = safe_divide(eq, shares_mm, 0.0)
                ev_table[(round(g, 4), round(m, 4))]    = ev
                price_table[(round(g, 4), round(m, 4))] = price
            except Exception:
                pass

    return {
        "growth_range":  growth_range,
        "margin_range":  margin_range,
        "ev_table":      ev_table,
        "price_table":   price_table,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tornado chart — single-variable sensitivity  (Part 48)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TornadoBar:
    variable:    str
    base_ev:     float
    low_ev:      float      # EV at low assumption
    high_ev:     float      # EV at high assumption
    low_delta:   float      # low_ev − base_ev
    high_delta:  float      # high_ev − base_ev
    low_assumption:  Any = None
    high_assumption: Any = None


_TORNADO_VARIABLES = {
    "wacc": {
        "low":  lambda base: base * 0.85,   # −15% relative
        "high": lambda base: base * 1.15,
        "kwarg": "wacc",
    },
    "terminal_growth": {
        "low":  lambda base: max(0.005, base - 0.010),
        "high": lambda base: min(0.050, base + 0.010),
        "kwarg": "terminal_growth",
    },
    "near_term_growth": {
        "low":  lambda base: max(0.0, base - 0.030),
        "high": lambda base: base + 0.030,
        "kwarg": "near_term_growth",
    },
    "target_ebit_margin": {
        "low":  lambda base: max(0.02, base - 0.040),
        "high": lambda base: base + 0.040,
        "kwarg": "target_ebit_margin",
    },
}


def build_tornado_chart(
    base_dcf_kwargs: dict,
    base_ev: float,
    variables: list[str] | None = None,
) -> list[TornadoBar]:
    """
    Compute ±1 standard shock for each key variable and record the EV impact.
    Results are sorted descending by |high_delta − low_delta| (widest bar first).

    Reference: Part 48.
    """
    if variables is None:
        variables = list(_TORNADO_VARIABLES.keys())

    bars: list[TornadoBar] = []

    for var in variables:
        spec = _TORNADO_VARIABLES.get(var)
        if spec is None:
            continue
        kwarg   = spec["kwarg"]
        base_val = base_dcf_kwargs.get(kwarg)
        if base_val is None:
            continue

        low_val  = spec["low"](base_val)
        high_val = spec["high"](base_val)

        try:
            kw_low  = {**base_dcf_kwargs, kwarg: low_val}
            kw_high = {**base_dcf_kwargs, kwarg: high_val}
            ev_low  = run_dcf(**kw_low).enterprise_value
            ev_high = run_dcf(**kw_high).enterprise_value
        except Exception:
            continue

        bars.append(TornadoBar(
            variable=var,
            base_ev=base_ev,
            low_ev=ev_low,
            high_ev=ev_high,
            low_delta=ev_low  - base_ev,
            high_delta=ev_high - base_ev,
            low_assumption=low_val,
            high_assumption=high_val,
        ))

    # Sort by total swing (widest bar first)
    bars.sort(key=lambda b: abs(b.high_ev - b.low_ev), reverse=True)
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Scenario engine — bull / base / bear  (Part 49)
# ─────────────────────────────────────────────────────────────────────────────

_SCENARIO_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "bull": {
        "near_term_growth_delta":   +0.03,
        "target_ebit_margin_delta": +0.03,
        "wacc_delta":               -0.005,
        "terminal_growth_delta":    +0.005,
    },
    "base": {
        "near_term_growth_delta":    0.0,
        "target_ebit_margin_delta":  0.0,
        "wacc_delta":                0.0,
        "terminal_growth_delta":     0.0,
    },
    "bear": {
        "near_term_growth_delta":   -0.03,
        "target_ebit_margin_delta": -0.03,
        "wacc_delta":               +0.005,
        "terminal_growth_delta":    -0.005,
    },
}


def run_scenario_analysis(
    base_dcf_kwargs: dict,
    scenarios: list[str] | None = None,
    custom_scenario_overrides: dict[str, dict] | None = None,
) -> dict[str, DCFResult]:
    """
    Run DCF for bull / base / bear scenarios.

    custom_scenario_overrides: optional dict {scenario_name: {kwarg_overrides}}
    Returns {scenario_name: DCFResult}.

    Reference: Part 49.
    """
    if scenarios is None:
        scenarios = ["bull", "base", "bear"]

    results: dict[str, DCFResult] = {}

    for scenario in scenarios:
        kwargs = dict(base_dcf_kwargs)
        kwargs["scenario"] = scenario

        # Apply standard deltas
        adj = _SCENARIO_ADJUSTMENTS.get(scenario, {})
        for delta_key, delta_val in adj.items():
            base_param = delta_key.replace("_delta", "")
            if base_param in kwargs:
                kwargs[base_param] = kwargs[base_param] + delta_val

        # Apply custom overrides (scenario-specific from config or CLI)
        if custom_scenario_overrides and scenario in custom_scenario_overrides:
            for k, v in custom_scenario_overrides[scenario].items():
                kwargs[k] = v

        try:
            results[scenario] = run_dcf(**kwargs)
        except Exception as e:
            # Record failure gracefully
            failed = DCFResult(ticker=kwargs.get("ticker", ""), scenario=scenario)
            failed.warnings = [f"Scenario '{scenario}' failed: {e}"]
            results[scenario] = failed

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Summary helpers
# ─────────────────────────────────────────────────────────────────────────────

def scenario_summary_table(
    scenario_results: dict[str, DCFResult],
    net_debt:  float = 0.0,
    shares_mm: float = 1.0,
) -> list[dict]:
    """
    Produce a list of summary rows (one per scenario) with EV, equity value, price.
    Reference: Part 49, 50.
    """
    rows: list[dict] = []
    for scenario, res in scenario_results.items():
        eq    = res.enterprise_value - net_debt
        price = safe_divide(eq, shares_mm, 0.0)
        rows.append({
            "scenario":          scenario,
            "enterprise_value":  res.enterprise_value,
            "equity_value":      eq,
            "price_per_share":   price,
            "pv_ufcfs":          res.pv_ufcfs,
            "pv_terminal_value": res.pv_terminal_value,
            "tv_pct_of_ev":      res.tv_pct_of_ev,
            "wacc":              res.wacc,
            "terminal_growth":   res.terminal_growth,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Market-implied WACC solver  (Part 16.3)
# ─────────────────────────────────────────────────────────────────────────────

def compute_irr_implied_wacc(
    base_dcf_kwargs: dict,
    target_ev: float,
    wacc_low: float  = 0.03,
    wacc_high: float = 0.40,
    tolerance: float = 1e-6,
    max_iter:  int   = 100,
) -> float | None:
    """
    Solve for the WACC that makes DCF enterprise value equal to *target_ev*.

    This is the market-implied cost of capital: the discount rate at which the
    DCF model prices the company in line with its current market valuation.

    Method: Bisection search over [wacc_low, wacc_high].
    Falls back to ``None`` if no root is found (e.g. target_ev is unreachable).

    Parameters
    ----------
    base_dcf_kwargs : dict  — all kwargs required by run_dcf() except 'wacc'
    target_ev       : float — target enterprise value (EV = market_cap + net_debt),
                              typically computed as current_price × diluted_shares + net_debt
    wacc_low        : float — lower bound of search range (default 3%)
    wacc_high       : float — upper bound of search range (default 40%)
    tolerance       : float — convergence criterion on |EV_implied − target_ev|
    max_iter        : int   — maximum bisection iterations

    Returns
    -------
    Implied WACC as a decimal (e.g. 0.092 = 9.2%), or None if unsolvable.

    Reference: Architecture Plan Part 16.3.
    """
    def _ev_at_wacc(w: float) -> float:
        kwargs = dict(base_dcf_kwargs, wacc=w)
        try:
            return run_dcf(**kwargs).enterprise_value
        except Exception:
            return float("nan")

    ev_low  = _ev_at_wacc(wacc_low)
    ev_high = _ev_at_wacc(wacc_high)

    # Sanity: EV is monotonically decreasing in WACC.
    # If target is outside [ev_high, ev_low], no solution in range.
    if math.isnan(ev_low) or math.isnan(ev_high):
        return None
    if not (ev_high <= target_ev <= ev_low):
        return None

    lo, hi = wacc_low, wacc_high
    for _ in range(max_iter):
        mid     = (lo + hi) / 2.0
        ev_mid  = _ev_at_wacc(mid)
        if math.isnan(ev_mid):
            return None
        if abs(ev_mid - target_ev) < tolerance:
            return round(mid, 8)
        if ev_mid > target_ev:
            lo = mid   # need higher WACC to push EV down
        else:
            hi = mid   # need lower WACC to push EV up

    return round((lo + hi) / 2.0, 8)


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo DCF simulation  (Part 65)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonteCarloResult:
    """Distribution statistics from a Monte Carlo DCF run."""
    n_simulations:    int

    # Enterprise value distribution (USD mm)
    ev_mean:   float
    ev_median: float
    ev_std:    float
    ev_p10:    float
    ev_p25:    float
    ev_p75:    float
    ev_p90:    float
    ev_min:    float
    ev_max:    float

    # Per-share price distribution (if net_debt / shares provided)
    price_mean:   float | None = None
    price_median: float | None = None
    price_std:    float | None = None
    price_p10:    float | None = None
    price_p25:    float | None = None
    price_p75:    float | None = None
    price_p90:    float | None = None

    # Raw samples (capped at 10,000 for memory)
    ev_samples:    list[float] = field(default_factory=list)
    price_samples: list[float] = field(default_factory=list)


def _percentile_list(data: list[float], pct: float) -> float:
    """Return the p-th percentile of a sorted list."""
    s = sorted(data)
    n = len(s)
    if n == 0:
        return 0.0
    idx  = (n - 1) * pct / 100.0
    lo   = int(idx)
    hi   = lo + 1
    if hi >= n:
        return s[-1]
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def run_monte_carlo_dcf(
    base_dcf_kwargs: dict,
    n_simulations:    int   = 10_000,
    wacc_std:         float = 0.010,    # ±1% standard deviation on WACC
    growth_std:       float = 0.015,    # ±1.5% std on near_term_growth
    margin_std:       float = 0.020,    # ±2% std on target_ebit_margin
    terminal_g_std:   float = 0.005,    # ±0.5% std on terminal_growth
    net_debt:         float = 0.0,
    shares_mm:        float = 1.0,
    seed:             int | None = None,
) -> MonteCarloResult:
    """
    Run a Monte Carlo simulation of the DCF model by sampling key assumptions
    from normal distributions centred on the base-case values.

    Sampled assumptions (independent draws):
      • wacc           ~ N(base_wacc,           wacc_std²)
      • near_term_growth ~ N(base_near_term,    growth_std²)
      • target_ebit_margin ~ N(base_margin,     margin_std²)
      • terminal_growth  ~ N(base_terminal_g,   terminal_g_std²)

    Hard floors / ceilings applied to each draw:
      • wacc            : [0.04, 0.35]
      • near_term_growth: [−0.20, 0.50]
      • target_ebit_margin: [0.01, 0.60]
      • terminal_growth : [0.00, 0.05]   (cannot exceed GDP ceiling)

    Parameters
    ----------
    base_dcf_kwargs : dict  — base-case kwargs for run_dcf()
    n_simulations   : int   — number of simulation trials (default 10,000)
    wacc_std        : float — std dev for WACC draws
    growth_std      : float — std dev for near_term_growth draws
    margin_std      : float — std dev for target_ebit_margin draws
    terminal_g_std  : float — std dev for terminal_growth draws
    net_debt        : float — net debt (USD mm) for equity bridge
    shares_mm       : float — diluted shares (mm) for per-share conversion
    seed            : int   — optional random seed for reproducibility

    Returns
    -------
    MonteCarloResult with distribution statistics.

    Reference: Architecture Plan Part 65.
    """
    rng = random.Random(seed)

    base_wacc    = base_dcf_kwargs.get("wacc",               0.10)
    base_growth  = base_dcf_kwargs.get("near_term_growth",   0.05)
    base_margin  = base_dcf_kwargs.get("target_ebit_margin", 0.15)
    base_term_g  = base_dcf_kwargs.get("terminal_growth",    0.025)

    ev_samples:    list[float] = []
    price_samples: list[float] = []

    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _gauss(mu: float, sigma: float) -> float:
        # Box-Muller via random.gauss
        return rng.gauss(mu, sigma)

    for _ in range(n_simulations):
        w  = _clamp(_gauss(base_wacc,   wacc_std),       0.04, 0.35)
        g  = _clamp(_gauss(base_growth, growth_std),    -0.20, 0.50)
        m  = _clamp(_gauss(base_margin, margin_std),     0.01, 0.60)
        tg = _clamp(_gauss(base_term_g, terminal_g_std), 0.00, 0.05)

        # Skip invalid combinations
        if w <= tg:
            tg = w * 0.60   # force terminal_growth < WACC

        kwargs = dict(
            base_dcf_kwargs,
            wacc=w,
            near_term_growth=g,
            target_ebit_margin=m,
            terminal_growth=tg,
        )
        try:
            ev = run_dcf(**kwargs).enterprise_value
            if math.isfinite(ev) and ev > 0:
                ev_samples.append(ev)
                if shares_mm > 0:
                    price_samples.append(max(0.0, (ev - net_debt) / shares_mm))
        except Exception:
            pass  # skip failed draws

    n = len(ev_samples)
    if n == 0:
        # Return empty result — all draws failed
        return MonteCarloResult(
            n_simulations=n_simulations,
            ev_mean=0, ev_median=0, ev_std=0,
            ev_p10=0, ev_p25=0, ev_p75=0, ev_p90=0,
            ev_min=0, ev_max=0,
        )

    ev_mean   = sum(ev_samples) / n
    ev_std    = math.sqrt(sum((x - ev_mean) ** 2 for x in ev_samples) / n)

    mc = MonteCarloResult(
        n_simulations = n_simulations,
        ev_mean       = ev_mean,
        ev_median     = _percentile_list(ev_samples, 50),
        ev_std        = ev_std,
        ev_p10        = _percentile_list(ev_samples, 10),
        ev_p25        = _percentile_list(ev_samples, 25),
        ev_p75        = _percentile_list(ev_samples, 75),
        ev_p90        = _percentile_list(ev_samples, 90),
        ev_min        = min(ev_samples),
        ev_max        = max(ev_samples),
        ev_samples    = ev_samples,
    )

    if price_samples:
        p_mean = sum(price_samples) / len(price_samples)
        mc.price_mean   = p_mean
        mc.price_median = _percentile_list(price_samples, 50)
        mc.price_std    = math.sqrt(
            sum((x - p_mean) ** 2 for x in price_samples) / len(price_samples)
        )
        mc.price_p10    = _percentile_list(price_samples, 10)
        mc.price_p25    = _percentile_list(price_samples, 25)
        mc.price_p75    = _percentile_list(price_samples, 75)
        mc.price_p90    = _percentile_list(price_samples, 90)
        mc.price_samples = price_samples

    return mc


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → wacc_growth_sensitivity
build_sensitivity_grid = wacc_growth_sensitivity

#: Canonical checklist name → wacc_growth_sensitivity (second alias)
compute_wacc_sensitivity = wacc_growth_sensitivity

#: Canonical checklist name → build_tornado_chart
build_tornado_chart_data = build_tornado_chart
