"""
assumptions/engine.py — AssumptionsEngine: derives the 25 core model drivers
from historical data, applying sector defaults and analyst overrides.

Reference: Architecture Plan Parts 51, 52, 57, 58, A.4.

All monetary values in USD millions.  All rates as decimals.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from auto_valuation.assumptions.defaults import (
    get_sector_ebit_margin,
    get_sector_capex_pct,
    get_sector_terminal_sbc_pct,
    get_sector_wc_days,
)


# ─────────────────────────────────────────────────────────────────────────────
# AssumptionSet — the 25 drivers used by the forecast engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AssumptionSet:
    """All model assumptions for a single DCF run."""
    # Revenue
    revenue_growth_rates: list[float] = field(default_factory=list)  # per-year list
    near_term_growth:     float = 0.05
    long_run_growth:      float = 0.025

    # Margins
    ebit_margin_current:  float = 0.15
    ebit_margin_terminal: float = 0.14
    ebit_margin_schedule: list[float] = field(default_factory=list)

    # Tax
    effective_tax_rate:   float = 0.21

    # Working capital (WC days)
    dso_days:  float = 45.0
    dio_days:  float = 45.0
    dpo_days:  float = 40.0

    # CapEx
    capex_pct_revenue:    float = 0.04
    capex_schedule:       list[float] = field(default_factory=list)   # per-year pct

    # D&A
    da_pct_revenue:       float = 0.03

    # SBC
    sbc_pct_revenue:      float = 0.01
    sbc_terminal_pct:     float = 0.01

    # Other
    other_income_mm:      float = 0.0   # recurring below-EBIT income (flat)

    # Shares
    basic_shares_mm:      float = 0.0
    net_new_shares_annual_mm: float = 0.0   # positive = issuances net of buybacks

    # Debt
    debt_to_total_assets:  float = 0.20   # D/TA ratio for IBD projection

    # NCI
    nci_pct_of_ebit:       float = 0.0

    # Pension
    pension_service_pct_rev: float = 0.0
    pension_interest_flat_mm: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — normalised multi-year averages
# ─────────────────────────────────────────────────────────────────────────────

def _safe_mean(values: list[float | None], n: int = 3) -> float | None:
    clean = [v for v in values[:n] if v is not None and not _is_nan(v)]
    if not clean:
        return None
    return statistics.mean(clean)


def _is_nan(x: Any) -> bool:
    try:
        import math
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


def _median_field(
    statements: list[dict],
    field_name: str,
    n: int = 5,
) -> float | None:
    vals = [
        s.get(field_name)
        for s in statements[:n]
        if s.get(field_name) is not None and not _is_nan(s.get(field_name, float("nan")))
    ]
    if not vals:
        return None
    return statistics.median(vals)


def _margin_series(
    statements: list[dict],
    numerator_field: str,
    denominator_field: str = "revenue",
    n: int = 5,
) -> list[float]:
    results = []
    for s in statements[:n]:
        num = s.get(numerator_field)
        den = s.get(denominator_field)
        if num is not None and den and abs(den) > 0:
            results.append(num / den)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# EBIT margin fade schedule  (Part 51.1)
# ─────────────────────────────────────────────────────────────────────────────

def compute_ebit_margin_schedule(
    current_margin: float,
    terminal_margin: float,
    forecast_years: int = 5,
    fade_start_year: int = 1,
) -> list[float]:
    """
    Linearly fade EBIT margin from current_margin (Year 1) to terminal_margin
    (Year forecast_years).

    Returns a list of `forecast_years` EBIT margin values.
    Reference: Architecture Plan Part 51.1.
    """
    if forecast_years <= 0:
        return []
    if forecast_years == 1:
        return [terminal_margin]

    schedule = []
    for yr in range(1, forecast_years + 1):
        fraction = (yr - fade_start_year) / (forecast_years - fade_start_year)
        fraction = max(0.0, min(1.0, fraction))
        margin = current_margin + (terminal_margin - current_margin) * fraction
        schedule.append(margin)
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# CapEx intensity fade schedule  (Part 51.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_capex_schedule(
    current_capex_pct: float,
    sector_capex_pct: float,
    forecast_years: int = 5,
) -> list[float]:
    """
    Linearly fade CapEx from current_capex_pct to sector_capex_pct
    over the forecast period.

    Returns a list of `forecast_years` CapEx-as-%-of-revenue values.
    Reference: Architecture Plan Part 51.2.
    """
    if forecast_years <= 0:
        return []
    if forecast_years == 1:
        return [sector_capex_pct]
    schedule = []
    for yr in range(1, forecast_years + 1):
        fraction = (yr - 1) / (forecast_years - 1)
        pct = current_capex_pct + (sector_capex_pct - current_capex_pct) * fraction
        schedule.append(max(0.0, pct))
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Normalised ETR  (Part 43.1)
# ─────────────────────────────────────────────────────────────────────────────

def compute_normalized_etr(
    income_stmts: list[dict],
    n_years: int = 5,
    etr_min: float = 0.05,
    etr_max: float = 0.35,
) -> float:
    """
    Weighted 5-year normalised effective tax rate.

    Excludes outlier years where |ETR| > 0.60 (valuation-year DTA release
    or legal settlements distort single years).

    Returns ETR as decimal, clamped to [etr_min, etr_max].
    Reference: Architecture Plan Part 43.1.
    """
    pairs: list[tuple[float, float]] = []
    for s in income_stmts[:n_years]:
        pretax = s.get("incomeBeforeTax") or s.get("pretaxIncome")
        taxes  = s.get("incomeTaxExpense") or s.get("taxExpense")
        if pretax and abs(pretax) > 0 and taxes is not None:
            etr = taxes / pretax
            if abs(etr) <= 0.60:   # exclude extreme outliers
                pairs.append((abs(pretax), etr))

    if not pairs:
        return 0.21   # US statutory fallback

    # Weighted average: weight by absolute pre-tax income
    total_weight = sum(w for w, _ in pairs)
    if total_weight <= 0:
        return 0.21
    weighted_etr = sum(w * e for w, e in pairs) / total_weight
    return max(etr_min, min(etr_max, weighted_etr))


# ─────────────────────────────────────────────────────────────────────────────
# Working capital days  (Part 4.2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_wc_days(
    income_stmts: list[dict],
    balance_sheets: list[dict],
    n_years: int = 3,
    use_average_balances: bool = True,
) -> dict[str, float]:
    """
    Compute average DSO, DIO, DPO over the last n_years.

    If use_average_balances=True, uses (opening+closing)/2 for BS items
    (more accurate for growing companies).

    Returns dict: {dso, dio, dpo, cwc_days}.
    Reference: Architecture Plan Part 4.2.
    """
    bs_list = sorted(
        balance_sheets or [],
        key=lambda b: b.get("calendarYear", b.get("date", "")),
        reverse=True,
    )
    is_list = sorted(
        income_stmts or [],
        key=lambda s: s.get("calendarYear", s.get("date", "")),
        reverse=True,
    )
    bs_by_yr = {b.get("calendarYear", b.get("date", "")): b for b in bs_list}

    dsos, dios, dpos = [], [], []

    for stmt in is_list[:n_years]:
        yr  = stmt.get("calendarYear", stmt.get("date", ""))
        rev = stmt.get("revenue") or 0
        cogs = stmt.get("costOfRevenue") or stmt.get("costOfGoodsSold") or 0

        bs  = bs_by_yr.get(yr, {})
        ar  = bs.get("netReceivables") or bs.get("accountsReceivable") or 0
        inv = bs.get("inventory") or 0
        ap  = bs.get("accountPayables") or bs.get("accountsPayable") or 0

        if use_average_balances and len(bs_list) > 1:
            idx = bs_list.index(bs_by_yr.get(yr, bs_list[0])) if yr in bs_by_yr else 0
            if idx + 1 < len(bs_list):
                prev = bs_list[idx + 1]
                ar  = (ar + (prev.get("netReceivables") or prev.get("accountsReceivable") or 0)) / 2
                inv = (inv + (prev.get("inventory") or 0)) / 2
                ap  = (ap + (prev.get("accountPayables") or prev.get("accountsPayable") or 0)) / 2

        if rev > 0:
            dsos.append(ar * 365 / rev)
        if cogs > 0:
            dios.append(inv * 365 / cogs)
            dpos.append(ap * 365 / cogs)

    dso = statistics.mean(dsos) if dsos else 45.0
    dio = statistics.mean(dios) if dios else 45.0
    dpo = statistics.mean(dpos) if dpos else 40.0

    return {"dso": dso, "dio": dio, "dpo": dpo, "cwc_days": dso + dio - dpo}


# ─────────────────────────────────────────────────────────────────────────────
# Revenue growth profile  (Parts 4.1, 57)
# ─────────────────────────────────────────────────────────────────────────────

def compute_revenue_growth_profile(
    income_stmts: list[dict],
    forecast_years: int = 7,
    terminal_g: float = 0.025,
    fade_years: int = 5,
    growth_profile: str = "1stage",
) -> tuple[float, list[float]]:
    """
    Compute revenue growth rate schedule for forecast_years.

    growth_profile options (Architecture Plan A.4):
      '1stage'  — DEFAULT, matches NIKE model: single constant rate for ALL years.
                  Use historical CAGR as the near-term rate; holds flat.
      '2stage'  — years 1-3 hold near-term, then linearly fade to terminal_g.
      'hmodel'  — H-model exponential decay from near-term toward terminal_g.

    Returns (near_term_growth, [growth_y1, ..., growth_yN]).
    Reference: Architecture Plan Parts 4.1, 57; v4.0 A.4.
    """
    stmts = sorted(
        income_stmts or [],
        key=lambda s: s.get("calendarYear", s.get("date", "")),
        reverse=True,
    )
    # Historical CAGR
    n = min(5, len(stmts) - 1)
    if n > 0:
        rev_now  = stmts[0].get("revenue") or 0
        rev_base = stmts[n].get("revenue") or 0
        if rev_now > 0 and rev_base > 0:
            near_term = (rev_now / rev_base) ** (1 / n) - 1
        else:
            near_term = terminal_g
    else:
        near_term = terminal_g

    # Clamp to reasonable range
    near_term = max(-0.20, min(0.50, near_term))

    if growth_profile == "1stage":
        # Constant rate for all years — DEFAULT, matches NIKE model (v4.0 A.4)
        schedule = [near_term] * forecast_years

    elif growth_profile == "2stage":
        # Linear fade from near_term to terminal_g over the forecast
        schedule = []
        total_fade = max(forecast_years - 1, 1)
        for yr in range(1, forecast_years + 1):
            fraction = (yr - 1) / total_fade
            g = near_term + (terminal_g - near_term) * fraction
            schedule.append(g)

    elif growth_profile == "hmodel":
        # H-model: exponential-style decay toward terminal_g
        H = fade_years  # half-life parameter
        schedule = [
            terminal_g + (near_term - terminal_g) * max(0.0, 1 - t / H)
            for t in range(forecast_years)
        ]

    else:
        raise ValueError(
            f"Unknown growth_profile '{growth_profile}'. "
            "Choose '1stage', '2stage', or 'hmodel'."
        )

    return near_term, schedule


# ─────────────────────────────────────────────────────────────────────────────
# SBC % of revenue (historical median)
# ─────────────────────────────────────────────────────────────────────────────

def compute_sbc_pct(
    income_stmts: list[dict],
    cash_flows: list[dict],
    n_years: int = 3,
) -> float:
    """
    Compute SBC as % of revenue from historical data.
    SBC is typically in the cash flow statement as 'stockBasedCompensation'.
    Falls back to 0 if not available.
    """
    cf_by_yr = {c.get("calendarYear", c.get("date", "")): c for c in (cash_flows or [])}
    pcts = []
    for stmt in income_stmts[:n_years]:
        yr  = stmt.get("calendarYear", stmt.get("date", ""))
        rev = stmt.get("revenue") or 0
        cf  = cf_by_yr.get(yr, {})
        sbc = cf.get("stockBasedCompensation") or cf.get("shareBasedCompensation") or 0
        if rev > 0 and sbc > 0:
            pcts.append(sbc / rev)
    return statistics.mean(pcts) if pcts else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# D&A % of revenue (historical median)
# ─────────────────────────────────────────────────────────────────────────────

def compute_da_pct(
    income_stmts: list[dict],
    cash_flows: list[dict],
    n_years: int = 3,
) -> float:
    """
    Compute D&A as % of revenue from historical data.
    D&A sourced from cash flow statement 'depreciationAndAmortization'.
    """
    cf_by_yr = {c.get("calendarYear", c.get("date", "")): c for c in (cash_flows or [])}
    pcts = []
    for stmt in income_stmts[:n_years]:
        yr  = stmt.get("calendarYear", stmt.get("date", ""))
        rev = stmt.get("revenue") or 0
        cf  = cf_by_yr.get(yr, {})
        da  = cf.get("depreciationAndAmortization") or cf.get("depreciation") or 0
        if rev > 0 and da > 0:
            pcts.append(da / rev)
    return statistics.mean(pcts) if pcts else 0.04


# ─────────────────────────────────────────────────────────────────────────────
# AssumptionsEngine — ties it all together
# ─────────────────────────────────────────────────────────────────────────────

class AssumptionsEngine:
    """
    Derives all 25 model drivers from historical data plus config overrides.

    Usage:
        engine = AssumptionsEngine(income_stmts, balance_sheets, cash_flows, cfg)
        assumptions = engine.build()
    """

    def __init__(
        self,
        income_stmts:  list[dict],
        balance_sheets: list[dict],
        cash_flows:    list[dict],
        sector:        str = "Default",
        forecast_years: int = 5,
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.income_stmts  = income_stmts or []
        self.balance_sheets = balance_sheets or []
        self.cash_flows    = cash_flows or []
        self.sector        = sector
        self.forecast_years = forecast_years
        self.overrides     = config_overrides or {}

    def build(self) -> AssumptionSet:
        """Compute and return the full AssumptionSet."""
        aset = AssumptionSet()
        stmts = self.income_stmts
        bss   = self.balance_sheets
        cfs   = self.cash_flows
        fy    = self.forecast_years
        ov    = self.overrides

        # ── 1. Revenue growth ──────────────────────────────────────────────────
        terminal_g = float(ov.get("terminal_g", 0.025))
        near_term, growth_sched = compute_revenue_growth_profile(
            stmts, fy, terminal_g=terminal_g
        )
        aset.near_term_growth     = float(ov.get("near_term_growth", near_term))
        aset.long_run_growth      = terminal_g
        aset.revenue_growth_rates = (
            list(ov["revenue_growth_rates"])
            if "revenue_growth_rates" in ov
            else growth_sched
        )

        # ── 2. EBIT margin ─────────────────────────────────────────────────────
        margin_series = _margin_series(stmts, "operatingIncome")
        if not margin_series:
            margin_series = _margin_series(stmts, "ebit")
        current_margin = (
            float(ov["ebit_margin_override"])
            if "ebit_margin_override" in ov
            else (statistics.mean(margin_series[:3]) if margin_series else 0.14)
        )
        terminal_margin = get_sector_ebit_margin(self.sector)
        terminal_margin = float(ov.get("ebit_margin_terminal", terminal_margin))
        aset.ebit_margin_current  = current_margin
        aset.ebit_margin_terminal = terminal_margin
        aset.ebit_margin_schedule = compute_ebit_margin_schedule(
            current_margin, terminal_margin, fy
        )

        # ── 3. Tax rate ────────────────────────────────────────────────────────
        if "tax_rate_override" in ov and ov["tax_rate_override"] is not None:
            aset.effective_tax_rate = float(ov["tax_rate_override"])
        else:
            aset.effective_tax_rate = compute_normalized_etr(stmts)

        # ── 4. Working capital days ────────────────────────────────────────────
        wc = compute_wc_days(stmts, bss)
        aset.dso_days = float(ov.get("dso_override", wc["dso"]))
        aset.dio_days = float(ov.get("dio_override", wc["dio"]))
        aset.dpo_days = float(ov.get("dpo_override", wc["dpo"]))

        # ── 5. CapEx ───────────────────────────────────────────────────────────
        # Historical CapEx pct of revenue
        capex_pcts = []
        cf_by_yr = {c.get("calendarYear", c.get("date", "")): c for c in cfs}
        for s in stmts[:5]:
            yr  = s.get("calendarYear", s.get("date", ""))
            rev = s.get("revenue") or 0
            cf  = cf_by_yr.get(yr, {})
            cx  = abs(cf.get("capitalExpenditure") or cf.get("capex") or 0)
            if rev > 0 and cx > 0:
                capex_pcts.append(cx / rev)
        current_capex_pct = (
            float(ov["capex_override"])
            if "capex_override" in ov and ov["capex_override"] is not None
            else (statistics.median(capex_pcts) if capex_pcts else get_sector_capex_pct(self.sector))
        )
        terminal_capex_pct = get_sector_capex_pct(self.sector)
        aset.capex_pct_revenue = current_capex_pct
        aset.capex_schedule = compute_capex_schedule(
            current_capex_pct, terminal_capex_pct, fy
        )

        # ── 6. D&A ─────────────────────────────────────────────────────────────
        aset.da_pct_revenue = compute_da_pct(stmts, cfs)

        # ── 7. SBC ─────────────────────────────────────────────────────────────
        sbc_pct = compute_sbc_pct(stmts, cfs)
        aset.sbc_pct_revenue = sbc_pct
        aset.sbc_terminal_pct = float(
            ov.get("sbc_terminal_pct", get_sector_terminal_sbc_pct(self.sector))
        )

        # ── 8. Other income ────────────────────────────────────────────────────
        other_vals = []
        for s in stmts[:3]:
            v = s.get("otherIncome") or s.get("otherNonOperatingIncome") or 0
            if v:
                other_vals.append(float(v))
        aset.other_income_mm = statistics.mean(other_vals) if other_vals else 0.0

        # ── 9. Shares ─────────────────────────────────────────────────────────
        latest_stmt = stmts[0] if stmts else {}
        aset.basic_shares_mm = float(
            latest_stmt.get("weightedAverageShsOut")
            or latest_stmt.get("sharesOutstanding")
            or 0
        )

        # ── 10. Debt / total assets ────────────────────────────────────────────
        latest_bs = bss[0] if bss else {}
        total_assets = latest_bs.get("totalAssets") or 1.0
        total_debt = (
            (latest_bs.get("shortTermDebt") or 0)
            + (latest_bs.get("longTermDebt") or 0)
        )
        aset.debt_to_total_assets = (
            float(ov["debt_to_total_assets"])
            if "debt_to_total_assets" in ov
            else max(0.0, total_debt / total_assets)
        )

        return aset


def build_assumptions(
    ticker: str,
    income_stmts: list[dict],
    balance_sheets: list[dict],
    cash_flows: list[dict],
    *,
    sector: str = "Default",
    industry: str = "",
    forecast_years: int = 5,
    config_overrides: dict[str, Any] | None = None,
    learning_mode: bool = False,
    company_name: str = "",
    market_cap_regime: str = "large",
    macro_regime: str = "neutral",
    research_insights: list[Any] | None = None,
    calibration_observations: list[Any] | None = None,
    analog_candidates: list[Any] | None = None,
    feature_vector: dict[str, float] | list[float] | None = None,
):
    """Build raw assumptions and optionally adapt them through the learning stack."""
    engine = AssumptionsEngine(
        income_stmts,
        balance_sheets,
        cash_flows,
        sector=sector,
        forecast_years=forecast_years,
        config_overrides=config_overrides,
    )
    raw = engine.build()
    if not learning_mode:
        return raw

    from auto_valuation.config import LEARNING_CONFIG
    if not LEARNING_CONFIG.get("learning_enabled", True):
        return raw

    from auto_valuation.learning.adapter import adapt_assumptions
    from auto_valuation.learning.online_research import fetch_insights

    insights = research_insights
    if insights is None:
        insights = fetch_insights(
            ticker,
            sector,
            industry,
            company_name=company_name or ticker,
            enabled=LEARNING_CONFIG.get("online_research_enabled", True),
        )

    return adapt_assumptions(
        ticker,
        sector,
        industry,
        len(income_stmts or []),
        market_cap_regime,
        macro_regime,
        raw,
        insights,
        feature_vector=feature_vector,
        calibration_observations=calibration_observations,
        analog_candidates=analog_candidates,
    )
