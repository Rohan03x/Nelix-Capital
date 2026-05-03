"""
webapp/data/reverse_dcf.py
──────────────────────────
Reverse DCF: given a stock price, compute the implied terminal growth rate
(and implied WACC) that makes the DCF IV equal the current market price.

All root-finding is via bisection — scipy is NEVER used.
"""

from __future__ import annotations


def _tv_formula(terminal_ufcf: float, wacc: float, g: float) -> float:
    """Gordon-growth terminal value: TV = UFCF*(1+g) / (WACC - g)."""
    spread = wacc - g
    if spread <= 0:
        return float("inf")
    return terminal_ufcf * (1 + g) / spread


def _iv_at_g(
    g: float,
    wacc: float,
    terminal_ufcf: float,
    pv_ufcfs: float,
    net_debt: float,
    diluted_shares: float,
    discount_years: float = 7.0,
) -> float:
    """Compute intrinsic value per share for a given terminal growth rate."""
    tv = _tv_formula(terminal_ufcf, wacc, g)
    if tv == float("inf"):
        return float("inf")
    pv_tv = tv / (1 + wacc) ** discount_years
    ev = pv_ufcfs + pv_tv
    equity = ev - net_debt
    if diluted_shares <= 0:
        return float("inf")
    return equity / diluted_shares


def _bisect(
    f,
    lo: float,
    hi: float,
    target: float,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """
    Bisection root-finder for g* s.t. f(g*) = target.
    f is assumed monotonically increasing in its argument.
    Returns None if no root in [lo, hi].
    """
    f_lo = f(lo) - target
    f_hi = f(hi) - target

    if f_lo > 0 or f_hi < 0:
        # no root in range (or monotonicity violated)
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = f(mid) - target
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def compute_reverse_dcf(data: dict) -> dict:
    """
    Compute reverse DCF for the company in *data*.

    Returns a dict with:
      implied_g         — terminal growth rate implied by current price (%)
      implied_g_bear    — implied g at +1 WACC (stress scenario)
      implied_wacc      — WACC at which model IV ≈ current price (g fixed at base)
      market_implied_growth_pct — same as implied_g, named for template
      model_g           — model's base-case terminal growth rate (%)
      model_wacc        — model's base-case WACC (%)
      g_spread_bps      — (model_g − implied_g) × 100  (how much the market is
                          discounting vs the model's terminal growth assumption)
      price             — current market price
      narrative         — 1-2 sentence interpretation for the UI
      sensitivity       — list of {g, iv} pairs for the slider chart
    """
    price   = data.get("price", 0)
    wacc_pct = data.get("wacc", 9.0)
    base_g  = data.get("terminal_growth", 2.5)
    pv_ufcfs = data.get("pv_ufcfs", 0)
    pv_terminal = data.get("pv_terminal", 0)
    net_debt = data.get("net_debt", 0)
    diluted_shares = data.get("diluted_shares", 1)

    wacc = wacc_pct / 100

    # Reconstruct terminal_ufcf from stored pv_terminal.
    # Main DCF: pv_terminal = TV / (1+wacc)^7, TV = UFCF*(1+g)/(wacc-g)
    # → terminal_ufcf = pv_terminal*(1+wacc)^7*(wacc-g)/(1+g)
    base_g_dec = base_g / 100
    tv_at_base = pv_terminal * (1 + wacc) ** 7
    terminal_ufcf = tv_at_base * (wacc - base_g_dec) / (1 + base_g_dec)

    def iv_fn(g_pct):
        return _iv_at_g(
            g=g_pct / 100,
            wacc=wacc,
            terminal_ufcf=terminal_ufcf,
            pv_ufcfs=pv_ufcfs,
            net_debt=net_debt,
            diluted_shares=diluted_shares,
        )

    # Find implied g at current price
    implied_g = _bisect(
        lambda g: iv_fn(g) - price,
        lo=-5.0,
        hi=wacc_pct - 0.5,
        target=0.0,
    )

    if implied_g is None:
        # Price is below even the lowest-g IV → market is implying negative growth
        implied_g = -5.0
        auto_bounded = True
    else:
        auto_bounded = False

    implied_g_rounded = round(implied_g, 2)

    # Implied WACC at current price (g fixed at base_g)
    def iv_fn_wacc(wacc_pct_local):
        local_wacc = wacc_pct_local / 100
        local_g    = base_g / 100
        spread = local_wacc - local_g
        if spread <= 0:
            return float("inf")
        tv = terminal_ufcf * (1 + local_g) / spread
        pv_tv = tv / (1 + local_wacc) ** 7
        ev = pv_ufcfs + pv_tv
        equity = ev - net_debt
        return equity / diluted_shares

    implied_wacc = _bisect(
        lambda w: iv_fn_wacc(w) - price,
        lo=base_g + 0.5,
        hi=30.0,
        target=0.0,
    )
    implied_wacc_rounded = round(implied_wacc, 2) if implied_wacc else wacc_pct

    g_spread_bps = round((base_g - implied_g_rounded) * 100)

    # Bear scenario: implied_g at WACC + 1pp
    def iv_fn_bear(g_pct):
        return _iv_at_g(
            g=g_pct / 100,
            wacc=wacc + 0.01,
            terminal_ufcf=terminal_ufcf,
            pv_ufcfs=pv_ufcfs,
            net_debt=net_debt,
            diluted_shares=diluted_shares,
        )

    implied_g_bear = _bisect(
        lambda g: iv_fn_bear(g) - price,
        lo=-5.0,
        hi=wacc_pct + 1 - 0.5,
        target=0.0,
    )
    implied_g_bear_rounded = round(implied_g_bear, 2) if implied_g_bear else implied_g_rounded

    # Sensitivity curve: IV at each g from -2% to wacc-0.5%
    curve = []
    g_step_start = max(-2.0, round(implied_g_rounded - 3, 1))
    g_step_end   = min(wacc_pct - 0.5, g_step_start + 7)
    steps = 20
    step = (g_step_end - g_step_start) / steps
    g_val = g_step_start
    while g_val <= g_step_end + 0.001:
        iv = iv_fn(round(g_val, 2))
        if iv != float("inf") and iv > 0:
            curve.append({"g": round(g_val, 2), "iv": round(iv, 2)})
        g_val += step

    # Interpretation narrative
    if price <= 0:
        narrative = "Market price data unavailable."
    elif implied_g < 0:
        narrative = (
            f"The market is pricing in a terminal growth rate of {implied_g_rounded:.1f}% — "
            f"implying long-term revenue contraction. This is {"very pessimistic" if implied_g < -2 else "pessimistic"} "
            f"relative to our base-case assumption of {base_g:.1f}%."
        )
    elif implied_g < base_g - 1.5:
        gap = base_g - implied_g_rounded
        narrative = (
            f"The market implies terminal growth of {implied_g_rounded:.1f}%, "
            f"a full {gap:.1f}pp below our {base_g:.1f}% base case. "
            f"This gap represents the market's pessimism about long-run earnings power — "
            f"or the implied WACC of {implied_wacc_rounded:.1f}% suggests higher perceived risk."
        )
    elif implied_g < base_g - 0.5:
        gap = base_g - implied_g_rounded
        narrative = (
            f"The market prices in {implied_g_rounded:.1f}% terminal growth vs. our {base_g:.1f}% base case "
            f"— a moderate {gap:.1f}pp discount. At an implied WACC of {implied_wacc_rounded:.1f}%, "
            f"the stock looks {"undervalued" if implied_wacc_rounded > wacc_pct + 0.5 else "fairly priced"} on our assumptions."
        )
    elif abs(implied_g - base_g) <= 0.5:
        narrative = (
            f"The market is broadly aligned with our base-case terminal growth assumption of {base_g:.1f}%. "
            f"Implied WACC of {implied_wacc_rounded:.1f}% vs. our {wacc_pct:.1f}% model WACC — "
            f"the stock appears fairly valued on current consensus."
        )
    else:
        premium = implied_g_rounded - base_g
        narrative = (
            f"The market is pricing in {implied_g_rounded:.1f}% terminal growth — "
            f"{premium:.1f}pp above our conservative {base_g:.1f}% base case. "
            f"This suggests the market is paying a premium for long-run optionality."
        )

    return {
        "implied_g":                   implied_g_rounded,
        "implied_g_bear":              implied_g_bear_rounded,
        "implied_wacc":                implied_wacc_rounded,
        "market_implied_growth_pct":   implied_g_rounded,
        "model_g":                     base_g,
        "model_wacc":                  wacc_pct,
        "g_spread_bps":                g_spread_bps,
        "price":                       price,
        "iv_at_model_g":               round(iv_fn(base_g), 2),
        "narrative":                   narrative,
        "sensitivity_curve":           curve,
        "auto_bounded":                auto_bounded,
    }
