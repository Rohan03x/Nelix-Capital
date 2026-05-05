from __future__ import annotations

import json
from pathlib import Path

import webapp.app as webapp_module
import webapp.data.eodhd_client as eodhd_client

from webapp.data import ticker_search


def test_search_tickers_keeps_same_code_across_multiple_exchanges(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="RIO.LSE",
                code="RIO",
                name="Rio Tinto plc",
                exchange="LSE",
                country="United Kingdom",
                source="cache",
            ),
            ticker_search._build_search_item(
                ticker="RIO.ASX",
                code="RIO",
                name="Rio Tinto Ltd",
                exchange="ASX",
                country="Australia",
                source="cache",
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_live_search_items", lambda query: ())
    monkeypatch.setattr(ticker_search, "_load_search_shard", lambda _letter: ())

    results = ticker_search.search_tickers("RIO", limit=10)
    tickers = [item["ticker"] for item in results]

    assert "RIO.LSE" in tickers
    assert "RIO.ASX" in tickers


def test_search_tickers_uses_full_index_when_shard_misses(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_load_search_shard",
        lambda _letter: (
            ticker_search._build_search_item(
                ticker="SAP.XETRA",
                code="SAP",
                name="SAP SE",
                exchange="XETRA",
                country="Germany",
                source="cache",
            ),
        ),
    )
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="LIGHT.AS",
                code="LIGHT",
                name="Signify N.V.",
                exchange="AS",
                country="Netherlands",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                has_fundamentals=True,
            ),
        ),
    )

    def _fail_live(_query):
        raise AssertionError("live search should not run while cached index can answer")

    monkeypatch.setattr(ticker_search, "_live_search_items", _fail_live)

    results = ticker_search.search_tickers("signify", limit=5)

    assert [item["ticker"] for item in results] == ["LIGHT.AS"]


def test_resolve_search_input_uses_ranked_prefix_match(monkeypatch):
    candidates = [
        ticker_search._build_search_item(
            ticker="PHPPY.US",
            code="PHPPY",
            name="Signify N.V. ADR",
            exchange="US",
            country="USA",
            source="search-cache",
            instrument_type="Depositary Receipt",
        ),
        ticker_search._build_search_item(
            ticker="LIGHT.AS",
            code="LIGHT",
            name="Signify N.V.",
            exchange="AS",
            country="Netherlands",
            source="cache",
            instrument_type="Common Stock",
            is_primary=True,
            market_cap=7_000_000_000,
            history_years=10,
            has_fundamentals=True,
        ),
    ]
    monkeypatch.setattr(ticker_search, "_search_candidates", lambda _query: candidates)

    assert ticker_search.resolve_search_input("signify") == "LIGHT.AS"


def test_available_exchanges_uses_manifest_without_loading_full_index(tmp_path, monkeypatch):
    manifest_path = tmp_path / "search_exchanges.json"
    manifest_path.write_text(json.dumps({"exchanges": ["US", "LSE", "KO"]}), encoding="utf-8")
    ticker_search.available_exchanges.cache_clear()
    monkeypatch.setattr(ticker_search, "_EXCHANGE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (_ for _ in ()).throw(AssertionError("full index should not load for manifest-backed exchanges")),
    )

    assert ticker_search.available_exchanges()[:3] == ("US", "NYSE", "NASDAQ")
    assert "LSE" in ticker_search.available_exchanges()
    assert "KO" in ticker_search.available_exchanges()

    ticker_search.available_exchanges.cache_clear()


def test_flask_ticker_search_route_uses_shards(tmp_path, monkeypatch):
    shard_item = ticker_search._build_search_item(
        ticker="LIGHT.AS",
        code="LIGHT",
        name="Signify N.V.",
        exchange="AS",
        country="Netherlands",
        source="cache",
        instrument_type="Common Stock",
        is_primary=True,
        has_fundamentals=True,
    )
    (tmp_path / "search_shard_s.json").write_text(json.dumps([shard_item]), encoding="utf-8")
    monkeypatch.setattr(ticker_search, "_CACHE_DIR", tmp_path)
    ticker_search._load_search_shard.cache_clear()

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    with webapp_module.app.test_client() as client:
        response = client.get("/api/ticker-search?q=signify&limit=20&exchange=auto")

    payload = response.get_json()

    assert response.status_code == 200
    assert payload["results"] == [
        {
            "ticker": "LIGHT.AS",
            "code": "LIGHT",
            "name": "Signify N.V.",
            "exchange": "AS",
            "country": "Netherlands",
        }
    ]
    ticker_search._load_search_shard.cache_clear()


def test_search_tickers_prefers_primary_common_stock_before_aliases_and_non_stocks(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_search_candidates",
        lambda _query: [
            ticker_search._build_search_item(
                ticker="GOOGL.SN",
                code="GOOGL",
                name="Alphabet Inc. Cl A",
                exchange="SN",
                country="Chile",
                source="live",
                instrument_type="Common Stock",
            ),
            ticker_search._build_search_item(
                ticker="GOOGL.MX",
                code="GOOGL",
                name="Alphabet Inc Class A",
                exchange="MX",
                country="Mexico",
                source="live",
                instrument_type="Common Stock",
            ),
            ticker_search._build_search_item(
                ticker="GOOGL.US",
                code="GOOGL",
                name="Alphabet Inc Class A",
                exchange="US",
                country="USA",
                source="live",
                instrument_type="Common Stock",
                is_primary=True,
                primary_ticker="GOOGL.US",
            ),
            ticker_search._build_search_item(
                ticker="GOOGL.NASDAQ",
                code="GOOGL",
                name="Alphabet Inc Class A",
                exchange="NASDAQ",
                country="USA",
                source="exchange-cache",
                instrument_type="Common Stock",
            ),
            ticker_search._build_search_item(
                ticker="GOOGLX-USD.CC",
                code="GOOGLX-USD",
                name="Alphabet tokenized stock (xStock)",
                exchange="CC",
                country="Unknown",
                source="live",
                instrument_type="Currency",
            ),
        ],
    )

    results = ticker_search.search_tickers("GOOGL", limit=5)
    tickers = [item["ticker"] for item in results]

    assert tickers[:2] == ["GOOGL.US", "GOOGL.NASDAQ"]
    assert set(tickers[2:4]) == {"GOOGL.MX", "GOOGL.SN"}
    assert tickers[-1] == "GOOGLX-USD.CC"


def test_seedable_tickers_prefers_primary_common_stock(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="ETF1.US",
                code="ETF1",
                name="ETF One",
                exchange="US",
                country="USA",
                source="search-cache",
                instrument_type="ETF",
                is_primary=False,
            ),
            ticker_search._build_search_item(
                ticker="RIO.LSE",
                code="RIO",
                name="Rio Tinto plc",
                exchange="LSE",
                country="United Kingdom",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=False,
            ),
            ticker_search._build_search_item(
                ticker="BHP.ASX",
                code="BHP",
                name="BHP Group Ltd",
                exchange="ASX",
                country="Australia",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
            ticker_search._build_search_item(
                ticker="AAPL.US",
                code="AAPL",
                name="Apple Inc",
                exchange="NASDAQ",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_cached_primary_listing_hints", lambda: {})
    monkeypatch.setattr(ticker_search, "_recent_seed_symbol_health", lambda: {})

    tickers = ticker_search.seedable_tickers(limit=3, common_stock_only=True)

    assert tickers == ["BHP.ASX", "AAPL.US", "RIO.LSE"]


def test_seedable_tickers_skips_cached_alias_when_primary_listing_exists(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="BHP1.HM",
                code="BHP1",
                name="BHP Group Limited",
                exchange="HM",
                country="Germany",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=False,
                isin="AU000000BHP4",
            ),
            ticker_search._build_search_item(
                ticker="BHP.AU",
                code="BHP",
                name="BHP Group Limited",
                exchange="AU",
                country="Australia",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                isin="AU000000BHP4",
                primary_ticker="BHP.AU",
            ),
            ticker_search._build_search_item(
                ticker="AAPL.US",
                code="AAPL",
                name="Apple Inc",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
        ),
    )
    monkeypatch.setattr(
        ticker_search,
        "_cached_primary_listing_hints",
        lambda: {"BHP1.HM": {"primary_ticker": "BHP.AU", "isin": "AU000000BHP4"}},
    )

    tickers = ticker_search.seedable_tickers(limit=5, common_stock_only=True)

    assert tickers == ["AAPL.US", "BHP.AU"]


def test_seedable_tickers_dedupes_same_company_across_cross_listings(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="BHP.LSE",
                code="BHP",
                name="BHP Group Limited",
                exchange="LSE",
                country="United Kingdom",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=True,
                isin="GB00BH0P3Z91",
            ),
            ticker_search._build_search_item(
                ticker="BHP.DU",
                code="BHP",
                name="BHP Group Limited",
                exchange="DU",
                country="Germany",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                isin="US0886061086",
            ),
            ticker_search._build_search_item(
                ticker="AAPL.US",
                code="AAPL",
                name="Apple Inc",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_cached_primary_listing_hints", lambda: {})

    tickers = ticker_search.seedable_tickers(limit=5, common_stock_only=True)

    assert tickers == ["BHP.LSE", "AAPL.US"]


def test_seedable_tickers_skips_recently_unavailable_symbols(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="DIH.VN",
                code="DIH",
                name="Development Investment Construction Hoi An JSC",
                exchange="VN",
                country="Vietnam",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
            ticker_search._build_search_item(
                ticker="AAPL.US",
                code="AAPL",
                name="Apple Inc",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_cached_primary_listing_hints", lambda: {})
    monkeypatch.setattr(
        ticker_search,
        "_recent_seed_symbol_health",
        lambda: {"DIH.VN": {"available": False, "source": "unavailable-demo"}},
    )

    tickers = ticker_search.seedable_tickers(limit=5, common_stock_only=True)

    assert tickers == ["AAPL.US"]


def test_seedable_tickers_prefers_live_success_and_richer_cached_fundamentals(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="THIN.SE",
                code="THIN",
                name="Thin Search Cache",
                exchange="SE",
                country="Sweden",
                source="search-cache",
                instrument_type="Common Stock",
                is_primary=True,
            ),
            ticker_search._build_search_item(
                ticker="RICH.US",
                code="RICH",
                name="Rich Fundamentals Inc",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                sector="Technology",
                industry="Software",
                market_cap=9_500_000_000,
                history_years=10,
                has_fundamentals=True,
            ),
            ticker_search._build_search_item(
                ticker="LIVE.US",
                code="LIVE",
                name="Live Success Corp",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                sector="Industrials",
                industry="Machinery",
                market_cap=1_200_000_000,
                history_years=7,
                has_fundamentals=True,
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_cached_primary_listing_hints", lambda: {})
    monkeypatch.setattr(
        ticker_search,
        "_recent_seed_symbol_health",
        lambda: {"LIVE.US": {"available": True, "source": "eodhd"}},
    )

    tickers = ticker_search.seedable_tickers(limit=5, common_stock_only=True)

    assert tickers == ["LIVE.US", "RICH.US", "THIN.SE"]


def test_seedable_tickers_skips_cached_dcf_unsuitable_sectors(monkeypatch):
    monkeypatch.setattr(
        ticker_search,
        "_ticker_search_index",
        lambda: (
            ticker_search._build_search_item(
                ticker="GOOD.US",
                code="GOOD",
                name="Gladstone Commercial Corporation",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                sector="Real Estate",
                industry="Specialty REIT",
                market_cap=850_000_000,
                history_years=10,
                has_fundamentals=True,
            ),
            ticker_search._build_search_item(
                ticker="LOB.US",
                code="LOB",
                name="Live Oak Bancshares, Inc.",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                sector="Financial Services",
                industry="Banks - Regional",
                market_cap=1_000_000_000,
                history_years=10,
                has_fundamentals=True,
            ),
            ticker_search._build_search_item(
                ticker="AAPL.US",
                code="AAPL",
                name="Apple Inc",
                exchange="US",
                country="USA",
                source="cache",
                instrument_type="Common Stock",
                is_primary=True,
                sector="Technology",
                industry="Consumer Electronics",
                market_cap=10_000_000_000,
                history_years=10,
                has_fundamentals=True,
            ),
        ),
    )
    monkeypatch.setattr(ticker_search, "_cached_primary_listing_hints", lambda: {})
    monkeypatch.setattr(ticker_search, "_recent_seed_symbol_health", lambda: {})

    tickers = ticker_search.seedable_tickers(limit=5, common_stock_only=True)

    assert tickers == ["AAPL.US"]


def test_refresh_exchange_symbol_cache_fetches_and_invalidates_index(monkeypatch):
    invalidations: list[str] = []
    cached_payloads: dict[str, object] = {}

    monkeypatch.setattr(ticker_search, "invalidate_ticker_search_index", lambda: invalidations.append("cleared"))
    monkeypatch.setattr(eodhd_client, "_cache_read", lambda key, ttl: None)
    monkeypatch.setattr(eodhd_client, "_cache_write", lambda key, payload: cached_payloads.__setitem__(key, payload))
    monkeypatch.setattr(
        eodhd_client,
        "_get",
        lambda endpoint: [
            {
                "Code": "SAP",
                "Exchange": "DE",
                "Name": "SAP SE",
                "Country": "Germany",
                "Type": "Common Stock",
                "isPrimary": True,
            },
            {
                "Code": "SIE",
                "Exchange": "DE",
                "Name": "Siemens AG",
                "Country": "Germany",
                "Type": "Common Stock",
                "isPrimary": True,
            },
        ] if endpoint == "exchange-symbol-list/DE" else [],
    )

    result = ticker_search.refresh_exchange_symbol_cache(["de"], per_exchange_limit=5, ttl_sec=60)

    assert result["exchanges"] == ["DE"]
    assert result["fetched_exchanges"] == ["DE"]
    assert result["total_items"] == 2
    assert [item["ticker"] for item in result["items"]] == ["SAP.DE", "SIE.DE"]
    assert cached_payloads[ticker_search._exchange_cache_key("DE")][0]["Code"] == "SAP"
    assert invalidations == ["cleared"]