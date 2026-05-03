"""
assumptions/defaults.py — GICS-sector industry defaults.

Provides sector-level median EBIT margins, CapEx intensity, terminal SBC,
and working capital benchmarks used as the fade targets in the forecast
engine when no analyst override is supplied.

Reference: Architecture Plan Parts 51.1, 51.2, A.4, 33.1.

All rates as decimals.  All values represent long-run steady-state medians.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# EBIT margin sector medians — terminal year fade target  (Part 51.1)
# Values represent approximate long-run sector median EBIT margins
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_MEDIAN_EBIT_MARGINS: dict[str, float] = {
    "Information Technology":   0.22,
    "Technology":               0.22,
    "Software":                 0.25,
    "Semiconductors":           0.20,
    "Health Care":              0.16,
    "Pharmaceuticals":          0.18,
    "Biotechnology":            0.10,
    "Consumer Discretionary":   0.10,
    "Consumer Staples":         0.12,
    "Financials":               0.25,   # gated — not used in UFCF DCF
    "Industrials":              0.11,
    "Materials":                0.12,
    "Energy":                   0.12,
    "Utilities":                0.16,
    "Real Estate":              0.30,   # uses FFO model
    "Communication Services":   0.18,
    "Retail":                   0.07,
    "Airlines":                 0.08,
    "Automotive":               0.06,
    "Default":                  0.14,   # global fallback
}


# ─────────────────────────────────────────────────────────────────────────────
# CapEx as % of revenue — long-run steady-state (Part 51.2)
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_CAPEX_PCT: dict[str, float] = {
    "Information Technology":   0.03,
    "Technology":               0.03,
    "Software":                 0.02,
    "Semiconductors":           0.10,
    "Health Care":              0.04,
    "Pharmaceuticals":          0.04,
    "Biotechnology":            0.06,
    "Consumer Discretionary":   0.04,
    "Consumer Staples":         0.04,
    "Financials":               0.02,
    "Industrials":              0.05,
    "Materials":                0.07,
    "Energy":                   0.10,
    "Utilities":                0.15,
    "Real Estate":              0.02,
    "Communication Services":   0.08,
    "Retail":                   0.03,
    "Airlines":                 0.08,
    "Automotive":               0.05,
    "Default":                  0.04,
}


# ─────────────────────────────────────────────────────────────────────────────
# Terminal-year SBC as % of revenue (Part 41.2)
# SBC is non-cash in OCF but represents real dilution cost.
# In the terminal year: CapEx must include SBC drag on UFCF to avoid overstating TV.
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_TERMINAL_SBC_PCT: dict[str, float] = {
    "Information Technology":   0.025,
    "Technology":               0.025,
    "Software":                 0.030,
    "Semiconductors":           0.020,
    "Health Care":              0.015,
    "Pharmaceuticals":          0.010,
    "Biotechnology":            0.020,
    "Consumer Discretionary":   0.008,
    "Consumer Staples":         0.005,
    "Financials":               0.010,
    "Industrials":              0.007,
    "Materials":                0.005,
    "Energy":                   0.005,
    "Utilities":                0.003,
    "Real Estate":              0.005,
    "Communication Services":   0.015,
    "Retail":                   0.005,
    "Airlines":                 0.005,
    "Automotive":               0.006,
    "Default":                  0.010,
}


# ─────────────────────────────────────────────────────────────────────────────
# Net debt defaults — flags for compute_net_debt() (Part 60)
# ─────────────────────────────────────────────────────────────────────────────
NET_DEBT_DEFAULTS: dict[str, bool] = {
    "add_pension":   True,
    "add_leases":    True,
    "add_preferred": True,
    "add_nci":       True,
}


# ─────────────────────────────────────────────────────────────────────────────
# DSO / DIO / DPO sector medians — used when historical data is absent
# Reference: Parts 4.2, 32.2
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_WC_DAYS: dict[str, dict[str, float]] = {
    "Information Technology":  {"dso": 55, "dio": 30, "dpo": 40},
    "Technology":              {"dso": 55, "dio": 30, "dpo": 40},
    "Software":                {"dso": 60, "dio":  0, "dpo": 35},
    "Health Care":             {"dso": 65, "dio": 60, "dpo": 45},
    "Consumer Discretionary":  {"dso": 35, "dio": 60, "dpo": 40},
    "Consumer Staples":        {"dso": 30, "dio": 50, "dpo": 35},
    "Industrials":             {"dso": 55, "dio": 60, "dpo": 45},
    "Materials":               {"dso": 50, "dio": 70, "dpo": 40},
    "Energy":                  {"dso": 40, "dio": 20, "dpo": 35},
    "Utilities":               {"dso": 35, "dio": 15, "dpo": 30},
    "Communication Services":  {"dso": 45, "dio":  5, "dpo": 30},
    "Retail":                  {"dso": 10, "dio": 40, "dpo": 45},
    "Airlines":                {"dso":  5, "dio": 15, "dpo": 30},
    "Default":                 {"dso": 45, "dio": 45, "dpo": 40},
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper: look up a sector default (fuzzy match)
# ─────────────────────────────────────────────────────────────────────────────

def _sector_lookup(table: dict[str, float], sector: str) -> float:
    """Return value from table for sector, using fuzzy matching, else Default."""
    if not sector:
        return table.get("Default", 0.0)
    if sector in table:
        return table[sector]
    # Partial match
    for key, val in table.items():
        if sector.lower() in key.lower() or key.lower() in sector.lower():
            return val
    return table.get("Default", 0.0)


def get_sector_ebit_margin(sector: str) -> float:
    """Return the sector-median EBIT margin fade target."""
    return _sector_lookup(SECTOR_MEDIAN_EBIT_MARGINS, sector)


def get_sector_capex_pct(sector: str) -> float:
    """Return the sector-median CapEx as % of revenue."""
    return _sector_lookup(SECTOR_CAPEX_PCT, sector)


def get_sector_terminal_sbc_pct(sector: str) -> float:
    """Return the sector-median terminal-year SBC as % of revenue."""
    return _sector_lookup(SECTOR_TERMINAL_SBC_PCT, sector)


def get_sector_wc_days(sector: str) -> dict[str, float]:
    """Return the sector-median WC days dict with keys dso, dio, dpo."""
    sector_clean = sector or ""
    if sector_clean in SECTOR_WC_DAYS:
        return dict(SECTOR_WC_DAYS[sector_clean])
    for key, val in SECTOR_WC_DAYS.items():
        if sector_clean.lower() in key.lower() or key.lower() in sector_clean.lower():
            return dict(val)
    return dict(SECTOR_WC_DAYS["Default"])
