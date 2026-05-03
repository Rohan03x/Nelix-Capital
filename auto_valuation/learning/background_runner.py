"""Background scheduler for expanding the shared-brain universe while the app is idle."""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Any, Callable

from auto_valuation.config import LEARNING_CONFIG

from .maintenance import run_live_evidence_bootstrap, run_scheduled_learning_maintenance


logger = logging.getLogger(__name__)

_RUNNER_LOCK = threading.Lock()
_RUNNER: "LearningBackgroundRunner | None" = None


def _default_fundamentals_provider(ticker: str) -> dict[str, Any] | None:
    try:
        from webapp.data.eodhd_client import _eodhd_code, _fetch_fundamentals

        return _fetch_fundamentals(_eodhd_code(ticker))
    except Exception as exc:
        logger.debug("Background fundamentals fetch failed for %s: %s", ticker, exc)
        return None


def run_background_learning_cycle(
    *,
    fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    if not bool(LEARNING_CONFIG.get("learning_enabled", True) and LEARNING_CONFIG.get("background_runner_enabled", True)):
        return {
            "enabled": False,
            "reason": "disabled",
            "bootstrap": {"enabled": False, "ran": False, "reason": "disabled"},
            "maintenance": {"enabled": False, "ran": False, "reason": "disabled"},
        }

    provider = fundamentals_provider or _default_fundamentals_provider
    bootstrap = run_live_evidence_bootstrap(
        fundamentals_provider=provider,
        interval_hours=int(LEARNING_CONFIG.get("background_runner_bootstrap_interval_hours", 6)),
        max_tickers=int(LEARNING_CONFIG.get("background_runner_bootstrap_max_tickers", 10)),
        max_replay_predictions_per_ticker=int(LEARNING_CONFIG.get("auto_bootstrap_replay_predictions_per_ticker", 5)),
        replay_enabled=True,
    )
    maintenance = run_scheduled_learning_maintenance(
        fundamentals_provider=provider,
        interval_hours=int(LEARNING_CONFIG.get("scheduled_postmortem_interval_hours", 24)),
        max_tickers=int(LEARNING_CONFIG.get("background_runner_maintenance_max_tickers", 6)),
    )

    bootstrap_payload = bootstrap.to_dict() if hasattr(bootstrap, "to_dict") else dict(bootstrap)
    maintenance_payload = maintenance.to_dict() if hasattr(maintenance, "to_dict") else dict(maintenance)
    return {
        "enabled": True,
        "reason": None,
        "bootstrap": bootstrap_payload,
        "maintenance": maintenance_payload,
    }


class LearningBackgroundRunner:
    def __init__(
        self,
        *,
        loop_seconds: int | None = None,
        fundamentals_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.loop_seconds = max(int(loop_seconds or LEARNING_CONFIG.get("background_runner_loop_seconds", 900)), 30)
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
            if self._stop_event.wait(self.loop_seconds):
                break


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


__all__ = [
    "LearningBackgroundRunner",
    "run_background_learning_cycle",
    "start_learning_background_runner",
    "stop_learning_background_runner",
]