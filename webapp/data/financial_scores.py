"""
webapp/data/financial_scores.py
================================
Altman Z-Score and Piotroski F-Score computation from raw financial statement data.

Altman Z-Score (public non-financial firms, 1968):
  Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
  X1 = Working Capital / Total Assets
  X2 = Retained Earnings / Total Assets
  X3 = EBIT / Total Assets
  X4 = Market Cap / Total Liabilities
  X5 = Revenue / Total Assets
  Zones: < 1.81 Distress | 1.81–2.99 Grey | >= 3.0 Safe

Piotroski F-Score (9 binary signals):
  Profitability (4): ROA>0, CFO>0, ΔROA>0, CFO>NI (accruals)
  Leverage/Liquidity (3): ΔLeverage<0, ΔCurrent Ratio>0, no new shares
  Efficiency (2): ΔGross Margin>0, ΔAsset Turnover>0
  Score: 0–3 weak | 4–6 average | 7–9 strong
"""

from __future__ import annotations
from typing import Optional


def compute_altman_z(
    working_capital: float,
    total_assets: float,
    retained_earnings: float,
    ebit: float,
    market_cap: float,
    total_liabilities: float,
    revenue: float,
) -> dict:
    """
    Compute Altman Z-Score for public non-financial firms.

    All monetary values in the same unit (e.g. $M).

    Returns a dict with score, zone, zone_color, zone_code, x1..x5, and a narrative.
    """
    if total_assets <= 0:
        return _altman_unavailable("Total assets = 0; cannot compute Z-Score.")
    if total_liabilities <= 0:
        total_liabilities = max(total_assets - working_capital, 1.0)

    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    x4 = market_cap / max(total_liabilities, 1.0)
    x5 = revenue / total_assets

    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

    if z >= 3.0:
        zone, zone_color, zone_code = "Safe Zone", "green", "safe"
        narrative = (
            f"Z-Score of {z:.2f} is in the Safe Zone (≥3.0). "
            "The company shows low financial distress risk with healthy asset utilization."
        )
    elif z >= 1.81:
        zone, zone_color, zone_code = "Grey Zone", "amber", "grey"
        narrative = (
            f"Z-Score of {z:.2f} is in the Grey Zone (1.81–2.99). "
            "Some financial stress signals present; monitor leverage and profitability trends."
        )
    else:
        zone, zone_color, zone_code = "Distress Zone", "red", "distress"
        narrative = (
            f"Z-Score of {z:.2f} is in the Distress Zone (<1.81). "
            "Elevated bankruptcy risk signals. Verify balance sheet health and cash runway."
        )

    return {
        "score": round(z, 2),
        "zone": zone,
        "zone_color": zone_color,
        "zone_code": zone_code,
        "narrative": narrative,
        "components": {
            "x1_wc_assets":      round(x1, 4),
            "x2_re_assets":      round(x2, 4),
            "x3_ebit_assets":    round(x3, 4),
            "x4_mktcap_liab":    round(x4, 4),
            "x5_rev_assets":     round(x5, 4),
        },
        "weights": {"x1": 1.2, "x2": 1.4, "x3": 3.3, "x4": 0.6, "x5": 1.0},
        "available": True,
    }


def _altman_unavailable(reason: str) -> dict:
    return {
        "score": None,
        "zone": "Unavailable",
        "zone_color": "grey",
        "zone_code": "na",
        "narrative": reason,
        "components": {},
        "weights": {},
        "available": False,
    }


def compute_piotroski_f(
    # Current year
    net_income: float,
    total_assets: float,
    operating_cash_flow: float,
    long_term_debt: float,
    current_assets: float,
    current_liabilities: float,
    shares_outstanding: float,
    gross_profit: float,
    revenue: float,
    # Prior year
    net_income_prev: float,
    total_assets_prev: float,
    long_term_debt_prev: float,
    current_assets_prev: float,
    current_liabilities_prev: float,
    shares_prev: float,
    gross_profit_prev: float,
    revenue_prev: float,
) -> dict:
    """
    Compute Piotroski F-Score (9 binary tests).

    Returns a dict with score (0-9), zone, zone_color, and per-test breakdown.
    """
    tests = []

    # ── Profitability signals (4) ────────────────────────────────────────────
    roa_curr = net_income / max(total_assets, 1)
    roa_prev = net_income_prev / max(total_assets_prev, 1)

    f1 = int(roa_curr > 0)
    tests.append({
        "name": "ROA > 0",
        "group": "Profitability",
        "pass": bool(f1),
        "detail": f"ROA = {roa_curr*100:.1f}%",
    })

    f2 = int(operating_cash_flow > 0)
    tests.append({
        "name": "Operating Cash Flow > 0",
        "group": "Profitability",
        "pass": bool(f2),
        "detail": f"OCF = ${operating_cash_flow:,.0f}M",
    })

    f3 = int(roa_curr > roa_prev)
    tests.append({
        "name": "Improving ROA (ΔROA > 0)",
        "group": "Profitability",
        "pass": bool(f3),
        "detail": f"ROA {roa_prev*100:.1f}% → {roa_curr*100:.1f}%",
    })

    # Accruals: cash conversion — OCF > NI
    f4 = int(operating_cash_flow / max(total_assets, 1) > roa_curr)
    tests.append({
        "name": "Cash Earnings Quality (OCF > NI)",
        "group": "Profitability",
        "pass": bool(f4),
        "detail": f"OCF/Assets {operating_cash_flow/max(total_assets,1)*100:.1f}% vs ROA {roa_curr*100:.1f}%",
    })

    # ── Leverage / Liquidity signals (3) ────────────────────────────────────
    lev_curr = long_term_debt / max(total_assets, 1)
    lev_prev = long_term_debt_prev / max(total_assets_prev, 1)
    f5 = int(lev_curr < lev_prev)
    tests.append({
        "name": "Decreasing Leverage (ΔLev < 0)",
        "group": "Leverage",
        "pass": bool(f5),
        "detail": f"LTD/Assets {lev_prev*100:.1f}% → {lev_curr*100:.1f}%",
    })

    cr_curr = current_assets / max(current_liabilities, 1)
    cr_prev = current_assets_prev / max(current_liabilities_prev, 1)
    f6 = int(cr_curr > cr_prev)
    tests.append({
        "name": "Improving Current Ratio",
        "group": "Leverage",
        "pass": bool(f6),
        "detail": f"Current Ratio {cr_prev:.2f}× → {cr_curr:.2f}×",
    })

    f7 = int(shares_outstanding <= shares_prev)
    tests.append({
        "name": "No Share Dilution",
        "group": "Leverage",
        "pass": bool(f7),
        "detail": f"Shares {shares_prev:,.0f}M → {shares_outstanding:,.0f}M",
    })

    # ── Operating Efficiency signals (2) ────────────────────────────────────
    gm_curr = gross_profit / max(revenue, 1)
    gm_prev = gross_profit_prev / max(revenue_prev, 1)
    f8 = int(gm_curr > gm_prev)
    tests.append({
        "name": "Improving Gross Margin",
        "group": "Efficiency",
        "pass": bool(f8),
        "detail": f"Gross Margin {gm_prev*100:.1f}% → {gm_curr*100:.1f}%",
    })

    at_curr = revenue / max(total_assets, 1)
    at_prev = revenue_prev / max(total_assets_prev, 1)
    f9 = int(at_curr > at_prev)
    tests.append({
        "name": "Improving Asset Turnover",
        "group": "Efficiency",
        "pass": bool(f9),
        "detail": f"Asset Turnover {at_prev:.2f}× → {at_curr:.2f}×",
    })

    score = f1 + f2 + f3 + f4 + f5 + f6 + f7 + f8 + f9

    if score >= 7:
        zone, zone_color, zone_code = "Strong", "green", "strong"
        narrative = (
            f"F-Score of {score}/9 — Strong fundamental quality. "
            "High probability of outperformance; company shows improving financial health on most dimensions."
        )
    elif score >= 4:
        zone, zone_color, zone_code = "Average", "amber", "average"
        narrative = (
            f"F-Score of {score}/9 — Average quality. "
            "Mixed signals; selective monitoring warranted. Not a clear long or short signal."
        )
    else:
        zone, zone_color, zone_code = "Weak", "red", "weak"
        narrative = (
            f"F-Score of {score}/9 — Weak fundamentals. "
            "Multiple deteriorating signals; typical of value-trap or distressed situations."
        )

    return {
        "score": score,
        "zone": zone,
        "zone_color": zone_color,
        "zone_code": zone_code,
        "narrative": narrative,
        "tests": tests,
        "groups": {
            "Profitability": sum(1 for t in tests if t["group"] == "Profitability" and t["pass"]),
            "Leverage":      sum(1 for t in tests if t["group"] == "Leverage" and t["pass"]),
            "Efficiency":    sum(1 for t in tests if t["group"] == "Efficiency" and t["pass"]),
        },
        "available": True,
    }


def compute_dupont(
    years: list[int],
    net_income: list[float],
    revenue: list[float],
    total_assets: list[float],
    equity: list[float],
) -> dict:
    """
    3-factor DuPont decomposition: ROE = Net Margin × Asset Turnover × Leverage
    Returns per-year lists suitable for Chart.js.
    Note: this computes ROE (Net Income / Equity), not ROIC.
    """
    n = min(len(years), len(net_income), len(revenue), len(total_assets), len(equity))
    net_margin, asset_turnover, leverage, roe_dupont = [], [], [], []
    for i in range(n):
        nm  = net_income[i] / max(revenue[i], 1) * 100          # %
        at  = revenue[i] / max(total_assets[i], 1)              # ×
        lev = total_assets[i] / max(equity[i], 0.001)           # ×
        roe = nm / 100 * at * lev * 100                         # %
        net_margin.append(round(nm, 1))
        asset_turnover.append(round(at, 3))
        leverage.append(round(lev, 2))
        roe_dupont.append(round(roe, 1))

    return {
        "years":           list(years[:n]),
        "net_margin":      net_margin,
        "asset_turnover":  asset_turnover,
        "leverage":        leverage,
        "roe_dupont":      roe_dupont,
        "available": True,
    }


def compute_earnings_quality(
    years: list[int],
    net_income: list[float],
    operating_cf: list[float],
    fcf: list[float],
) -> dict:
    """
    Earnings quality: convergence of NI, OCF, and FCF.
    Cash conversion ratio = OCF / NI (>1 means cash earnings beat reported NI).
    """
    n = min(len(years), len(net_income), len(operating_cf), len(fcf))
    ccr = []
    for i in range(n):
        if net_income[i] and net_income[i] != 0:
            ccr.append(round(operating_cf[i] / net_income[i], 2))
        else:
            ccr.append(None)

    avg_ccr = sum(c for c in ccr if c is not None) / max(sum(1 for c in ccr if c is not None), 1)

    if avg_ccr >= 1.1:
        quality_note = "High quality: Operating cash flow consistently exceeds reported earnings — strong cash conversion."
        quality_color = "green"
    elif avg_ccr >= 0.8:
        quality_note = "Moderate quality: OCF broadly tracks net income. Watch for non-cash accruals."
        quality_color = "amber"
    else:
        quality_note = "Low quality: Reported earnings persistently exceed operating cash flow — potential accruals risk."
        quality_color = "red"

    return {
        "years":                list(years[:n]),
        "net_income":           list(net_income[:n]),
        "operating_cf":         list(operating_cf[:n]),
        "fcf":                  list(fcf[:n]),
        "cash_conversion_ratio": ccr,
        "avg_cash_conversion":  round(avg_ccr, 2),
        "quality_note":         quality_note,
        "quality_color":        quality_color,
        "available": True,
    }
