from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auto_valuation.learning.discovery import DiscoveryStore
from auto_valuation.learning.shared_db import reset_shared_brain_engine_cache
from auto_valuation.learning.universe import SymbolUniverseStore


def test_discovery_store_records_search_watchlist_and_compare_signals(tmp_path):
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)

    store.record_search_impression(
        "samsung",
        [
            {"ticker": "005930.KO", "name": "Samsung Electronics Co Ltd", "exchange": "KO", "country": "Korea"},
            {"ticker": "SMSN.LSE", "name": "Samsung Pref", "exchange": "LSE", "country": "UK"},
        ],
        exchange="auto",
        selected_ticker="005930.KO",
    )
    store.add_to_watchlist(
        {
            "ticker": "005930.KO",
            "company_name": "Samsung Electronics Co Ltd",
            "exchange": "KO",
            "sector": "Technology",
            "industry": "Consumer Electronics",
        }
    )
    result = store.record_manual_compare(
        {
            "ticker": "005930.KO",
            "company_name": "Samsung Electronics Co Ltd",
            "exchange": "KO",
            "sector": "Technology",
            "industry": "Consumer Electronics",
        },
        [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc",
                "exchange": "US",
                "sector": "Technology",
                "industry": "Consumer Electronics",
            }
        ],
    )
    auto_peers = store.record_auto_peer_basket(
        {
            "ticker": "005930.KO",
            "company_name": "Samsung Electronics Co Ltd",
            "exchange": "KO",
            "sector": "Technology",
            "industry": "Consumer Electronics",
        },
        [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc",
                "exchange": "US",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "canonical_industry": "Consumer Electronics",
                "industry_family": "consumer-hardware",
                "base_peer_learning_score": 5.4,
                "pair_strength_score": 0.0,
            }
        ],
    )

    samsung = universe.get_symbol("005930.KO")
    apple = universe.get_symbol("AAPL")
    relationship = store.get_peer_relationship("005930.KO", "AAPL")

    assert samsung is not None
    assert samsung["search_hits"] >= 1
    assert samsung["metadata"]["selection_hits"] == 1
    assert samsung["metadata"]["watchlist_hits"] == 1
    assert samsung["metadata"]["watchlist_active"] is True
    assert apple is not None
    assert apple["metadata"]["compare_hits"] == 1
    assert relationship is not None
    assert relationship["manual_compare_hits"] == 1
    assert relationship["auto_peer_hits"] == 1
    assert relationship["pair_strength_score"] > 0
    assert relationship["pair_strength_score_raw"] >= relationship["pair_strength_score"]
    assert relationship["pair_decay_multiplier"] <= 1.0
    assert auto_peers["peer_count"] == 1
    assert result["peer_count"] == 1
    assert store.list_watchlist(limit=5)[0]["ticker"] == "005930.KO"
    assert store.list_manual_compares(subject_ticker="005930.KO", limit=5)[0]["ticker"] == "AAPL"


def test_peer_relationship_scores_decay_with_staleness(tmp_path):
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)

    store.record_auto_peer_basket(
        {
            "ticker": "CDI.PA",
            "company_name": "Christian Dior",
            "exchange": "PA",
            "sector": "Consumer Cyclical",
            "industry": "Luxury Goods",
        },
        [
            {
                "ticker": "RMS.PA",
                "company_name": "Hermes",
                "exchange": "PA",
                "sector": "Consumer Cyclical",
                "industry": "Luxury Goods",
                "base_peer_learning_score": 5.8,
            }
        ],
    )

    fresh = store.get_peer_relationship("CDI.PA", "RMS.PA")
    assert fresh is not None

    stale_at = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    store._execute(
        "UPDATE peer_relationships SET last_seen_at = :last_seen_at WHERE subject_ticker = :subject_ticker AND peer_ticker = :peer_ticker",
        {
            "last_seen_at": stale_at,
            "subject_ticker": "CDI.PA",
            "peer_ticker": "RMS.PA",
        },
    )

    decayed = store.get_peer_relationship("CDI.PA", "RMS.PA")

    assert decayed is not None
    assert decayed["pair_strength_score_raw"] == fresh["pair_strength_score_raw"]
    assert decayed["pair_decay_multiplier"] < fresh["pair_decay_multiplier"]
    assert decayed["pair_strength_score"] < fresh["pair_strength_score"]
    assert decayed["pair_age_days"] >= 179
    assert store.list_peer_relationships(subject_ticker="CDI.PA", limit=1)[0]["peer_ticker"] == "RMS.PA"


def test_watchlist_device_replay_does_not_double_count_hits(tmp_path):
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)

    store.add_to_watchlist(
        {
            "ticker": "AAPL",
            "company_name": "Apple Inc",
            "exchange": "US",
            "sector": "Technology",
            "industry": "Consumer Electronics",
        }
    )
    store.add_to_watchlist(
        {
            "ticker": "AAPL",
            "company_name": "Apple Inc",
            "exchange": "US",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "_device_sync_mode": "client-replay",
        }
    )

    symbol = universe.get_symbol("AAPL")

    assert symbol is not None
    assert symbol["metadata"]["watchlist_hits"] == 1


def test_manual_compare_event_id_is_idempotent(tmp_path):
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    store = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)

    payload = {
        "ticker": "005930.KO",
        "company_name": "Samsung Electronics Co Ltd",
        "exchange": "KO",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
    peer = {
        "ticker": "AAPL",
        "company_name": "Apple Inc",
        "exchange": "US",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }

    store.record_manual_compare(payload, [peer], event_id="device:005930.KO:AAPL")
    store.record_manual_compare(payload, [peer], event_id="device:005930.KO:AAPL")

    relationship = store.get_peer_relationship("005930.KO", "AAPL")
    recent = store.list_manual_compares(subject_ticker="005930.KO", limit=5)

    assert relationship is not None
    assert relationship["manual_compare_hits"] == 1
    assert recent[0]["event_id"] == "device:005930.KO:AAPL"


def test_discovery_store_uses_database_url_when_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-brain.db"
    database_url = f"sqlite:///{db_path.resolve().as_posix()}"
    monkeypatch.setenv("SHARED_BRAIN_DATABASE_URL", database_url)
    reset_shared_brain_engine_cache()

    try:
        universe = SymbolUniverseStore()
        store = DiscoveryStore(universe_store=universe)

        store.add_to_watchlist(
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc",
                "exchange": "US",
                "sector": "Technology",
                "industry": "Consumer Electronics",
            }
        )

        assert store.storage_backend == "sqlite-url"
        assert universe.storage_backend == "sqlite-url"
        assert store.list_watchlist(limit=1)[0]["ticker"] == "AAPL"
        assert db_path.exists()
    finally:
        reset_shared_brain_engine_cache()