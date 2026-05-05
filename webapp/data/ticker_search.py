from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from auto_valuation.model.sector import FINANCIAL, MINING, REIT, detect_sector_type
from webapp.data.samples import SUPPORTED_TICKERS


def _resolve_cache_dir() -> Path:
    candidates = [
        Path(__file__).with_name("cache"),
        Path(tempfile.gettempdir()) / "nelix-capital-cache",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    fallback = Path(tempfile.gettempdir()) / "nelix-capital-cache"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


_CACHE_DIR = _resolve_cache_dir()
_PREBUILT_INDEX_PATH = _CACHE_DIR / "search_index_prebuilt.json"
_EXCHANGE_MANIFEST_PATH = _CACHE_DIR / "search_exchanges.json"
_SPACE_RE = re.compile(r"[^A-Z0-9]+")
_SEARCH_TTL_SEC = 43_200
_SEED_HEALTH_STALE_HOURS = 72


def _normalise_search_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.upper()).strip()


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _history_year_count(financials: object) -> int:
    if not isinstance(financials, dict):
        return 0
    income_statement = financials.get("Income_Statement")
    if not isinstance(income_statement, dict):
        return 0
    yearly = income_statement.get("yearly")
    if not isinstance(yearly, dict):
        return 0
    return len(yearly)


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
    isin: str = "",
    primary_ticker: str = "",
    sector: str = "",
    industry: str = "",
    market_cap: float = 0.0,
    history_years: int = 0,
    has_fundamentals: bool = False,
) -> dict[str, object]:
    ticker_text = str(ticker or "").strip().upper()
    code_text = str(code or ticker_text.split(".")[0]).strip().upper()
    name_text = str(name or ticker_text).strip()
    exchange_text = str(exchange or "").strip().upper()
    country_text = str(country or "").strip()
    isin_text = str(isin or "").strip().upper()
    primary_ticker_text = str(primary_ticker or "").strip().upper()
    if not primary_ticker_text and is_primary:
        primary_ticker_text = ticker_text
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
        "sector": str(sector or "").strip(),
        "industry": str(industry or "").strip(),
        "market_cap": max(_safe_float(market_cap), 0.0),
        "history_years": max(int(history_years or 0), 0),
        "has_fundamentals": bool(has_fundamentals),
        "search_text": search_text,
        "name_key": name_key,
        "ticker_key": _normalise_search_text(ticker_text),
        "code_key": _normalise_search_text(code_text),
        "name_words": name_key.split(),
        "source": source,
        "instrument_type": str(instrument_type or "").strip(),
        "is_primary": bool(is_primary),
        "isin": isin_text,
        "primary_ticker": primary_ticker_text,
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
    highlights = data.get("Highlights") if isinstance(data.get("Highlights"), dict) else {}

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
        isin=str(general.get("ISIN") or "").strip(),
        primary_ticker=primary_ticker,
        sector=str(general.get("Sector") or "").strip(),
        industry=str(general.get("Industry") or "").strip(),
        market_cap=_safe_float(highlights.get("MarketCapitalization")),
        history_years=_history_year_count(data.get("Financials")),
        has_fundamentals=True,
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
        isin=str(row.get("ISIN") or row.get("Isin") or "").strip(),
        primary_ticker=str(row.get("PrimaryTicker") or "").strip(),
        sector=str(row.get("Sector") or "").strip(),
        industry=str(row.get("Industry") or "").strip(),
        market_cap=_safe_float(row.get("MarketCapitalization") or row.get("MarketCap")),
        history_years=max(int(row.get("HistoryYears") or 0), 0),
        has_fundamentals=bool(row.get("HistoryYears") or row.get("Sector") or row.get("Industry") or row.get("MarketCapitalization")),
    )


def _search_cache_key(query: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", query.upper()).strip("_")
    return f"ticker_search_{slug or 'EMPTY'}"


def _exchange_cache_key(exchange: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", str(exchange or "").upper()).strip("_")
    return f"exchange_symbols_{slug or 'UNKNOWN'}"


def _seed_health_path() -> Path:
    return _CACHE_DIR / "seed_symbol_health.json"


def _parse_health_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@lru_cache(maxsize=1)
def _recent_seed_symbol_health() -> dict[str, dict[str, object]]:
    path = _seed_health_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}

    stale_before = datetime.now(timezone.utc) - timedelta(hours=_SEED_HEALTH_STALE_HOURS)
    recent: dict[str, dict[str, object]] = {}
    for raw_ticker, raw_entry in (payload.items() if isinstance(payload, dict) else []):
        if not isinstance(raw_entry, dict):
            continue
        checked_at = _parse_health_timestamp(raw_entry.get("checked_at"))
        if checked_at is None or checked_at < stale_before:
            continue
        ticker = str(raw_ticker or "").strip().upper()
        if not ticker:
            continue
        recent[ticker] = dict(raw_entry)
    return recent


def record_seed_symbol_health(ticker: str, *, available: bool, source: str = "", note: str = "") -> None:
    ticker_text = str(ticker or "").strip().upper()
    if not ticker_text:
        return

    path = _seed_health_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload[ticker_text] = {
        "available": bool(available),
        "source": str(source or "").strip(),
        "note": str(note or "").strip(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        return
    _recent_seed_symbol_health.cache_clear()


def _seed_candidate_is_healthy(ticker: str) -> bool:
    entry = _recent_seed_symbol_health().get(str(ticker or "").strip().upper())
    if not entry:
        return True
    return bool(entry.get("available", True))


def _seed_candidate_health_rank(ticker: str) -> int:
    entry = _recent_seed_symbol_health().get(str(ticker or "").strip().upper())
    if not entry:
        return 1
    return 2 if bool(entry.get("available", True)) else 0


def _seed_candidate_sector_type(item: dict[str, object]) -> str | None:
    sector = str(item.get("sector") or "").strip()
    industry = str(item.get("industry") or "").strip()
    if not sector and not industry:
        return None
    return detect_sector_type(sector, industry)


def _seed_candidate_is_dcf_suitable(item: dict[str, object]) -> bool:
    sector_type = _seed_candidate_sector_type(item)
    if sector_type is None:
        return True
    return sector_type not in {FINANCIAL, REIT, MINING}


def _seed_candidate_richness(item: dict[str, object]) -> int:
    history_years = max(int(item.get("history_years") or 0), 0)
    return (
        3 * int(bool(item.get("has_fundamentals")))
        + 2 * int(bool(str(item.get("sector") or "").strip()))
        + 2 * int(bool(str(item.get("industry") or "").strip()))
        + min(history_years, 10)
    )


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



@lru_cache(maxsize=28)
def _load_search_shard(letter: str) -> tuple[dict[str, object], ...]:
    shard_path = _CACHE_DIR / ("search_shard_" + letter + ".json")
    if shard_path.exists():
        try:
            data = json.loads(shard_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return tuple(data)
        except (OSError, json.JSONDecodeError):
            pass
    return ()


def _matched_candidates(query_key: str, items) -> list[dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for item in items:
        if _match_score(query_key, item) is not None:
            ticker = str(item["ticker"])
            results.setdefault(ticker, item)
    return list(results.values())


def _search_candidates(query: str) -> list[dict[str, object]]:
    query_key = _normalise_search_text(query)
    if not query_key:
        return []
    first = query_key[0].lower() if query_key[0].isalpha() else "misc"
    shard = _load_search_shard(first)
    local_matches: list[dict[str, object]] = []
    if shard:
        local_matches = _matched_candidates(query_key, shard)
        if local_matches:
            return local_matches
    index_matches = _matched_candidates(query_key, _ticker_search_index())
    if index_matches:
        return index_matches
    return _matched_candidates(query_key, _live_search_items(query))


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
    if _PREBUILT_INDEX_PATH.exists():
        try:
            data = json.loads(_PREBUILT_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return tuple(data)
        except (OSError, json.JSONDecodeError):
            pass
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


@lru_cache(maxsize=1)
def available_exchanges() -> tuple[str, ...]:
    preferred = (
        "US",
        "NYSE",
        "NASDAQ",
        "LSE",
        "XETRA",
        "PA",
        "SW",
        "TO",
        "V",
        "KO",
        "KQ",
        "TSE",
        "HK",
        "HKEX",
        "AU",
    )
    seen: set[str] = set()
    if _EXCHANGE_MANIFEST_PATH.exists():
        try:
            payload = json.loads(_EXCHANGE_MANIFEST_PATH.read_text(encoding="utf-8"))
            rows = payload.get("exchanges") if isinstance(payload, dict) else payload
            if isinstance(rows, list):
                seen = {str(item or "").strip().upper() for item in rows if str(item or "").strip()}
        except (OSError, json.JSONDecodeError):
            seen = set()
    if not seen:
        seen = {
            str(item.get("exchange") or "").strip().upper()
            for item in _ticker_search_index()
            if str(item.get("exchange") or "").strip()
        }
    ordered = [exchange for exchange in preferred if exchange in seen or exchange in {"NYSE", "NASDAQ", "HKEX", "TSE"}]
    ordered.extend(exchange for exchange in sorted(seen) if exchange not in set(ordered))
    return tuple(ordered)


@lru_cache(maxsize=1)
def _cached_primary_listing_hints() -> dict[str, dict[str, str]]:
    hints: dict[str, dict[str, str]] = {}
    for payload_path in sorted(_CACHE_DIR.glob("eodhd_fund_*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        general = data.get("General")
        if not isinstance(general, dict):
            continue

        alias_ticker = _compose_ticker(
            str(general.get("Code") or "").strip().upper(),
            str(general.get("Exchange") or "").strip().upper(),
        )
        primary_ticker = str(general.get("PrimaryTicker") or alias_ticker).strip().upper()
        if not alias_ticker or not primary_ticker:
            continue

        hints[alias_ticker] = {
            "primary_ticker": primary_ticker,
            "isin": str(general.get("ISIN") or "").strip().upper(),
        }
    return hints


def _seedable_issuer_key(item: dict[str, object], hints: dict[str, dict[str, str]]) -> str:
    ticker = str(item.get("ticker") or "").strip().upper()
    hint = hints.get(ticker, {})
    primary_ticker = str(item.get("primary_ticker") or hint.get("primary_ticker") or "").strip().upper()
    if primary_ticker:
        return f"primary:{primary_ticker}"

    isin = str(item.get("isin") or hint.get("isin") or "").strip().upper()
    if isin:
        return f"isin:{isin}"

    name_key = str(item.get("name_key") or "").strip()
    country = str(item.get("country") or "").strip().upper()
    if name_key:
        return f"name:{name_key}:{country}"

    return f"ticker:{ticker}"


def invalidate_ticker_search_index() -> None:
    _ticker_search_index.cache_clear()
    available_exchanges.cache_clear()
    _cached_primary_listing_hints.cache_clear()
    _recent_seed_symbol_health.cache_clear()


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
            -_seed_candidate_health_rank(str(item.get("ticker") or "")),
            -_seed_candidate_richness(item),
            -_safe_float(item.get("market_cap")),
            0 if bool(item.get("is_primary")) else 1,
            source_priority.get(str(item.get("source") or ""), 9),
            str(item.get("name") or "").upper(),
            str(item.get("ticker") or ""),
        ),
    )

    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    seen_issuers: set[str] = set()
    seen_companies: set[str] = set()
    available_tickers = {str(item.get("ticker") or "").strip().upper() for item in ranked_items}
    primary_hints = _cached_primary_listing_hints()
    max_items = int(limit) if limit is not None and int(limit) > 0 else None
    for item in ranked_items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        if not _seed_candidate_is_healthy(ticker):
            continue
        if not _seed_candidate_is_dcf_suitable(item):
            continue
        instrument_type = str(item.get("instrument_type") or "").strip().lower()
        if common_stock_only and instrument_type not in allowed_types:
            continue
        primary_ticker = str(
            item.get("primary_ticker")
            or (primary_hints.get(ticker) or {}).get("primary_ticker")
            or ""
        ).strip().upper()
        if primary_ticker and primary_ticker != ticker and primary_ticker in available_tickers:
            continue
        issuer_key = _seedable_issuer_key(item, primary_hints)
        if issuer_key in seen_issuers:
            continue
        company_key = str(item.get("name_key") or "").strip()
        if company_key and company_key in seen_companies:
            continue
        seen.add(ticker)
        seen_issuers.add(issuer_key)
        if company_key:
            seen_companies.add(company_key)
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


def _instrument_priority(item: dict[str, object]) -> int:
    instrument_type = str(item.get("instrument_type") or "").strip().lower()
    if instrument_type in {"common stock", "common shares", "ordinary shares"}:
        return 0
    if instrument_type in {"depositary receipt", "adr"}:
        return 1
    if not instrument_type:
        return 2
    return 3


def _search_source_priority(item: dict[str, object]) -> int:
    source = str(item.get("source") or "").strip().lower()
    return {
        "search-cache": 0,
        "exchange-cache": 1,
        "cache": 2,
        "live": 3,
        "supported": 4,
    }.get(source, 9)


def _listing_exchange_priority(item: dict[str, object]) -> int:
    exchange = str(item.get("exchange") or "").strip().upper()
    if exchange in {"PINK", "OTC", "OTCQB", "OTCQX", "GREY"}:
        return 3
    if exchange in {"US", "NYSE", "NASDAQ", "LSE", "XETRA", "AS", "PA", "SW", "TO", "V", "KO", "KQ", "TSE", "HK", "HKEX", "AU"}:
        return 0
    return 1


def search_tickers(query: str, limit: int = 12, exchange: str = "auto") -> list[dict[str, str]]:
    query_key = _normalise_search_text(query)
    if not query_key:
        return []

    exchange_key = str(exchange or "auto").strip().upper()
    matches: list[tuple[tuple[object, ...], dict[str, object]]] = []
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
        instrument_priority = _instrument_priority(item)
        primary_penalty = 0 if bool(item.get("is_primary")) else 1
        listing_exchange_priority = _listing_exchange_priority(item)
        fundamentals_penalty = 0 if bool(item.get("has_fundamentals")) else 1
        source_priority = _search_source_priority(item)
        market_cap_rank = -_safe_float(item.get("market_cap"))
        history_rank = -max(int(item.get("history_years") or 0), 0)
        matches.append((
            (
                score[0],
                exchange_penalty,
                listing_exchange_priority,
                primary_penalty,
                instrument_priority,
                fundamentals_penalty,
                source_priority,
                market_cap_rank,
                history_rank,
                score[1],
                score[2],
            ),
            item,
        ))

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

    if prefix_matches:
        preferred = search_tickers(value, limit=1, exchange=exchange)
        if preferred:
            return str(preferred[0]["ticker"])

    return None