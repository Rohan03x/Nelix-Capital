"""Append-only prediction ledger backed by SQLite and JSONL exports."""

from __future__ import annotations

import calendar
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .storage_paths import learning_db_dir, learning_ledger_dir


SCHEMA_VERSION = 2
PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_DIR = learning_db_dir()
DEFAULT_EXPORT_DIR = learning_ledger_dir()
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "predictions.db"
DEFAULT_FISCAL_YEAR_END_MONTH = 12
DEFAULT_FISCAL_YEAR_END_DAY = 31
REALIZED_VALUE_FIELDS = (
    "actual_revenue_mm",
    "actual_ebit_margin",
    "actual_ufcf_mm",
    "actual_price_at_horizon",
)


def _today() -> date:
    return date.today()


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _month_end_day(year: int, month: int) -> int:
    month = min(max(int(month or DEFAULT_FISCAL_YEAR_END_MONTH), 1), 12)
    year = max(int(year or _today().year), 1)
    return calendar.monthrange(year, month)[1]


def fiscal_year_end_date(year: int, month: int = DEFAULT_FISCAL_YEAR_END_MONTH, day: int = DEFAULT_FISCAL_YEAR_END_DAY) -> date:
    if year <= 0:
        return _today()
    safe_month = min(max(int(month or DEFAULT_FISCAL_YEAR_END_MONTH), 1), 12)
    safe_day = min(max(int(day or DEFAULT_FISCAL_YEAR_END_DAY), 1), _month_end_day(year, safe_month))
    return date(year, safe_month, safe_day)


def resolve_prediction_horizon(
    run_date: date,
    forecast_horizon_year: int,
    *,
    fiscal_year_end_month: int = DEFAULT_FISCAL_YEAR_END_MONTH,
    fiscal_year_end_day: int = DEFAULT_FISCAL_YEAR_END_DAY,
) -> tuple[date | None, int]:
    if forecast_horizon_year <= 0:
        return None, 0
    target_date = fiscal_year_end_date(forecast_horizon_year, fiscal_year_end_month, fiscal_year_end_day)
    horizon_months = max(0, (target_date.year - run_date.year) * 12 + (target_date.month - run_date.month))
    if target_date.day < run_date.day:
        horizon_months = max(0, horizon_months - 1)
    return target_date, horizon_months


def _known_realized_value_count(payload: dict[str, Any]) -> int:
    return sum(1 for key in REALIZED_VALUE_FIELDS if payload.get(key) is not None)


def _derive_unknown_targets(payload: dict[str, Any], explicit_unknown_targets: list[str] | None = None) -> list[str]:
    unknown_targets = list(explicit_unknown_targets or [])
    for key in REALIZED_VALUE_FIELDS:
        if payload.get(key) is None and key not in unknown_targets:
            unknown_targets.append(key)
    return sorted(set(unknown_targets))


def _classify_realized_label_status(payload: dict[str, Any], explicit_unknown_targets: list[str] | None = None) -> str:
    known_value_count = _known_realized_value_count(payload)
    unknown_targets = _derive_unknown_targets(payload, explicit_unknown_targets)
    if known_value_count == 0:
        return "pending"
    if unknown_targets:
        return "partial"
    return "complete"


@dataclass(frozen=True)
class PredictionRecord:
    ticker: str
    company_name: str = ""
    sector: str = ""
    industry: str = ""
    run_date: date = field(default_factory=_today)
    forecast_horizon_year: int = 0
    years_since_ipo: int = 0
    data_vintage_years: int = 0
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    predicted_revenue_mm: float = 0.0
    predicted_ebit_margin: float = 0.0
    predicted_ebit_mm: float = 0.0
    predicted_ufcf_mm: float = 0.0
    predicted_wacc: float = 0.0
    predicted_terminal_growth: float = 0.0
    predicted_ev_mm: float = 0.0
    predicted_equity_value_mm: float = 0.0
    predicted_price_per_share: float = 0.0
    scenario: str = "base"
    near_term_revenue_growth: float = 0.0
    target_ebit_margin: float = 0.0
    da_pct_revenue: float = 0.0
    capex_pct_revenue: float = 0.0
    beta: float = 0.0
    erp: float = 0.0
    rf_rate: float = 0.0
    actual_price_at_prediction: float = 0.0
    actual_ev_at_prediction: float = 0.0
    market_cycle_phase: str = ""
    macro_backdrop: dict[str, Any] = field(default_factory=dict)
    actual_revenue_mm: float | None = None
    actual_ebit_margin: float | None = None
    actual_ufcf_mm: float | None = None
    actual_ev_mm: float | None = None
    actual_price_at_horizon: float | None = None
    postmortem_date: date | None = None
    postmortem_notes: str | None = None
    market_cap_regime: str = ""
    macro_regime: str = ""
    feature_vector: tuple[float, ...] | None = None
    prediction_timestamp: str | None = None
    horizon_target_date: date | None = None
    horizon_label: str = ""
    horizon_months: int = 0
    fiscal_year_end_month: int = DEFAULT_FISCAL_YEAR_END_MONTH
    fiscal_year_end_day: int = DEFAULT_FISCAL_YEAR_END_DAY
    prediction_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.macro_backdrop is None:
            object.__setattr__(self, "macro_backdrop", {})
        if self.prediction_context is None:
            object.__setattr__(self, "prediction_context", {})
        if self.feature_vector is not None and not isinstance(self.feature_vector, tuple):
            object.__setattr__(self, "feature_vector", tuple(self.feature_vector))
        if self.prediction_timestamp is None:
            object.__setattr__(self, "prediction_timestamp", datetime.combine(self.run_date, datetime.min.time()).isoformat())
        if self.forecast_horizon_year > 0:
            target_date, derived_horizon_months = resolve_prediction_horizon(
                self.run_date,
                self.forecast_horizon_year,
                fiscal_year_end_month=self.fiscal_year_end_month,
                fiscal_year_end_day=self.fiscal_year_end_day,
            )
            object.__setattr__(self, "horizon_target_date", target_date)
            if not self.horizon_label:
                object.__setattr__(self, "horizon_label", f"FY{self.forecast_horizon_year}")
            object.__setattr__(self, "horizon_months", derived_horizon_months)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["run_date"] = self.run_date.isoformat()
        payload["postmortem_date"] = self.postmortem_date.isoformat() if self.postmortem_date else None
        payload["horizon_target_date"] = self.horizon_target_date.isoformat() if self.horizon_target_date else None
        payload["feature_vector"] = list(self.feature_vector) if self.feature_vector is not None else None
        return payload


@dataclass(frozen=True)
class RealizedOutcomeRecord:
    record_id: str
    ticker: str
    forecast_horizon_year: int
    horizon_target_date: date
    horizon_label: str
    label_as_of_date: date = field(default_factory=_today)
    aligned_period_end: date | None = None
    alignment_method: str = "fiscal_year_exact"
    source_name: str = "legacy_backfill"
    source_kind: str = "fundamentals"
    actual_revenue_mm: float | None = None
    actual_ebit_margin: float | None = None
    actual_ufcf_mm: float | None = None
    actual_ev_mm: float | None = None
    actual_price_at_horizon: float | None = None
    macro_backdrop: dict[str, Any] = field(default_factory=dict)
    surprise_flags: list[str] = field(default_factory=list)
    structural_break_hints: list[str] = field(default_factory=list)
    unknown_targets: list[str] = field(default_factory=list)
    source_payload: dict[str, Any] = field(default_factory=dict)
    evidence_notes: str | None = None
    label_status: str = ""
    known_target_count: int = 0
    outcome_id: str = ""
    created_at: str | None = None

    def __post_init__(self) -> None:
        if self.macro_backdrop is None:
            object.__setattr__(self, "macro_backdrop", {})
        if self.surprise_flags is None:
            object.__setattr__(self, "surprise_flags", [])
        if self.structural_break_hints is None:
            object.__setattr__(self, "structural_break_hints", [])
        if self.source_payload is None:
            object.__setattr__(self, "source_payload", {})
        if self.aligned_period_end is None:
            object.__setattr__(self, "aligned_period_end", self.horizon_target_date)
        unknown_targets = _derive_unknown_targets(asdict(self), list(self.unknown_targets or []))
        if list(self.unknown_targets or []) != unknown_targets:
            object.__setattr__(self, "unknown_targets", unknown_targets)
        known_target_count = _known_realized_value_count(asdict(self))
        if self.known_target_count != known_target_count:
            object.__setattr__(self, "known_target_count", known_target_count)
        label_status = self.label_status or _classify_realized_label_status(asdict(self), unknown_targets)
        if self.label_status != label_status:
            object.__setattr__(self, "label_status", label_status)
        if not self.outcome_id:
            identity = {
                "record_id": self.record_id,
                "ticker": self.ticker,
                "forecast_horizon_year": self.forecast_horizon_year,
                "horizon_target_date": _iso_date(self.horizon_target_date),
                "horizon_label": self.horizon_label,
                "aligned_period_end": _iso_date(self.aligned_period_end),
                "alignment_method": self.alignment_method,
                "source_name": self.source_name,
                "source_kind": self.source_kind,
                "label_as_of_date": _iso_date(self.label_as_of_date),
                "actual_revenue_mm": self.actual_revenue_mm,
                "actual_ebit_margin": self.actual_ebit_margin,
                "actual_ufcf_mm": self.actual_ufcf_mm,
                "actual_ev_mm": self.actual_ev_mm,
                "actual_price_at_horizon": self.actual_price_at_horizon,
                "macro_backdrop": self.macro_backdrop,
                "surprise_flags": self.surprise_flags,
                "structural_break_hints": self.structural_break_hints,
                "unknown_targets": self.unknown_targets,
                "source_payload": self.source_payload,
                "evidence_notes": self.evidence_notes,
            }
            object.__setattr__(self, "outcome_id", str(uuid.uuid5(uuid.NAMESPACE_URL, _json_dumps(identity))))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["horizon_target_date"] = self.horizon_target_date.isoformat()
        payload["label_as_of_date"] = self.label_as_of_date.isoformat()
        payload["aligned_period_end"] = self.aligned_period_end.isoformat() if self.aligned_period_end else None
        return payload


@dataclass(frozen=True)
class MaintenanceRunRecord:
    run_id: str
    started_at: str
    completed_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PredictionOutcomePair:
    prediction: PredictionRecord
    realized_outcome: RealizedOutcomeRecord | None = None
    postmortems: tuple[dict[str, Any], ...] = ()
    is_matured: bool = False

    @property
    def label_status(self) -> str:
        if self.realized_outcome is None:
            return "pending"
        return self.realized_outcome.label_status


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _row_to_prediction(row: sqlite3.Row) -> PredictionRecord:
    payload = dict(row)
    payload["run_date"] = _parse_date(payload.get("run_date")) or _today()
    payload["postmortem_date"] = _parse_date(payload.get("postmortem_date"))
    payload["horizon_target_date"] = _parse_date(payload.get("horizon_target_date"))
    payload["macro_backdrop"] = _json_loads(payload.pop("macro_backdrop_json", None), {})
    payload["prediction_context"] = _json_loads(payload.pop("prediction_context_json", None), {})
    feature_vector = _json_loads(payload.pop("feature_vector_json", None), None)
    payload["feature_vector"] = tuple(feature_vector) if feature_vector is not None else None
    payload.pop("created_at", None)
    return PredictionRecord(**payload)


def _row_to_realized_outcome(row: sqlite3.Row) -> RealizedOutcomeRecord:
    payload = dict(row)
    payload["horizon_target_date"] = _parse_date(payload.get("horizon_target_date")) or _today()
    payload["label_as_of_date"] = _parse_date(payload.get("label_as_of_date")) or _today()
    payload["aligned_period_end"] = _parse_date(payload.get("aligned_period_end"))
    payload["macro_backdrop"] = _json_loads(payload.pop("macro_backdrop_json", None), {})
    payload["surprise_flags"] = _json_loads(payload.pop("surprise_flags_json", None), [])
    payload["structural_break_hints"] = _json_loads(payload.pop("structural_break_hints_json", None), [])
    payload["unknown_targets"] = _json_loads(payload.pop("unknown_targets_json", None), [])
    payload["source_payload"] = _json_loads(payload.pop("source_payload_json", None), {})
    return RealizedOutcomeRecord(**payload)


def _row_to_maintenance_run(row: sqlite3.Row) -> MaintenanceRunRecord:
    payload = dict(row)
    payload["payload"] = _json_loads(payload.pop("payload_json", None), {})
    return MaintenanceRunRecord(**payload)


def prediction_horizon_target_date(prediction: PredictionRecord) -> date:
    target_date = prediction.horizon_target_date
    if target_date is not None:
        return target_date
    resolved_target_date, _ = resolve_prediction_horizon(
        prediction.run_date,
        prediction.forecast_horizon_year,
        fiscal_year_end_month=prediction.fiscal_year_end_month,
        fiscal_year_end_day=prediction.fiscal_year_end_day,
    )
    return resolved_target_date or prediction.run_date


class _LedgerBase:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, export_dir: str | Path = DEFAULT_EXPORT_DIR) -> None:
        self.db_path = Path(db_path)
        self.export_dir = Path(export_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        return conn

    def _ensure_columns(self, conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
        existing_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, column_definition in columns.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prediction_records (
                    record_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    company_name TEXT,
                    sector TEXT,
                    industry TEXT,
                    run_date TEXT NOT NULL,
                    forecast_horizon_year INTEGER NOT NULL,
                    years_since_ipo INTEGER,
                    data_vintage_years INTEGER,
                    predicted_revenue_mm REAL,
                    predicted_ebit_margin REAL,
                    predicted_ebit_mm REAL,
                    predicted_ufcf_mm REAL,
                    predicted_wacc REAL,
                    predicted_terminal_growth REAL,
                    predicted_ev_mm REAL,
                    predicted_equity_value_mm REAL,
                    predicted_price_per_share REAL,
                    scenario TEXT,
                    near_term_revenue_growth REAL,
                    target_ebit_margin REAL,
                    da_pct_revenue REAL,
                    capex_pct_revenue REAL,
                    beta REAL,
                    erp REAL,
                    rf_rate REAL,
                    actual_price_at_prediction REAL,
                    actual_ev_at_prediction REAL,
                    market_cycle_phase TEXT,
                    macro_backdrop_json TEXT,
                    actual_revenue_mm REAL,
                    actual_ebit_margin REAL,
                    actual_ufcf_mm REAL,
                    actual_ev_mm REAL,
                    actual_price_at_horizon REAL,
                    postmortem_date TEXT,
                    postmortem_notes TEXT,
                    market_cap_regime TEXT,
                    macro_regime TEXT,
                    feature_vector_json TEXT,
                    prediction_timestamp TEXT,
                    horizon_target_date TEXT,
                    horizon_label TEXT,
                    horizon_months INTEGER,
                    fiscal_year_end_month INTEGER,
                    fiscal_year_end_day INTEGER,
                    prediction_context_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_columns(
                conn,
                "prediction_records",
                {
                    "prediction_timestamp": "TEXT",
                    "horizon_target_date": "TEXT",
                    "horizon_label": "TEXT",
                    "horizon_months": "INTEGER",
                    "fiscal_year_end_month": "INTEGER",
                    "fiscal_year_end_day": "INTEGER",
                    "prediction_context_json": "TEXT",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS realized_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    forecast_horizon_year INTEGER NOT NULL,
                    horizon_target_date TEXT NOT NULL,
                    horizon_label TEXT NOT NULL,
                    label_as_of_date TEXT NOT NULL,
                    aligned_period_end TEXT,
                    alignment_method TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    actual_revenue_mm REAL,
                    actual_ebit_margin REAL,
                    actual_ufcf_mm REAL,
                    actual_ev_mm REAL,
                    actual_price_at_horizon REAL,
                    macro_backdrop_json TEXT,
                    surprise_flags_json TEXT,
                    structural_break_hints_json TEXT,
                    unknown_targets_json TEXT,
                    source_payload_json TEXT,
                    evidence_notes TEXT,
                    label_status TEXT NOT NULL,
                    known_target_count INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS postmortem_records (
                    postmortem_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prediction_lookup ON prediction_records (ticker, forecast_horizon_year, scenario)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prediction_horizon_target ON prediction_records (horizon_target_date, ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_realized_outcome_lookup ON realized_outcomes (ticker, horizon_target_date, label_as_of_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_realized_record_lookup ON realized_outcomes (record_id, label_as_of_date, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_run_lookup ON maintenance_runs (completed_at, run_id)")
            conn.commit()


class LedgerWriter(_LedgerBase):
    """Append-only writer for prediction, realized-outcome, and post-mortem records."""

    def append(self, record: PredictionRecord) -> str:
        payload = record.to_dict()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM prediction_records WHERE record_id = ?",
                (record.record_id,),
            ).fetchone()
            if existing:
                raise ValueError(f"Prediction record already exists: {record.record_id}")
            conn.execute(
                """
                INSERT INTO prediction_records (
                    record_id, ticker, company_name, sector, industry, run_date,
                    forecast_horizon_year, years_since_ipo, data_vintage_years,
                    predicted_revenue_mm, predicted_ebit_margin, predicted_ebit_mm,
                    predicted_ufcf_mm, predicted_wacc, predicted_terminal_growth,
                    predicted_ev_mm, predicted_equity_value_mm, predicted_price_per_share,
                    scenario, near_term_revenue_growth, target_ebit_margin,
                    da_pct_revenue, capex_pct_revenue, beta, erp, rf_rate,
                    actual_price_at_prediction, actual_ev_at_prediction, market_cycle_phase,
                    macro_backdrop_json, actual_revenue_mm, actual_ebit_margin,
                    actual_ufcf_mm, actual_ev_mm, actual_price_at_horizon,
                    postmortem_date, postmortem_notes, market_cap_regime, macro_regime,
                    feature_vector_json, prediction_timestamp, horizon_target_date, horizon_label,
                    horizon_months, fiscal_year_end_month, fiscal_year_end_day, prediction_context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["record_id"], payload["ticker"], payload["company_name"], payload["sector"], payload["industry"], payload["run_date"],
                    payload["forecast_horizon_year"], payload["years_since_ipo"], payload["data_vintage_years"],
                    payload["predicted_revenue_mm"], payload["predicted_ebit_margin"], payload["predicted_ebit_mm"],
                    payload["predicted_ufcf_mm"], payload["predicted_wacc"], payload["predicted_terminal_growth"],
                    payload["predicted_ev_mm"], payload["predicted_equity_value_mm"], payload["predicted_price_per_share"],
                    payload["scenario"], payload["near_term_revenue_growth"], payload["target_ebit_margin"],
                    payload["da_pct_revenue"], payload["capex_pct_revenue"], payload["beta"], payload["erp"], payload["rf_rate"],
                    payload["actual_price_at_prediction"], payload["actual_ev_at_prediction"], payload["market_cycle_phase"],
                    _json_dumps(payload["macro_backdrop"]), payload["actual_revenue_mm"], payload["actual_ebit_margin"],
                    payload["actual_ufcf_mm"], payload["actual_ev_mm"], payload["actual_price_at_horizon"],
                    payload["postmortem_date"], payload["postmortem_notes"], payload["market_cap_regime"], payload["macro_regime"],
                    _json_dumps(payload["feature_vector"]), payload["prediction_timestamp"], payload["horizon_target_date"], payload["horizon_label"],
                    payload["horizon_months"], payload["fiscal_year_end_month"], payload["fiscal_year_end_day"], _json_dumps(payload["prediction_context"]),
                ),
            )
            conn.commit()

        export_path = self.export_dir / f"{record.ticker.upper()}.jsonl"
        with export_path.open("a", encoding="utf-8") as handle:
            handle.write(_json_dumps(payload) + "\n")
        return record.record_id

    def append_realized_outcome(self, outcome: RealizedOutcomeRecord) -> bool:
        payload = outcome.to_dict()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO realized_outcomes (
                    outcome_id, record_id, ticker, forecast_horizon_year, horizon_target_date, horizon_label,
                    label_as_of_date, aligned_period_end, alignment_method, source_name, source_kind,
                    actual_revenue_mm, actual_ebit_margin, actual_ufcf_mm, actual_ev_mm, actual_price_at_horizon,
                    macro_backdrop_json, surprise_flags_json, structural_break_hints_json, unknown_targets_json,
                    source_payload_json, evidence_notes, label_status, known_target_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    payload["outcome_id"], payload["record_id"], payload["ticker"], payload["forecast_horizon_year"], payload["horizon_target_date"], payload["horizon_label"],
                    payload["label_as_of_date"], payload["aligned_period_end"], payload["alignment_method"], payload["source_name"], payload["source_kind"],
                    payload["actual_revenue_mm"], payload["actual_ebit_margin"], payload["actual_ufcf_mm"], payload["actual_ev_mm"], payload["actual_price_at_horizon"],
                    _json_dumps(payload["macro_backdrop"]), _json_dumps(payload["surprise_flags"]), _json_dumps(payload["structural_break_hints"]), _json_dumps(payload["unknown_targets"]),
                    _json_dumps(payload["source_payload"]), payload["evidence_notes"], payload["label_status"], payload["known_target_count"], payload.get("created_at"),
                ),
            )
            inserted = cursor.rowcount > 0
            conn.commit()

        if inserted:
            export_path = self.export_dir / f"{outcome.ticker.upper()}_realized.jsonl"
            with export_path.open("a", encoding="utf-8") as handle:
                handle.write(_json_dumps(payload) + "\n")
        return inserted

    def append_maintenance_run(self, payload: dict[str, Any], *, started_at: str, completed_at: str) -> str:
        run_identity = {
            "started_at": started_at,
            "completed_at": completed_at,
            "payload": payload,
        }
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json_dumps(run_identity)))
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO maintenance_runs (run_id, started_at, completed_at, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, started_at, completed_at, _json_dumps(payload)),
            )
            conn.commit()
        if cursor.rowcount > 0:
            export_path = self.export_dir / "maintenance_runs.jsonl"
            with export_path.open("a", encoding="utf-8") as handle:
                handle.write(_json_dumps({"run_id": run_id, **run_identity}) + "\n")
        return run_id

    def append_postmortem(self, record: Any) -> str:
        payload = record.to_dict() if hasattr(record, "to_dict") else asdict(record)
        postmortem_id = payload.get("postmortem_id") or str(uuid.uuid4())
        payload["postmortem_id"] = postmortem_id
        with self._connect() as conn:
            parent = conn.execute(
                "SELECT ticker FROM prediction_records WHERE record_id = ?",
                (payload["record_id"],),
            ).fetchone()
            if not parent:
                raise ValueError(f"Unknown prediction record: {payload['record_id']}")
            conn.execute(
                "INSERT INTO postmortem_records (postmortem_id, record_id, payload_json) VALUES (?, ?, ?)",
                (postmortem_id, payload["record_id"], _json_dumps(payload)),
            )
            conn.commit()

        export_path = self.export_dir / f"{parent['ticker'].upper()}_postmortems.jsonl"
        with export_path.open("a", encoding="utf-8") as handle:
            handle.write(_json_dumps(payload) + "\n")
        return postmortem_id

    def backfill_actuals(
        self,
        record_id: str,
        *,
        actual_revenue_mm: float | None = None,
        actual_ebit_margin: float | None = None,
        actual_ufcf_mm: float | None = None,
        actual_ev_mm: float | None = None,
        actual_price_at_horizon: float | None = None,
        postmortem_date: date | None = None,
        postmortem_notes: str | None = None,
        label_as_of_date: date | None = None,
        aligned_period_end: date | None = None,
        source_name: str = "legacy_backfill",
        source_kind: str = "fundamentals",
        alignment_method: str = "fiscal_year_exact",
        macro_backdrop: dict[str, Any] | None = None,
        surprise_flags: list[str] | None = None,
        structural_break_hints: list[str] | None = None,
        unknown_targets: list[str] | None = None,
        source_payload: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prediction_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown prediction record: {record_id}")

            prediction = _row_to_prediction(row)
            current = dict(row)
            outcome_inserted = self.append_realized_outcome(
                RealizedOutcomeRecord(
                    record_id=prediction.record_id,
                    ticker=prediction.ticker,
                    forecast_horizon_year=prediction.forecast_horizon_year,
                    horizon_target_date=prediction_horizon_target_date(prediction),
                    horizon_label=prediction.horizon_label or f"FY{prediction.forecast_horizon_year}",
                    label_as_of_date=label_as_of_date or _today(),
                    aligned_period_end=aligned_period_end or prediction_horizon_target_date(prediction),
                    alignment_method=alignment_method,
                    source_name=source_name,
                    source_kind=source_kind,
                    actual_revenue_mm=actual_revenue_mm,
                    actual_ebit_margin=actual_ebit_margin,
                    actual_ufcf_mm=actual_ufcf_mm,
                    actual_ev_mm=actual_ev_mm,
                    actual_price_at_horizon=actual_price_at_horizon,
                    macro_backdrop=dict(macro_backdrop or {}),
                    surprise_flags=list(surprise_flags or []),
                    structural_break_hints=list(structural_break_hints or []),
                    unknown_targets=list(unknown_targets or []),
                    source_payload=dict(source_payload or {}),
                    evidence_notes=postmortem_notes,
                )
            )

            updates = {
                "actual_revenue_mm": actual_revenue_mm,
                "actual_ebit_margin": actual_ebit_margin,
                "actual_ufcf_mm": actual_ufcf_mm,
                "actual_ev_mm": actual_ev_mm,
                "actual_price_at_horizon": actual_price_at_horizon,
                "postmortem_date": (postmortem_date or _today()).isoformat() if any(
                    value is not None
                    for value in (
                        actual_revenue_mm,
                        actual_ebit_margin,
                        actual_ufcf_mm,
                        actual_ev_mm,
                        actual_price_at_horizon,
                    )
                ) else None,
                "postmortem_notes": postmortem_notes,
            }

            payload: dict[str, Any] = {}
            legacy_changed = False
            for key, incoming in updates.items():
                existing = current.get(key)
                if existing is None and incoming is not None:
                    payload[key] = incoming
                    legacy_changed = True
                else:
                    payload[key] = existing

            if legacy_changed:
                conn.execute(
                    """
                    UPDATE prediction_records
                    SET actual_revenue_mm = ?,
                        actual_ebit_margin = ?,
                        actual_ufcf_mm = ?,
                        actual_ev_mm = ?,
                        actual_price_at_horizon = ?,
                        postmortem_date = ?,
                        postmortem_notes = ?
                    WHERE record_id = ?
                    """,
                    (
                        payload["actual_revenue_mm"],
                        payload["actual_ebit_margin"],
                        payload["actual_ufcf_mm"],
                        payload["actual_ev_mm"],
                        payload["actual_price_at_horizon"],
                        payload["postmortem_date"],
                        payload["postmortem_notes"],
                        record_id,
                    ),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM prediction_records WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                record = _row_to_prediction(updated)
                export_path = self.export_dir / f"{record.ticker.upper()}.jsonl"
                with export_path.open("a", encoding="utf-8") as handle:
                    handle.write(_json_dumps(record.to_dict()) + "\n")

        return legacy_changed or outcome_inserted


class LedgerReader(_LedgerBase):
    """Query helper for historical prediction and realized records."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, export_dir: str | Path = DEFAULT_EXPORT_DIR) -> None:
        super().__init__(db_path=db_path, export_dir=export_dir)

    def query(
        self,
        ticker: str | None = None,
        horizon_year: int | None = None,
        scenario: str | None = None,
        limit: int | None = None,
    ) -> list[PredictionRecord]:
        where: list[str] = []
        params: list[Any] = []
        if ticker:
            where.append("ticker = ?")
            params.append(ticker)
        if horizon_year is not None:
            where.append("forecast_horizon_year = ?")
            params.append(horizon_year)
        if scenario:
            where.append("scenario = ?")
            params.append(scenario)

        sql = "SELECT * FROM prediction_records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY run_date DESC, record_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_prediction(row) for row in rows]

    def query_realized_outcomes(
        self,
        *,
        ticker: str | None = None,
        record_id: str | None = None,
        label_status: str | None = None,
        limit: int | None = None,
    ) -> list[RealizedOutcomeRecord]:
        where: list[str] = []
        params: list[Any] = []
        if ticker:
            where.append("ticker = ?")
            params.append(ticker)
        if record_id:
            where.append("record_id = ?")
            params.append(record_id)
        if label_status:
            where.append("label_status = ?")
            params.append(label_status)

        sql = "SELECT * FROM realized_outcomes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY label_as_of_date DESC, known_target_count DESC, created_at DESC, outcome_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_realized_outcome(row) for row in rows]

    def query_maintenance_runs(self, limit: int | None = None) -> list[MaintenanceRunRecord]:
        sql = "SELECT * FROM maintenance_runs ORDER BY completed_at DESC, run_id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_maintenance_run(row) for row in rows]

    def get_best_realized_outcome(
        self,
        record_id: str,
        *,
        as_of_date: date | None = None,
        include_partial: bool = True,
    ) -> RealizedOutcomeRecord | None:
        cutoff = as_of_date or _today()
        candidates = []
        for outcome in self.query_realized_outcomes(record_id=record_id):
            if outcome.label_as_of_date > cutoff:
                continue
            if not include_partial and outcome.label_status != "complete":
                continue
            candidates.append(outcome)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.known_target_count,
                item.label_status == "complete",
                item.label_as_of_date,
                item.created_at or "",
                item.outcome_id,
            ),
        )

    def query_aligned_pairs(
        self,
        *,
        ticker: str | None = None,
        scenario: str | None = "base",
        as_of_date: date | None = None,
        include_partial: bool = True,
        matured_only: bool = True,
        require_realized: bool = True,
        include_postmortems: bool = False,
        limit: int | None = None,
    ) -> list[PredictionOutcomePair]:
        cutoff = as_of_date or _today()
        predictions = self.query(ticker=ticker, scenario=scenario, limit=limit)
        aligned_pairs: list[PredictionOutcomePair] = []
        for prediction in predictions:
            target_date = prediction_horizon_target_date(prediction)
            is_matured = target_date <= cutoff
            if matured_only and not is_matured:
                continue
            realized_outcome = self.get_best_realized_outcome(
                prediction.record_id,
                as_of_date=cutoff,
                include_partial=include_partial,
            )
            if require_realized and realized_outcome is None:
                continue
            postmortems = tuple(self.query_postmortems(record_id=prediction.record_id)) if include_postmortems else ()
            aligned_pairs.append(
                PredictionOutcomePair(
                    prediction=prediction,
                    realized_outcome=realized_outcome,
                    postmortems=postmortems,
                    is_matured=is_matured,
                )
            )
        return aligned_pairs

    def query_postmortems(self, record_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload_json FROM postmortem_records"
        params: tuple[Any, ...] = ()
        if record_id:
            sql += " WHERE record_id = ?"
            params = (record_id,)
        sql += " ORDER BY created_at DESC, postmortem_id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_json_loads(row["payload_json"], {}) for row in rows]

    def export_records(self, records: Iterable[PredictionRecord]) -> list[dict[str, Any]]:
        return [record.to_dict() for record in records]
