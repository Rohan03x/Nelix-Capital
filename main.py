"""
main.py — CLI entry point for the Automated Valuation System.

Usage examples:
    python main.py --ticker AAPL
    python main.py --ticker NKE  --scenario bull --override overrides/NKE.json
    python main.py --batch tickers.csv --output-dir ./output
    python main.py --ticker MSFT --email analyst@firm.com

Exit codes (Part 70.1):
    0  — success
    1  — unhandled error
    2  — data fetch failure
    3  — data quality failure
    4  — unsupported company type (Financials, Mining)
    5  — configuration error

Reference: Architecture Plan Parts 9.3, 47.3, 70.1.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ── Bootstrap: ensure project root is on sys.path ─────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from auto_valuation.config import load_config, ensure_directories, ValuationConfig
from auto_valuation.utils import get_logger, log_run_header, ValuationError, UnsupportedCompanyError

# Package version
__version__ = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
# ValuationResult  —  single return contract from run_valuation()
# Reference: Part 70.1
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValuationResult:
    ticker:               str
    run_date:             str
    scenario:             str

    # Core outputs
    enterprise_value_mm:  float | None = None   # USD millions
    equity_value_mm:      float | None = None
    price_per_share:      float | None = None
    current_price:        float | None = None
    implied_upside_pct:   float | None = None

    # Key assumptions used
    wacc:                 float | None = None
    terminal_growth:      float | None = None
    tv_pct_of_ev:         float | None = None

    # DCF sub-totals
    pv_ufcfs_mm:          float | None = None
    pv_terminal_value_mm: float | None = None

    # Comps implied ranges
    comps_ev_low_mm:      float | None = None
    comps_ev_high_mm:     float | None = None

    # Quality / validation
    validation_passed:    bool         = False
    warnings:             list[str]    = field(default_factory=list)

    # Delivery
    output_path:          str | None   = None
    exit_code:            int          = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def print_summary(self) -> None:
        from datetime import date as _date
        print(f"\n{'─'*60}")
        print(f"  Valuation Summary  |  {self.ticker}  |  {self.scenario.upper()} case")
        print(f"{'─'*60}")
        if self.price_per_share is not None:
            print(f"  Intrinsic Value (DCF):  ${self.price_per_share:>10.2f}")
        if self.current_price is not None:
            print(f"  Current Market Price:   ${self.current_price:>10.2f}")
        if self.implied_upside_pct is not None:
            sign = "+" if self.implied_upside_pct >= 0 else ""
            print(f"  Implied Upside/Down:    {sign}{self.implied_upside_pct:.1%}")
        if self.wacc is not None:
            print(f"  WACC:                   {self.wacc:.2%}")
        if self.terminal_growth is not None:
            print(f"  Terminal Growth:        {self.terminal_growth:.2%}")
        if self.tv_pct_of_ev is not None:
            print(f"  TV % of EV:             {self.tv_pct_of_ev:.1%}")
        if self.warnings:
            print(f"\n  Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                print(f"    ⚠  {w}")
        if self.output_path:
            print(f"\n  Output: {self.output_path}")
        print(f"{'─'*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Core runner  (wired up phase-by-phase as modules are built)
# ─────────────────────────────────────────────────────────────────────────────

def run_valuation(cfg: ValuationConfig) -> ValuationResult:  # noqa: C901
    """
    Orchestrate the full valuation pipeline:
      Phase 1 — Data fetch, clean, TTM, FX, validate
      Phase 2 — WACC, growth assumptions, DCF
      Phase 3 — Comps, transactions, sensitivity, output
    """
    import os
    from datetime import date

    log = get_logger(cfg.ticker, logs_dir=cfg.logs_dir)
    log_run_header(log, cfg.ticker, __version__, cfg.scenario)

    result = ValuationResult(
        ticker=cfg.ticker,
        run_date=date.today().isoformat(),
        scenario=cfg.scenario,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 1 — Data layer
    # ─────────────────────────────────────────────────────────────────────────
    log.info("Phase 1: Fetching financial data …")

    from auto_valuation.data.fetcher import (
        fetch_income_statement, fetch_balance_sheet, fetch_cash_flow,
        fetch_quarterly_income_statement, fetch_quarterly_balance_sheet,
        fetch_quarterly_cash_flow,
        fetch_profile, fetch_ntm_estimates, fetch_yfinance_info,
        fetch_52wk_range, check_price_freshness,
        fetch_risk_free_rate, fetch_damodaran_industry_beta, fetch_damodaran_erp,
    )
    from auto_valuation.data.cleaner import (
        unit_normalize, standardise_field_names, deduplicate_financial_data,
        detect_ma_years, normalize_one_time_items, capitalise_rd,
    )
    from auto_valuation.data.fiscal_year import compute_ttm
    from auto_valuation.data.bridge import compute_net_debt
    from auto_valuation.validation.checks import run_all_data_checks

    api_key = os.getenv("FMP_API_KEY", "")

    income_stmts = fetch_income_statement(cfg.ticker, api_key)
    balance_sheets = fetch_balance_sheet(cfg.ticker, api_key)
    cash_flows = fetch_cash_flow(cfg.ticker, api_key)
    profile = fetch_profile(cfg.ticker, api_key)

    q_income = fetch_quarterly_income_statement(cfg.ticker, api_key)
    q_balance = fetch_quarterly_balance_sheet(cfg.ticker, api_key)
    q_cashflow = fetch_quarterly_cash_flow(cfg.ticker, api_key)

    ntm_estimates = fetch_ntm_estimates(cfg.ticker, api_key)
    yf_info = fetch_yfinance_info(cfg.ticker)

    # Clean: standardise field names
    income_stmts  = [standardise_field_names(unit_normalize(r)) for r in income_stmts]
    balance_sheets = [standardise_field_names(unit_normalize(r)) for r in balance_sheets]
    cash_flows    = [standardise_field_names(unit_normalize(r)) for r in cash_flows]

    income_stmts  = deduplicate_financial_data(income_stmts)
    balance_sheets = deduplicate_financial_data(balance_sheets)
    cash_flows    = deduplicate_financial_data(cash_flows)

    if cfg.rd_capitalise:
        income_stmts = [capitalise_rd(r, cash_flows) for r in income_stmts]

    income_stmts  = [normalize_one_time_items(r) for r in income_stmts]

    # TTM
    q_is = [standardise_field_names(unit_normalize(r)) for r in q_income]
    q_bs = [standardise_field_names(unit_normalize(r)) for r in q_balance]
    q_cf = [standardise_field_names(unit_normalize(r)) for r in q_cashflow]
    ttm  = compute_ttm(q_is, q_cf, q_bs)

    # Data quality checks
    validation_warnings = run_all_data_checks(income_stmts, balance_sheets, cash_flows)
    result.validation_passed = True
    for vr in validation_warnings:
        if not vr.is_ok():
            result.warnings.append(f"[DATA] {vr.name}: {vr.message}")

    # Key balance sheet items
    latest_bs  = balance_sheets[0] if balance_sheets else {}
    net_debt   = compute_net_debt(latest_bs)

    # Price / market cap
    current_price  = yf_info.get("currentPrice") or yf_info.get("regularMarketPrice")
    shares_out_mm  = (yf_info.get("sharesOutstanding") or 0) / 1e6
    market_cap_mm  = (yf_info.get("marketCap") or 0) / 1e6
    if market_cap_mm <= 0 and current_price and shares_out_mm > 0:
        market_cap_mm = current_price * shares_out_mm

    # Company profile
    sector   = (profile.get("sector")   or "") if profile else ""
    industry = (profile.get("industry") or "") if profile else ""
    beta_raw = (profile.get("beta")     or 1.0) if profile else 1.0
    log.info(f"Sector: {sector}  |  Beta(raw): {beta_raw:.2f}  |  Market Cap: ${market_cap_mm:,.0f}mm")

    # Sector gating — raises UnsupportedCompanyError (exit 4) for unsupported types
    from auto_valuation.model.sector import apply_sector_gate, is_lease_heavy, RETAIL, AIRLINE
    if cfg.financial_company_gate:
        from auto_valuation.utils import UnsupportedCompanyError
        raise UnsupportedCompanyError(
            f"{cfg.ticker}: financial_company_gate override is set in config."
        )
    sector_type = apply_sector_gate(sector, industry, allow_reit=False)
    log.info(f"Sector type: {sector_type}")

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 — Assumptions + DCF
    # ─────────────────────────────────────────────────────────────────────────
    log.info("Phase 2: Building assumptions and running DCF …")

    from auto_valuation.assumptions.wacc import build_wacc
    from auto_valuation.assumptions.growth import build_growth_assumptions
    from auto_valuation.model.income_statement import normalise_tax_rate
    from auto_valuation.model.dilution import compute_fully_diluted_shares, compute_price_per_share
    from auto_valuation.forecast.dcf import run_dcf

    # WACC
    rf_rate      = fetch_risk_free_rate()
    ind_beta     = fetch_damodaran_industry_beta(industry or sector)
    damod_erp    = fetch_damodaran_erp()
    total_debt   = latest_bs.get("total_debt") or latest_bs.get("totalDebt") or 0
    wacc_dict    = build_wacc(
        ticker=cfg.ticker,
        market_cap_mm=market_cap_mm,
        total_debt_mm=total_debt,
        beta_raw=float(beta_raw),
        industry_beta=ind_beta,
        risk_free_rate=rf_rate,
        erp=damod_erp or cfg.erp,
        tax_rate=cfg.tax_rate_default,
        size_premium=cfg.size_premium,
        country_risk_premium=cfg.crp,
        blume_adjust=cfg.blume_adjustment,
    )
    wacc = wacc_dict["wacc"]
    if cfg.wacc_hard_min <= wacc <= cfg.wacc_hard_max:
        pass
    else:
        result.warnings.append(f"WACC {wacc:.2%} outside hard limits [{cfg.wacc_hard_min:.0%}, {cfg.wacc_hard_max:.0%}]")

    # Growth & margin assumptions
    growth_dict = build_growth_assumptions(
        income_stmts=income_stmts,
        ntm_estimates=ntm_estimates,
        sector=sector,
        terminal_growth=cfg.terminal_growth_default,
        forecast_years=cfg.forecast_years,
        fade_years=cfg.revenue_growth_fade_years,
        margin_fade_years=cfg.ebit_margin_fade_years,
    )
    near_term_growth     = growth_dict["near_term_growth"]
    terminal_growth      = growth_dict["terminal_growth"]
    target_ebit_margin   = growth_dict["target_ebit_margin"]

    # Tax rate
    latest_is = income_stmts[0] if income_stmts else {}
    base_revenue = ttm.get("revenue") or latest_is.get("revenue") or 0
    tax_rate = normalise_tax_rate(
        income_stmts,
        statutory_rate=cfg.tax_rate_default,
        years=cfg.tax_rate_lookback,
        max_rate=cfg.tax_rate_cap,
        min_rate=cfg.tax_rate_min,
    )

    # DCF
    dcf_result = run_dcf(
        ticker=cfg.ticker,
        scenario=cfg.scenario,
        income_stmts=income_stmts,
        cash_flows=cash_flows,
        balance_sheets=balance_sheets,
        wacc=wacc,
        terminal_growth=terminal_growth,
        near_term_growth=near_term_growth,
        target_ebit_margin=target_ebit_margin,
        forecast_years=cfg.forecast_years,
        tax_rate=tax_rate,
        mid_year_convention=cfg.mid_year_convention,
        exit_multiple=cfg.exit_multiple_default,
    )

    # Diluted shares
    options_mm     = (yf_info.get("impliedSharesOutstanding") or shares_out_mm) - shares_out_mm
    options_strike = current_price or 50.0
    dil_dict = compute_fully_diluted_shares(
        basic_shares_mm=shares_out_mm,
        options_outstanding_mm=max(0.0, options_mm),
        options_avg_strike=options_strike,
        current_price=current_price or 0.0,
    )
    shares_mm = dil_dict["fully_diluted_mm"]

    equity_value = dcf_result.enterprise_value - net_debt
    price_per_share = compute_price_per_share(equity_value, shares_mm)
    upside = (price_per_share - (current_price or price_per_share)) / (current_price or price_per_share) if current_price else 0.0

    result.enterprise_value_mm  = dcf_result.enterprise_value
    result.equity_value_mm      = equity_value
    result.price_per_share      = price_per_share
    result.current_price        = current_price
    result.implied_upside_pct   = upside
    result.wacc                 = wacc
    result.terminal_growth      = terminal_growth
    result.tv_pct_of_ev         = dcf_result.tv_pct_of_ev
    result.pv_ufcfs_mm          = dcf_result.pv_ufcfs
    result.pv_terminal_value_mm = dcf_result.pv_terminal_value
    result.warnings.extend(dcf_result.warnings or [])

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 3 — Comps, sensitivity, output
    # ─────────────────────────────────────────────────────────────────────────
    log.info("Phase 3: Comps, sensitivity, and output …")

    from auto_valuation.data.transactions import (
        load_precedent_transactions, compute_transaction_multiples,
        compute_transaction_comps_result,
    )
    from auto_valuation.data.comps import build_football_field
    from auto_valuation.sensitivity.analysis import (
        wacc_growth_sensitivity, run_scenario_analysis, scenario_summary_table,
    )
    from auto_valuation.output.excel import write_excel_output
    from auto_valuation.output.report import format_valuation_summary, print_valuation_summary, write_json_output

    # Precedent transactions
    transactions = load_precedent_transactions(cfg.ticker, str(OVERRIDES_DIR))
    txn_multiples = compute_transaction_multiples(transactions) if transactions else {}
    subject_ebitda  = ttm.get("ebitda") or (ttm.get("ebit", 0) or 0) * 1.2
    subject_revenue = ttm.get("revenue") or base_revenue or 0
    txn_comps = compute_transaction_comps_result(subject_ebitda, subject_revenue, txn_multiples) if txn_multiples else {}

    txn_ev_low  = txn_comps.get("blended_ev_range", {}).get("low")
    txn_ev_high = txn_comps.get("blended_ev_range", {}).get("high")
    if txn_ev_low:
        result.comps_ev_low_mm  = txn_ev_low
        result.comps_ev_high_mm = txn_ev_high

    # Scenario analysis
    base_dcf_kwargs = dict(
        ticker=cfg.ticker,
        scenario=cfg.scenario,
        income_stmts=income_stmts,
        cash_flows=cash_flows,
        balance_sheets=balance_sheets,
        wacc=wacc,
        terminal_growth=terminal_growth,
        near_term_growth=near_term_growth,
        target_ebit_margin=target_ebit_margin,
        forecast_years=cfg.forecast_years,
        tax_rate=tax_rate,
        mid_year_convention=cfg.mid_year_convention,
        exit_multiple=cfg.exit_multiple_default,
    )
    scenario_results = run_scenario_analysis(base_dcf_kwargs)
    scen_table = scenario_summary_table(scenario_results, net_debt=net_debt, shares_mm=shares_mm)

    # WACC × terminal growth sensitivity
    try:
        sens_wg = wacc_growth_sensitivity(
            base_dcf_kwargs=base_dcf_kwargs,
            net_debt=net_debt,
            shares_mm=shares_mm,
        )
    except Exception:
        sens_wg = None

    # Football field (DCF range from sensitivity)
    dcf_evs = list(sens_wg["ev_table"].values()) if sens_wg else [dcf_result.enterprise_value]
    ff_dcf_low  = min(dcf_evs) if dcf_evs else dcf_result.enterprise_value * 0.8
    ff_dcf_high = max(dcf_evs) if dcf_evs else dcf_result.enterprise_value * 1.2

    football_field = build_football_field(
        dcf_ev_low=ff_dcf_low,
        dcf_ev_high=ff_dcf_high,
        comps_ev_low=txn_ev_low or ff_dcf_low * 0.9,
        comps_ev_high=txn_ev_high or ff_dcf_high * 1.1,
        transactions_ev_low=txn_ev_low,
        transactions_ev_high=txn_ev_high,
        net_debt=net_debt,
        shares_mm=shares_mm,
        current_price=current_price,
    )

    # Assumptions dict for Excel sheet
    assumptions = {
        "ticker":               cfg.ticker,
        "scenario":             cfg.scenario,
        "wacc":                 wacc,
        "terminal_growth":      terminal_growth,
        "near_term_growth":     near_term_growth,
        "target_ebit_margin":   target_ebit_margin,
        "tax_rate":             tax_rate,
        "forecast_years":       cfg.forecast_years,
        "risk_free_rate":       rf_rate,
        "equity_risk_premium":  damod_erp or cfg.erp,
        "net_debt_mm":          net_debt,
        "shares_mm":            shares_mm,
        "current_price":        current_price,
        **wacc_dict,
    }

    # Write Excel
    from auto_valuation.config import OUTPUT_FILENAME_TEMPLATE, OUTPUT_VERSION
    from datetime import date as _date
    filename = OUTPUT_FILENAME_TEMPLATE.format(
        ticker=cfg.ticker.upper(),
        date=_date.today().strftime("%Y%m%d"),
        version=OUTPUT_VERSION,
    )
    output_path = str(Path(cfg.output_dir) / filename)
    try:
        write_excel_output(
            output_path=output_path,
            ticker=cfg.ticker,
            dcf_result=dcf_result,
            net_debt=net_debt,
            shares_mm=shares_mm,
            current_price=current_price,
            football_field=football_field,
            scenario_table=scen_table,
            sensitivity_wacc_g=sens_wg,
            transactions=transactions,
            transaction_multiples=txn_multiples if transactions else None,
            assumptions=assumptions,
        )
        result.output_path = output_path
        log.info(f"Excel output written → {output_path}")
    except Exception as exc:
        log.warning(f"Excel output failed (non-fatal): {exc}")
        result.warnings.append(f"Excel output failed: {exc}")

    # Write JSON
    summary_dict = format_valuation_summary(
        ticker=cfg.ticker,
        dcf_result=dcf_result,
        net_debt=net_debt,
        shares_mm=shares_mm,
        current_price=current_price,
        football_field=football_field,
        scenario_table=scen_table,
        assumptions=assumptions,
    )
    json_path = output_path.replace(".xlsx", ".json") if output_path else None
    if json_path:
        try:
            write_json_output(summary_dict, json_path)
            log.info(f"JSON output written → {json_path}")
        except Exception as exc:
            log.warning(f"JSON output failed (non-fatal): {exc}")

    # Console summary
    print_valuation_summary(summary_dict)

    result.exit_code = 0
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Batch mode (Part 47.3)
# ─────────────────────────────────────────────────────────────────────────────

def run_batch(
    tickers_file: str,
    base_args: argparse.Namespace,
) -> list[ValuationResult]:
    """Run the full pipeline for every ticker in a CSV/JSON list file."""
    path = Path(tickers_file)
    if not path.exists():
        raise FileNotFoundError(f"Batch file not found: {tickers_file}")

    tickers: list[str] = []
    if path.suffix.lower() == ".json":
        with open(path) as fh:
            data = json.load(fh)
        tickers = data if isinstance(data, list) else data.get("tickers", [])
    else:
        # Plain text or CSV: one ticker per line, skip comments
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip().split(",")[0].strip()
            if line and not line.startswith("#"):
                tickers.append(line.upper())

    results: list[ValuationResult] = []
    for ticker in tickers:
        try:
            overrides = _parse_override_file(base_args.override) if base_args.override else {}
            cfg = load_config(
                ticker=ticker,
                scenario=base_args.scenario,
                cli_overrides=overrides,
            )
            result = run_valuation(cfg)
            results.append(result)
        except ValuationError as exc:
            log = get_logger(ticker)
            log.error(f"Batch run failed for {ticker}: {exc}")
            results.append(ValuationResult(
                ticker=ticker, run_date="", scenario=base_args.scenario,
                exit_code=getattr(exc, "exit_code", 1),
                warnings=[str(exc)],
            ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_override_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Override file not found: {path}")
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    # Strip comment keys
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _load_api_keys() -> None:
    """Load .env file into environment. Raise ConfigError if FMP_API_KEY missing."""
    from dotenv import load_dotenv
    import os
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    if not os.getenv("FMP_API_KEY"):
        from auto_valuation.utils import ConfigError
        raise ConfigError(
            "FMP_API_KEY is not set. Copy .env.example → .env and add your key.\n"
            "Get a free key at: https://financialmodelingprep.com"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python main.py",
        description="Automated Valuation System — DCF + Comps + Football Field",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --ticker AAPL
  python main.py --ticker NKE --scenario bull --override overrides/NKE.json
  python main.py --batch tickers.csv
  python main.py --ticker MSFT --email analyst@firm.com --webhook $SLACK_URL
        """,
    )

    # ── Target ──────────────────────────────────────────────────────────────
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker",  metavar="TICKER",   help="Single ticker (e.g. AAPL)")
    group.add_argument("--batch",   metavar="FILE",     help="CSV/JSON file with list of tickers")

    # ── Model parameters ────────────────────────────────────────────────────
    p.add_argument("--exchange",  metavar="EXCH",    default="",     help="Exchange (e.g. NYSE). Optional.")
    p.add_argument("--currency",  metavar="CCY",     default="USD",  help="Output currency (default: USD)")
    p.add_argument("--scenario",  metavar="SCENARIO",default="base",
                   choices=["base", "bull", "bear"],  help="Scenario (default: base)")
    p.add_argument("--override",  metavar="FILE",    default=None,
                   help="Path to a JSON override file (overrides/EXAMPLE.json for schema)")
    p.add_argument("--forecast-years", metavar="N", type=int, default=None,
                   help="Forecast horizon in years (default: 5)")
    p.add_argument("--terminal-growth", metavar="G", type=float, default=None,
                   help="Terminal growth rate override (e.g. 0.025 for 2.5%%)")
    p.add_argument("--wacc",      metavar="W",    type=float, default=None,
                   help="WACC override — skips WACC computation")

    # ── Output ──────────────────────────────────────────────────────────────
    p.add_argument("--output-dir", metavar="DIR",  default="output",
                   help="Directory for Excel output files (default: output/)")
    p.add_argument("--no-pdf",     action="store_true",
                   help="Skip PDF export even if LibreOffice is available")

    # ── Delivery ────────────────────────────────────────────────────────────
    p.add_argument("--email",   metavar="EMAIL",  default=None,
                   help="Send completed workbook to this email address")
    p.add_argument("--webhook", metavar="URL",    default=None,
                   help="POST completion notification to this webhook URL")

    # ── Debug ───────────────────────────────────────────────────────────────
    p.add_argument("--verbose", "-v", action="store_true", help="Print DEBUG-level log to console")
    p.add_argument("--version",       action="version",    version=f"%(prog)s {__version__}")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # ── Setup ────────────────────────────────────────────────────────────────
    ensure_directories()

    try:
        _load_api_keys()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5   # ConfigError

    # ── Batch mode ───────────────────────────────────────────────────────────
    if args.batch:
        try:
            results = run_batch(args.batch, args)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 5
        success = sum(1 for r in results if r.exit_code == 0)
        print(f"\nBatch complete: {success}/{len(results)} succeeded.")
        return 0 if success == len(results) else 1

    # ── Single ticker ────────────────────────────────────────────────────────
    cli_overrides: dict[str, Any] = {}
    if args.forecast_years is not None:
        cli_overrides["forecast_years"] = args.forecast_years
    if args.terminal_growth is not None:
        cli_overrides["terminal_growth_default"] = args.terminal_growth
    if args.wacc is not None:
        cli_overrides["wacc_override"] = args.wacc
    cli_overrides["output_dir"] = args.output_dir

    # Merge --override file
    try:
        file_overrides = _parse_override_file(args.override)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5

    cli_overrides.update(file_overrides)

    cfg = load_config(
        ticker=args.ticker,
        exchange=args.exchange,
        currency=args.currency,
        scenario=args.scenario,
        cli_overrides=cli_overrides,
    )

    # Verbose: lower console log level
    if args.verbose:
        import logging
        for handler in logging.getLogger(f"avs.{cfg.ticker.upper()}").handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.DEBUG)

    try:
        result = run_valuation(cfg)
    except UnsupportedCompanyError as exc:
        print(f"\nUNSUPPORTED: {exc}", file=sys.stderr)
        return 4
    except ValuationError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)

    result.print_summary()
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())


# ─────────────────────────────────────────────────────────────────────────────
# Ticker input parsing  (Architecture Plan Part N13)
# ─────────────────────────────────────────────────────────────────────────────

def parse_ticker_input(ticker_str: str) -> list[str]:
    """
    Parse a ticker input string into a clean list of ticker symbols.

    Delegates to auto_valuation.data.fetcher.parse_ticker_input for
    consistency across entry points.

    Reference: Architecture Plan Part N13.
    """
    from auto_valuation.data.fetcher import parse_ticker_input as _parse
    return _parse(ticker_str)


def build_log_path(
    ticker: str,
    logs_dir: str | None = None,
    scenario: str = "base",
) -> str:
    """
    Build a standardised log file path for *ticker*.
    Delegates to auto_valuation.output.excel_writer.build_log_path.
    Reference: Architecture Plan Part 33.3.
    """
    from auto_valuation.output.excel_writer import build_log_path as _blp
    return _blp(ticker=ticker, logs_dir=logs_dir, scenario=scenario)
