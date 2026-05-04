"""
forecast/dcf.py — Main DCF engine: UFCF forecast + PV + EV.

Reference: Architecture Plan Parts 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
           16, 17, 20, 21, 25, 27, 30, 37, 39, 41, 45.

All monetary values in USD millions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from auto_valuation.model.income_statement import (
    build_revenue_forecast,
    infer_revenue_lifecycle_stage,
    build_ebit_margin_forecast,
    historical_da_pct,
    normalise_tax_rate,
    compute_nopat,
    compute_ufcf,
    average_sbc_pct_revenue,
)
from auto_valuation.model.balance_sheet import (
    historical_capex_pct,
    build_capex_forecast,
    build_ppe_rollforward,
    compute_invested_capital,
    compute_roic,
)
from auto_valuation.model.working_capital import (
    historical_wc_days,
    historical_cogs_pct,
    build_nowc_forecast,
    compute_nowc_from_bs,
)
from auto_valuation.forecast.terminal_value import (
    compute_reinvestment_rate,
    gordon_growth_tv,
    exit_multiple_tv,
    pv_terminal_value,
)
from auto_valuation.utils.error import safe_divide, ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Forecast row dataclass  (for structured output)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ForecastYear:
    year:         int
    revenue:      float = 0.0
    ebit_margin:  float = 0.0
    ebit:         float = 0.0
    tax_rate:     float = 0.0
    nopat:        float = 0.0
    da:           float = 0.0
    capex:        float = 0.0
    nowc:         float = 0.0
    delta_nowc:   float = 0.0
    ufcf:         float = 0.0
    discount_factor: float = 0.0
    pv_ufcf:      float = 0.0
    confidence_intervals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float]:
        return {
            "year":           self.year,
            "revenue":        self.revenue,
            "ebit_margin":    self.ebit_margin,
            "ebit":           self.ebit,
            "tax_rate":       self.tax_rate,
            "nopat":          self.nopat,
            "da":             self.da,
            "capex":          self.capex,
            "nowc":           self.nowc,
            "delta_nowc":     self.delta_nowc,
            "ufcf":           self.ufcf,
            "discount_factor": self.discount_factor,
            "pv_ufcf":        self.pv_ufcf,
            "confidence_intervals": _serialise_mapping(self.confidence_intervals),
        }


@dataclass
class DCFResult:
    """Full DCF output. Reference: Part 2."""
    ticker:             str
    scenario:           str

    # Forecast schedule
    forecast_years_data: list[ForecastYear] = field(default_factory=list)
    confidence_intervals: dict[str, Any] = field(default_factory=dict)
    model_confidence_score: float = 0.0
    monte_carlo_result: dict[str, float] | None = None

    # Terminal value
    terminal_ufcf:      float = 0.0
    terminal_value_ggm: float = 0.0
    terminal_value_em:  float = 0.0   # exit multiple cross-check
    pv_terminal_value:  float = 0.0
    tv_pct_of_ev:       float = 0.0

    # PV of forecast UFCFs
    pv_ufcfs:           float = 0.0

    # Enterprise value
    enterprise_value:   float = 0.0

    # Key assumptions
    wacc:               float = 0.0
    terminal_growth:    float = 0.0
    tax_rate:           float = 0.0
    forecast_years:     int   = 10

    # Warnings
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker":             self.ticker,
            "scenario":           self.scenario,
            "pv_ufcfs":           self.pv_ufcfs,
            "pv_terminal_value":  self.pv_terminal_value,
            "enterprise_value":   self.enterprise_value,
            "tv_pct_of_ev":       self.tv_pct_of_ev,
            "terminal_value_ggm": self.terminal_value_ggm,
            "terminal_value_em":  self.terminal_value_em,
            "wacc":               self.wacc,
            "terminal_growth":    self.terminal_growth,
            "tax_rate":           self.tax_rate,
            "forecast_years":     self.forecast_years,
            "forecast_schedule":  [y.to_dict() for y in self.forecast_years_data],
            "confidence_intervals": _serialise_mapping(self.confidence_intervals),
            "model_confidence_score": self.model_confidence_score,
            "monte_carlo_result": self.monte_carlo_result,
        }


def _serialise_mapping(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return {
            key: _serialise_mapping(item)
            for key, item in value.__dict__.items()
        }
    if isinstance(value, dict):
        return {key: _serialise_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialise_mapping(item) for item in value]
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Discount factors with mid-year convention  (Part 4)
# ─────────────────────────────────────────────────────────────────────────────

def discount_factors(
    wacc: float,
    forecast_years: int,
    mid_year: bool = True,
) -> list[float]:
    """
    Compute per-year discount factors.
    Mid-year convention: factor_t = 1 / (1 + WACC)^(t − 0.5)
    End-of-year:         factor_t = 1 / (1 + WACC)^t

    Reference: Part 4.
    """
    exponents = [
        (t - 0.5 if mid_year else t)
        for t in range(1, forecast_years + 1)
    ]
    return [1.0 / ((1.0 + wacc) ** e) for e in exponents]


def enforce_terminal_growth_consistency(
    terminal_growth: float,
    terminal_roic: float,
    terminal_reinvestment_rate: float,
    tolerance: float = 0.02,
) -> tuple[float, str | None]:
    """Cap terminal growth when it exceeds ROIC × reinvestment capacity."""
    if terminal_growth <= 0 or terminal_roic <= 0:
        return terminal_growth, None
    implied_growth = terminal_roic * terminal_reinvestment_rate
    max_growth = max(0.0, implied_growth + tolerance)
    if terminal_growth <= max_growth:
        return terminal_growth, None
    return max_growth, (
        f"Terminal growth capped from {terminal_growth:.2%} to {max_growth:.2%}; "
        f"ROIC ({terminal_roic:.2%}) × reinvestment rate "
        f"({terminal_reinvestment_rate:.2%}) implies {implied_growth:.2%}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core DCF engine  (Part 3, 27, 39)
# ─────────────────────────────────────────────────────────────────────────────

def run_dcf(
    ticker: str,
    scenario: str,
    income_stmts: list[dict],
    cash_flows: list[dict],
    balance_sheets: list[dict],
    wacc: float,
    terminal_growth: float,
    near_term_growth: float,
    target_ebit_margin: float,
    forecast_years: int = 7,
    hold_years: int = 3,
    ebit_margin_fade_years: int = 7,
    exit_ev_ebitda_multiple: float | None = None,
    tax_rate_override: float | None = None,
    da_pct_override: float | None = None,
    capex_pct_override: float | None = None,
    sbc_pct_override: float | None = None,
    mid_year_convention: bool = True,
    assumption_set: Any | None = None,
    monte_carlo_enabled: bool | None = None,
    monte_carlo_samples: int | None = None,
    monte_carlo_seed: int | None = None,
) -> DCFResult:
    """
    Run the 7-year UFCF DCF model (NIKE convention, v3.0 C.1).

    Steps:
      1. Derive historical assumptions (tax rate, D&A%, CapEx%, SBC%, WC days)
      2. Build revenue, EBIT margin, D&A, CapEx, SBC, NOWC schedules
      3. Compute per-year UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC  (v4.0 A.1)
      4. Discount UFCFs to PV
      5. Compute terminal value (GGM) using year-N FCF directly as TV base
      6. EV = PV(UFCFs) + PV(TV)

    SBC add-back default = 'addback' (matching NIKE template, v4.0 A.1).
    To expense SBC instead, pass sbc_pct_override=0.0.

    Reference: Parts 2, 3, 4, 27; v3.0 C.1; v4.0 A.1.
    """
    result  = DCFResult(ticker=ticker, scenario=scenario, forecast_years=forecast_years)
    warns:  list[str] = []

    if assumption_set is not None:
        near_term_growth = getattr(assumption_set, "revenue_growth_adj", near_term_growth)
        target_ebit_margin = getattr(assumption_set, "ebit_margin_adj", target_ebit_margin)
        wacc = wacc + float(getattr(assumption_set, "wacc_adj", 0.0) or 0.0)
        terminal_growth = getattr(assumption_set, "terminal_growth_adj", terminal_growth)
        result.confidence_intervals = _serialise_mapping(getattr(assumption_set, "confidence_intervals", {}))
        result.model_confidence_score = float(
            getattr(assumption_set, "model_confidence_score", 0.0)
            or getattr(getattr(assumption_set, "confidence_intervals", None), "overall_score", 0.0)
        )

    # ── 1. Historical assumptions ────────────────────────────────────────────
    tax_rate   = tax_rate_override   or normalise_tax_rate(income_stmts)
    da_pct     = da_pct_override     or historical_da_pct(income_stmts, years=3)
    capex_pct  = capex_pct_override  or historical_capex_pct(income_stmts, cash_flows, years=3)
    cogs_pct   = historical_cogs_pct(income_stmts, years=3)
    wc_days    = historical_wc_days(income_stmts, balance_sheets, years=3)

    # SBC % of revenue (v4.0 A.1 — add back SBC as non-cash charge)
    if sbc_pct_override is not None:
        sbc_pct = sbc_pct_override
    else:
        sbc_pct = average_sbc_pct_revenue(income_stmts, cash_flows, years=3)

    result.tax_rate      = tax_rate
    result.wacc          = wacc
    result.terminal_growth = terminal_growth

    # Latest balance sheet for base NOWC and CapEx seed
    latest_bs = balance_sheets[0] if balance_sheets else {}
    base_revenue = income_stmts[0].get("revenue") or 0 if income_stmts else 0
    base_ebit_margin = (
        (income_stmts[0].get("ebit_normalized")
         or income_stmts[0].get("ebit")
         or income_stmts[0].get("operatingIncome") or 0)
        / base_revenue if base_revenue > 0 else 0.10
    ) if income_stmts else 0.10
    base_nowc  = compute_nowc_from_bs(latest_bs)

    # ── 2. Forecast schedules ────────────────────────────────────────────────
    revenues = build_revenue_forecast(
        base_revenue, near_term_growth, terminal_growth,
        forecast_years, hold_years,
        lifecycle_stage=getattr(assumption_set, "lifecycle_stage", "auto") if assumption_set is not None else "auto",
    )
    ebit_margins = build_ebit_margin_forecast(
        base_ebit_margin, target_ebit_margin, forecast_years, ebit_margin_fade_years
    )
    da_schedule    = [rev * da_pct   for rev in revenues]
    capex_schedule = build_capex_forecast(revenues, capex_pct)
    sbc_schedule   = [rev * sbc_pct  for rev in revenues]
    nowc_schedule, delta_nowc_schedule = build_nowc_forecast(
        revenues, cogs_pct,
        wc_days["dso"], wc_days["dio"], wc_days["dpo"],
        base_nowc,
    )

    # ── 3. Per-year UFCF ────────────────────────────────────────────────────
    factors = discount_factors(wacc, forecast_years, mid_year_convention)
    forecast_rows: list[ForecastYear] = []
    confidence_bundle = getattr(assumption_set, "confidence_intervals", None)
    confidence_module = None
    if assumption_set is not None and confidence_bundle is not None:
        from auto_valuation.learning import confidence as confidence_module

    for i in range(forecast_years):
        rev        = revenues[i]
        ebit_m     = ebit_margins[i]
        ebit       = rev * ebit_m
        da         = da_schedule[i]
        capex      = capex_schedule[i]
        sbc        = sbc_schedule[i]
        d_nowc     = delta_nowc_schedule[i]
        nopat      = compute_nopat(ebit, tax_rate)
        # UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC  (v4.0 A.1)
        ufcf       = compute_ufcf(ebit, tax_rate, da, capex, d_nowc, sbc)
        df         = factors[i]
        pv         = ufcf * df
        confidence_intervals: dict[str, Any] = {}
        if confidence_module is not None:
            year_index = i + 1
            data_vintage = len(income_stmts or [])
            calibration_conf = float(getattr(assumption_set, "calibration_confidence", 0.0))
            analog_conf = float(getattr(getattr(assumption_set, "analog_set", None), "analog_confidence", 0.0))
            confidence_intervals = {
                "revenue": confidence_module.compute_confidence_interval(
                    rev,
                    base_std=0.06,
                    year_index=year_index,
                    data_vintage_years=data_vintage,
                    calibration_confidence=calibration_conf,
                    analog_confidence=analog_conf,
                    calibration_cohort_size=getattr(assumption_set, "calibration_cohort_size", None),
                ),
                "ebit_margin": confidence_module.compute_confidence_interval(
                    ebit_m,
                    base_std=0.025,
                    year_index=year_index,
                    data_vintage_years=data_vintage,
                    calibration_confidence=calibration_conf,
                    analog_confidence=analog_conf,
                    calibration_cohort_size=getattr(assumption_set, "calibration_cohort_size", None),
                    scale_with_value=False,
                ),
                "ebit": confidence_module.compute_confidence_interval(
                    ebit,
                    base_std=0.08,
                    year_index=year_index,
                    data_vintage_years=data_vintage,
                    calibration_confidence=calibration_conf,
                    analog_confidence=analog_conf,
                    calibration_cohort_size=getattr(assumption_set, "calibration_cohort_size", None),
                ),
                "ufcf": confidence_module.compute_confidence_interval(
                    ufcf,
                    base_std=0.10,
                    year_index=year_index,
                    data_vintage_years=data_vintage,
                    calibration_confidence=calibration_conf,
                    analog_confidence=analog_conf,
                    calibration_cohort_size=getattr(assumption_set, "calibration_cohort_size", None),
                ),
            }

        forecast_rows.append(ForecastYear(
            year=i + 1,
            revenue=rev, ebit_margin=ebit_m, ebit=ebit,
            tax_rate=tax_rate, nopat=nopat,
            da=da, capex=capex,
            nowc=nowc_schedule[i], delta_nowc=d_nowc,
            ufcf=ufcf, discount_factor=df, pv_ufcf=pv,
            confidence_intervals=confidence_intervals,
        ))

    result.forecast_years_data = forecast_rows

    lifecycle_stage = infer_revenue_lifecycle_stage(base_revenue, near_term_growth, terminal_growth)
    if lifecycle_stage in {"hypergrowth", "growth"} and near_term_growth > terminal_growth + 0.05:
        warns.append(
            f"Revenue lifecycle classified as {lifecycle_stage}; growth fades dynamically toward terminal growth."
        )

    # ── 4. PV of forecast UFCFs ──────────────────────────────────────────────
    result.pv_ufcfs = sum(row.pv_ufcf for row in forecast_rows)

    # ── 5. Terminal value (v3.0 C.1 NIKE convention) ─────────────────────────
    # TV = FCF_yr_N / (WACC − g)   [year-N FCF is the TV base, no (1+g) growth]
    # This is mathematically equivalent to the textbook formula using N−1 explicit
    # years + TV = FCF_N*(1+g)/(WACC−g). Either way, TV is discounted at period N.
    last_ufcf      = forecast_rows[-1].ufcf if forecast_rows else 0.0
    terminal_ufcf  = last_ufcf                # NIKE: no extra (1+g) step
    result.terminal_ufcf = terminal_ufcf

    if forecast_rows:
        terminal_row = forecast_rows[-1]
        terminal_reinvestment_rate = compute_reinvestment_rate(
            terminal_row.nopat,
            terminal_row.capex,
            terminal_row.da,
            terminal_row.delta_nowc,
        )
        base_invested_capital = compute_invested_capital(latest_bs)
        cumulative_reinvestment = sum(
            max(row.capex - row.da, 0.0) + row.delta_nowc
            for row in forecast_rows
        )
        terminal_invested_capital = max(base_invested_capital + cumulative_reinvestment, base_invested_capital, 0.0)
        terminal_roic = compute_roic(terminal_row.nopat, terminal_invested_capital)
        capped_growth, growth_warning = enforce_terminal_growth_consistency(
            terminal_growth,
            terminal_roic,
            terminal_reinvestment_rate,
        )
        if growth_warning:
            warns.append(growth_warning)
            terminal_growth = capped_growth
            result.terminal_growth = terminal_growth

    if terminal_growth >= wacc:
        capped_growth = max(0.0, wacc - 0.005)
        warns.append(
            f"Terminal growth capped from {terminal_growth:.2%} to {capped_growth:.2%} to keep WACC-g positive."
        )
        terminal_growth = capped_growth
        result.terminal_growth = terminal_growth

    tv_ggm = gordon_growth_tv(terminal_ufcf, wacc, terminal_growth)
    result.terminal_value_ggm = tv_ggm

    # Exit multiple cross-check (if multiple provided)
    if exit_ev_ebitda_multiple is not None and forecast_rows:
        last_ebit   = forecast_rows[-1].ebit
        last_da     = forecast_rows[-1].da
        last_ebitda = last_ebit + last_da
        tv_em = exit_multiple_tv(last_ebitda, exit_ev_ebitda_multiple)
        result.terminal_value_em = tv_em
        # Use GGM as primary; flag if divergence > 30%
        if last_ebitda > 0:
            div = abs(tv_ggm - tv_em) / max(abs(tv_ggm), abs(tv_em))
            if div > 0.30:
                warns.append(
                    f"GGM TV ({tv_ggm:,.0f}M) and exit-multiple TV ({tv_em:,.0f}M) "
                    f"diverge by {div:.0%}. Review terminal assumptions."
                )

    pv_tv = pv_terminal_value(tv_ggm, wacc, forecast_years, mid_year_convention)
    result.pv_terminal_value = pv_tv

    # ── 6. Enterprise value ───────────────────────────────────────────────────
    ev = result.pv_ufcfs + pv_tv
    result.enterprise_value = ev
    result.tv_pct_of_ev = safe_divide(pv_tv, ev, 0.0) if ev > 0 else 0.0

    # TV% warning
    if result.tv_pct_of_ev > 0.80:
        warns.append(
            f"Terminal value is {result.tv_pct_of_ev:.0%} of total EV — "
            "model is highly sensitive to terminal assumptions."
        )

    result.warnings = warns

    if assumption_set is not None:
        from auto_valuation.config import LEARNING_CONFIG
        from auto_valuation.learning.confidence import run_monte_carlo

        enable_mc = LEARNING_CONFIG["monte_carlo_enabled"] if monte_carlo_enabled is None else monte_carlo_enabled
        if enable_mc and confidence_bundle is not None:
            intervals = {
                "near_term_growth": confidence_bundle.get("revenue_growth_adj") or confidence_bundle.get("revenue_growth"),
                "target_ebit_margin": confidence_bundle.get("ebit_margin_adj") or confidence_bundle.get("ebit_margin"),
                "wacc": confidence_bundle.get("wacc_adj") or confidence_bundle.get("wacc"),
                "terminal_growth": confidence_bundle.get("terminal_growth_adj") or confidence_bundle.get("terminal_growth"),
            }
            intervals = {key: value for key, value in intervals.items() if value is not None}
            if len(intervals) == 4:
                base_kwargs = {
                    "ticker": ticker,
                    "scenario": scenario,
                    "income_stmts": income_stmts,
                    "cash_flows": cash_flows,
                    "balance_sheets": balance_sheets,
                    "wacc": wacc,
                    "terminal_growth": terminal_growth,
                    "near_term_growth": near_term_growth,
                    "target_ebit_margin": target_ebit_margin,
                    "forecast_years": forecast_years,
                    "hold_years": hold_years,
                    "ebit_margin_fade_years": ebit_margin_fade_years,
                    "exit_ev_ebitda_multiple": exit_ev_ebitda_multiple,
                    "tax_rate_override": tax_rate_override,
                    "da_pct_override": da_pct_override,
                    "capex_pct_override": capex_pct_override,
                    "sbc_pct_override": sbc_pct_override,
                    "mid_year_convention": mid_year_convention,
                }

                def _evaluate(sampled: dict[str, float]) -> float:
                    sampled_kwargs = dict(base_kwargs)
                    sampled_kwargs["near_term_growth"] = sampled["near_term_growth"]
                    sampled_kwargs["target_ebit_margin"] = sampled["target_ebit_margin"]
                    sampled_kwargs["wacc"] = sampled["wacc"]
                    sampled_kwargs["terminal_growth"] = min(sampled["terminal_growth"], sampled["wacc"] - 0.005)
                    sampled_result = run_dcf(
                        **sampled_kwargs,
                        assumption_set=None,
                        monte_carlo_enabled=False,
                    )
                    return sampled_result.enterprise_value

                result.monte_carlo_result = run_monte_carlo(
                    intervals,
                    _evaluate,
                    samples=int(monte_carlo_samples or LEARNING_CONFIG["monte_carlo_samples"]),
                    seed=int(monte_carlo_seed or LEARNING_CONFIG["monte_carlo_seed"]),
                )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical standalone helpers (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

def compute_pv_ufcfs(
    ufcfs: list[float],
    wacc: float,
    mid_year_convention: bool = True,
) -> float:
    """
    Present-value sum of forecast UFCFs.

    Uses mid-year convention by default: PV = UFCF / (1 + WACC)^(t - 0.5).
    Reference: Architecture Plan Parts 3.2, 4.5.
    """
    pv_total = 0.0
    for t, fcf in enumerate(ufcfs, start=1):
        exp = (t - 0.5) if mid_year_convention else t
        pv_total += fcf / (1.0 + wacc) ** exp
    return pv_total


def compute_enterprise_value(
    pv_ufcfs: float,
    pv_terminal_value_: float,
) -> float:
    """
    Enterprise Value = PV(UFCFs) + PV(Terminal Value).

    Reference: Architecture Plan Part 3.
    """
    return pv_ufcfs + pv_terminal_value_
