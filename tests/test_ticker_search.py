from __future__ import annotations

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

    results = ticker_search.search_tickers("RIO", limit=10)
    tickers = [item["ticker"] for item in results]

    assert "RIO.LSE" in tickers
    assert "RIO.ASX" in tickers


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