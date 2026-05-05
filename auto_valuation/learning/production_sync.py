from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._layered_calibrator import CALIBRATION_DB_PATH, CalibrationStore
from .background_runner import BACKGROUND_RUNNER_STATE_PATH
from .discovery import DISCOVERY_DB_PATH, DiscoveryStore
from .ledger import DEFAULT_DB_PATH, DEFAULT_EXPORT_DIR, LedgerReader
from .maintenance import MAINTENANCE_STATE_PATH
from .postmortem import POSTMORTEM_DB_PATH, QuinquennialStore
from .universe import SYMBOL_UNIVERSE_DB_PATH, SymbolUniverseStore


logger = logging.getLogger(__name__)

_SNAPSHOT_TABLE = "learning_state_snapshots"
_SYNC_LOCK = threading.Lock()
_LAST_HYDRATE_AT = 0.0
_HYDRATE_TTL_SEC = 30.0
_LAST_PERSIST_AT = 0.0
_PERSIST_MIN_INTERVAL_SEC = 300.0  # max 1 push per 5 minutes (unless force=True)
_LAST_PERSIST_RESULT: dict[str, Any] = {}
_DSN_ENV_KEYS = (
    "LEARNING_STORE_DSN",
    "LEARNING_POSTGRES_DSN",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
    "SUPABASE_POOLER_URL",
    "SUPABASE_DB_URL",
    "SUPABASE_DIRECT_URL",
)

_LIBPQ_URI_QUERY_KEYS = {
    "application_name",
    "channel_binding",
    "connect_timeout",
    "fallback_application_name",
    "gssencmode",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "load_balance_hosts",
    "options",
    "passfile",
    "require_auth",
    "requiressl",
    "service",
    "sslcert",
    "sslcompression",
    "sslcrl",
    "sslcrldir",
    "sslkey",
    "sslmode",
    "sslnegotiation",
    "sslpassword",
    "sslrootcert",
    "sslsni",
    "target_session_attrs",
    "tcp_user_timeout",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_dsn(value: str) -> str:
    dsn = str(value or "").strip()
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    if dsn.startswith(("postgresql://", "postgresql+psycopg://")) and "?" in dsn:
        parts = urlsplit(dsn)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() in _LIBPQ_URI_QUERY_KEYS
        ]
        dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return dsn


def _dsn() -> str:
    for env_key in _DSN_ENV_KEYS:
        value = _normalize_dsn(str(os.environ.get(env_key) or ""))
        if value:
            return value
    return ""


def external_learning_enabled() -> bool:
    return bool(_dsn())


def _connect_remote():
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg
    except Exception as exc:
        logger.warning("External learning store unavailable: psycopg import failed: %s", exc)
        return None
    return psycopg.connect(dsn, autocommit=True)


def _ensure_remote_schema(conn: Any) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_SNAPSHOT_TABLE} (
                namespace TEXT PRIMARY KEY,
                payload_json JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def load_remote_snapshot(namespace: str) -> dict[str, Any] | None:
    conn = _connect_remote()
    if conn is None:
        return None
    with conn:
        _ensure_remote_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT payload_json, updated_at FROM {_SNAPSHOT_TABLE} WHERE namespace = %s",
                (namespace,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    payload = dict(row[0] or {})
    payload.setdefault("updated_at", row[1].isoformat() if row[1] is not None else None)
    return payload


def save_remote_snapshot(namespace: str, payload: dict[str, Any]) -> bool:
    conn = _connect_remote()
    if conn is None:
        return False
    encoded = json.dumps(payload, default=str)
    with conn:
        _ensure_remote_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {_SNAPSHOT_TABLE}(namespace, payload_json, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT(namespace) DO UPDATE
                SET payload_json = EXCLUDED.payload_json,
                    updated_at = NOW()
                """,
                (namespace, encoded),
            )
    return True


def _snapshot_sqlite_db(db_path: Path, tables: tuple[str, ...]) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_path": str(db_path), "tables": {table: [] for table in tables}, "captured_at": _utcnow_iso()}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        available_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        payload = {}
        for table in tables:
            if table not in available_tables:
                payload[table] = []
                continue
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            payload[table] = [dict(row) for row in rows]
    return {"db_path": str(db_path), "tables": payload, "captured_at": _utcnow_iso()}


def _restore_sqlite_db(
    db_path: Path,
    tables: tuple[str, ...],
    snapshot: dict[str, Any],
    ensure_schema: Callable[[], Any],
) -> int:
    ensure_schema()
    rows_restored = 0
    table_payloads = dict(snapshot.get("tables") or {})
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
            rows = list(table_payloads.get(table) or [])
            if not rows:
                continue
            columns = list(rows[0].keys())
            placeholders = ", ".join("?" for _ in columns)
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            for row in rows:
                conn.execute(sql, tuple(row.get(column) for column in columns))
            rows_restored += len(rows)
        conn.commit()
    return rows_restored


def _snapshot_json_file(file_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return {
        "file_path": str(file_path),
        "payload": payload,
        "captured_at": _utcnow_iso(),
    }


def _restore_json_file(file_path: Path, snapshot: dict[str, Any]) -> int:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(snapshot.get("payload") or {})
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 1 if payload else 0


def _component_specs() -> dict[str, dict[str, Any]]:
    return {
        "ledger": {
            "kind": "sqlite",
            "path": DEFAULT_DB_PATH,
            "tables": ("prediction_records", "realized_outcomes", "postmortem_records", "maintenance_runs"),
            "ensure": lambda: LedgerReader(db_path=DEFAULT_DB_PATH, export_dir=DEFAULT_EXPORT_DIR),
        },
        "calibration": {
            "kind": "sqlite",
            "path": CALIBRATION_DB_PATH,
            "tables": ("calibration_priors",),
            "ensure": lambda: CalibrationStore(db_path=CALIBRATION_DB_PATH),
        },
        "universe": {
            "kind": "sqlite",
            "path": SYMBOL_UNIVERSE_DB_PATH,
            "tables": ("symbol_universe",),
            "ensure": lambda: SymbolUniverseStore(db_path=SYMBOL_UNIVERSE_DB_PATH),
        },
        "discovery": {
            "kind": "sqlite",
            "path": DISCOVERY_DB_PATH,
            "tables": ("watchlist_items", "search_impressions", "manual_compare_events", "peer_relationships"),
            "ensure": lambda: DiscoveryStore(db_path=DISCOVERY_DB_PATH),
        },
        "quinquennial": {
            "kind": "sqlite",
            "path": POSTMORTEM_DB_PATH,
            "tables": ("quinquennial_reports",),
            "ensure": lambda: QuinquennialStore(db_path=POSTMORTEM_DB_PATH),
        },
        "runner_state": {
            "kind": "json",
            "path": BACKGROUND_RUNNER_STATE_PATH,
        },
        "maintenance_state": {
            "kind": "json",
            "path": MAINTENANCE_STATE_PATH,
        },
    }


def hydrate_external_learning_state(*, force: bool = False) -> dict[str, Any]:
    global _LAST_HYDRATE_AT
    if not external_learning_enabled():
        return {"enabled": False, "reason": "disabled"}
    if not force and (time.monotonic() - _LAST_HYDRATE_AT) < _HYDRATE_TTL_SEC:
        return {"enabled": True, "reason": "cached"}

    restored: dict[str, int] = {}
    with _SYNC_LOCK:
        for namespace, spec in _component_specs().items():
            snapshot = load_remote_snapshot(namespace)
            if not snapshot:
                continue
            if spec["kind"] == "sqlite":
                restored[namespace] = _restore_sqlite_db(
                    Path(spec["path"]),
                    tuple(spec["tables"]),
                    snapshot,
                    spec["ensure"],
                )
            else:
                restored[namespace] = _restore_json_file(Path(spec["path"]), snapshot)
        _LAST_HYDRATE_AT = time.monotonic()
    return {"enabled": True, "reason": None, "restored": restored}


def persist_external_learning_state(*, force: bool = False) -> dict[str, Any]:
    global _LAST_PERSIST_AT, _LAST_PERSIST_RESULT
    if not external_learning_enabled():
        return {"enabled": False, "reason": "disabled"}
    if not force and (time.monotonic() - _LAST_PERSIST_AT) < _PERSIST_MIN_INTERVAL_SEC:
        return {"enabled": True, "reason": "throttled", **_LAST_PERSIST_RESULT}

    persisted: dict[str, bool] = {}
    with _SYNC_LOCK:
        for namespace, spec in _component_specs().items():
            if spec["kind"] == "sqlite":
                spec["ensure"]()
                snapshot = _snapshot_sqlite_db(Path(spec["path"]), tuple(spec["tables"]))
            else:
                snapshot = _snapshot_json_file(Path(spec["path"]))
            persisted[namespace] = save_remote_snapshot(namespace, snapshot)
        _LAST_PERSIST_AT = time.monotonic()
        _LAST_PERSIST_RESULT = {"persisted": persisted, "synced_at": _utcnow_iso()}
    return {"enabled": True, "reason": None, **_LAST_PERSIST_RESULT}


def get_sync_stats() -> dict[str, Any]:
    """Return sync state for the status endpoint."""
    enabled = external_learning_enabled()
    last_at = _LAST_PERSIST_RESULT.get("synced_at")
    elapsed = time.monotonic() - _LAST_PERSIST_AT if _LAST_PERSIST_AT > 0 else None
    next_in = max(0.0, _PERSIST_MIN_INTERVAL_SEC - (elapsed or _PERSIST_MIN_INTERVAL_SEC))
    return {
        "enabled": enabled,
        "last_synced_at": last_at,
        "next_sync_in_seconds": round(next_in) if enabled else None,
        "namespaces_synced": list((_LAST_PERSIST_RESULT.get("persisted") or {}).keys()),
    }


__all__ = [
    "external_learning_enabled",
    "get_sync_stats",
    "hydrate_external_learning_state",
    "load_remote_snapshot",
    "persist_external_learning_state",
    "save_remote_snapshot",
    "_restore_sqlite_db",
    "_snapshot_sqlite_db",
]