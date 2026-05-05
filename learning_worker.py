"""Standalone learning worker for local high-throughput training cycles."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.learning.background_runner import run_background_learning_cycle
from auto_valuation.learning.performance_report import build_learning_performance_report
from auto_valuation.learning.storage_paths import learning_db_dir


LOCK_PATH = learning_db_dir() / "learning_worker.lock"


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def _read_lock_pid(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload.get("pid") or 0)
    except Exception:
        return None


def acquire_lock(*, force: bool = False) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        pid = _read_lock_pid(LOCK_PATH)
        if not force and pid and _pid_running(pid):
            raise RuntimeError(f"learning worker already running with pid {pid}")
        try:
            LOCK_PATH.unlink()
        except OSError:
            if not force:
                raise
    payload = {"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(LOCK_PATH), flags)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def release_lock() -> None:
    try:
        pid = _read_lock_pid(LOCK_PATH)
        if pid in (None, os.getpid()):
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _print_payload(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local learning cycles without the Flask server.")
    parser.add_argument("--once", action="store_true", help="Run one learning cycle and exit.")
    parser.add_argument("--status", action="store_true", help="Print the learning performance report and exit.")
    parser.add_argument("--loop-seconds", type=int, default=int(LEARNING_CONFIG.get("background_runner_loop_seconds", 30)))
    parser.add_argument("--force-lock", action="store_true", help="Replace a stale worker lock.")
    args = parser.parse_args()

    if args.status:
        _print_payload(build_learning_performance_report())
        return 0

    loop_seconds = max(int(args.loop_seconds or 30), 30)
    acquire_lock(force=args.force_lock)
    try:
        if args.once:
            _print_payload(run_background_learning_cycle())
            return 0
        while True:
            started_at = time.time()
            result = run_background_learning_cycle()
            result["worker"] = {"pid": os.getpid(), "loop_seconds": loop_seconds}
            _print_payload(result)
            elapsed = time.time() - started_at
            time.sleep(max(loop_seconds - elapsed, 1.0))
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
