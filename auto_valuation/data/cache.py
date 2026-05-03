"""
data/cache.py — Disk-based JSON cache with TTL for API response memoisation.

Each entry is stored as a JSON file with the schema::

    {"expires_at": <unix_timestamp_float>, "data": <any_json_serialisable>}

The filename is derived from a 32-character SHA-256 prefix of the key string,
so the cache is safe for keys of arbitrary length.

All public functions accept an optional ``cache_dir`` parameter (defaults to
``CACHE_DIR`` from config) for easy test isolation via pytest ``tmp_path``.

**Known limitation**: if ``fn()`` returns ``None``, the result is stored but
a subsequent ``get_cached`` call will look like a miss (because ``None`` is the
sentinel for "not found"). Avoid caching ``None`` results — use an empty list
``[]`` or empty dict ``{}`` instead.

Reference: Architecture Plan, Part 2.1 (fetch → cache → clean pipeline).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from auto_valuation.config import CACHE_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Key generation
# ─────────────────────────────────────────────────────────────────────────────

def cache_key(*args: Any, **kwargs: Any) -> str:
    """
    Build a deterministic string key from positional and keyword arguments.

    ``kwargs`` are sorted alphabetically so call-site ordering does not affect
    the key.

    Example::

        cache_key("AAPL", "income_statement", limit=10)
        # → 'AAPL|income_statement|limit=10'
    """
    parts  = [str(a) for a in args]
    parts += [f"{k}={v}" for k, v in sorted(kwargs.items())]
    return "|".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_dir(cache_dir: Path | str | None) -> Path:
    """Return the resolved cache directory as a Path."""
    return Path(cache_dir) if cache_dir is not None else Path(CACHE_DIR)


def _cache_path(key: str, cache_dir: Path | str | None = None) -> Path:
    """Map *key* to a filesystem path using a 32-character SHA-256 prefix."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return _resolve_dir(cache_dir) / f"{digest}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Read / write / invalidate
# ─────────────────────────────────────────────────────────────────────────────

def get_cached(key: str, cache_dir: Path | str | None = None) -> Any | None:
    """
    Return the cached value for *key* if present and not yet expired.

    Returns ``None`` on any of: cache miss, TTL expiry, read error, or
    unexpected file schema.  Callers must treat ``None`` as "no valid data".
    """
    path = _cache_path(key, cache_dir)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if time.time() > entry["expires_at"]:
            return None   # entry has expired
        return entry["data"]
    except Exception:
        return None   # corrupt file or unexpected schema → treat as miss


def set_cached(
    key: str,
    data: Any,
    ttl_hours: float = 24.0,
    cache_dir: Path | str | None = None,
) -> None:
    """
    Persist *data* under *key*, expiring after *ttl_hours*.

    Creates the cache directory if it does not exist.  Any write error is
    silently suppressed — a failed cache write is non-fatal; the caller will
    simply re-fetch on the next call.
    """
    path = _cache_path(key, cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "expires_at": time.time() + ttl_hours * 3_600,
            "data":       data,
        }
        path.write_text(json.dumps(entry), encoding="utf-8")
    except Exception:
        pass   # non-fatal: caller will refetch on next invocation


def invalidate(key: str, cache_dir: Path | str | None = None) -> bool:
    """
    Delete the cache entry for *key*.

    Returns ``True`` if a file was removed, ``False`` if no entry existed.
    """
    path = _cache_path(key, cache_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def clear_all(cache_dir: Path | str | None = None) -> int:
    """
    Remove every ``.json`` file from the cache directory.

    Returns the number of files deleted.  Safe to call when the directory does
    not yet exist (returns 0).
    """
    target = _resolve_dir(cache_dir)
    if not target.exists():
        return 0
    removed = 0
    for f in target.glob("*.json"):
        f.unlink()
        removed += 1
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# High-level fetch-or-cache helper
# ─────────────────────────────────────────────────────────────────────────────

def cached_fetch(
    key: str,
    fn: Callable[[], Any],
    ttl_hours: float = 24.0,
    cache_dir: Path | str | None = None,
) -> Any:
    """
    Return the cached value for *key* if still valid; otherwise call ``fn()``,
    store the result under *key*, and return it.

    ``fn`` must be a **zero-argument** callable.  Use ``functools.partial`` or
    a ``lambda`` to bind any required arguments before passing it in.

    Any exception raised by ``fn`` propagates to the caller unchanged; nothing
    is cached on failure.

    Example::

        from functools import partial
        from auto_valuation.data import fetcher, cache

        data = cache.cached_fetch(
            cache.cache_key("AAPL", "income", limit=10),
            partial(fetcher.fetch_income_statement, "AAPL", limit=10),
            ttl_hours=6.0,
        )
    """
    hit = get_cached(key, cache_dir)
    if hit is not None:
        return hit

    result = fn()                              # may raise — propagate to caller
    set_cached(key, result, ttl_hours, cache_dir)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Class-based interface  (Architecture Plan Part 39)
# ─────────────────────────────────────────────────────────────────────────────

class DataCache:
    """
    Object-oriented wrapper around the functional cache API.

    Usage::

        cache = DataCache(ttl_hours=6.0)
        data  = cache.fetch_or_get(
            cache_key("AAPL", "income"),
            lambda: fetcher.fetch_income_statement("AAPL", api_key),
        )

    Reference: Architecture Plan Part 39.
    """

    def __init__(
        self,
        ttl_hours: float = 24.0,
        cache_dir: str | None = None,
    ) -> None:
        self.ttl_hours  = ttl_hours
        self.cache_dir  = cache_dir

    # ── Delegate to functional API ─────────────────────────────────────────

    def get(self, key: str):
        """Return cached data for *key* or None if absent / expired."""
        return get_cached(key, self.cache_dir)

    def set(self, key: str, data) -> None:
        """Persist *data* under *key* for ttl_hours."""
        set_cached(key, data, self.ttl_hours, self.cache_dir)

    def invalidate(self, key: str) -> bool:
        """Remove a single cache entry. Returns True if it existed."""
        return invalidate(key, self.cache_dir)

    def clear_all(self) -> int:
        """Delete all cache entries. Returns count removed."""
        return clear_all(self.cache_dir)

    def fetch_or_get(self, key: str, fn):
        """
        Return cached data if available; otherwise call *fn()*, cache result,
        and return it.
        """
        return cached_fetch(key, fn, self.ttl_hours, self.cache_dir)

    def make_key(self, *args, **kwargs) -> str:
        """Convenience wrapper around cache_key()."""
        return cache_key(*args, **kwargs)
