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


def _mean(values: list[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _obs_dict(observation: Any) -> dict[str, Any]:
    if isinstance(observation, dict):
        return dict(observation)
    if is_dataclass(observation):
        return asdict(observation)
    return dict(getattr(observation, "__dict__", {}) or {})


def _historical_replay_summary() -> dict[str, dict[str, Any]]:
    from auto_valuation.learning.historical_replay import get_all_observations

    grouped: dict[str, dict[str, Any]] = {}
    for observation in _safe_call([], get_all_observations):
        item = _obs_dict(observation)
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        row = grouped.setdefault(
            ticker,
            {
                "records": 0,
                "annual_records": 0,
                "quarterly_records": 0,
                "first_year": None,
                "last_year": None,
                "sector": item.get("sector") or "Default",
                "industry": item.get("industry") or "",
                "data_vintage_years": int(item.get("data_vintage_years") or 1),
                "market_cap_regime": item.get("market_cap_regime") or "large",
                "macro_regime": item.get("macro_regime") or "neutral",
                "growth_regime": item.get("growth_regime") or "unknown",
                "predicted_wacc": float(item.get("predicted_wacc") or 0.0),
                "_revenue_residuals": [],
                "_margin_residuals": [],
                "_ufcf_residuals": [],
            },
        )
        row["records"] += 1
        is_annual = item.get("predicted_ufcf_margin") is not None or item.get("actual_ufcf_margin") is not None
        row["annual_records" if is_annual else "quarterly_records"] += 1
        year = item.get("as_of_year")
        if year is not None:
            year = int(year)
            row["first_year"] = year if row["first_year"] is None else min(int(row["first_year"]), year)
            row["last_year"] = year if row["last_year"] is None else max(int(row["last_year"]), year)
        if row["last_year"] is None or (year is not None and int(year) >= int(row["last_year"])):
            row["sector"] = item.get("sector") or row["sector"]
            row["industry"] = item.get("industry") or row["industry"]
            row["data_vintage_years"] = int(item.get("data_vintage_years") or row["data_vintage_years"] or 1)
            row["market_cap_regime"] = item.get("market_cap_regime") or row["market_cap_regime"]
            row["macro_regime"] = item.get("macro_regime") or row["macro_regime"]
            row["growth_regime"] = item.get("growth_regime") or row["growth_regime"]
            row["predicted_wacc"] = float(item.get("predicted_wacc") or row["predicted_wacc"] or 0.0)
        predicted_revenue = item.get("predicted_revenue_growth")
        actual_revenue = item.get("actual_revenue_growth")
        if predicted_revenue is not None and actual_revenue is not None:
            row["_revenue_residuals"].append(float(actual_revenue) - float(predicted_revenue))
        predicted_margin = item.get("predicted_ebit_margin")
        actual_margin = item.get("actual_ebit_margin")
        if predicted_margin is not None and actual_margin is not None:
            row["_margin_residuals"].append(float(actual_margin) - float(predicted_margin))
        predicted_ufcf = item.get("predicted_ufcf_margin")
        actual_ufcf = item.get("actual_ufcf_margin")
        if predicted_ufcf is not None and actual_ufcf is not None:
            row["_ufcf_residuals"].append(float(actual_ufcf) - float(predicted_ufcf))

    summary: dict[str, dict[str, Any]] = {}
    for ticker, row in grouped.items():
        revenue_residuals = row.pop("_revenue_residuals", [])
        margin_residuals = row.pop("_margin_residuals", [])
        ufcf_residuals = row.pop("_ufcf_residuals", [])
        mean_revenue = _mean(revenue_residuals)
        mean_margin = _mean(margin_residuals)
        mean_ufcf = _mean(ufcf_residuals)
        row["mean_revenue_residual"] = round(float(mean_revenue or 0.0), 6)
        row["mean_ebit_margin_residual"] = round(float(mean_margin or 0.0), 6)
        row["mean_ufcf_margin_residual"] = round(float(mean_ufcf or 0.0), 6)
        row["mean_abs_revenue_error_pct"] = round(_mean([abs(value) * 100.0 for value in revenue_residuals]) or 0.0, 2)
        row["mean_abs_margin_error_pp"] = round(_mean([abs(value) * 100.0 for value in margin_residuals]) or 0.0, 2)
        summary[ticker] = row
    return summary


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
    replay_summary = _historical_replay_summary()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort_observations": cohort,
        "historical_replay_summary": replay_summary,
        "analog_observations": analogs,
        "universe_summary": _safe_call({}, _universe_summary),
        "background_runner_state": _safe_call({}, read_background_runner_state),
        "peer_relationships": _peer_relationships(peer_limit),
        "watchlist": _safe_call([], DiscoveryStore().list_watchlist, limit=workflow_limit),
        "manual_compares": _manual_compares(workflow_limit),
        "source_counts": {
            "cohort_observations": len(cohort),
            "historical_replay_symbols": len(replay_summary),
            "historical_replay_observations": sum(int(item.get("records") or 0) for item in replay_summary.values()),
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
                "historical_replay_symbols": len(payload.get("historical_replay_summary") or {}),
                "historical_replay_observations": sum(
                    int(item.get("records") or 0)
                    for item in (payload.get("historical_replay_summary") or {}).values()
                    if isinstance(item, dict)
                ),
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
