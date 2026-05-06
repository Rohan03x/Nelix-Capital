"""Background scheduler for expanding the shared-brain universe while the app is idle."""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.learning.storage_paths import learning_db_dir

from .historical_replay import run_full_universe_replay
from .live_evidence_bootstrap import (
    DEFAULT_BOOTSTRAP_TICKERS,
    _load_cached_bootstrap_tickers,
    _load_universe_priority_tickers,
    run_live_evidence_bootstrap,
)
from .maintenance import run_scheduled_learning_maintenance


logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).resolve().parent
BACKGROUND_RUNNER_STATE_PATH = learning_db_dir() / "background_runner_state.json"

_RUNNER_LOCK = threading.Lock()
_RUNNER: "LearningBackgroundRunner | None" = None
_CYCLE_LOCK = threading.Lock()
_SEED_CURSOR_LOCK = threading.Lock()
_BACKGROUND_SEED_CURSOR = 0
_EXCHANGE_CURSOR_LOCK = threading.Lock()
_BACKGROUND_EXCHANGE_CURSOR = 0


def _cycle_skipped_payload(reason: str, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": True,
        "reason": reason,
        "bootstrap": {"enabled": True, "ran": False, "reason": reason},
        "maintenance": {"enabled": True, "ran": False, "reason": reason},
        "replay": {"enabled": True, "ran": False, "reason": reason},
    }
    if error:
        payload["error"] = error
    return payload


def _is_database_locked_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return int(default)


def _serverless_learning_overrides() -> dict[str, Any]:
    """Return bounded background-runner settings for serverless cron requests.

    Local/dev runners can use the full high-throughput loop. Vercel requests
    have a finite function window, so production cron should process the
    largest batch that can reliably complete and persist state.
    """
    if not os.environ.get("VERCEL"):
        return {}
    return {
        "background_runner_bootstrap_max_tickers": _env_int("VERCEL_BACKGROUND_BOOTSTRAP_MAX_TICKERS", 24),
        "background_runner_maintenance_max_tickers": _env_int("VERCEL_BACKGROUND_MAINTENANCE_MAX_TICKERS", 4),
        "background_runner_seed_prefix_per_cycle": _env_int("VERCEL_BACKGROUND_SEED_PREFIX_PER_CYCLE", 8),
        "background_runner_seed_pool_limit": _env_int("VERCEL_BACKGROUND_SEED_POOL_LIMIT", 2000),
        "background_runner_exchange_refresh_batch": _env_int("VERCEL_BACKGROUND_EXCHANGE_REFRESH_BATCH", 1),
        "background_runner_exchange_refresh_per_exchange_limit": _env_int(
            "VERCEL_BACKGROUND_EXCHANGE_REFRESH_PER_EXCHANGE_LIMIT",
            120,
        ),
        "background_runner_concurrent_workers": _env_int("VERCEL_BACKGROUND_CONCURRENT_WORKERS", 8),
        "background_runner_replay_enabled": False,
    }


class _temporary_learning_config:
    def __init__(self, overrides: dict[str, Any]) -> None:
        self.overrides = dict(overrides or {})
        self.original: dict[str, Any] = {}

    def __enter__(self) -> None:
        for key, value in self.overrides.items():
            self.original[key] = LEARNING_CONFIG.get(key)
            LEARNING_CONFIG[key] = value

    def __exit__(self, *_exc: object) -> None:
        for key, value in self.original.items():
            if value is None:
                LEARNING_CONFIG.pop(key, None)
            else:
                LEARNING_CONFIG[key] = value


class _TokenBucket:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate        # tokens added per second
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        import time as _time
        while True:
            with self._lock:
                now = _time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            _time.sleep(wait)


# EODHD paid plan: 100,000 req/day; tested ceiling ~19 req/sec at high concurrency.
# Keep the local worker cap at 16 to match this workstation's available threads.
_EODHD_RATE_LIMITER = _TokenBucket(rate=19.0, capacity=40.0)

# Number of concurrent workers for parallel fundamentals pre-fetching.
_CONCURRENT_WORKERS: int = 16


def _default_fundamentals_provider(ticker: str) -> dict[str, Any] | None:
    try:
        from webapp.data.eodhd_client import (
            _TTL_FUND_SEC,
            _cache_read,
            _eodhd_code,
            _fetch_fundamentals,
        )

        code = _eodhd_code(ticker)
        cache_key = f"fund_{code.replace('.', '_')}"
        # Only rate-limit actual network requests — not disk-cache hits.
        if not _cache_read(cache_key, _TTL_FUND_SEC):
            _EODHD_RATE_LIMITER.acquire()
        return _fetch_fundamentals(code)
    except Exception as exc:
        logger.debug("Background fundamentals fetch failed for %s: %s", ticker, exc)
        return None


def _prefetch_fundamentals_parallel(
    tickers: list[str],
    *,
    provider: Callable[[str], dict[str, Any] | None],
    max_workers: int = 16,
) -> dict[str, dict[str, Any]]:
    """Fetch fundamentals for *tickers* concurrently with *max_workers* threads.

    Returns a ticker→data mapping.  As a side-effect, each successful fetch
    also writes to the on-disk cache so subsequent provider calls are instant
    cache hits (no API call or rate-limit wait).
    Failed tickers are recorded as unavailable so they are excluded from the
    seed pool for the next 72 hours.
    """
    if not tickers:
        return {}
    workers = max(1, min(int(max_workers), _CONCURRENT_WORKERS))
    results: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    lock = threading.Lock()

    def _fetch(ticker: str) -> None:
        data = provider(ticker)
        if isinstance(data, dict) and data:
            with lock:
                results[ticker.upper()] = data
        else:
            with lock:
                failed.append(ticker.upper())

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bg-fund") as pool:
        list(pool.map(_fetch, tickers))

    if failed:
        try:
            from webapp.data.ticker_search import record_seed_symbol_health
            for ticker in failed:
                record_seed_symbol_health(ticker, available=False, source="prefetch-unavailable")
        except Exception:
            pass

    return results


def _safe_tracked_symbol_count() -> int:
    try:
        from auto_valuation.learning.universe import SymbolUniverseStore

        summary = SymbolUniverseStore().summary(
            stale_after_hours=int(LEARNING_CONFIG.get("symbol_universe_bootstrap_interval_hours", 18)),
            recent_days=int(LEARNING_CONFIG.get("symbol_universe_recent_days", 21)),
        )
    except Exception:
        return 0
    return max(int(summary.get("tracked_symbols") or 0), 0)


def _read_background_runner_state(state_path: str | Path | None = None) -> dict[str, Any]:
    state_file = Path(state_path) if state_path else BACKGROUND_RUNNER_STATE_PATH
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_background_runner_state(payload: dict[str, Any], state_path: str | Path | None = None) -> None:
    state_file = Path(state_path) if state_path else BACKGROUND_RUNNER_STATE_PATH
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_background_runner_state(state_path: str | Path | None = None) -> dict[str, Any]:
    state = _read_background_runner_state(state_path)
    if state:
        return state
    return {
        "enabled": bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("background_runner_enabled", True)),
        "last_run_at": None,
        "requested_tickers": [],
        "requested_exchanges": [],
        "fetched_exchanges": [],
        "exchange_counts": {},
        "exchange_discovered_symbols": 0,
        "exchange_enrolled_symbols": 0,
        "seed_cursor": max(int(_BACKGROUND_SEED_CURSOR), 0),
        "exchange_cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
        "tracked_symbols": _safe_tracked_symbol_count(),
    }


def _restore_background_runner_cursors(state_path: str | Path | None = None) -> None:
    state = _read_background_runner_state(state_path)
    if not state:
        return

    global _BACKGROUND_SEED_CURSOR, _BACKGROUND_EXCHANGE_CURSOR
    try:
        seed_cursor = max(int(state.get("seed_cursor") or 0), 0)
    except (TypeError, ValueError):
        seed_cursor = 0
    try:
        exchange_cursor = max(int(state.get("exchange_cursor") or 0), 0)
    except (TypeError, ValueError):
        exchange_cursor = 0

    with _SEED_CURSOR_LOCK:
        if _BACKGROUND_SEED_CURSOR == 0 and seed_cursor > 0:
            _BACKGROUND_SEED_CURSOR = seed_cursor
    with _EXCHANGE_CURSOR_LOCK:
        if _BACKGROUND_EXCHANGE_CURSOR == 0 and exchange_cursor > 0:
            _BACKGROUND_EXCHANGE_CURSOR = exchange_cursor


def _load_background_seed_tickers(limit: int) -> list[str]:
    try:
        from webapp.data.ticker_search import seedable_tickers
    except Exception:
        return []
    return seedable_tickers(limit=limit if limit > 0 else None, common_stock_only=True)


def _configured_background_seed_exchanges() -> list[str]:
    raw_exchanges = LEARNING_CONFIG.get("background_runner_seed_exchanges") or []
    if isinstance(raw_exchanges, str):
        candidates = [part.strip() for part in raw_exchanges.split(",")]
    else:
        candidates = [str(part or "").strip() for part in list(raw_exchanges)]

    exchanges: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        exchange = candidate.upper()
        if not exchange or exchange in seen:
            continue
        seen.add(exchange)
        exchanges.append(exchange)
    return exchanges


def _next_background_seed_batch(
    seed_pool: list[str],
    size: int,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    if not seed_pool or size <= 0:
        return []

    blocked = {str(ticker or "").strip().upper() for ticker in (exclude or set()) if str(ticker or "").strip()}
    candidates = [ticker for ticker in seed_pool if str(ticker or "").strip().upper() not in blocked]
    if not candidates:
        return []

    global _BACKGROUND_SEED_CURSOR
    batch_size = min(size, len(candidates))
    with _SEED_CURSOR_LOCK:
        start = _BACKGROUND_SEED_CURSOR % len(candidates)
        rotated = candidates[start:] + candidates[:start]
        batch = rotated[:batch_size]
        _BACKGROUND_SEED_CURSOR = (start + batch_size) % len(candidates)
    return batch


def _next_background_exchange_batch(exchange_pool: list[str], size: int) -> list[str]:
    if not exchange_pool or size <= 0:
        return []

    global _BACKGROUND_EXCHANGE_CURSOR
    batch_size = min(size, len(exchange_pool))
    with _EXCHANGE_CURSOR_LOCK:
        start = _BACKGROUND_EXCHANGE_CURSOR % len(exchange_pool)
        rotated = exchange_pool[start:] + exchange_pool[:start]
        batch = rotated[:batch_size]
        _BACKGROUND_EXCHANGE_CURSOR = (start + batch_size) % len(exchange_pool)
    return batch


def _enroll_background_seed_items(items: list[dict[str, object]]) -> int:
    try:
        from auto_valuation.learning.universe import SymbolUniverseStore
    except Exception:
        return 0

    store = SymbolUniverseStore()
    enrolled = 0
    seen: set[str] = set()
    for item in items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        store.upsert_symbol(
            ticker,
            company_name=str(item.get("name") or ticker),
            exchange=str(item.get("exchange") or ""),
            country=str(item.get("country") or ""),
            sector=str(item.get("sector") or ""),
            industry=str(item.get("industry") or ""),
            source="background-seed-cache",
            fundamentals_cached=bool(item.get("has_fundamentals")),
            metadata={
                "background_seed_source": str(item.get("source") or "exchange-cache"),
                "background_seed_exchange": str(item.get("exchange") or ""),
                "background_seed_market_cap": float(item.get("market_cap") or 0.0),
                "background_seed_history_years": int(item.get("history_years") or 0),
            },
            metadata_increments={"background_seed_hits": 1},
        )
        enrolled += 1
    return enrolled


def _refresh_background_seed_cache() -> dict[str, Any]:
    exchange_pool = _configured_background_seed_exchanges()
    refresh_batch = max(int(LEARNING_CONFIG.get("background_runner_exchange_refresh_batch", 2) or 0), 0)
    per_exchange_limit = max(
        int(LEARNING_CONFIG.get("background_runner_exchange_refresh_per_exchange_limit", 250) or 0),
        0,
    )
    ttl_sec = max(int(LEARNING_CONFIG.get("background_runner_exchange_cache_ttl_sec", 604800) or 0), 0)

    if not exchange_pool or refresh_batch <= 0:
        return {
            "enabled": False,
            "configured_exchanges": exchange_pool,
            "requested_exchanges": [],
            "fetched_exchanges": [],
            "counts": {},
            "total_items": 0,
            "enrolled_symbols": 0,
            "cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
        }

    requested_exchanges = _next_background_exchange_batch(exchange_pool, refresh_batch)
    if not requested_exchanges:
        return {
            "enabled": True,
            "configured_exchanges": exchange_pool,
            "requested_exchanges": [],
            "fetched_exchanges": [],
            "counts": {},
            "total_items": 0,
            "enrolled_symbols": 0,
            "cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
        }

    try:
        from webapp.data.ticker_search import refresh_exchange_symbol_cache

        refresh_payload = refresh_exchange_symbol_cache(
            requested_exchanges,
            per_exchange_limit=per_exchange_limit,
            ttl_sec=ttl_sec,
        )
    except Exception as exc:
        logger.warning("Background exchange seed refresh failed: %s", exc)
        return {
            "enabled": True,
            "configured_exchanges": exchange_pool,
            "requested_exchanges": requested_exchanges,
            "fetched_exchanges": [],
            "counts": {},
            "total_items": 0,
            "enrolled_symbols": 0,
            "cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
            "error": str(exc),
        }

    items = list(refresh_payload.get("items") or [])
    enrolled = _enroll_background_seed_items(items)
    return {
        "enabled": True,
        "configured_exchanges": exchange_pool,
        "requested_exchanges": list(refresh_payload.get("exchanges") or requested_exchanges),
        "fetched_exchanges": list(refresh_payload.get("fetched_exchanges") or []),
        "counts": dict(refresh_payload.get("counts") or {}),
        "total_items": int(refresh_payload.get("total_items") or len(items)),
        "enrolled_symbols": enrolled,
        "cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
    }


def _build_background_bootstrap_tickers(max_tickers: int) -> list[str]:
    effective_max = max(int(max_tickers or 0), 1)
    tracked_symbols = _safe_tracked_symbol_count()
    seed_target = max(int(LEARNING_CONFIG.get("background_runner_seed_target_symbols", 1000) or 0), 0)
    configured_seed_slots = min(
        max(int(LEARNING_CONFIG.get("background_runner_seed_prefix_per_cycle", 8) or 0), 0),
        effective_max,
    )
    if configured_seed_slots <= 0:
        configured_seed_slots = min(4, effective_max)

    if seed_target > 0 and tracked_symbols < seed_target:
        seed_slots = configured_seed_slots
    else:
        seed_slots = min(max(configured_seed_slots // 2, 2), effective_max)

    priority_tickers = _load_universe_priority_tickers(
        max(int(LEARNING_CONFIG.get("symbol_universe_priority_limit", 144) or 144), effective_max)
    )
    seed_pool = _load_background_seed_tickers(
        max(int(LEARNING_CONFIG.get("background_runner_seed_pool_limit", 1000) or 1000), effective_max)
    )

    ordered: list[str] = []
    seen: set[str] = set()

    def add(symbol: str | None) -> None:
        ticker = str(symbol or "").strip().upper()
        if not ticker or ticker in seen:
            return
        seen.add(ticker)
        ordered.append(ticker)

    priority_prefix = min(max(effective_max - seed_slots, 0), len(priority_tickers))
    for ticker in priority_tickers[:priority_prefix]:
        add(ticker)

    for ticker in _next_background_seed_batch(seed_pool, seed_slots, exclude=seen):
        add(ticker)

    for source in (
        priority_tickers[priority_prefix:],
        _load_cached_bootstrap_tickers(int(LEARNING_CONFIG.get("bootstrap_cached_ticker_limit", 128) or 128)),
        list(DEFAULT_BOOTSTRAP_TICKERS),
        seed_pool,
    ):
        for ticker in source:
            add(ticker)
            if len(ordered) >= effective_max:
                return ordered[:effective_max]
    return ordered[:effective_max]


# Timestamp of the last successful full-universe replay so we can throttle it.
_LAST_REPLAY_TS: float = 0.0
# Timestamp of the last CAGR model training run (Layer F Tier 2).
_LAST_CAGR_TRAIN_TS: float = 0.0


def _should_run_replay(interval_hours: int) -> bool:
    """Return True if the replay hasn't run within *interval_hours*."""
    global _LAST_REPLAY_TS  # noqa: PLW0603
    import time as _time
    if _time.monotonic() - _LAST_REPLAY_TS >= interval_hours * 3600:
        _LAST_REPLAY_TS = _time.monotonic()
        return True
    return False


def _should_train_cagr_models(interval_hours: int) -> bool:
    """Return True if the CAGR Ridge models haven't been trained within *interval_hours*."""
    global _LAST_CAGR_TRAIN_TS  # noqa: PLW0603
    import time as _time
    if _time.monotonic() - _LAST_CAGR_TRAIN_TS >= interval_hours * 3600:
        _LAST_CAGR_TRAIN_TS = _time.monotonic()
        return True
    return False


def _train_cagr_models_from_ledger() -> dict[str, Any]:
    """Train per-regime Ridge CAGR models from predictions + realized-outcomes data.

    Derives YoY actual revenue growth from consecutive realized_outcomes rows
    and the implied predicted growth from prediction_records.  Falls back to
    calibration_observations if the predictions DB is unavailable.

    Returns a summary dict with sample_counts and status.
    """
    try:
        import sqlite3
        from auto_valuation.learning.near_term_cagr_predictor import NearTermCagrPredictor
        from auto_valuation.learning.storage_paths import learning_db_dir

        db_dir = learning_db_dir()
        pred_db = db_dir / "predictions.db"
        calib_db = db_dir / "calibration.db"

        training_records: list[dict] = []

        # Primary source: realized_outcomes consecutive years + prediction_records
        if pred_db.exists():
            conn = sqlite3.connect(str(pred_db))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    r1.ticker,
                    r1.forecast_horizon_year as year,
                    (r1.actual_revenue_mm / r0.actual_revenue_mm - 1) as actual_growth,
                    p.predicted_revenue_mm,
                    r0.actual_revenue_mm as base_rev
                FROM realized_outcomes r1
                JOIN realized_outcomes r0
                    ON r1.ticker = r0.ticker
                    AND r1.forecast_horizon_year = r0.forecast_horizon_year + 1
                    AND r1.rowid > r0.rowid
                LEFT JOIN prediction_records p
                    ON p.ticker = r1.ticker
                    AND p.forecast_horizon_year = r1.forecast_horizon_year
                WHERE r1.actual_revenue_mm > 0
                  AND r0.actual_revenue_mm > 0
                GROUP BY r1.ticker, r1.forecast_horizon_year
            """).fetchall()
            conn.close()

            for row in rows:
                actual_g = float(row["actual_growth"])
                # Clamp extreme outliers (>300% or <-80%) — data artefacts
                if actual_g > 3.0 or actual_g < -0.80:
                    continue
                base_rev = float(row["base_rev"])
                pred_mm = row["predicted_revenue_mm"]
                # Implied predicted growth: if prediction exists, compute from base
                if pred_mm and base_rev > 0:
                    ntm_g = float(pred_mm) / base_rev - 1.0
                    ntm_g = max(-0.80, min(ntm_g, 3.0))
                else:
                    ntm_g = actual_g  # use actual as proxy when prediction missing
                training_records.append({
                    "actual_revenue_growth": actual_g,
                    "feature_vector": {
                        "ntm_growth": ntm_g,
                        "cagr_3yr": ntm_g,
                        "cagr_5yr": ntm_g,
                    },
                })

        # Fallback: calibration_observations (actual/predicted_revenue_growth)
        if not training_records and calib_db.exists():
            conn = sqlite3.connect(str(calib_db))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT actual_revenue_growth, predicted_revenue_growth "
                "FROM calibration_observations "
                "WHERE actual_revenue_growth IS NOT NULL "
                "  AND predicted_revenue_growth IS NOT NULL "
                "  AND (actual_revenue_growth != 0 OR predicted_revenue_growth != 0)"
            ).fetchall()
            conn.close()
            for row in rows:
                actual_g = float(row["actual_revenue_growth"])
                pred_g = float(row["predicted_revenue_growth"])
                if actual_g > 3.0 or actual_g < -0.80:
                    continue
                training_records.append({
                    "actual_revenue_growth": actual_g,
                    "feature_vector": {
                        "ntm_growth": pred_g,
                        "cagr_3yr": pred_g,
                        "cagr_5yr": pred_g,
                    },
                })

        if not training_records:
            return {"ran": False, "reason": "no-training-records", "sample_counts": {}}

        predictor = NearTermCagrPredictor()
        sample_counts = predictor.train(training_records, alpha=1.0)
        total_samples = sum(sample_counts.values())
        logger.info(
            "CAGR Ridge model training complete: %d total samples across %d regimes",
            total_samples, len([v for v in sample_counts.values() if v >= 5]),
        )
        return {"ran": True, "sample_counts": sample_counts, "total_samples": total_samples}
    except Exception as exc:
        logger.warning("CAGR Ridge model training failed: %s", exc)
        return {"ran": False, "reason": str(exc), "sample_counts": {}}


def run_background_learning_cycle(
    *,
    fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    if not _CYCLE_LOCK.acquire(blocking=False):
        return _cycle_skipped_payload("learning-store-busy")
    try:
        serverless_overrides = _serverless_learning_overrides()
        if serverless_overrides:
            with _temporary_learning_config(serverless_overrides):
                return _run_background_learning_cycle(
                    fundamentals_provider=fundamentals_provider,
                    state_path=state_path,
                )
        return _run_background_learning_cycle(
            fundamentals_provider=fundamentals_provider,
            state_path=state_path,
        )
    except Exception as exc:
        if _is_database_locked_error(exc):
            logger.info("Background learning cycle skipped: %s", exc)
            return _cycle_skipped_payload("database-locked", error=str(exc))
        raise
    finally:
        _CYCLE_LOCK.release()


def _run_background_learning_cycle(
    *,
    fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    if not bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("background_runner_enabled", True)):
        return {
            "enabled": False,
            "reason": "disabled",
            "bootstrap": {"enabled": False, "ran": False, "reason": "disabled"},
            "maintenance": {"enabled": False, "ran": False, "reason": "disabled"},
        }

    if state_path is not None or fundamentals_provider is None:
        _restore_background_runner_cursors(state_path)
    provider = fundamentals_provider or _default_fundamentals_provider
    seed_refresh = _refresh_background_seed_cache()
    bootstrap_max_tickers = int(LEARNING_CONFIG.get("background_runner_bootstrap_max_tickers", 500))
    bootstrap_tickers = _build_background_bootstrap_tickers(bootstrap_max_tickers)

    # Pre-fetch all bootstrap tickers concurrently so the sequential bootstrap
    # loop only touches the disk cache (fast, no API rate-limit waits).
    n_workers = int(LEARNING_CONFIG.get("background_runner_concurrent_workers", _CONCURRENT_WORKERS))
    prefetched = _prefetch_fundamentals_parallel(bootstrap_tickers, provider=provider, max_workers=n_workers)
    _prefetch_map = {k.upper(): v for k, v in prefetched.items()}

    def _cached_provider(ticker: str) -> dict[str, Any] | None:
        return _prefetch_map.get(ticker.upper()) or provider(ticker)

    bootstrap = run_live_evidence_bootstrap(
        tickers=bootstrap_tickers or None,
        fundamentals_provider=_cached_provider,
        interval_hours=int(LEARNING_CONFIG.get("background_runner_bootstrap_interval_hours", 1)),
        max_tickers=bootstrap_max_tickers,
        max_replay_predictions_per_ticker=int(LEARNING_CONFIG.get("auto_bootstrap_replay_predictions_per_ticker", 5)),
        replay_enabled=True,
    )
    maintenance = run_scheduled_learning_maintenance(
        fundamentals_provider=provider,
        interval_hours=int(LEARNING_CONFIG.get("scheduled_postmortem_interval_hours", 24)),
        max_tickers=int(LEARNING_CONFIG.get("background_runner_maintenance_max_tickers", 6)),
    )

    # Replay all cached fundamentals to keep sector/cohort calibration priors
    # up to date.  This also primes the in-process get_all_observations() cache
    # so sector/cohort layers have peer data on the next model call.
    # Runs at most once per day (interval_hours=24).
    if bool(LEARNING_CONFIG.get("background_runner_replay_enabled", True)):
        replay_interval = int(LEARNING_CONFIG.get("historical_replay_interval_hours", 24))
        replay = run_full_universe_replay(
            start_year=int(LEARNING_CONFIG.get("historical_replay_start_year", 2016)),
            quarterly=True,
            checkpoint_every=20,
        ) if _should_run_replay(replay_interval) else {"enabled": True, "ran": False, "reason": "interval"}
    else:
        replay = {"enabled": False, "ran": False, "reason": "disabled"}

    # Layer F Tier 2 — train per-regime Ridge CAGR models from accumulated
    # postmortem records. Runs once every 24 h (configurable). Harmlessly skips
    # when < 5 records are available per regime.
    cagr_train_interval = int(LEARNING_CONFIG.get("cagr_model_train_interval_hours", 24))
    if _should_train_cagr_models(cagr_train_interval):
        cagr_train_result = _train_cagr_models_from_ledger()
    else:
        cagr_train_result = {"ran": False, "reason": "interval", "sample_counts": {}}

    bootstrap_payload = bootstrap.to_dict() if hasattr(bootstrap, "to_dict") else dict(bootstrap)
    maintenance_payload = maintenance.to_dict() if hasattr(maintenance, "to_dict") else dict(maintenance)
    bootstrap_payload.setdefault("requested_tickers", bootstrap_tickers)
    bootstrap_payload.setdefault(
        "background_seed_target_symbols",
        int(LEARNING_CONFIG.get("background_runner_seed_target_symbols", 1000) or 0),
    )
    seed_pool_limit = int(LEARNING_CONFIG.get("background_runner_seed_pool_limit", 1000) or 0)
    state_payload = {
        "enabled": True,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "requested_tickers": list(bootstrap_tickers),
        "requested_exchanges": list(seed_refresh.get("requested_exchanges") or []),
        "fetched_exchanges": list(seed_refresh.get("fetched_exchanges") or []),
        "exchange_counts": dict(seed_refresh.get("counts") or {}),
        "exchange_discovered_symbols": int(seed_refresh.get("total_items") or 0),
        "exchange_enrolled_symbols": int(seed_refresh.get("enrolled_symbols") or 0),
        "tracked_symbols": _safe_tracked_symbol_count(),
        "seed_pool_size": len(_load_background_seed_tickers(seed_pool_limit if seed_pool_limit > 0 else 0)),
        "seed_target_symbols": int(LEARNING_CONFIG.get("background_runner_seed_target_symbols", 1000) or 0),
        "seed_cursor": max(int(_BACKGROUND_SEED_CURSOR), 0),
        "exchange_cursor": max(int(_BACKGROUND_EXCHANGE_CURSOR), 0),
        "bootstrap": {
            "ran": bool(bootstrap_payload.get("ran")),
            "reason": bootstrap_payload.get("reason"),
            "requested_tickers": list(bootstrap_payload.get("requested_tickers") or bootstrap_tickers),
        },
        "maintenance": {
            "ran": bool(maintenance_payload.get("ran")),
            "reason": maintenance_payload.get("reason"),
        },
    }
    _write_background_runner_state(state_payload, state_path)
    return {
        "enabled": True,
        "reason": None,
        "bootstrap": bootstrap_payload,
        "maintenance": maintenance_payload,
        "replay": replay,
        "cagr_train": cagr_train_result,
        "seed_refresh": seed_refresh,
        "state": state_payload,
    }


def run_bulk_universe_seed(
    *,
    max_workers: int | None = None,
    fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    daily_budget: int | None = None,
) -> dict[str, Any]:
    """One-shot: fetch fundamentals for every universe symbol not yet on disk.

    Designed to run in a daemon thread on app startup.  Respects *daily_budget*
    (default 80,000) to stay safely below the 100,000/day EODHD plan limit.
    At 16 concurrent workers the full 3,800-symbol universe takes ~10 minutes.
    """
    workers = max(
        1,
        min(
            int(max_workers or LEARNING_CONFIG.get("background_runner_concurrent_workers", _CONCURRENT_WORKERS)),
            _CONCURRENT_WORKERS,
        ),
    )
    budget = int(daily_budget or LEARNING_CONFIG.get("bulk_seed_daily_budget", 80_000))
    provider = fundamentals_provider or _default_fundamentals_provider

    try:
        from auto_valuation.learning.universe import SymbolUniverseStore
        symbols = SymbolUniverseStore().list_symbols()
        all_tickers = [s["ticker"] for s in symbols if s.get("ticker")]
    except Exception as exc:
        logger.warning("Bulk seed: could not load universe symbols: %s", exc)
        return {"enabled": True, "ran": False, "reason": "universe_error", "error": str(exc)}

    if not all_tickers:
        return {"enabled": True, "ran": False, "reason": "no_universe_tickers"}

    # Filter to tickers whose fundamentals are NOT yet on disk.
    try:
        from webapp.data.eodhd_client import _TTL_FUND_SEC, _cache_read, _eodhd_code
        uncached: list[str] = []
        for ticker in all_tickers:
            code = _eodhd_code(ticker)
            cache_key = f"fund_{code.replace('.', '_')}"
            if not _cache_read(cache_key, _TTL_FUND_SEC):
                uncached.append(ticker)
    except Exception as exc:
        logger.warning("Bulk seed: cache-check failed: %s", exc)
        uncached = list(all_tickers)

    if not uncached:
        logger.info("Bulk seed: all %d universe symbols already cached.", len(all_tickers))
        return {"enabled": True, "ran": False, "reason": "all_cached", "universe_size": len(all_tickers)}

    to_fetch = uncached[:budget]
    logger.info(
        "Bulk seed: fetching %d/%d uncached symbols with %d workers …",
        len(to_fetch), len(uncached), workers,
    )

    fetched = 0
    failed = 0
    fetch_lock = threading.Lock()

    def _do_fetch(ticker: str) -> None:
        nonlocal fetched, failed
        data = provider(ticker)
        with fetch_lock:
            if isinstance(data, dict) and data:
                fetched += 1
            else:
                failed += 1

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bulk-seed") as pool:
        list(pool.map(_do_fetch, to_fetch))

    logger.info("Bulk seed complete: %d fetched, %d failed.", fetched, failed)
    return {
        "enabled": True,
        "ran": True,
        "universe_size": len(all_tickers),
        "uncached_found": len(uncached),
        "fetched": fetched,
        "failed": failed,
        "workers": workers,
    }


class LearningBackgroundRunner:
    def __init__(
        self,
        *,
        loop_seconds: int | None = None,
        fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.loop_seconds = max(int(loop_seconds or LEARNING_CONFIG.get("background_runner_loop_seconds", 60)), 30)
        self.fundamentals_provider = fundamentals_provider
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "LearningBackgroundRunner":
        if self.running:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="learning-background-runner", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_cycle(self) -> dict[str, Any]:
        return run_background_learning_cycle(fundamentals_provider=self.fundamentals_provider)

    def _run_loop(self) -> None:
        # Startup grace period: let the server handle early requests before
        # the first heavy background cycle (universe replay, etc.) fires.
        startup_grace = int(LEARNING_CONFIG.get("background_runner_startup_grace_sec", 90))
        if startup_grace > 0 and self._stop_event.wait(startup_grace):
            return
        while not self._stop_event.is_set():
            cycle_result: dict[str, Any] | None = None
            try:
                cycle_result = self.run_cycle()
            except Exception as exc:
                logger.warning("Background learning cycle failed: %s", exc)
            # Push latest learning state to remote (Supabase) in a daemon thread
            # so it doesn't delay the next cycle. Throttled to max once per 5 min.
            if not cycle_result or cycle_result.get("reason") not in {"learning-store-busy", "database-locked"}:
                _t = threading.Thread(target=_push_to_remote_async, daemon=True, name="learning-sync")
                _t.start()
            if self._stop_event.wait(self.loop_seconds):
                break


def _push_to_remote_async() -> None:
    """Fire-and-forget push of all learning state to remote (Supabase). Throttled."""
    if not _CYCLE_LOCK.acquire(blocking=False):
        logger.debug("Remote learning sync skipped: learning store is busy.")
        return
    try:
        from auto_valuation.learning.production_sync import persist_external_learning_state
        result = persist_external_learning_state()
        if result.get("enabled") and result.get("reason") not in (None, "throttled"):
            logger.warning("Remote learning sync returned: %s", result.get("reason"))
        elif result.get("enabled") and result.get("reason") is None:
            logger.debug("Remote learning sync: pushed %d namespaces", len(result.get("persisted") or {}))
    except Exception as exc:
        if _is_database_locked_error(exc):
            logger.debug("Remote learning sync skipped: %s", exc)
        else:
            logger.debug("Remote learning sync failed (will retry next cycle): %s", exc)
    finally:
        _CYCLE_LOCK.release()


def start_learning_background_runner() -> LearningBackgroundRunner | None:
    global _RUNNER

    if "pytest" in sys.modules:
        return None

    if not bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("background_runner_enabled", True)):
        return None

    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = LearningBackgroundRunner().start()
            atexit.register(stop_learning_background_runner)
            # Kick off a one-shot bulk seed in a daemon thread so the app
            # fills the fundamentals cache for all universe symbols on startup.
            if bool(LEARNING_CONFIG.get("bulk_seed_on_startup", True)):
                _seed_thread = threading.Thread(
                    target=run_bulk_universe_seed,
                    daemon=True,
                    name="bulk-universe-seed",
                )
                _seed_thread.start()
        elif not _RUNNER.running:
            _RUNNER.start()
        return _RUNNER


def stop_learning_background_runner() -> None:
    global _RUNNER

    with _RUNNER_LOCK:
        if _RUNNER is not None:
            _RUNNER.stop()
            _RUNNER = None


def get_daily_stats() -> dict[str, Any]:
    """Return today's training counters read from the persisted state file."""
    from datetime import date as _date
    today = _date.today().isoformat()
    state = read_background_runner_state()
    # Count tickers from last recorded run; reset to 0 if last_run_at is not today
    last_run = str(state.get("last_run_at") or "")[:10]
    if last_run == today:
        tickers = len(state.get("requested_tickers") or [])
    else:
        tickers = 0
    return {
        "date": today,
        "tickers_processed_today": tickers,
        "runner_running": _RUNNER is not None and _RUNNER.running,
        "loop_seconds": _RUNNER.loop_seconds if _RUNNER is not None else int(
            LEARNING_CONFIG.get("background_runner_loop_seconds", 60)
        ),
        "last_run_at": state.get("last_run_at"),
    }


__all__ = [
    "BACKGROUND_RUNNER_STATE_PATH",
    "LearningBackgroundRunner",
    "get_daily_stats",
    "read_background_runner_state",
    "run_background_learning_cycle",
    "run_bulk_universe_seed",
    "start_learning_background_runner",
    "stop_learning_background_runner",
]