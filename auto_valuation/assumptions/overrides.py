"""
assumptions/overrides.py — Load, validate, and apply analyst override files.

Overrides files live at overrides/{TICKER}.json.
The EXAMPLE.json template shows all supported keys.

Config hierarchy (highest → lowest priority):
  1. CLI arguments
  2. overrides/{TICKER}.json
  3. Sector defaults (config.py SECTOR_DEFAULTS)
  4. Global defaults (config.py constants)

Reference: Architecture Plan Parts 31, 66.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_valuation.utils.error import ConfigError


# ─────────────────────────────────────────────────────────────────────────────
# Schema: all keys accepted in an override file (v4)
# ─────────────────────────────────────────────────────────────────────────────

# Allowed top-level keys mapped to their expected Python type (or None = any)
_ALLOWED_KEYS: dict[str, type | tuple | None] = {
    # Revenue
    "revenue_growth_rates":      list,
    "near_term_growth":          float,
    "revenue_bridge":            dict,
    # Margins
    "ebit_margin_override":      float,
    "ebit_margin_terminal":      float,
    "ebitda_margin_override":    float,
    # WACC
    "beta_override":             float,
    "wacc_override":             float,
    "rf_rate_override":          float,
    "erp_override":              float,
    "size_premium":              float,
    "crp":                       float,
    # CapEx
    "capex_override":            float,
    "capitalize_rd":             bool,
    # Terminal value
    "terminal_g":                float,
    "exit_multiple_override":    float,
    # Working capital
    "dso_override":              float,
    "dpo_override":              float,
    "dio_override":              float,
    # Tax
    "tax_rate_override":         float,
    "deferred_tax_change_annual": float,
    # Debt
    "debt_schedule":             dict,
    "debt_to_total_assets":      float,
    # Net debt adjustments
    "net_debt_flags":            dict,
    # Acquisitions
    "annual_acquisitions_m":     list,
    # Misc
    "intangibles_amort_years":   int,
    "normalization_years":       int,
    "forecast_years":            int,
    "scenario":                  str,
    "sbc_terminal_pct":          float,
    "nci_pct":                   float,
    "use_total_beta":            bool,
    "correlation":               float,
    # Pension
    "pension":                   dict,
    # NTM estimates
    "ntm_revenue_mm":            float,
    "ntm_ebitda_mm":             float,
    # Peer / comps
    "peer_tickers":              list,
    "exclude_peers":             list,
    "peer_proforma_adjustments": dict,
    # Precedent transactions
    "precedent_transactions":    list,
    # Lease
    "lease":                     dict,
}

# Numeric range validation: (min, max) inclusive
_RANGE_CHECKS: dict[str, tuple[float, float]] = {
    "near_term_growth":       (-0.50, 2.0),
    "ebit_margin_override":   (-1.0,  1.0),
    "ebit_margin_terminal":   (-1.0,  1.0),
    "beta_override":          (0.0,   5.0),
    "wacc_override":          (0.01,  0.80),
    "rf_rate_override":       (0.0,   0.20),
    "erp_override":           (0.0,   0.20),
    "size_premium":           (0.0,   0.20),
    "crp":                    (0.0,   0.30),
    "capex_override":         (0.0,   1.0),
    "terminal_g":             (-0.05, 0.10),
    "tax_rate_override":      (0.0,   0.70),
    "dso_override":           (0.0, 365.0),
    "dpo_override":           (0.0, 365.0),
    "dio_override":           (0.0, 730.0),
    "debt_to_total_assets":   (0.0,   1.0),
    "nci_pct":                (0.0,   1.0),
    "correlation":            (0.01,  1.0),
    "sbc_terminal_pct":       (0.0,   0.20),
    "intangibles_amort_years": (1,    50),
    "normalization_years":    (2,     10),
    "forecast_years":         (3,     15),
}


# ─────────────────────────────────────────────────────────────────────────────
# Load and validate
# ─────────────────────────────────────────────────────────────────────────────

def load_overrides(
    ticker: str,
    overrides_dir: str | Path = "overrides",
) -> dict[str, Any]:
    """
    Load the override file for `ticker` from overrides/{TICKER}.json.

    Returns an empty dict if the file does not exist.
    Raises ConfigError on JSON parse failure or schema violations.
    """
    path = Path(overrides_dir) / f"{ticker.upper()}.json"
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as fh:
            raw: dict = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in overrides/{ticker.upper()}.json: {exc}"
        ) from exc

    return validate_overrides(raw, ticker)


def validate_overrides(
    overrides: dict[str, Any],
    ticker: str = "",
) -> dict[str, Any]:
    """
    Validate the contents of an override dict.

    - Strips private _comment / _version / _reference / _note keys.
    - Warns about (but does not reject) unrecognised keys.
    - Raises ConfigError for type mismatches and range violations.

    Returns the cleaned, validated dict.
    """
    cleaned: dict[str, Any] = {}

    for key, value in overrides.items():
        # Strip metadata keys
        if key.startswith("_"):
            continue
        # Strip None values (explicit null = "use default")
        if value is None:
            continue

        if key not in _ALLOWED_KEYS:
            # Unknown key — silently skip (forward-compatible)
            continue

        expected_type = _ALLOWED_KEYS[key]
        if expected_type is not None and value is not None:
            if isinstance(expected_type, tuple):
                if not isinstance(value, expected_type):
                    raise ConfigError(
                        f"overrides/{ticker}.json: '{key}' expected "
                        f"{expected_type}, got {type(value).__name__}."
                    )
            elif not isinstance(value, expected_type):
                # Coerce int → float where float is expected
                if expected_type is float and isinstance(value, int):
                    value = float(value)
                elif expected_type is int and isinstance(value, float) and value == int(value):
                    value = int(value)
                else:
                    raise ConfigError(
                        f"overrides/{ticker}.json: '{key}' expected "
                        f"{expected_type.__name__}, got {type(value).__name__}."
                    )

        # Range validation
        if key in _RANGE_CHECKS and isinstance(value, (int, float)):
            lo, hi = _RANGE_CHECKS[key]
            if not (lo <= value <= hi):
                raise ConfigError(
                    f"overrides/{ticker}.json: '{key}' = {value} is outside "
                    f"valid range [{lo}, {hi}]."
                )

        cleaned[key] = value

    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Deep merge helper  (Part 66.1)
# ─────────────────────────────────────────────────────────────────────────────

def deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` into `base`.  Override wins on conflicts.
    For nested dicts, inner keys are merged individually.

    Reference: Architecture Plan Part 66.1.
    """
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Apply overrides to an AssumptionSet / config dict
# ─────────────────────────────────────────────────────────────────────────────

def apply_overrides(
    assumptions_dict: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """
    Apply validated overrides to an assumptions dict.
    Returns the merged dict (does not mutate inputs).
    """
    return deep_merge(assumptions_dict, overrides)
