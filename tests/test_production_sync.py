from __future__ import annotations

import sqlite3

import webapp.app as webapp_module
from auto_valuation.learning import production_sync


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