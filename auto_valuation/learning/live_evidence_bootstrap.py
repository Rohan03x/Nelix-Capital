from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable

from auto_valuation.config import ERP_DEFAULT, LEARNING_CONFIG, TERMINAL_GROWTH_DEFAULT
from auto_valuation.forecast.dcf import run_dcf
from auto_valuation.learning.ledger import (
    LedgerReader,
    LedgerWriter,
    PredictionRecord,
    RealizedOutcomeRecord,
    prediction_horizon_target_date,
)
from auto_valuation.learning.maintenance import (
    MAINTENANCE_STATE_PATH,
    _annual_periods_by_year,
    _candidate_base_years,
    _load_state,
    _optional_float,
    _parse_date_value,
    _save_state,
    align_prediction_record_to_actuals,
    extract_actuals_from_fundamentals,
)
from auto_valuation.learning.postmortem import QuinquennialStore, run_5year_postmortem, run_annual_postmortem
from webapp.data.knowledge_model import refine_live_assumptions


WEBAPP_CACHE_DIR = Path(__file__).resolve().parents[2] / "webapp" / "data" / "cache"
DEFAULT_BOOTSTRAP_TICKERS = (
    # ── US mega-cap tech ──────────────────────────────────────────────────
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "ORCL",
    "CSCO",
    "IBM",
    # ── US financials & energy ────────────────────────────────────────────
    "JPM",
    "BRK-B",
    "XOM",
    "CVX",
    "BAC",
    "WFC",
    "GS",
    # ── US healthcare & consumer ──────────────────────────────────────────
    "LLY",
    "JNJ",
    "UNH",
    "PG",
    "KO",
    "WMT",
    "COST",
    "MCD",
    "NKE",
    # ── US industrials & materials ────────────────────────────────────────
    "CAT",
    "DE",
    "HON",
    "GE",
    "MMM",
    "LIN",
    # ── European blue-chips ───────────────────────────────────────────────
    "ASML",
    "SAP",
    "NESN.SW",
    "NOVN.SW",
    "OR.PA",           # L'Oréal
    "MC.PA",           # LVMH
    "SIE.XETRA",       # Siemens
    "AIR.PA",          # Airbus
    "SAN.MC",          # Santander
    "ULVR.LSE",        # Unilever
    "BP.LSE",
    "RIO.LSE",
    # ── Asia-Pacific ──────────────────────────────────────────────────────
    "TM",              # Toyota
    "7203.T",          # Toyota (local)
    "005930.KO",       # Samsung
    "BHP.AU",          # BHP Group
    "9988.HK",         # Alibaba
    "700.HK",          # Tencent
)
REALIZED_VALUE_TARGETS = {
    "actual_revenue_mm",
    "actual_ebit_margin",
    "actual_ufcf_mm",
    "actual_ev_mm",
    "actual_price_at_horizon",
}


def _safe_symbol_universe_store() -> Any | None:
    if not LEARNING_CONFIG.get("symbol_universe_enabled", True):
        return None
    try:
        from .universe import SymbolUniverseStore

        return SymbolUniverseStore()
    except Exception:
        return None


def _load_universe_priority_tickers(limit: int) -> list[str]:
    if limit <= 0:
        return []
    store = _safe_symbol_universe_store()
    if store is None:
        return []
    try:
        return store.priority_tickers(
            limit=limit,
            stale_after_hours=int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18)),
        )
    except Exception:
        return []


def _track_bootstrap_symbol(
    universe_store: Any | None,
    ticker: str,
    *,
    fundamentals: dict[str, Any] | None = None,
    source: str,
    bootstrapped: bool = False,
    bootstrap_status: str = "",
    fundamentals_cached: bool = False,
) -> None:
    if universe_store is None:
        return
    general = dict((fundamentals or {}).get("General") or {})
    try:
        universe_store.upsert_symbol(
            ticker,
            company_name=str(general.get("Name") or ""),
            exchange=str(general.get("Exchange") or ""),
            country=str(general.get("CountryName") or general.get("CountryISO") or ""),
            sector=str(general.get("Sector") or ""),
            industry=str(general.get("Industry") or ""),
            source=source,
            bootstrapped=bootstrapped,
            bootstrap_status=bootstrap_status,
            fundamentals_cached=fundamentals_cached,
        )
    except Exception:
        return


@dataclass(frozen=True)
class LiveEvidenceBootstrapResult:
    enabled: bool
    ran: bool
    reason: str | None = None
    as_of_date: str | None = None
    maintenance_run_id: str | None = None
    replay_tickers: list[str] = field(default_factory=list)
    replay_predictions_created: int = 0
    duplicate_predictions: int = 0
    matured_predictions_scanned: int = 0
    realized_outcomes_created: int = 0
    partial_realized_outcomes_created: int = 0
    full_realized_outcomes_created: int = 0
    duplicate_realized_outcomes: int = 0
    annual_postmortems_created: int = 0
    quinquennial_reports_created: int = 0
    skipped_records: int = 0
    missing_labels: dict[str, int] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnnualFundamentalSnapshot:
    ticker: str
    year: int
    period_end: date
    available_date: date
    revenue_mm: float
    ebit_mm: float | None
    ebit_margin: float
    gross_margin_pct: float | None
    operating_cf_mm: float | None
    fcf_mm: float | None
    capex_mm: float | None
    da_mm: float | None
    sbc_mm: float | None
    total_assets_mm: float | None
    total_debt_mm: float | None
    net_debt_mm: float | None
    shares_outstanding_mm: float | None
    pretax_income_mm: float | None
    tax_provision_mm: float | None
    net_receivables_mm: float | None
    inventory_mm: float | None
    accounts_payable_mm: float | None
    dso: float
    dio: float
    dpo: float


def run_live_evidence_bootstrap(
    *,
    tickers: list[str] | None = None,
    fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    price_history_provider: Callable[[str, date, date], list[dict[str, Any]]] | None = None,
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    report_store: QuinquennialStore | None = None,
    as_of_date: date | None = None,
    state_path: str | Path | None = None,
    interval_hours: int | None = None,
    max_tickers: int | None = None,
    max_replay_predictions_per_ticker: int | None = 6,
    replay_enabled: bool = True,
) -> LiveEvidenceBootstrapResult:
    enabled = True
    today = as_of_date or date.today()
    reader = ledger_reader or LedgerReader()
    writer = ledger_writer or LedgerWriter(reader.db_path)
    store = report_store or QuinquennialStore()
    universe_store = _safe_symbol_universe_store()
    state_file = Path(state_path) if state_path else MAINTENANCE_STATE_PATH
    started_at = datetime.now(timezone.utc)
    bootstrap_interval_hours = max(int(interval_hours or 0), 0)
    state = _load_state(state_file)

    last_bootstrap_at: datetime | None = None
    last_bootstrap_text = state.get("bootstrap_last_run_at")
    if last_bootstrap_text:
        try:
            last_bootstrap_at = datetime.fromisoformat(str(last_bootstrap_text))
            if last_bootstrap_at.tzinfo is None:
                last_bootstrap_at = last_bootstrap_at.replace(tzinfo=timezone.utc)
        except ValueError:
            last_bootstrap_at = None

    if bootstrap_interval_hours > 0 and last_bootstrap_at and started_at - last_bootstrap_at < timedelta(hours=bootstrap_interval_hours):
        return LiveEvidenceBootstrapResult(
            enabled=enabled,
            ran=False,
            reason="throttled",
            as_of_date=today.isoformat(),
            maintenance_run_id=state.get("bootstrap_last_run_id") or state.get("last_run_id"),
        )

    fundamentals_loader = fundamentals_provider or _default_fundamentals_provider
    price_loader = price_history_provider or _default_price_history_provider
    selected_tickers = _resolve_bootstrap_tickers(tickers, reader=reader, max_tickers=max_tickers)
    if not selected_tickers:
        return LiveEvidenceBootstrapResult(enabled=enabled, ran=False, reason="no_tickers", as_of_date=today.isoformat())

    counts = Counter()
    missing_labels = Counter()
    issues: list[dict[str, Any]] = []
    replayed_tickers: list[str] = []

    for ticker in selected_tickers:
        fundamentals = fundamentals_loader(ticker)
        if not isinstance(fundamentals, dict) or not fundamentals:
            _track_bootstrap_symbol(
                universe_store,
                ticker,
                source="live-evidence-bootstrap",
                bootstrapped=False,
                bootstrap_status="missing_fundamentals",
            )
            missing_labels["missing_fundamentals"] += 1
            issues.append({"ticker": ticker, "reason": "missing_fundamentals"})
            continue

        _track_bootstrap_symbol(
            universe_store,
            ticker,
            fundamentals=fundamentals,
            source="bootstrap-fundamentals",
            fundamentals_cached=True,
        )

        if replay_enabled:
            replay_created, replay_duplicates = _replay_predictions_for_ticker(
                ticker,
                fundamentals,
                ledger_writer=writer,
                price_history_provider=price_loader,
                as_of_date=today,
                max_predictions=max_replay_predictions_per_ticker,
                missing_labels=missing_labels,
                issues=issues,
            )
            if replay_created or replay_duplicates:
                replayed_tickers.append(ticker)
            counts["replay_predictions_created"] += replay_created
            counts["duplicate_predictions"] += replay_duplicates

        matured_records = [
            record
            for record in reader.query(ticker=ticker, limit=5000)
            if prediction_horizon_target_date(record) <= today and record.scenario == "base"
        ]
        counts["matured_predictions_scanned"] += len(matured_records)
        if not matured_records:
            bootstrap_status = "replayed" if replay_enabled else "seeded"
            _track_bootstrap_symbol(
                universe_store,
                ticker,
                fundamentals=fundamentals,
                source="live-evidence-bootstrap",
                bootstrapped=True,
                bootstrap_status=bootstrap_status,
                fundamentals_cached=True,
            )
            continue

        earliest_needed = min(
            min(record.run_date, prediction_horizon_target_date(record)) for record in matured_records
        ) - timedelta(days=10)
        price_history = price_loader(ticker, earliest_needed, today)

        labeled_horizon_years: set[int] = set()
        for record in matured_records:
            outcome_result = _materialize_realized_outcomes_for_record(
                record,
                fundamentals,
                price_history,
                ledger_reader=reader,
                ledger_writer=writer,
                as_of_date=today,
                missing_labels=missing_labels,
                issues=issues,
            )
            counts["realized_outcomes_created"] += outcome_result["created"]
            counts["partial_realized_outcomes_created"] += outcome_result["partial_created"]
            counts["full_realized_outcomes_created"] += outcome_result["full_created"]
            counts["duplicate_realized_outcomes"] += outcome_result["duplicates"]
            if outcome_result["skipped"]:
                counts["skipped_records"] += 1
            if outcome_result["has_realized"]:
                labeled_horizon_years.add(record.forecast_horizon_year)

        for horizon_year in sorted(labeled_horizon_years):
            created_postmortems = run_annual_postmortem(
                ticker,
                horizon_year,
                ledger_reader=reader,
                ledger_writer=writer,
                persist=True,
                skip_existing=True,
            )
            counts["annual_postmortems_created"] += len(created_postmortems)

        counts["quinquennial_reports_created"] += _materialize_quinquennial_reports(
            ticker,
            ledger_reader=reader,
            ledger_writer=writer,
            report_store=store,
        )

        bootstrap_status = "realized" if labeled_horizon_years else "replayed"
        _track_bootstrap_symbol(
            universe_store,
            ticker,
            fundamentals=fundamentals,
            source="live-evidence-bootstrap",
            bootstrapped=True,
            bootstrap_status=bootstrap_status,
            fundamentals_cached=True,
        )

    completed_at = datetime.now(timezone.utc)
    maintenance_payload = {
        "type": "live_evidence_bootstrap",
        "tickers": selected_tickers,
        "as_of_date": today.isoformat(),
        "replay_enabled": replay_enabled,
        "replay_predictions_created": int(counts["replay_predictions_created"]),
        "duplicate_predictions": int(counts["duplicate_predictions"]),
        "matured_predictions_scanned": int(counts["matured_predictions_scanned"]),
        "realized_outcomes_created": int(counts["realized_outcomes_created"]),
        "annual_postmortems_created": int(counts["annual_postmortems_created"]),
        "quinquennial_reports_created": int(counts["quinquennial_reports_created"]),
        "missing_labels": dict(missing_labels),
    }
    maintenance_run_id = writer.append_maintenance_run(
        maintenance_payload,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )
    prior_state = _load_state(state_file)
    _save_state(
        state_file,
        {
            **prior_state,
            "last_run_at": completed_at.isoformat(),
            "last_run_id": maintenance_run_id,
            "bootstrap_last_run_at": completed_at.isoformat(),
            "bootstrap_last_run_id": maintenance_run_id,
            "bootstrap_mode": "live_evidence",
        },
    )

    return LiveEvidenceBootstrapResult(
        enabled=enabled,
        ran=True,
        as_of_date=today.isoformat(),
        maintenance_run_id=maintenance_run_id,
        replay_tickers=replayed_tickers,
        replay_predictions_created=int(counts["replay_predictions_created"]),
        duplicate_predictions=int(counts["duplicate_predictions"]),
        matured_predictions_scanned=int(counts["matured_predictions_scanned"]),
        realized_outcomes_created=int(counts["realized_outcomes_created"]),
        partial_realized_outcomes_created=int(counts["partial_realized_outcomes_created"]),
        full_realized_outcomes_created=int(counts["full_realized_outcomes_created"]),
        duplicate_realized_outcomes=int(counts["duplicate_realized_outcomes"]),
        annual_postmortems_created=int(counts["annual_postmortems_created"]),
        quinquennial_reports_created=int(counts["quinquennial_reports_created"]),
        skipped_records=int(counts["skipped_records"]),
        missing_labels=dict(missing_labels),
        issues=issues,
    )


def _resolve_bootstrap_tickers(
    requested: list[str] | None,
    *,
    reader: LedgerReader,
    max_tickers: int | None,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    requested_explicitly = bool(requested)
    effective_max_tickers = max_tickers
    if effective_max_tickers is None and not requested_explicitly:
        effective_max_tickers = int(LEARNING_CONFIG.get("bootstrap_default_max_tickers_per_run", 18))

    def add(symbol: str | None) -> None:
        ticker = str(symbol or "").strip().upper()
        if not ticker or ticker in seen:
            return
        seen.add(ticker)
        ordered.append(ticker)

    for ticker in requested or []:
        add(ticker)
    if not requested_explicitly:
        priority_tickers = _load_universe_priority_tickers(int(LEARNING_CONFIG.get("symbol_universe_priority_limit", 72)))
        priority_prefix = 3 if effective_max_tickers is None else max(1, min(3, effective_max_tickers))
        for ticker in priority_tickers[:priority_prefix]:
            add(ticker)

        source_queues = [
            deque(priority_tickers[priority_prefix:]),
            deque(record.ticker for record in reader.query(limit=5000)),
            deque(_load_cached_bootstrap_tickers(int(LEARNING_CONFIG.get("bootstrap_cached_ticker_limit", 48)))),
            deque(DEFAULT_BOOTSTRAP_TICKERS),
            deque(_load_supported_bootstrap_tickers()),
        ]
        while any(source_queues):
            made_progress = False
            for queue in source_queues:
                if not queue:
                    continue
                add(queue.popleft())
                made_progress = True
                if effective_max_tickers is not None and len(ordered) >= effective_max_tickers:
                    return ordered[:effective_max_tickers]
            if not made_progress:
                break

    if effective_max_tickers is not None and effective_max_tickers > 0:
        return ordered[:effective_max_tickers]
    return ordered


def _load_supported_bootstrap_tickers() -> list[str]:
    pool_limit = int(
        LEARNING_CONFIG.get(
            "background_runner_seed_pool_limit",
            LEARNING_CONFIG.get("background_runner_seed_target_symbols", 1000),
        )
        or 0
    )
    try:
        from webapp.data.ticker_search import seedable_tickers
    except Exception:
        try:
            from webapp.data.samples import SUPPORTED_TICKERS
        except Exception:
            return []
        return [str(ticker or "").strip().upper() for ticker in SUPPORTED_TICKERS]
    tickers = seedable_tickers(limit=pool_limit if pool_limit > 0 else None, common_stock_only=True)
    return [str(ticker or "").strip().upper() for ticker in tickers]


def _load_cached_bootstrap_tickers(limit: int) -> list[str]:
    if limit <= 0 or not WEBAPP_CACHE_DIR.exists():
        return []

    tickers: list[str] = []
    for payload_path in sorted(
        WEBAPP_CACHE_DIR.glob("eodhd_fund_*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    ):
        ticker = _cached_primary_ticker(payload_path)
        if not ticker:
            continue
        tickers.append(ticker)
        if len(tickers) >= limit:
            break
    return tickers


def _cached_primary_ticker(payload_path: Path) -> str:
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    general = data.get("General")
    if not isinstance(general, dict):
        return ""

    primary = str(general.get("PrimaryTicker") or "").strip().upper()
    if primary:
        return primary

    code = str(general.get("Code") or "").strip().upper()
    exchange = str(general.get("Exchange") or "").strip().upper()
    if not code:
        return ""
    if exchange and "." not in code:
        return f"{code}.{exchange}"
    return code


def _default_fundamentals_provider(ticker: str) -> dict[str, Any] | None:
    from webapp.data.eodhd_client import _eodhd_code, _fetch_fundamentals

    return _fetch_fundamentals(_eodhd_code(ticker))


def _default_price_history_provider(ticker: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    from webapp.data.eodhd_client import _eodhd_code, fetch_historical_price_series

    return fetch_historical_price_series(
        _eodhd_code(ticker),
        start_date=start_date,
        end_date=end_date,
    )


def _annual_snapshots_from_fundamentals(
    ticker: str,
    fundamentals: dict[str, Any],
    *,
    as_of_date: date,
) -> list[AnnualFundamentalSnapshot]:
    financials = dict(fundamentals.get("Financials") or {})
    income_by_year = _annual_periods_by_year(
        dict((financials.get("Income_Statement") or {}).get("yearly") or {}),
        as_of_date=as_of_date,
    )
    cash_flow_by_year = _annual_periods_by_year(
        dict((financials.get("Cash_Flow") or {}).get("yearly") or {}),
        as_of_date=as_of_date,
    )
    balance_by_year = _annual_periods_by_year(
        dict((financials.get("Balance_Sheet") or {}).get("yearly") or {}),
        as_of_date=as_of_date,
    )

    snapshots: list[AnnualFundamentalSnapshot] = []
    for year in sorted(set(income_by_year) & set(balance_by_year)):
        income = dict(income_by_year.get(year) or {})
        cash_flow = dict(cash_flow_by_year.get(year) or {})
        balance = dict(balance_by_year.get(year) or {})

        period_end = (
            _parse_date_value(income.get("_period_end"))
            or _parse_date_value(balance.get("_period_end"))
            or _parse_date_value(cash_flow.get("_period_end"))
        )
        if period_end is None:
            continue

        revenue_raw = _optional_float(income.get("totalRevenue"))
        if revenue_raw is None or revenue_raw <= 0:
            continue

        ebit_raw = _optional_float(income.get("ebit"))
        if ebit_raw is None:
            ebit_raw = _optional_float(income.get("operatingIncome"))
        gross_profit_raw = _optional_float(income.get("grossProfit"))
        operating_cf_raw = _optional_float(cash_flow.get("totalCashFromOperatingActivities"))
        capex_raw = _optional_float(cash_flow.get("capitalExpenditures"))
        da_raw = _optional_float(cash_flow.get("depreciationAndAmortization"))
        if da_raw is None:
            da_raw = _optional_float(cash_flow.get("depreciation"))
        sbc_raw = _optional_float(cash_flow.get("stockBasedCompensation"))
        free_cash_flow_raw = _optional_float(cash_flow.get("freeCashFlow"))
        if free_cash_flow_raw is None and operating_cf_raw is not None and capex_raw is not None:
            free_cash_flow_raw = operating_cf_raw - abs(capex_raw)

        total_assets_raw = _optional_float(balance.get("totalAssets"))
        total_debt_raw = _resolve_total_debt(balance)
        net_debt_raw = _resolve_net_debt(balance)
        shares_outstanding_raw = _resolve_shares_outstanding(balance)
        pretax_income_raw = _optional_float(income.get("incomeBeforeTax"))
        if pretax_income_raw is None:
            pretax_income_raw = _optional_float(income.get("pretaxIncome"))
        tax_provision_raw = _optional_float(income.get("incomeTaxExpense"))
        if tax_provision_raw is None:
            tax_provision_raw = _optional_float(income.get("taxProvision"))
        net_receivables_raw = _optional_float(balance.get("netReceivables"))
        if net_receivables_raw is None:
            net_receivables_raw = _optional_float(balance.get("accountsReceivable"))
        inventory_raw = _optional_float(balance.get("inventory"))
        accounts_payable_raw = _optional_float(balance.get("accountsPayable"))
        if accounts_payable_raw is None:
            accounts_payable_raw = _optional_float(balance.get("accountPayables"))

        available_dates = [
            _parse_date_value(income.get("_available_date")),
            _parse_date_value(balance.get("_available_date")),
            _parse_date_value(cash_flow.get("_available_date")),
        ]
        available_date = max(item for item in available_dates if item is not None)

        gross_margin_pct = _pct(gross_profit_raw, revenue_raw)
        cost_of_sales_raw = revenue_raw - gross_profit_raw if gross_profit_raw is not None else None
        snapshots.append(
            AnnualFundamentalSnapshot(
                ticker=ticker,
                year=year,
                period_end=period_end,
                available_date=available_date,
                revenue_mm=round(revenue_raw / 1e6, 2),
                ebit_mm=round(ebit_raw / 1e6, 2) if ebit_raw is not None else None,
                ebit_margin=(ebit_raw / revenue_raw) if ebit_raw is not None else 0.0,
                gross_margin_pct=gross_margin_pct,
                operating_cf_mm=round(operating_cf_raw / 1e6, 2) if operating_cf_raw is not None else None,
                fcf_mm=round(free_cash_flow_raw / 1e6, 2) if free_cash_flow_raw is not None else None,
                capex_mm=round(abs(capex_raw) / 1e6, 2) if capex_raw is not None else None,
                da_mm=round(da_raw / 1e6, 2) if da_raw is not None else None,
                sbc_mm=round(sbc_raw / 1e6, 2) if sbc_raw is not None else None,
                total_assets_mm=round(total_assets_raw / 1e6, 2) if total_assets_raw is not None else None,
                total_debt_mm=round(total_debt_raw / 1e6, 2) if total_debt_raw is not None else None,
                net_debt_mm=round(net_debt_raw / 1e6, 2) if net_debt_raw is not None else None,
                shares_outstanding_mm=round(shares_outstanding_raw / 1e6, 4) if shares_outstanding_raw is not None else None,
                pretax_income_mm=round(pretax_income_raw / 1e6, 2) if pretax_income_raw is not None else None,
                tax_provision_mm=round(tax_provision_raw / 1e6, 2) if tax_provision_raw is not None else None,
                net_receivables_mm=round(net_receivables_raw / 1e6, 2) if net_receivables_raw is not None else None,
                inventory_mm=round(inventory_raw / 1e6, 2) if inventory_raw is not None else None,
                accounts_payable_mm=round(accounts_payable_raw / 1e6, 2) if accounts_payable_raw is not None else None,
                dso=_ratio_days(net_receivables_raw, revenue_raw),
                dio=_ratio_days(inventory_raw, cost_of_sales_raw),
                dpo=_ratio_days(accounts_payable_raw, cost_of_sales_raw),
            )
        )
    return snapshots


def _replay_predictions_for_ticker(
    ticker: str,
    fundamentals: dict[str, Any],
    *,
    ledger_writer: LedgerWriter,
    price_history_provider: Callable[[str, date, date], list[dict[str, Any]]],
    as_of_date: date,
    max_predictions: int | None,
    missing_labels: Counter[str],
    issues: list[dict[str, Any]],
) -> tuple[int, int]:
    snapshots = _annual_snapshots_from_fundamentals(ticker, fundamentals, as_of_date=as_of_date)
    if len(snapshots) < 4:
        missing_labels["insufficient_replay_history"] += 1
        issues.append({"ticker": ticker, "reason": "insufficient_replay_history"})
        return 0, 0

    snapshot_by_year = {snapshot.year: snapshot for snapshot in snapshots}
    base_years = [year for year in sorted(snapshot_by_year) if (year + 1) in snapshot_by_year]
    if max_predictions is not None and max_predictions > 0:
        base_years = base_years[-max_predictions:]
    if not base_years:
        missing_labels["no_replay_candidates"] += 1
        issues.append({"ticker": ticker, "reason": "no_replay_candidates"})
        return 0, 0

    earliest_price_date = min(snapshot_by_year[year].available_date for year in base_years) - timedelta(days=10)
    price_history = price_history_provider(ticker, earliest_price_date, as_of_date)
    if not price_history:
        missing_labels["missing_price_history"] += 1
        issues.append({"ticker": ticker, "reason": "missing_price_history"})
        return 0, 0

    replay_created = 0
    duplicate_predictions = 0
    for base_year in base_years:
        base_snapshot = snapshot_by_year[base_year]
        history = [snapshot for snapshot in snapshots if snapshot.year <= base_year]
        if len(history) < 3:
            missing_labels["insufficient_replay_history"] += 1
            issues.append({"ticker": ticker, "reason": "insufficient_replay_history", "base_year": base_year})
            continue

        price_row = _price_row_on_or_before(price_history, base_snapshot.available_date)
        if price_row is None:
            missing_labels["missing_prediction_price"] += 1
            issues.append({"ticker": ticker, "reason": "missing_prediction_price", "base_year": base_year})
            continue

        try:
            record = _build_replay_prediction_record(
                ticker,
                fundamentals,
                history,
                base_snapshot=base_snapshot,
                price_row=price_row,
            )
            ledger_writer.append(record)
            replay_created += 1
        except ValueError as exc:
            duplicate_predictions += 1
            issues.append({"ticker": ticker, "reason": "duplicate_prediction", "base_year": base_year, "detail": str(exc)})
        except Exception as exc:
            missing_labels["replay_build_failed"] += 1
            issues.append({"ticker": ticker, "reason": "replay_build_failed", "base_year": base_year, "detail": str(exc)})
    return replay_created, duplicate_predictions


def _build_replay_prediction_record(
    ticker: str,
    fundamentals: dict[str, Any],
    history: list[AnnualFundamentalSnapshot],
    *,
    base_snapshot: AnnualFundamentalSnapshot,
    price_row: dict[str, Any],
) -> PredictionRecord:
    company_info = dict(fundamentals.get("General") or {})
    technicals = dict(fundamentals.get("Technicals") or {})
    ticker_upper = ticker.upper()
    company_name = str(company_info.get("Name") or ticker_upper)
    sector = str(company_info.get("Sector") or "")
    industry = str(company_info.get("Industry") or "")
    prediction_price = _optional_float(price_row.get("close"))
    if prediction_price is None or prediction_price <= 0:
        raise RuntimeError(f"Missing prediction price for {ticker_upper} {base_snapshot.available_date.isoformat()}")
    if base_snapshot.shares_outstanding_mm is None or base_snapshot.shares_outstanding_mm <= 0:
        raise RuntimeError(f"Missing shares outstanding for {ticker_upper} {base_snapshot.year}")

    revenues = [snapshot.revenue_mm for snapshot in history]
    ebit_margins = [round(snapshot.ebit_margin * 100.0, 2) for snapshot in history]
    gross_margin_values = [snapshot.gross_margin_pct for snapshot in history if snapshot.gross_margin_pct is not None]
    gross_margin_base_pct = gross_margin_values[-1] if gross_margin_values else round(max(ebit_margins[-1] + 15.0, 20.0), 2)
    revenue_growth_near = _recent_growth_pct(revenues)
    ebit_margin_base_pct = ebit_margins[-1]
    ebit_margin_target = round(max(ebit_margin_base_pct, _central_tendency(ebit_margins[-4:], default=ebit_margin_base_pct)), 2)

    beta = _optional_float(technicals.get("Beta")) or 1.0
    rf_rate = _current_risk_free_rate()
    erp = _config_pct(ERP_DEFAULT)
    kd_post = round(max(2.5, rf_rate + 1.5), 2)
    terminal_growth = _config_pct(TERMINAL_GROWTH_DEFAULT)

    market_cap = round(prediction_price * base_snapshot.shares_outstanding_mm, 2)
    total_debt = round(max(base_snapshot.total_debt_mm or 0.0, 0.0), 2)
    total_assets = round(max(base_snapshot.total_assets_mm or base_snapshot.revenue_mm, base_snapshot.revenue_mm), 2)
    revenue_base = base_snapshot.revenue_mm
    operating_cf = round(base_snapshot.operating_cf_mm or base_snapshot.fcf_mm or (revenue_base * 0.15), 2)
    fcf = round(base_snapshot.fcf_mm or max(operating_cf - (base_snapshot.capex_mm or 0.0), 0.0), 2)
    capex_pct = _last_ratio_pct([snapshot.capex_mm for snapshot in history], revenues, default=4.0)
    da_pct = _last_ratio_pct([snapshot.da_mm for snapshot in history], revenues, default=2.0)
    sbc_pct = _last_ratio_pct([snapshot.sbc_mm for snapshot in history], revenues, default=0.0)
    capexes = [abs(snapshot.capex_mm or 0.0) for snapshot in history]
    das = [snapshot.da_mm or 0.0 for snapshot in history]
    sbcs = [snapshot.sbc_mm or 0.0 for snapshot in history]
    tax_rate_pct = _tax_rate_pct(history, default=21.0)
    pretax_incomes = [snapshot.pretax_income_mm or 0.0 for snapshot in history]
    tax_provisions = [snapshot.tax_provision_mm or 0.0 for snapshot in history]
    enterprise_base = max(market_cap + max(base_snapshot.net_debt_mm or 0.0, 0.0), 1.0)
    e_wt = round((market_cap / enterprise_base) * 100.0, 2)
    d_wt = round(max(0.0, 100.0 - e_wt), 2)
    cost_of_equity = rf_rate + beta * erp
    wacc = round(((e_wt / 100.0) * cost_of_equity) + ((d_wt / 100.0) * kd_post * (1.0 - tax_rate_pct / 100.0)), 2)

    knowledge_model = refine_live_assumptions(
        ticker=ticker_upper,
        sector=sector,
        industry=industry,
        market_cap=market_cap,
        revenues=revenues,
        ebit_margins=ebit_margins,
        gross_margin_base_pct=gross_margin_base_pct,
        revenue_growth_near=revenue_growth_near,
        terminal_growth=terminal_growth,
        ebit_margin_base_pct=ebit_margin_base_pct,
        ebit_margin_target=ebit_margin_target,
        beta=beta,
        wacc=wacc,
        rf_rate=rf_rate,
        erp=erp,
        kd_post=kd_post,
        e_wt=e_wt,
        d_wt=d_wt,
        total_assets=total_assets,
        total_debt=total_debt,
        revenue_base=revenue_base,
        operating_cf=operating_cf,
        fcf=fcf,
        capex_pct=capex_pct,
        capexes=capexes,
        da_pct=da_pct,
        das=das,
        sbc_pct=sbc_pct,
        sbcs=sbcs,
        tax_rate_pct=tax_rate_pct,
        pretax_incomes=pretax_incomes,
        tax_provisions=tax_provisions,
        dso=_latest_nonzero([snapshot.dso for snapshot in history], default=0.0),
        dio=_latest_nonzero([snapshot.dio for snapshot in history], default=0.0),
        dpo=_latest_nonzero([snapshot.dpo for snapshot in history], default=0.0),
        observations=[],
    )

    income_stmts, cash_flows, balance_sheets = _statement_payloads_from_history(history)
    dcf_result = run_dcf(
        ticker=ticker_upper,
        scenario="base",
        income_stmts=income_stmts,
        cash_flows=cash_flows,
        balance_sheets=balance_sheets,
        wacc=float(knowledge_model.get("wacc") or wacc) / 100.0,
        terminal_growth=float(knowledge_model.get("terminal_growth") or terminal_growth) / 100.0,
        near_term_growth=float(knowledge_model.get("revenue_growth_near") or revenue_growth_near) / 100.0,
        target_ebit_margin=float(knowledge_model.get("ebit_margin_target") or ebit_margin_target) / 100.0,
        tax_rate_override=float(knowledge_model.get("tax_rate_pct") or tax_rate_pct) / 100.0,
        da_pct_override=float(knowledge_model.get("da_pct") or da_pct) / 100.0,
        capex_pct_override=float(knowledge_model.get("capex_pct") or capex_pct) / 100.0,
        sbc_pct_override=float(knowledge_model.get("sbc_pct") or sbc_pct) / 100.0,
    )
    if not dcf_result.forecast_years_data:
        raise RuntimeError(f"DCF replay produced no forecast rows for {ticker_upper}")

    first_year = dcf_result.forecast_years_data[0]
    predicted_equity_value = max(float(dcf_result.enterprise_value) - float(base_snapshot.net_debt_mm or 0.0), 0.0)
    predicted_price = predicted_equity_value / base_snapshot.shares_outstanding_mm if base_snapshot.shares_outstanding_mm > 0 else 0.0
    feature_vector = tuple(knowledge_model.get("feature_vector") or ()) or None
    record_id = f"bootstrap::{ticker_upper}::{base_snapshot.available_date.isoformat()}::FY{base_snapshot.year + 1}"

    return PredictionRecord(
        record_id=record_id,
        ticker=ticker_upper,
        company_name=company_name,
        sector=sector,
        industry=industry,
        run_date=base_snapshot.available_date,
        forecast_horizon_year=base_snapshot.year + 1,
        years_since_ipo=max(len(history) - 1, 0),
        data_vintage_years=len(history),
        predicted_revenue_mm=round(float(first_year.revenue), 2),
        predicted_ebit_margin=round(float(first_year.ebit_margin), 6),
        predicted_ebit_mm=round(float(first_year.ebit), 2),
        predicted_ufcf_mm=round(float(first_year.ufcf), 2),
        predicted_wacc=round(float(knowledge_model.get("wacc") or wacc) / 100.0, 6),
        predicted_terminal_growth=round(float(knowledge_model.get("terminal_growth") or terminal_growth) / 100.0, 6),
        predicted_ev_mm=round(float(dcf_result.enterprise_value), 2),
        predicted_equity_value_mm=round(predicted_equity_value, 2),
        predicted_price_per_share=round(predicted_price, 4),
        scenario="base",
        near_term_revenue_growth=round(float(knowledge_model.get("revenue_growth_near") or revenue_growth_near) / 100.0, 6),
        target_ebit_margin=round(float(knowledge_model.get("ebit_margin_target") or ebit_margin_target) / 100.0, 6),
        da_pct_revenue=round(float(knowledge_model.get("da_pct") or da_pct) / 100.0, 6),
        capex_pct_revenue=round(float(knowledge_model.get("capex_pct") or capex_pct) / 100.0, 6),
        beta=round(beta, 6),
        erp=round(erp / 100.0, 6),
        rf_rate=round(rf_rate / 100.0, 6),
        actual_price_at_prediction=round(prediction_price, 4),
        actual_ev_at_prediction=round(market_cap + float(base_snapshot.net_debt_mm or 0.0), 2),
        market_cycle_phase="neutral",
        macro_backdrop={
            "rf_rate": round(rf_rate / 100.0, 6),
            "erp": round(erp / 100.0, 6),
            "terminal_growth": round(float(knowledge_model.get("terminal_growth") or terminal_growth) / 100.0, 6),
            "wacc": round(float(knowledge_model.get("wacc") or wacc) / 100.0, 6),
        },
        market_cap_regime=str(knowledge_model.get("market_cap_regime") or _market_cap_regime(market_cap)),
        macro_regime="neutral",
        feature_vector=feature_vector,
        fiscal_year_end_month=base_snapshot.period_end.month,
        fiscal_year_end_day=base_snapshot.period_end.day,
        prediction_context={
            "source": "historical_replay_bootstrap",
            "price_date": str(price_row.get("date") or base_snapshot.available_date.isoformat()),
            "price_source_field": str(price_row.get("source_field") or "close"),
            "replay_base_year": base_snapshot.year,
            "replay_target_year": base_snapshot.year + 1,
            "history_years": [snapshot.year for snapshot in history],
        },
    )


def _statement_payloads_from_history(
    history: list[AnnualFundamentalSnapshot],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    newest_first = list(reversed(history))
    income_stmts: list[dict[str, Any]] = []
    cash_flows: list[dict[str, Any]] = []
    balance_sheets: list[dict[str, Any]] = []
    for snapshot in newest_first:
        gross_margin_pct = snapshot.gross_margin_pct
        if gross_margin_pct is None:
            gross_margin_pct = round(max(snapshot.ebit_margin * 100.0 + 15.0, 20.0), 2)
        gross_profit = round(snapshot.revenue_mm * (gross_margin_pct / 100.0), 2)
        ebit = snapshot.ebit_mm if snapshot.ebit_mm is not None else round(snapshot.revenue_mm * snapshot.ebit_margin, 2)
        income_stmts.append(
            {
                "calendarYear": str(snapshot.year),
                "revenue": snapshot.revenue_mm,
                "ebit": ebit,
                "operatingIncome": ebit,
                "grossProfit": gross_profit,
            }
        )
        cash_flows.append(
            {
                "calendarYear": str(snapshot.year),
                "depreciationAndAmortization": snapshot.da_mm or 0.0,
                "capitalExpenditure": -abs(snapshot.capex_mm or 0.0),
                "stockBasedCompensation": snapshot.sbc_mm or 0.0,
            }
        )
        balance_sheets.append(
            {
                "calendarYear": str(snapshot.year),
                "totalAssets": snapshot.total_assets_mm or 0.0,
                "netReceivables": snapshot.net_receivables_mm or 0.0,
                "inventory": snapshot.inventory_mm or 0.0,
                "accountPayables": snapshot.accounts_payable_mm or 0.0,
            }
        )
    return income_stmts, cash_flows, balance_sheets


def _materialize_realized_outcomes_for_record(
    record: PredictionRecord,
    fundamentals: dict[str, Any],
    price_history: list[dict[str, Any]],
    *,
    ledger_reader: LedgerReader,
    ledger_writer: LedgerWriter,
    as_of_date: date,
    missing_labels: Counter[str],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    target_date = prediction_horizon_target_date(record)
    existing_outcomes = ledger_reader.query_realized_outcomes(record_id=record.record_id, limit=100)
    actuals_by_year = extract_actuals_from_fundamentals(fundamentals, as_of_date=as_of_date)
    aligned_actuals = align_prediction_record_to_actuals(record, actuals_by_year, as_of_date=as_of_date, strict=True)
    price_row = _price_row_on_or_before(price_history, target_date)

    created = 0
    partial_created = 0
    full_created = 0
    duplicates = 0

    if price_row is not None:
        inserted = _persist_realized_outcome(
            ledger_writer,
            _price_only_outcome(record, price_row),
        )
        if inserted:
            created += 1
            partial_created += 1
        else:
            duplicates += 1
    else:
        missing_labels["missing_horizon_price"] += 1
        issues.append({"ticker": record.ticker, "record_id": record.record_id, "reason": "missing_horizon_price"})

    if aligned_actuals is not None:
        fundamental_outcome = _fundamental_outcome(record, aligned_actuals, price_row=price_row)
        inserted = _persist_realized_outcome(ledger_writer, fundamental_outcome)
        if inserted:
            created += 1
            if fundamental_outcome.label_status == "complete":
                full_created += 1
            else:
                partial_created += 1
        else:
            duplicates += 1
    else:
        missing_labels["missing_aligned_actuals"] += 1
        issues.append({"ticker": record.ticker, "record_id": record.record_id, "reason": "missing_aligned_actuals"})

    has_realized = bool(created or existing_outcomes or ledger_reader.query_realized_outcomes(record_id=record.record_id, limit=1))
    skipped = not has_realized
    return {
        "created": created,
        "partial_created": partial_created,
        "full_created": full_created,
        "duplicates": duplicates,
        "has_realized": has_realized,
        "skipped": skipped,
    }


def _price_only_outcome(record: PredictionRecord, price_row: dict[str, Any]) -> RealizedOutcomeRecord:
    target_date = prediction_horizon_target_date(record)
    price_date = _parse_date_value(price_row.get("date")) or target_date
    close_value = round(float(_optional_float(price_row.get("close")) or 0.0), 4)
    return RealizedOutcomeRecord(
        record_id=record.record_id,
        ticker=record.ticker,
        forecast_horizon_year=record.forecast_horizon_year,
        horizon_target_date=target_date,
        horizon_label=record.horizon_label or f"FY{record.forecast_horizon_year}",
        label_as_of_date=price_date,
        aligned_period_end=target_date,
        alignment_method="market_close_on_or_before_target",
        source_name="eodhd_eod",
        source_kind="market_price",
        actual_price_at_horizon=close_value,
        macro_backdrop={},
        unknown_targets=["actual_revenue_mm", "actual_ebit_margin", "actual_ufcf_mm", "actual_ev_mm"],
        source_payload={
            "price_date": price_date.isoformat(),
            "price_source_field": str(price_row.get("source_field") or "close"),
            "trading_day_offset_days": (target_date - price_date).days,
        },
        evidence_notes=f"Historical market close aligned on or before {target_date.isoformat()}.",
    )


def _fundamental_outcome(
    record: PredictionRecord,
    aligned_actuals: dict[str, Any],
    *,
    price_row: dict[str, Any] | None,
) -> RealizedOutcomeRecord:
    target_date = prediction_horizon_target_date(record)
    payload = dict(aligned_actuals)
    unknown_targets = {
        str(target)
        for target in (payload.get("unknown_targets") or [])
        if str(target) in REALIZED_VALUE_TARGETS
    }
    source_payload = dict(payload.get("source_payload") or {})

    actual_price_at_horizon = None
    actual_ev_mm = payload.get("actual_ev_mm")
    if price_row is not None:
        actual_price_at_horizon = round(float(_optional_float(price_row.get("close")) or 0.0), 4)
        price_date = _parse_date_value(price_row.get("date"))
        if price_date is not None:
            source_payload["horizon_price_date"] = price_date.isoformat()
            source_payload["price_source_field"] = str(price_row.get("source_field") or "close")
        shares_outstanding = _optional_float(payload.get("_shares_outstanding_mm"))
        net_debt = _optional_float(payload.get("_net_debt_mm"))
        if shares_outstanding is not None and net_debt is not None:
            actual_ev_mm = round(actual_price_at_horizon * shares_outstanding + net_debt, 2)

    known_targets = {
        "actual_revenue_mm": payload.get("actual_revenue_mm"),
        "actual_ebit_margin": payload.get("actual_ebit_margin"),
        "actual_ufcf_mm": payload.get("actual_ufcf_mm"),
        "actual_ev_mm": actual_ev_mm,
        "actual_price_at_horizon": actual_price_at_horizon,
    }
    for target_name, value in known_targets.items():
        if value is None:
            unknown_targets.add(target_name)
        else:
            unknown_targets.discard(target_name)

    source_name = str(payload.get("source_name") or "eodhd_fundamentals")
    source_kind = str(payload.get("source_kind") or "fundamentals")
    if actual_price_at_horizon is not None:
        source_name = f"{source_name}+eodhd_eod"
        source_kind = "blended_realized_evidence"

    return RealizedOutcomeRecord(
        record_id=record.record_id,
        ticker=record.ticker,
        forecast_horizon_year=record.forecast_horizon_year,
        horizon_target_date=target_date,
        horizon_label=record.horizon_label or f"FY{record.forecast_horizon_year}",
        label_as_of_date=_parse_date_value(payload.get("label_as_of_date")) or target_date,
        aligned_period_end=_parse_date_value(payload.get("aligned_period_end")) or target_date,
        alignment_method=str(payload.get("alignment_method") or "fiscal_year_exact"),
        source_name=source_name,
        source_kind=source_kind,
        actual_revenue_mm=_optional_float(payload.get("actual_revenue_mm")),
        actual_ebit_margin=_optional_float(payload.get("actual_ebit_margin")),
        actual_ufcf_mm=_optional_float(payload.get("actual_ufcf_mm")),
        actual_ev_mm=_optional_float(actual_ev_mm),
        actual_price_at_horizon=actual_price_at_horizon,
        macro_backdrop=dict(payload.get("macro_backdrop") or {}),
        surprise_flags=list(payload.get("surprise_flags") or []),
        structural_break_hints=list(payload.get("structural_break_hints") or []),
        unknown_targets=sorted(unknown_targets),
        source_payload=source_payload,
        evidence_notes=str(payload.get("notes") or f"Aligned realized evidence for {target_date.isoformat()}"),
    )


def _persist_realized_outcome(ledger_writer: LedgerWriter, outcome: RealizedOutcomeRecord) -> bool:
    inserted = ledger_writer.append_realized_outcome(outcome)
    ledger_writer.backfill_actuals(
        outcome.record_id,
        actual_revenue_mm=outcome.actual_revenue_mm,
        actual_ebit_margin=outcome.actual_ebit_margin,
        actual_ufcf_mm=outcome.actual_ufcf_mm,
        actual_ev_mm=outcome.actual_ev_mm,
        actual_price_at_horizon=outcome.actual_price_at_horizon,
        label_as_of_date=outcome.label_as_of_date,
        aligned_period_end=outcome.aligned_period_end,
        source_name=outcome.source_name,
        source_kind=outcome.source_kind,
        alignment_method=outcome.alignment_method,
        macro_backdrop=outcome.macro_backdrop,
        surprise_flags=outcome.surprise_flags,
        structural_break_hints=outcome.structural_break_hints,
        unknown_targets=outcome.unknown_targets,
        source_payload=outcome.source_payload,
        postmortem_notes=outcome.evidence_notes,
    )
    return inserted


def _materialize_quinquennial_reports(
    ticker: str,
    *,
    ledger_reader: LedgerReader,
    ledger_writer: LedgerWriter,
    report_store: QuinquennialStore,
) -> int:
    postmortem_years: set[int] = set()
    for record in ledger_reader.query(ticker=ticker, limit=5000):
        if ledger_reader.query_postmortems(record_id=record.record_id):
            postmortem_years.add(record.forecast_horizon_year)

    created = 0
    for base_year in _candidate_base_years(sorted(postmortem_years)):
        if report_store.has_report(ticker, base_year):
            continue
        report = run_5year_postmortem(
            ticker,
            base_year,
            ledger_reader=ledger_reader,
            ledger_writer=ledger_writer,
            report_store=report_store,
        )
        if report.annual_records:
            created += 1
    return created


def _price_row_on_or_before(price_history: list[dict[str, Any]], target_date: date) -> dict[str, Any] | None:
    candidate: dict[str, Any] | None = None
    candidate_date: date | None = None
    for row in price_history:
        row_date = _parse_date_value(row.get("date"))
        if row_date is None or row_date > target_date:
            continue
        if candidate_date is None or row_date > candidate_date:
            candidate = dict(row)
            candidate_date = row_date
    return candidate


def _resolve_total_debt(balance_sheet: dict[str, Any]) -> float | None:
    total_debt = _optional_float(balance_sheet.get("shortLongTermDebtTotal"))
    if total_debt is not None:
        return total_debt
    total_debt = _optional_float(balance_sheet.get("totalDebt"))
    if total_debt is not None:
        return total_debt
    long_term = _optional_float(balance_sheet.get("longTermDebtTotal"))
    if long_term is None:
        long_term = _optional_float(balance_sheet.get("longTermDebt"))
    short_term = _optional_float(balance_sheet.get("shortTermDebt"))
    if long_term is None and short_term is None:
        return None
    return (long_term or 0.0) + (short_term or 0.0)


def _resolve_net_debt(balance_sheet: dict[str, Any]) -> float | None:
    net_debt = _optional_float(balance_sheet.get("netDebt"))
    if net_debt is not None:
        return net_debt
    total_debt = _resolve_total_debt(balance_sheet)
    if total_debt is None:
        return None
    cash_balance = _optional_float(balance_sheet.get("cashAndShortTermInvestments"))
    if cash_balance is None:
        cash_balance = _optional_float(balance_sheet.get("cashAndEquivalents"))
    if cash_balance is None:
        cash_balance = _optional_float(balance_sheet.get("cash"))
    return total_debt - (cash_balance or 0.0)


def _resolve_shares_outstanding(balance_sheet: dict[str, Any]) -> float | None:
    shares = _optional_float(balance_sheet.get("commonStockSharesOutstanding"))
    if shares is not None and shares > 0:
        return shares
    shares = _optional_float(balance_sheet.get("commonStockSharesIssued"))
    if shares is not None and shares > 0:
        return shares
    return None


def _ratio_days(numerator: float | None, denominator: float | None) -> float:
    if numerator is None or denominator is None or denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 365.0, 2)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round((numerator / denominator) * 100.0, 2)


def _recent_growth_pct(revenues: list[float]) -> float:
    trailing = [value for value in revenues if value > 0]
    if len(trailing) < 2:
        return 0.0
    growth_rates: list[float] = []
    for previous, current in zip(trailing[-4:], trailing[-3:]):
        if previous <= 0:
            continue
        growth_rates.append((current / previous) - 1.0)
    if not growth_rates:
        previous = trailing[-2]
        current = trailing[-1]
        return round(((current / previous) - 1.0) * 100.0, 2) if previous > 0 else 0.0
    return round((sum(growth_rates) / len(growth_rates)) * 100.0, 2)


def _central_tendency(values: list[float], *, default: float) -> float:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return default
    return sum(clean) / len(clean)


def _last_ratio_pct(amounts: list[float | None], revenues: list[float], *, default: float) -> float:
    ratios: list[float] = []
    for amount, revenue in zip(amounts, revenues):
        if amount is None or revenue <= 0:
            continue
        ratios.append(abs(amount) / revenue * 100.0)
    if not ratios:
        return default
    return round(ratios[-1], 2)


def _tax_rate_pct(history: list[AnnualFundamentalSnapshot], *, default: float) -> float:
    for snapshot in reversed(history):
        pretax = snapshot.pretax_income_mm
        tax = snapshot.tax_provision_mm
        if pretax is None or tax is None or pretax <= 0:
            continue
        return round(max(0.0, min(60.0, abs(tax) / pretax * 100.0)), 2)
    return default


def _latest_nonzero(values: list[float], *, default: float) -> float:
    for value in reversed(values):
        if value > 0:
            return float(value)
    return default


def _config_pct(value: float | int | None) -> float:
    raw = _optional_float(value)
    if raw is None:
        return 0.0
    return round(raw * 100.0, 2) if abs(raw) <= 1.0 else round(raw, 2)


def _current_risk_free_rate() -> float:
    from webapp.data.eodhd_client import _get_risk_free_rate

    return round(float(_get_risk_free_rate() or 4.4), 2)


def _market_cap_regime(market_cap_mm: float) -> str:
    if market_cap_mm < 2_000:
        return "small"
    if market_cap_mm < 10_000:
        return "mid"
    if market_cap_mm < 50_000:
        return "large"
    return "mega"