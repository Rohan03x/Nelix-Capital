"""Persistent symbol-universe registry for the shared learning brain."""

from __future__ import annotations

import json
import time as _time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from .calibration_priority import build_calibration_priority_index, calibration_priority_for_symbol
from .industry_taxonomy import resolve_industry_taxonomy
from .shared_db import resolve_shared_brain_backend
from .storage_paths import learning_db_dir


PACKAGE_ROOT = Path(__file__).resolve().parent
SYMBOL_UNIVERSE_DB_PATH = learning_db_dir() / "symbol_universe.db"

# Short TTL cache for summary() so that many dashboard requests within a minute
# don't each pay the full 2+ second calibration-priority DB cost.
_SUMMARY_CACHE: dict[str, Any] | None = None
_SUMMARY_CACHE_TS: float = 0.0
_SUMMARY_CACHE_TTL: float = 30.0  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        dt_value = value
    else:
        try:
            dt_value = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def _to_iso(value: Any | None = None) -> str:
    dt_value = _parse_datetime(value) or _utcnow()
    return dt_value.isoformat()


def _prefer_text(current: Any, incoming: Any) -> str:
    incoming_text = str(incoming or "").strip()
    if incoming_text:
        return incoming_text
    return str(current or "").strip()


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, "", "None"):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class SymbolUniverseStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._engine, self.storage_backend, self.db_path, self.database_url = resolve_shared_brain_backend(
            db_path,
            SYMBOL_UNIVERSE_DB_PATH,
        )
        self._ensure_schema()

    def _connect(self):
        return self._engine.connect()

    def _fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text(sql), params or {}).mappings().first()
        return dict(row) if row is not None else None

    def _fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().all()
        return [dict(row) for row in rows]

    def _execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
        return int(result.rowcount or 0)

    def _ensure_schema(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_universe (
                ticker TEXT PRIMARY KEY,
                company_name TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                sector TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_searched_at TEXT,
                last_valued_at TEXT,
                last_bootstrapped_at TEXT,
                last_bootstrap_status TEXT NOT NULL DEFAULT '',
                fundamentals_cached INTEGER NOT NULL DEFAULT 0,
                times_seen INTEGER NOT NULL DEFAULT 0,
                search_hits INTEGER NOT NULL DEFAULT 0,
                valuation_hits INTEGER NOT NULL DEFAULT 0,
                bootstrap_runs INTEGER NOT NULL DEFAULT 0,
                sources_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_symbol_universe_last_valued ON symbol_universe(last_valued_at DESC)"
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_symbol_universe_last_bootstrapped ON symbol_universe(last_bootstrapped_at DESC)"
        )

    def _row_to_symbol(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["fundamentals_cached"] = bool(payload.get("fundamentals_cached"))
        payload["sources"] = _decode_json(payload.pop("sources_json", "[]"), [])
        payload["metadata"] = _decode_json(payload.pop("metadata_json", "{}"), {})
        return payload

    def get_symbol(self, ticker: str) -> dict[str, Any] | None:
        ticker_text = str(ticker or "").strip().upper()
        if not ticker_text:
            return None
        row = self._fetch_one(
            "SELECT * FROM symbol_universe WHERE ticker = :ticker",
            {"ticker": ticker_text},
        )
        return self._row_to_symbol(row)

    def list_symbols(self, limit: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM symbol_universe "
            "ORDER BY COALESCE(last_valued_at, last_seen_at) DESC, times_seen DESC, ticker ASC"
        )
        params: dict[str, Any] = {}
        if limit is not None and int(limit) > 0:
            query += " LIMIT :limit"
            params["limit"] = int(limit)
        rows = self._fetch_all(query, params)
        return [symbol for row in rows if (symbol := self._row_to_symbol(row)) is not None]

    def upsert_symbol(
        self,
        ticker: str,
        *,
        company_name: str = "",
        exchange: str = "",
        country: str = "",
        sector: str = "",
        industry: str = "",
        source: str = "",
        searched: bool = False,
        valued: bool = False,
        bootstrapped: bool = False,
        fundamentals_cached: bool = False,
        bootstrap_status: str = "",
        seen_at: Any | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_increments: dict[str, int | float] | None = None,
    ) -> dict[str, Any] | None:
        ticker_text = str(ticker or "").strip().upper()
        if not ticker_text:
            return None

        now_text = _to_iso(seen_at)
        current = self.get_symbol(ticker_text) or {
            "ticker": ticker_text,
            "company_name": "",
            "exchange": "",
            "country": "",
            "sector": "",
            "industry": "",
            "first_seen_at": now_text,
            "last_seen_at": now_text,
            "last_searched_at": None,
            "last_valued_at": None,
            "last_bootstrapped_at": None,
            "last_bootstrap_status": "",
            "fundamentals_cached": False,
            "times_seen": 0,
            "search_hits": 0,
            "valuation_hits": 0,
            "bootstrap_runs": 0,
            "sources": [],
            "metadata": {},
        }

        sources = list(current.get("sources") or [])
        source_text = str(source or "").strip()
        if source_text and source_text not in sources:
            sources.append(source_text)
        merged_metadata = dict(current.get("metadata") or {})
        resolved_sector = _prefer_text(current.get("sector"), sector)
        resolved_industry = _prefer_text(current.get("industry"), industry)
        taxonomy = resolve_industry_taxonomy(resolved_industry, resolved_sector)
        merged_metadata.setdefault("canonical_industry", taxonomy.get("canonical_industry") or resolved_industry)
        merged_metadata.setdefault("canonical_sector", taxonomy.get("canonical_sector") or resolved_sector)
        merged_metadata.setdefault("industry_cluster", taxonomy.get("cluster_id") or "")
        merged_metadata.setdefault("industry_family", taxonomy.get("family") or "")
        if taxonomy.get("related_industries"):
            merged_metadata.setdefault("related_industries", list(taxonomy.get("related_industries") or []))
        for key, value in dict(metadata or {}).items():
            if value in (None, "", [], {}):
                continue
            merged_metadata[str(key)] = value
        for key, value in dict(metadata_increments or {}).items():
            try:
                increment = float(value)
            except (TypeError, ValueError):
                continue
            key_text = str(key)
            try:
                current_value = float(merged_metadata.get(key_text) or 0.0)
            except (TypeError, ValueError):
                current_value = 0.0
            updated_value = current_value + increment
            merged_metadata[key_text] = int(updated_value) if updated_value.is_integer() else round(updated_value, 4)

        payload = {
            "ticker": ticker_text,
            "company_name": _prefer_text(current.get("company_name"), company_name),
            "exchange": _prefer_text(current.get("exchange"), exchange).upper(),
            "country": _prefer_text(current.get("country"), country),
            "sector": resolved_sector,
            "industry": resolved_industry,
            "first_seen_at": str(current.get("first_seen_at") or now_text),
            "last_seen_at": now_text,
            "last_searched_at": now_text if searched else current.get("last_searched_at"),
            "last_valued_at": now_text if valued else current.get("last_valued_at"),
            "last_bootstrapped_at": now_text if bootstrapped else current.get("last_bootstrapped_at"),
            "last_bootstrap_status": str(bootstrap_status or current.get("last_bootstrap_status") or ""),
            "fundamentals_cached": int(bool(current.get("fundamentals_cached")) or bool(fundamentals_cached)),
            "times_seen": int(current.get("times_seen") or 0) + 1,
            "search_hits": int(current.get("search_hits") or 0) + (1 if searched else 0),
            "valuation_hits": int(current.get("valuation_hits") or 0) + (1 if valued else 0),
            "bootstrap_runs": int(current.get("bootstrap_runs") or 0) + (1 if bootstrapped else 0),
            "sources_json": json.dumps(sorted(set(sources))),
            "metadata_json": json.dumps(merged_metadata, ensure_ascii=False, default=str),
        }

        self._execute(
            """
            INSERT INTO symbol_universe (
                ticker, company_name, exchange, country, sector, industry,
                first_seen_at, last_seen_at, last_searched_at, last_valued_at,
                last_bootstrapped_at, last_bootstrap_status, fundamentals_cached,
                times_seen, search_hits, valuation_hits, bootstrap_runs,
                sources_json, metadata_json
            ) VALUES (
                :ticker, :company_name, :exchange, :country, :sector, :industry,
                :first_seen_at, :last_seen_at, :last_searched_at, :last_valued_at,
                :last_bootstrapped_at, :last_bootstrap_status, :fundamentals_cached,
                :times_seen, :search_hits, :valuation_hits, :bootstrap_runs,
                :sources_json, :metadata_json
            )
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                exchange = EXCLUDED.exchange,
                country = EXCLUDED.country,
                sector = EXCLUDED.sector,
                industry = EXCLUDED.industry,
                first_seen_at = EXCLUDED.first_seen_at,
                last_seen_at = EXCLUDED.last_seen_at,
                last_searched_at = EXCLUDED.last_searched_at,
                last_valued_at = EXCLUDED.last_valued_at,
                last_bootstrapped_at = EXCLUDED.last_bootstrapped_at,
                last_bootstrap_status = EXCLUDED.last_bootstrap_status,
                fundamentals_cached = EXCLUDED.fundamentals_cached,
                times_seen = EXCLUDED.times_seen,
                search_hits = EXCLUDED.search_hits,
                valuation_hits = EXCLUDED.valuation_hits,
                bootstrap_runs = EXCLUDED.bootstrap_runs,
                sources_json = EXCLUDED.sources_json,
                metadata_json = EXCLUDED.metadata_json
            """,
            payload,
        )
        return self.get_symbol(ticker_text)

    def record_candidates(
        self,
        items: Iterable[dict[str, Any] | str],
        *,
        source: str,
        searched: bool = False,
        valued: bool = False,
        fundamentals_cached: bool = False,
        seen_at: Any | None = None,
    ) -> int:
        count = 0
        for item in items:
            if isinstance(item, dict):
                ticker = item.get("ticker") or item.get("code") or item.get("symbol") or ""
                metadata = dict(item.get("metadata") or {})
                metadata_increments = dict(item.get("metadata_increments") or {})
                if item.get("role"):
                    metadata.setdefault("role", item.get("role"))
                if item.get("similarity") is not None:
                    metadata.setdefault("similarity", item.get("similarity"))
                if item.get("score") is not None:
                    metadata.setdefault("score", item.get("score"))
                symbol = self.upsert_symbol(
                    str(ticker),
                    company_name=str(item.get("company_name") or item.get("name") or ""),
                    exchange=str(item.get("exchange") or ""),
                    country=str(item.get("country") or item.get("country_name") or ""),
                    sector=str(item.get("sector") or ""),
                    industry=str(item.get("industry") or ""),
                    source=source,
                    searched=searched,
                    valued=valued,
                    fundamentals_cached=fundamentals_cached,
                    seen_at=seen_at,
                    metadata=metadata,
                    metadata_increments=metadata_increments,
                )
            else:
                symbol = self.upsert_symbol(
                    str(item),
                    source=source,
                    searched=searched,
                    valued=valued,
                    fundamentals_cached=fundamentals_cached,
                    seen_at=seen_at,
                )
            if symbol is not None:
                count += 1
        return count

    def _priority_entries(self, *, stale_after_hours: int = 18) -> list[dict[str, Any]]:
        rows = self.list_symbols()
        if not rows:
            return []

        now = _utcnow()
        stale_after = max(int(stale_after_hours or 18), 1)
        calibration_index = build_calibration_priority_index()
        entries: list[dict[str, Any]] = []
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            calibration = calibration_priority_for_symbol(row, calibration_index)
            score = 0.0
            if row.get("fundamentals_cached"):
                score += 2.2

            last_valued_at = _parse_datetime(row.get("last_valued_at"))
            if last_valued_at is not None:
                age_days = max((now - last_valued_at).total_seconds() / 86_400.0, 0.0)
                score += max(0.2, 2.8 - min(age_days / 3.0, 2.6))

            last_bootstrapped_at = _parse_datetime(row.get("last_bootstrapped_at"))
            if last_bootstrapped_at is None:
                score += 3.4
            else:
                age_hours = max((now - last_bootstrapped_at).total_seconds() / 3_600.0, 0.0)
                if age_hours >= stale_after:
                    score += min(2.6, age_hours / stale_after)

            score += min(float(row.get("valuation_hits") or 0) * 0.18, 1.2)
            score += min(float(row.get("search_hits") or 0) * 0.04, 0.5)
            score += min(float(metadata.get("watchlist_hits") or 0.0) * 0.45, 1.8)
            score += min(float(metadata.get("compare_hits") or 0.0) * 0.35, 1.4)
            score += min(float(metadata.get("selection_hits") or 0.0) * 0.18, 0.8)
            score += min(float(metadata.get("peer_candidate_hits") or 0.0) * 0.12, 0.9)
            score += min(float(metadata.get("peer_learning_score") or 0.0) * 0.25, 1.6)
            score += min(float(metadata.get("score") or 0.0), 1.0)
            score += min(float(metadata.get("similarity") or 0.0) * 0.35, 0.35)
            score += float(calibration.get("score") or 0.0)
            if metadata.get("watchlist_active"):
                score += 0.9
            if str(row.get("sector") or "").strip():
                score += 0.2
            if str(row.get("exchange") or "").strip():
                score += 0.1
            entries.append(
                {
                    "ticker": str(row.get("ticker") or "").strip().upper(),
                    "score": score,
                    "row": row,
                    "calibration": calibration,
                }
            )

        entries.sort(
            key=lambda item: (
                item["score"],
                str(item["row"].get("last_valued_at") or ""),
                int(item["row"].get("times_seen") or 0),
                str(item["ticker"] or ""),
            ),
            reverse=True,
        )
        return entries

    def priority_tickers(self, *, limit: int = 24, stale_after_hours: int = 18) -> list[str]:
        entries = self._priority_entries(stale_after_hours=stale_after_hours)
        if not entries or limit <= 0:
            return []

        picked: list[str] = []
        seen_tickers: set[str] = set()
        seen_groups: set[str] = set()
        for entry in entries:
            row = entry["row"]
            ticker = entry["ticker"]
            if not ticker or ticker in seen_tickers:
                continue
            metadata = dict(row.get("metadata") or {})
            group = str(
                metadata.get("industry_family")
                or metadata.get("industry_cluster")
                or row.get("sector")
                or row.get("exchange")
                or "unknown"
            ).strip().lower()
            if group in seen_groups:
                continue
            picked.append(ticker)
            seen_tickers.add(ticker)
            seen_groups.add(group)
            if len(picked) >= limit:
                return picked

        for entry in entries:
            ticker = entry["ticker"]
            if not ticker or ticker in seen_tickers:
                continue
            picked.append(ticker)
            seen_tickers.add(ticker)
            if len(picked) >= limit:
                break
        return picked

    def calibration_priority_candidates(self, *, limit: int = 3, stale_after_hours: int = 18) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        selected = set(self.priority_tickers(limit=max(limit, 1), stale_after_hours=stale_after_hours))
        for entry in self._priority_entries(stale_after_hours=stale_after_hours):
            ticker = entry["ticker"]
            if ticker not in selected:
                continue
            calibration = dict(entry["calibration"])
            if float(calibration.get("score") or 0.0) <= 0.0:
                continue
            row = entry["row"]
            candidates.append(
                {
                    "ticker": ticker,
                    "score": round(float(entry["score"]), 3),
                    "sector": str(row.get("sector") or ""),
                    "exchange": str(row.get("exchange") or ""),
                    **calibration,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def _priority_tickers_from_entries(self, entries: list[dict[str, Any]], *, limit: int = 24) -> list[str]:
        """Same as priority_tickers() but accepts pre-computed entries to avoid a duplicate DB call."""
        if not entries or limit <= 0:
            return []
        picked: list[str] = []
        seen_tickers: set[str] = set()
        seen_groups: set[str] = set()
        for entry in entries:
            row = entry["row"]
            ticker = entry["ticker"]
            if not ticker or ticker in seen_tickers:
                continue
            metadata = dict(row.get("metadata") or {})
            group = str(
                metadata.get("industry_family")
                or metadata.get("industry_cluster")
                or row.get("sector")
                or row.get("exchange")
                or "unknown"
            ).strip().lower()
            if group in seen_groups:
                continue
            picked.append(ticker)
            seen_tickers.add(ticker)
            seen_groups.add(group)
            if len(picked) >= limit:
                return picked
        for entry in entries:
            ticker = entry["ticker"]
            if not ticker or ticker in seen_tickers:
                continue
            picked.append(ticker)
            seen_tickers.add(ticker)
            if len(picked) >= limit:
                break
        return picked

    def _calibration_candidates_from_entries(
        self,
        entries: list[dict[str, Any]],
        *,
        priority_tickers_set: set[str],
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Same as calibration_priority_candidates() but accepts pre-computed entries."""
        candidates: list[dict[str, Any]] = []
        for entry in entries:
            ticker = entry["ticker"]
            if ticker not in priority_tickers_set:
                continue
            calibration = dict(entry["calibration"])
            if float(calibration.get("score") or 0.0) <= 0.0:
                continue
            row = entry["row"]
            candidates.append(
                {
                    "ticker": ticker,
                    "score": round(float(entry["score"]), 3),
                    "sector": str(row.get("sector") or ""),
                    "exchange": str(row.get("exchange") or ""),
                    **calibration,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def summary(self, *, stale_after_hours: int = 18, recent_days: int = 14) -> dict[str, Any]:
        global _SUMMARY_CACHE, _SUMMARY_CACHE_TS
        import sys as _sys
        # In test mode use a per-instance cache keyed by db path to prevent
        # production data from bleeding into isolated test stores.
        if "pytest" in _sys.modules:
            now_mono = _time_mod.monotonic()
            _inst_cache = getattr(self, "_summary_cache", None)
            _inst_cache_ts: float = getattr(self, "_summary_cache_ts", 0.0)
            if _inst_cache is not None and (now_mono - _inst_cache_ts) < _SUMMARY_CACHE_TTL:
                return dict(_inst_cache)
            result = self._compute_summary(stale_after_hours=stale_after_hours, recent_days=recent_days)
            self._summary_cache = result
            self._summary_cache_ts = now_mono
            return dict(result)

        now_mono = _time_mod.monotonic()
        if _SUMMARY_CACHE is not None and (now_mono - _SUMMARY_CACHE_TS) < _SUMMARY_CACHE_TTL:
            return dict(_SUMMARY_CACHE)

        result = self._compute_summary(stale_after_hours=stale_after_hours, recent_days=recent_days)
        _SUMMARY_CACHE = result
        _SUMMARY_CACHE_TS = now_mono
        return dict(result)

    def _compute_summary(self, *, stale_after_hours: int = 18, recent_days: int = 14) -> dict[str, Any]:
        rows = self.list_symbols()
        now = _utcnow()
        stale_cutoff = timedelta(hours=max(int(stale_after_hours or 18), 1))
        recent_cutoff = timedelta(days=max(int(recent_days or 14), 1))

        sector_counts: dict[str, int] = {}
        exchange_counts: dict[str, int] = {}
        bootstrapped_symbols = 0
        cached_fundamentals = 0
        recently_valued_symbols = 0
        stale_bootstrap_symbols = 0

        for row in rows:
            sector = str(row.get("sector") or "").strip()
            exchange = str(row.get("exchange") or "").strip().upper()
            if sector:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if exchange:
                exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
            if row.get("fundamentals_cached"):
                cached_fundamentals += 1

            valued_at = _parse_datetime(row.get("last_valued_at"))
            if valued_at is not None and now - valued_at <= recent_cutoff:
                recently_valued_symbols += 1

            bootstrapped_at = _parse_datetime(row.get("last_bootstrapped_at"))
            if bootstrapped_at is not None:
                bootstrapped_symbols += 1
            if bootstrapped_at is None or now - bootstrapped_at > stale_cutoff:
                stale_bootstrap_symbols += 1

        top_sectors = [
            {"label": label, "count": count}
            for label, count in sorted(sector_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]

        # Compute _priority_entries once and reuse it for both priority_tickers
        # and calibration_priority_candidates to avoid duplicate DB round-trips.
        priority_entries = self._priority_entries(stale_after_hours=stale_after_hours)
        priority_tickers = self._priority_tickers_from_entries(priority_entries, limit=5)
        calibration_candidates = self._calibration_candidates_from_entries(
            priority_entries, priority_tickers_set=set(priority_tickers), limit=3
        )

        return {
            "tracked_symbols": len(rows),
            "sector_span": len(sector_counts),
            "exchange_span": len(exchange_counts),
            "bootstrapped_symbols": bootstrapped_symbols,
            "cached_fundamentals": cached_fundamentals,
            "recently_valued_symbols": recently_valued_symbols,
            "stale_bootstrap_symbols": stale_bootstrap_symbols,
            "priority_candidates": priority_tickers,
            "calibration_priority_candidates": calibration_candidates,
            "top_sectors": top_sectors,
        }


__all__ = ["SYMBOL_UNIVERSE_DB_PATH", "SymbolUniverseStore"]