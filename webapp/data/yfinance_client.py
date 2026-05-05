"""
webapp/data/yfinance_client.py
──────────────────────────────
Live DCF dashboard data from Yahoo Finance (yfinance). No API key required.

Fetches: income statement, balance sheet, cash flow, price/quote, analyst targets.
Runs:    full 7-year UFCF DCF with mid-year discounting, sensitivity table,
         bear/base/bull scenarios, financial scores (Altman Z, Piotroski F),
         DuPont decomposition, earnings quality, analyst consensus.

Returns: a dict matching the exact schema expected by dashboard.html / app.js.
Falls back: returns None on any exception — caller uses hardcoded sample data.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logger.warning("yfinance not installed — live data unavailable.")

try:
    from webapp.data.peer_lists import get_peers_for_ticker, fetch_peer_metrics
    _PEERS_AVAILABLE = True
except ImportError:
    try:
        from peer_lists import get_peers_for_ticker, fetch_peer_metrics
        _PEERS_AVAILABLE = True
    except ImportError:
        _PEERS_AVAILABLE = False


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_risk_free_rate() -> float:
    """Fetch 10-yr Treasury yield from FRED (no key needed). Fallback 4.4."""
    try:
        from webapp.data.fmp_client import get_treasury_rate
        rate = get_treasury_rate()
        return float(rate) if rate else 4.4
    except Exception:
        return 4.4


def _v(series: Any, key: str, default: float = 0.0) -> float:
    """Safe numeric get from a pandas Series / dict. Returns float."""
    try:
        val = series.get(key) if hasattr(series, "get") else series[key]
        if val is None:
            return float(default)
        f = float(val)
        import math
        return float(default) if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return float(default)


def _col(df: Any, idx: int = 0) -> Any:
    """Return column *idx* of a DataFrame as a Series (or empty dict on fail)."""
    try:
        if df is None or df.empty or idx >= len(df.columns):
            return {}
        return df[df.columns[idx]]
    except Exception:
        return {}


def _hist(df: Any, key: str, n: int = 10, div: float = 1e6) -> list[float]:
    """Extract up to n years of *key* from df (newest first → reversed to oldest-first).

    Returns a list of floats in millions (divided by *div*).
    """
    out = []
    for i in range(min(n, len(df.columns) if df is not None and not df.empty else 0)):
        val = _v(_col(df, i), key)
        out.append(round(val / div))
    return list(reversed(out))


def _reconstruct_quarterly_history(
    t: Any,
    existing_fy_years: list[int],
) -> dict:
    """Attempt to reconstruct additional annual periods from quarterly data.

    Groups quarterly statements by calendar year; if a year has 4 quarters
    available it sums income-statement / CF items and uses Q4 for balance-sheet
    snapshot.  Only years NOT already in *existing_fy_years* are returned.

    Returns a dict  {year: {metric_key: value_in_M, ...}}
    """
    result: dict = {}
    try:
        q_fin = t.quarterly_financials
        q_bs  = t.quarterly_balance_sheet
        q_cf  = t.quarterly_cashflow

        if q_fin is None or q_fin.empty:
            return result

        from collections import defaultdict
        fy_cols: dict = defaultdict(list)
        for col in q_fin.columns:
            fy_cols[col.year].append(col)

        for year, cols in sorted(fy_cols.items()):
            if year in existing_fy_years:
                continue
            if len(cols) < 4:
                continue  # incomplete year

            def _sum_metric(df, metric):
                total = 0.0
                for c in cols:
                    try:
                        total += abs(_v(df[c], metric)) if metric == "Capital Expenditure" else _v(df[c], metric)
                    except Exception:
                        pass
                return total

            rev    = _sum_metric(q_fin, "Total Revenue")
            if rev <= 0:
                continue  # sanity check

            gp     = _sum_metric(q_fin, "Gross Profit")
            ebit   = _sum_metric(q_fin, "EBIT")
            ni     = _sum_metric(q_fin, "Net Income")
            tax    = _sum_metric(q_fin, "Tax Provision")
            preinc = _sum_metric(q_fin, "Pretax Income")

            op_cf  = 0.0; capex = 0.0; da = 0.0; sbc = 0.0; fcf = 0.0
            if q_cf is not None and not q_cf.empty:
                op_cf = _sum_metric(q_cf, "Operating Cash Flow")
                capex = _sum_metric(q_cf, "Capital Expenditure")
                da    = _sum_metric(q_cf, "Depreciation And Amortization")
                sbc   = _sum_metric(q_cf, "Stock Based Compensation")
                fcf_val = _sum_metric(q_cf, "Free Cash Flow")
                fcf = fcf_val if fcf_val != 0 else op_cf - capex

            # Balance sheet: use latest Q4 (or latest available)
            td = 0.0; ta = 0.0
            if q_bs is not None and not q_bs.empty:
                q4_col = max(cols)
                try:
                    td = _v(q_bs[q4_col], "Total Debt")
                    ta = _v(q_bs[q4_col], "Total Assets")
                except Exception:
                    pass

            result[year] = {
                "revenue":      round(rev   / 1e6),
                "gross_profit": round(gp    / 1e6),
                "ebit":         round(ebit  / 1e6),
                "net_income":   round(ni    / 1e6),
                "op_cf":        round(op_cf / 1e6),
                "capex":        round(capex / 1e6),
                "da":           round(da    / 1e6),
                "sbc":          round(sbc   / 1e6),
                "fcf":          round(fcf   / 1e6),
                "total_debt":   round(td    / 1e6),
                "total_assets": round(ta    / 1e6),
                "tax":          round(tax   / 1e6),
                "pretax":       round(preinc/ 1e6),
                "source":       "quarterly_reconstruction",
            }
    except Exception as exc:
        logger.debug("Quarterly reconstruction failed: %s", exc)
    return result


def _sensitivity(terminal_ufcf: float, pv_ufcfs: float,
                 net_debt: float, diluted_shares: float,
                 wacc_pcts: list, g_pcts: list,
                 base_wacc: float, base_g: float,
                 forecast_years: int = 7) -> dict:
    """WACC × g sensitivity grid (same formula as samples.py)."""
    values = []
    for g_pct in g_pcts:
        row = []
        for w_pct in wacc_pcts:
            w = w_pct / 100
            g = g_pct / 100
            spread = w - g
            if spread < 0.005:
                row.append(None)
                continue
            tv = terminal_ufcf / spread
            pv_tv = tv / (1 + w) ** (forecast_years + 0.5)
            # Scale pv_ufcfs for this WACC (annuity factor ratio)
            af_new  = sum(1 / (1 + w) ** t for t in range(1, forecast_years + 1))
            af_base = sum(1 / (1 + base_wacc / 100) ** t for t in range(1, forecast_years + 1))
            pv_uf_scaled = pv_ufcfs * (af_new / af_base) if af_base > 0 else pv_ufcfs
            ev = pv_uf_scaled + pv_tv
            eq = ev - net_debt
            iv = eq / diluted_shares if diluted_shares > 0 else 0
            row.append(round(iv, 1))
        values.append(row)
    return {
        "wacc_labels":    [f"{w:.1f}%" for w in wacc_pcts],
        "g_labels":       [f"{g:.1f}%" for g in g_pcts],
        "iv_grid":        values,
        "base_wacc_idx":  wacc_pcts.index(base_wacc),
        "base_g_idx":     g_pcts.index(base_g),
    }


def is_available() -> bool:
    return _YF_AVAILABLE


def _safe_last_price(ticker_obj: Any, info: dict[str, Any]) -> float:
    fast_info = getattr(ticker_obj, "fast_info", None)
    if fast_info is not None:
        try:
            price = float(getattr(fast_info, "last_price", 0) or 0)
            if price > 0:
                return price
        except Exception:
            pass

    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        try:
            price = float((info or {}).get(key) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price
    return 0.0


def _safe_fast_info_value(fast_info: Any, attribute: str, fallback: float = 0.0) -> float:
    if fast_info is None:
        return float(fallback)
    try:
        value = getattr(fast_info, attribute, None)
        if value in (None, 0, 0.0):
            return float(fallback)
        numeric = float(value)
        return numeric if numeric > 0 else float(fallback)
    except Exception:
        return float(fallback)


# ─── Main builder ─────────────────────────────────────────────────────────────

def build_dashboard_data(ticker: str) -> dict | None:  # noqa: C901
    """
    Fetch live data from yfinance and run a full 7-year DCF.
    Returns the complete dashboard dict, or None on any failure.
    """
    if not _YF_AVAILABLE:
        return None

    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        fast_info = getattr(t, "fast_info", None)

        # Must have a valid price
        price = _safe_last_price(t, info)
        if price <= 0:
            logger.warning("yfinance: no price for %s", ticker)
            return None

        fin = t.financials    # income statement  — cols = newest first
        bs  = t.balance_sheet # balance sheet
        cf  = t.cashflow      # cash flow statement

        if fin is None or fin.empty or bs is None or bs.empty or cf is None or cf.empty:
            logger.warning("yfinance: no financials for %s", ticker)
            return None

        n_fin = len(fin.columns)
        n_bs  = len(bs.columns)
        n_cf  = len(cf.columns)
        # Use ALL available annual history (up to 10 years)
        n     = min(n_fin, n_bs, n_cf, 10)

        # Fiscal year labels (newest first → sorted for charts)
        fy_years_desc = [int(fin.columns[i].year) for i in range(n)]
        fy_years_asc  = sorted(fy_years_desc)

        # ── Try to extend history via quarterly reconstruction ──────────
        _quarterly_extra = _reconstruct_quarterly_history(t, set(fy_years_asc))
        _extra_sorted    = sorted(_quarterly_extra.keys())  # oldest-first extra years

        # ── Historical arrays from annual filings (oldest-first, $M) ──────
        revenues       = _hist(fin, "Total Revenue",        n)
        gross_profits  = _hist(fin, "Gross Profit",         n)
        ebits          = _hist(fin, "EBIT",                 n)
        net_incomes    = _hist(fin, "Net Income",           n)
        op_cfs         = _hist(cf,  "Operating Cash Flow",  n)
        capexes        = [round(abs(_v(_col(cf, i), "Capital Expenditure")) / 1e6)
                          for i in range(min(n, n_cf))]
        capexes        = list(reversed(capexes))
        fcfs           = _hist(cf,  "Free Cash Flow",       n)
        das            = _hist(cf,  "Depreciation And Amortization", n)
        sbcs           = _hist(cf,  "Stock Based Compensation", n)
        total_debts_h  = _hist(bs,  "Total Debt",           n)
        shares_h       = [round(_v(_col(fin, i), "Diluted Average Shares") / 1e6, 1)
                          for i in range(min(n, n_fin))]
        shares_h       = list(reversed(shares_h))

        # ── Prepend quarterly-reconstructed years if available ──────────
        if _extra_sorted:
            _prepend_years  = [y for y in _extra_sorted if y < min(fy_years_asc)]
            if _prepend_years:
                ex = _quarterly_extra
                revenues      = [ex[y]["revenue"]      for y in _prepend_years] + revenues
                gross_profits = [ex[y]["gross_profit"] for y in _prepend_years] + gross_profits
                ebits         = [ex[y]["ebit"]         for y in _prepend_years] + ebits
                net_incomes   = [ex[y]["net_income"]   for y in _prepend_years] + net_incomes
                op_cfs        = [ex[y]["op_cf"]        for y in _prepend_years] + op_cfs
                capexes       = [ex[y]["capex"]        for y in _prepend_years] + capexes
                fcfs          = [ex[y]["fcf"]          for y in _prepend_years] + fcfs
                das           = [ex[y]["da"]           for y in _prepend_years] + das
                sbcs          = [ex[y]["sbc"]          for y in _prepend_years] + sbcs
                total_debts_h = [ex[y]["total_debt"]   for y in _prepend_years] + total_debts_h
                shares_h      = [0.0] * len(_prepend_years) + shares_h
                fy_years_asc  = _prepend_years + fy_years_asc
                n             = len(revenues)

        gross_margins  = [round(g / r * 100, 1) if r else 0
                          for g, r in zip(gross_profits, revenues)]
        ebit_margins   = [round(e / r * 100, 1) if r else 0
                          for e, r in zip(ebits, revenues)]

        # ROIC = NOPAT / Invested Capital  (use all available annual years)
        roics = []
        for i in range(min(n, n_fin, n_bs)):
            # i=0 is most recent; reverse at end
            ebit_i  = _v(_col(fin, i), "EBIT")
            pre_i   = _v(_col(fin, i), "Pretax Income")
            tax_i   = _v(_col(fin, i), "Tax Provision")
            tr_i    = tax_i / pre_i if pre_i > 0 else 0.21
            nopat_i = ebit_i * (1 - tr_i)
            ic_i    = _v(_col(bs, i), "Invested Capital")
            roics.append(round(nopat_i / ic_i * 100, 1) if ic_i > 0 else 0.0)
        # Prepend zeros for any quarterly-reconstructed prepended years
        roics = list(reversed(roics))
        n_extra_prepended = len(fy_years_asc) - len(roics)
        if n_extra_prepended > 0:
            roics = [0.0] * n_extra_prepended + roics

        # Count quarterly periods available for reporting
        try:
            _n_quarterly = len(t.quarterly_financials.columns) if t.quarterly_financials is not None else 0
        except Exception:
            _n_quarterly = 0

        # ── Most-recent year scalars ────────────────────────────────────────
        l_fin = _col(fin, 0)
        l_bs  = _col(bs,  0)
        l_cf  = _col(cf,  0)

        revenue_base    = _v(l_fin, "Total Revenue")    / 1e6
        gross_profit    = _v(l_fin, "Gross Profit")     / 1e6
        ebit_base       = _v(l_fin, "EBIT")             / 1e6
        net_income_base = _v(l_fin, "Net Income")       / 1e6
        pretax_income   = _v(l_fin, "Pretax Income")    / 1e6
        tax_prov        = _v(l_fin, "Tax Provision")    / 1e6
        # Prefer current shares outstanding from info (more accurate than historical
        # weighted-average diluted shares from the income statement, which can be
        # significantly stale after a year of buybacks).
        _info_shares = float(info.get("sharesOutstanding") or 0) / 1e6
        _stmt_shares = _v(l_fin, "Diluted Average Shares") / 1e6
        if _info_shares > 10:  # sanity: must be > 10M
            diluted_shares = _info_shares
        elif _stmt_shares > 10:
            diluted_shares = _stmt_shares
        else:
            diluted_shares = _safe_fast_info_value(fast_info, "shares") / 1e6 or 100.0

        total_assets    = _v(l_bs, "Total Assets")      / 1e6
        total_debt      = _v(l_bs, "Total Debt")        / 1e6
        long_term_debt  = _v(l_bs, "Long Term Debt")    / 1e6
        cash            = _v(l_bs, "Cash And Cash Equivalents") / 1e6
        cash_broad      = _v(l_bs, "Cash Cash Equivalents And Short Term Investments") / 1e6
        if cash_broad > cash:
            cash = cash_broad
        inventory       = _v(l_bs, "Inventory")         / 1e6
        accounts_recv   = _v(l_bs, "Accounts Receivable") / 1e6
        accounts_pay    = _v(l_bs, "Accounts Payable")  / 1e6
        total_liab      = _v(l_bs, "Total Liabilities Net Minority Interest") / 1e6
        stockholders_eq = _v(l_bs, "Stockholders Equity") / 1e6
        retained_earn   = _v(l_bs, "Retained Earnings") / 1e6
        working_cap     = _v(l_bs, "Working Capital")   / 1e6
        current_assets  = _v(l_bs, "Current Assets")    / 1e6
        current_liab    = _v(l_bs, "Current Liabilities") / 1e6

        operating_cf    = _v(l_cf, "Operating Cash Flow")        / 1e6
        capex           = abs(_v(l_cf, "Capital Expenditure"))   / 1e6
        da              = _v(l_cf, "Depreciation And Amortization") / 1e6
        sbc             = _v(l_cf, "Stock Based Compensation")   / 1e6
        interest_paid   = _v(l_cf, "Interest Paid Supplemental Data") / 1e6
        buyback_raw     = abs(_v(l_cf, "Repurchase Of Capital Stock")) / 1e6

        net_debt  = total_debt - cash
        market_cap = _safe_fast_info_value(
            fast_info,
            "market_cap",
            float(info.get("marketCap") or price * diluted_shares * 1e6),
        ) / 1e6

        # ── WACC ───────────────────────────────────────────────────────────
        beta     = float(info.get("beta") or 1.0)
        beta     = max(0.3, min(3.0, beta))
        rf_rate  = _get_risk_free_rate()
        erp      = 5.2
        ke       = rf_rate + beta * erp

        kd_pre   = (interest_paid / total_debt * 100) if total_debt > 1 and interest_paid > 0 else 4.5
        kd_pre   = max(2.0, min(12.0, kd_pre))

        tax_rate_pct = (tax_prov / pretax_income * 100) if pretax_income > 1 else 21.0
        tax_rate_pct = max(5.0, min(35.0, tax_rate_pct))

        kd_post  = kd_pre * (1 - tax_rate_pct / 100)

        equity_val = price * diluted_shares   # $M (shares already in M)
        total_cap  = equity_val + total_debt
        e_wt       = equity_val / total_cap * 100 if total_cap > 0 else 85.0
        d_wt       = 100 - e_wt

        wacc = round(max(5.0, min(20.0, (e_wt / 100) * ke + (d_wt / 100) * kd_post)), 1)

        # ── Operating assumptions ──────────────────────────────────────────
        ebit_margin_base_pct  = ebit_base  / revenue_base * 100 if revenue_base > 0 else 10.0
        gross_margin_base_pct = gross_profit / revenue_base * 100 if revenue_base > 0 else 40.0
        da_pct    = da    / revenue_base * 100 if revenue_base > 0 else 2.5
        capex_pct = capex / revenue_base * 100 if revenue_base > 0 else 3.0
        sbc_pct   = sbc   / revenue_base * 100 if revenue_base > 0 else 1.5

        # Revenue CAGR over the full available history (up to 4 years)
        # Use revenues[0] → revenues[-1] with the correct exponent = n_years.
        if len(revenues) >= 2 and revenues[0] > 0 and revenues[-1] > 0:
            n_rev_years = len(revenues) - 1   # e.g. 3 for 4 data points
            rev_cagr = (revenues[-1] / revenues[0]) ** (1.0 / n_rev_years) - 1
            revenue_growth_near = round(max(-15.0, min(40.0, rev_cagr * 100)), 1)
        else:
            revenue_growth_near = 5.0

        terminal_growth = 2.5

        # EBIT margin target: base + (historical peak - base) * 0.6, capped at base+5
        hist_peak = max(ebit_margins) if ebit_margins else ebit_margin_base_pct
        ebit_margin_target = round(
            min(ebit_margin_base_pct + 5.0, max(ebit_margin_base_pct,
                ebit_margin_base_pct + (hist_peak - ebit_margin_base_pct) * 0.6)), 1)
        ebit_margin_target = max(ebit_margin_base_pct, ebit_margin_target)

        # Working capital days
        cogs = revenue_base - gross_profit
        dso  = round(accounts_recv / revenue_base * 365, 1) if accounts_recv > 0 and revenue_base > 0 else 30.0
        dio  = round(inventory     / cogs * 365, 1)         if inventory > 0 and cogs > 0 else 60.0
        dpo  = round(accounts_pay  / cogs * 365, 1)         if accounts_pay > 0 and cogs > 0 else 40.0

        # ── 7-year DCF forecast ───────────────────────────────────────────
        FORECAST_YEARS = 7
        forecast   = []
        pv_ufcfs   = 0.0
        prev_rev   = revenue_base
        prev_ar    = accounts_recv
        prev_inv   = inventory
        prev_ap    = accounts_pay

        for n_yr in range(1, FORECAST_YEARS + 1):
            # Revenue growth: linear taper near→terminal
            alpha = (n_yr - 1) / max(FORECAST_YEARS - 1, 1)
            g_yr  = revenue_growth_near * (1 - alpha) + terminal_growth * alpha
            rev_n = prev_rev * (1 + g_yr / 100)

            # Margin: linear base→target
            margin_n = ebit_margin_base_pct + (ebit_margin_target - ebit_margin_base_pct) * n_yr / FORECAST_YEARS
            ebit_n   = rev_n * margin_n / 100
            nopat_n  = ebit_n * (1 - tax_rate_pct / 100)
            da_n     = rev_n * da_pct    / 100
            sbc_n    = rev_n * sbc_pct   / 100
            capex_n  = rev_n * capex_pct / 100

            # NWC change
            cogs_n = rev_n * (1 - gross_margin_base_pct / 100)
            ar_n   = rev_n  * dso / 365
            inv_n  = cogs_n * dio / 365 if cogs_n > 0 else rev_n * 0.15
            ap_n   = cogs_n * dpo / 365 if cogs_n > 0 else rev_n * 0.08
            d_nwc  = (ar_n - prev_ar) + (inv_n - prev_inv) - (ap_n - prev_ap)

            ufcf_n = nopat_n + da_n + sbc_n - capex_n - d_nwc
            df_n   = 1 / (1 + wacc / 100) ** (n_yr - 0.5)
            pv_n   = ufcf_n * df_n
            pv_ufcfs += pv_n

            forecast.append({
                "year":    f"FY{fy_years_asc[-1] + n_yr}",
                "n":       n_yr,
                "revenue": round(rev_n),
                "ebit_m":  round(margin_n, 1),
                "ebit":    round(ebit_n),
                "nopat":   round(nopat_n),
                "da":      round(da_n),
                "sbc":     round(sbc_n),
                "capex":   round(capex_n),
                "d_nowc":  round(d_nwc),
                "ufcf":    round(ufcf_n),
                "df":      round(df_n, 4),
                "pv":      round(pv_n),
            })
            prev_rev = rev_n
            prev_ar, prev_inv, prev_ap = ar_n, inv_n, ap_n

        pv_ufcfs = round(pv_ufcfs)

        # Terminal value (NIKE convention: TV = last_ufcf / (wacc - g))
        terminal_ufcf = forecast[-1]["ufcf"]
        spread = max(wacc / 100 - terminal_growth / 100, 0.005)
        tv     = terminal_ufcf / spread
        pv_tv  = round(tv / (1 + wacc / 100) ** (FORECAST_YEARS + 0.5))

        ev           = pv_ufcfs + pv_tv
        equity_value = max(0, ev - net_debt)
        iv           = equity_value / diluted_shares if diluted_shares > 0 else 0
        upside       = (iv - price) / price * 100 if price > 0 else 0
        tv_pct       = round(pv_tv / ev * 100, 1) if ev > 0 else 0

        rec       = "Undervalued" if upside >= 15 else ("Fairly Valued" if upside >= -10 else "Overvalued")
        rec_class = "green"       if upside >= 15 else ("amber"        if upside >= -10 else "red")

        # ── 52-week range ─────────────────────────────────────────────────
        try:
            year_high = _safe_fast_info_value(fast_info, "year_high", float(info.get("fiftyTwoWeekHigh") or price * 1.2))
            year_low  = _safe_fast_info_value(fast_info, "year_low",  float(info.get("fiftyTwoWeekLow")  or price * 0.8))
        except Exception:
            year_high = price * 1.2
            year_low  = price * 0.8

        # ── Analyst targets ───────────────────────────────────────────────
        analyst_median = float(info.get("targetMeanPrice")   or 0)
        analyst_low    = float(info.get("targetLowPrice")    or 0)
        analyst_high   = float(info.get("targetHighPrice")   or 0)
        n_analysts     = int(info.get("numberOfAnalystOpinions") or 0)
        fwd_eps        = float(info.get("forwardEps")        or 0)

        # ── Yields ────────────────────────────────────────────────────────
        div_yield_raw   = float(info.get("dividendYield") or 0)
        dividend_yield  = round(div_yield_raw * 100 if div_yield_raw < 0.5 else div_yield_raw, 2)
        buyback_yield   = round(buyback_raw / market_cap * 100, 1) if market_cap > 0 else 0.0

        # ── Sensitivity table ─────────────────────────────────────────────
        wacc_pcts = [round(wacc - 1.0, 1), round(wacc - 0.5, 1), round(wacc, 1),
                     round(wacc + 0.5, 1), round(wacc + 1.0, 1)]
        g_pcts    = [round(terminal_growth - 1.0, 1), round(terminal_growth - 0.5, 1),
                     round(terminal_growth, 1), round(terminal_growth + 0.5, 1),
                     round(terminal_growth + 1.0, 1)]
        sens = _sensitivity(
            terminal_ufcf=terminal_ufcf,
            pv_ufcfs=pv_ufcfs,
            net_debt=net_debt,
            diluted_shares=diluted_shares,
            wacc_pcts=wacc_pcts,
            g_pcts=g_pcts,
            base_wacc=wacc,
            base_g=terminal_growth,
        )

        # ── Scenarios ─────────────────────────────────────────────────────
        def _quick_iv(w_p: float, g_p: float, ufcf_mult: float, ufcf_pv_mult: float) -> tuple[float, float]:
            sp = max(w_p / 100 - g_p / 100, 0.005)
            t  = terminal_ufcf * ufcf_mult / sp
            p  = t / (1 + w_p / 100) ** (FORECAST_YEARS + 0.5)
            af_n = sum(1 / (1 + w_p / 100) ** j for j in range(1, FORECAST_YEARS + 1))
            af_b = sum(1 / (1 + wacc   / 100) ** j for j in range(1, FORECAST_YEARS + 1))
            pu   = pv_ufcfs * (af_n / af_b) * ufcf_pv_mult if af_b > 0 else pv_ufcfs * ufcf_pv_mult
            ev_  = pu + p
            eq_  = max(0, ev_ - net_debt)
            iv_  = round(eq_ / diluted_shares, 2) if diluted_shares > 0 else 0
            up_  = round((iv_ - price) / price * 100, 1) if price > 0 else 0
            return iv_, up_, round(ev_)

        bull_wacc, bull_g   = round(wacc - 1.0, 1), round(terminal_growth + 0.5, 1)
        bear_wacc, bear_g   = round(wacc + 1.5, 1), round(terminal_growth - 1.0, 1)
        bull_iv, bull_up, bull_ev = _quick_iv(bull_wacc, bull_g, 1.15, 1.05)
        bear_iv, bear_up, bear_ev = _quick_iv(bear_wacc, bear_g, 0.85, 0.95)
        bull_rec = "Undervalued" if bull_up >= 15 else "Fairly Valued"
        bear_rec = "Overvalued"  if bear_up < -10 else "Fairly Valued"

        # ── Financial scores ──────────────────────────────────────────────
        financial_scores = None
        try:
            from webapp.data.financial_scores import (
                compute_altman_z, compute_piotroski_f,
                compute_dupont, compute_earnings_quality,
            )
            l_bs_p  = _col(bs,  1) if n_bs  >= 2 else l_bs
            l_fin_p = _col(fin, 1) if n_fin >= 2 else l_fin
            financial_scores = {
                "altman_z": compute_altman_z(
                    working_capital    = round(working_cap),
                    total_assets       = round(total_assets),
                    retained_earnings  = round(retained_earn),
                    ebit               = round(ebit_base),
                    market_cap         = round(market_cap),
                    total_liabilities  = round(total_liab),
                    revenue            = round(revenue_base),
                ),
                "piotroski_f": compute_piotroski_f(
                    net_income               = round(net_income_base),
                    total_assets             = round(total_assets),
                    operating_cash_flow      = round(operating_cf),
                    long_term_debt           = round(long_term_debt),
                    current_assets           = round(current_assets),
                    current_liabilities      = round(current_liab),
                    shares_outstanding       = round(diluted_shares),
                    gross_profit             = round(gross_profit),
                    revenue                  = round(revenue_base),
                    net_income_prev          = round(_v(l_fin_p, "Net Income")              / 1e6),
                    total_assets_prev        = round(_v(l_bs_p,  "Total Assets")             / 1e6),
                    long_term_debt_prev      = round(_v(l_bs_p,  "Long Term Debt")            / 1e6),
                    current_assets_prev      = round(_v(l_bs_p,  "Current Assets")            / 1e6),
                    current_liabilities_prev = round(_v(l_bs_p,  "Current Liabilities")       / 1e6),
                    shares_prev              = round(_v(l_fin_p,  "Diluted Average Shares")   / 1e6),
                    gross_profit_prev        = round(_v(l_fin_p,  "Gross Profit")             / 1e6),
                    revenue_prev             = round(_v(l_fin_p,  "Total Revenue")            / 1e6),
                ),
            }
        except Exception as exc:
            logger.warning("financial_scores failed for %s: %s", ticker, exc)

        dupont           = None
        earnings_quality = None
        try:
            total_assets_h = _hist(bs,  "Total Assets",        n)
            equity_h       = _hist(bs,  "Stockholders Equity", n)
            dupont = compute_dupont(
                years       = fy_years_asc,
                net_income  = net_incomes,
                revenue     = revenues,
                total_assets= total_assets_h,
                equity      = equity_h,
            )
            earnings_quality = compute_earnings_quality(
                years       = fy_years_asc,
                net_income  = net_incomes,
                operating_cf= op_cfs,
                fcf         = fcfs,
            )
        except Exception as exc:
            logger.warning("dupont/eq failed for %s: %s", ticker, exc)

        analyst_consensus = {
            "revenue_y1_consensus": round(revenue_base * (1 + revenue_growth_near / 100)),
            "revenue_y1_model":     forecast[0]["revenue"] if forecast else 0,
            "eps_y1_consensus":     round(fwd_eps, 2),
            "buy_count":  0,
            "hold_count": 0,
            "sell_count": 0,
            "total_analysts": n_analysts,
            "mean_target": round(analyst_median, 2),
        }

        # ── Assumption rows ───────────────────────────────────────────────
        assumptions = [
            {"driver": "Revenue Growth (Near-Term)", "auto": revenue_growth_near, "active": revenue_growth_near, "unit": "%",    "mode": "AUTO", "source": "3-yr historical CAGR", "warn": None},
            {"driver": "Revenue Growth (Terminal)",  "auto": terminal_growth,     "active": terminal_growth,     "unit": "%",    "mode": "AUTO", "source": "Long-run nominal GDP proxy", "warn": None},
            {"driver": "EBIT Margin (Base)",         "auto": round(ebit_margin_base_pct, 1),  "active": round(ebit_margin_base_pct, 1),  "unit": "%", "mode": "AUTO", "source": "LTM EBIT / Revenue", "warn": None},
            {"driver": "EBIT Margin (Target Y7)",    "auto": round(ebit_margin_target, 1),    "active": round(ebit_margin_target, 1),    "unit": "%", "mode": "AUTO", "source": "Historical peak margin (60% reversion)", "warn": None},
            {"driver": "WACC",                       "auto": wacc,  "active": wacc,  "unit": "%", "mode": "AUTO", "source": f"CAPM: Rf {round(rf_rate, 1)}% + β {round(beta, 2)} × ERP {erp}%", "warn": None},
            {"driver": "Cost of Debt (Pre-Tax)",     "auto": round(kd_pre, 1),  "active": round(kd_pre, 1),  "unit": "%", "mode": "AUTO", "source": "Interest paid / total debt", "warn": None},
            {"driver": "Beta (Levered)",             "auto": round(beta, 2),    "active": round(beta, 2),    "unit": "×",  "mode": "AUTO", "source": "Yahoo Finance beta", "warn": None},
            {"driver": "Tax Rate",                   "auto": round(tax_rate_pct, 1), "active": round(tax_rate_pct, 1), "unit": "%", "mode": "AUTO", "source": "LTM effective tax rate", "warn": None},
            {"driver": "D&A % Revenue",              "auto": round(da_pct, 1),    "active": round(da_pct, 1),    "unit": "%", "mode": "AUTO", "source": "LTM D&A / Revenue", "warn": None},
            {"driver": "CapEx % Revenue",            "auto": round(capex_pct, 1), "active": round(capex_pct, 1), "unit": "%", "mode": "AUTO", "source": "LTM CapEx / Revenue", "warn": None},
            {"driver": "SBC % Revenue",              "auto": round(sbc_pct, 1),   "active": round(sbc_pct, 1),   "unit": "%", "mode": "AUTO", "source": "LTM SBC / Revenue", "warn": None},
            {"driver": "DSO (Days Sales Outstanding)", "auto": round(dso, 1), "active": round(dso, 1), "unit": "days", "mode": "AUTO", "source": "LTM AR / (Rev/365)", "warn": None},
            {"driver": "DIO (Days Inventory Outst.)", "auto": round(dio, 1),  "active": round(dio, 1),  "unit": "days", "mode": "AUTO", "source": "LTM Inventory / (COGS/365)", "warn": None},
            {"driver": "DPO (Days Payable Outst.)",  "auto": round(dpo, 1),   "active": round(dpo, 1),   "unit": "days", "mode": "AUTO", "source": "LTM AP / (COGS/365)", "warn": None},
            {"driver": "Buyback Yield",              "auto": buyback_yield,   "active": buyback_yield,   "unit": "%", "mode": "AUTO", "source": "LTM buybacks / market cap", "warn": None},
            {"driver": "Dividend Yield",             "auto": dividend_yield,  "active": dividend_yield,  "unit": "%", "mode": "AUTO", "source": "Current dividend / price", "warn": None},
        ]

        # ── Validation flags ──────────────────────────────────────────────
        flags = [
            {"name": "Data Freshness",  "status": "pass", "message": f"Live data from Yahoo Finance. {n} years of annual financials available."},
            {"name": "Revenue Sanity",  "status": "pass" if revenue_base > 10 else "warn", "message": f"Latest annual revenue: ${revenue_base:,.0f}M."},
            {"name": "WACC Range",      "status": "pass", "message": f"WACC {wacc}% computed from CAPM (β={beta:.2f}, Rf={rf_rate:.1f}%, ERP={erp}%)."},
            {"name": "WACC–g Spread",   "status": "pass" if wacc - terminal_growth >= 0.5 else "fail",
             "message": f"Spread {wacc - terminal_growth:.1f}pp {'above' if wacc - terminal_growth >= 0.5 else 'below'} 50bp minimum."},
            {"name": "TV % of EV",      "status": "warn" if tv_pct > 70 else "pass", "message": f"Terminal value = {tv_pct}% of EV."},
            {"name": "Net Debt Sign",   "status": "pass" if net_debt < revenue_base * 3 else "warn",
             "message": f"Net debt ${net_debt:,.0f}M vs revenue ${revenue_base:,.0f}M."},
        ]

        company_name = info.get("longName") or info.get("shortName") or ticker
        description  = info.get("longBusinessSummary") or f"{company_name} is a publicly traded company."
        if len(description) > 400:
            description = description[:397] + "..."

        _result = {
            # ── Identity ──────────────────────────────────────────────────
            "ticker":        ticker.upper(),
            "company_name":  company_name,
            "exchange":      info.get("exchange") or "",
            "currency":      info.get("currency", "USD"),
            "sector":        info.get("sector", ""),
            "industry":      info.get("industry", ""),
            "description":   description,

            # ── Market data ───────────────────────────────────────────────
            "price":              round(price, 2),
            "price_date":         str(datetime.utcnow().date()),
            "market_cap":         round(market_cap),
            "fifty_two_week_low": round(year_low,  2),
            "fifty_two_week_high":round(year_high, 2),
            "analyst_low":        round(analyst_low,    2),
            "analyst_high":       round(analyst_high,   2),
            "analyst_median":     round(analyst_median, 2),

            # ── Valuation output ──────────────────────────────────────────
            "intrinsic_value":     round(iv, 2),
            "upside_pct":          round(upside, 1),
            "recommendation":      rec,
            "recommendation_class":rec_class,
            "confidence_score":    70,   # recomputed by confidence engine
            "data_freshness":      "Live (yfinance)",

            # ── DCF bridge ────────────────────────────────────────────────
            "enterprise_value": round(ev),
            "equity_value":     round(equity_value),
            "pv_ufcfs":         pv_ufcfs,
            "pv_terminal":      pv_tv,
            "tv_pct":           tv_pct,
            "diluted_shares":   round(diluted_shares, 1),
            "terminal_ufcf":    terminal_ufcf,

            # ── WACC ──────────────────────────────────────────────────────
            "wacc":              wacc,
            "cost_of_equity":    round(ke, 1),
            "cost_of_debt_pre":  round(kd_pre, 1),
            "cost_of_debt_post": round(kd_post, 1),
            "terminal_growth":   terminal_growth,
            "tax_rate":          round(tax_rate_pct, 1),
            "beta":              round(beta, 2),
            "risk_free_rate":    round(rf_rate, 1),
            "erp":               erp,
            "size_premium":      0.0,
            "equity_weight":     round(e_wt, 1),
            "debt_weight":       round(d_wt, 1),

            # ── Capital structure ─────────────────────────────────────────
            "total_debt":  round(total_debt),
            "cash_equiv":  round(cash),
            "net_debt":    round(net_debt),

            # ── Operating assumptions ─────────────────────────────────────
            "revenue_growth_near":  revenue_growth_near,
            "revenue_growth_term":  terminal_growth,
            "ebit_margin_base":     round(ebit_margin_base_pct, 1),
            "ebit_margin_target":   round(ebit_margin_target, 1),
            "da_pct":               round(da_pct, 1),
            "capex_pct":            round(capex_pct, 1),
            "sbc_pct":              round(sbc_pct, 1),
            "dso":                  round(dso, 1),
            "dio":                  round(dio, 1),
            "dpo":                  round(dpo, 1),
            "buyback_yield":        buyback_yield,
            "dividend_yield":       dividend_yield,

            # ── Historical ────────────────────────────────────────────────
            "historical": {
                "years":        fy_years_asc,
                "revenue":      revenues,
                "gross_margin": gross_margins,
                "ebit_margin":  ebit_margins,
                "net_income":   net_incomes,
                "fcf":          fcfs,
                "capex":        capexes,
                "debt":         total_debts_h,
                "roic":         roics,
                "shares":       shares_h,
            },

            # ── DCF schedule ──────────────────────────────────────────────
            "forecast":    forecast,
            "sensitivity": sens,

            # ── Comps — live peer fetching ─────────────────────────────
            "peers":       [],
            "peer_median": {},

            # NOTE: peer data is populated lazily after this return block
            # to avoid blocking the main dashboard load. See below.

            # ── Flags ─────────────────────────────────────────────────────
            "flags": flags,

            # ── Assumptions table ─────────────────────────────────────────
            "assumptions": assumptions,

            # ── Insights ─────────────────────────────────────────────────
            "insights": [
                {
                    "icon": "📊", "category": "Revenue Growth", "status": "neutral",
                    "headline": f"Revenue growth {revenue_growth_near:.1f}% (3-yr CAGR)",
                    "body": f"Latest annual revenue: ${revenue_base:,.0f}M. Near-term growth {revenue_growth_near:.1f}%, tapering to {terminal_growth}% terminal rate over 7 years.",
                },
                {
                    "icon": "📈", "category": "Margin Trajectory", "status": "neutral",
                    "headline": f"EBIT margin {ebit_margin_base_pct:.1f}% → target {ebit_margin_target:.1f}%",
                    "body": f"Gross margin: {gross_margin_base_pct:.1f}%. Model forecasts {ebit_margin_target - ebit_margin_base_pct:.1f}pp EBIT margin improvement over 7 years based on historical peak.",
                },
                {
                    "icon": "🏛️", "category": "WACC", "status": "neutral",
                    "headline": f"WACC {wacc}% (β={beta:.2f}, Rf={rf_rate:.1f}%, ERP={erp}%)",
                    "body": f"Ke={ke:.1f}%, Kd(pre-tax)={kd_pre:.1f}%, equity weight={e_wt:.1f}%. Spread vs terminal growth: {wacc - terminal_growth:.1f}pp.",
                },
                {
                    "icon": "⚡", "category": "Terminal Value", "status": "warn" if tv_pct > 70 else "neutral",
                    "headline": f"{tv_pct}% of EV in terminal value",
                    "body": f"TV/EV = {tv_pct}%. {'Elevated — model is sensitive to terminal growth and WACC assumptions.' if tv_pct > 70 else 'Within a normal range for a DCF model.'}",
                },
            ],

            # ── Scenarios ────────────────────────────────────────────────
            "scenarios": {
                "base": {
                    "label": "Base Case", "wacc": wacc, "g": terminal_growth,
                    "margin_target": round(ebit_margin_target, 1), "rev_growth": revenue_growth_near,
                    "iv": round(iv, 2), "upside": round(upside, 1), "ev": round(ev), "recommendation": rec,
                },
                "bull": {
                    "label": "Bull Case", "wacc": bull_wacc, "g": bull_g,
                    "margin_target": round(ebit_margin_target + 2, 1), "rev_growth": round(revenue_growth_near + 2, 1),
                    "iv": bull_iv, "upside": bull_up, "ev": bull_ev, "recommendation": bull_rec,
                    "narrative": "Accelerated revenue growth, margin expansion ahead of plan, and WACC compression as interest rates ease.",
                },
                "bear": {
                    "label": "Bear Case", "wacc": bear_wacc, "g": bear_g,
                    "margin_target": round(ebit_margin_base_pct - 1, 1), "rev_growth": round(revenue_growth_near - 3, 1),
                    "iv": bear_iv, "upside": bear_up, "ev": bear_ev, "recommendation": bear_rec,
                    "narrative": "Margin compression, slowing topline growth, higher discount rate reflecting macro headwinds.",
                },
            },

            # ── Analyst view ─────────────────────────────────────────────
            "analyst_view": {
                "valuation_says": (
                    f"Live DCF from Yahoo Finance data. "
                    f"IV=${iv:.2f} vs current price=${price:.2f} "
                    f"({'+' if upside >= 0 else ''}{upside:.1f}% upside). "
                    f"Analyst consensus target: ${analyst_median:.2f}."
                ),
                "key_assumptions": (
                    f"WACC {wacc}%, terminal growth {terminal_growth}%, "
                    f"EBIT margin {ebit_margin_base_pct:.1f}%→{ebit_margin_target:.1f}%, "
                    f"revenue growth {revenue_growth_near:.1f}% near-term."
                ),
                "model_risks": (
                    f"Model uses {n} years of yfinance data. "
                    "Assumptions are auto-derived from historical averages — "
                    "verify against latest earnings guidance before use."
                ),
                "verify_before_use": [
                    "Review latest earnings report and forward revenue guidance",
                    "Check analyst consensus revenue and EPS estimates vs model",
                    f"Verify beta ({beta:.2f}) is appropriate for current market conditions",
                    "Confirm no major acquisitions or divestitures distort historical averages",
                    "Check latest debt maturity schedule and refinancing risk",
                ],
            },

            # ── Enriched fields ───────────────────────────────────────────
            "financial_scores":   financial_scores,
            "dupont":             dupont,
            "earnings_quality":   earnings_quality,
            "analyst_consensus":  analyst_consensus,

            "is_demo": False,
            "is_live": True,

            # ── Data quality / coverage ─────────────────────────────────
            "data_quality": {
                "annual_years":        n,
                "quarterly_periods":   _n_quarterly,
                "has_quarterly_recon": len(_extra_sorted) > 0,
                "reconstructed_years": len([y for y in _extra_sorted if y < min(fy_years_desc)]),
                "source":              "Yahoo Finance (yfinance)",
                "n_fin_cols":          n_fin,
                "n_bs_cols":           n_bs,
                "n_cf_cols":           n_cf,
            },
        }

        # ── Live peer / comps fetching ────────────────────────────────────
        # Done AFTER the main dict is built so the dashboard never blocks.
        if _PEERS_AVAILABLE:
            try:
                _peer_tickers = get_peers_for_ticker(
                    ticker,
                    info.get("sector", ""),
                    info.get("industry", ""),
                )
                _peers, _peer_median = fetch_peer_metrics(
                    _peer_tickers,
                    ticker,
                    target_sector=str(info.get("sector") or ""),
                    target_industry=str(info.get("industry") or ""),
                )
                _result["peers"]       = _peers
                _result["peer_median"] = _peer_median

                try:
                    from webapp.data.eodhd_client import (
                        _merge_peer_learning_relationships,
                        _record_peer_learning_signals,
                        _register_global_universe_symbols,
                        _safe_discovery_store,
                        _safe_symbol_universe_store,
                    )

                    universe_store = _safe_symbol_universe_store()
                    discovery_store = _safe_discovery_store()
                    peer_candidates = [
                        {
                            "ticker": peer.get("ticker") or peer.get("symbol"),
                            "company_name": peer.get("name") or peer.get("company_name") or "",
                            "exchange": str(peer.get("exchange") or ""),
                            "sector": str(peer.get("sector") or info.get("sector") or ""),
                            "industry": str(peer.get("industry") or info.get("industry") or ""),
                            "canonical_industry": str(peer.get("canonical_industry") or ""),
                            "industry_family": str(peer.get("industry_family") or ""),
                            "peer_learning_score": float(peer.get("base_peer_learning_score") or peer.get("peer_learning_score") or 0.0),
                            "base_peer_learning_score": float(peer.get("base_peer_learning_score") or peer.get("peer_learning_score") or 0.0),
                            "industry_similarity": float(peer.get("industry_similarity") or 0.0),
                            "pair_strength_score": float(peer.get("pair_strength_score") or 0.0),
                        }
                        for peer in _peers
                        if str(peer.get("ticker") or peer.get("symbol") or "").strip()
                    ]
                    _register_global_universe_symbols(
                        universe_store,
                        ticker=ticker,
                        company_name=company_name,
                        exchange=str(info.get("exchange") or info.get("fullExchangeName") or ""),
                        country=str(info.get("country") or ""),
                        sector=str(info.get("sector") or ""),
                        industry=str(info.get("industry") or ""),
                        knowledge_model=None,
                        peer_items=peer_candidates,
                    )
                    peer_relationships = _record_peer_learning_signals(
                        discovery_store,
                        ticker=ticker,
                        company_name=company_name,
                        exchange=str(info.get("exchange") or info.get("fullExchangeName") or ""),
                        country=str(info.get("country") or ""),
                        sector=str(info.get("sector") or ""),
                        industry=str(info.get("industry") or ""),
                        peer_items=peer_candidates,
                    )
                    _merge_peer_learning_relationships(_peers, peer_relationships)
                except Exception:
                    pass
            except Exception as _pe:
                logger.debug("Peer fetch failed: %s", _pe)

        return _result

    except Exception as exc:
        logger.warning("yfinance build_dashboard_data(%s) failed: %s", ticker, exc, exc_info=True)
        return None
