from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .cross_industry import AnalogObservation


logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parent / "data" / "dashboard_learning_seed.json"
_SEED_CACHE: dict[str, Any] | None = None


def reset_seed_cache() -> None:
    global _SEED_CACHE
    _SEED_CACHE = None


def _load_seed_payload() -> dict[str, Any]:
    global _SEED_CACHE
    if _SEED_CACHE is not None:
        return _SEED_CACHE
    if not SEED_PATH.exists():
        _SEED_CACHE = {}
        return _SEED_CACHE
    try:
        _SEED_CACHE = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load dashboard learning seed: %s", exc)
        _SEED_CACHE = {}
    return _SEED_CACHE


def cohort_observations(limit: int | None = None) -> list[dict[str, Any]]:
    items = list((_load_seed_payload().get("cohort_observations") or []))
    if limit is not None and int(limit) > 0:
        items = items[: int(limit)]
    return [dict(item) for item in items if isinstance(item, dict)]


def analog_observations(limit: int | None = None) -> list[AnalogObservation]:
    items = list((_load_seed_payload().get("analog_observations") or []))
    if limit is not None and int(limit) > 0:
        items = items[: int(limit)]
    observations: list[AnalogObservation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            observations.append(AnalogObservation(**item))
        except Exception:
            continue
    return observations


def universe_summary() -> dict[str, Any]:
    payload = _load_seed_payload().get("universe_summary") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def background_runner_state() -> dict[str, Any]:
    payload = _load_seed_payload().get("background_runner_state") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def peer_relationships(subject_ticker: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    payload = _load_seed_payload().get("peer_relationships") or {}
    if not isinstance(payload, dict):
        return []
    if subject_ticker:
        items = list(payload.get(str(subject_ticker or "").strip().upper()) or [])
    else:
        items = []
        for rows in payload.values():
            if isinstance(rows, list):
                items.extend(rows)
    if limit is not None and int(limit) > 0:
        items = items[: int(limit)]
    return [dict(item) for item in items if isinstance(item, dict)]


def watchlist_items(limit: int | None = None) -> list[dict[str, Any]]:
    items = list((_load_seed_payload().get("watchlist") or []))
    if limit is not None and int(limit) > 0:
        items = items[: int(limit)]
    return [dict(item) for item in items if isinstance(item, dict)]


def manual_compare_items(subject_ticker: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    payload = _load_seed_payload().get("manual_compares") or {}
    if not isinstance(payload, dict):
        return []
    if subject_ticker:
        items = list(payload.get(str(subject_ticker or "").strip().upper()) or [])
    else:
        items = []
        for rows in payload.values():
            if isinstance(rows, list):
                items.extend(rows)
    if limit is not None and int(limit) > 0:
        items = items[: int(limit)]
    return [dict(item) for item in items if isinstance(item, dict)]


def seed_summary() -> dict[str, Any]:
    payload = _load_seed_payload()
    peer_payload = payload.get("peer_relationships") or {}
    manual_payload = payload.get("manual_compares") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "cohort_observations": len(payload.get("cohort_observations") or []),
        "analog_observations": len(payload.get("analog_observations") or []),
        "peer_subjects": len(peer_payload) if isinstance(peer_payload, dict) else 0,
        "watchlist": len(payload.get("watchlist") or []),
        "manual_compare_subjects": len(manual_payload) if isinstance(manual_payload, dict) else 0,
    }