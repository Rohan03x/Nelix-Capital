"""Persistent discovery signals that feed the shared-brain symbol universe."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .universe import SymbolUniverseStore


PACKAGE_ROOT = Path(__file__).resolve().parent
DISCOVERY_DB_PATH = PACKAGE_ROOT / "db" / "discovery.db"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.db_path = Path(db_path) if db_path else DISCOVERY_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.universe_store = universe_store or SymbolUniverseStore()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
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
            conn.execute(
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
            conn.execute(
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

    def list_watchlist(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist_items ORDER BY last_touched_at DESC, ticker ASC LIMIT ?",
                (max(int(limit or 20), 1),),
            ).fetchall()
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
        current = next((entry for entry in self.list_watchlist(limit=200) if entry.get("ticker") == symbol["ticker"]), None)
        added_at = str((current or {}).get("added_at") or now_text)
        payload = {
            **symbol,
            "added_at": added_at,
            "last_touched_at": now_text,
            "payload_json": json.dumps(symbol, ensure_ascii=False),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO watchlist_items (
                    ticker, company_name, exchange, country, sector, industry,
                    added_at, last_touched_at, payload_json
                ) VALUES (
                    :ticker, :company_name, :exchange, :country, :sector, :industry,
                    :added_at, :last_touched_at, :payload_json
                )
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
            metadata_increments={"watchlist_hits": 1},
        )
        return next((entry for entry in self.list_watchlist(limit=200) if entry.get("ticker") == symbol["ticker"]), None)

    def remove_from_watchlist(self, ticker: str) -> bool:
        ticker_text = str(ticker or "").strip().upper()
        if not ticker_text:
            return False
        with self._connect() as conn:
            rowcount = conn.execute("DELETE FROM watchlist_items WHERE ticker = ?", (ticker_text,)).rowcount
        self.universe_store.upsert_symbol(
            ticker_text,
            source="watchlist-remove",
            metadata={"watchlist_active": False, "watchlist_removed_at": _utcnow_iso()},
        )
        return bool(rowcount)

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
            with self._connect() as conn:
                conn.execute(
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
    ) -> dict[str, Any]:
        subject_item = _clean_symbol_payload(subject)
        peer_items = [_clean_symbol_payload(item) for item in peers]
        peer_items = [item for item in peer_items if item["ticker"] and item["ticker"] != subject_item["ticker"]]
        if not subject_item["ticker"] or not peer_items:
            return {"subject_ticker": subject_item["ticker"], "peer_count": 0, "items": []}

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

        with self._connect() as conn:
            for peer in peer_items:
                conn.execute(
                    """
                    INSERT INTO manual_compare_events(compare_id, subject_ticker, peer_ticker, created_at, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        subject_item["ticker"],
                        peer["ticker"],
                        now_text,
                        json.dumps({"subject": subject_item, "peer": peer}, ensure_ascii=False),
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
        params: tuple[Any, ...] = ()
        if subject_ticker:
            sql += " WHERE subject_ticker = ?"
            params = (str(subject_ticker or "").strip().upper(),)
        sql += " ORDER BY created_at DESC, compare_id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"] or "{}"))
            peer = dict(payload.get("peer") or {})
            peer_ticker = str(peer.get("ticker") or row["peer_ticker"] or "").strip().upper()
            if not peer_ticker or peer_ticker in seen:
                continue
            seen.add(peer_ticker)
            items.append(
                {
                    "ticker": peer_ticker,
                    "company_name": str(peer.get("company_name") or peer.get("name") or ""),
                    "exchange": str(peer.get("exchange") or ""),
                    "sector": str(peer.get("sector") or ""),
                    "industry": str(peer.get("industry") or ""),
                    "subject_ticker": str(row["subject_ticker"] or ""),
                    "created_at": str(row["created_at"] or ""),
                }
            )
            if len(items) >= max(int(limit or 8), 1):
                break
        return items


__all__ = ["DISCOVERY_DB_PATH", "DiscoveryStore"]