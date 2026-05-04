"""Background scheduler for expanding the shared-brain universe while the app is idle."""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
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
_SEED_CURSOR_LOCK = threading.Lock()
_BACKGROUND_SEED_CURSOR = 0
_EXCHANGE_CURSOR_LOCK = threading.Lock()
_BACKGROUND_EXCHANGE_CURSOR = 0


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


# EODHD paid plan: 1,000 req/min = 16.67 req/sec.
# Use 15 req/sec to leave 10% headroom for dashboard/UI requests.
_EODHD_RATE_LIMITER = _TokenBucket(rate=15.0, capacity=20.0)


def _default_fundamentals_provider(ticker: str) -> dict[str, Any] | None:
    _EODHD_RATE_LIMITER.acquire()
    try:
        from webapp.data.eodhd_client import _eodhd_code, _fetch_fundamentals

        return _fetch_fundamentals(_eodhd_code(ticker))
    except Exception as exc:
        logger.debug("Background fundamentals fetch failed for %s: %s", ticker, exc)
        return None


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


def _should_run_replay(interval_hours: int) -> bool:
    """Return True if the replay hasn't run within *interval_hours*."""
    global _LAST_REPLAY_TS  # noqa: PLW0603
    import time as _time
    if _time.monotonic() - _LAST_REPLAY_TS >= interval_hours * 3600:
        _LAST_REPLAY_TS = _time.monotonic()
        return True
    return False


def run_background_learning_cycle(
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

    _restore_background_runner_cursors(state_path)
    provider = fundamentals_provider or _default_fundamentals_provider
    seed_refresh = _refresh_background_seed_cache()
    bootstrap_max_tickers = int(LEARNING_CONFIG.get("background_runner_bootstrap_max_tickers", 100))
    bootstrap_tickers = _build_background_bootstrap_tickers(bootstrap_max_tickers)
    bootstrap = run_live_evidence_bootstrap(
        tickers=bootstrap_tickers or None,
        fundamentals_provider=provider,
        interval_hours=int(LEARNING_CONFIG.get("background_runner_bootstrap_interval_hours", 6)),
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
    replay_interval = int(LEARNING_CONFIG.get("historical_replay_interval_hours", 24))
    replay = run_full_universe_replay(
        start_year=int(LEARNING_CONFIG.get("historical_replay_start_year", 2016)),
        quarterly=True,
        checkpoint_every=20,
    ) if _should_run_replay(replay_interval) else {"enabled": True, "ran": False, "reason": "interval"}

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
        "seed_refresh": seed_refresh,
        "state": state_payload,
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
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                logger.warning("Background learning cycle failed: %s", exc)
            # Push latest learning state to remote (Supabase) in a daemon thread
            # so it doesn't delay the next cycle. Throttled to max once per 5 min.
            _t = threading.Thread(target=_push_to_remote_async, daemon=True, name="learning-sync")
            _t.start()
            if self._stop_event.wait(self.loop_seconds):
                break


def _push_to_remote_async() -> None:
    """Fire-and-forget push of all learning state to remote (Supabase). Throttled."""
    try:
        from auto_valuation.learning.production_sync import persist_external_learning_state
        result = persist_external_learning_state()
        if result.get("enabled") and result.get("reason") not in (None, "throttled"):
            logger.warning("Remote learning sync returned: %s", result.get("reason"))
        elif result.get("enabled") and result.get("reason") is None:
            logger.debug("Remote learning sync: pushed %d namespaces", len(result.get("persisted") or {}))
    except Exception as exc:
        logger.debug("Remote learning sync failed (will retry next cycle): %s", exc)


def start_learning_background_runner() -> LearningBackgroundRunner | None:
    global _RUNNER

    if not bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("background_runner_enabled", True)):
        return None

    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = LearningBackgroundRunner().start()
            atexit.register(stop_learning_background_runner)
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
    last_run = state.get("last_run_at", "")[:10]
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
    "start_learning_background_runner",
    "stop_learning_background_runner",
]