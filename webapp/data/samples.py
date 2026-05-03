"""
webapp/data/samples.py
Demo / placeholder data for the valuation dashboard.

Nike (NKE) — full dataset (primary demo).
Apple (AAPL) — secondary demo.
Tesla (TSLA) — secondary demo.
Any unsupported ticker returns NKE data with the name substituted.
"""

from __future__ import annotations
import copy
from webapp.data.confidence import score_confidence
from webapp.data.reverse_dcf import compute_reverse_dcf
from webapp.data.financial_scores import (
    compute_altman_z,
    compute_piotroski_f,
    compute_dupont,
    compute_earnings_quality,
)
from webapp.data.ai_commentary import generate_commentary


# ─── Sensitivity table helper ────────────────────────────────────────────────

def _sensitivity(terminal_ufcf: float, pv_ufcfs: float,
                 net_debt: float, diluted_shares: float,
                 wacc_pcts: list, g_pcts: list,
                 base_wacc: float, base_g: float,
                 forecast_years: int = 7) -> dict:
    values = []
    af_base = sum(1 / (1 + base_wacc / 100) ** (t - 0.5)
                  for t in range(1, forecast_years + 1))
    # Outer loop = g (rows), inner loop = WACC (columns).
    # Matches dashboard layout: g labels on rows, WACC labels on columns.
    for g_pct in g_pcts:
        row = []
        for w_pct in wacc_pcts:
            w = w_pct / 100
            g = g_pct / 100
            spread = w - g
            if spread < 0.005:
                row.append(None)
                continue
            # Gordon Growth: TV = UFCF*(1+g)/(WACC-g), discounted at end of year 7
            tv = terminal_ufcf * (1 + g) / spread
            pv_tv = tv / (1 + w) ** forecast_years
            # Mid-year annuity factors to scale pv_ufcfs
            af_new  = sum(1 / (1 + w) ** (t - 0.5) for t in range(1, forecast_years + 1))
            pv_uf = pv_ufcfs * (af_new / af_base) if af_base > 0 else pv_ufcfs
            ev = pv_uf + pv_tv
            equity = ev - net_debt
            iv = max(0, equity) / diluted_shares if diluted_shares > 0 else 0
            row.append(round(iv, 1))
        values.append(row)
    base_g_idx    = g_pcts.index(base_g)
    base_wacc_idx = wacc_pcts.index(base_wacc)
    return {
        "wacc_labels": [f"{w:.1f}%" for w in wacc_pcts],
        "g_labels":    [f"{g:.1f}%" for g in g_pcts],
        "iv_grid":     values,
        "base_wacc_idx": base_wacc_idx,
        "base_g_idx":    base_g_idx,
    }


# ─── Nike (NKE) ──────────────────────────────────────────────────────────────

_NKE = {
    # Identity
    "ticker": "NKE",
    "company_name": "NIKE, Inc.",
    "exchange": "NYSE",
    "currency": "USD",
    "sector": "Consumer Discretionary",
    "industry": "Footwear & Apparel",
    "description": (
        "NIKE, Inc. designs, develops, markets, and sells athletic footwear, "
        "apparel, equipment, and accessories worldwide through both direct-to-consumer "
        "and wholesale channels. The Swoosh brand spans 190+ countries with "
        "$51B in annual revenue."
    ),

    # Market data
    "price": 78.42,
    "price_date": "2026-04-29",
    "market_cap": 98_870,    # $M
    "fifty_two_week_low": 68.04,
    "fifty_two_week_high": 95.80,
    "analyst_low": 72.00,
    "analyst_high": 105.00,
    "analyst_median": 92.00,

    # Valuation output
    "intrinsic_value": 91.30,
    "upside_pct": 16.4,
    "recommendation": "Undervalued",
    "recommendation_class": "green",
    "confidence_score": 78,
    "data_freshness": "Current",

    # DCF bridge
    "enterprise_value": 109_500,
    "equity_value":     102_000,
    "pv_ufcfs":          35_200,
    "pv_terminal":       74_300,
    "tv_pct":            67.9,
    "diluted_shares":   1_118.0,
    "terminal_ufcf":     8_400,   # back-calc from pv_terminal=74300 at wacc=8.9% g=2.5%

    # WACC / discount rate
    "wacc":               8.9,
    "cost_of_equity":    10.2,
    "cost_of_debt_pre":   4.1,
    "cost_of_debt_post":  3.3,
    "terminal_growth":    2.5,
    "tax_rate":          18.5,
    "beta":               1.12,
    "risk_free_rate":     4.4,
    "erp":                5.2,
    "size_premium":       0.0,
    "equity_weight":     91.5,
    "debt_weight":        8.5,

    # Capital structure
    "total_debt":   9_200,
    "cash_equiv":   1_700,
    "net_debt":     7_500,

    # Key operating assumptions
    "revenue_growth_near":  5.2,
    "revenue_growth_term":  2.5,
    "ebit_margin_base":    12.3,
    "ebit_margin_target":  14.5,
    "da_pct":               2.1,
    "capex_pct":            2.8,
    "sbc_pct":              1.5,
    "dso":                 32.4,
    "dio":                 92.8,
    "dpo":                 45.2,
    "buyback_yield":        3.2,
    "dividend_yield":       1.8,

    # 10-year historical (FY2015–FY2024, most-recent first in lists → we reverse for charts)
    "historical": {
        "years":        [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        "revenue":      [27799, 32376, 34350, 36397, 39117, 37403, 44538, 46710, 51217, 51362],
        "gross_margin": [44.8,  46.2,  44.1,  43.8,  44.7,  43.4,  44.8,  44.3,  43.5,  44.6],
        "ebit_margin":  [13.4,  13.0,  12.2,  11.7,  12.2,   8.3,  10.4,  12.7,  13.0,  12.3],
        "net_income":   [3273,  3760,  4240,  1933,  4029,  2539,  5147,  5147,  5070,  5700],
        "fcf":          [2678,  3064,  3263,  2714,  3898,  2563,  5765,  4369,  3891,  4200],
        "capex":        [ 963,   958,  1105,  1031,  1057,  1448,   695,   878,   969,  1050],
        "debt":         [1079,  1993,  3471,  3464,  3464,  9045,  9034,  8921,  8554,  8400],
        "roic":         [28.2,  26.5,  22.8,  14.3,  28.3,  13.2,  32.4,  41.8,  36.7,  34.1],
        "shares":       [1669,  1654,  1605,  1558,  1485,  1425,  1376,  1334,  1297,  1260],
    },

    # 7-year forecast schedule (FY2025–FY2031)
    "forecast": [
        {"year": "FY2025", "n": 1,  "revenue": 54073, "ebit_m": 12.5, "ebit": 6759,  "nopat": 5509, "da": 1136, "sbc": 811,  "capex": 1514, "d_nowc": 350, "ufcf": 5692, "df": 0.9583, "pv": 5454},
        {"year": "FY2026", "n": 2,  "revenue": 56885, "ebit_m": 12.7, "ebit": 7224,  "nopat": 5887, "da": 1195, "sbc": 853,  "capex": 1593, "d_nowc": 368, "ufcf": 5974, "df": 0.8800, "pv": 5257},
        {"year": "FY2027", "n": 3,  "revenue": 59843, "ebit_m": 12.9, "ebit": 7720,  "nopat": 6292, "da": 1257, "sbc": 898,  "capex": 1676, "d_nowc": 387, "ufcf": 6384, "df": 0.8073, "pv": 5154},
        {"year": "FY2028", "n": 4,  "revenue": 62117, "ebit_m": 13.1, "ebit": 8137,  "nopat": 6632, "da": 1305, "sbc": 932,  "capex": 1739, "d_nowc": 302, "ufcf": 6828, "df": 0.7404, "pv": 5057},
        {"year": "FY2029", "n": 5,  "revenue": 63900, "ebit_m": 13.3, "ebit": 8499,  "nopat": 6927, "da": 1342, "sbc": 959,  "capex": 1789, "d_nowc": 218, "ufcf": 7221, "df": 0.6789, "pv": 4904},
        {"year": "FY2030", "n": 6,  "revenue": 65499, "ebit_m": 13.4, "ebit": 8777,  "nopat": 7153, "da": 1375, "sbc": 982,  "capex": 1834, "d_nowc": 195, "ufcf": 7481, "df": 0.6224, "pv": 4657},
        {"year": "FY2031", "n": 7,  "revenue": 67136, "ebit_m": 14.5, "ebit": 9735,  "nopat": 7934, "da": 1410, "sbc": 1007, "capex": 1880, "d_nowc": 200, "ufcf": 8271, "df": 0.5707, "pv": 4722},
    ],

    # Sensitivity table (WACC vs terminal growth)
    "sensitivity": None,   # populated below

    # Comparable companies
    "peers": [
        {"ticker": "NKE",   "name": "NIKE",        "ev_ebitda": 14.4, "ev_ebit": 16.9, "ev_rev": 2.07, "pe": 17.3, "p_fcf": 23.5, "subject": True},
        {"ticker": "ADDYY", "name": "Adidas",       "ev_ebitda": 12.1, "ev_ebit": 16.2, "ev_rev": 1.19, "pe": 22.1, "p_fcf": 18.4, "subject": False},
        {"ticker": "LULU",  "name": "Lululemon",    "ev_ebitda": 18.2, "ev_ebit": 24.3, "ev_rev": 3.82, "pe": 34.2, "p_fcf": 31.1, "subject": False},
        {"ticker": "ONON",  "name": "On Holding",   "ev_ebitda": 22.4, "ev_ebit": 28.1, "ev_rev": 4.21, "pe": 45.8, "p_fcf": 52.3, "subject": False},
        {"ticker": "DECK",  "name": "Deckers",      "ev_ebitda": 13.8, "ev_ebit": 16.9, "ev_rev": 2.82, "pe": 23.4, "p_fcf": 27.6, "subject": False},
        {"ticker": "VFC",   "name": "VF Corp",      "ev_ebitda":  8.2, "ev_ebit": 11.4, "ev_rev": 0.91, "pe": 15.3, "p_fcf": 12.8, "subject": False},
        {"ticker": "UAA",   "name": "Under Armour", "ev_ebitda":  9.4, "ev_ebit": 13.1, "ev_rev": 0.83, "pe": 18.6, "p_fcf": 14.2, "subject": False},
    ],
    "peer_median": {"ev_ebitda": 13.0, "ev_ebit": 16.6, "ev_rev": 2.01, "pe": 22.8, "p_fcf": 23.0},

    # Validation flags
    "flags": [
        {"name": "Data Freshness",        "status": "pass",   "message": "10 years of clean FMP data retrieved successfully."},
        {"name": "Revenue Sanity",         "status": "pass",   "message": "No revenue anomalies detected across 10 years."},
        {"name": "WACC Range",             "status": "pass",   "message": "WACC 8.9% within typical range (6–15%)."},
        {"name": "WACC–g Spread",          "status": "pass",   "message": "Spread of 6.4% well above the 50bp minimum."},
        {"name": "Terminal Growth Ceiling","status": "pass",   "message": "g = 2.5% is below nominal GDP ceiling (4%)."},
        {"name": "TV % of EV",             "status": "warn",   "message": "Terminal value is 67.9% of total EV. Model is sensitive to terminal assumptions."},
        {"name": "CapEx vs D&A",           "status": "pass",   "message": "CapEx/D&A ratio averaging 0.9× — indicating modest maintenance capex with growth investment."},
        {"name": "Net Debt Sign",          "status": "pass",   "message": "Net debt of $7.5B is 1.0× EBITDA — manageable leverage."},
        {"name": "SBC Dilution",           "status": "warn",   "message": "SBC at 1.5% of revenue. Annual dilution partially offset by buybacks."},
        {"name": "NCI Materiality",        "status": "pass",   "message": "No material minority interest detected."},
        {"name": "Coverage Ratio",         "status": "pass",   "message": "Interest coverage of 12.6× is well above the 3× threshold."},
        {"name": "Balance Sheet Closure",  "status": "pass",   "message": "Balance sheet reconciles within $1M tolerance."},
        {"name": "M&A Distortion",         "status": "pass",   "message": "No large acquisition-year revenue jumps detected."},
        {"name": "Tax Rate Variability",   "status": "warn",   "message": "Tax rate has varied between 14% and 22% over 5 years. Normalised to 18.5%."},
        {"name": "Negative Equity",        "status": "pass",   "message": "Equity is positive across all forecast years."},
    ],

    # Assumption rows (for the assumption editor)
    "assumptions": [
        {"driver": "Revenue Growth (Near-Term)", "auto": 5.2,  "active": 5.2,  "unit": "%",  "mode": "AUTO", "source": "3-yr historical CAGR + analyst consensus", "warn": None},
        {"driver": "Revenue Growth (Terminal)",  "auto": 2.5,  "active": 2.5,  "unit": "%",  "mode": "AUTO", "source": "Long-run nominal GDP proxy (US)", "warn": None},
        {"driver": "EBIT Margin (Base)",         "auto": 12.3, "active": 12.3, "unit": "%",  "mode": "AUTO", "source": "LTM EBIT / Revenue", "warn": None},
        {"driver": "EBIT Margin (Target Y7)",    "auto": 14.5, "active": 14.5, "unit": "%",  "mode": "AUTO", "source": "10-yr peak margin + DTC mix-shift thesis", "warn": None},
        {"driver": "WACC",                       "auto": 8.9,  "active": 8.9,  "unit": "%",  "mode": "AUTO", "source": "CAPM: Rf 4.4% + β 1.12 × ERP 5.2%", "warn": None},
        {"driver": "Cost of Debt (Pre-Tax)",     "auto": 4.1,  "active": 4.1,  "unit": "%",  "mode": "AUTO", "source": "Weighted avg coupon on outstanding bonds", "warn": None},
        {"driver": "Beta (Levered)",             "auto": 1.12, "active": 1.12, "unit": "×",  "mode": "AUTO", "source": "Blume-adjusted 5-yr monthly beta vs S&P 500", "warn": None},
        {"driver": "Tax Rate",                   "auto": 18.5, "active": 18.5, "unit": "%",  "mode": "AUTO", "source": "Normalised 5-yr average (ex-TCJA transition)", "warn": None},
        {"driver": "D&A % Revenue",              "auto": 2.1,  "active": 2.1,  "unit": "%",  "mode": "AUTO", "source": "3-yr average D&A / Revenue", "warn": None},
        {"driver": "CapEx % Revenue",            "auto": 2.8,  "active": 2.8,  "unit": "%",  "mode": "AUTO", "source": "3-yr average CapEx / Revenue", "warn": None},
        {"driver": "SBC % Revenue",              "auto": 1.5,  "active": 1.5,  "unit": "%",  "mode": "AUTO", "source": "3-yr average SBC / Revenue", "warn": None},
        {"driver": "DSO (Days Sales Outstanding)","auto": 32.4, "active": 32.4, "unit": "days","mode": "AUTO","source": "3-yr average receivables / (Rev/365)", "warn": None},
        {"driver": "DIO (Days Inventory Outst.)","auto": 92.8, "active": 92.8, "unit": "days","mode": "AUTO","source": "3-yr average inventory / (COGS/365)", "warn": None},
        {"driver": "DPO (Days Payable Outst.)",  "auto": 45.2, "active": 45.2, "unit": "days","mode": "AUTO","source": "3-yr average payables / (COGS/365)", "warn": None},
        {"driver": "Buyback Yield",              "auto": 3.2,  "active": 3.2,  "unit": "%",  "mode": "AUTO", "source": "Average annual buyback / market cap", "warn": None},
        {"driver": "Dividend Yield",             "auto": 1.8,  "active": 1.8,  "unit": "%",  "mode": "AUTO", "source": "Current annualised dividend / price", "warn": None},
    ],

    # Insight cards
    "insights": [
        {
            "icon": "📈", "category": "Revenue Growth", "status": "neutral",
            "headline": "Growth moderating but still above peers",
            "body": "Revenue has grown at a 7.8% CAGR over 10 years. Near-term guidance of 5.2% is conservative given brand momentum and DTC mix-shift. The model assumes gradual deceleration to 2.5% terminal growth."
        },
        {
            "icon": "📊", "category": "Margin Trajectory", "status": "positive",
            "headline": "Margin expansion thesis intact, but requires execution",
            "body": "EBIT margins compressed from 12.7% (FY2022) to 12.3% (FY2024) due to elevated freight/promotional costs. The model forecasts recovery to 14.5% by FY2031 — achievable if DTC mix shift continues and supply chain normalises."
        },
        {
            "icon": "🏛️", "category": "WACC", "status": "neutral",
            "headline": "WACC of 8.9% is in-line with consumer discretionary peers",
            "body": "Beta of 1.12 reflects moderate cyclicality. At Rf=4.4% and ERP=5.2%, cost of equity is 10.2%. Debt component is minimal (8.5% weight), so WACC is largely driven by equity cost."
        },
        {
            "icon": "⚡", "category": "Terminal Value", "status": "warn",
            "headline": "67.9% of EV is in terminal value — watch your assumptions",
            "body": "A 68% terminal value share is elevated. Each 50bp change in WACC shifts intrinsic value by ±$7–9/share. Sensitivity to g is even more pronounced. Investors should stress-test both inputs."
        },
        {
            "icon": "💧", "category": "Working Capital", "status": "positive",
            "headline": "Negative NWC is a structural cash flow advantage",
            "body": "NKE collects from retail partners (DSO=32 days) faster than it pays suppliers (DPO=45 days), generating a natural cash float. The model maintains this advantage stable as a % of revenue."
        },
        {
            "icon": "🔄", "category": "Buybacks", "status": "positive",
            "headline": "Share count down 24% over 10 years — per-share compounding",
            "body": "From 1,669M (2015) to 1,260M (2024) shares, NKE has deployed $2–3B/year in buybacks. Continuing at this pace reduces the share count to ~1,100M by FY2031, adding ~$8/share to intrinsic value."
        },
        {
            "icon": "🏦", "category": "Balance Sheet & Debt", "status": "positive",
            "headline": "Leverage is manageable — 1.0× net debt / EBITDA",
            "body": "Net debt of $7.5B is backed by $7.4B LTM EBITDA, giving a 1.0× coverage ratio. Interest expense of $700M is 12.6× covered by EBIT. No near-term refinancing risk."
        },
        {
            "icon": "🔍", "category": "Peer Valuation", "status": "neutral",
            "headline": "NKE trades at a modest premium vs. median comps",
            "body": "At 14.4× EV/EBITDA vs. peer median of 13.0×, NKE commands a premium. This is warranted by its brand moat, global distribution, and superior FCF conversion. However, high-growth peers like LULU and ONON trade at 18–22×."
        },
    ],

    # Scenarios
    "scenarios": {
        "base": {
            "label": "Base Case",
            "wacc": 8.9, "g": 2.5, "margin_target": 14.5, "rev_growth": 5.2,
            "iv": 91.30, "upside": 16.4, "ev": 109_500, "recommendation": "Undervalued",
        },
        "bull": {
            "label": "Bull Case",
            "wacc": 7.9, "g": 3.0, "margin_target": 16.0, "rev_growth": 7.5,
            "iv": 128.40, "upside": 63.7, "ev": 143_200, "recommendation": "Undervalued",
            "narrative": "China recovery accelerates; DTC margins expand faster than modelled; buybacks continue at elevated pace; WACC compresses as rates fall.",
        },
        "bear": {
            "label": "Bear Case",
            "wacc": 10.4, "g": 1.5, "margin_target": 11.5, "rev_growth": 2.5,
            "iv": 52.80, "upside": -32.7, "ev": 64_300, "recommendation": "Overvalued",
            "narrative": "Prolonged DTC channel challenges; Chinese consumer weakness persists; elevated promotional activity pressures margins; competing brands (On, Hoka) take market share.",
        },
    },

    # Analyst view
    "analyst_view": {
        "valuation_says": (
            "At $78.42, NIKE is trading at a ~16% discount to our base-case intrinsic value "
            "of $91.30. The stock is pricing in continued margin pressure without crediting the "
            "brand's long-term earnings power. The DCF model implies the market's implied "
            "WACC for NIKE is approximately 10.4% — suggesting investors demand above-normal "
            "risk premium given recent execution disappointments."
        ),
        "key_assumptions": (
            "The two variables that matter most are (1) the path of EBIT margins and (2) the "
            "terminal growth rate. Every 100bp of margin improvement at year 7 adds approximately "
            "$9/share to intrinsic value. A 50bp change in terminal growth shifts IV by ±$6–8/share. "
            "The revenue growth assumption (5.2% near-term) is the least controversial input."
        ),
        "model_risks": (
            "The model could be wrong if: (a) China recovery fails to materialise over the next "
            "2–3 years, permanently impressing margins; (b) DTC transition costs exceed current "
            "estimates; (c) competing brands (ONON, Hoka/Deckers) sustainably take market share "
            "in the performance segment; (d) macro slowdown reduces discretionary spending. "
            "The bull-case sensitivity ($128) is also highly sensitive to sub-8% WACC assumptions."
        ),
        "verify_before_use": [
            "Review Q2/Q3 FY2026 Direct-to-Consumer (DTC) revenue and margin trends",
            "Verify China segment revenue recovery vs. prior-year comparatives",
            "Check inventory levels — excess inventory signals continued promotional risk",
            "Confirm FY2026 FMP financial data is most recently filed (not TTM proxy)",
            "Review latest beta estimate — beta may have risen with increased volatility",
            "Cross-check analyst consensus EPS against model NOPAT for final year sanity",
        ],
    },
}

# Compute sensitivity table
# terminal_ufcf back-calculated from stored pv_terminal=74300 at wacc=8.9%, g=2.5%:
# tv = 74300 × (1.089)^7 × (0.089−0.025) / 1.025 ≈ 8418 → rounded to 8400.
_NKE["sensitivity"] = _sensitivity(
    terminal_ufcf=8_400,
    pv_ufcfs=35_200,
    net_debt=7_500,
    diluted_shares=1_118.0,
    wacc_pcts=[7.9, 8.4, 8.9, 9.4, 9.9],
    g_pcts=[1.5, 2.0, 2.5, 3.0, 3.5],
    base_wacc=8.9,
    base_g=2.5,
)

# NKE: Altman Z-Score (FY2024 balance sheet: WC≈$9.2B, TA≈$23.0B, RE≈$8.1B, EBIT≈$6.3B, MCap≈$98.9B, TL≈$14.8B, Rev≈$51.4B)
_NKE["financial_scores"] = {
    "altman_z": compute_altman_z(
        working_capital=9_200,
        total_assets=23_000,
        retained_earnings=8_100,
        ebit=6_300,
        market_cap=98_870,
        total_liabilities=14_800,
        revenue=51_362,
    ),
    "piotroski_f": compute_piotroski_f(
        net_income=5_700,       total_assets=23_000,
        operating_cash_flow=5_100,
        long_term_debt=8_400,   current_assets=13_500, current_liabilities=9_800,
        shares_outstanding=1_260, gross_profit=22_900,  revenue=51_362,
        net_income_prev=5_070,  total_assets_prev=22_700,
        long_term_debt_prev=8_554, current_assets_prev=13_100, current_liabilities_prev=9_400,
        shares_prev=1_297,      gross_profit_prev=22_270,  revenue_prev=51_217,
    ),
}

_NKE["dupont"] = compute_dupont(
    years=_NKE["historical"]["years"],
    net_income=_NKE["historical"]["net_income"],
    revenue=_NKE["historical"]["revenue"],
    total_assets=[12800, 14000, 13900, 15300, 16200, 17100, 19600, 20700, 22700, 23000],
    equity=      [4700,  5600,  6100,  1900,  2000,  2100,  5600,  5500,  5600,  5400],
)

_NKE["earnings_quality"] = compute_earnings_quality(
    years=_NKE["historical"]["years"],
    net_income=_NKE["historical"]["net_income"],
    operating_cf=[3241, 3642, 3852, 3222, 4560, 3148, 6647, 5765, 5557, 5100],
    fcf=_NKE["historical"]["fcf"],
)

_NKE["analyst_consensus"] = {
    "revenue_y1_consensus": 54_200,
    "revenue_y1_model":     54_073,
    "eps_y1_consensus":      3.82,
    "buy_count":  18,
    "hold_count":  9,
    "sell_count":  3,
    "total_analysts": 30,
    "mean_target": 92.00,
}


# ─── Apple (AAPL) ────────────────────────────────────────────────────────────

_AAPL = {
    "ticker": "AAPL",
    "company_name": "Apple Inc.",
    "exchange": "NASDAQ",
    "currency": "USD",
    "sector": "Information Technology",
    "industry": "Technology Hardware",
    "description": (
        "Apple Inc. designs, manufactures, and markets smartphones, personal computers, "
        "tablets, wearables, and accessories worldwide. Services segment (App Store, iCloud, "
        "Apple Pay) now represents ~24% of revenue and growing."
    ),
    "price": 211.50,
    "price_date": "2026-04-29",
    "market_cap": 3_190_000,
    "fifty_two_week_low": 169.21,
    "fifty_two_week_high": 237.49,
    "analyst_low": 175.00,
    "analyst_high": 265.00,
    "analyst_median": 225.00,
    "intrinsic_value": 195.20,
    "upside_pct": -7.7,
    "recommendation": "Fairly Valued",
    "recommendation_class": "amber",
    "confidence_score": 84,
    "data_freshness": "Current",
    "enterprise_value": 3_225_000,
    "equity_value":     3_095_000,
    "pv_ufcfs":          950_000,
    "pv_terminal":      2_145_000,
    "tv_pct":            69.3,
    "diluted_shares":   15_085.0,
    "terminal_ufcf":   185_000,   # last-year UFCF used in TV = ufcf / (wacc-g)
    "wacc": 9.1,
    "cost_of_equity": 10.5,
    "cost_of_debt_pre": 3.8,
    "cost_of_debt_post": 3.0,
    "terminal_growth": 2.5,
    "tax_rate": 16.5,
    "beta": 1.18,
    "risk_free_rate": 4.4,
    "erp": 5.2,
    "size_premium": 0.0,
    "equity_weight": 96.2,
    "debt_weight": 3.8,
    "total_debt": 104_590,
    "cash_equiv": 55_000,
    "net_debt": 49_590,
    "revenue_growth_near": 6.8,
    "revenue_growth_term": 2.5,
    "ebit_margin_base": 30.4,
    "ebit_margin_target": 32.0,
    "da_pct": 2.7,
    "capex_pct": 2.9,
    "sbc_pct": 2.4,
    "dso": 28.1,
    "dio": 8.2,
    "dpo": 96.4,
    "buyback_yield": 4.1,
    "dividend_yield": 0.5,
    "historical": {
        "years":        [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        "revenue":      [233715, 215639, 229234, 265595, 260174, 274515, 365817, 394328, 383285, 391035],
        "gross_margin": [40.1,  39.1,  38.5,  38.3,  37.8,  38.2,  41.8,  43.3,  44.1,  46.2],
        "ebit_margin":  [30.5,  27.8,  26.8,  26.7,  24.6,  24.1,  29.8,  30.3,  29.8,  30.4],
        "net_income":   [53394, 45687, 48351, 59531, 55256, 57411, 94680, 99803, 96995, 93736],
        "fcf":          [70019, 52895, 51774, 72338, 58896, 73365, 92953, 111443, 99584, 108807],
        "capex":        [11488, 12734, 12795, 13313, 10495, 7309,  11085, 10708, 10959, 9447],
        "debt":         [64462, 87032, 97207, 114483, 108047, 122672, 136522, 132480, 109280, 104590],
        "roic":         [32.8,  23.2,  20.7,  22.6,  19.6,  19.8,  35.1,  49.7,  55.5,  57.2],
        "shares":       [26294, 23318, 21331, 20000, 18596, 17528, 16956, 16215, 15744, 15408],
    },
    "forecast": [
        {"year": "FY2025", "n": 1, "revenue": 418,  "ebit_m": 30.8, "ebit": 128.7, "nopat": 107.5, "da": 11.3, "sbc": 10.0, "capex": 12.1, "d_nowc": -5.2, "ufcf": 121.9, "df": 0.9583, "pv": 116.8},
        {"year": "FY2026", "n": 2, "revenue": 447,  "ebit_m": 31.2, "ebit": 139.5, "nopat": 116.5, "da": 12.1, "sbc": 10.7, "capex": 12.9, "d_nowc": -3.8, "ufcf": 132.2, "df": 0.8800, "pv": 116.3},
        {"year": "FY2027", "n": 3, "revenue": 478,  "ebit_m": 31.5, "ebit": 150.6, "nopat": 125.7, "da": 12.9, "sbc": 11.5, "capex": 13.9, "d_nowc": -4.1, "ufcf": 142.3, "df": 0.8073, "pv": 114.9},
        {"year": "FY2028", "n": 4, "revenue": 504,  "ebit_m": 31.7, "ebit": 159.8, "nopat": 133.4, "da": 13.6, "sbc": 12.1, "capex": 14.6, "d_nowc": -2.8, "ufcf": 152.3, "df": 0.7404, "pv": 112.8},
        {"year": "FY2029", "n": 5, "revenue": 529,  "ebit_m": 31.8, "ebit": 168.2, "nopat": 140.4, "da": 14.3, "sbc": 12.7, "capex": 15.3, "d_nowc": -2.5, "ufcf": 160.6, "df": 0.6789, "pv": 109.1},
        {"year": "FY2030", "n": 6, "revenue": 554,  "ebit_m": 31.9, "ebit": 176.6, "nopat": 147.5, "da": 15.0, "sbc": 13.3, "capex": 16.1, "d_nowc": -2.2, "ufcf": 167.9, "df": 0.6224, "pv": 104.5},
        {"year": "FY2031", "n": 7, "revenue": 582,  "ebit_m": 32.0, "ebit": 186.2, "nopat": 155.5, "da": 15.7, "sbc": 14.0, "capex": 16.9, "d_nowc": -2.0, "ufcf": 176.3, "df": 0.5707, "pv": 100.6},
    ],
    "sensitivity": None,
    "peers": [
        {"ticker": "AAPL",  "name": "Apple",     "ev_ebitda": 26.2, "ev_ebit": 29.3, "ev_rev": 8.24, "pe": 34.0, "p_fcf": 29.3, "subject": True},
        {"ticker": "MSFT",  "name": "Microsoft", "ev_ebitda": 29.1, "ev_ebit": 31.8, "ev_rev": 13.2, "pe": 38.4, "p_fcf": 37.1, "subject": False},
        {"ticker": "GOOG",  "name": "Alphabet",  "ev_ebitda": 18.4, "ev_ebit": 22.1, "ev_rev": 5.81, "pe": 23.6, "p_fcf": 24.8, "subject": False},
        {"ticker": "META",  "name": "Meta",      "ev_ebitda": 22.8, "ev_ebit": 26.4, "ev_rev": 8.13, "pe": 28.7, "p_fcf": 26.2, "subject": False},
        {"ticker": "AMZN",  "name": "Amazon",    "ev_ebitda": 20.3, "ev_ebit": 38.2, "ev_rev": 3.42, "pe": 44.1, "p_fcf": 36.4, "subject": False},
        {"ticker": "NVDA",  "name": "Nvidia",    "ev_ebitda": 48.1, "ev_ebit": 51.2, "ev_rev": 28.4, "pe": 44.2, "p_fcf": 46.8, "subject": False},
    ],
    "peer_median": {"ev_ebitda": 24.5, "ev_ebit": 29.1, "ev_rev": 9.67, "pe": 36.3, "p_fcf": 31.8},
    "flags": [
        {"name": "Data Freshness",     "status": "pass", "message": "10 years of clean FMP data retrieved."},
        {"name": "WACC Range",         "status": "pass", "message": "WACC 9.1% within typical range."},
        {"name": "TV % of EV",         "status": "warn", "message": "TV at 69.3% of EV. Normal for a mature compounder."},
        {"name": "SBC Dilution",       "status": "warn", "message": "SBC 2.4% of revenue. Significant but largely offset by buybacks."},
        {"name": "Negative NWC",       "status": "pass", "message": "Negative NWC ($-96B) is structural advantage — supplier financing model."},
        {"name": "Coverage Ratio",     "status": "pass", "message": "Interest coverage ~38× — pristine balance sheet."},
        {"name": "Revenue Growth",     "status": "pass", "message": "Growth moderating from hardware cycle; Services accelerating."},
        {"name": "Tax Rate Stability", "status": "pass", "message": "Tax rate stable at 15–17% over 5 years."},
    ],
    "assumptions": [
        {"driver": "Revenue Growth (Near-Term)", "auto": 6.8,  "active": 6.8,  "unit": "%",  "mode": "AUTO", "source": "Services growth + iPhone unit recovery", "warn": None},
        {"driver": "Revenue Growth (Terminal)",  "auto": 2.5,  "active": 2.5,  "unit": "%",  "mode": "AUTO", "source": "Long-run nominal GDP proxy", "warn": None},
        {"driver": "EBIT Margin (Target Y7)",    "auto": 32.0, "active": 32.0, "unit": "%",  "mode": "AUTO", "source": "Services mix-shift driving margin expansion", "warn": None},
        {"driver": "WACC",                       "auto": 9.1,  "active": 9.1,  "unit": "%",  "mode": "AUTO", "source": "CAPM: Rf 4.4% + β 1.18 × ERP 5.2%", "warn": None},
        {"driver": "Beta (Levered)",             "auto": 1.18, "active": 1.18, "unit": "×",  "mode": "AUTO", "source": "Blume-adjusted 5-yr monthly beta", "warn": None},
        {"driver": "Tax Rate",                   "auto": 16.5, "active": 16.5, "unit": "%",  "mode": "AUTO", "source": "5-yr normalised effective rate", "warn": None},
        {"driver": "CapEx % Revenue",            "auto": 2.9,  "active": 2.9,  "unit": "%",  "mode": "AUTO", "source": "3-yr average CapEx / Revenue", "warn": None},
    ],
    "insights": [
        {"icon": "📱", "category": "Services Growth", "status": "positive", "headline": "Services now drives valuation", "body": "Services revenue (~24% of total, 70%+ gross margin) is the primary valuation driver. The segment is growing at 12–15%/yr, materially above hardware. Model assigns 60% of terminal value to Services."},
        {"icon": "🔄", "category": "Buybacks", "status": "positive", "headline": "The world's biggest capital returner", "body": "Apple has returned over $700B to shareholders since 2012. At 4.1% annual buyback yield, the share count has declined from 26B to 15B over 10 years — mechanically enhancing per-share value."},
        {"icon": "⚡", "category": "Terminal Value", "status": "warn", "headline": "Premium valuation demands premium execution", "body": "At 29.3× EV/EBIT, Apple trades at a significant premium to the S&P 500 multiple. The model's base IV of $195 implies slight overvaluation. Key risk: hardware upgrade cycles lengthening."},
    ],
    "scenarios": {
        "base": {"label": "Base Case", "wacc": 9.1, "g": 2.5, "margin_target": 32.0, "rev_growth": 6.8, "iv": 195.20, "upside": -7.7, "ev": 3225000, "recommendation": "Fairly Valued"},
        "bull": {"label": "Bull Case", "wacc": 8.1, "g": 3.0, "margin_target": 34.0, "rev_growth": 9.0, "iv": 248.50, "upside": 17.5, "ev": 3840000, "recommendation": "Undervalued", "narrative": "AI integration drives iPhone super-cycle; Services margin expands to 35%+; Vision Pro creates new category."},
        "bear": {"label": "Bear Case", "wacc": 10.1, "g": 1.5, "margin_target": 28.0, "rev_growth": 3.0, "iv": 148.30, "upside": -29.9, "ev": 2280000, "recommendation": "Overvalued", "narrative": "Regulatory headwinds on App Store; China sales decline; hardware commoditisation accelerates."},
    },
    "analyst_view": {
        "valuation_says": "Apple is currently trading at a ~8% premium to our base-case intrinsic value. The stock is pricing in continued execution of the Services growth story. At these levels, investors are paying for expected earnings power 10+ years into the future.",
        "key_assumptions": "Services revenue mix is the single biggest variable. Each 100bp of margin expansion in Services adds ~$8-10/share. WACC sensitivity is less severe than for NKE because the business is more predictable.",
        "model_risks": "Key risks: App Store regulatory pressure (EU DMA compliance could reduce take rates); China market access; Vision Pro revenue contribution timing; hardware refresh cycle lengthening.",
        "verify_before_use": ["Verify Q2 FY2026 Services revenue and gross margin vs. consensus", "Check App Store regulatory updates in EU and US", "Review China iPhone sell-through data", "Confirm FY2025 annual filing data is available in FMP"],
    },
}

_AAPL["sensitivity"] = _sensitivity(
    terminal_ufcf=185_000,
    pv_ufcfs=875_000,
    net_debt=49_590,
    diluted_shares=15_085.0,
    wacc_pcts=[7.9, 8.4, 8.9, 9.4, 9.9],
    g_pcts=[1.5, 2.0, 2.5, 3.0, 3.5],
    base_wacc=8.9,
    base_g=2.5,
)

# AAPL: Altman Z (FY2024: WC≈-96B but gross assets large; TA≈352B, RE≈-19B, EBIT≈119B, MCap≈3190B, TL≈308B, Rev≈391B)
_AAPL["financial_scores"] = {
    "altman_z": compute_altman_z(
        working_capital=-96_000,
        total_assets=352_000,
        retained_earnings=-19_000,
        ebit=119_000,
        market_cap=3_190_000,
        total_liabilities=308_000,
        revenue=391_035,
    ),
    "piotroski_f": compute_piotroski_f(
        net_income=93_736,    total_assets=352_000,
        operating_cash_flow=118_254,
        long_term_debt=85_750, current_assets=152_987, current_liabilities=176_392,
        shares_outstanding=15_408, gross_profit=180_683, revenue=391_035,
        net_income_prev=96_995, total_assets_prev=352_755,
        long_term_debt_prev=95_281, current_assets_prev=143_566, current_liabilities_prev=145_308,
        shares_prev=15_744, gross_profit_prev=169_148, revenue_prev=383_285,
    ),
}

_AAPL["dupont"] = compute_dupont(
    years=_AAPL["historical"]["years"],
    net_income=_AAPL["historical"]["net_income"],
    revenue=_AAPL["historical"]["revenue"],
    total_assets=[290000, 321700, 375319, 365725, 338516, 323888, 351002, 352755, 352755, 352000],
    equity=      [119355, 128249, 134047, 107147, 90488,  65339,  63090,  50672,  62146,  56950],
)

_AAPL["earnings_quality"] = compute_earnings_quality(
    years=_AAPL["historical"]["years"],
    net_income=_AAPL["historical"]["net_income"],
    operating_cf=[81266, 65824, 63598, 77434, 69391, 80674, 104038, 122151, 110543, 118254],
    fcf=_AAPL["historical"]["fcf"],
)

_AAPL["analyst_consensus"] = {
    "revenue_y1_consensus": 421_000,
    "revenue_y1_model":     418_000,
    "eps_y1_consensus":      6.65,
    "buy_count":  38,
    "hold_count": 12,
    "sell_count":  2,
    "total_analysts": 52,
    "mean_target": 225.00,
}


# ─── Tesla (TSLA) ────────────────────────────────────────────────────────────

_TSLA = {
    "ticker": "TSLA",
    "company_name": "Tesla, Inc.",
    "exchange": "NASDAQ",
    "currency": "USD",
    "sector": "Consumer Discretionary",
    "industry": "Electric Vehicles",
    "description": (
        "Tesla, Inc. designs, develops, manufactures, leases, and sells electric vehicles, "
        "energy generation/storage systems, and solar products. The company also operates "
        "a supercharger network and offers vehicle software (FSD) subscriptions."
    ),
    "price": 172.20,
    "price_date": "2026-04-29",
    "market_cap": 551_000,
    "fifty_two_week_low": 138.80,
    "fifty_two_week_high": 479.86,
    "analyst_low": 85.00,
    "analyst_high": 500.00,
    "analyst_median": 195.00,
    "intrinsic_value": 145.80,
    "upside_pct": -15.3,
    "recommendation": "Overvalued",
    "recommendation_class": "red",
    "confidence_score": 55,
    "data_freshness": "Current",
    "enterprise_value": 540_000,
    "equity_value":     498_000,
    "pv_ufcfs":          98_000,
    "pv_terminal":      400_000,
    "tv_pct":            80.3,
    "diluted_shares":   3_200.0,
    "terminal_ufcf":    23_000,   # last-year UFCF used in TV = ufcf / (wacc-g)
    "wacc": 10.5,
    "cost_of_equity": 13.2,
    "cost_of_debt_pre": 5.2,
    "cost_of_debt_post": 4.0,
    "terminal_growth": 3.0,
    "tax_rate": 17.0,
    "beta": 1.85,
    "risk_free_rate": 4.4,
    "erp": 5.2,
    "size_premium": 0.0,
    "equity_weight": 92.0,
    "debt_weight": 8.0,
    "total_debt": 5_500,
    "cash_equiv": 30_720,
    "net_debt": -25_220,
    "revenue_growth_near": 15.0,
    "revenue_growth_term": 3.0,
    "ebit_margin_base": 8.2,
    "ebit_margin_target": 16.0,
    "da_pct": 4.1,
    "capex_pct": 8.2,
    "sbc_pct": 2.8,
    "dso": 9.4,
    "dio": 12.2,
    "dpo": 52.3,
    "buyback_yield": 0.0,
    "dividend_yield": 0.0,
    "historical": {
        "years":        [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        "revenue":      [4046, 7000, 11759, 21461, 24578, 31536, 53823, 81462, 96773, 97690],
        "gross_margin": [22.8, 22.8, 18.9,  18.5,  16.6,  21.3,  25.3,  25.6,  18.2,  17.9],
        "ebit_margin":  [-20.6,-14.6,-16.7, -2.9,  -0.2,  6.3,   12.1,  16.8,  9.2,   8.2],
        "net_income":   [-717, -675, -1962,  -976,  -862, 721,   5519,  12556, 14997, 7091],
        "fcf":          [-2446,-667, -3975, -1009,  -982, 2786,  5044,  7576,  4358,  3572],
        "capex":        [1635, 1280, 3415,  2101,  1327, 3157,  6515,  7160,  8898,  11013],
        "debt":         [2738, 5921, 10990, 11631, 13419, 13712, 6831,  5765,  5196,  5500],
        "roic":         [-18.2,-11.4,-14.4, -3.1,  0.1,  8.2,   20.4,  32.1,  18.3,  9.8],
        "shares":       [1305, 1356, 1583,  1663,  1736, 1934,  2959,  3130,  3170,  3185],
    },
    "forecast": [
        {"year": "FY2025", "n": 1, "revenue": 112_000, "ebit_m": 10.0, "ebit": 11200, "nopat": 9296, "da": 4592, "sbc": 3136, "capex": 9184, "d_nowc": 800,  "ufcf": 7040,  "df": 0.9535, "pv": 6712},
        {"year": "FY2026", "n": 2, "revenue": 128_000, "ebit_m": 11.5, "ebit": 14720, "nopat": 12218, "da": 5248, "sbc": 3584, "capex": 10496, "d_nowc": 900, "ufcf": 9654,  "df": 0.8630, "pv": 8332},
        {"year": "FY2027", "n": 3, "revenue": 147_000, "ebit_m": 13.0, "ebit": 19110, "nopat": 15861, "da": 6027, "sbc": 4116, "capex": 12054, "d_nowc": 1050,"ufcf": 12900, "df": 0.7814, "pv": 10082},
        {"year": "FY2028", "n": 4, "revenue": 162_000, "ebit_m": 14.0, "ebit": 22680, "nopat": 18824, "da": 6642, "sbc": 4536, "capex": 13284, "d_nowc": 800, "ufcf": 15918, "df": 0.7076, "pv": 11264},
        {"year": "FY2029", "n": 5, "revenue": 175_000, "ebit_m": 14.5, "ebit": 25375, "nopat": 21061, "da": 7175, "sbc": 4900, "capex": 14350, "d_nowc": 650, "ufcf": 18136, "df": 0.6411, "pv": 11629},
        {"year": "FY2030", "n": 6, "revenue": 187_000, "ebit_m": 15.2, "ebit": 28424, "nopat": 23592, "da": 7667, "sbc": 5236, "capex": 15334, "d_nowc": 540, "ufcf": 20621, "df": 0.5808, "pv": 11977},
        {"year": "FY2031", "n": 7, "revenue": 200_000, "ebit_m": 16.0, "ebit": 32000, "nopat": 26560, "da": 8200, "sbc": 5600, "capex": 16400, "d_nowc": 500, "ufcf": 23460, "df": 0.5261, "pv": 12336},
    ],
    "sensitivity": None,
    "peers": [
        {"ticker": "TSLA",  "name": "Tesla",     "ev_ebitda": 55.1, "ev_ebit": 66.7, "ev_rev": 5.53, "pe": 77.6, "p_fcf": 154.2, "subject": True},
        {"ticker": "GM",    "name": "GM",        "ev_ebitda":  4.2, "ev_ebit":  5.8, "ev_rev": 0.31, "pe":  5.1, "p_fcf":  8.3,  "subject": False},
        {"ticker": "F",     "name": "Ford",      "ev_ebitda":  5.8, "ev_ebit":  7.2, "ev_rev": 0.38, "pe":  7.2, "p_fcf": 11.4,  "subject": False},
        {"ticker": "RIVN",  "name": "Rivian",    "ev_ebitda":  None,"ev_ebit": None, "ev_rev": 2.14, "pe": None, "p_fcf": None,   "subject": False},
        {"ticker": "NIO",   "name": "NIO",       "ev_ebitda":  None,"ev_ebit": None, "ev_rev": 1.42, "pe": None, "p_fcf": None,   "subject": False},
        {"ticker": "STLA",  "name": "Stellantis","ev_ebitda":  2.9, "ev_ebit":  3.4, "ev_rev": 0.22, "pe":  4.8, "p_fcf":  5.1,  "subject": False},
        {"ticker": "TM",    "name": "Toyota",    "ev_ebitda":  9.4, "ev_ebit": 11.2, "ev_rev": 1.12, "pe": 10.3, "p_fcf": 14.8,  "subject": False},
    ],
    "peer_median": {"ev_ebitda": 5.0, "ev_ebit": 6.5, "ev_rev": 0.85, "pe": 7.8, "p_fcf": 11.0},
    "flags": [
        {"name": "Data Freshness",     "status": "pass", "message": "10 years of FMP data retrieved."},
        {"name": "WACC Range",         "status": "warn", "message": "WACC 10.5% is at the high end — reflects EV execution risk."},
        {"name": "TV % of EV",         "status": "fail", "message": "Terminal value is 80.3% of EV — extreme sensitivity to assumptions. Use wide scenario range."},
        {"name": "Revenue Variability", "status": "warn", "message": "Revenue CAGR of 46% since 2015 is exceptional but unsustainable. Growth decelerating materially."},
        {"name": "Margin Volatility",  "status": "warn", "message": "EBIT margins ranged from -21% to +17% over the past 10 years. High uncertainty in terminal margin."},
        {"name": "Beta",               "status": "warn", "message": "Beta of 1.85 indicates high market sensitivity. Premium discount rate appropriate."},
        {"name": "Negative Net Debt",  "status": "pass", "message": "Net cash position of $25.2B provides significant financial flexibility."},
        {"name": "CapEx Intensity",    "status": "warn", "message": "CapEx at 8.2% of revenue is high. Gigafactory build-out is capital intensive."},
    ],
    "assumptions": [
        {"driver": "Revenue Growth (Near-Term)", "auto": 15.0, "active": 15.0, "unit": "%",  "mode": "AUTO", "source": "FSD + Model Y ramp + new model introductions", "warn": None},
        {"driver": "Revenue Growth (Terminal)",  "auto": 3.0,  "active": 3.0,  "unit": "%",  "mode": "AUTO", "source": "Long-run nominal GDP + EV penetration premium", "warn": None},
        {"driver": "EBIT Margin (Target Y7)",    "auto": 16.0, "active": 16.0, "unit": "%",  "mode": "AUTO", "source": "Software/FSD revenue contribution + scale", "warn": "Aggressive — current LTM EBIT margin is 8.2%."},
        {"driver": "WACC",                       "auto": 10.5, "active": 10.5, "unit": "%",  "mode": "AUTO", "source": "CAPM: Rf 4.4% + β 1.85 × ERP 5.2%", "warn": None},
        {"driver": "Beta (Levered)",             "auto": 1.85, "active": 1.85, "unit": "×",  "mode": "AUTO", "source": "Blume-adjusted 5-yr monthly beta", "warn": None},
        {"driver": "CapEx % Revenue",            "auto": 8.2,  "active": 8.2,  "unit": "%",  "mode": "AUTO", "source": "3-yr average; Gigafactory expansion phase", "warn": "High. Will need to normalise post-capacity build-out."},
    ],
    "insights": [
        {"icon": "⚡", "category": "Growth Premium", "status": "warn", "headline": "Priced for near-perfection", "body": "At 55× EV/EBITDA, Tesla trades at a massive premium to traditional automakers. The stock is pricing in >15% revenue CAGR for the next decade AND 16%+ operating margins — a combination that few companies achieve."},
        {"icon": "🤖", "category": "FSD / AI Optionality", "status": "positive", "headline": "Significant optionality not captured in base DCF", "body": "Full Self-Driving software and Optimus robotics represent high-value options that are hard to model deterministically. Our base DCF does not assign value to these; they could add $30-80/share in an upside scenario."},
        {"icon": "📊", "category": "Margin Execution Risk", "status": "warn", "headline": "Aggressive price cuts compressed margins", "body": "Gross margins fell from 25.6% (2022) to 17.9% (2024) due to price competition. Recovery to 16%+ EBIT margin requires FSD monetisation, lower battery costs, and volume leverage — all plausible but uncertain."},
    ],
    "scenarios": {
        "base": {"label": "Base Case", "wacc": 10.5, "g": 3.0, "margin_target": 16.0, "rev_growth": 15.0, "iv": 145.80, "upside": -15.3, "ev": 540_000, "recommendation": "Overvalued"},
        "bull": {"label": "Bull Case", "wacc": 9.0, "g": 4.0, "margin_target": 22.0, "rev_growth": 22.0, "iv": 348.50, "upside": 102.4, "ev": 1_120_000, "recommendation": "Undervalued", "narrative": "FSD achieves Level 4 autonomy; Optimus enters mass production; energy division reaches $30B revenue; margins recover to peak levels."},
        "bear": {"label": "Bear Case", "wacc": 12.0, "g": 1.5, "margin_target": 8.0, "rev_growth": 5.0, "iv": 68.40, "upside": -60.3, "ev": 218_000, "recommendation": "Overvalued", "narrative": "Chinese EV competition intensifies; FSD timeline slips further; autonomous driving monetisation fails to materialise; margin pressure continues."},
    },
    "analyst_view": {
        "valuation_says": "Tesla is currently trading at a 15% premium to our base-case intrinsic value. The market is assigning significant probability to the FSD/Optimus bull case. Base DCF suggests downside risk, but the stock is partially a technology/AI call option that traditional DCF struggles to value.",
        "key_assumptions": "Three variables dominate: (1) terminal EBIT margin — each 100bp is worth ~$9/share; (2) revenue CAGR over 10 years — the difference between 10% and 20% is $80+/share; (3) whether FSD/robotics optionality gets assigned any probability-weighted value.",
        "model_risks": "Bull case: FSD monetisation at scale, Optimus at $20K/unit, energy division 10× growth. Bear case: commodity EV pricing continues, Chinese brands take share globally, brand perception issues in key markets.",
        "verify_before_use": ["Q1 2026 deliveries vs. consensus", "FSD penetration rate and pricing changes", "Optimus production timeline updates", "China factory utilisation and ASP trends", "Energy storage deployment run-rate"],
    },
}

_TSLA["sensitivity"] = _sensitivity(
    terminal_ufcf=23_000,
    pv_ufcfs=72_000,
    net_debt=-25_220,
    diluted_shares=3_200.0,
    wacc_pcts=[8.5, 9.5, 10.5, 11.5, 12.5],
    g_pcts=[1.5, 2.0, 3.0, 3.5, 4.0],
    base_wacc=10.5,
    base_g=3.0,
)

# TSLA: Altman Z (FY2024: WC≈$25B, TA≈$122B, RE≈$30B, EBIT≈$7.9B, MCap≈$551B, TL≈$44B, Rev≈$97.7B)
_TSLA["financial_scores"] = {
    "altman_z": compute_altman_z(
        working_capital=25_000,
        total_assets=122_000,
        retained_earnings=30_000,
        ebit=7_900,
        market_cap=551_000,
        total_liabilities=44_000,
        revenue=97_690,
    ),
    "piotroski_f": compute_piotroski_f(
        net_income=7_091,    total_assets=122_000,
        operating_cash_flow=14_923,
        long_term_debt=5_500, current_assets=51_000, current_liabilities=28_600,
        shares_outstanding=3_185, gross_profit=17_498, revenue=97_690,
        net_income_prev=14_997, total_assets_prev=106_618,
        long_term_debt_prev=5_196, current_assets_prev=44_800, current_liabilities_prev=26_700,
        shares_prev=3_170, gross_profit_prev=17_660, revenue_prev=96_773,
    ),
}

_TSLA["dupont"] = compute_dupont(
    years=_TSLA["historical"]["years"],
    net_income=_TSLA["historical"]["net_income"],
    revenue=_TSLA["historical"]["revenue"],
    total_assets=[8068, 10055, 28655, 34309, 34309, 52148, 62131, 82338, 106618, 122000],
    equity=      [1034,  4753,  4237,  4923,  6618,  22225, 30189, 44704, 62634, 71900],
)

_TSLA["earnings_quality"] = compute_earnings_quality(
    years=_TSLA["historical"]["years"],
    net_income=_TSLA["historical"]["net_income"],
    operating_cf=[-524, 1265, -61, 2098, 2405, 5943, 11497, 14185, 13256, 14923],
    fcf=_TSLA["historical"]["fcf"],
)

_TSLA["analyst_consensus"] = {
    "revenue_y1_consensus": 113_000,
    "revenue_y1_model":     112_000,
    "eps_y1_consensus":      2.10,
    "buy_count":  16,
    "hold_count": 18,
    "sell_count": 10,
    "total_analysts": 44,
    "mean_target": 195.00,
}


# ─── Registry & lookup ───────────────────────────────────────────────────────

REGISTRY: dict[str, dict] = {
    "NKE":  _NKE,
    "AAPL": _AAPL,
    "TSLA": _TSLA,
}

SUPPORTED_TICKERS = list(REGISTRY.keys())


def get_dashboard_data(ticker: str, overrides: dict | None = None) -> dict:
    """Return dashboard data for *ticker*.

    Priority:
      1. EODHD    — primary live source; 10–20+ years of annual history
      2. yfinance  — fallback live source (4-year limit but no API key needed)
      3. FMP       — second fallback (requires FMP_API_KEY env var)
      4. REGISTRY  — hardcoded NKE/AAPL/TSLA samples
      5. NKE demo  — last-resort fallback for unknown tickers
    """
    from webapp.data.eodhd_client import (
        build_dashboard_data as eodhd_build,
        is_available as eodhd_available,
    )
    from webapp.data.yfinance_client import (
        build_dashboard_data as yf_build,
        is_available as yf_available,
    )
    from webapp.data.fmp_client import is_available as fmp_available
    from webapp.data.fmp_client import build_dashboard_data as fmp_build

    ticker = ticker.upper().strip()
    data = None

    # 1. Try EODHD first (best historical depth — 10–20+ years)
    if eodhd_available():
        data = eodhd_build(ticker, overrides=overrides)

    # 2. Fallback to yfinance if EODHD failed
    if data is None and yf_available():
        data = yf_build(ticker)

    # 3. Fallback to FMP if yfinance also failed
    if data is None and fmp_available():
        data = fmp_build(ticker)

    # 4. Fallback to hardcoded sample for NKE/AAPL/TSLA
    if data is None and ticker in REGISTRY:
        data = copy.deepcopy(REGISTRY[ticker])
        data["is_demo"] = True
        data["demo_note"] = (
            f"Showing sample data for {ticker} (live data temporarily unavailable)."
        )

    # 5. Final fallback: NKE demo for any unknown ticker
    if data is None:
        data = copy.deepcopy(_NKE)
        data["is_demo"] = True
        data["demo_note"] = (
            f"'{ticker}' not found. Showing Nike (NKE) demo data. "
            "Check your internet connection for live data."
        )

    if overrides:
        # Only apply override scaling for sample/yfinance/fmp data.
        # When EODHD builds the data, overrides are already applied inside
        # build_dashboard_data() before running the DCF, so we skip this.
        if data.get("data_source") != "eodhd":
            _apply_overrides(data, overrides)

    # ── Enrich with computed fields ───────────────────────────────────────────
    # Confidence score
    conf = score_confidence(data)
    data["confidence_score"] = conf.total
    data["confidence_breakdown"] = conf.as_dict()

    # Reverse DCF (only if we have the required inputs)
    if data.get("pv_ufcfs") and data.get("pv_terminal") and data.get("diluted_shares"):
        data["reverse_dcf"] = compute_reverse_dcf(data)
    else:
        data["reverse_dcf"] = None

    # AI commentary (generated fresh so it reflects current data / overrides)
    data["ai_commentary"] = generate_commentary(data)

    # Investment memo (generate from existing fields)
    data["investment_memo"] = _build_investment_memo(data)

    # Market expectations (derived from reverse DCF + analyst data)
    data["market_expectations"] = _build_market_expectations(data)

    return data


def _annuity_factor(rate: float, years: int = 7) -> float:
    """Sum of mid-year discount factors 1/(1+r)^(t-0.5) for t=1..years.
    Uses mid-year convention to match the main DCF model."""
    r = rate / 100
    if r <= 0:
        return years
    return sum(1 / (1 + r) ** (t - 0.5) for t in range(1, years + 1))


def _apply_overrides(data: dict, overrides: dict) -> None:
    """Apply user overrides and recompute intrinsic value.

    Correct approach:
    1. Retrieve the stored last-year terminal UFCF (TV = ufcf / (wacc - g)).
    2. Recompute PV of terminal value with new wacc/g.
    3. Scale PV UFCFs by the ratio of annuity discount factors (new vs orig wacc).
    """
    orig_wacc_pct = data["wacc"]
    orig_g_pct    = data["terminal_growth"]

    wacc_pct = overrides.get("wacc", orig_wacc_pct)
    g_pct    = overrides.get("g",    orig_g_pct)
    if wacc_pct is None:
        wacc_pct = orig_wacc_pct
    if g_pct is None:
        g_pct = orig_g_pct

    data["wacc"]            = wacc_pct
    data["terminal_growth"] = g_pct

    for key in ("revenue_growth_near", "ebit_margin_target", "da_pct",
                "capex_pct", "sbc_pct", "tax_rate", "beta"):
        if key in overrides:
            data[key] = overrides[key]

    wacc   = wacc_pct / 100
    g      = g_pct    / 100
    spread = wacc - g
    forecast_years = max(len(data.get("forecast") or []), 7)
    if spread > 0:
        # Use stored terminal_ufcf (last forecast-year FCF) — do NOT back-calculate
        # from pv_terminal with the new wacc (that algebraically self-cancels).
        terminal_ufcf = data.get("terminal_ufcf") or (
            data["pv_terminal"] * (1 + orig_wacc_pct / 100) ** forecast_years
            * (orig_wacc_pct - orig_g_pct) / 100
            / (1 + orig_g_pct / 100)
        )

        # Recompute PV of terminal value at new wacc/g using Gordon Growth:
        # TV = UFCF_last * (1+g) / (WACC-g), discounted back FORECAST_YEARS=7 periods
        tv_new     = terminal_ufcf * (1 + g) / spread
        pv_tv_new  = tv_new / (1 + wacc) ** forecast_years

        # Scale PV UFCFs: same FCFs, discounted at new wacc instead of original
        af_new  = _annuity_factor(wacc_pct, forecast_years)
        af_orig = _annuity_factor(orig_wacc_pct, forecast_years)
        scale   = af_new / af_orig if af_orig > 0 else 1.0
        pv_ufcfs_new = data["pv_ufcfs"] * scale

        ev_new     = pv_ufcfs_new + pv_tv_new
        equity_new = ev_new - data["net_debt"]
        iv_new     = equity_new / data["diluted_shares"]
        upside_new = (iv_new - data["price"]) / data["price"] * 100

        data["enterprise_value"] = round(ev_new)
        data["equity_value"]     = round(equity_new)
        data["pv_ufcfs"]         = round(pv_ufcfs_new)
        data["pv_terminal"]      = round(pv_tv_new)
        data["tv_pct"]           = round(pv_tv_new / ev_new * 100, 1) if ev_new > 0 else 0
        data["intrinsic_value"]  = round(iv_new, 2)
        data["upside_pct"]       = round(upside_new, 1)

        if upside_new >= 15:
            data["recommendation"] = "Undervalued"
            data["recommendation_class"] = "green"
        elif upside_new >= -10:
            data["recommendation"] = "Fairly Valued"
            data["recommendation_class"] = "amber"
        else:
            data["recommendation"] = "Overvalued"
            data["recommendation_class"] = "red"

    # Recompute sensitivity (use stored or just-computed terminal_ufcf)
    t_ufcf = data.get("terminal_ufcf") or (
        data["pv_terminal"] * (1 + data["wacc"] / 100) ** forecast_years
        * (data["wacc"] - data["terminal_growth"]) / 100
        / (1 + data["terminal_growth"] / 100)
    )
    data["sensitivity"] = _sensitivity(
        terminal_ufcf=t_ufcf,
        pv_ufcfs=data["pv_ufcfs"],
        net_debt=data["net_debt"],
        diluted_shares=data["diluted_shares"],
        wacc_pcts=[data["wacc"] - 1.0, data["wacc"] - 0.5, data["wacc"],
                   data["wacc"] + 0.5, data["wacc"] + 1.0],
        g_pcts=[data["terminal_growth"] - 1.0, data["terminal_growth"] - 0.5,
                data["terminal_growth"], data["terminal_growth"] + 0.5,
                data["terminal_growth"] + 1.0],
        base_wacc=data["wacc"],
        base_g=data["terminal_growth"],
        forecast_years=forecast_years,
    )


# ─── Investment Memo builder ──────────────────────────────────────────────────

def _build_investment_memo(data: dict) -> dict:
    """Auto-generate a structured investment memo from dashboard data."""
    name     = data.get("company_name", data.get("ticker", ""))
    ticker   = data.get("ticker", "")
    price    = data.get("price", 0)
    iv       = data.get("intrinsic_value", 0)
    upside   = data.get("upside_pct", 0)
    rec      = data.get("recommendation", "")
    wacc     = data.get("wacc", 9.0)
    g        = data.get("terminal_growth", 2.5)
    tv_pct   = data.get("tv_pct", 70)
    conf     = data.get("confidence_score", 50)
    sector   = data.get("sector", "")
    industry = data.get("industry", "")
    desc     = data.get("description", "")
    ebit_b   = data.get("ebit_margin_base", 10)
    ebit_t   = data.get("ebit_margin_target", 12)
    rev_g    = data.get("revenue_growth_near", 5)
    mc       = data.get("market_cap", 0)
    ev       = data.get("enterprise_value", 0)
    net_debt = data.get("net_debt", 0)

    # Use existing analyst_view or generate defaults
    av = data.get("analyst_view", {})

    # Verdict
    if upside >= 20:
        verdict = "Strong Buy"
        verdict_class = "green"
        verdict_rationale = f"Trading at a {abs(upside):.0f}% discount to intrinsic value with strong FCF generation."
    elif upside >= 10:
        verdict = "Buy"
        verdict_class = "green"
        verdict_rationale = f"Trading at a {abs(upside):.0f}% discount to intrinsic value."
    elif upside >= -5:
        verdict = "Hold / Fairly Valued"
        verdict_class = "amber"
        verdict_rationale = f"Trading close to intrinsic value ({upside:+.1f}%). Margin of safety is thin."
    elif upside >= -20:
        verdict = "Underperform"
        verdict_class = "red"
        verdict_rationale = f"Trading at a {abs(upside):.0f}% premium to intrinsic value. Limited margin of safety."
    else:
        verdict = "Sell / Avoid"
        verdict_class = "red"
        verdict_rationale = f"Trading at a {abs(upside):.0f}% premium to intrinsic value. Significant downside risk."

    scenarios = data.get("scenarios", {})
    bull = scenarios.get("bull", {})
    bear = scenarios.get("bear", {})

    return {
        "ticker":           ticker,
        "company_name":     name,
        "date":             data.get("price_date", ""),
        "verdict":          verdict,
        "verdict_class":    verdict_class,
        "verdict_rationale": verdict_rationale,
        "confidence_score": conf,

        "company_overview": desc or f"{name} ({ticker}) is a {sector} company in the {industry} industry.",

        "valuation_summary": (
            av.get("valuation_says") or
            f"Our DCF model assigns an intrinsic value of ${iv:.2f} per share vs. current price of ${price:.2f}, "
            f"implying {upside:+.1f}% {'upside' if upside > 0 else 'downside'}. "
            f"We recommend {rec} at current levels."
        ),

        "key_drivers": [
            f"Revenue growth: {rev_g:.1f}% near-term, declining to {g:.1f}% terminal rate",
            f"EBIT margin expansion: {ebit_b:.1f}% → {ebit_t:.1f}% over the forecast period",
            f"WACC: {wacc:.1f}% | Terminal growth: {g:.1f}%",
            f"TV as % of EV: {tv_pct:.0f}% — {'moderate' if tv_pct < 70 else 'high'} terminal dependency",
        ],

        "bull_case": {
            "iv": bull.get("iv", iv * 1.4),
            "upside": bull.get("upside", 40),
            "narrative": bull.get("narrative", "Upside scenario: better growth and margins than base case."),
            "wacc": bull.get("wacc", wacc - 1),
            "g": bull.get("g", g + 0.5),
        },
        "bear_case": {
            "iv": bear.get("iv", iv * 0.6),
            "upside": bear.get("upside", -40),
            "narrative": bear.get("narrative", "Bear scenario: worse growth and margins than base case."),
            "wacc": bear.get("wacc", wacc + 1.5),
            "g": bear.get("g", g - 1),
        },

        "key_assumptions": (
            av.get("key_assumptions") or
            f"The model is most sensitive to: (1) terminal EBIT margin assumption of {ebit_t:.1f}%; "
            f"(2) terminal growth rate of {g:.1f}%; and (3) WACC of {wacc:.1f}%."
        ),

        "model_risks": (
            av.get("model_risks") or
            "Key risks to the base case: execution on margin expansion; competition; macro/rate sensitivity."
        ),

        "verify_before_use": av.get("verify_before_use", [
            "Review latest quarterly earnings vs. model assumptions",
            "Verify current analyst consensus estimates",
            "Check most recent balance sheet for debt changes",
        ]),

        "balance_sheet_note": (
            f"Net debt: ${net_debt:,.0f}M, Enterprise Value: ${ev:,.0f}M. "
            f"Net Debt/EV ratio: {net_debt/ev*100:.1f}%." if ev > 0 else "Balance sheet data unavailable."
        ),
    }


# ─── Market Expectations builder ─────────────────────────────────────────────

def _build_market_expectations(data: dict) -> dict:
    """Build market expectations section from reverse DCF + peer data."""
    rdcf = data.get("reverse_dcf") or {}

    price          = data.get("price", 0)
    iv             = data.get("intrinsic_value", 0)
    analyst_median = data.get("analyst_median", 0)
    analyst_low    = data.get("analyst_low", 0)
    analyst_high   = data.get("analyst_high", 0)
    wacc           = data.get("wacc", 9.0)
    model_g        = data.get("terminal_growth", 2.5)
    implied_g      = rdcf.get("implied_g", model_g - 1)
    implied_wacc   = rdcf.get("implied_wacc", wacc + 1)

    # Price vs. intrinsic value vs. analyst targets
    def _pct(a, b):
        return round((a - b) / b * 100, 1) if b else 0

    price_vs_iv    = _pct(price, iv)
    price_vs_cons  = _pct(price, analyst_median) if analyst_median else None

    # Signal interpretation
    if price_vs_iv >= 15:
        signal = "Market is Optimistic"
        signal_class = "red"
        signal_note = "The stock is pricing in significantly better outcomes than the base-case DCF."
    elif price_vs_iv >= -10:
        signal = "Market is Fairly Priced"
        signal_class = "amber"
        signal_note = "The market price is broadly consistent with our DCF assumptions."
    else:
        signal = "Market is Pessimistic"
        signal_class = "green"
        signal_note = "The stock is pricing in worse outcomes than our base case — potential upside."

    return {
        "price":             price,
        "iv":                iv,
        "analyst_median":    analyst_median,
        "analyst_low":       analyst_low,
        "analyst_high":      analyst_high,
        "price_vs_iv_pct":   price_vs_iv,
        "price_vs_cons_pct": price_vs_cons,
        "signal":            signal,
        "signal_class":      signal_class,
        "signal_note":       signal_note,
        "implied_g":         implied_g,
        "model_g":           model_g,
        "implied_wacc":      implied_wacc,
        "model_wacc":        wacc,
        "g_gap_bps":         round((model_g - implied_g) * 100),
        "wacc_gap_bps":      round((implied_wacc - wacc) * 100),
        "narrative":         rdcf.get("narrative", ""),
        "sensitivity_curve": rdcf.get("sensitivity_curve", []),
    }

