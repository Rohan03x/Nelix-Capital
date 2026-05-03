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

    assert tickers == ["BHP.ASX", "RIO.LSE", "AAPL.US"]


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