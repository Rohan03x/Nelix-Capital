from __future__ import annotations

import json
import re
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_SPACE_RE = re.compile(r"[^A-Z0-9]+")


def _normalise_search_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.upper()).strip()


@lru_cache(maxsize=1)
def _cache_dir() -> Path:
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for base in candidates:
        cache_dir = base / "webapp" / "data" / "cache"
        if cache_dir.exists():
            return cache_dir
    return Path.cwd() / "webapp" / "data" / "cache"


@lru_cache(maxsize=28)
def _load_search_shard(letter: str) -> tuple[dict[str, object], ...]:
    shard_path = _cache_dir() / f"search_shard_{letter}.json"
    try:
        rows = json.loads(shard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(rows, list):
        return ()
    return tuple(row for row in rows if isinstance(row, dict))


def _match_score(query_key: str, item: dict[str, object]) -> tuple[int, int, str] | None:
    ticker_key = str(item.get("ticker_key") or "")
    code_key = str(item.get("code_key") or "")
    name_key = str(item.get("name_key") or "")
    name_words = [str(word) for word in item.get("name_words") or []]

    if query_key in {ticker_key, code_key, name_key}:
        return (0, len(str(item.get("name") or "")), str(item.get("ticker") or ""))
    if ticker_key.startswith(query_key) or code_key.startswith(query_key):
        return (1, len(str(item.get("ticker") or "")), str(item.get("ticker") or ""))
    if name_key.startswith(query_key):
        return (2, len(str(item.get("name") or "")), str(item.get("ticker") or ""))
    if any(word.startswith(query_key) for word in name_words):
        return (3, len(str(item.get("name") or "")), str(item.get("ticker") or ""))
    if query_key in str(item.get("search_text") or ""):
        return (4, len(str(item.get("name") or "")), str(item.get("ticker") or ""))
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


def _source_priority(item: dict[str, object]) -> int:
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


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def search_tickers_payload(query: str, limit: int = 20, exchange: str = "auto") -> dict[str, list[dict[str, str]]]:
    query_key = _normalise_search_text(query)
    if not query_key:
        return {"results": []}

    first = query_key[0].lower() if query_key[0].isalpha() else "misc"
    exchange_key = str(exchange or "auto").strip().upper()
    matches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    seen: set[str] = set()

    for item in _load_search_shard(first):
        score = _match_score(query_key, item)
        if score is None:
            continue
        ticker = str(item.get("ticker") or "")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

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

        matches.append((
            (
                score[0],
                exchange_penalty,
                _listing_exchange_priority(item),
                0 if bool(item.get("is_primary")) else 1,
                _instrument_priority(item),
                0 if bool(item.get("has_fundamentals")) else 1,
                _source_priority(item),
                -_safe_float(item.get("market_cap")),
                -max(int(item.get("history_years") or 0), 0),
                score[1],
                score[2],
            ),
            item,
        ))

    matches.sort(key=lambda pair: pair[0])
    results: list[dict[str, str]] = []
    for _, item in matches[: max(1, min(int(limit or 20), 25))]:
        results.append({
            "ticker": str(item.get("ticker") or ""),
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or ""),
            "exchange": str(item.get("exchange") or ""),
            "country": str(item.get("country") or ""),
        })
    return {"results": results}


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        query = params.get("q", [""])[0]
        exchange = params.get("exchange", ["auto"])[0]
        try:
            limit = int(params.get("limit", ["20"])[0])
        except (TypeError, ValueError):
            limit = 20

        body = json.dumps(search_tickers_payload(query, limit=limit, exchange=exchange), separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=60, s-maxage=86400, stale-while-revalidate=604800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)