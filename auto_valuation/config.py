"""
config.py — Global constants and 4-layer configuration hierarchy.

Layer 1 (lowest priority): Global defaults defined here.
Layer 2: Sector defaults from SECTOR_DEFAULTS.
Layer 3: Per-ticker override file  overrides/{TICKER}.json
Layer 4 (highest priority): CLI arguments passed into load_config().

Reference: Architecture Plan Parts 33.1, 66.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ── Auto-load .env file if present (Part 39.2) ────────────────────────────────
# Priority: system environment variables > .env file in project root.
# The .env file is NEVER committed (add to .gitignore). .env.example is the template.
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    _load_dotenv(dotenv_path=_env_path, override=False)   # don't override existing env vars
except ImportError:
    pass   # python-dotenv not installed; rely on system environment variables

# ── Directory layout ───────────────────────────────────────────────────────────
ROOT_DIR       = Path(__file__).resolve().parent.parent
OUTPUT_DIR     = ROOT_DIR / "output"
LOGS_DIR       = ROOT_DIR / "logs"
OVERRIDES_DIR  = ROOT_DIR / "overrides"
CACHE_DIR      = ROOT_DIR / ".damodaran_cache"

# ── Forecast horizon ──────────────────────────────────────────────────────────
FORECAST_YEARS: int = 7   # 7-year forecast (NIKE convention, v3.0 C.1)

# ── DCF mechanics (Part 3) ────────────────────────────────────────────────────
MID_YEAR_CONVENTION: bool  = True     # mid-year discounting (t - 0.5 exponent)
USE_XNPV:            bool  = False    # exact-date XNPV (requires fiscal year-end date)

# ── Discount rate bounds (Part 7, 33.1) ───────────────────────────────────────
WACC_WARN_LOW:  float = 0.06   # warn below 6%
WACC_WARN_HIGH: float = 0.15   # warn above 15%
WACC_HARD_MIN:  float = 0.03   # halt: WACC cannot be < 3%
WACC_HARD_MAX:  float = 0.30   # halt: WACC cannot be > 30%

# ── CAPM components (Parts 4.3, 38) ───────────────────────────────────────────
ERP_DEFAULT:           float = 0.055   # implied ERP; updated annually from Damodaran
RF_FRED_SERIES:        str   = "GS10"  # FRED 10-year constant-maturity Treasury
RF_DEFAULT_FALLBACK:   float = 0.045   # used if FRED call fails
SIZE_PREMIUM_DEFAULT:  float = 0.00    # Duff & Phelps / Kroll lookup; 0 = large-cap
BLUME_ADJUSTMENT:      bool  = True    # B_adj = 0.67×B_raw + 0.33×1.0

# ── Terminal value defaults (Parts 3.3, 52) ────────────────────────────────────
TERMINAL_GROWTH_DEFAULT:  float = 0.025   # 2.5% default long-run growth
TERMINAL_GROWTH_GDP_CAP:  float = 0.040   # hard cap (≈ nominal US GDP growth)
TERMINAL_GROWTH_FLOOR:    float = 0.005   # floors at 0.5% (not negative)
TV_PCT_EV_WARN_THRESHOLD: float = 0.80    # warn if PV(TV) > 80% of total EV
EXIT_MULTIPLE_DEFAULT:    float = 10.0    # EV/EBITDA exit multiple (Gordon Growth is primary)

# ── Tax rate (Parts 43.1, 33.1) ───────────────────────────────────────────────
TAX_RATE_DEFAULT:    float = 0.21   # US statutory corporate rate
TAX_RATE_MIN:        float = 0.05   # floor for normalised rate
TAX_RATE_CAP:        float = 0.35   # cap for normalised rate (don't exceed statutory)
TAX_RATE_LOOKBACK:   int   = 3      # average over N years

# ── Capital expenditure (Parts 51.2, 32.3) ────────────────────────────────────
CAPEX_MAINTENANCE_PCT_PPE: float = 0.03   # maintenance capex as % of opening PP&E
CAPEX_CONVERGENCE_YEARS:   int   = 5      # years until capex converges to D&A

# ── Working capital (Parts 4.2, 32.2) ─────────────────────────────────────────
WC_DAYS_LOOKBACK_YEARS: int = 3   # average WC days over N years

# ── EBIT / revenue fade (Parts 51.1, A.4) ─────────────────────────────────────
EBIT_MARGIN_FADE_YEARS:    int = 5   # fade EBIT margin toward sector median
REVENUE_GROWTH_FADE_YEARS: int = 5   # fade revenue growth toward long-run rate

# ── Share count / dilution (Parts 3.6, 44) ────────────────────────────────────
DILUTED_SHARES_USE_TSM: bool = True   # treasury stock method for options

# ── Materiality thresholds (Parts 67.1, 68.4) ─────────────────────────────────
NCI_MATERIALITY_THRESHOLD:     float = 0.05   # NCI > 5% of NOPAT triggers adjustment
PENSION_MATERIALITY_THRESHOLD: float = 0.03   # pension > 3% of EBIT triggers module
RD_CAP_SECTORS: list[str] = ["Technology", "Health Care"]   # capitalise R&D by default

# ── Price / data freshness (Part 55.1) ────────────────────────────────────────
PRICE_STALENESS_DAYS: int = 3   # warn if price data older than N trading days

# ── API / rate limiting (Part 8.4) ────────────────────────────────────────────
FMP_BASE_URL:       str   = "https://financialmodelingprep.com/api"
API_RATE_LIMIT_SLEEP: float = 0.25   # seconds between FMP requests
MAX_RETRIES:        int   = 3
RETRY_BACKOFF:      float = 1.5      # exponential backoff multiplier

# ── Comps outlier exclusion (Part 21.2, 77.4) ─────────────────────────────────
COMPS_OUTLIER_IQR_MULTIPLE: float = 3.0   # exclude if > N×IQR from median
COMPS_MIN_PEERS:            int   = 3     # minimum peers required for stats
COMPS_MAX_PEERS:            int   = 15    # maximum peers in screen
COMPS_MCAP_MIN_FRACTION:    float = 0.20  # peer mcap >= 20% of subject mcap
COMPS_MCAP_MAX_FRACTION:    float = 5.0   # peer mcap <= 5× subject mcap
COMPS_EXCLUDE_PROFORMA_FLAGGED: bool = True     # exclude proforma/restated peers by default
COMPS_PROFORMA_LOOKBACK_DAYS:   int  = 365      # flag deals/restatements within this window

# ── Circular reference solver (IBD / revolver, Part 4.1) ──────────────────────
CIRCULAR_REF_MAX_ITER: int   = 50      # maximum IBD convergence iterations
CIRCULAR_REF_TOL:      float = 0.001   # convergence tolerance in USD millions

# ── Balance sheet amortisation (Part 53.3) ────────────────────────────────────
INTANGIBLES_AMORT_YEARS_DEFAULT: int = 10   # straight-line amortisation for acquired intangibles

# ── Risk-free rate series by currency (Part 38, FRED tickers) ─────────────────
FRED_RF_SERIES: dict[str, str] = {
    "USD": "GS10",    # US 10-year constant maturity Treasury
    "EUR": "IRLTLT01EZM156N",  # ECB 10-year Eurozone govt bond yield (FRED)
    "GBP": "IRLTLT01GBM156N",  # UK 10-year gilt yield (FRED)
    "CAD": "IRLTLT01CAM156N",  # Canada 10-year govt bond (FRED)
    "AUD": "IRLTLT01AUM156N",  # Australia 10-year govt bond (FRED)
    "JPY": "IRLTLT01JPM156N",  # Japan 10-year JGB (FRED)
    "CHF": "IRLTLT01CHM156N",  # Switzerland 10-year (FRED)
    "default": "GS10",          # fallback for all other currencies
}

# ── Net debt bridge defaults (Parts 55, 69, 70) ───────────────────────────────
NET_DEBT_DEFAULTS: dict[str, bool] = {
    "add_pension":          True,   # add unfunded pension to net debt
    "add_finance_leases":   True,   # add finance lease obligations
    "add_preferred":        True,   # add preferred equity (at liquidation value)
    "add_nci":              True,   # deduct NCI from equity value
    "exclude_restricted_cash": True, # restricted cash excluded from cash offset
}

# ── Sector gating (Parts 61, 62) ──────────────────────────────────────────────
FINANCIAL_SECTORS:    frozenset[str] = frozenset({"Financials", "Financial Services"})
MINING_SECTORS:       frozenset[str] = frozenset({"Energy", "Materials"})
REAL_ESTATE_SECTORS:  frozenset[str] = frozenset({"Real Estate"})

# ── Output / reporting (Parts 63, 49) ─────────────────────────────────────────
OUTPUT_FILENAME_TEMPLATE: str = "{ticker}_{date}_v{version}.xlsx"
OUTPUT_VERSION:           str = "1.0"

# ─────────────────────────────────────────────────────────────────────────────
# SECTOR DEFAULTS  (Layer 2)
# Keys mirror ValuationConfig fields. Keyed by GICS sector name (from FMP profile).
# ─────────────────────────────────────────────────────────────────────────────

SECTOR_DEFAULTS: dict[str, dict[str, Any]] = {
    "Information Technology": {
        "ebit_margin_fade_years": 7,
        "revenue_growth_fade_years": 7,
        "rd_capitalise": True,
        "capex_maintenance_pct_ppe": 0.02,
    },
    "Health Care": {
        "ebit_margin_fade_years": 7,
        "revenue_growth_fade_years": 7,
        "rd_capitalise": True,
        "tax_rate_cap": 0.25,
    },
    "Consumer Discretionary": {
        "ebitdar_adjustment": True,   # lease-heavy — use EV/EBITDAR
        "capex_maintenance_pct_ppe": 0.04,
    },
    "Industrials": {
        "capex_maintenance_pct_ppe": 0.04,
        "wc_days_lookback_years": 5,
    },
    "Energy": {
        "terminal_growth_default": 0.015,
        "capex_maintenance_pct_ppe": 0.05,
    },
    "Real Estate": {
        "use_reit_model": True,        # FFO/AFFO instead of UFCF
    },
    "Financials": {
        "financial_company_gate": True  # halt UFCF; output error message
    },
    "Communication Services": {
        "ebit_margin_fade_years": 6,
    },
    "Utilities": {
        "terminal_growth_default": 0.020,
        "wacc_warn_low": 0.05,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL_DEFAULTS  — layer-4 fallbacks as a plain dict (Part 66)
# Useful for tools that consume raw dicts rather than ValuationConfig dataclass.
# ─────────────────────────────────────────────────────────────────────────────

GLOBAL_DEFAULTS: dict[str, Any] = {
    "forecast_years":         FORECAST_YEARS,
    "terminal_g":             TERMINAL_GROWTH_DEFAULT,
    "mid_year_convention":    MID_YEAR_CONVENTION,
    "capex_override":         None,
    "ebit_margin_override":   None,
    "beta_override":          None,
    "wacc_override":          None,
    "net_debt_flags": {
        "add_pension":   True,
        "add_leases":    True,
        "add_preferred": True,
        "add_nci":       True,
    },
    "capitalize_rd":          False,
    "normalization_years":    5,
}


LEARNING_CONFIG: dict[str, Any] = {
    "learning_enabled": True,
    "learning_observation_limit": 1000,
    "learning_candidate_pool_limit": 12000,
    "symbol_universe_enabled": True,
    "symbol_universe_priority_limit": 500,
    "symbol_universe_recent_days": 21,
    "symbol_universe_bootstrap_interval_hours": 18,
    "background_runner_enabled": True,
    "background_runner_loop_seconds": 30,
    "background_runner_bootstrap_interval_hours": 0,
    "background_runner_bootstrap_max_tickers": 1000,
    "background_runner_maintenance_max_tickers": 64,
    "background_runner_seed_target_symbols": 20000,
    "background_runner_seed_prefix_per_cycle": 500,
    "background_runner_seed_pool_limit": 8000,
    "background_runner_exchange_refresh_batch": 10,
    "background_runner_exchange_refresh_per_exchange_limit": 500,
    "background_runner_exchange_cache_ttl_sec": 86400,
    "background_runner_concurrent_workers": 16,
    "bulk_seed_on_startup": False,
    "bulk_seed_daily_budget": 95000,
    "background_runner_seed_exchanges": [
        "US",
        "LSE",
        "TO",
        "ASX",
        "PA",
        "DE",
        "DU",
        "MI",
        "MC",
        "SW",
        "ST",
        "HE",
        "OL",
        "CO",
        "NSE",
        "BSE",
        "TSE",
        "HK",
        "KO",
        "SI",
    ],
    "auto_bootstrap_current_ticker": True,
    "auto_bootstrap_replay_predictions_per_ticker": 5,
    "min_calibration_observations": 5,
    "online_research_enabled": True,
    "research_cache_ttl_days": 7,
    "max_research_queries_per_run": 12,
    "min_source_credibility": 0.3,
    "min_analog_similarity": 0.75,
    "max_analogs_returned": 10,
    "relationship_graph_max_neighbors": 10,
    "cross_sector_only": True,
    "monte_carlo_enabled": True,
    "monte_carlo_samples": 1000,
    "monte_carlo_seed": 42,
    "market_residual_overlay_enabled": True,
    "market_residual_sample_limit": 4000,
    "historical_replay_limit": 4000,
    "annual_postmortem_enabled": True,
    "quinquennial_postmortem_enabled": True,
    "scheduled_postmortem_enabled": True,
    "scheduled_postmortem_interval_hours": 0,
    "scheduled_postmortem_max_tickers_per_run": 6,
    "bootstrap_default_max_tickers_per_run": 36,
    "bootstrap_cached_ticker_limit": 500,
    "strict_horizon_alignment": True,
    "allow_partial_realized_labels": True,
    "maintenance_store_run_history": True,
    "realized_actuals_source_name": "eodhd_fundamentals",
    "postmortem_min_data_quality_score": 0.6,
    "base_revenue_uncertainty": 0.06,
    "base_margin_uncertainty": 0.025,
    "base_wacc_uncertainty": 0.01,
    "uncertainty_growth_per_year": 0.08,
    "pair_relationship_half_life_days": 45,
    "pair_relationship_decay_floor": 0.2,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge *override* into *base*.  Override wins on key conflicts.
    Nested dicts are merged recursively rather than replaced wholesale.

    Reference: Architecture Plan Part 66.
    """
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ValuationConfig  —  the single config object passed around the system
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValuationConfig:
    # ── Identity ──────────────────────────────────────────────────────────────
    ticker:   str   = ""
    exchange: str   = ""          # e.g. "NYSE", "NASDAQ" (optional)
    currency: str   = "USD"       # reporting currency for model output

    # ── Forecast horizon ──────────────────────────────────────────────────────
    forecast_years:  int  = FORECAST_YEARS
    scenario:        str  = "base"   # "base" | "bull" | "bear"

    # ── DCF mechanics ─────────────────────────────────────────────────────────
    mid_year_convention:    bool  = MID_YEAR_CONVENTION
    use_xnpv:               bool  = USE_XNPV
    terminal_growth_default: float = TERMINAL_GROWTH_DEFAULT
    terminal_growth_gdp_cap: float = TERMINAL_GROWTH_GDP_CAP
    terminal_growth_floor:   float = TERMINAL_GROWTH_FLOOR
    exit_multiple_default:   float = EXIT_MULTIPLE_DEFAULT
    tv_pct_ev_warn_threshold: float = TV_PCT_EV_WARN_THRESHOLD

    # ── WACC ──────────────────────────────────────────────────────────────────
    wacc_warn_low:   float = WACC_WARN_LOW
    wacc_warn_high:  float = WACC_WARN_HIGH
    wacc_hard_min:   float = WACC_HARD_MIN
    wacc_hard_max:   float = WACC_HARD_MAX
    erp:             float = ERP_DEFAULT
    rf_default:      float = RF_DEFAULT_FALLBACK
    size_premium:    float = SIZE_PREMIUM_DEFAULT
    crp:             float = 0.0      # country risk premium (non-US)
    blume_adjustment: bool = BLUME_ADJUSTMENT

    # ── Tax ───────────────────────────────────────────────────────────────────
    tax_rate_default:  float = TAX_RATE_DEFAULT
    tax_rate_min:      float = TAX_RATE_MIN
    tax_rate_cap:      float = TAX_RATE_CAP
    tax_rate_lookback: int   = TAX_RATE_LOOKBACK

    # ── CapEx ─────────────────────────────────────────────────────────────────
    capex_maintenance_pct_ppe: float = CAPEX_MAINTENANCE_PCT_PPE
    capex_convergence_years:   int   = CAPEX_CONVERGENCE_YEARS

    # ── Working capital ───────────────────────────────────────────────────────
    wc_days_lookback_years: int = WC_DAYS_LOOKBACK_YEARS

    # ── Fade / convergence ────────────────────────────────────────────────────
    ebit_margin_fade_years:    int = EBIT_MARGIN_FADE_YEARS
    revenue_growth_fade_years: int = REVENUE_GROWTH_FADE_YEARS

    # ── Share count ───────────────────────────────────────────────────────────
    diluted_shares_use_tsm: bool = DILUTED_SHARES_USE_TSM

    # ── Materiality ───────────────────────────────────────────────────────────
    nci_materiality_threshold:     float = NCI_MATERIALITY_THRESHOLD
    pension_materiality_threshold: float = PENSION_MATERIALITY_THRESHOLD

    # ── Feature flags ─────────────────────────────────────────────────────────
    rd_capitalise:          bool = False   # capitalise R&D (tech/pharma only)
    ebitdar_adjustment:     bool = False   # lease-heavy sector
    use_reit_model:         bool = False   # REIT FFO/AFFO
    financial_company_gate: bool = False   # halt for Financials sector

    # ── Output paths ──────────────────────────────────────────────────────────
    output_dir: str = str(OUTPUT_DIR)
    logs_dir:   str = str(LOGS_DIR)


def load_config(
    ticker:        str  = "",
    exchange:      str  = "",
    currency:      str  = "USD",
    sector:        str  = "",
    scenario:      str  = "base",
    cli_overrides: dict[str, Any] | None = None,
) -> ValuationConfig:
    """
    Build a ValuationConfig by merging all 4 layers.

    Layer 1 — global defaults (ValuationConfig defaults above)
    Layer 2 — sector defaults from SECTOR_DEFAULTS
    Layer 3 — per-ticker JSON file: overrides/{TICKER}.json
    Layer 4 — cli_overrides dict (highest priority)
    """
    # Layer 1 — start from global defaults
    cfg = ValuationConfig(ticker=ticker, exchange=exchange, currency=currency, scenario=scenario)

    # Layer 2 — sector overrides
    if sector and sector in SECTOR_DEFAULTS:
        for key, value in SECTOR_DEFAULTS[sector].items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    # Layer 3 — per-ticker JSON override file
    if ticker:
        override_path = OVERRIDES_DIR / f"{ticker.upper()}.json"
        if override_path.exists():
            try:
                with open(override_path, encoding="utf-8") as fh:
                    ticker_overrides: dict = json.load(fh)
                for key, value in ticker_overrides.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except (json.JSONDecodeError, OSError):
                pass   # logged by caller

    # Layer 4 — CLI / runtime overrides
    if cli_overrides:
        for key, value in cli_overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    return cfg


def ensure_directories() -> None:
    """Create output/, logs/, and .damodaran_cache/ if they don't exist."""
    for d in [OUTPUT_DIR, LOGS_DIR, OVERRIDES_DIR, CACHE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_api_keys(require_fmp: bool = True) -> dict[str, str]:
    """
    Load API keys from environment variables (or .env file).

    Required keys
    -------------
    FMP_API_KEY   — Financial Modelling Prep (required if require_fmp=True)

    Optional keys
    -------------
    FRED_API_KEY  — FRED (for risk-free rate fallback)

    Returns a dict mapping key names to their values (empty string if absent).
    Raises DataFetchError if a required key is missing.

    Reference: Architecture Plan Part 33.1.
    """
    import os
    from auto_valuation.utils.error import DataFetchError

    fmp_key  = os.getenv("FMP_API_KEY", "") or ""
    fred_key = os.getenv("FRED_API_KEY", "") or ""

    if require_fmp and not fmp_key:
        raise DataFetchError(
            "FMP_API_KEY is not set. "
            "Add it to your .env file or set the environment variable."
        )

    return {
        "FMP_API_KEY":  fmp_key,
        "FRED_API_KEY": fred_key,
    }
