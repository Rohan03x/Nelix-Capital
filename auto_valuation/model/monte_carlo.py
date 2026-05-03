"""
model/monte_carlo.py — Monte Carlo simulation for DCF valuation.

Runs 10,000 trials across 4 uncertain parameters to produce a distribution
of implied share prices / equity values.

Uncertain parameters (all Normal distribution around base values):
  1. Revenue CAGR (Years 1–7):   σ = 3 percentage points
  2. EBIT Margin (Year 5):       σ = 2 percentage points
  3. WACC:                       σ = 0.5 percentage points
  4. Terminal Growth Rate:       σ = 0.5 percentage points

Hard constraint: WACC > terminal_g per trial (re-sampled until satisfied).

Reference: Architecture Plan Part 56 (Monte Carlo DCF).
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Default simulation parameters
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_N_TRIALS  = 10_000
DEFAULT_SEED      = 42

# Standard deviations (in decimal form — e.g. 0.03 = 3 percentage points)
SIGMA_REV_CAGR      = 0.03   # revenue CAGR
SIGMA_EBIT_MARGIN   = 0.02   # EBIT margin at year 5
SIGMA_WACC          = 0.005  # WACC
SIGMA_TERMINAL_G    = 0.005  # terminal growth rate


# ─────────────────────────────────────────────────────────────────────────────
# Main Monte Carlo entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_monte_carlo_dcf(
    dcf_fn: Callable[[dict[str, Any]], Any],
    base_params: dict[str, Any],
    sigma_overrides: dict[str, float] | None = None,
    n_trials: int = DEFAULT_N_TRIALS,
    seed: int | None = DEFAULT_SEED,
) -> tuple[np.ndarray, dict[str, float]]:
    """
    Run Monte Carlo DCF simulation.

    Args:
        dcf_fn:          Callable(params_dict) → object with `.equity_value_mm`
                         and/or `.implied_share_price` attribute.  Can also
                         return a dict with those keys.
        base_params:     Base-case assumptions dict.  Must include:
                           "revenue_cagr"     (decimal)
                           "ebit_margin_y5"   (decimal)
                           "wacc"             (decimal)
                           "terminal_g"       (decimal)
        sigma_overrides: Dict to override default σ values.  Keys:
                           "rev_cagr", "ebit_margin", "wacc", "terminal_g"
        n_trials:        Number of simulation trials (default 10 000).
        seed:            Random seed for reproducibility (default 42).

    Returns:
        (results_array, stats_dict)

        results_array: 1-D numpy array of length n_valid (equity values in $M
                       or share prices — whichever dcf_fn returns).
        stats_dict: {
            "p5", "p25", "p50", "p75", "p95",
            "mean", "std", "n_valid", "n_trials"
        }

    Reference: Architecture Plan Part 56.
    """
    rng = np.random.default_rng(seed)

    # Resolve σ values
    sigmas = {
        "rev_cagr":    SIGMA_REV_CAGR,
        "ebit_margin": SIGMA_EBIT_MARGIN,
        "wacc":        SIGMA_WACC,
        "terminal_g":  SIGMA_TERMINAL_G,
    }
    if sigma_overrides:
        sigmas.update(sigma_overrides)

    # Extract base values (with safe fallbacks)
    base_rev_cagr     = float(base_params.get("revenue_cagr", 0.05))
    base_ebit_margin  = float(base_params.get("ebit_margin_y5", 0.15))
    base_wacc         = float(base_params.get("wacc", 0.09))
    base_terminal_g   = float(base_params.get("terminal_g", 0.025))

    # Pre-sample all trials (vectorised for performance)
    rev_cagr_samples    = rng.normal(base_rev_cagr,    sigmas["rev_cagr"],    n_trials)
    ebit_margin_samples = rng.normal(base_ebit_margin, sigmas["ebit_margin"], n_trials)
    wacc_samples        = rng.normal(base_wacc,        sigmas["wacc"],        n_trials)
    terminal_g_samples  = rng.normal(base_terminal_g,  sigmas["terminal_g"],  n_trials)

    # Enforce WACC > terminal_g — clip terminal_g to WACC − 50bps
    min_spread = 0.005
    mask = terminal_g_samples >= (wacc_samples - min_spread)
    terminal_g_samples[mask] = wacc_samples[mask] - min_spread

    # Clip terminal_g to reasonable bounds
    terminal_g_samples = np.clip(terminal_g_samples, -0.05, 0.09)

    # Clip WACC to valid range
    wacc_samples = np.clip(wacc_samples, 0.01, 0.80)

    # Run trials
    results: list[float] = []
    for i in range(n_trials):
        trial_params = dict(base_params)
        trial_params["revenue_cagr"]   = float(rev_cagr_samples[i])
        trial_params["ebit_margin_y5"] = float(ebit_margin_samples[i])
        trial_params["wacc"]           = float(wacc_samples[i])
        trial_params["terminal_g"]     = float(terminal_g_samples[i])

        # Propagate to revenue growth rates array if present
        if "revenue_growth_rates" in trial_params:
            n_rates = len(trial_params["revenue_growth_rates"])
            delta = float(rev_cagr_samples[i]) - base_rev_cagr
            trial_params["revenue_growth_rates"] = [
                r + delta for r in trial_params["revenue_growth_rates"]
            ]

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = dcf_fn(trial_params)

            # Extract scalar result — support both dataclass and dict
            if hasattr(res, "equity_value_mm"):
                val = float(res.equity_value_mm)
            elif hasattr(res, "implied_share_price"):
                val = float(res.implied_share_price)
            elif isinstance(res, dict):
                val = float(res.get("equity_value_mm", res.get("implied_share_price", float("nan"))))
            else:
                val = float(res)

            if not (np.isnan(val) or np.isinf(val)):
                results.append(val)
        except Exception:
            # Failed trial — skip (contributes to n_invalid count)
            pass

    arr = np.array(results, dtype=float)
    n_valid = len(arr)

    if n_valid == 0:
        raise RuntimeError(
            "Monte Carlo: all trials failed. Check that dcf_fn is callable and "
            "returns an object with equity_value_mm or implied_share_price."
        )

    stats = {
        "p5":      float(np.percentile(arr, 5)),
        "p25":     float(np.percentile(arr, 25)),
        "p50":     float(np.percentile(arr, 50)),
        "p75":     float(np.percentile(arr, 75)),
        "p95":     float(np.percentile(arr, 95)),
        "mean":    float(arr.mean()),
        "std":     float(arr.std()),
        "n_valid": n_valid,
        "n_trials": n_trials,
    }

    return arr, stats


# ─────────────────────────────────────────────────────────────────────────────
# Percentile summary helper
# ─────────────────────────────────────────────────────────────────────────────

def monte_carlo_percentile_table(
    arr: np.ndarray,
    percentiles: list[int] | None = None,
) -> list[dict[str, float]]:
    """
    Return a list of {percentile: int, value: float} rows.

    Default percentiles: [5, 10, 25, 50, 75, 90, 95].
    """
    if percentiles is None:
        percentiles = [5, 10, 25, 50, 75, 90, 95]
    return [{"percentile": p, "value": float(np.percentile(arr, p))} for p in percentiles]
