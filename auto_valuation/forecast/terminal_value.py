"""
forecast/terminal_value.py — Terminal value via Gordon Growth Model and exit multiple.

Reference: Architecture Plan Parts 3.3, 4, 41, 43, 44, 46, 52.

All monetary values in USD millions.
"""

from __future__ import annotations

from auto_valuation.utils.error import safe_divide, ValuationWarning


# ─────────────────────────────────────────────────────────────────────────────
# Reinvestment rate  (Part 52.1)
# ─────────────────────────────────────────────────────────────────────────────

def compute_reinvestment_rate(
    nopat: float,
    capex: float,
    da: float,
    delta_nowc: float,
) -> float:
    """
    Reinvestment rate = net reinvestment / NOPAT.

    Net reinvestment = net CapEx + change in NOWC
    Net CapEx = gross CapEx − D&A  (growth CapEx only; D&A replaces depreciating assets)
    Net CapEx is floored at 0 (cannot be negative).

    Used in the terminal year to check ROIC-growth consistency:
        g_implied = ROIC_terminal × reinvestment_rate

    This should match the terminal growth rate (g) used in the TV formula.
    A large inconsistency (>3pp) triggers a validation warning.

    Args:
        nopat:      Terminal-year NOPAT ($M).
        capex:      Gross capital expenditure ($M, positive).
        da:         Depreciation & amortisation ($M, positive).
        delta_nowc: Change in net operating working capital ($M; positive = increase).

    Returns:
        float: reinvestment rate (0.0 if NOPAT ≤ 0).

    Reference: Architecture Plan Part 52.1.
    """
    net_capex = max(capex - da, 0.0)
    reinvestment = net_capex + delta_nowc
    if nopat <= 0:
        return 0.0
    return safe_divide(reinvestment, nopat, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Gordon Growth Model  (Part 3.3, 41)
# ─────────────────────────────────────────────────────────────────────────────

def gordon_growth_tv(
    terminal_ufcf: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    """
    Gordon Growth Model terminal value:
        TV = UFCF_{n+1} / (WACC − g)

    UFCF_{n+1} = terminal_ufcf (the first cash flow in the terminal period,
    i.e., last forecast-year UFCF grown by terminal_growth).

    Raises ValueError if WACC ≤ terminal_growth (model breaks down).
    Reference: Part 3.3.
    """
    spread = wacc - terminal_growth
    if spread <= 0:
        raise ValueError(
            f"WACC ({wacc:.2%}) must exceed terminal growth ({terminal_growth:.2%}). "
            "Adjust assumptions."
        )
    return terminal_ufcf / spread


def gordon_growth_tv_nycf(
    terminal_ufcf: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    """
    Gordon Growth Model terminal value using *next-year* cash flow convention
    (CFI / textbook standard):

        TV = UFCF_n × (1 + g) / (WACC − g)

    This grows the last forecast-period FCF by one terminal growth period before
    capitalising, whereas :func:`gordon_growth_tv` uses the UFCF as-is (NIKE
    convention where UFCF_n is already the first terminal-period cash flow).

    Use this function when:
    - The last forecast-year UFCF is the *prior-period* base (not yet grown)
    - Comparing to a textbook or CFI-style DCF that applies the (1+g) step

    Args:
        terminal_ufcf  : Last explicit forecast-year UFCF ($M).
        wacc           : Weighted average cost of capital (decimal, e.g. 0.09).
        terminal_growth: Perpetuity growth rate (decimal, e.g. 0.025).

    Returns:
        float: Terminal value ($M).

    Raises:
        ValueError: if WACC ≤ terminal_growth.

    Reference: CFI "Terminal Value" guide; Damodaran *Valuation* Ch. 12.
    """
    spread = wacc - terminal_growth
    if spread <= 0:
        raise ValueError(
            f"WACC ({wacc:.2%}) must exceed terminal growth ({terminal_growth:.2%}). "
            "Adjust assumptions."
        )
    return terminal_ufcf * (1.0 + terminal_growth) / spread


def gordon_growth_tv_from_nopat(
    terminal_nopat: float,
    reinvestment_rate: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    """
    Economically consistent Gordon Growth TV using NOPAT and reinvestment rate:
        UFCF_{n+1} = NOPAT_{n+1} × (1 − reinvestment_rate)

    reinvestment_rate = g / ROIC  (where g = terminal_growth, ROIC = terminal ROIC)
    Reference: Parts 3.3, 41.
    """
    terminal_ufcf_adj = terminal_nopat * (1.0 - reinvestment_rate)
    return gordon_growth_tv(terminal_ufcf_adj, wacc, terminal_growth)


# ─────────────────────────────────────────────────────────────────────────────
# Exit multiple TV  (Part 43)
# ─────────────────────────────────────────────────────────────────────────────

def exit_multiple_tv(
    terminal_ebitda: float,
    ev_ebitda_multiple: float,
) -> float:
    """
    Terminal value via EV/EBITDA exit multiple:
        TV = EBITDA_n × EV/EBITDA_multiple

    Used as a cross-check against the GGM, not the primary method.
    Reference: Part 43.
    """
    return terminal_ebitda * ev_ebitda_multiple


def exit_revenue_tv(
    terminal_revenue: float,
    ev_revenue_multiple: float,
) -> float:
    """
    Terminal value via EV/Revenue multiple (for pre-profit or high-growth companies).
    Reference: Part 43.
    """
    return terminal_revenue * ev_revenue_multiple


# ─────────────────────────────────────────────────────────────────────────────
# TV discounting  (Part 4, 41)
# ─────────────────────────────────────────────────────────────────────────────

def pv_terminal_value(
    terminal_value: float,
    wacc: float,
    forecast_years: int,
    mid_year_convention: bool = True,
) -> float:
    """
    Discount the terminal value back to today:
        PV_TV = TV / (1 + WACC) ^ (n + 0.5)    [mid-year]
        PV_TV = TV / (1 + WACC) ^ n              [end-of-year]

    Reference: Parts 4, 41.
    """
    exponent = forecast_years + (0.5 if mid_year_convention else 0.0)
    return terminal_value / ((1.0 + wacc) ** exponent)


# ─────────────────────────────────────────────────────────────────────────────
# TV sensitivity — implied WACC / g  (Part 46)
# ─────────────────────────────────────────────────────────────────────────────

def implied_terminal_growth(
    tv: float,
    terminal_ufcf: float,
    wacc: float,
) -> float:
    """
    Back-solve the implied terminal growth from a given TV value.
        g = WACC − UFCF / TV

    Reference: Part 46.
    """
    if tv <= 0:
        return 0.0
    return wacc - terminal_ufcf / tv


def tv_sensitivity_table(
    terminal_ufcf: float,
    wacc_range: list[float],
    growth_range: list[float],
    forecast_years: int,
    mid_year_convention: bool = True,
) -> dict[tuple[float, float], float]:
    """
    Compute PV of terminal value for a grid of (WACC, g) combinations.

    Returns dict {(wacc, g): pv_tv} for all valid combinations
    (skips where WACC ≤ g).
    Reference: Part 46.
    """
    result: dict[tuple[float, float], float] = {}
    for wacc in wacc_range:
        for g in growth_range:
            if wacc <= g:
                continue
            tv = gordon_growth_tv(terminal_ufcf, wacc, g)
            pv = pv_terminal_value(tv, wacc, forecast_years, mid_year_convention)
            result[(round(wacc, 4), round(g, 4))] = pv
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Convenience aliases  (arch plan compatibility)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Two-stage terminal value  (multi-stage perpetuity)
# ─────────────────────────────────────────────────────────────────────────────

def gordon_growth_tv_two_stage(
    ufcf_n: float,
    near_terminal_g: float,
    stable_g: float,
    wacc: float,
    transition_years: int = 5,
) -> float:
    """
    Two-stage terminal value for companies where the growth rate
    transitions from a *near-terminal* rate to a *stable* (perpetuity) rate
    over an explicit transition window.

    Stage 1 — Explicit transition (years 1 … transition_years after the
    forecast horizon):
        UFCF_t = UFCF_n × (1 + near_terminal_g)^t

    Stage 2 — Stable perpetuity starting at year (transition_years + 1):
        TV_stable = UFCF_{transition_years} × (1 + stable_g) / (WACC − stable_g)
        Discounted to period N at: (1 + WACC)^transition_years

    Total TV = PV(Stage-1 UFCFs) + PV(Stage-2 perpetuity),
    both discounted to the end of the explicit forecast horizon (period N).

    Use when:
    - The company is still in a high-but-declining growth phase at year N
    - near_terminal_g > stable_g (e.g. 5% → 2.5%)
    - A single-stage GGM would over-state terminal value

    Args:
        ufcf_n          : Last forecast-year UFCF ($M, period-N base).
        near_terminal_g : Near-term transitional growth rate (decimal).
        stable_g        : Long-run stable perpetuity growth rate (decimal).
        wacc            : WACC (decimal). Must exceed stable_g.
        transition_years: Number of transition years in Stage 1 (default 5).

    Returns:
        float: Terminal value at period N ($M).

    Raises:
        ValueError: if WACC ≤ stable_g or transition_years < 1.

    Reference: Damodaran *Valuation* Ch. 12 (two-stage DCF); CFI "Terminal Value"
               ("If the growth rate changes, a multiple-stage terminal value
                can then be determined instead.").
    """
    if wacc <= stable_g:
        raise ValueError(
            f"WACC ({wacc:.2%}) must exceed stable terminal growth ({stable_g:.2%})."
        )
    if transition_years < 1:
        raise ValueError("transition_years must be at least 1.")

    # Stage 1: sum discounted transition-period UFCFs
    pv_stage1 = 0.0
    for t in range(1, transition_years + 1):
        cf_t = ufcf_n * (1.0 + near_terminal_g) ** t
        pv_stage1 += cf_t / (1.0 + wacc) ** t

    # Stage 2: stable perpetuity valued at end of transition period, PV'd back to N
    cf_transition = ufcf_n * (1.0 + near_terminal_g) ** transition_years
    tv_stable = cf_transition * (1.0 + stable_g) / (wacc - stable_g)
    pv_stage2 = tv_stable / (1.0 + wacc) ** transition_years

    return pv_stage1 + pv_stage2


def tv_gordon_growth(
    terminal_ufcf: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    """
    Alias for gordon_growth_tv().
    Compute terminal value using Gordon Growth Model.
    Reference: Architecture Plan Part 46.
    """
    return gordon_growth_tv(terminal_ufcf, wacc, terminal_growth)


def tv_nopat_reinvestment(
    terminal_nopat: float,
    reinvestment_rate: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    """
    Alias for gordon_growth_tv_from_nopat().
    Compute terminal value from NOPAT and reinvestment rate.
    Reference: Architecture Plan Part 46.
    """
    return gordon_growth_tv_from_nopat(terminal_nopat, reinvestment_rate, wacc, terminal_growth)


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → gordon_growth_tv
compute_terminal_value_gordon = gordon_growth_tv

#: Canonical checklist name → exit_multiple_tv
compute_terminal_value_exit_multiple = exit_multiple_tv

#: Canonical checklist name → gordon_growth_tv_from_nopat
compute_tv_nopat_reinvestment = gordon_growth_tv_from_nopat

#: Canonical checklist name → pv_terminal_value
compute_pv_terminal_value = pv_terminal_value


# ─────────────────────────────────────────────────────────────────────────────
# TV Cross-check helpers  (Macabacus standard — "Cross-Checking Terminal Value")
# ─────────────────────────────────────────────────────────────────────────────

def compute_implied_ebitda_multiple(
    tv: float,
    ebitda_n: float,
) -> float:
    """
    Implied EV/EBITDA multiple from a given terminal value.

        implied_multiple = TV / EBITDA_n

    Used to cross-check a GGM-derived TV against prevailing market multiples:
    if the GGM TV implies an EBITDA multiple far from current trading comps,
    the growth rate assumption should be revisited.

    Also used to cross-check an exit-multiple TV — compute the GGM TV and then
    calculate what multiple it implies, comparing against the input multiple.

    Args:
        tv      : Terminal value ($M) — from either GGM or exit multiple method.
        ebitda_n: Terminal-year EBITDA ($M, must be positive).

    Returns:
        float — implied EV/EBITDA multiple (e.g. 10.5 = 10.5×).
                Returns 0.0 if ebitda_n ≤ 0.

    Reference: Macabacus "Cross-Checking Terminal Value for Accuracy".
    """
    if ebitda_n <= 0:
        return 0.0
    return tv / ebitda_n


def compute_tv_crosscheck(
    tv_primary: float,
    terminal_ufcf: float,
    wacc: float,
    ebitda_n: float,
    ev_ebitda_multiple_comps: float | None = None,
) -> dict:
    """
    Full TV cross-check per Macabacus best-practice.

    Computes:
      1. implied_g        — perpetuity growth rate implied by the TV
      2. implied_multiple — EV/EBITDA multiple implied by the TV
      3. delta_multiple   — implied multiple vs. comps multiple (if provided)
      4. warning          — flag if |implied_g| > 5% or multiple is extreme

    Args:
        tv_primary               : Primary TV value ($M).
        terminal_ufcf            : Last forecast year UFCF used as TV base ($M).
        wacc                     : WACC used in the DCF.
        ebitda_n                 : Terminal-year EBITDA ($M).
        ev_ebitda_multiple_comps : Optional — current comps EV/EBITDA (e.g. 8.5).

    Returns:
        dict with keys:
          'tv', 'implied_g', 'implied_multiple', 'delta_multiple', 'warnings'

    Reference: Macabacus "Cross-Checking Terminal Value for Accuracy".
    """
    warnings_list: list[str] = []

    # 1) Implied perpetuity growth (NIKE convention: TV = UFCF / (WACC − g))
    implied_g = implied_terminal_growth(tv_primary, terminal_ufcf, wacc)

    # 2) Implied EV/EBITDA
    imp_mult = compute_implied_ebitda_multiple(tv_primary, ebitda_n)

    # 3) Delta vs. comps
    delta_mult: float | None = None
    if ev_ebitda_multiple_comps is not None and ev_ebitda_multiple_comps > 0:
        delta_mult = imp_mult - ev_ebitda_multiple_comps
        if abs(delta_mult) > 3.0:
            warnings_list.append(
                f"TV-implied multiple ({imp_mult:.1f}x) deviates from comps "
                f"({ev_ebitda_multiple_comps:.1f}x) by {delta_mult:+.1f}x — "
                "review terminal assumptions."
            )

    # 4) Sanity on implied_g
    if implied_g > 0.05:
        warnings_list.append(
            f"Implied perpetuity growth ({implied_g:.2%}) exceeds 5% — "
            "this implies the company grows faster than the economy forever."
        )
    if implied_g < 0:
        warnings_list.append(
            f"Implied perpetuity growth ({implied_g:.2%}) is negative — "
            "review TV or UFCF assumptions."
        )

    return {
        "tv":               tv_primary,
        "implied_g":        implied_g,
        "implied_multiple": imp_mult,
        "delta_multiple":   delta_mult,
        "warnings":         warnings_list,
    }
