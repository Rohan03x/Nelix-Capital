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


def historical_replay_summary(subject_ticker: str | None = None) -> dict[str, Any]:
    payload = _load_seed_payload().get("historical_replay_summary") or {}
    if not isinstance(payload, dict):
        return {}
    if subject_ticker:
        key = str(subject_ticker or "").strip().upper()
        item = payload.get(key) or {}
        if not item and "." in key:
            # Fallback: US stocks are stored without the exchange suffix (e.g. "NKE.US" -> "NKE")
            base_key = key.split(".")[0]
            item = payload.get(base_key) or {}
        return dict(item) if isinstance(item, dict) else {}
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def historical_replay_summary_observations(subject_ticker: str | None = None) -> list[dict[str, Any]]:
    summaries = historical_replay_summary(subject_ticker)
    if subject_ticker:
        summaries = {str(subject_ticker or "").strip().upper(): summaries} if summaries else {}
    observations: list[dict[str, Any]] = []
    for ticker, item in summaries.items():
        if not isinstance(item, dict):
            continue
        count = int(item.get("records") or 0)
        if count <= 0:
            continue
        observations.append(
            {
                "ticker": ticker,
                "sector": item.get("sector") or "Default",
                "industry": item.get("industry") or "",
                "data_vintage_years": int(item.get("data_vintage_years") or 1),
                "market_cap_regime": item.get("market_cap_regime") or "large",
                "macro_regime": item.get("macro_regime") or "neutral",
                "predicted_revenue_growth": 0.0,
                "actual_revenue_growth": float(item.get("mean_revenue_residual") or 0.0),
                "predicted_ebit_margin": 0.0,
                "actual_ebit_margin": float(item.get("mean_ebit_margin_residual") or 0.0),
                "predicted_wacc": float(item.get("predicted_wacc") or 0.0),
                "actual_wacc": float(item.get("predicted_wacc") or 0.0),
                "predicted_terminal_growth": 0.025,
                "actual_terminal_growth": 0.025,
                "predicted_beta": 1.0,
                "actual_beta": 1.0,
                "predicted_ufcf_margin": 0.0,
                "actual_ufcf_margin": float(item.get("mean_ufcf_margin_residual") or 0.0),
                "structural_break_flag": False,
                "quality_score": 1.0,
                "as_of_year": item.get("last_year"),
                "growth_regime": item.get("growth_regime") or "unknown",
                "observation_type": "deployment_historical_replay_summary",
                "evidence_count": count,
                "annual_records": int(item.get("annual_records") or 0),
                "quarterly_records": int(item.get("quarterly_records") or 0),
            }
        )
    return observations


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


def seeded_ledger_evidence(subject_ticker: str | None = None) -> dict[str, Any]:
    """Return the seeded ledger back-test counts for a ticker (or all tickers if None)."""
    payload = _load_seed_payload().get("ledger_evidence_summary") or {}
    if not isinstance(payload, dict):
        return {}
    if subject_ticker:
        key = str(subject_ticker or "").strip().upper()
        item = payload.get(key) or {}
        if not item and "." in key:
            base_key = key.split(".")[0]
            item = payload.get(base_key) or {}
        return dict(item) if isinstance(item, dict) else {}
    return {str(k): dict(v) for k, v in payload.items() if isinstance(v, dict)}


def seed_summary() -> dict[str, Any]:
    payload = _load_seed_payload()
    peer_payload = payload.get("peer_relationships") or {}
    manual_payload = payload.get("manual_compares") or {}
    replay_payload = payload.get("historical_replay_summary") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "cohort_observations": len(payload.get("cohort_observations") or []),
        "historical_replay_symbols": len(replay_payload) if isinstance(replay_payload, dict) else 0,
        "historical_replay_observations": sum(
            int(item.get("records") or 0)
            for item in replay_payload.values()
            if isinstance(item, dict)
        ) if isinstance(replay_payload, dict) else 0,
        "analog_observations": len(payload.get("analog_observations") or []),
        "peer_subjects": len(peer_payload) if isinstance(peer_payload, dict) else 0,
        "watchlist": len(payload.get("watchlist") or []),
        "manual_compare_subjects": len(manual_payload) if isinstance(manual_payload, dict) else 0,
    }