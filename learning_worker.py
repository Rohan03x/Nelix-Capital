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
    if os.name == "nt":
        return _windows_pid_running(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def _windows_pid_running(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        still_active = 259
        process_handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not process_handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(process_handle)
    except Exception:
        return False


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


def _persist_remote_learning_state(*, force: bool = False) -> dict[str, Any]:
    try:
        from auto_valuation.learning.production_sync import persist_external_learning_state

        return dict(persist_external_learning_state(force=force) or {})
    except Exception as exc:
        return {"enabled": False, "reason": str(exc)}


def _attach_remote_sync(payload: dict[str, Any], *, force: bool = False, enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        payload["remote_sync"] = {"enabled": False, "reason": "disabled-by-worker-flag"}
        return payload
    if payload.get("reason") in {"learning-store-busy", "database-locked"}:
        payload["remote_sync"] = {"enabled": False, "reason": "cycle-skipped"}
        return payload
    payload["remote_sync"] = _persist_remote_learning_state(force=force)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local learning cycles without the Flask server.")
    parser.add_argument("--once", action="store_true", help="Run one learning cycle and exit.")
    parser.add_argument("--status", action="store_true", help="Print the learning performance report and exit.")
    parser.add_argument("--loop-seconds", type=int, default=int(LEARNING_CONFIG.get("background_runner_loop_seconds", 30)))
    parser.add_argument("--force-lock", action="store_true", help="Replace a stale worker lock.")
    parser.add_argument("--no-remote-sync", action="store_true", help="Do not persist completed cycles to external learning storage.")
    parser.add_argument("--force-remote-sync", action="store_true", help="Bypass the normal external sync throttle after each cycle.")
    args = parser.parse_args()

    if args.status:
        _print_payload(build_learning_performance_report())
        return 0

    loop_seconds = max(int(args.loop_seconds or 30), 30)
    acquire_lock(force=args.force_lock)
    try:
        if args.once:
            result = run_background_learning_cycle()
            _print_payload(
                _attach_remote_sync(
                    result,
                    force=bool(args.force_remote_sync),
                    enabled=not bool(args.no_remote_sync),
                )
            )
            return 0
        while True:
            started_at = time.time()
            result = run_background_learning_cycle()
            result = _attach_remote_sync(
                result,
                force=bool(args.force_remote_sync),
                enabled=not bool(args.no_remote_sync),
            )
            result["worker"] = {"pid": os.getpid(), "loop_seconds": loop_seconds}
            _print_payload(result)
            elapsed = time.time() - started_at
            time.sleep(max(loop_seconds - elapsed, 1.0))
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
