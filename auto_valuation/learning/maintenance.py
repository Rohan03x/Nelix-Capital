"""Scheduled maintenance for adaptive learning records."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from auto_valuation.config import LEARNING_CONFIG

from .ledger import LedgerReader, LedgerWriter, PredictionRecord, prediction_horizon_target_date
from .postmortem import QuinquennialStore, run_5year_postmortem, run_annual_postmortem
from .storage_paths import learning_db_dir


PACKAGE_ROOT = Path(__file__).resolve().parent
MAINTENANCE_STATE_PATH = learning_db_dir() / "maintenance_state.json"


@dataclass(frozen=True)
class LearningMaintenanceResult:
    enabled: bool
    ran: bool
    reason: str | None = None
    scanned_tickers: int = 0
    matured_records: int = 0
    backfilled_records: int = 0
    partial_backfilled_records: int = 0
    annual_postmortems_created: int = 0
    quinquennial_reports_created: int = 0
    skipped_tickers: int = 0
    tickers_processed: list[str] = field(default_factory=list)
    available_years: dict[str, list[int]] = field(default_factory=dict)
    last_run_at: str | None = None
    maintenance_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_actuals_from_fundamentals(
    fundamentals: dict[str, Any],
    *,
    as_of_date: date | None = None,
    source_name: str | None = None,
) -> dict[int, dict[str, Any]]:
    cutoff = as_of_date or date.today()
    source_name = source_name or str(LEARNING_CONFIG.get("realized_actuals_source_name", "eodhd_fundamentals"))

    financials = dict(fundamentals.get("Financials") or {})
    income_yearly = dict((financials.get("Income_Statement") or {}).get("yearly") or {})
    cash_flow_yearly = dict((financials.get("Cash_Flow") or {}).get("yearly") or {})
    balance_sheet_yearly = dict((financials.get("Balance_Sheet") or {}).get("yearly") or {})

    income_by_year = _annual_periods_by_year(income_yearly, as_of_date=cutoff)
    cash_flow_by_year = _annual_periods_by_year(cash_flow_yearly, as_of_date=cutoff)
    balance_sheet_by_year = _annual_periods_by_year(balance_sheet_yearly, as_of_date=cutoff)

    actuals: dict[int, dict[str, Any]] = {}
    prior_actuals: dict[str, Any] | None = None
    for year in sorted(set(income_by_year) | set(cash_flow_by_year) | set(balance_sheet_by_year)):
        income_dict = dict(income_by_year.get(year) or {})
        cash_flow = dict(cash_flow_by_year.get(year) or {})
        balance_sheet = dict(balance_sheet_by_year.get(year) or {})

        period_end = (
            _parse_date_value(income_dict.get("_period_end"))
            or _parse_date_value(cash_flow.get("_period_end"))
            or _parse_date_value(balance_sheet.get("_period_end"))
        )
        if period_end is None:
            continue

        revenue_raw = _optional_float(income_dict.get("totalRevenue"))
        if revenue_raw is None or revenue_raw <= 0:
            continue
        revenue_mm = round(revenue_raw / 1e6, 2)

        ebit_raw = _optional_float(income_dict.get("ebit"))
        actual_ebit_margin = (ebit_raw / revenue_raw) if ebit_raw is not None else None

        cash_flow_period_end = _parse_date_value(cash_flow.get("_period_end"))
        free_cash_flow_raw = _optional_float(cash_flow.get("freeCashFlow"))
        if free_cash_flow_raw is not None:
            actual_ufcf_mm = round(free_cash_flow_raw / 1e6, 2)
        else:
            operating_cf_raw = _optional_float(cash_flow.get("totalCashFromOperatingActivities"))
            capex_raw = _optional_float(cash_flow.get("capitalExpenditures"))
            if operating_cf_raw is not None and capex_raw is not None:
                actual_ufcf_mm = round((operating_cf_raw - abs(capex_raw)) / 1e6, 2)
            else:
                actual_ufcf_mm = None

        shares_outstanding_raw = _optional_float(balance_sheet.get("commonStockSharesOutstanding"))
        shares_outstanding_mm = (
            round(shares_outstanding_raw / 1e6, 4)
            if shares_outstanding_raw is not None and shares_outstanding_raw > 0
            else None
        )
        net_debt_raw = _optional_float(balance_sheet.get("netDebt"))
        if net_debt_raw is None:
            total_debt_raw = _optional_float(balance_sheet.get("shortLongTermDebtTotal"))
            if total_debt_raw is None:
                long_term_debt_raw = _optional_float(balance_sheet.get("longTermDebtTotal"))
                if long_term_debt_raw is None:
                    long_term_debt_raw = _optional_float(balance_sheet.get("longTermDebt"))
                short_term_debt_raw = _optional_float(balance_sheet.get("shortTermDebt"))
                if long_term_debt_raw is not None or short_term_debt_raw is not None:
                    total_debt_raw = (long_term_debt_raw or 0.0) + (short_term_debt_raw or 0.0)
            cash_raw = _optional_float(balance_sheet.get("cashAndShortTermInvestments"))
            if cash_raw is None:
                cash_raw = _optional_float(balance_sheet.get("cashAndEquivalents"))
            if cash_raw is None:
                cash_raw = _optional_float(balance_sheet.get("cash"))
            if total_debt_raw is not None:
                net_debt_raw = total_debt_raw - (cash_raw or 0.0)
        net_debt_mm = round(net_debt_raw / 1e6, 2) if net_debt_raw is not None else None

        component_dates = [
            _parse_date_value(income_dict.get("_available_date")),
            _parse_date_value(cash_flow.get("_available_date")) if actual_ufcf_mm is not None else None,
            _parse_date_value(balance_sheet.get("_available_date")),
        ]
        known_component_dates = [item for item in component_dates if item is not None]
        label_as_of = max(known_component_dates) if known_component_dates else period_end

        surprise_flags = _surprise_markers(actual_ebit_margin=actual_ebit_margin, actual_ufcf_mm=actual_ufcf_mm)
        structural_break_hints = _structural_break_hints(
            actual_revenue_mm=revenue_mm,
            actual_ebit_margin=actual_ebit_margin,
            prior_actuals=prior_actuals,
        )

        unknown_targets: list[str] = ["actual_ev_mm", "actual_price_at_horizon", "macro_backdrop"]
        if actual_ebit_margin is None:
            unknown_targets.append("actual_ebit_margin")
        if actual_ufcf_mm is None:
            unknown_targets.append("actual_ufcf_mm")

        source_payload = {"income_statement_date": period_end.isoformat()}
        income_filing_date = _parse_date_value(income_dict.get("_filing_date"))
        if income_filing_date is not None:
            source_payload["income_statement_filing_date"] = income_filing_date.isoformat()
        if cash_flow_period_end is not None:
            source_payload["cash_flow_date"] = cash_flow_period_end.isoformat()
        cash_flow_filing_date = _parse_date_value(cash_flow.get("_filing_date"))
        if cash_flow_filing_date is not None:
            source_payload["cash_flow_filing_date"] = cash_flow_filing_date.isoformat()
        balance_sheet_period_end = _parse_date_value(balance_sheet.get("_period_end"))
        if balance_sheet_period_end is not None:
            source_payload["balance_sheet_date"] = balance_sheet_period_end.isoformat()
        balance_sheet_filing_date = _parse_date_value(balance_sheet.get("_filing_date"))
        if balance_sheet_filing_date is not None:
            source_payload["balance_sheet_filing_date"] = balance_sheet_filing_date.isoformat()

        actuals[year] = {
            "actual_revenue_mm": revenue_mm,
            "actual_ebit_margin": actual_ebit_margin,
            "actual_ufcf_mm": actual_ufcf_mm,
            "actual_ev_mm": None,
            "actual_price_at_horizon": None,
            "macro_backdrop": {},
            "surprise_flags": surprise_flags,
            "structural_break_hints": structural_break_hints,
            "unknown_targets": sorted(set(unknown_targets)),
            "aligned_period_end": period_end.isoformat(),
            "label_as_of_date": label_as_of.isoformat(),
            "source_name": source_name,
            "source_kind": "fundamentals",
            "source_payload": source_payload,
            "notes": f"Auto-backfilled from {source_name} for period ending {period_end.isoformat()}.",
            "_shares_outstanding_mm": shares_outstanding_mm,
            "_net_debt_mm": net_debt_mm,
            "_income_available_date": income_dict.get("_available_date"),
            "_cash_flow_available_date": cash_flow.get("_available_date"),
            "_balance_sheet_available_date": balance_sheet.get("_available_date"),
        }
        prior_actuals = actuals[year]
    return actuals


def extract_quarterly_actuals_from_fundamentals(
    fundamentals: dict[str, Any],
    *,
    as_of_date: date | None = None,
) -> dict[str, dict[str, Any]]:
    """Extract realized quarterly EPS actuals from EODHD Earnings.History.

    Returns a dict keyed by quarter_end date string (e.g. '2025-09-30') containing
    realized EPS data used to verify quarterly predictions in the ledger.
    """
    cutoff = as_of_date or date.today()
    earnings = dict(fundamentals.get("Earnings") or {})
    history = dict(earnings.get("History") or {})

    quarterly: dict[str, dict[str, Any]] = {}
    for date_str, entry in history.items():
        try:
            q_date = date.fromisoformat(str(date_str))
        except (ValueError, TypeError):
            continue
        if q_date > cutoff:
            continue  # Future quarter, not yet reported
        eps_actual = entry.get("epsActual")
        if eps_actual is None:
            continue
        try:
            eps_actual = float(eps_actual)
        except (ValueError, TypeError):
            continue
        quarterly[date_str] = {
            "quarter_end": date_str,
            "eps_actual": eps_actual,
            "eps_estimate": entry.get("epsEstimate"),
            "eps_surprise_pct": entry.get("surprisePercent"),
            "report_date": entry.get("reportDate"),
        }
    return quarterly


def run_live_evidence_bootstrap(*args: Any, **kwargs: Any) -> Any:
    from .live_evidence_bootstrap import run_live_evidence_bootstrap as _run_live_evidence_bootstrap

    return _run_live_evidence_bootstrap(*args, **kwargs)


def align_prediction_record_to_actuals(
    record: PredictionRecord,
    actuals_by_year: dict[int, dict[str, Any]],
    *,
    as_of_date: date | None = None,
    strict: bool = True,
) -> dict[str, Any] | None:
    cutoff = as_of_date or date.today()
    target_date = prediction_horizon_target_date(record)
    if target_date > cutoff:
        return None

    candidate = dict(actuals_by_year.get(int(record.forecast_horizon_year or 0)) or {})
    if not candidate:
        return None

    aligned_period_end = _parse_date_value(candidate.get("aligned_period_end"))
    if aligned_period_end is None:
        if strict:
            return None
        aligned_period_end = target_date
        candidate["aligned_period_end"] = aligned_period_end.isoformat()

    if aligned_period_end > cutoff:
        return None
    if strict and aligned_period_end != target_date:
        return None

    candidate.setdefault("label_as_of_date", cutoff.isoformat())
    candidate.setdefault("source_name", str(LEARNING_CONFIG.get("realized_actuals_source_name", "eodhd_fundamentals")))
    candidate.setdefault("source_kind", "fundamentals")
    candidate.setdefault("source_payload", {})
    candidate.setdefault("unknown_targets", [])
    candidate.setdefault("macro_backdrop", {})
    candidate.setdefault("surprise_flags", [])
    candidate.setdefault("structural_break_hints", [])
    candidate.setdefault("horizon_target_date", target_date.isoformat())
    return candidate


def run_scheduled_learning_maintenance(
    *,
    fundamentals_provider: Callable[[str], dict[str, Any] | None],
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    report_store: QuinquennialStore | None = None,
    as_of_date: date | None = None,
    state_path: str | Path | None = None,
    interval_hours: int | None = None,
    max_tickers: int | None = None,
    prefetched_fundamentals: dict[str, dict[str, Any]] | None = None,
) -> LearningMaintenanceResult:
    enabled = bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("scheduled_postmortem_enabled", True))
    if not enabled:
        return LearningMaintenanceResult(enabled=False, ran=False, reason="disabled")

    state_file = Path(state_path) if state_path else MAINTENANCE_STATE_PATH
    started_at = datetime.now(timezone.utc)
    interval_hours = int(interval_hours if interval_hours is not None else LEARNING_CONFIG.get("scheduled_postmortem_interval_hours", 24))
    state = _load_state(state_file)
    last_run_at = _parse_datetime(state.get("maintenance_last_run_at"))
    if interval_hours > 0 and last_run_at and started_at - last_run_at < timedelta(hours=interval_hours):
        return LearningMaintenanceResult(
            enabled=True,
            ran=False,
            reason="throttled",
            last_run_at=last_run_at.isoformat(),
            maintenance_run_id=state.get("maintenance_last_run_id") or state.get("last_run_id"),
        )

    ledger_reader = ledger_reader or LedgerReader()
    ledger_writer = ledger_writer or LedgerWriter(ledger_reader.db_path)
    report_store = report_store or QuinquennialStore()
    today = as_of_date or date.today()
    prefetched = {key.upper(): value for key, value in (prefetched_fundamentals or {}).items()}
    strict_alignment = bool(LEARNING_CONFIG.get("strict_horizon_alignment", True))

    all_records = [record for record in ledger_reader.query() if record.scenario == "base"]
    candidate_records = [record for record in all_records if prediction_horizon_target_date(record) <= today]
    tickers: list[str] = []
    seen: set[str] = set()
    for record in candidate_records:
        ticker = str(record.ticker or "").upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        tickers.append(ticker)

    max_tickers = int(max_tickers if max_tickers is not None else LEARNING_CONFIG.get("scheduled_postmortem_max_tickers_per_run", 6))
    selected_tickers = tickers[:max_tickers]

    tickers_processed: list[str] = []
    available_years: dict[str, list[int]] = {}
    matured_records = 0
    backfilled_records = 0
    partial_backfilled_records = 0
    annual_postmortems_created = 0
    quinquennial_reports_created = 0
    skipped_tickers = 0

    for ticker in selected_tickers:
        fundamentals = prefetched.get(ticker)
        if fundamentals is None:
            fundamentals = fundamentals_provider(ticker)
        if not fundamentals:
            skipped_tickers += 1
            continue

        actuals_by_year = extract_actuals_from_fundamentals(fundamentals, as_of_date=today)
        available_years[ticker] = sorted(actuals_by_year)
        if not actuals_by_year:
            skipped_tickers += 1
            continue

        ticker_records = [
            record
            for record in candidate_records
            if str(record.ticker or "").upper() == ticker and int(record.forecast_horizon_year or 0) in actuals_by_year
        ]
        if not ticker_records:
            continue

        tickers_processed.append(ticker)
        aligned_years: set[int] = set()
        for record in ticker_records:
            actuals = align_prediction_record_to_actuals(
                record,
                actuals_by_year,
                as_of_date=today,
                strict=strict_alignment,
            )
            if not actuals:
                continue
            matured_records += 1
            aligned_years.add(int(record.forecast_horizon_year or 0))
            if ledger_writer.backfill_actuals(
                record.record_id,
                actual_revenue_mm=actuals.get("actual_revenue_mm"),
                actual_ebit_margin=actuals.get("actual_ebit_margin"),
                actual_ufcf_mm=actuals.get("actual_ufcf_mm"),
                actual_ev_mm=actuals.get("actual_ev_mm"),
                actual_price_at_horizon=actuals.get("actual_price_at_horizon"),
                postmortem_notes=actuals.get("notes"),
                label_as_of_date=_parse_date_value(actuals.get("label_as_of_date")),
                aligned_period_end=_parse_date_value(actuals.get("aligned_period_end")),
                source_name=str(actuals.get("source_name") or LEARNING_CONFIG.get("realized_actuals_source_name", "eodhd_fundamentals")),
                source_kind=str(actuals.get("source_kind") or "fundamentals"),
                macro_backdrop=dict(actuals.get("macro_backdrop") or {}),
                surprise_flags=list(actuals.get("surprise_flags") or []),
                structural_break_hints=list(actuals.get("structural_break_hints") or []),
                unknown_targets=list(actuals.get("unknown_targets") or []),
                source_payload=dict(actuals.get("source_payload") or {}),
            ):
                backfilled_records += 1
                if actuals.get("unknown_targets"):
                    partial_backfilled_records += 1

        if LEARNING_CONFIG.get("annual_postmortem_enabled", True):
            for horizon_year in sorted(aligned_years):
                year_predictions = ledger_reader.query(ticker=ticker, horizon_year=horizon_year, scenario="base")
                if year_predictions and all(ledger_reader.query_postmortems(record_id=prediction.record_id) for prediction in year_predictions):
                    continue
                annual_postmortems_created += len(
                    run_annual_postmortem(
                        ticker,
                        horizon_year,
                        ledger_reader=ledger_reader,
                        ledger_writer=ledger_writer,
                        actual_fetcher=_actual_fetcher(actuals_by_year),
                        persist=True,
                    )
                )

        if LEARNING_CONFIG.get("quinquennial_postmortem_enabled", True):
            postmortem_years = sorted(
                {
                    int(record.forecast_horizon_year or 0)
                    for record in ticker_records
                    if ledger_reader.query_postmortems(record_id=record.record_id)
                }
            )
            for base_year in _candidate_base_years(postmortem_years):
                if report_store.has_report(ticker, base_year):
                    continue
                report = run_5year_postmortem(
                    ticker,
                    base_year,
                    ledger_reader=ledger_reader,
                    ledger_writer=ledger_writer,
                    actual_fetcher=_actual_fetcher(actuals_by_year),
                    report_store=report_store,
                )
                if report.annual_records:
                    quinquennial_reports_created += 1

    completed_at = datetime.now(timezone.utc).isoformat()
    result_payload = {
        "enabled": True,
        "ran": True,
        "scanned_tickers": len(selected_tickers),
        "matured_records": matured_records,
        "backfilled_records": backfilled_records,
        "partial_backfilled_records": partial_backfilled_records,
        "annual_postmortems_created": annual_postmortems_created,
        "quinquennial_reports_created": quinquennial_reports_created,
        "skipped_tickers": skipped_tickers,
        "tickers_processed": tickers_processed,
        "available_years": available_years,
        "last_run_at": completed_at,
    }
    maintenance_run_id: str | None = None
    if LEARNING_CONFIG.get("maintenance_store_run_history", True) and hasattr(ledger_writer, "append_maintenance_run"):
        maintenance_run_id = ledger_writer.append_maintenance_run(
            result_payload,
            started_at=started_at.isoformat(),
            completed_at=completed_at,
        )

    _save_state(
        state_file,
        {
            **state,
            "last_run_at": completed_at,
            "last_run_id": maintenance_run_id,
            "maintenance_last_run_at": completed_at,
            "maintenance_last_run_id": maintenance_run_id,
            "tickers_processed": tickers_processed,
            "annual_postmortems_created": annual_postmortems_created,
            "quinquennial_reports_created": quinquennial_reports_created,
        },
    )

    return LearningMaintenanceResult(
        enabled=True,
        ran=True,
        scanned_tickers=len(selected_tickers),
        matured_records=matured_records,
        backfilled_records=backfilled_records,
        partial_backfilled_records=partial_backfilled_records,
        annual_postmortems_created=annual_postmortems_created,
        quinquennial_reports_created=quinquennial_reports_created,
        skipped_tickers=skipped_tickers,
        tickers_processed=tickers_processed,
        available_years=available_years,
        last_run_at=completed_at,
        maintenance_run_id=maintenance_run_id,
    )


def _actual_fetcher(actuals_by_year: dict[int, dict[str, Any]]) -> Callable[[str, int], dict[str, Any]]:
    def fetch(_ticker: str, horizon_year: int) -> dict[str, Any]:
        payload = dict(actuals_by_year.get(horizon_year) or {})
        payload.setdefault("macro_backdrop", {})
        payload.setdefault("surprise_flags", [])
        payload.setdefault("structural_break_hints", [])
        payload.setdefault("unknown_targets", [])
        payload.setdefault("notes", f"No auto-backfilled actuals available for FY{horizon_year}.")
        return payload

    return fetch


def _annual_periods_by_year(periods: dict[str, Any], *, as_of_date: date) -> dict[int, dict[str, Any]]:
    annual_periods: dict[int, dict[str, Any]] = {}
    for as_of, period in periods.items():
        period_dict = dict(period or {})
        period_end = _parse_date_value(period_dict.get("date") or as_of)
        filing_date = _parse_date_value(period_dict.get("filing_date")) or period_end
        available_date = max(period_end, filing_date) if filing_date is not None else period_end
        if period_end is None or available_date is None or available_date > as_of_date:
            continue
        current = annual_periods.get(period_end.year)
        current_end = _parse_date_value((current or {}).get("_period_end"))
        if current_end is None or period_end > current_end:
            annual_periods[period_end.year] = {
                **period_dict,
                "_period_end": period_end,
                "_filing_date": filing_date,
                "_available_date": available_date,
            }
    return annual_periods


def _surprise_markers(*, actual_ebit_margin: float | None, actual_ufcf_mm: float | None) -> list[str]:
    flags: list[str] = []
    if actual_ebit_margin is not None and actual_ebit_margin < 0:
        flags.append("negative_ebit_margin")
    if actual_ufcf_mm is not None and actual_ufcf_mm < 0:
        flags.append("negative_ufcf")
    return flags


def _structural_break_hints(
    *,
    actual_revenue_mm: float,
    actual_ebit_margin: float | None,
    prior_actuals: dict[str, Any] | None,
) -> list[str]:
    hints: list[str] = []
    if not prior_actuals:
        return hints
    prior_revenue = prior_actuals.get("actual_revenue_mm")
    if prior_revenue not in (None, 0):
        revenue_delta_pct = (actual_revenue_mm / float(prior_revenue)) - 1.0
        if revenue_delta_pct <= -0.20:
            hints.append("revenue_drop_gt_20pct")
    prior_margin = prior_actuals.get("actual_ebit_margin")
    if prior_margin is not None and actual_ebit_margin is not None and actual_ebit_margin - float(prior_margin) <= -0.05:
        hints.append("ebit_margin_drop_gt_500bps")
    return hints


def _candidate_base_years(postmortem_years: list[int]) -> list[int]:
    years = sorted(set(postmortem_years))
    base_years: list[int] = []
    year_set = set(years)
    for start_year in years:
        window = {start_year + offset for offset in range(5)}
        if window.issubset(year_set):
            base_years.append(start_year - 1)
    return base_years


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date_value(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
