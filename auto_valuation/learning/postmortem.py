"""Annual and quinquennial postmortems for adaptive DCF learning."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .attribution import ErrorDriver, aggregate_attributions, attribute_postmortem
from .ledger import (
    LedgerReader,
    LedgerWriter,
    PredictionRecord,
    REALIZED_VALUE_FIELDS,
    RealizedOutcomeRecord,
    prediction_horizon_target_date,
)
from .storage_paths import learning_db_dir


POSTMORTEM_DB_PATH = learning_db_dir() / "postmortems.db"


@dataclass(frozen=True)
class PostmortemRecord:
    postmortem_id: str
    record_id: str
    ticker: str
    forecast_horizon_year: int
    postmortem_date: date
    actual_revenue_mm: float | None
    actual_ebit_margin: float | None
    actual_ufcf_mm: float | None
    actual_ev_mm: float | None
    actual_price_at_horizon: float | None
    revenue_error_pct: float
    margin_error_bps: float
    ev_error_pct: float
    price_return_error_pct: float
    primary_miss_driver: str
    surprise_flags: list[str] = field(default_factory=list)
    error_attribution: list[tuple[ErrorDriver | str, float]] = field(default_factory=list)
    structural_break_detected: bool = False
    # BRAIN_IMPROVEMENT_PLAN.md (H4) — graded break score (0.0=clean, 1.0=severe)
    structural_break_score: float = 0.0
    model_bias_signal: str = "neutral"
    postmortem_notes: str | None = None
    prediction_snapshot: dict[str, Any] = field(default_factory=dict)
    macro_backdrop_at_prediction: dict[str, float] = field(default_factory=dict)
    macro_backdrop_at_horizon: dict[str, float] = field(default_factory=dict)
    actual_wacc: float | None = None
    actual_terminal_growth: float | None = None
    realized_outcome_id: str | None = None
    realized_label_status: str = "pending"
    realized_unknown_targets: list[str] = field(default_factory=list)
    horizon_target_date: date | None = None
    aligned_period_end: date | None = None
    alignment_method: str | None = None
    realized_source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["postmortem_date"] = self.postmortem_date.isoformat()
        payload["horizon_target_date"] = self.horizon_target_date.isoformat() if self.horizon_target_date else None
        payload["aligned_period_end"] = self.aligned_period_end.isoformat() if self.aligned_period_end else None
        payload["error_attribution"] = [
            ((driver.value if isinstance(driver, ErrorDriver) else driver), contribution)
            for driver, contribution in self.error_attribution
        ]
        return payload


@dataclass(frozen=True)
class QuinquennialReport:
    report_id: str
    ticker: str
    base_year: int
    created_at: date
    annual_records: list[PostmortemRecord] = field(default_factory=list)
    trajectory_analysis: dict[str, Any] = field(default_factory=dict)
    assumption_drift_diagnosis: dict[str, Any] = field(default_factory=dict)
    structural_breaks: list[dict[str, Any]] = field(default_factory=list)
    compounding_error_attribution: list[tuple[ErrorDriver | str, float]] = field(default_factory=list)
    cross_industry_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["annual_records"] = [record.to_dict() for record in self.annual_records]
        payload["compounding_error_attribution"] = [
            ((driver.value if isinstance(driver, ErrorDriver) else driver), contribution)
            for driver, contribution in self.compounding_error_attribution
        ]
        return payload


class QuinquennialStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else POSTMORTEM_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quinquennial_reports (
                    report_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    base_year INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, report: QuinquennialReport) -> QuinquennialReport:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM quinquennial_reports WHERE ticker = ? AND base_year = ?",
                (report.ticker, report.base_year),
            )
            conn.execute(
                "INSERT OR REPLACE INTO quinquennial_reports(report_id, ticker, base_year, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    report.report_id,
                    report.ticker,
                    report.base_year,
                    report.created_at.isoformat(),
                    json.dumps(report.to_dict(), default=str),
                ),
            )
            conn.commit()
        return report

    def has_report(self, ticker: str, base_year: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM quinquennial_reports WHERE ticker = ? AND base_year = ? LIMIT 1",
                (ticker, base_year),
            ).fetchone()
        return row is not None


def should_run_quinquennial(years_since_ipo: int) -> bool:
    return years_since_ipo >= 5 and years_since_ipo % 5 == 0


def _safe_pct_error(actual: float | None, predicted: float | None) -> float:
    if actual is None or predicted is None or abs(predicted) < 1e-9:
        return 0.0
    return ((actual - predicted) / predicted) * 100.0


def _postmortem_from_payload(payload: dict[str, Any]) -> PostmortemRecord:
    return PostmortemRecord(
        postmortem_id=str(payload.get("postmortem_id") or ""),
        record_id=str(payload.get("record_id") or ""),
        ticker=str(payload.get("ticker") or ""),
        forecast_horizon_year=int(payload.get("forecast_horizon_year") or 0),
        postmortem_date=_parse_date_value(payload.get("postmortem_date")) or date.today(),
        actual_revenue_mm=payload.get("actual_revenue_mm"),
        actual_ebit_margin=payload.get("actual_ebit_margin"),
        actual_ufcf_mm=payload.get("actual_ufcf_mm"),
        actual_ev_mm=payload.get("actual_ev_mm"),
        actual_price_at_horizon=payload.get("actual_price_at_horizon"),
        revenue_error_pct=float(payload.get("revenue_error_pct") or 0.0),
        margin_error_bps=float(payload.get("margin_error_bps") or 0.0),
        ev_error_pct=float(payload.get("ev_error_pct") or 0.0),
        price_return_error_pct=float(payload.get("price_return_error_pct") or 0.0),
        primary_miss_driver=str(payload.get("primary_miss_driver") or "revenue"),
        surprise_flags=list(payload.get("surprise_flags") or []),
        error_attribution=[tuple(item) for item in (payload.get("error_attribution") or [])],
        structural_break_detected=bool(payload.get("structural_break_detected")),
        model_bias_signal=str(payload.get("model_bias_signal") or "neutral"),
        postmortem_notes=payload.get("postmortem_notes"),
        prediction_snapshot=dict(payload.get("prediction_snapshot") or {}),
        macro_backdrop_at_prediction=dict(payload.get("macro_backdrop_at_prediction") or {}),
        macro_backdrop_at_horizon=dict(payload.get("macro_backdrop_at_horizon") or {}),
        actual_wacc=payload.get("actual_wacc"),
        actual_terminal_growth=payload.get("actual_terminal_growth"),
        realized_outcome_id=payload.get("realized_outcome_id"),
        realized_label_status=str(payload.get("realized_label_status") or "pending"),
        realized_unknown_targets=list(payload.get("realized_unknown_targets") or []),
        horizon_target_date=_parse_date_value(payload.get("horizon_target_date")),
        aligned_period_end=_parse_date_value(payload.get("aligned_period_end")),
        alignment_method=payload.get("alignment_method"),
        realized_source=dict(payload.get("realized_source") or {}),
    )


def _primary_miss_driver(revenue_error_pct: float, margin_error_bps: float, ev_error_pct: float, price_error_pct: float) -> str:
    candidates = {
        "revenue": abs(revenue_error_pct),
        "margin": abs(margin_error_bps) / 100.0,
        "enterprise_value": abs(ev_error_pct),
        "price_return": abs(price_error_pct),
    }
    return max(candidates, key=candidates.get)


def _model_bias_signal(errors: list[float]) -> str:
    if not errors:
        return "neutral"
    mean_error = sum(errors) / len(errors)
    if mean_error <= -10.0:
        return "optimistic"
    if mean_error >= 10.0:
        return "pessimistic"
    return "neutral"


def fetch_actual_financials_for_year(ticker: str, horizon_year: int) -> dict[str, Any]:
    return {
        "actual_revenue_mm": None,
        "actual_ebit_margin": None,
        "actual_ufcf_mm": None,
        "actual_ev_mm": None,
        "actual_price_at_horizon": None,
        "macro_backdrop": {},
        "surprise_flags": [],
        "structural_break_hints": [],
        "unknown_targets": list(REALIZED_VALUE_FIELDS),
        "notes": f"No default actuals provider configured for {ticker} FY{horizon_year}.",
    }


def run_annual_postmortem(
    ticker: str,
    horizon_year: int,
    *,
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    actual_fetcher: Callable[[str, int], dict[str, Any]] | None = None,
    persist: bool = True,
    skip_existing: bool = True,
) -> list[PostmortemRecord]:
    ledger_reader = ledger_reader or LedgerReader()
    ledger_writer = ledger_writer or LedgerWriter(ledger_reader.db_path)
    actual_fetcher = actual_fetcher or fetch_actual_financials_for_year

    predictions = ledger_reader.query(ticker=ticker, horizon_year=horizon_year)
    if not predictions:
        return []

    fallback_actuals = actual_fetcher(ticker, horizon_year)
    bias_errors: list[float] = []
    results: list[PostmortemRecord] = []
    existing_record_ids: set[str] = set()
    if persist and skip_existing:
        for prediction in predictions:
            if ledger_reader.query_postmortems(record_id=prediction.record_id):
                existing_record_ids.add(prediction.record_id)

    for prediction in predictions:
        if prediction.record_id in existing_record_ids:
            continue

        actuals = _resolve_actuals_for_prediction(
            prediction,
            ledger_reader=ledger_reader,
            fallback_actuals=fallback_actuals,
        )
        if not _has_any_actual_labels(actuals):
            continue

        revenue_error_pct = _safe_pct_error(actuals.get("actual_revenue_mm"), prediction.predicted_revenue_mm)
        actual_margin = actuals.get("actual_ebit_margin")
        margin_error_bps = 0.0 if actual_margin is None else (actual_margin - prediction.predicted_ebit_margin) * 10_000
        ev_error_pct = _safe_pct_error(actuals.get("actual_ev_mm"), prediction.predicted_ev_mm)
        predicted_return_pct = _safe_pct_error(prediction.predicted_price_per_share, prediction.actual_price_at_prediction)
        actual_return_pct = _safe_pct_error(actuals.get("actual_price_at_horizon"), prediction.actual_price_at_prediction)
        price_return_error_pct = actual_return_pct - predicted_return_pct

        bias_errors.append(ev_error_pct)
        structural_break_hints = list(actuals.get("structural_break_hints") or [])
        # BRAIN_IMPROVEMENT_PLAN.md (H4) — graded structural break score instead of binary flag.
        # Scales 0.0 (clean) to 1.0 (severe break); hint keywords each add 0.15.
        structural_break_score = min(
            1.0,
            abs(revenue_error_pct) / 50.0 + len(structural_break_hints) * 0.15
        )
        structural_break = structural_break_score >= 0.50 or bool(structural_break_hints)
        surprise_flags = list(actuals.get("surprise_flags") or [])
        if structural_break and "structural break candidate" not in {flag.lower() for flag in surprise_flags}:
            surprise_flags.append("structural break candidate")

        record = PostmortemRecord(
            postmortem_id=str(uuid.uuid4()),
            record_id=prediction.record_id,
            ticker=prediction.ticker,
            forecast_horizon_year=prediction.forecast_horizon_year,
            postmortem_date=_parse_date_value(actuals.get("label_as_of_date")) or date.today(),
            actual_revenue_mm=actuals.get("actual_revenue_mm"),
            actual_ebit_margin=actual_margin,
            actual_ufcf_mm=actuals.get("actual_ufcf_mm"),
            actual_ev_mm=actuals.get("actual_ev_mm"),
            actual_price_at_horizon=actuals.get("actual_price_at_horizon"),
            revenue_error_pct=revenue_error_pct,
            margin_error_bps=margin_error_bps,
            ev_error_pct=ev_error_pct,
            price_return_error_pct=price_return_error_pct,
            primary_miss_driver=_primary_miss_driver(revenue_error_pct, margin_error_bps, ev_error_pct, price_return_error_pct),
            surprise_flags=surprise_flags,
            structural_break_detected=structural_break,
            structural_break_score=structural_break_score,
            model_bias_signal="neutral",
            postmortem_notes=actuals.get("notes"),
            prediction_snapshot={
                "predicted_revenue_mm": prediction.predicted_revenue_mm,
                "predicted_ebit_margin": prediction.predicted_ebit_margin,
                "predicted_ev_mm": prediction.predicted_ev_mm,
                "predicted_price_per_share": prediction.predicted_price_per_share,
                "predicted_wacc": prediction.predicted_wacc,
                "predicted_terminal_growth": prediction.predicted_terminal_growth,
                "prediction_timestamp": prediction.prediction_timestamp,
                "horizon_target_date": prediction_horizon_target_date(prediction).isoformat(),
            },
            macro_backdrop_at_prediction=dict(prediction.macro_backdrop or {}),
            macro_backdrop_at_horizon=dict(actuals.get("macro_backdrop") or {}),
            actual_wacc=actuals.get("actual_wacc"),
            actual_terminal_growth=actuals.get("actual_terminal_growth"),
            realized_outcome_id=actuals.get("realized_outcome_id"),
            realized_label_status=str(actuals.get("realized_label_status") or "pending"),
            realized_unknown_targets=list(actuals.get("unknown_targets") or []),
            horizon_target_date=prediction_horizon_target_date(prediction),
            aligned_period_end=_parse_date_value(actuals.get("aligned_period_end")),
            alignment_method=actuals.get("alignment_method"),
            realized_source=dict(actuals.get("realized_source") or {}),
        )
        results.append(record)

    bias_signal = _model_bias_signal(bias_errors)
    bias_history = [item.ev_error_pct for item in results]
    final_results: list[PostmortemRecord] = []
    for record in results:
        attributions = attribute_postmortem(record, bias_history=bias_history)
        enriched = PostmortemRecord(
            **{
                **asdict(record),
                "model_bias_signal": bias_signal,
                "error_attribution": attributions,
            }
        )
        final_results.append(enriched)
        if persist:
            ledger_writer.append_postmortem(enriched)
    return final_results


def run_5year_postmortem(
    ticker: str,
    base_year: int,
    *,
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    actual_fetcher: Callable[[str, int], dict[str, Any]] | None = None,
    analog_provider: Callable[[str, int], dict[str, Any]] | None = None,
    report_store: QuinquennialStore | None = None,
) -> QuinquennialReport:
    ledger_reader = ledger_reader or LedgerReader()
    ledger_writer = ledger_writer or LedgerWriter(ledger_reader.db_path)
    actual_fetcher = actual_fetcher or fetch_actual_financials_for_year
    report_store = report_store or QuinquennialStore()

    annual_records: list[PostmortemRecord] = []
    for year in range(base_year + 1, base_year + 6):
        created = run_annual_postmortem(
            ticker,
            year,
            ledger_reader=ledger_reader,
            ledger_writer=ledger_writer,
            actual_fetcher=actual_fetcher,
            persist=True,
            skip_existing=True,
        )
        if created:
            annual_records.extend(created)
            continue

        for prediction in ledger_reader.query(ticker=ticker, horizon_year=year, scenario="base"):
            payloads = ledger_reader.query_postmortems(record_id=prediction.record_id)
            if payloads:
                annual_records.append(_postmortem_from_payload(payloads[0]))

    ordered = sorted(annual_records, key=lambda item: item.forecast_horizon_year)
    predicted_revenues = [record.prediction_snapshot.get("predicted_revenue_mm", 0.0) for record in ordered]
    actual_revenues = [record.actual_revenue_mm or 0.0 for record in ordered]
    predicted_margins = [record.prediction_snapshot.get("predicted_ebit_margin", 0.0) for record in ordered]
    actual_margins = [record.actual_ebit_margin or 0.0 for record in ordered]

    def _direction(series: list[float]) -> int:
        if len(series) < 2:
            return 0
        delta = series[-1] - series[0]
        if delta > 0:
            return 1
        if delta < 0:
            return -1
        return 0

    trajectory = {
        "revenue_direction_correct": _direction(predicted_revenues) == _direction(actual_revenues),
        "margin_direction_correct": _direction(predicted_margins) == _direction(actual_margins),
        "predicted_revenue_direction": _direction(predicted_revenues),
        "actual_revenue_direction": _direction(actual_revenues),
        "predicted_margin_direction": _direction(predicted_margins),
        "actual_margin_direction": _direction(actual_margins),
    }

    drift_candidates = {
        "revenue_growth": abs((actual_revenues[-1] if actual_revenues else 0.0) - (predicted_revenues[-1] if predicted_revenues else 0.0)),
        "ebit_margin": abs((actual_margins[-1] if actual_margins else 0.0) - (predicted_margins[-1] if predicted_margins else 0.0)),
    }
    largest_drift = max(drift_candidates, key=drift_candidates.get) if drift_candidates else "revenue_growth"
    assumption_drift = {
        "largest_moved_assumption": largest_drift,
        "drift_magnitudes": drift_candidates,
    }

    structural_breaks = [
        {
            "forecast_horizon_year": record.forecast_horizon_year,
            "revenue_error_pct": record.revenue_error_pct,
            "candidate": record.structural_break_detected,
            "source": record.realized_source,
        }
        for record in ordered
        if record.structural_break_detected
    ]

    cross_industry_comparison = analog_provider(ticker, base_year) if analog_provider else {}
    report = QuinquennialReport(
        report_id=str(uuid.uuid4()),
        ticker=ticker,
        base_year=base_year,
        created_at=date.today(),
        annual_records=ordered,
        trajectory_analysis=trajectory,
        assumption_drift_diagnosis=assumption_drift,
        structural_breaks=structural_breaks,
        compounding_error_attribution=aggregate_attributions(ordered),
        cross_industry_comparison=cross_industry_comparison,
    )
    report_store.save(report)
    return report


def _actuals_from_realized_outcome(outcome: RealizedOutcomeRecord) -> dict[str, Any]:
    return {
        "actual_revenue_mm": outcome.actual_revenue_mm,
        "actual_ebit_margin": outcome.actual_ebit_margin,
        "actual_ufcf_mm": outcome.actual_ufcf_mm,
        "actual_ev_mm": outcome.actual_ev_mm,
        "actual_price_at_horizon": outcome.actual_price_at_horizon,
        "macro_backdrop": dict(outcome.macro_backdrop or {}),
        "surprise_flags": list(outcome.surprise_flags or []),
        "structural_break_hints": list(outcome.structural_break_hints or []),
        "unknown_targets": list(outcome.unknown_targets or []),
        "notes": outcome.evidence_notes,
        "label_as_of_date": outcome.label_as_of_date.isoformat(),
        "aligned_period_end": outcome.aligned_period_end.isoformat() if outcome.aligned_period_end else None,
        "alignment_method": outcome.alignment_method,
        "realized_outcome_id": outcome.outcome_id,
        "realized_label_status": outcome.label_status,
        "realized_source": {
            "source_name": outcome.source_name,
            "source_kind": outcome.source_kind,
            "label_as_of_date": outcome.label_as_of_date.isoformat(),
            "source_payload": dict(outcome.source_payload or {}),
        },
    }


def _resolve_actuals_for_prediction(
    prediction: PredictionRecord,
    *,
    ledger_reader: LedgerReader,
    fallback_actuals: dict[str, Any],
) -> dict[str, Any]:
    resolved = dict(fallback_actuals or {})
    realized_outcome = ledger_reader.get_best_realized_outcome(prediction.record_id, include_partial=True)
    if realized_outcome is None:
        return resolved

    realized_payload = _actuals_from_realized_outcome(realized_outcome)
    for key, value in realized_payload.items():
        if value is None:
            continue
        if isinstance(value, dict) and not value:
            continue
        if isinstance(value, list) and not value:
            continue
        resolved[key] = value

    if "unknown_targets" in realized_payload:
        resolved["unknown_targets"] = list(realized_payload.get("unknown_targets") or resolved.get("unknown_targets") or [])
    return resolved


def _has_any_actual_labels(actuals: dict[str, Any]) -> bool:
    return any(actuals.get(key) is not None for key in REALIZED_VALUE_FIELDS)


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
