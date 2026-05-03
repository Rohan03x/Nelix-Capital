from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auto_valuation.learning.postmortem import PostmortemRecord
from auto_valuation.learning.ledger import LedgerWriter
from auto_valuation.learning.industry_taxonomy import industry_similarity, resolve_industry_taxonomy
from auto_valuation.learning.universe import SymbolUniverseStore


def _disable_calibration_priority(monkeypatch):
    monkeypatch.setattr(
        "auto_valuation.learning.universe.build_calibration_priority_index",
        lambda: {"ticker": {}, "sector": {}, "industry": {}},
    )
    monkeypatch.setattr(
        "auto_valuation.learning.universe.calibration_priority_for_symbol",
        lambda row, index: {
            "score": 0.0,
            "mode": "none",
            "direct_samples": 0,
            "cohort_samples": 0,
            "mean_abs_error_pct": 0.0,
            "structural_break_rate": 0.0,
            "note": "disabled for isolated test",
        },
    )


def test_symbol_universe_tracks_sources_and_recent_valuation_priority(tmp_path, monkeypatch):
    _disable_calibration_priority(monkeypatch)
    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    now = datetime.now(timezone.utc)

    store.upsert_symbol(
        "AAPL",
        company_name="Apple Inc",
        exchange="US",
        country="USA",
        sector="Technology",
        industry="Consumer Electronics",
        source="dashboard-live",
        valued=True,
        fundamentals_cached=True,
        seen_at=now - timedelta(hours=2),
    )
    store.upsert_symbol(
        "RIO.LSE",
        company_name="Rio Tinto",
        exchange="LSE",
        country="UK",
        sector="Materials",
        industry="Metals & Mining",
        source="ticker-search",
        searched=True,
        fundamentals_cached=True,
        seen_at=now - timedelta(days=4),
    )
    store.upsert_symbol(
        "AAPL",
        source="relationship-graph",
        bootstrapped=True,
        bootstrap_status="realized",
        fundamentals_cached=True,
        seen_at=now - timedelta(hours=1),
    )

    symbol = store.get_symbol("AAPL")
    summary = store.summary(stale_after_hours=12, recent_days=7)
    priorities = store.priority_tickers(limit=2, stale_after_hours=12)

    assert symbol is not None
    assert symbol["company_name"] == "Apple Inc"
    assert set(symbol["sources"]) == {"dashboard-live", "relationship-graph"}
    assert symbol["valuation_hits"] == 1
    assert symbol["bootstrap_runs"] == 1
    assert summary["tracked_symbols"] == 2
    assert summary["sector_span"] == 2
    assert summary["bootstrapped_symbols"] == 1
    assert summary["recently_valued_symbols"] == 1
    assert priorities[0] == "RIO.LSE"


def test_symbol_universe_record_candidates_keeps_sector_diversity_in_priority_order(tmp_path, monkeypatch):
    _disable_calibration_priority(monkeypatch)
    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store.record_candidates(
        [
            {"ticker": "MSFT", "sector": "Technology", "exchange": "US", "name": "Microsoft", "score": 0.9},
            {"ticker": "SAP", "sector": "Technology", "exchange": "XETRA", "name": "SAP", "score": 0.82},
            {"ticker": "XOM", "sector": "Energy", "exchange": "US", "name": "Exxon Mobil", "score": 0.78},
            {"ticker": "NESN.SW", "sector": "Consumer Staples", "exchange": "SW", "name": "Nestle", "score": 0.76},
        ],
        source="relationship-graph",
        fundamentals_cached=True,
    )

    priorities = store.priority_tickers(limit=3, stale_after_hours=12)

    assert priorities[0] in {"MSFT", "SAP"}
    assert "XOM" in priorities
    assert any(ticker in priorities for ticker in {"NESN.SW", "SAP", "MSFT"})


def test_symbol_universe_priority_rewards_watchlist_and_compare_signals(tmp_path, monkeypatch):
    _disable_calibration_priority(monkeypatch)
    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store.upsert_symbol(
        "TRACK",
        company_name="Tracked Co",
        exchange="US",
        sector="Industrials",
        source="watchlist",
        metadata={"watchlist_active": True},
        metadata_increments={"watchlist_hits": 1, "compare_hits": 1},
    )
    store.upsert_symbol(
        "PLAIN",
        company_name="Plain Co",
        exchange="US",
        sector="Utilities",
        source="ticker-search",
    )

    priorities = store.priority_tickers(limit=2, stale_after_hours=12)

    assert priorities[0] == "TRACK"


def test_symbol_universe_priority_rewards_peer_learning_signal(tmp_path, monkeypatch):
    _disable_calibration_priority(monkeypatch)
    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store.upsert_symbol(
        "LUX1",
        company_name="Luxury One",
        exchange="PA",
        sector="Consumer Cyclical",
        industry="Luxury Goods",
        source="peer-comps",
        metadata={"peer_learning_score": 4.5},
        metadata_increments={"peer_candidate_hits": 4},
    )
    store.upsert_symbol(
        "PLAIN",
        company_name="Plain Co",
        exchange="US",
        sector="Consumer Cyclical",
        industry="Internet Retail",
        source="ticker-search",
    )

    priorities = store.priority_tickers(limit=2, stale_after_hours=12)

    assert priorities[0] == "LUX1"


def test_industry_taxonomy_normalizes_luxury_cluster_and_similarity():
    resolved = resolve_industry_taxonomy("designer brands", "Consumer Cyclical")
    similarity = industry_similarity(
        "Luxury Goods",
        "Apparel Retail",
        subject_sector="Consumer Cyclical",
        candidate_sector="Consumer Cyclical",
    )

    assert resolved["canonical_industry"] == "Luxury Goods"
    assert resolved["industry_cluster"] if "industry_cluster" in resolved else resolved["cluster_id"] == "luxury-goods"
    assert similarity >= 0.6


def test_symbol_universe_priority_can_use_calibration_signal(tmp_path, monkeypatch):
    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store.upsert_symbol("CAL", company_name="Calibrated Co", exchange="US", sector="Technology", source="dashboard-live")
    store.upsert_symbol("RECENT", company_name="Recent Co", exchange="US", sector="Industrials", source="dashboard-live", valued=True)

    monkeypatch.setattr(
        "auto_valuation.learning.universe.build_calibration_priority_index",
        lambda: {"ticker": {}, "sector": {}, "industry": {}},
    )
    monkeypatch.setattr(
        "auto_valuation.learning.universe.calibration_priority_for_symbol",
        lambda row, index: {
            "score": 5.0 if row.get("ticker") == "CAL" else 0.0,
            "mode": "ticker" if row.get("ticker") == "CAL" else "none",
            "direct_samples": 4 if row.get("ticker") == "CAL" else 0,
            "cohort_samples": 0,
            "mean_abs_error_pct": 18.0 if row.get("ticker") == "CAL" else 0.0,
            "structural_break_rate": 0.5 if row.get("ticker") == "CAL" else 0.0,
            "note": "synthetic",
        },
    )

    priorities = store.priority_tickers(limit=2, stale_after_hours=12)
    calibration = store.calibration_priority_candidates(limit=1, stale_after_hours=12)

    assert priorities[0] == "CAL"
    assert calibration[0]["ticker"] == "CAL"


def test_symbol_universe_summary_surfaces_real_calibration_candidates(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)

    from tests.test_learning_spine import _make_prediction_record as _make_prediction_record

    base_prediction = _make_prediction_record()
    writer.append(base_prediction)
    writer.append_postmortem(
        PostmortemRecord(
            postmortem_id="pm-1",
            record_id=base_prediction.record_id,
            ticker=base_prediction.ticker,
            forecast_horizon_year=base_prediction.forecast_horizon_year,
            postmortem_date=base_prediction.run_date,
            actual_revenue_mm=80.0,
            actual_ebit_margin=0.08,
            actual_ufcf_mm=6.0,
            actual_ev_mm=85.0,
            actual_price_at_horizon=8.5,
            revenue_error_pct=-24.0,
            margin_error_bps=-260.0,
            ev_error_pct=-18.0,
            price_return_error_pct=-21.0,
            primary_miss_driver="enterprise_value",
            structural_break_detected=True,
        )
    )

    store = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store.upsert_symbol("ACME", company_name="Acme Corp", exchange="US", sector="Industrials", industry="Industrial", source="dashboard-live")

    import auto_valuation.learning.calibration_priority as calibration_priority

    original_reader = calibration_priority.LedgerReader
    calibration_priority.LedgerReader = lambda: original_reader(db_path=db_path, export_dir=export_dir)
    try:
        summary = store.summary(stale_after_hours=12, recent_days=7)
    finally:
        calibration_priority.LedgerReader = original_reader

    assert summary["calibration_priority_candidates"][0]["ticker"] == "ACME"