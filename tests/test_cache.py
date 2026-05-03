"""
tests/test_cache.py — Unit tests for auto_valuation/data/cache.py

Phase 12 — 22 tests covering:
  - cache_key:     key generation, determinism, kwarg sorting
  - get/set:       roundtrip, TTL expiry, overwrite, corrupt file, bad schema
  - invalidate:    existing/missing key, post-invalidate miss
  - clear_all:     empty dir, multiple entries, non-existent directory
  - cached_fetch:  miss calls fn, hit skips fn, result correct, exception propagates

No network calls.  All tests use pytest's ``tmp_path`` fixture for isolation;
the production CACHE_DIR is never touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_valuation.data.cache import (
    cache_key,
    cached_fetch,
    clear_all,
    get_cached,
    invalidate,
    set_cached,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_raw(tmp_path: Path, key: str, payload: dict) -> None:
    """Directly write a raw JSON payload to the cache file for *key*."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    path   = tmp_path / f"{digest}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# TestCacheKey
# ─────────────────────────────────────────────────────────────────────────────

class TestCacheKey:
    def test_deterministic_same_args(self):
        """Identical calls always produce the same key."""
        k1 = cache_key("AAPL", "income_statement", limit=10)
        k2 = cache_key("AAPL", "income_statement", limit=10)
        assert k1 == k2

    def test_different_positional_args_differ(self):
        """Different positional arguments produce different keys."""
        assert cache_key("AAPL") != cache_key("MSFT")

    def test_kwargs_sorted(self):
        """Keyword argument order at the call site does not affect the key."""
        k1 = cache_key("X", a=1, b=2)
        k2 = cache_key("X", b=2, a=1)
        assert k1 == k2

    def test_empty_args_returns_string(self):
        """cache_key() with no arguments returns an empty string."""
        k = cache_key()
        assert isinstance(k, str)
        assert k == ""


# ─────────────────────────────────────────────────────────────────────────────
# TestGetSetCached
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSetCached:
    def test_miss_returns_none(self, tmp_path):
        """Cache miss (key never written) returns None."""
        assert get_cached("nonexistent_key_xyz", cache_dir=tmp_path) is None

    def test_roundtrip_dict(self, tmp_path):
        """A dict written and read back is identical."""
        data = {"revenue": 1_000.0, "ebit": 160.0, "net_income": 120.0}
        set_cached("k_dict", data, ttl_hours=1.0, cache_dir=tmp_path)
        assert get_cached("k_dict", cache_dir=tmp_path) == data

    def test_roundtrip_list_of_dicts(self, tmp_path):
        """A list of dicts (typical FMP payload) survives a cache round-trip."""
        data = [
            {"calendarYear": "2023", "revenue": 500.0},
            {"calendarYear": "2022", "revenue": 450.0},
        ]
        set_cached("k_list", data, ttl_hours=1.0, cache_dir=tmp_path)
        assert get_cached("k_list", cache_dir=tmp_path) == data

    def test_roundtrip_nested_dict(self, tmp_path):
        """Deeply nested structures are preserved through JSON serialisation."""
        data = {"level1": {"level2": {"values": [1.0, 2.0, 3.0]}}}
        set_cached("k_nested", data, ttl_hours=1.0, cache_dir=tmp_path)
        assert get_cached("k_nested", cache_dir=tmp_path) == data

    def test_expired_entry_returns_none(self, tmp_path):
        """An entry whose expires_at is in the past returns None."""
        key = "k_expired"
        # Write a raw entry with a unix timestamp of 1.0 (effectively epoch)
        _write_raw(tmp_path, key, {"expires_at": 1.0, "data": {"x": 99}})
        assert get_cached(key, cache_dir=tmp_path) is None

    def test_overwrite_updates_value(self, tmp_path):
        """Writing to the same key twice returns the most recent value."""
        set_cached("k_ow", {"v": 1}, ttl_hours=1.0, cache_dir=tmp_path)
        set_cached("k_ow", {"v": 2}, ttl_hours=1.0, cache_dir=tmp_path)
        assert get_cached("k_ow", cache_dir=tmp_path) == {"v": 2}

    def test_corrupt_file_returns_none(self, tmp_path):
        """A file containing invalid JSON is treated as a cache miss."""
        key = "k_corrupt"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        (tmp_path / f"{digest}.json").write_text("{{NOT JSON!!", encoding="utf-8")
        assert get_cached(key, cache_dir=tmp_path) is None

    def test_missing_expires_at_key_returns_none(self, tmp_path):
        """Valid JSON that lacks the 'expires_at' key is treated as a miss."""
        key = "k_no_expiry"
        # Entry has 'data' but no 'expires_at' → KeyError → caught → None
        _write_raw(tmp_path, key, {"data": {"x": 1}})
        assert get_cached(key, cache_dir=tmp_path) is None

    def test_creates_cache_dir_if_missing(self, tmp_path):
        """set_cached creates a nested cache directory if it does not exist."""
        nested = tmp_path / "sub" / "cache"
        set_cached("k_newdir", {"ok": True}, ttl_hours=1.0, cache_dir=nested)
        assert get_cached("k_newdir", cache_dir=nested) == {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# TestInvalidate
# ─────────────────────────────────────────────────────────────────────────────

class TestInvalidate:
    def test_invalidate_existing_returns_true(self, tmp_path):
        """invalidate() returns True when a cached entry is removed."""
        set_cached("k_del", {"v": 1}, ttl_hours=1.0, cache_dir=tmp_path)
        assert invalidate("k_del", cache_dir=tmp_path) is True

    def test_invalidate_missing_returns_false(self, tmp_path):
        """invalidate() returns False when no entry exists for the key."""
        assert invalidate("k_ghost", cache_dir=tmp_path) is False

    def test_invalidate_then_get_returns_none(self, tmp_path):
        """get_cached() returns None after invalidating the key."""
        set_cached("k_inv", {"v": 1}, ttl_hours=1.0, cache_dir=tmp_path)
        invalidate("k_inv", cache_dir=tmp_path)
        assert get_cached("k_inv", cache_dir=tmp_path) is None


# ─────────────────────────────────────────────────────────────────────────────
# TestClearAll
# ─────────────────────────────────────────────────────────────────────────────

class TestClearAll:
    def test_clear_empty_dir_returns_zero(self, tmp_path):
        """clear_all() on an existing but empty directory returns 0."""
        assert clear_all(cache_dir=tmp_path) == 0

    def test_clear_removes_all_json_files(self, tmp_path):
        """clear_all() removes every .json file and returns the correct count."""
        for i in range(4):
            set_cached(f"k_clear_{i}", {"n": i}, ttl_hours=1.0, cache_dir=tmp_path)
        removed = clear_all(cache_dir=tmp_path)
        assert removed == 4
        assert list(tmp_path.glob("*.json")) == []

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        """clear_all() on a directory that has never been created returns 0."""
        ghost = tmp_path / "does_not_exist"
        assert clear_all(cache_dir=ghost) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TestCachedFetch
# ─────────────────────────────────────────────────────────────────────────────

class TestCachedFetch:
    def test_miss_calls_fn(self, tmp_path):
        """On a cache miss, the provided callable is invoked exactly once."""
        fn = MagicMock(return_value={"rows": [1, 2, 3]})
        cached_fetch("cf_miss", fn, ttl_hours=1.0, cache_dir=tmp_path)
        fn.assert_called_once()

    def test_hit_skips_fn(self, tmp_path):
        """On a cache hit, the callable is NOT invoked a second time."""
        fn  = MagicMock(return_value={"rows": [1, 2, 3]})
        key = "cf_hit"
        cached_fetch(key, fn, ttl_hours=1.0, cache_dir=tmp_path)  # miss → fn called
        cached_fetch(key, fn, ttl_hours=1.0, cache_dir=tmp_path)  # hit  → fn NOT called
        assert fn.call_count == 1

    def test_returns_correct_result(self, tmp_path):
        """cached_fetch returns the exact value produced by fn."""
        expected = {"ticker": "AAPL", "enterprise_value": 2_800.0}
        fn = MagicMock(return_value=expected)
        result = cached_fetch("cf_result", fn, ttl_hours=1.0, cache_dir=tmp_path)
        assert result == expected

    def test_fn_exception_propagates(self, tmp_path):
        """If fn raises, the exception bubbles to the caller; nothing is cached."""
        def bad_fn() -> None:
            raise RuntimeError("upstream API unavailable")

        with pytest.raises(RuntimeError, match="upstream API unavailable"):
            cached_fetch("cf_exc", bad_fn, ttl_hours=1.0, cache_dir=tmp_path)
        # Nothing should have been cached
        assert get_cached("cf_exc", cache_dir=tmp_path) is None
