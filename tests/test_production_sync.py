from __future__ import annotations

import sqlite3

import webapp.app as webapp_module
from auto_valuation.learning import production_sync


_R2_ENV_KEYS = (
    "LEARNING_R2_ACCOUNT_ID",
    "LEARNING_R2_ENDPOINT_URL",
    "LEARNING_R2_ENDPOINT",
    "LEARNING_R2_ACCESS_KEY_ID",
    "LEARNING_R2_SECRET_ACCESS_KEY",
    "LEARNING_R2_BUCKET",
    "LEARNING_R2_PREFIX",
    "R2_ENDPOINT_URL",
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)


def _clear_r2_env(monkeypatch):
    for env_key in _R2_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)


def test_sqlite_snapshot_roundtrip_restores_rows(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO sample(id, payload) VALUES (?, ?)", ("one", '{"ok": true}'))
        conn.commit()

    snapshot = production_sync._snapshot_sqlite_db(db_path, ("sample",))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM sample")
        conn.commit()

    production_sync._restore_sqlite_db(
        db_path,
        ("sample",),
        snapshot,
        ensure_schema=lambda: None,
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, payload FROM sample").fetchall()

    assert rows == [("one", '{"ok": true}')]


def test_dsn_accepts_supabase_aliases(monkeypatch):
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://user:pass@host:5432/postgres?sslmode=require")

    assert production_sync._dsn() == "postgresql://user:pass@host:5432/postgres?sslmode=require"


def test_dsn_prefers_vercel_postgres_before_supabase_direct(monkeypatch):
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://bad:bad@direct.supabase.local:5432/postgres")
    monkeypatch.setenv("POSTGRES_URL", "postgres://good:good@pooler.vercel.local:5432/postgres")

    assert production_sync._dsn() == "postgresql://good:good@pooler.vercel.local:5432/postgres"


def test_dsn_prefers_prisma_pooler_before_regular_postgres(monkeypatch):
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv("POSTGRES_URL", "postgres://direct:pass@direct.vercel.local:5432/postgres")
    monkeypatch.setenv(
        "POSTGRES_PRISMA_URL",
        "postgres://pooler:pass@pooler.vercel.local:5432/postgres?sslmode=require&pgbouncer=true&connection_limit=1",
    )

    assert production_sync._dsn() == "postgresql://pooler:pass@pooler.vercel.local:5432/postgres?sslmode=require"


def test_dsn_strips_supabase_pooler_hint_for_psycopg(monkeypatch):
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv(
        "POSTGRES_URL",
        "postgres://user:pass@pooler.example.com:5432/postgres?sslmode=require&supa=base-pooler.x",
    )

    assert production_sync._dsn() == "postgresql://user:pass@pooler.example.com:5432/postgres?sslmode=require"


def test_component_specs_include_calibration_and_maintenance_state():
    specs = production_sync._component_specs()

    assert specs["calibration"]["kind"] == "sqlite"
    assert specs["calibration"]["tables"] == ("calibration_priors",)
    assert specs["calibration"]["path"].name == "calibration.db"
    assert specs["maintenance_state"]["kind"] == "json"
    assert specs["maintenance_state"]["path"].name == "maintenance_state.json"


def test_external_learning_enabled_with_supabase_storage(monkeypatch):
    _clear_r2_env(monkeypatch)
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    assert production_sync.external_learning_enabled() is True


def test_external_learning_enabled_with_cloudflare_r2(monkeypatch):
    for env_key in production_sync._DSN_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    monkeypatch.setenv("LEARNING_R2_ENDPOINT_URL", "https://account.r2.cloudflarestorage.com")
    monkeypatch.setenv("LEARNING_R2_BUCKET", "learning-state")
    monkeypatch.setenv("LEARNING_R2_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("LEARNING_R2_SECRET_ACCESS_KEY", "secret")

    assert production_sync.external_learning_enabled() is True
    assert "cloudflare-r2" in production_sync.get_sync_stats()["backends"]


def test_storage_snapshot_prefers_r2_when_configured(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_r2_save(namespace, payload):
        captured["namespace"] = namespace
        captured["payload"] = payload
        return True

    monkeypatch.setattr(production_sync, "_save_r2_snapshot", _fake_r2_save)
    monkeypatch.setattr(production_sync, "_supabase_storage_configs", lambda: [])

    assert production_sync._save_storage_snapshot("ledger", {"ok": True}) is True
    assert captured == {"namespace": "ledger", "payload": {"ok": True}}


def test_storage_snapshot_loads_from_r2_before_supabase(monkeypatch):
    monkeypatch.setattr(production_sync, "_load_r2_snapshot", lambda namespace: {"namespace": namespace})
    monkeypatch.setattr(production_sync, "_supabase_storage_configs", lambda: [])

    assert production_sync._load_storage_snapshot("runner_state") == {"namespace": "runner_state"}


def test_save_remote_snapshot_prefers_r2_over_postgres_when_configured(monkeypatch):
    calls: list[str] = []

    def _unexpected_connect():
        calls.append("postgres")
        raise AssertionError("postgres should not be used when R2 is primary")

    monkeypatch.setattr(production_sync, "_r2_storage_enabled", lambda: True)
    monkeypatch.setattr(production_sync, "_save_storage_snapshot", lambda namespace, payload: calls.append(namespace) or True)
    monkeypatch.setattr(production_sync, "_connect_remote", _unexpected_connect)

    assert production_sync.save_remote_snapshot("ledger", {"ok": True}) is True
    assert calls == ["ledger"]


def test_load_remote_snapshot_prefers_r2_over_postgres_when_configured(monkeypatch):
    calls: list[str] = []

    def _unexpected_connect():
        calls.append("postgres")
        raise AssertionError("postgres should not be used when R2 is primary")

    monkeypatch.setattr(production_sync, "_r2_storage_enabled", lambda: True)
    monkeypatch.setattr(production_sync, "_load_storage_snapshot", lambda namespace: calls.append(namespace) or {"namespace": namespace})
    monkeypatch.setattr(production_sync, "_connect_remote", _unexpected_connect)

    assert production_sync.load_remote_snapshot("runner_state") == {"namespace": "runner_state"}
    assert calls == ["runner_state"]


def test_r2_raw_restore_replaces_empty_sqlite_file(monkeypatch, tmp_path):
    local_db = tmp_path / "local.db"
    source_db = tmp_path / "source.db"
    conn = sqlite3.connect(local_db)
    try:
        conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(source_db)
    try:
        conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sample(id) VALUES ('one')")
        conn.commit()
    finally:
        conn.close()

    def _download_file(_key, destination):
        destination.write_bytes(source_db.read_bytes())
        return True

    monkeypatch.setattr(production_sync, "_r2_raw_component_sync_enabled", lambda: True)
    monkeypatch.setattr("auto_valuation.learning.r2_store.download_file", _download_file)

    restored = production_sync._restore_r2_raw_component(
        "sample",
        {"kind": "sqlite", "path": local_db, "tables": ("sample",), "r2_key": "brain/db/sample.db"},
    )

    assert restored == 1
    conn = sqlite3.connect(local_db)
    try:
        assert conn.execute("SELECT id FROM sample").fetchall() == [("one",)]
    finally:
        conn.close()


def test_snapshot_save_falls_back_to_supabase_storage(monkeypatch):
    captured: dict[str, object] = {}

    def _raise_connect():
        raise RuntimeError("db unavailable")

    def _fake_storage_save(namespace, payload):
        captured["namespace"] = namespace
        captured["payload"] = payload
        return True

    monkeypatch.setattr(production_sync, "_connect_remote", _raise_connect)
    monkeypatch.setattr(production_sync, "_save_storage_snapshot", _fake_storage_save)

    assert production_sync.save_remote_snapshot("runner_state", {"ok": True}) is True
    assert captured == {"namespace": "runner_state", "payload": {"ok": True}}


def test_snapshot_load_falls_back_to_supabase_storage(monkeypatch):
    def _raise_connect():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(production_sync, "_connect_remote", _raise_connect)
    monkeypatch.setattr(production_sync, "_load_storage_snapshot", lambda namespace: {"namespace": namespace})

    assert production_sync.load_remote_snapshot("runner_state") == {"namespace": "runner_state"}


def test_chunked_sqlite_snapshot_roundtrip_uses_manifest(monkeypatch):
    stored: dict[str, dict[str, object]] = {}
    snapshot = {
        "db_path": "state.db",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "tables": {"sample": [{"id": "one"}, {"id": "two"}, {"id": "three"}]},
    }

    monkeypatch.setenv("LEARNING_SYNC_CHUNK_ROWS", "2")
    monkeypatch.setattr(production_sync, "_connect_remote", lambda: None)
    monkeypatch.setattr(production_sync, "_save_storage_snapshot", lambda namespace, payload: stored.setdefault(namespace, payload) is payload)
    monkeypatch.setattr(production_sync, "_load_storage_snapshot", lambda namespace: stored.get(namespace))

    assert production_sync._save_sqlite_snapshot("ledger", snapshot) is True
    assert stored["ledger"]["chunked"] is True
    assert [chunk["namespace"] for chunk in stored["ledger"]["chunks"]] == ["ledger__sample__0000", "ledger__sample__0001"]

    restored = production_sync.load_remote_snapshot("ledger")
    assert restored["tables"] == snapshot["tables"]

def test_storage_snapshot_saves_object_without_bucket_create(monkeypatch):
    _clear_r2_env(monkeypatch)
    calls: list[tuple[str, str]] = []

    class Response:
        status_code = 200
        reason = "OK"
        text = "{}"

    class Requests:
        @staticmethod
        def post(url, headers=None, data=None, json=None, timeout=None):
            calls.append(("post", url))
            return Response()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setattr("requests.post", Requests.post)

    assert production_sync._save_storage_snapshot("runner_state", {"ok": True}) is True
    assert calls == [("post", "https://example.supabase.co/storage/v1/object/learning-state/runner_state.json")]


def test_storage_snapshot_creates_bucket_only_when_missing(monkeypatch):
    _clear_r2_env(monkeypatch)
    calls: list[tuple[str, str]] = []

    class Response:
        def __init__(self, status_code, text="{}", reason="OK"):
            self.status_code = status_code
            self.text = text
            self.reason = reason

    def post(url, headers=None, data=None, json=None, timeout=None):
        calls.append(("post", url))
        if "/object/" in url and len([call for call in calls if call == ("post", url)]) == 1:
            return Response(404, '{"message":"Bucket not found"}', "Not Found")
        return Response(200)

    def get(url, headers=None, timeout=None):
        calls.append(("get", url))
        return Response(404, '{"message":"Bucket not found"}', "Not Found")

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)

    assert production_sync._save_storage_snapshot("ledger", {"ok": True}) is True
    assert calls == [
        ("post", "https://example.supabase.co/storage/v1/object/learning-state/ledger.json"),
        ("get", "https://example.supabase.co/storage/v1/bucket/learning-state"),
        ("post", "https://example.supabase.co/storage/v1/bucket"),
        ("post", "https://example.supabase.co/storage/v1/object/learning-state/ledger.json"),
    ]


def test_storage_snapshot_retries_alternate_supabase_key(monkeypatch):
    _clear_r2_env(monkeypatch)
    auth_headers: list[str] = []

    class Response:
        def __init__(self, status_code, text="{}", reason="OK"):
            self.status_code = status_code
            self.text = text
            self.reason = reason

    def post(url, headers=None, data=None, json=None, timeout=None):
        auth_headers.append(headers["Authorization"])
        return Response(200 if headers["Authorization"] == "Bearer service-role" else 500)

    def get(url, headers=None, timeout=None):
        return Response(500)

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "secret-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)

    assert production_sync._save_storage_snapshot("ledger", {"ok": True}) is True
    assert "Bearer secret-key" in auth_headers
    assert "Bearer service-role" in auth_headers


def test_api_internal_learning_cron_runs_cycle_and_sync(monkeypatch):
    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    monkeypatch.setenv("LEARNING_CRON_SECRET", "secret")
    monkeypatch.setattr(webapp_module, "_sync_external_learning_state", lambda force=False: {"ok": True, "force": force})
    monkeypatch.setattr(webapp_module, "_persist_external_learning_state", lambda force=False: {"ok": True, "force": force})
    monkeypatch.setattr(
        "auto_valuation.learning.background_runner.run_background_learning_cycle",
        lambda: {"enabled": True, "bootstrap": {"ran": True}, "maintenance": {"ran": False}},
    )

    with webapp_module.app.test_client() as client:
        response = client.get(
            "/api/internal/learning/cron",
            headers={"Authorization": "Bearer secret"},
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["sync_in"]["force"] is True
    assert payload["sync_out"]["force"] is True
    assert payload["cycle"]["bootstrap"]["ran"] is True