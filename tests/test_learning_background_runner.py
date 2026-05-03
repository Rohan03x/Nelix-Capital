from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

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


def test_background_cycle_runs_bootstrap_and_maintenance(monkeypatch):
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

    payload = run_background_learning_cycle(fundamentals_provider=lambda _ticker: {})

    assert calls == ["bootstrap", "maintenance"]
    assert payload["enabled"] is True
    assert payload["bootstrap"]["ran"] is True
    assert payload["maintenance"]["ran"] is True