from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from webapp.data.samples import SUPPORTED_TICKERS

_CACHE_DIR = Path(__file__).with_name("cache")
_SPACE_RE = re.compile(r"[^A-Z0-9]+")
_SEARCH_TTL_SEC = 43_200


def _normalise_search_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.upper()).strip()


def _build_search_item(
    *,
    ticker: str,
    code: str,
    name: str,
    exchange: str,
    country: str,
    source: str,
    instrument_type: str = "",
    is_primary: bool = False,
) -> dict[str, object]:
    ticker_text = str(ticker or "").strip().upper()
    code_text = str(code or ticker_text.split(".")[0]).strip().upper()
    name_text = str(name or ticker_text).strip()
    exchange_text = str(exchange or "").strip().upper()
    country_text = str(country or "").strip()
    search_text = _normalise_search_text(
        " ".join(part for part in (ticker_text, code_text, name_text, exchange_text, country_text) if part)
    )
    name_key = _normalise_search_text(name_text)
    return {
        "ticker": ticker_text,
        "code": code_text,
        "name": name_text,
        "exchange": exchange_text,
        "country": country_text,
        "search_text": search_text,
        "name_key": name_key,
        "ticker_key": _normalise_search_text(ticker_text),
        "code_key": _normalise_search_text(code_text),
        "name_words": name_key.split(),
        "source": source,
        "instrument_type": str(instrument_type or "").strip(),
        "is_primary": bool(is_primary),
    }


def _compose_ticker(code: str, exchange: str) -> str:
    code_text = str(code or "").strip().upper()
    exchange_text = str(exchange or "").strip().upper()
    if not code_text:
        return ""
    if "." in code_text or not exchange_text:
        return code_text
    return f"{code_text}.{exchange_text}"


def _build_index_item(payload_path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    general = data.get("General")
    if not isinstance(general, dict):
        return None

    code = str(general.get("Code") or "").strip().upper()
    exchange = str(general.get("Exchange") or "").strip().upper()
    primary_ticker = str(general.get("PrimaryTicker") or "").strip().upper()
    if not primary_ticker:
        primary_ticker = _compose_ticker(code, exchange)
    if not primary_ticker:
        return None

    return _build_search_item(
        ticker=primary_ticker,
        code=code or primary_ticker.split(".")[0],
        name=str(general.get("Name") or primary_ticker).strip(),
        exchange=exchange,
        country=str(general.get("CountryName") or general.get("CountryISO") or "").strip(),
        source="cache",
        instrument_type="Common Stock",
        is_primary=True,
    )


def _build_cached_search_item(row: dict[str, object], *, source: str = "search-cache") -> dict[str, object] | None:
    code = str(row.get("Code") or "").strip().upper()
    exchange = str(row.get("Exchange") or "").strip().upper()
    ticker = _compose_ticker(code, exchange)
    if not ticker:
        return None
    return _build_search_item(
        ticker=ticker,
        code=code or ticker.split(".")[0],
        name=str(row.get("Name") or ticker).strip(),
        exchange=exchange,
        country=str(row.get("Country") or row.get("CountryName") or "").strip(),
        source=source,
        instrument_type=str(row.get("Type") or "").strip(),
        is_primary=bool(row.get("isPrimary") or row.get("IsPrimary")),
    )


def _search_cache_key(query: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", query.upper()).strip("_")
    return f"ticker_search_{slug or 'EMPTY'}"


def _exchange_cache_key(exchange: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", str(exchange or "").upper()).strip("_")
    return f"exchange_symbols_{slug or 'UNKNOWN'}"


def _live_search_items(query: str) -> tuple[dict[str, object], ...]:
    query_text = str(query or "").strip()
    if len(query_text) < 2:
        return ()

    try:
        from webapp.data.eodhd_client import _cache_read, _cache_write, _get
    except Exception:
        return ()

    cache_key = _search_cache_key(query_text)
    payload = _cache_read(cache_key, _SEARCH_TTL_SEC)
    if payload is None:
        payload = _get(f"search/{quote(query_text, safe='')}")
        if isinstance(payload, list):
            _cache_write(cache_key, payload)
        else:
            payload = []

    items: list[dict[str, object]] = []
    seen_tickers: set[str] = set()
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("Code") or "").strip().upper()
        exchange = str(row.get("Exchange") or "").strip().upper()
        ticker = _compose_ticker(code, exchange)
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        items.append(
            _build_search_item(
                ticker=ticker,
                code=code or ticker.split(".")[0],
                name=str(row.get("Name") or ticker).strip(),
                exchange=exchange,
                country=str(row.get("Country") or row.get("CountryName") or "").strip(),
                source="live",
                instrument_type=str(row.get("Type") or "").strip(),
                is_primary=bool(row.get("isPrimary") or row.get("IsPrimary")),
            )
        )
    return tuple(items)


def _search_candidates(query: str) -> list[dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    for item in _live_search_items(query):
        candidates[str(item["ticker"])] = item
    for item in _ticker_search_index():
        candidates.setdefault(str(item["ticker"]), item)
    return list(candidates.values())


def _iter_cached_search_items() -> tuple[dict[str, object], ...]:
    items: list[dict[str, object]] = []
    for payload_path in sorted(_CACHE_DIR.glob("eodhd_ticker_search_*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = _build_cached_search_item(row, source="search-cache")
            if item is not None:
                items.append(item)
    return tuple(items)


def _iter_exchange_cache_items() -> tuple[dict[str, object], ...]:
    items: list[dict[str, object]] = []
    for payload_path in sorted(_CACHE_DIR.glob("eodhd_exchange_symbols_*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = _build_cached_search_item(row, source="exchange-cache")
            if item is not None:
                items.append(item)
    return tuple(items)


def invalidate_ticker_search_index() -> None:
    _ticker_search_index.cache_clear()


@lru_cache(maxsize=1)
def _ticker_search_index() -> tuple[dict[str, object], ...]:
    items: list[dict[str, object]] = []
    seen_tickers: set[str] = set()

    for payload_path in sorted(_CACHE_DIR.glob("eodhd_fund_*.json")):
        item = _build_index_item(payload_path)
        if item is None:
            continue
        ticker = str(item["ticker"])
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        items.append(item)

    for item in _iter_cached_search_items():
        ticker = str(item["ticker"])
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        items.append(item)

    for item in _iter_exchange_cache_items():
        ticker = str(item["ticker"])
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        items.append(item)

    for ticker in SUPPORTED_TICKERS:
        ticker_code = str(ticker).strip().upper()
        if not ticker_code or ticker_code in seen_tickers:
            continue
        items.append(
            _build_search_item(
                ticker=ticker_code,
                code=ticker_code,
                name=ticker_code,
                exchange="",
                country="",
                source="supported",
            )
        )

    items.sort(key=lambda item: (str(item["name"]).upper(), str(item["ticker"])))
    return tuple(items)


def seedable_symbol_items(limit: int | None = None, *, common_stock_only: bool = True) -> list[dict[str, object]]:
    allowed_types = {
        "",
        "common stock",
        "common shares",
        "ordinary shares",
        "depositary receipt",
        "adr",
    }
    source_priority = {"search-cache": 0, "exchange-cache": 1, "cache": 2, "supported": 3}
    ranked_items = sorted(
        _ticker_search_index(),
        key=lambda item: (
            source_priority.get(str(item.get("source") or ""), 9),
            0 if bool(item.get("is_primary")) else 1,
            str(item.get("name") or "").upper(),
            str(item.get("ticker") or ""),
        ),
    )

    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    max_items = int(limit) if limit is not None and int(limit) > 0 else None
    for item in ranked_items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        instrument_type = str(item.get("instrument_type") or "").strip().lower()
        if common_stock_only and instrument_type not in allowed_types:
            continue
        seen.add(ticker)
        selected.append(dict(item))
        if max_items is not None and len(selected) >= max_items:
            break
    return selected


def seedable_tickers(limit: int | None = None, *, common_stock_only: bool = True) -> list[str]:
    tickers: list[str] = []
    for item in seedable_symbol_items(limit=limit, common_stock_only=common_stock_only):
        ticker = str(item.get("ticker") or "").strip().upper()
        if ticker:
            tickers.append(ticker)
    return tickers


def refresh_exchange_symbol_cache(
    exchanges: Iterable[str],
    *,
    per_exchange_limit: int = 250,
    ttl_sec: int = 604_800,
) -> dict[str, Any]:
    requested_exchanges: list[str] = []
    seen_exchanges: set[str] = set()
    for raw_exchange in exchanges:
        exchange = str(raw_exchange or "").strip().upper()
        if not exchange or exchange in seen_exchanges:
            continue
        seen_exchanges.add(exchange)
        requested_exchanges.append(exchange)

    if not requested_exchanges:
        return {"exchanges": [], "items": [], "fetched_exchanges": [], "total_items": 0}

    try:
        from webapp.data.eodhd_client import _cache_read, _cache_write, _get
    except Exception:
        return {
            "exchanges": requested_exchanges,
            "items": [],
            "fetched_exchanges": [],
            "total_items": 0,
            "error": "eodhd-unavailable",
        }

    all_items: list[dict[str, object]] = []
    fetched_exchanges: list[str] = []
    counts: dict[str, int] = {}
    cache_changed = False

    for exchange in requested_exchanges:
        cache_key = _exchange_cache_key(exchange)
        cached = _cache_read(cache_key, ttl_sec)
        rows = cached if isinstance(cached, list) else None

        if rows is None:
            payload = _get(f"exchange-symbol-list/{quote(exchange, safe='')}")
            if isinstance(payload, dict):
                payload_rows = payload.get("data")
                rows = payload_rows if isinstance(payload_rows, list) else []
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []
            _cache_write(cache_key, rows)
            fetched_exchanges.append(exchange)
            cache_changed = True

        exchange_items: list[dict[str, object]] = []
        max_items = max(int(per_exchange_limit or 0), 0)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = _build_cached_search_item(row, source="exchange-cache")
            if item is None:
                continue
            exchange_items.append(item)
            if max_items and len(exchange_items) >= max_items:
                break
        counts[exchange] = len(exchange_items)
        all_items.extend(exchange_items)

    if cache_changed:
        invalidate_ticker_search_index()

    return {
        "exchanges": requested_exchanges,
        "items": all_items,
        "fetched_exchanges": fetched_exchanges,
        "counts": counts,
        "total_items": len(all_items),
    }


def _match_score(query_key: str, item: dict[str, object]) -> tuple[int, int, str] | None:
    ticker_key = str(item["ticker_key"])
    code_key = str(item["code_key"])
    name_key = str(item["name_key"])
    name_words = [str(word) for word in item["name_words"]]

    if query_key in {ticker_key, code_key, name_key}:
        return (0, len(str(item["name"])), str(item["ticker"]))
    if ticker_key.startswith(query_key) or code_key.startswith(query_key):
        return (1, len(str(item["ticker"])), str(item["ticker"]))
    if name_key.startswith(query_key):
        return (2, len(str(item["name"])), str(item["ticker"]))
    if any(word.startswith(query_key) for word in name_words):
        return (3, len(str(item["name"])), str(item["ticker"]))
    if query_key in str(item["search_text"]):
        return (4, len(str(item["name"])), str(item["ticker"]))
    return None


def search_tickers(query: str, limit: int = 12, exchange: str = "auto") -> list[dict[str, str]]:
    query_key = _normalise_search_text(query)
    if not query_key:
        return []

    exchange_key = str(exchange or "auto").strip().upper()
    matches: list[tuple[tuple[int, int, int, str], dict[str, object]]] = []
    for item in _search_candidates(query):
        score = _match_score(query_key, item)
        if score is None:
            continue
        item_exchange = str(item.get("exchange") or "").upper()
        exchange_penalty = 1
        if exchange_key not in {"", "AUTO"}:
            if exchange_key in {"NYSE", "NASDAQ"}:
                exchange_penalty = 0 if item_exchange in {exchange_key, "US"} else 1
            elif exchange_key == "HKEX":
                exchange_penalty = 0 if item_exchange in {"HKEX", "HK"} else 1
            elif exchange_key == "TSE":
                exchange_penalty = 0 if item_exchange in {"TSE", "T"} else 1
            else:
                exchange_penalty = 0 if item_exchange == exchange_key else 1
        matches.append(((score[0], exchange_penalty, score[1], score[2]), item))

    matches.sort(key=lambda pair: pair[0])

    results: list[dict[str, str]] = []
    for _, item in matches[:limit]:
        results.append(
            {
                "ticker": str(item["ticker"]),
                "code": str(item["code"]),
                "name": str(item["name"]),
                "exchange": str(item["exchange"]),
                "country": str(item["country"]),
            }
        )
    return results


def resolve_search_input(value: str, exchange: str = "auto") -> str | None:
    query_key = _normalise_search_text(value)
    if not query_key:
        return None

    candidates = _search_candidates(value)

    exact_matches = [
        item for item in candidates
        if query_key in {str(item["ticker_key"]), str(item["code_key"]), str(item["name_key"])}
    ]
    if exchange and str(exchange).strip().lower() != "auto" and len(exact_matches) > 1:
        preferred = search_tickers(value, limit=10, exchange=exchange)
        if preferred:
            preferred_tickers = {item["ticker"] for item in preferred}
            exact_matches = [item for item in exact_matches if str(item["ticker"]) in preferred_tickers] or exact_matches
    if len(exact_matches) == 1:
        return str(exact_matches[0]["ticker"])

    if len(query_key) < 3:
        return None

    prefix_matches = [
        item for item in candidates
        if str(item["ticker_key"]).startswith(query_key)
        or str(item["code_key"]).startswith(query_key)
        or str(item["name_key"]).startswith(query_key)
        or any(str(word).startswith(query_key) for word in item["name_words"])
    ]
    if exchange and str(exchange).strip().lower() != "auto" and len(prefix_matches) > 1:
        preferred = search_tickers(value, limit=10, exchange=exchange)
        if preferred:
            preferred_tickers = {item["ticker"] for item in preferred}
            prefix_matches = [item for item in prefix_matches if str(item["ticker"]) in preferred_tickers] or prefix_matches
    if len(prefix_matches) == 1:
        return str(prefix_matches[0]["ticker"])

    return None