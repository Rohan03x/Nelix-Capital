from __future__ import annotations

import os
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent


def _first_writable_dir(candidates: list[Path]) -> Path:
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    fallback = Path(tempfile.gettempdir()) / "nelix-learning"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def learning_runtime_root() -> Path:
    override = str(os.environ.get("LEARNING_RUNTIME_ROOT") or "").strip()
    temp_root = Path(tempfile.gettempdir()) / "nelix-learning"
    if override:
        return _first_writable_dir([Path(override), temp_root])

    serverless_hint = any(
        str(os.environ.get(name) or "").strip()
        for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "FUNCTIONS_WORKER_RUNTIME")
    )
    if serverless_hint:
        return _first_writable_dir([temp_root])
    return _first_writable_dir([PACKAGE_ROOT, temp_root])


def learning_db_dir() -> Path:
    return learning_runtime_root() / "db"


def learning_ledger_dir() -> Path:
    return learning_runtime_root() / "ledger"


def learning_models_dir() -> Path:
    """Writable directory for trained model .pkl files.

    On serverless runtimes (Vercel / AWS Lambda) the package directory is
    read-only, so model files are restored here from R2 on every cold-start
    hydrate and saved here after retraining.  On persist, the background
    runner uploads the newly trained files from this directory to R2.

    Locally returns ``PACKAGE_ROOT / "data"`` — the directory where model
    files are committed to git and where training saves them.  This keeps
    local behaviour fully backward-compatible.
    """
    serverless = any(
        str(os.environ.get(name) or "").strip()
        for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "FUNCTIONS_WORKER_RUNTIME")
    )
    if serverless:
        p = Path(tempfile.gettempdir()) / "nelix-learning" / "models"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return PACKAGE_ROOT / "data"