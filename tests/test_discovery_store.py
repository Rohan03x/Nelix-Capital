from __future__ import annotations

from auto_valuation.learning.discovery import DiscoveryStore
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
    assert auto_peers["peer_count"] == 1
    assert result["peer_count"] == 1
    assert store.list_watchlist(limit=5)[0]["ticker"] == "005930.KO"
    assert store.list_manual_compares(subject_ticker="005930.KO", limit=5)[0]["ticker"] == "AAPL"