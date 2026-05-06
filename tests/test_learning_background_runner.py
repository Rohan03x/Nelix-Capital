from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import auto_valuation.learning.background_runner as background_runner_module
from auto_valuation.learning.background_runner import run_background_learning_cycle
from auto_valuation.learning.ledger import LedgerReader, LedgerWriter
from auto_valuation.learning.maintenance import run_scheduled_learning_maintenance
from auto_valuation.learning.live_evidence_bootstrap import run_live_evidence_bootstrap
from auto_valuation.learning.postmortem import QuinquennialStore


def _make_prediction_record():
    from tests.test_learning_spine import _make_prediction_record as _factory

    return _factory()


def _make_fundamentals_for_periods(periods):
    from tests.test_learning_spine import _make_fundamentals_for_periods as _factory

    return _factory(periods)


def _make_bootstrap_fundamentals(periods):
    from tests.test_learning_spine import _make_bootstrap_fundamentals as _factory

    return _factory(periods)


def _make_price_history(points):
    from tests.test_learning_spine import _make_price_history as _factory

    return _factory(points)


def test_scheduled_maintenance_uses_dedicated_throttle_state(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    writer.append(_make_prediction_record())

    now_text = datetime.now(timezone.utc).isoformat()
    state_path.write_text(
        json.dumps(
            {
                "last_run_at": now_text,
                "bootstrap_last_run_at": now_text,
                "bootstrap_last_run_id": "bootstrap-1",
            }
        ),
        encoding="utf-8",
    )

    result = run_scheduled_learning_maintenance(
        fundamentals_provider=lambda _ticker: _make_fundamentals_for_periods([("2025-12-31", 108.0, 0.18, 9.8)]),
        ledger_reader=reader,
        ledger_writer=writer,
        state_path=state_path,
        interval_hours=24,
        max_tickers=5,
        as_of_date=date(2026, 2, 1),
    )

    assert result.ran is True
    assert result.reason is None
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_state.get("maintenance_last_run_at")


def test_live_evidence_bootstrap_can_throttle_independently(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)

    state_path.write_text(
        json.dumps(
            {
                "maintenance_last_run_at": datetime.now(timezone.utc).isoformat(),
                "maintenance_last_run_id": "maintenance-1",
                "bootstrap_last_run_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                "bootstrap_last_run_id": "bootstrap-1",
            }
        ),
        encoding="utf-8",
    )

    result = run_live_evidence_bootstrap(
        tickers=["ACME"],
        fundamentals_provider=lambda _ticker: _make_bootstrap_fundamentals(
            [
                {
                    "period_end": "2025-12-31",
                    "filing_date": "2026-01-28",
                    "revenue_mm": 108.0,
                    "ebit_margin": 0.18,
                    "ufcf_mm": 9.8,
                    "shares_mm": 10.0,
                    "net_debt_mm": 18.0,
                }
            ]
        ),
        price_history_provider=lambda _ticker, _start, _end: _make_price_history({"2025-12-30": 11.25}),
        ledger_reader=reader,
        ledger_writer=writer,
        report_store=QuinquennialStore(tmp_path / "quinquennial.db"),
        state_path=state_path,
        interval_hours=6,
        as_of_date=date(2026, 2, 1),
        replay_enabled=False,
    )

    assert result.ran is False
    assert result.reason == "throttled"
    assert result.maintenance_run_id == "bootstrap-1"


def test_background_cycle_runs_bootstrap_and_maintenance(monkeypatch, tmp_path):
    calls: list[str] = []

    class _Result:
        def __init__(self, **payload):
            self._payload = payload

        def to_dict(self):
            return dict(self._payload)

    monkeypatch.setattr(
        "auto_valuation.learning.background_runner.run_live_evidence_bootstrap",
        lambda **kwargs: (calls.append("bootstrap"), _Result(enabled=True, ran=True, reason=None))[1],
    )
    monkeypatch.setattr(
        "auto_valuation.learning.background_runner.run_scheduled_learning_maintenance",
        lambda **kwargs: (calls.append("maintenance"), _Result(enabled=True, ran=True, reason=None))[1],
    )
    monkeypatch.setattr(background_runner_module, "_refresh_background_seed_cache", lambda: {"requested_exchanges": []})
    monkeypatch.setattr(background_runner_module, "_build_background_bootstrap_tickers", lambda limit: ["ACME"])
    monkeypatch.setattr(background_runner_module, "_prefetch_fundamentals_parallel", lambda *args, **kwargs: {})
    monkeypatch.setattr(background_runner_module, "_safe_tracked_symbol_count", lambda: 0)
    monkeypatch.setattr(background_runner_module, "_load_background_seed_tickers", lambda limit: [])
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_replay_enabled", False)

    payload = run_background_learning_cycle(
        fundamentals_provider=lambda _ticker: {},
        state_path=tmp_path / "background-cycle.json",
    )

    assert calls == ["bootstrap", "maintenance"]
    assert payload["enabled"] is True
    assert payload["bootstrap"]["ran"] is True
    assert payload["maintenance"]["ran"] is True


def test_background_cycle_skips_when_learning_store_busy(monkeypatch):
    acquired = background_runner_module._CYCLE_LOCK.acquire(blocking=False)
    assert acquired is True
    try:
        payload = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {})
    finally:
        background_runner_module._CYCLE_LOCK.release()

    assert payload["enabled"] is True
    assert payload["reason"] == "learning-store-busy"
    assert payload["bootstrap"]["ran"] is False
    assert payload["maintenance"]["ran"] is False


def test_background_cycle_returns_skipped_payload_for_sqlite_lock(monkeypatch):
    monkeypatch.setattr(
        background_runner_module,
        "_run_background_learning_cycle",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )

    payload = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {})

    assert payload["enabled"] is True
    assert payload["reason"] == "database-locked"
    assert payload["bootstrap"]["ran"] is False
    assert "database is locked" in payload["error"]


def test_background_cycle_reserves_rotating_seed_slots(monkeypatch, tmp_path):
    captured: list[dict[str, object]] = []

    class _Result:
        def __init__(self, **payload):
            self._payload = payload

        def to_dict(self):
            return dict(self._payload)

    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_bootstrap_max_tickers", 6)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_seed_target_symbols", 1000)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_seed_prefix_per_cycle", 2)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_seed_pool_limit", 10)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_maintenance_max_tickers", 2)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_replay_enabled", False)
    monkeypatch.setattr(background_runner_module, "_safe_tracked_symbol_count", lambda: 25)
    monkeypatch.setattr(background_runner_module, "_load_background_seed_tickers", lambda limit: ["SEED1", "SEED2", "SEED3", "SEED4"])
    monkeypatch.setattr(background_runner_module, "_load_universe_priority_tickers", lambda limit: ["PRI1", "PRI2", "PRI3", "PRI4"])
    monkeypatch.setattr(background_runner_module, "_load_cached_bootstrap_tickers", lambda limit: ["CACHE1", "CACHE2"])
    monkeypatch.setattr(background_runner_module, "_BACKGROUND_SEED_CURSOR", 0)
    monkeypatch.setattr(
        background_runner_module,
        "run_live_evidence_bootstrap",
        lambda **kwargs: (captured.append(kwargs), _Result(enabled=True, ran=True, reason=None))[1],
    )
    monkeypatch.setattr(
        background_runner_module,
        "run_scheduled_learning_maintenance",
        lambda **kwargs: _Result(enabled=True, ran=True, reason=None),
    )

    state_path = tmp_path / "rotating-seed-state.json"
    first = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {}, state_path=state_path)
    second = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {}, state_path=state_path)

    assert captured[0]["tickers"][:4] == ["PRI1", "PRI2", "PRI3", "PRI4"]
    assert captured[0]["tickers"][4:] == ["SEED1", "SEED2"]
    assert captured[1]["tickers"][4:] == ["SEED3", "SEED4"]
    assert first["bootstrap"]["requested_tickers"][4:] == ["SEED1", "SEED2"]
    assert second["bootstrap"]["requested_tickers"][4:] == ["SEED3", "SEED4"]


def test_background_cycle_persists_exchange_refresh_state(tmp_path, monkeypatch):
    state_path = tmp_path / "background-runner.json"

    class _Result:
        def __init__(self, **payload):
            self._payload = payload

        def to_dict(self):
            return dict(self._payload)

    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_bootstrap_max_tickers", 4)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_seed_pool_limit", 12)
    monkeypatch.setitem(background_runner_module.LEARNING_CONFIG, "background_runner_replay_enabled", False)
    monkeypatch.setattr(background_runner_module, "_safe_tracked_symbol_count", lambda: 42)
    monkeypatch.setattr(background_runner_module, "_build_background_bootstrap_tickers", lambda limit: ["PRI1", "SEED1"])
    monkeypatch.setattr(background_runner_module, "_load_background_seed_tickers", lambda limit: ["SEED1", "SEED2", "SEED3"])
    monkeypatch.setattr(
        background_runner_module,
        "_refresh_background_seed_cache",
        lambda: {
            "enabled": True,
            "configured_exchanges": ["US", "LSE", "PA"],
            "requested_exchanges": ["US", "LSE"],
            "fetched_exchanges": ["US"],
            "counts": {"US": 2, "LSE": 1},
            "total_items": 3,
            "enrolled_symbols": 3,
            "cursor": 2,
        },
    )
    monkeypatch.setattr(
        background_runner_module,
        "run_live_evidence_bootstrap",
        lambda **kwargs: _Result(enabled=True, ran=True, reason=None, requested_tickers=kwargs.get("tickers") or []),
    )
    monkeypatch.setattr(
        background_runner_module,
        "run_scheduled_learning_maintenance",
        lambda **kwargs: _Result(enabled=True, ran=False, reason="throttled"),
    )

    payload = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {}, state_path=state_path)
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["seed_refresh"]["requested_exchanges"] == ["US", "LSE"]
    assert payload["state"]["requested_tickers"] == ["PRI1", "SEED1"]
    assert saved_state["requested_exchanges"] == ["US", "LSE"]
    assert saved_state["exchange_discovered_symbols"] == 3
    assert saved_state["exchange_enrolled_symbols"] == 3
    assert saved_state["tracked_symbols"] == 42


def test_background_cycle_uses_bounded_serverless_batch(monkeypatch, tmp_path):
    captured_bootstrap: list[dict[str, object]] = []
    captured_maintenance: list[dict[str, object]] = []

    class _Result:
        def __init__(self, **payload):
            self._payload = payload

        def to_dict(self):
            return dict(self._payload)

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_BACKGROUND_BOOTSTRAP_MAX_TICKERS", "3")
    monkeypatch.setenv("VERCEL_BACKGROUND_MAINTENANCE_MAX_TICKERS", "2")
    monkeypatch.setenv("VERCEL_BACKGROUND_CONCURRENT_WORKERS", "2")
    monkeypatch.setattr(background_runner_module, "_refresh_background_seed_cache", lambda: {"requested_exchanges": []})
    monkeypatch.setattr(
        background_runner_module,
        "_build_background_bootstrap_tickers",
        lambda limit: ["ACME", "BETA", "CHARLIE", "DELTA"][:limit],
    )
    monkeypatch.setattr(background_runner_module, "_load_background_seed_tickers", lambda limit: ["ACME", "BETA"])
    monkeypatch.setattr(background_runner_module, "_safe_tracked_symbol_count", lambda: 7)
    monkeypatch.setattr(
        background_runner_module,
        "run_live_evidence_bootstrap",
        lambda **kwargs: (captured_bootstrap.append(kwargs), _Result(enabled=True, ran=True, reason=None))[1],
    )
    monkeypatch.setattr(
        background_runner_module,
        "run_scheduled_learning_maintenance",
        lambda **kwargs: (captured_maintenance.append(kwargs), _Result(enabled=True, ran=True, reason=None))[1],
    )
    monkeypatch.setattr(
        background_runner_module,
        "run_full_universe_replay",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("serverless cron must not run full replay")),
    )

    payload = run_background_learning_cycle(
        fundamentals_provider=lambda _ticker: {},
        state_path=tmp_path / "serverless_background_state.json",
    )

    assert captured_bootstrap[0]["tickers"] == ["ACME", "BETA", "CHARLIE"]
    assert captured_bootstrap[0]["max_tickers"] == 3
    assert captured_maintenance[0]["max_tickers"] == 2
    assert payload["replay"] == {"enabled": False, "ran": False, "reason": "disabled"}
    assert payload["state"]["tracked_symbols"] == 7


def test_daily_stats_handles_missing_runner_timestamp(monkeypatch):
    monkeypatch.setattr(
        background_runner_module,
        "read_background_runner_state",
        lambda: {"last_run_at": None, "requested_tickers": ["ACME"]},
    )
    monkeypatch.setattr(background_runner_module, "_RUNNER", None)

    stats = background_runner_module.get_daily_stats()

    assert stats["tickers_processed_today"] == 0
    assert stats["last_run_at"] is None