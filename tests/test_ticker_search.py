from __future__ import annotations

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