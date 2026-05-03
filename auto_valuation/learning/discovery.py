"""Persistent discovery signals that feed the shared-brain symbol universe."""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from auto_valuation.config import LEARNING_CONFIG

from .shared_db import resolve_shared_brain_backend
from .storage_paths import learning_db_dir
from .universe import SymbolUniverseStore


PACKAGE_ROOT = Path(__file__).resolve().parent
DISCOVERY_DB_PATH = learning_db_dir() / "discovery.db"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> datetime | None:
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


def _pair_decay_profile(last_seen_at: Any) -> tuple[float, float]:
    seen_at = _parse_iso_datetime(last_seen_at)
    if seen_at is None:
        return 0.0, 1.0

    age_days = max((datetime.now(timezone.utc) - seen_at).total_seconds() / 86_400.0, 0.0)
    half_life_days = max(float(LEARNING_CONFIG.get("pair_relationship_half_life_days", 45.0) or 45.0), 1.0)
    decay_floor = min(max(float(LEARNING_CONFIG.get("pair_relationship_decay_floor", 0.2) or 0.2), 0.0), 1.0)
    multiplier = max(decay_floor, min(math.pow(0.5, age_days / half_life_days), 1.0))
    return round(age_days, 2), round(multiplier, 4)


def _clean_symbol_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    item = dict(payload or {})
    ticker = str(item.get("ticker") or item.get("symbol") or item.get("code") or "").strip().upper()
    return {
        "ticker": ticker,
        "company_name": str(item.get("company_name") or item.get("name") or "").strip(),
        "exchange": str(item.get("exchange") or "").strip().upper(),
        "country": str(item.get("country") or item.get("country_name") or "").strip(),
        "sector": str(item.get("sector") or "").strip(),
        "industry": str(item.get("industry") or "").strip(),
    }


class DiscoveryStore:
    def __init__(
        self,
        db_path: Path | str | None = None,
        universe_store: SymbolUniverseStore | None = None,
    ) -> None:
        self._engine, self.storage_backend, self.db_path, self.database_url = resolve_shared_brain_backend(
            db_path,
            DISCOVERY_DB_PATH,
        )
        self.universe_store = universe_store or SymbolUniverseStore()
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
            CREATE TABLE IF NOT EXISTS watchlist_items (
                ticker TEXT PRIMARY KEY,
                company_name TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                sector TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                last_touched_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS search_impressions (
                impression_id TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT '',
                selected_ticker TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS manual_compare_events (
                compare_id TEXT PRIMARY KEY,
                subject_ticker TEXT NOT NULL,
                peer_ticker TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS peer_relationships (
                subject_ticker TEXT NOT NULL,
                peer_ticker TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                auto_peer_hits INTEGER NOT NULL DEFAULT 0,
                manual_compare_hits INTEGER NOT NULL DEFAULT 0,
                signal_points REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(subject_ticker, peer_ticker)
            )
            """
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_relationships_subject ON peer_relationships(subject_ticker, last_seen_at DESC)"
        )

    def list_watchlist(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            "SELECT * FROM watchlist_items ORDER BY last_touched_at DESC, ticker ASC LIMIT :limit",
            {"limit": max(int(limit or 20), 1)},
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["payload"] = json.loads(str(payload.pop("payload_json", "{}")) or "{}")
            items.append(payload)
        return items

    def add_to_watchlist(self, item: dict[str, Any]) -> dict[str, Any] | None:
        symbol = _clean_symbol_payload(item)
        if not symbol["ticker"]:
            return None
        now_text = _utcnow_iso()
        sync_mode = str((item or {}).get("_device_sync_mode") or "").strip().lower()
        is_device_replay = sync_mode == "client-replay"
        current = next((entry for entry in self.list_watchlist(limit=200) if entry.get("ticker") == symbol["ticker"]), None)
        added_at = str((current or {}).get("added_at") or now_text)
        payload = {
            **symbol,
            "added_at": added_at,
            "last_touched_at": now_text,
            "payload_json": json.dumps(symbol, ensure_ascii=False),
        }
        self._execute(
            """
            INSERT INTO watchlist_items (
                ticker, company_name, exchange, country, sector, industry,
                added_at, last_touched_at, payload_json
            ) VALUES (
                :ticker, :company_name, :exchange, :country, :sector, :industry,
                :added_at, :last_touched_at, :payload_json
            )
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                exchange = EXCLUDED.exchange,
                country = EXCLUDED.country,
                sector = EXCLUDED.sector,
                industry = EXCLUDED.industry,
                added_at = EXCLUDED.added_at,
                last_touched_at = EXCLUDED.last_touched_at,
                payload_json = EXCLUDED.payload_json
            """,
            payload,
        )

        self.universe_store.upsert_symbol(
            symbol["ticker"],
            company_name=symbol["company_name"],
            exchange=symbol["exchange"],
            country=symbol["country"],
            sector=symbol["sector"],
            industry=symbol["industry"],
            source="watchlist",
            metadata={"watchlist_active": True, "watchlist_added_at": added_at},
            metadata_increments={} if (current and is_device_replay) else {"watchlist_hits": 1},
        )
        return next((entry for entry in self.list_watchlist(limit=200) if entry.get("ticker") == symbol["ticker"]), None)

    def remove_from_watchlist(self, ticker: str) -> bool:
        ticker_text = str(ticker or "").strip().upper()
        if not ticker_text:
            return False
        rowcount = self._execute(
            "DELETE FROM watchlist_items WHERE ticker = :ticker",
            {"ticker": ticker_text},
        )
        self.universe_store.upsert_symbol(
            ticker_text,
            source="watchlist-remove",
            metadata={"watchlist_active": False, "watchlist_removed_at": _utcnow_iso()},
        )
        return bool(rowcount)

    def _row_to_peer_relationship(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["payload"] = json.loads(str(payload.pop("payload_json", "{}")) or "{}")
        auto_peer_hits = int(payload.get("auto_peer_hits") or 0)
        manual_compare_hits = int(payload.get("manual_compare_hits") or 0)
        signal_points = float(payload.get("signal_points") or 0.0)
        age_days, decay_multiplier = _pair_decay_profile(payload.get("last_seen_at"))
        raw_strength = min(signal_points * 0.25 + manual_compare_hits * 1.75 + auto_peer_hits * 0.45, 12.0)
        payload["pair_hits"] = auto_peer_hits + manual_compare_hits
        payload["pair_strength_score_raw"] = round(raw_strength, 4)
        payload["pair_age_days"] = age_days
        payload["pair_decay_multiplier"] = decay_multiplier
        payload["pair_strength_score"] = round(raw_strength * decay_multiplier, 4)
        payload["pair_last_source"] = str((payload.get("payload") or {}).get("last_source") or "")
        return payload

    def _relationship_snapshot(self, cleaned_symbol: dict[str, Any], raw_payload: dict[str, Any] | None) -> dict[str, Any]:
        snapshot = dict(cleaned_symbol)
        raw = dict(raw_payload or {})
        for key in ("canonical_industry", "industry_family"):
            value = str(raw.get(key) or "").strip()
            if value:
                snapshot[key] = value
        for key in ("peer_learning_score", "base_peer_learning_score", "industry_similarity", "pair_strength_score"):
            try:
                value = float(raw.get(key))
            except (TypeError, ValueError):
                continue
            snapshot[key] = round(value, 4)
        try:
            if raw.get("peer_rank") is not None:
                snapshot["peer_rank"] = int(raw.get("peer_rank"))
        except (TypeError, ValueError):
            pass
        return snapshot

    def get_peer_relationship(self, subject_ticker: str, peer_ticker: str) -> dict[str, Any] | None:
        subject_text = str(subject_ticker or "").strip().upper()
        peer_text = str(peer_ticker or "").strip().upper()
        if not subject_text or not peer_text:
            return None
        row = self._fetch_one(
            "SELECT * FROM peer_relationships WHERE subject_ticker = :subject_ticker AND peer_ticker = :peer_ticker",
            {"subject_ticker": subject_text, "peer_ticker": peer_text},
        )
        return self._row_to_peer_relationship(row)

    def list_peer_relationships(self, *, subject_ticker: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        sql = "SELECT * FROM peer_relationships"
        params: dict[str, Any] = {}
        if subject_ticker:
            sql += " WHERE subject_ticker = :subject_ticker"
            params["subject_ticker"] = str(subject_ticker or "").strip().upper()
        sql += " ORDER BY last_seen_at DESC, subject_ticker ASC, peer_ticker ASC LIMIT :limit"
        params["limit"] = max(int(limit or 8), 1)
        rows = self._fetch_all(sql, params)

        items = [relationship for row in rows if (relationship := self._row_to_peer_relationship(row)) is not None]
        items.sort(
            key=lambda item: (
                -float(item.get("pair_strength_score") or 0.0),
                -int(item.get("manual_compare_hits") or 0),
                -int(item.get("auto_peer_hits") or 0),
                str(item.get("last_seen_at") or ""),
                str(item.get("peer_ticker") or ""),
            )
        )
        return items[: max(int(limit or 8), 1)]

    def _upsert_peer_relationship(
        self,
        *,
        subject_item: dict[str, Any],
        peer_item: dict[str, Any],
        raw_peer: dict[str, Any] | None,
        source: str,
        seen_at: str,
        signal_points: float,
    ) -> dict[str, Any] | None:
        current = self.get_peer_relationship(subject_item["ticker"], peer_item["ticker"]) or {}
        payload = {
            "subject_ticker": subject_item["ticker"],
            "peer_ticker": peer_item["ticker"],
            "first_seen_at": str(current.get("first_seen_at") or seen_at),
            "last_seen_at": seen_at,
            "auto_peer_hits": int(current.get("auto_peer_hits") or 0) + (1 if source == "auto-peer-basket" else 0),
            "manual_compare_hits": int(current.get("manual_compare_hits") or 0) + (1 if source == "manual-compare" else 0),
            "signal_points": round(float(current.get("signal_points") or 0.0) + max(float(signal_points or 0.0), 0.0), 4),
            "payload_json": json.dumps(
                {
                    "subject": self._relationship_snapshot(subject_item, subject_item),
                    "peer": self._relationship_snapshot(peer_item, raw_peer),
                    "last_source": source,
                },
                ensure_ascii=False,
            ),
        }
        self._execute(
            """
            INSERT INTO peer_relationships(
                subject_ticker, peer_ticker, first_seen_at, last_seen_at,
                auto_peer_hits, manual_compare_hits, signal_points, payload_json
            ) VALUES (
                :subject_ticker, :peer_ticker, :first_seen_at, :last_seen_at,
                :auto_peer_hits, :manual_compare_hits, :signal_points, :payload_json
            )
            ON CONFLICT (subject_ticker, peer_ticker) DO UPDATE SET
                first_seen_at = EXCLUDED.first_seen_at,
                last_seen_at = EXCLUDED.last_seen_at,
                auto_peer_hits = EXCLUDED.auto_peer_hits,
                manual_compare_hits = EXCLUDED.manual_compare_hits,
                signal_points = EXCLUDED.signal_points,
                payload_json = EXCLUDED.payload_json
            """,
            payload,
        )
        return self.get_peer_relationship(subject_item["ticker"], peer_item["ticker"])

    def record_auto_peer_basket(
        self,
        subject: dict[str, Any],
        peers: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        subject_item = _clean_symbol_payload(subject)
        raw_peer_items = [dict(item or {}) for item in peers]
        paired_peer_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_peer in raw_peer_items:
            peer_item = _clean_symbol_payload(raw_peer)
            if not peer_item["ticker"] or peer_item["ticker"] == subject_item["ticker"]:
                continue
            paired_peer_items.append((raw_peer, peer_item))

        if not subject_item["ticker"] or not paired_peer_items:
            return {"subject_ticker": subject_item["ticker"], "peer_count": 0, "items": []}

        now_text = _utcnow_iso()
        items: list[dict[str, Any]] = []
        for raw_peer, peer_item in paired_peer_items:
            relationship = self._upsert_peer_relationship(
                subject_item=subject_item,
                peer_item=peer_item,
                raw_peer=raw_peer,
                source="auto-peer-basket",
                seen_at=now_text,
                signal_points=max(
                    float(raw_peer.get("base_peer_learning_score") or raw_peer.get("peer_learning_score") or 0.0),
                    0.6,
                ),
            )
            if relationship is not None:
                items.append(relationship)

        return {
            "subject_ticker": subject_item["ticker"],
            "peer_count": len(items),
            "items": items,
        }

    def record_search_impression(
        self,
        query: str,
        results: Iterable[dict[str, Any]] | None,
        *,
        exchange: str = "auto",
        selected_ticker: str | None = None,
    ) -> dict[str, Any]:
        query_text = str(query or "").strip()
        cleaned_results = [_clean_symbol_payload(item) for item in list(results or [])[:8]]
        cleaned_results = [item for item in cleaned_results if item["ticker"]]
        selected_text = str(selected_ticker or "").strip().upper()
        payload = {
            "query_text": query_text,
            "exchange": str(exchange or "auto").strip().upper(),
            "selected_ticker": selected_text,
            "created_at": _utcnow_iso(),
            "payload_json": json.dumps({"results": cleaned_results}, ensure_ascii=False),
            "impression_id": str(uuid.uuid4()),
        }
        if query_text or selected_text or cleaned_results:
            self._execute(
                """
                INSERT INTO search_impressions(impression_id, query_text, exchange, selected_ticker, created_at, payload_json)
                VALUES (:impression_id, :query_text, :exchange, :selected_ticker, :created_at, :payload_json)
                """,
                payload,
            )

        if cleaned_results:
            self.universe_store.record_candidates(
                [
                    {
                        **item,
                        "metadata": {
                            "search_query": query_text,
                            "search_exchange_hint": payload["exchange"],
                            "search_rank": index + 1,
                        },
                        "metadata_increments": {"search_impression_hits": 1},
                    }
                    for index, item in enumerate(cleaned_results)
                ],
                source="ticker-search-impression",
                searched=True,
            )

        if selected_text:
            selected_payload = next((item for item in cleaned_results if item["ticker"] == selected_text), {"ticker": selected_text})
            self.universe_store.upsert_symbol(
                selected_text,
                company_name=str(selected_payload.get("company_name") or ""),
                exchange=str(selected_payload.get("exchange") or ""),
                country=str(selected_payload.get("country") or ""),
                sector=str(selected_payload.get("sector") or ""),
                industry=str(selected_payload.get("industry") or ""),
                source="ticker-search-selection",
                searched=True,
                metadata={"selected_query": query_text, "search_exchange_hint": payload["exchange"]},
                metadata_increments={"selection_hits": 1},
            )

        return {
            "logged_results": len(cleaned_results),
            "selected_ticker": selected_text,
        }

    def record_manual_compare(
        self,
        subject: dict[str, Any],
        peers: Iterable[dict[str, Any]],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        subject_item = _clean_symbol_payload(subject)
        raw_peer_items = [dict(item or {}) for item in peers]
        paired_peer_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_peer in raw_peer_items:
            peer_item = _clean_symbol_payload(raw_peer)
            if not peer_item["ticker"] or peer_item["ticker"] == subject_item["ticker"]:
                continue
            paired_peer_items.append((raw_peer, peer_item))
        peer_items = [peer_item for _, peer_item in paired_peer_items]
        if not subject_item["ticker"] or not peer_items:
            return {"subject_ticker": subject_item["ticker"], "peer_count": 0, "items": []}

        event_id_text = str(event_id or "").strip()
        if event_id_text:
            existing_event = self._fetch_one(
                "SELECT compare_id FROM manual_compare_events WHERE compare_id = :compare_id",
                {"compare_id": event_id_text},
            )
            if existing_event is not None:
                return {
                    "subject_ticker": subject_item["ticker"],
                    "peer_count": len(peer_items),
                    "items": self.list_manual_compares(subject_ticker=subject_item["ticker"], limit=8),
                }

        now_text = _utcnow_iso()
        self.universe_store.upsert_symbol(
            subject_item["ticker"],
            company_name=subject_item["company_name"],
            exchange=subject_item["exchange"],
            country=subject_item["country"],
            sector=subject_item["sector"],
            industry=subject_item["industry"],
            source="manual-compare-subject",
            metadata={"last_compared_at": now_text},
            metadata_increments={"compare_subject_hits": 1},
        )

        with self._engine.begin() as conn:
            for index, peer in enumerate(peer_items):
                conn.execute(
                    text(
                        """
                        INSERT INTO manual_compare_events(compare_id, subject_ticker, peer_ticker, created_at, payload_json)
                        VALUES (:compare_id, :subject_ticker, :peer_ticker, :created_at, :payload_json)
                        """
                    ),
                    {
                        "compare_id": event_id_text if event_id_text and len(peer_items) == 1 else f"{event_id_text}:{index}" if event_id_text else str(uuid.uuid4()),
                        "subject_ticker": subject_item["ticker"],
                        "peer_ticker": peer["ticker"],
                        "created_at": now_text,
                        "payload_json": json.dumps({"subject": subject_item, "peer": peer}, ensure_ascii=False),
                    },
                )

        for raw_peer, peer in paired_peer_items:
            self._upsert_peer_relationship(
                subject_item=subject_item,
                peer_item=peer,
                raw_peer=raw_peer,
                source="manual-compare",
                seen_at=now_text,
                signal_points=max(
                    float(raw_peer.get("peer_learning_score") or raw_peer.get("base_peer_learning_score") or 0.0) + 1.5,
                    4.0,
                ),
            )

        self.universe_store.record_candidates(
            [
                {
                    **peer,
                    "metadata": {
                        "last_compared_with": subject_item["ticker"],
                        "last_compared_at": now_text,
                        "compare_subject_sector": subject_item["sector"],
                    },
                    "metadata_increments": {"compare_hits": 1},
                }
                for peer in peer_items
            ],
            source="manual-compare",
        )

        return {
            "subject_ticker": subject_item["ticker"],
            "peer_count": len(peer_items),
            "items": self.list_manual_compares(subject_ticker=subject_item["ticker"], limit=8),
        }

    def list_manual_compares(self, *, subject_ticker: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        sql = "SELECT * FROM manual_compare_events"
        params: dict[str, Any] = {}
        if subject_ticker:
            sql += " WHERE subject_ticker = :subject_ticker"
            params["subject_ticker"] = str(subject_ticker or "").strip().upper()
        sql += " ORDER BY created_at DESC, compare_id DESC LIMIT :limit"
        params["limit"] = max(int(limit or 8), 1)
        rows = self._fetch_all(sql, params)

        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row.get("payload_json") or "{}"))
            peer = dict(payload.get("peer") or {})
            peer_ticker = str(peer.get("ticker") or row.get("peer_ticker") or "").strip().upper()
            if not peer_ticker or peer_ticker in seen:
                continue
            seen.add(peer_ticker)
            items.append(
                {
                    "event_id": str(row.get("compare_id") or ""),
                    "ticker": peer_ticker,
                    "company_name": str(peer.get("company_name") or peer.get("name") or ""),
                    "exchange": str(peer.get("exchange") or ""),
                    "sector": str(peer.get("sector") or ""),
                    "industry": str(peer.get("industry") or ""),
                    "subject_ticker": str(row.get("subject_ticker") or ""),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
            if len(items) >= max(int(limit or 8), 1):
                break
        return items


__all__ = ["DISCOVERY_DB_PATH", "DiscoveryStore"]