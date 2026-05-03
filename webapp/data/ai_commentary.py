"""
webapp/data/ai_commentary.py
==============================
Rule-based AI analyst commentary generator.
No external API key required — pure Python logic driven by model data.

Generates:
  - company_summary: One-paragraph company description with key metrics
  - valuation_summary: What the DCF says about fair value
  - bull_narrative: Why the bull case could play out
  - bear_narrative: Why the bear case could play out
  - where_wrong: Top-3 ways the model could be wrong
  - why_market_disagrees: Explanation of the market vs. model price gap
  - verify_checklist: Actionable pre-investment checks
  - wacc_commentary: Interpretation of WACC components
  - earnings_quality_commentary: Interpretation of cash conversion
"""

from __future__ import annotations


def _money_symbol(data: dict) -> str:
    return data.get("display_currency_symbol") or data.get("currency_symbol") or "$"


def _money(value: float, symbol: str, digits: int = 2) -> str:
    return f"{symbol}{value:,.{digits}f}"


def _money_m(value: float, symbol: str) -> str:
    if abs(value) >= 1000:
        return f"{symbol}{value / 1000:.1f}B"
    return f"{symbol}{value:.0f}M"


def generate_commentary(data: dict) -> dict:
    """
    Master function: takes a dashboard data dict and returns the full
    ai_commentary sub-dict.
    """
    return {
        "company_summary":       _company_summary(data),
        "valuation_summary":     _valuation_summary(data),
        "bull_narrative":        _bull_narrative(data),
        "bear_narrative":        _bear_narrative(data),
        "where_wrong":           _where_wrong(data),
        "why_market_disagrees":  _why_market_disagrees(data),
        "verify_checklist":      _verify_checklist(data),
        "wacc_commentary":       _wacc_commentary(data),
        "earnings_quality_note": _earnings_quality_note(data),
        "consensus_delta_note":  _consensus_delta_note(data),
    }


# ─── Individual generators ────────────────────────────────────────────────────

def _company_summary(d: dict) -> str:
    name  = d.get("company_name", d.get("ticker", "The company"))
    sect  = d.get("sector", "")
    symbol = _money_symbol(d)
    rev   = d.get("historical", {}).get("revenue", [])
    rev_s = _money_m(rev[-1], symbol) if rev else "N/A"
    mc    = d.get("market_cap", 0)
    mc_s  = _money_m(mc, symbol)
    desc  = d.get("description", "")
    if desc:
        return desc
    return (
        f"{name} ({d.get('ticker','')}) is a {sect} company with approximately "
        f"{rev_s} in trailing revenue and a market cap of {mc_s}. "
        f"The stock is analysed using a 7-year discounted cash flow model."
    )


def _valuation_summary(d: dict) -> str:
    name  = d.get("company_name", d.get("ticker", "The company"))
    symbol = _money_symbol(d)
    price = d.get("price", 0)
    iv    = d.get("intrinsic_value", 0)
    up    = d.get("upside_pct", 0)
    wacc  = d.get("wacc", 0)
    g     = d.get("terminal_growth", 0)
    tv    = d.get("tv_pct", 0)
    conf  = d.get("confidence_score", 0)
    direction = "premium" if up >= 0 else "discount"
    pct_abs   = abs(up)
    quality   = "high-quality" if conf >= 70 else "moderate-quality" if conf >= 50 else "lower-quality"

    lines = [
        f"Our {quality} DCF model (confidence {conf}/100) values {name} at "
        f"{_money(iv, symbol)}/share — a {pct_abs:.1f}% {direction} to the current market price of {_money(price, symbol)}.",
        f"At WACC = {wacc}% and terminal growth = {g}%, terminal value represents "
        f"{tv:.1f}% of enterprise value.",
    ]
    if tv > 70:
        lines.append(
            "The elevated terminal value concentration means small changes in WACC or g "
            "will materially affect the output. Treat the point estimate as a range."
        )
    elif tv < 40:
        lines.append(
            "With a relatively modest terminal value share, near-term cash flow visibility "
            "is the primary value driver — improving estimate reliability."
        )
    return " ".join(lines)


def _bull_narrative(d: dict) -> str:
    sc   = d.get("scenarios", {}).get("bull", {})
    name = d.get("company_name", d.get("ticker", "The company"))
    symbol = _money_symbol(d)
    if sc.get("narrative"):
        return sc["narrative"]
    iv    = sc.get("iv", d.get("intrinsic_value", 0))
    wacc  = sc.get("wacc", d.get("wacc", 0))
    g     = sc.get("g", d.get("terminal_growth", 0))
    m     = sc.get("margin_target", d.get("ebit_margin_target", 0))
    up    = sc.get("upside", 0)
    return (
        f"In a bull case (WACC={wacc}%, g={g}%, margin={m}%), {name} is worth "
        f"{_money(iv, symbol)}/share (+{up:.1f}%). This assumes above-consensus revenue growth, "
        f"accelerating margin expansion, and stable/declining interest rates."
    )


def _bear_narrative(d: dict) -> str:
    sc   = d.get("scenarios", {}).get("bear", {})
    name = d.get("company_name", d.get("ticker", "The company"))
    symbol = _money_symbol(d)
    if sc.get("narrative"):
        return sc["narrative"]
    iv   = sc.get("iv", d.get("intrinsic_value", 0))
    wacc = sc.get("wacc", d.get("wacc", 0))
    g    = sc.get("g", d.get("terminal_growth", 0))
    m    = sc.get("margin_target", d.get("ebit_margin_base", 0))
    up   = sc.get("upside", 0)
    return (
        f"In a bear case (WACC={wacc}%, g={g}%, margin={m}%), {name} is worth "
        f"{_money(iv, symbol)}/share ({up:.1f}%). This assumes margin compression, slowing revenue, "
        f"and elevated risk premium due to macro or competitive headwinds."
    )


def _where_wrong(d: dict) -> list[str]:
    """Return 3–5 ways the model could be wrong."""
    items = []
    symbol = _money_symbol(d)
    tv    = d.get("tv_pct", 0)
    wacc  = d.get("wacc", 0)
    g     = d.get("terminal_growth", 0)
    spread = wacc - g
    beta  = d.get("beta", 1.0)
    ebit_target = d.get("ebit_margin_target", 0)
    ebit_base   = d.get("ebit_margin_base", 0)
    margin_lift = ebit_target - ebit_base

    # Terminal value sensitivity
    if tv > 65:
        items.append(
            f"Terminal value is {tv:.1f}% of EV — if the true long-run growth rate is 50bp "
            f"lower than assumed ({g}%), intrinsic value falls significantly."
        )

    # WACC-g spread
    if spread < 3:
        items.append(
            f"The WACC–g spread of {spread:.1f}% is narrow. A 50bp WACC increase "
            f"(interest rate shock or risk re-rating) compresses fair value materially."
        )
    else:
        items.append(
            f"Beta of {beta}× may understate true risk if the company faces structural headwinds. "
            f"A higher beta (e.g. {beta + 0.3:.2f}×) would lift WACC by ~{0.3 * 5.2:.1f}% and "
            f"reduce fair value by an estimated {_money(d.get('intrinsic_value', 0) * 0.06, symbol, 0)}/share."
        )

    # Margin expansion
    if margin_lift > 1.5:
        items.append(
            f"The model assumes {margin_lift:.1f}pp of EBIT margin expansion by Year 7. "
            f"If competitive dynamics prevent this (pricing pressure, cost inflation), "
            f"every 100bp shortfall reduces intrinsic value by ~{_money(d.get('intrinsic_value', 0) * 0.08, symbol, 0)}/share."
        )

    # Revenue growth
    rg = d.get("revenue_growth_near", 0)
    if rg > 8:
        items.append(
            f"Near-term revenue growth of {rg:.1f}% is above the sector median. "
            f"A slowdown to 4–5% (macro recession or share loss) could cut Year 3–5 FCF by 15–20%."
        )
    elif rg < 3:
        items.append(
            f"Revenue growth of only {rg:.1f}% leaves little buffer if costs inflate faster — "
            f"margin compression risk is elevated at low top-line growth rates."
        )
    else:
        items.append(
            f"Revenue CAGR of {rg:.1f}% is broadly in-line with consensus, but positive guidance "
            f"surprises (or misses) in the first 2 forecast years dominate near-term value inflection."
        )

    # Data quality
    conf = d.get("confidence_score", 75)
    if conf < 60:
        items.append(
            f"Model confidence is {conf}/100 — limited historical data or unstable margins "
            f"make the DCF less reliable. Apply a wider valuation range."
        )

    return items[:5]


def _why_market_disagrees(d: dict) -> str:
    """Explain the gap between model IV and current market price."""
    symbol = _money_symbol(d)
    price = d.get("price", 0)
    iv    = d.get("intrinsic_value", 0)
    up    = d.get("upside_pct", 0)
    name  = d.get("company_name", d.get("ticker", "the company"))
    rdcf  = d.get("reverse_dcf") or {}
    imp_g = rdcf.get("implied_g")

    if abs(up) < 5:
        return (
            f"The market price of {_money(price, symbol)} and our intrinsic value of {_money(iv, symbol)} are broadly "
            f"in agreement — within a ±5% band. This suggests the market is already pricing in "
            f"a fundamentally similar view to the model. Minor discrepancy may reflect timing, "
            f"liquidity premium, or short-term sentiment."
        )

    direction = "undervalued" if up > 0 else "overvalued"
    pct_abs   = abs(up)
    diff      = abs(iv - price)

    lines = [
        f"Our model shows {name} as {direction} by {pct_abs:.1f}% ({_money(diff, symbol)}/share gap).",
    ]

    if imp_g is not None:
        model_g = rdcf.get("model_g", d.get("terminal_growth", 2.5))
        if up > 0:
            lines.append(
                f"The reverse DCF shows the market is implying a terminal growth rate of "
                f"{imp_g:.2f}% vs. our model's {model_g:.2f}%. "
                f"The market appears to be discounting a slower long-run growth trajectory."
            )
        else:
            lines.append(
                f"The reverse DCF shows the market prices in {imp_g:.2f}% terminal growth "
                f"vs. our model's {model_g:.2f}%. "
                f"Either the market expects stronger growth than our model, or it is applying "
                f"a lower risk premium (higher multiple expansion expectations)."
            )

    # Add possible explanations
    if up > 10:
        lines.append(
            "Possible reasons the market is more cautious: near-term execution risk, "
            "rising macro uncertainty, or sector rotation out of growth equities. "
            "The stock may re-rate upward as catalysts materialise."
        )
    elif up < -10:
        lines.append(
            "Possible reasons the market is more optimistic: stronger growth expectations, "
            "AI/platform premium, or anticipated margin expansion not captured in our base case. "
            "The market may be pricing in optionality our DCF does not fully credit."
        )

    return " ".join(lines)


def _verify_checklist(d: dict) -> list[str]:
    """Return 5–7 actionable pre-investment verification items."""
    checks = list(d.get("analyst_view", {}).get("verify_before_use", []))
    if len(checks) >= 5:
        return checks[:7]

    # Generate generic checks
    name  = d.get("company_name", d.get("ticker", "the company"))
    wacc  = d.get("wacc", 0)
    g     = d.get("terminal_growth", 0)
    tv    = d.get("tv_pct", 0)
    checks = [
        f"Confirm the most recent quarterly results for {name} are reflected in this model.",
        f"Verify analyst consensus revenue and EPS estimates match FY+1 model output.",
        f"Cross-check WACC of {wacc}% against current 10-year Treasury rate and beta.",
        f"Review terminal growth rate of {g}% against long-run GDP/inflation forecasts.",
    ]
    if tv > 65:
        checks.append(
            f"Terminal value is {tv:.1f}% of EV — stress-test g at g−1% and g+0.5%."
        )
    checks.append("Check for any pending M&A, spin-off, or capital structure changes.")
    checks.append("Review insider buying/selling activity over the past 6 months.")
    return checks[:7]


def _wacc_commentary(d: dict) -> str:
    """Return a sentence or two interpreting WACC components."""
    rf    = d.get("risk_free_rate", 4.4)
    beta  = d.get("beta", 1.0)
    erp   = d.get("erp", 5.2)
    size  = d.get("size_premium", 0.0)
    ke    = d.get("cost_of_equity", 0)
    wacc  = d.get("wacc", 0)
    ew    = d.get("equity_weight", 0)

    if not ke:
        ke = rf + beta * erp + size

    parts = [
        f"WACC of {wacc}% is built from: Rf={rf}% (10-yr Treasury), "
        f"β={beta}× × ERP={erp}% = equity risk {beta*erp:.1f}%, "
    ]
    if size > 0:
        parts.append(f"size premium {size}%, ")
    parts.append(
        f"giving Ke={ke:.1f}% weighted at {ew:.0f}% equity. "
    )
    if ew >= 90:
        parts.append("The capital structure is predominantly equity-funded, so WACC is nearly equal to Ke.")
    else:
        kd = d.get("cost_of_debt_post", 0)
        dw = d.get("debt_weight", 0)
        parts.append(f"After-tax cost of debt of {kd:.1f}% weighted at {dw:.0f}% debt brings WACC below Ke.")
    return "".join(parts)


def _earnings_quality_note(d: dict) -> str:
    """Summarise earnings quality from the earnings_quality dict if present."""
    eq = d.get("earnings_quality") or {}
    if not eq.get("available"):
        hist = d.get("historical", {})
        fcf  = hist.get("fcf", [])
        ni   = hist.get("net_income", [])
        if fcf and ni and len(fcf) == len(ni):
            avg_conv = sum(f / max(n, 1) for f, n in zip(fcf[-5:], ni[-5:]) if n > 0) / 5
            if avg_conv >= 1.1:
                return f"FCF/NI averaged {avg_conv:.2f}× over the last 5 years — strong cash conversion quality."
            elif avg_conv >= 0.7:
                return f"FCF/NI averaged {avg_conv:.2f}× over the last 5 years — moderate cash conversion quality."
            else:
                return f"FCF/NI averaged {avg_conv:.2f}× over the last 5 years — low cash conversion; accruals review recommended."
        return "Earnings quality data not available for this period."
    return eq.get("quality_note", "")


def _consensus_delta_note(d: dict) -> str:
    """Compare model Year-1 revenue vs. analyst consensus."""
    cons = d.get("analyst_consensus") or {}
    if not cons:
        return ""
    cons_rev  = cons.get("revenue_y1_consensus")
    model_rev = cons.get("revenue_y1_model")
    if cons_rev and model_rev:
        delta = (model_rev - cons_rev) / cons_rev * 100
        direction = "above" if delta > 0 else "below"
        abs_d = abs(delta)
        if abs_d < 2:
            return (
                f"Model Year-1 revenue of ${model_rev:,.0f}M is within 2% of analyst consensus "
                f"(${cons_rev:,.0f}M) — estimates are well-anchored."
            )
        return (
            f"Model Year-1 revenue of ${model_rev:,.0f}M is {abs_d:.1f}% {direction} "
            f"analyst consensus (${cons_rev:,.0f}M). "
            + ("Verify the growth drivers behind the bullish relative stance." if delta > 0
               else "Consider whether the conservative stance is justified by recent guidance.")
        )
    buy  = cons.get("buy_count", 0)
    hold = cons.get("hold_count", 0)
    sell = cons.get("sell_count", 0)
    total = buy + hold + sell
    if total:
        buy_pct = buy / total * 100
        return (
            f"Analyst consensus: {buy} Buy / {hold} Hold / {sell} Sell "
            f"({buy_pct:.0f}% bullish). "
            + ("Strong buy-side conviction aligns with our undervalued view." if buy_pct >= 60
               else "Mixed consensus suggests uncertainty remains.")
        )
    return ""
