"""Database backend helpers for the live shared-brain stores."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL
from sqlalchemy.pool import NullPool


_DATABASE_ENV_KEYS = (
    "SHARED_BRAIN_DATABASE_URL",
    "LEARNING_DATABASE_URL",
    "DATABASE_URL",
)
_ENGINE_CACHE: dict[str, Engine] = {}
_ENGINE_LOCK = Lock()


def _normalize_database_url(database_url: str) -> str:
    url = str(database_url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def resolve_shared_brain_database_url() -> str | None:
    for env_key in _DATABASE_ENV_KEYS:
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            return _normalize_database_url(value)
    return None


def _sqlite_url(database_path: Path) -> URL:
    return URL.create("sqlite", database=str(database_path))


def _cached_engine(cache_key: str, factory: Callable[[], Engine]) -> Engine:
    with _ENGINE_LOCK:
        engine = _ENGINE_CACHE.get(cache_key)
        if engine is not None:
            return engine
        engine = factory()
        _ENGINE_CACHE[cache_key] = engine
        return engine


def resolve_shared_brain_backend(
    db_path: Path | str | None,
    default_path: Path,
) -> tuple[Engine, str, Path | None, str | None]:
    if db_path is not None:
        database_path = Path(db_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        cache_key = f"sqlite-file::{database_path.resolve()}"
        engine = _cached_engine(
            cache_key,
            lambda: create_engine(
                _sqlite_url(database_path),
                connect_args={"check_same_thread": False},
            ),
        )
        return engine, "sqlite-file", database_path, None

    database_url = resolve_shared_brain_database_url()
    if database_url:
        backend = "sqlite-url" if database_url.startswith("sqlite") else "postgresql"
        cache_key = f"database-url::{database_url}"
        if backend == "sqlite-url":
            engine = _cached_engine(
                cache_key,
                lambda: create_engine(
                    database_url,
                    connect_args={"check_same_thread": False},
                ),
            )
        else:
            engine = _cached_engine(
                cache_key,
                lambda: create_engine(
                    database_url,
                    pool_pre_ping=True,
                    poolclass=NullPool,
                ),
            )
        return engine, backend, None, database_url

    database_path = Path(default_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    cache_key = f"sqlite-file::{database_path.resolve()}"
    engine = _cached_engine(
        cache_key,
        lambda: create_engine(
            _sqlite_url(database_path),
            connect_args={"check_same_thread": False},
        ),
    )
    return engine, "sqlite-file", database_path, None


def reset_shared_brain_engine_cache() -> None:
    with _ENGINE_LOCK:
        for engine in _ENGINE_CACHE.values():
            engine.dispose()
        _ENGINE_CACHE.clear()


__all__ = [
    "reset_shared_brain_engine_cache",
    "resolve_shared_brain_backend",
    "resolve_shared_brain_database_url",
]