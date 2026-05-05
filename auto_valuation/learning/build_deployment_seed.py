"""Build the bundled dashboard learning seed from local runtime stores."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.learning.background_runner import read_background_runner_state
from auto_valuation.learning.deployment_seed import SEED_PATH


def _safe_call(default: Any, func, *args, **kwargs) -> Any:
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _learning_cohort(limit: int) -> list[dict[str, Any]]:
    from webapp.data.knowledge_model import _load_learning_cohort

    rows = _safe_call([], _load_learning_cohort, limit=limit)
    return [_jsonable(row) for row in list(rows or [])[:limit] if isinstance(row, dict)]


def _analog_observations(limit: int) -> list[dict[str, Any]]:
    from webapp.data.knowledge_model import _load_analog_candidates

    rows = _safe_call([], _load_analog_candidates, limit=limit)
    return [_jsonable(row) for row in list(rows or [])[:limit]]


def _universe_summary() -> dict[str, Any]:
    from auto_valuation.learning.universe import SymbolUniverseStore

    return dict(
        SymbolUniverseStore().summary(
            stale_after_hours=int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18)),
            recent_days=int(LEARNING_CONFIG.get("symbol_universe_recent_days", 21)),
        )
    )


def _peer_relationships(limit: int) -> dict[str, list[dict[str, Any]]]:
    from auto_valuation.learning.discovery import DiscoveryStore

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _safe_call([], DiscoveryStore().list_peer_relationships, limit=limit):
        subject = str(row.get("subject_ticker") or "").strip().upper()
        if subject:
            grouped.setdefault(subject, []).append(_jsonable(row))
    return grouped


def _manual_compares(limit: int) -> dict[str, list[dict[str, Any]]]:
    from auto_valuation.learning.discovery import DiscoveryStore

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _safe_call([], DiscoveryStore().list_manual_compares, limit=limit):
        subject = str(row.get("subject_ticker") or "").strip().upper()
        if subject:
            grouped.setdefault(subject, []).append(_jsonable(row))
    return grouped


def build_deployment_seed(
    *,
    cohort_limit: int = 2000,
    analog_limit: int = 500,
    peer_limit: int = 500,
    workflow_limit: int = 200,
) -> dict[str, Any]:
    from auto_valuation.learning.discovery import DiscoveryStore

    cohort = _learning_cohort(cohort_limit)
    analogs = _analog_observations(analog_limit)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort_observations": cohort,
        "analog_observations": analogs,
        "universe_summary": _safe_call({}, _universe_summary),
        "background_runner_state": _safe_call({}, read_background_runner_state),
        "peer_relationships": _peer_relationships(peer_limit),
        "watchlist": _safe_call([], DiscoveryStore().list_watchlist, limit=workflow_limit),
        "manual_compares": _manual_compares(workflow_limit),
        "source_counts": {
            "cohort_observations": len(cohort),
            "analog_observations": len(analogs),
        },
    }
    return _jsonable(payload)


def write_deployment_seed(path: str | Path = SEED_PATH, **kwargs: Any) -> dict[str, Any]:
    payload = build_deployment_seed(**kwargs)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate auto_valuation/learning/data/dashboard_learning_seed.json")
    parser.add_argument("--cohort-limit", type=int, default=2000)
    parser.add_argument("--analog-limit", type=int, default=500)
    parser.add_argument("--peer-limit", type=int, default=500)
    parser.add_argument("--workflow-limit", type=int, default=200)
    parser.add_argument("--output", default=str(SEED_PATH))
    args = parser.parse_args()
    payload = write_deployment_seed(
        args.output,
        cohort_limit=args.cohort_limit,
        analog_limit=args.analog_limit,
        peer_limit=args.peer_limit,
        workflow_limit=args.workflow_limit,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "generated_at": payload.get("generated_at"),
                "cohort_observations": len(payload.get("cohort_observations") or []),
                "analog_observations": len(payload.get("analog_observations") or []),
                "peer_subjects": len(payload.get("peer_relationships") or {}),
                "watchlist": len(payload.get("watchlist") or []),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
