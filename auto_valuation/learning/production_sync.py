from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._layered_calibrator import CALIBRATION_DB_PATH, CalibrationStore
from .background_runner import BACKGROUND_RUNNER_STATE_PATH
from .discovery import DISCOVERY_DB_PATH, DiscoveryStore
from .historical_replay import _OBS_DISK_CACHE_PATH
from .ledger import DEFAULT_DB_PATH, DEFAULT_EXPORT_DIR, LedgerReader
from .maintenance import MAINTENANCE_STATE_PATH
from .postmortem import POSTMORTEM_DB_PATH, QuinquennialStore
from .storage_paths import PACKAGE_ROOT, learning_db_dir
from .universe import SYMBOL_UNIVERSE_DB_PATH, SymbolUniverseStore


logger = logging.getLogger(__name__)

_SNAPSHOT_TABLE = "learning_state_snapshots"
_SYNC_LOCK = threading.Lock()
_LAST_HYDRATE_AT = 0.0
_HYDRATE_TTL_SEC = 30.0

# Key under which Vercel cron runs upload new prediction records as JSONL lines
# so that the next local sync can merge them into the main predictions.db.
_LEDGER_DELTA_R2_KEY = "brain/db/ledger_delta.jsonl"
# Safety cap: if accumulated delta exceeds this size, skip uploading more
_LEDGER_DELTA_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def _export_ledger_rows_as_jsonl(db_path: Path) -> bytes:
    """Return all prediction_records in *db_path* serialised as JSONL bytes."""
    if not db_path.exists():
        return b""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM prediction_records").fetchall()
            if not rows:
                return b""
            return b"\n".join(json.dumps(dict(row)).encode("utf-8") for row in rows)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Ledger delta export failed: %s", exc)
        return b""


def _import_ledger_rows_from_jsonl(db_path: Path, data: bytes) -> int:
    """INSERT OR IGNORE JSONL prediction rows into *db_path*. Returns imported count."""
    if not data or not db_path.exists():
        return 0
    lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        return 0
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return 0
    # Ensure the schema exists
    try:
        LedgerReader(db_path=db_path, export_dir=DEFAULT_EXPORT_DIR)
    except Exception:
        pass
    try:
        conn = sqlite3.connect(db_path)
        try:
            schema_cols = [r[1] for r in conn.execute("PRAGMA table_info(prediction_records)").fetchall()]
            if not schema_cols:
                return 0
            placeholders = ", ".join("?" for _ in schema_cols)
            sql = f"INSERT OR IGNORE INTO prediction_records ({', '.join(schema_cols)}) VALUES ({placeholders})"
            count = 0
            for row in rows:
                try:
                    conn.execute(sql, tuple(row.get(col) for col in schema_cols))
                    count += 1
                except Exception:
                    pass
            conn.commit()
            return count
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Ledger delta import failed: %s", exc)
        return 0
_LAST_PERSIST_AT = 0.0
_PERSIST_MIN_INTERVAL_SEC = 300.0  # max 1 push per 5 minutes (unless force=True)
_LAST_PERSIST_RESULT: dict[str, Any] = {}
_SYNC_CHUNK_ROWS_ENV = "LEARNING_SYNC_CHUNK_ROWS"
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
    return bool(_dsn() or _r2_storage_enabled() or _supabase_storage_configs())


def _r2_storage_enabled() -> bool:
    try:
        from .r2_store import r2_enabled

        return bool(r2_enabled())
    except Exception:
        return False


def _storage_backend_names() -> list[str]:
    names: list[str] = []
    if _prefer_object_storage():
        names.append("object-storage-primary")
    if _dsn():
        names.append("postgres")
    if _r2_storage_enabled():
        names.append("cloudflare-r2")
    if _supabase_storage_configs():
        names.append("supabase-storage")
    return names


def _sync_chunk_rows() -> int:
    try:
        return max(int(str(os.environ.get(_SYNC_CHUNK_ROWS_ENV) or "500").strip()), 1)
    except (TypeError, ValueError):
        return 500


def _truthy_env(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _prefer_object_storage() -> bool:
    return _r2_storage_enabled() and _truthy_env("LEARNING_SYNC_PREFER_OBJECT_STORAGE", True)


def _r2_raw_component_sync_enabled() -> bool:
    return _r2_storage_enabled() and _truthy_env("LEARNING_R2_RAW_COMPONENT_SYNC", True)


def _supabase_storage_configs() -> list[dict[str, str]]:
    url = str(os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        return []
    bucket = str(os.environ.get("LEARNING_SNAPSHOT_BUCKET") or "learning-state").strip() or "learning-state"
    keys = [
        str(os.environ.get("SUPABASE_SECRET_KEY") or "").strip(),
        str(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
    ]
    configs: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        configs.append({"url": url, "key": key, "bucket": bucket})
    return configs


def _supabase_storage_config() -> dict[str, str]:
    configs = _supabase_storage_configs()
    return configs[0] if configs else {}


def _supabase_storage_headers(config: dict[str, str], *, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": config["key"],
        "Authorization": f"Bearer {config['key']}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _storage_error_message(response: Any) -> str:
    try:
        body = str(response.text or "").strip()
    except Exception:
        body = ""
    if len(body) > 500:
        body = body[:500] + "..."
    return f"{response.status_code} {response.reason}: {body}".strip()


def _ensure_supabase_storage_bucket(config: dict[str, str]) -> bool:
    import requests

    bucket_url = f"{config['url']}/storage/v1/bucket/{config['bucket']}"
    lookup = requests.get(
        bucket_url,
        headers=_supabase_storage_headers(config),
        timeout=15,
    )
    if lookup.status_code == 200:
        return True

    response = requests.post(
        f"{config['url']}/storage/v1/bucket",
        headers=_supabase_storage_headers(config, content_type="application/json"),
        json={"id": config["bucket"], "name": config["bucket"], "public": False},
        timeout=15,
    )
    if response.status_code in {200, 201, 409}:
        return True
    logger.warning("Supabase learning snapshot bucket ensure failed: %s", _storage_error_message(response))
    return False


def _load_storage_snapshot(namespace: str) -> dict[str, Any] | None:
    r2_payload = _load_r2_snapshot(namespace)
    if r2_payload is not None:
        return r2_payload

    import requests

    configs = _supabase_storage_configs()
    if not configs:
        return None
    object_name = f"{namespace}.json"
    errors: list[str] = []
    for config in configs:
        response = requests.get(
            f"{config['url']}/storage/v1/object/{config['bucket']}/{object_name}",
            headers=_supabase_storage_headers(config),
            timeout=30,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            errors.append(_storage_error_message(response))
            continue
        payload = response.json()
        return dict(payload or {}) if isinstance(payload, dict) else None
    if errors:
        raise RuntimeError(f"Supabase storage snapshot load failed for {namespace}: {'; '.join(errors)}")
    return None


def _save_storage_snapshot(namespace: str, payload: dict[str, Any]) -> bool:
    if _save_r2_snapshot(namespace, payload):
        return True

    import requests

    configs = _supabase_storage_configs()
    if not configs:
        return False
    encoded = json.dumps(payload, default=str).encode("utf-8")
    object_name = f"{namespace}.json"
    errors: list[str] = []
    for config in configs:
        object_url = f"{config['url']}/storage/v1/object/{config['bucket']}/{object_name}"
        headers = {
            **_supabase_storage_headers(config, content_type="application/json"),
            "x-upsert": "true",
        }
        response = requests.post(object_url, headers=headers, data=encoded, timeout=60)
        if response.status_code in {200, 201}:
            return True
        _ensure_supabase_storage_bucket(config)
        response = requests.post(object_url, headers=headers, data=encoded, timeout=60)
        if response.status_code in {200, 201}:
            return True
        errors.append(_storage_error_message(response))
    raise RuntimeError(f"Supabase storage snapshot save failed for {namespace}: {'; '.join(errors)}")


def _load_r2_snapshot(namespace: str) -> dict[str, Any] | None:
    try:
        from .r2_store import load_json_object, snapshot_object_key

        payload = load_json_object(snapshot_object_key(namespace))
    except Exception as exc:
        logger.warning("R2 learning snapshot load failed for %s: %s", namespace, exc)
        return None
    return dict(payload or {}) if isinstance(payload, dict) else None


def _save_r2_snapshot(namespace: str, payload: dict[str, Any]) -> bool:
    try:
        from .r2_store import r2_enabled, save_json_object, snapshot_object_key

        if not r2_enabled():
            return False
        return bool(save_json_object(snapshot_object_key(namespace), payload))
    except Exception as exc:
        logger.warning("R2 learning snapshot save failed for %s: %s", namespace, exc)
        return False


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


def _load_remote_snapshot_raw(namespace: str) -> dict[str, Any] | None:
    if _prefer_object_storage():
        payload = _load_storage_snapshot(namespace)
        if payload is not None:
            return payload
    try:
        conn = _connect_remote()
        if conn is not None:
            with conn:
                _ensure_remote_schema(conn)
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"SELECT payload_json, updated_at FROM {_SNAPSHOT_TABLE} WHERE namespace = %s",
                        (namespace,),
                    )
                    row = cursor.fetchone()
            if row:
                payload = dict(row[0] or {})
                payload.setdefault("updated_at", row[1].isoformat() if row[1] is not None else None)
                return payload
    except Exception as exc:
        logger.warning("Postgres learning snapshot load failed for %s: %s", namespace, exc)
    return _load_storage_snapshot(namespace)


def _expand_chunked_snapshot(namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("chunked"):
        return payload
    chunks = list(payload.get("chunks") or [])
    tables: dict[str, list[dict[str, Any]]] = {str(table): [] for table in dict(payload.get("tables") or {})}
    for chunk in chunks:
        chunk_namespace = str(chunk.get("namespace") or "").strip()
        table = str(chunk.get("table") or "").strip()
        if not chunk_namespace or not table:
            raise RuntimeError(f"Chunked learning snapshot {namespace} has an invalid chunk manifest")
        chunk_payload = _load_remote_snapshot_raw(chunk_namespace)
        if not chunk_payload:
            raise RuntimeError(f"Chunked learning snapshot {namespace} is missing chunk {chunk_namespace}")
        tables.setdefault(table, []).extend(list(chunk_payload.get("rows") or []))
    expanded = dict(payload)
    expanded.pop("chunked", None)
    expanded.pop("chunks", None)
    expanded["tables"] = tables
    return expanded


def load_remote_snapshot(namespace: str) -> dict[str, Any] | None:
    payload = _load_remote_snapshot_raw(namespace)
    if not payload:
        return None
    return _expand_chunked_snapshot(namespace, payload)


def save_remote_snapshot(namespace: str, payload: dict[str, Any]) -> bool:
    if _prefer_object_storage() and _save_storage_snapshot(namespace, payload):
        return True
    try:
        conn = _connect_remote()
        if conn is not None:
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
    except Exception as exc:
        logger.warning("Postgres learning snapshot save failed for %s: %s", namespace, exc)
    return _save_storage_snapshot(namespace, payload)


def _chunk_namespace(namespace: str, table: str, index: int) -> str:
    safe_table = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in table)
    return f"{namespace}__{safe_table}__{index:04d}"


def _split_sqlite_snapshot(namespace: str, snapshot: dict[str, Any], *, rows_per_chunk: int | None = None) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    chunk_size = max(int(rows_per_chunk or _sync_chunk_rows()), 1)
    tables = dict(snapshot.get("tables") or {})
    total_rows = sum(len(list(rows or [])) for rows in tables.values())
    if total_rows <= chunk_size:
        return snapshot, []

    manifest_tables = {str(table): [] for table in tables}
    manifest_chunks: list[dict[str, Any]] = []
    chunk_payloads: list[tuple[str, dict[str, Any]]] = []
    for table, raw_rows in tables.items():
        rows = list(raw_rows or [])
        for start in range(0, len(rows), chunk_size):
            chunk_rows = rows[start : start + chunk_size]
            chunk_index = len(chunk_payloads)
            chunk_name = _chunk_namespace(namespace, str(table), chunk_index)
            manifest_chunks.append({"namespace": chunk_name, "table": str(table), "row_count": len(chunk_rows)})
            chunk_payloads.append(
                (
                    chunk_name,
                    {
                        "parent_namespace": namespace,
                        "table": str(table),
                        "chunk_index": chunk_index,
                        "rows": chunk_rows,
                        "captured_at": snapshot.get("captured_at") or _utcnow_iso(),
                    },
                )
            )

    manifest = dict(snapshot)
    manifest["tables"] = manifest_tables
    manifest["chunked"] = True
    manifest["chunk_rows"] = chunk_size
    manifest["total_rows"] = total_rows
    manifest["chunks"] = manifest_chunks
    return manifest, chunk_payloads


def _save_sqlite_snapshot(namespace: str, snapshot: dict[str, Any]) -> bool:
    manifest, chunks = _split_sqlite_snapshot(namespace, snapshot)
    for chunk_namespace, chunk_payload in chunks:
        if not save_remote_snapshot(chunk_namespace, chunk_payload):
            return False
    return save_remote_snapshot(namespace, manifest)


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


def _sqlite_sidecar_paths(db_path: Path) -> tuple[Path, Path]:
    return (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for sidecar in _sqlite_sidecar_paths(db_path):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass


def _sqlite_table_count(db_path: Path, tables: tuple[str, ...]) -> int:
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            available_tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            return sum(
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                for table in tables
                if table in available_tables
            )
        finally:
            conn.close()
    except Exception:
        return 0


def _r2_component_object_key(namespace: str, spec: dict[str, Any]) -> str:
    return str(spec.get("r2_key") or f"brain/db/{Path(spec['path']).name}")


def _serverless_runtime() -> bool:
    return any(
        str(os.environ.get(name) or "").strip()
        for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "FUNCTIONS_WORKER_RUNTIME")
    )


def _backup_sqlite_for_upload(source: Path, temp_dir: Path) -> Path:
    backup_path = temp_dir / source.name
    try:
        src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(backup_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return backup_path
    except Exception:
        backup_path.write_bytes(source.read_bytes())
        return backup_path


def _save_r2_raw_component(namespace: str, spec: dict[str, Any]) -> bool:
    if not _r2_raw_component_sync_enabled():
        return False
    from .r2_store import upload_file

    path = Path(spec["path"])
    if not path.exists():
        return False
    object_key = _r2_component_object_key(namespace, spec)
    if spec["kind"] == "sqlite":
        with tempfile.TemporaryDirectory(prefix="nelix-r2-sqlite-") as tmp_name:
            upload_path = _backup_sqlite_for_upload(path, Path(tmp_name))
            return bool(upload_file(object_key, upload_path, content_type="application/vnd.sqlite3"))
    content_type = "application/json" if path.suffix.lower() == ".json" else None
    return bool(upload_file(object_key, path, content_type=content_type))


def _restore_r2_raw_component(namespace: str, spec: dict[str, Any], *, force: bool = False) -> int | None:
    if not _r2_raw_component_sync_enabled():
        return None
    from .r2_store import download_file

    path = Path(spec["path"])
    if path.exists() and path.stat().st_size > 0 and not force and not _serverless_runtime():
        if spec["kind"] == "sqlite":
            existing_rows = _sqlite_table_count(path, tuple(spec["tables"]))
            if existing_rows > 0:
                return existing_rows
        else:
            return 1
    object_key = _r2_component_object_key(namespace, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec["kind"] == "sqlite" and _serverless_runtime():
        _remove_sqlite_sidecars(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if not download_file(object_key, path):
            return None
        return _sqlite_table_count(path, tuple(spec["tables"]))

    with tempfile.TemporaryDirectory(prefix="nelix-r2-restore-") as tmp_name:
        download_path = Path(tmp_name) / path.name
        if not download_file(object_key, download_path):
            return None
        if spec["kind"] == "sqlite":
            _remove_sqlite_sidecars(path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        os.replace(download_path, path)
    if spec["kind"] == "sqlite":
        return _sqlite_table_count(path, tuple(spec["tables"]))
    return 1


def _component_specs() -> dict[str, dict[str, Any]]:
    # On serverless runtimes (Vercel) /tmp is limited to ~512 MB.
    # Skip the two largest components (predictions.db ~235 MB, obs_cache.pkl ~95 MB)
    # that are only needed for background training — Vercel uses the pre-trained PKL instead.
    serverless = _serverless_runtime()
    return {
        "calibration": {
            "kind": "sqlite",
            "path": CALIBRATION_DB_PATH,
            "r2_key": "brain/db/calibration.db",
            "tables": ("calibration_priors",),
            "ensure": lambda: CalibrationStore(db_path=CALIBRATION_DB_PATH),
        },
        "universe": {
            "kind": "sqlite",
            "path": SYMBOL_UNIVERSE_DB_PATH,
            "r2_key": "brain/db/symbol_universe.db",
            "tables": ("symbol_universe",),
            "ensure": lambda: SymbolUniverseStore(db_path=SYMBOL_UNIVERSE_DB_PATH),
        },
        "discovery": {
            "kind": "sqlite",
            "path": DISCOVERY_DB_PATH,
            "r2_key": "brain/db/discovery.db",
            "tables": ("watchlist_items", "search_impressions", "manual_compare_events", "peer_relationships"),
            "ensure": lambda: DiscoveryStore(db_path=DISCOVERY_DB_PATH),
        },
        "quinquennial": {
            "kind": "sqlite",
            "path": POSTMORTEM_DB_PATH,
            "r2_key": "brain/db/postmortems.db",
            "tables": ("quinquennial_reports",),
            "ensure": lambda: QuinquennialStore(db_path=POSTMORTEM_DB_PATH),
        },
        "scenario_outcomes": {
            "kind": "sqlite",
            "path": learning_db_dir() / "scenario_outcomes.db",
            "r2_key": "brain/db/scenario_outcomes.db",
            "tables": ("scenario_outcomes", "scenario_calibration_priors"),
            "ensure": lambda: None,
        },
        "runner_state": {
            "kind": "json",
            "path": BACKGROUND_RUNNER_STATE_PATH,
            "r2_key": "brain/db/background_runner_state.json",
        },
        "maintenance_state": {
            "kind": "json",
            "path": MAINTENANCE_STATE_PATH,
            "r2_key": "brain/db/maintenance_state.json",
        },
        **({} if serverless else {
            "ledger": {
                "kind": "sqlite",
                "path": DEFAULT_DB_PATH,
                "r2_key": "brain/db/predictions.db",
                "tables": ("prediction_records", "realized_outcomes", "postmortem_records", "maintenance_runs"),
                "ensure": lambda: LedgerReader(db_path=DEFAULT_DB_PATH, export_dir=DEFAULT_EXPORT_DIR),
            },
            "historical_observations": {
                "kind": "file",
                "path": _OBS_DISK_CACHE_PATH,
                "r2_key": "brain/db/obs_cache.pkl",
            },
            "spm_model": {
                "kind": "file",
                "path": PACKAGE_ROOT / "data" / "scenario_probability_model.pkl",
                "r2_key": "brain/models/scenario_probability_model.pkl",
            },
            "cagr_models": {
                "kind": "file",
                "path": PACKAGE_ROOT / "data" / "near_term_cagr_models.pkl",
                "r2_key": "brain/models/near_term_cagr_models.pkl",
            },
            "regime_classifier": {
                "kind": "file",
                "path": PACKAGE_ROOT / "data" / "regime_classifier.pkl",
                "r2_key": "brain/models/regime_classifier.pkl",
            },
            "deployment_seed": {
                "kind": "file",
                "path": PACKAGE_ROOT / "data" / "dashboard_learning_seed.json",
                "r2_key": "brain/data/dashboard_learning_seed.json",
            },
            "industry_taxonomy": {
                "kind": "file",
                "path": PACKAGE_ROOT / "data" / "industry_taxonomy.json",
                "r2_key": "brain/data/industry_taxonomy.json",
            },
        }),
    }


def hydrate_external_learning_state(*, force: bool = False) -> dict[str, Any]:
    global _LAST_HYDRATE_AT
    if not external_learning_enabled():
        return {"enabled": False, "reason": "disabled"}
    if not force and (time.monotonic() - _LAST_HYDRATE_AT) < _HYDRATE_TTL_SEC:
        return {"enabled": True, "reason": "cached"}

    restored: dict[str, int] = {}
    errors: dict[str, str] = {}
    with _SYNC_LOCK:
        for namespace, spec in _component_specs().items():
            try:
                if _prefer_object_storage() and _r2_raw_component_sync_enabled():
                    restored_count = _restore_r2_raw_component(namespace, spec, force=force)
                    if restored_count is not None:
                        restored[namespace] = restored_count
                        continue
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
                elif spec["kind"] == "file":
                    continue
                else:
                    restored[namespace] = _restore_json_file(Path(spec["path"]), snapshot)
            except Exception as exc:
                errors[namespace] = str(exc)
        # On non-serverless: merge any pending ledger delta uploaded by Vercel cron runs.
        if not _serverless_runtime() and _r2_raw_component_sync_enabled():
            try:
                from .r2_store import get_object, put_object
                delta_data = get_object(_LEDGER_DELTA_R2_KEY)
                if delta_data:
                    n = _import_ledger_rows_from_jsonl(Path(DEFAULT_DB_PATH), delta_data)
                    if n > 0:
                        restored["ledger_delta"] = n
                        # Clear the delta after a successful merge.
                        put_object(_LEDGER_DELTA_R2_KEY, b"", content_type="application/x-ndjson")
            except Exception as exc:
                errors["ledger_delta"] = str(exc)
        _LAST_HYDRATE_AT = time.monotonic()
    reason = None if not errors else "partial-failure"
    return {"enabled": True, "reason": reason, "restored": restored, "errors": errors}


def persist_external_learning_state(*, force: bool = False) -> dict[str, Any]:
    global _LAST_PERSIST_AT, _LAST_PERSIST_RESULT
    if not external_learning_enabled():
        return {"enabled": False, "reason": "disabled"}
    if not force and (time.monotonic() - _LAST_PERSIST_AT) < _PERSIST_MIN_INTERVAL_SEC:
        return {"enabled": True, "reason": "throttled", **_LAST_PERSIST_RESULT}

    persisted: dict[str, bool] = {}
    with _SYNC_LOCK:
        errors: dict[str, str] = {}
        for namespace, spec in _component_specs().items():
            try:
                if _prefer_object_storage() and _r2_raw_component_sync_enabled():
                    if spec["kind"] == "sqlite":
                        spec["ensure"]()
                    persisted[namespace] = _save_r2_raw_component(namespace, spec)
                    continue
                if spec["kind"] == "sqlite":
                    spec["ensure"]()
                    snapshot = _snapshot_sqlite_db(Path(spec["path"]), tuple(spec["tables"]))
                    persisted[namespace] = _save_sqlite_snapshot(namespace, snapshot)
                elif spec["kind"] == "file":
                    persisted[namespace] = False
                else:
                    snapshot = _snapshot_json_file(Path(spec["path"]))
                    persisted[namespace] = save_remote_snapshot(namespace, snapshot)
            except Exception as exc:
                persisted[namespace] = False
                errors[namespace] = str(exc)
        # On serverless: export new prediction records as a JSONL delta so the
        # next local sync can merge them into the main predictions.db.
        if _serverless_runtime() and _r2_raw_component_sync_enabled():
            try:
                from .r2_store import get_object, put_object
                new_data = _export_ledger_rows_as_jsonl(Path(DEFAULT_DB_PATH))
                if new_data:
                    existing = get_object(_LEDGER_DELTA_R2_KEY) or b""
                    if len(existing) < _LEDGER_DELTA_MAX_BYTES:
                        merged = (existing.rstrip(b"\n") + b"\n" + new_data).lstrip(b"\n") if existing else new_data
                        persisted["ledger_delta"] = bool(
                            put_object(_LEDGER_DELTA_R2_KEY, merged, content_type="application/x-ndjson")
                        )
            except Exception as exc:
                errors["ledger_delta"] = str(exc)
        _LAST_PERSIST_AT = time.monotonic()
        _LAST_PERSIST_RESULT = {"persisted": persisted, "synced_at": _utcnow_iso(), "errors": errors}
    reason = None if all(persisted.values()) else "partial-failure"
    return {"enabled": True, "reason": reason, **_LAST_PERSIST_RESULT}


def get_sync_stats() -> dict[str, Any]:
    """Return sync state for the status endpoint."""
    enabled = external_learning_enabled()
    last_at = _LAST_PERSIST_RESULT.get("synced_at")
    elapsed = time.monotonic() - _LAST_PERSIST_AT if _LAST_PERSIST_AT > 0 else None
    next_in = max(0.0, _PERSIST_MIN_INTERVAL_SEC - (elapsed or _PERSIST_MIN_INTERVAL_SEC))
    return {
        "enabled": enabled,
        "backends": _storage_backend_names(),
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