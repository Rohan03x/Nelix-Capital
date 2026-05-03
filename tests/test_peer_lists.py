from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types

from auto_valuation.learning.discovery import DiscoveryStore
from auto_valuation.learning.universe import SymbolUniverseStore


def _write_cache(path: Path, payload: dict) -> None:
    path.write_text(json.dumps({"_ts": "2026-05-03T00:00:00+00:00", "data": payload}), encoding="utf-8")


def test_get_peers_for_luxury_goods_avoids_broad_sector_fallback():
    from webapp.data.peer_lists import get_peers_for_ticker

    peers = get_peers_for_ticker("CDI.PA", "Consumer Cyclical", "Luxury Goods")

    assert "MC.PA" in peers
    assert "RMS.PA" in peers
    assert "AMZN" not in peers
    assert "TSLA" not in peers
    assert "NKE" not in peers[:6]


def test_get_peers_prefers_cached_same_industry_before_sector(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _write_cache(
        cache_dir / "eodhd_fund_CDI_PA.json",
        {
            "General": {"Code": "CDI", "Exchange": "PA", "Sector": "Consumer Cyclical", "Industry": "Luxury Goods"},
            "Highlights": {"MarketCapitalizationMln": 76710.0},
        },
    )
    _write_cache(
        cache_dir / "eodhd_fund_MC_PA.json",
        {
            "General": {"Code": "MC", "Exchange": "PA", "Sector": "Consumer Cyclical", "Industry": "Luxury Goods"},
            "Highlights": {"MarketCapitalizationMln": 380000.0},
        },
    )
    _write_cache(
        cache_dir / "eodhd_fund_RMS_PA.json",
        {
            "General": {"Code": "RMS", "Exchange": "PA", "Sector": "Consumer Cyclical", "Industry": "Luxury Goods"},
            "Highlights": {"MarketCapitalizationMln": 220000.0},
        },
    )
    _write_cache(
        cache_dir / "eodhd_fund_AMZN_US.json",
        {
            "General": {"Code": "AMZN", "Exchange": "US", "Sector": "Consumer Cyclical", "Industry": "Internet Retail"},
            "Highlights": {"MarketCapitalizationMln": 2000000.0},
        },
    )

    import webapp.data.peer_lists as peer_lists

    monkeypatch.setattr(peer_lists.Path, "with_name", lambda self, name: cache_dir)
    peer_lists._load_cached_peer_profiles.cache_clear()
    reloaded = importlib.reload(peer_lists)
    monkeypatch.setattr(reloaded.Path, "with_name", lambda self, name: cache_dir)
    reloaded._load_cached_peer_profiles.cache_clear()

    peers = reloaded.get_peers_for_ticker("CDI.PA", "Consumer Cyclical", "Luxury Goods")

    assert set(peers[:2]) == {"MC.PA", "RMS.PA"}
    assert "AMZN" not in peers[:4]


def test_get_peers_for_known_industry_skips_broad_sector_fallback(monkeypatch):
    import webapp.data.peer_lists as peer_lists

    monkeypatch.setattr(peer_lists, "_discover_cached_peers", lambda *args, **kwargs: [])
    monkeypatch.setattr(peer_lists, "_industry_peers", lambda *args, **kwargs: [])
    monkeypatch.setattr(peer_lists, "_sector_peers", lambda _sector: ["RTX", "HON", "CAT"])

    peers = peer_lists.get_peers_for_ticker("ADEN.SW", "Industrials", "Staffing & Employment Services")

    assert peers == []


def test_discover_cached_peers_rejects_same_family_only_matches(monkeypatch):
    import webapp.data.peer_lists as peer_lists

    monkeypatch.setattr(
        peer_lists,
        "_load_cached_peer_profiles",
        lambda: (
            {
                "ticker": "ADEN.SW",
                "variants": {"ADEN.SW", "ADEN"},
                "exchange": "SW",
                "sector": "Industrials",
                "industry": "Staffing & Employment Services",
                "market_cap_mln": 3000.0,
            },
            {
                "ticker": "LIGHT.AS",
                "variants": {"LIGHT.AS", "LIGHT"},
                "exchange": "AS",
                "sector": "Industrials",
                "industry": "Electrical Equipment & Parts",
                "market_cap_mln": 2500.0,
            },
        ),
    )

    def _fake_taxonomy(industry: str, sector: str = "") -> dict[str, object]:
        if "Staffing" in industry:
            return {
                "canonical_industry": "Staffing & Employment Services",
                "family": "industrials",
                "related_industries": [],
            }
        return {
            "canonical_industry": "Electrical Equipment & Parts",
            "family": "industrials",
            "related_industries": [],
        }

    monkeypatch.setattr(peer_lists, "resolve_industry_taxonomy", _fake_taxonomy)
    monkeypatch.setattr(peer_lists, "industry_similarity", lambda *args, **kwargs: 0.6)

    peers = peer_lists._discover_cached_peers(
        "ADEN.SW",
        "Industrials",
        "Staffing & Employment Services",
        include_related=True,
    )

    assert peers == []


def test_peer_cache_key_changes_when_peer_basket_changes():
    from webapp.data.peer_lists import _peer_cache_key

    first = _peer_cache_key("CDI.PA", ["MC.PA", "RMS.PA"])
    second = _peer_cache_key("CDI.PA", ["AMZN", "TSLA"])

    assert first != second


def test_get_peers_uses_universe_learning_signal_to_rank_candidates(tmp_path, monkeypatch):
    import webapp.data.peer_lists as peer_lists

    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    universe.upsert_symbol(
        "RMS.PA",
        exchange="PA",
        sector="Consumer Cyclical",
        industry="Luxury Goods",
        source="manual-compare",
        metadata={"peer_learning_score": 5.0},
        metadata_increments={"compare_hits": 3},
    )
    monkeypatch.setattr(peer_lists, "_safe_universe_store", lambda: universe)

    ranked = peer_lists._rank_peer_tickers(
        ["MC.PA", "RMS.PA", "KER.PA"],
        subject_ticker="CDI.PA",
        sector="Consumer Cyclical",
        industry="Luxury Goods",
    )

    assert ranked[0] == "RMS.PA"


def test_rank_peer_tickers_uses_pair_relationship_memory(tmp_path, monkeypatch):
    import webapp.data.peer_lists as peer_lists

    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    discovery = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)
    discovery.record_auto_peer_basket(
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
                "base_peer_learning_score": 5.5,
            }
        ],
    )
    discovery.record_manual_compare(
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
            }
        ],
    )

    monkeypatch.setattr(peer_lists, "_safe_universe_store", lambda: universe)
    monkeypatch.setattr(peer_lists, "_safe_discovery_store", lambda: discovery)

    ranked = peer_lists._rank_peer_tickers(
        ["MC.PA", "RMS.PA", "KER.PA"],
        subject_ticker="CDI.PA",
        sector="Consumer Cyclical",
        industry="Luxury Goods",
    )

    assert ranked[0] == "RMS.PA"


def test_fetch_peer_metrics_preserves_ranked_input_order_from_cache(monkeypatch):
    import webapp.data.peer_lists as peer_lists

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace())
    monkeypatch.setattr(
        peer_lists,
        "_load_cache",
        lambda key: [
            {"ticker": "MC.PA", "name": "LVMH", "market_cap": 380000, "subject": False},
            {"ticker": "RMS.PA", "name": "Hermes", "market_cap": 220000, "subject": False},
        ],
    )
    monkeypatch.setattr(
        peer_lists,
        "_load_cached_peer_profiles",
        lambda: [
            {"ticker": "CDI.PA", "variants": {"CDI.PA", "CDI"}, "exchange": "PA", "sector": "Consumer Cyclical", "industry": "Luxury Goods", "market_cap_mln": 76710.0},
            {"ticker": "MC.PA", "variants": {"MC.PA", "MC"}, "exchange": "PA", "sector": "Consumer Cyclical", "industry": "Luxury Goods", "market_cap_mln": 380000.0},
            {"ticker": "RMS.PA", "variants": {"RMS.PA", "RMS"}, "exchange": "PA", "sector": "Consumer Cyclical", "industry": "Luxury Goods", "market_cap_mln": 220000.0},
        ],
    )
    monkeypatch.setattr(peer_lists, "_safe_universe_store", lambda: None)

    peers, peer_median = peer_lists.fetch_peer_metrics(["RMS.PA", "MC.PA"], "CDI.PA")

    assert [peer["ticker"] for peer in peers[:2]] == ["RMS.PA", "MC.PA"]
    assert peers[0]["industry_similarity"] == 1.0
    assert peers[0]["peer_learning_score"] >= 5.0
    assert peer_median["ev_rev"] is None


def test_fetch_peer_metrics_backfills_curated_industry_when_live_metadata_missing(monkeypatch):
    import webapp.data.peer_lists as peer_lists

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace())
    monkeypatch.setattr(
        peer_lists,
        "_load_cache",
        lambda key: [
            {"ticker": "MC.PA", "name": "LVMH", "market_cap": 380000, "subject": False, "industry": "", "sector": ""},
        ],
    )
    monkeypatch.setattr(peer_lists, "_load_cached_peer_profiles", lambda: [])
    monkeypatch.setattr(peer_lists, "_safe_universe_store", lambda: None)
    monkeypatch.setattr(peer_lists, "_safe_discovery_store", lambda: None)

    peers, _ = peer_lists.fetch_peer_metrics(
        ["MC.PA"],
        "CDI.PA",
        target_sector="Consumer Cyclical",
        target_industry="Luxury Goods",
    )

    assert peers[0]["canonical_industry"] == "Luxury Goods"
    assert peers[0]["industry_family"] == "luxury-fashion"
    assert peers[0]["sector"] == "Consumer Cyclical"


def test_fetch_peer_metrics_normalizes_eodhd_suffixes_for_yfinance(monkeypatch):
    import webapp.data.peer_lists as peer_lists

    requested: list[str] = []

    class _FakeTicker:
        def __init__(self, symbol: str):
            requested.append(symbol)
            self.info = {
                "shortName": symbol,
                "marketCap": 1_000_000_000,
                "totalRevenue": 500_000_000,
                "ebitda": 100_000_000,
                "ebit": 80_000_000,
                "netIncomeToCommon": 50_000_000,
                "freeCashflow": 40_000_000,
                "totalDebt": 200_000_000,
                "totalCash": 50_000_000,
            }

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_FakeTicker))
    monkeypatch.setattr(peer_lists, "_load_cache", lambda _key: None)
    monkeypatch.setattr(peer_lists, "_save_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(peer_lists, "_load_cached_peer_profiles", lambda: ())
    monkeypatch.setattr(peer_lists, "_safe_universe_store", lambda: None)
    monkeypatch.setattr(peer_lists, "_safe_discovery_store", lambda: None)

    peers, peer_median = peer_lists.fetch_peer_metrics(
        ["G14.XETRA", "70GD.LSE", "JBGS.US"],
        "ADEN.SW",
    )

    assert requested == ["G14.DE", "70GD.L", "JBGS"]
    assert [peer["ticker"] for peer in peers] == ["G14.XETRA", "70GD.LSE", "JBGS.US"]
    assert peer_median["ev_rev"] == 2.3