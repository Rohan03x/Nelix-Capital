"""Rank symbols by how much additional learning is likely to improve forecast calibration."""

from __future__ import annotations

import time as _time_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_valuation.config import LEARNING_CONFIG

from .ledger import LedgerReader

_WEBAPP_CACHE_DIR = Path(__file__).resolve().parents[2] / "webapp" / "data" / "cache"
_CALIB_PKL_PATH = _WEBAPP_CACHE_DIR / "_calib_priority.pkl"
_CALIB_PKL_TTL: float = 6 * 3600.0  # 6 hours (same as peer profiles)

# Short in-process TTL cache so the default (no-reader) call is re-used within a request
# cycle and across multiple summary() calls without hitting the DB every time.
_CALIB_PRIORITY_CACHE: dict[str, dict[Any, "CalibrationBucket"]] | None = None
_CALIB_PRIORITY_CACHE_TS: float = 0.0
_CALIB_PRIORITY_CACHE_TTL: float = 30.0  # seconds


def invalidate_calibration_priority_index_cache() -> None:
    global _CALIB_PRIORITY_CACHE, _CALIB_PRIORITY_CACHE_TS
    _CALIB_PRIORITY_CACHE = None
    _CALIB_PRIORITY_CACHE_TS = 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clean_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _weighted_error(postmortem: dict[str, Any]) -> float:
    revenue = abs(_safe_float(postmortem.get("revenue_error_pct")))
    margin = abs(_safe_float(postmortem.get("margin_error_bps"))) / 100.0
    ev = abs(_safe_float(postmortem.get("ev_error_pct")))
    price = abs(_safe_float(postmortem.get("price_return_error_pct")))
    return (revenue * 0.25) + (margin * 0.15) + (ev * 0.35) + (price * 0.25)


@dataclass(frozen=True)
class CalibrationBucket:
    samples: int
    mean_abs_error_pct: float
    structural_break_rate: float


def build_calibration_priority_index(ledger_reader: LedgerReader | None = None) -> dict[str, dict[Any, CalibrationBucket]]:
    global _CALIB_PRIORITY_CACHE, _CALIB_PRIORITY_CACHE_TS
    # Use the process-level cache only for the default (no custom reader) path.
    if ledger_reader is None:
        now = _time_mod.monotonic()
        if _CALIB_PRIORITY_CACHE is not None and (now - _CALIB_PRIORITY_CACHE_TS) < _CALIB_PRIORITY_CACHE_TTL:
            return _CALIB_PRIORITY_CACHE
        # Try disk pickle cache to skip full ledger scan on cold start.
        try:
            import pickle as _pickle
            import time as _t
            if _CALIB_PKL_PATH.exists():
                age = _t.time() - _CALIB_PKL_PATH.stat().st_mtime
                if age < _CALIB_PKL_TTL:
                    with _CALIB_PKL_PATH.open("rb") as _f:
                        snap = _pickle.load(_f)
                    if isinstance(snap, dict) and snap:
                        _CALIB_PRIORITY_CACHE = snap
                        _CALIB_PRIORITY_CACHE_TS = now
                        return snap
        except Exception:
            pass

    reader = ledger_reader or LedgerReader()
    predictions = {record.record_id: record for record in reader.query(limit=5000)}

    buckets: dict[str, dict[Any, list[tuple[float, float]]]] = {
        "ticker": {},
        "sector": {},
        "industry": {},
    }

    for postmortem in reader.query_postmortems():
        record_id = str(postmortem.get("record_id") or "")
        prediction = predictions.get(record_id)
        if prediction is None:
            continue
        weighted_error = _weighted_error(postmortem)
        structural_break = 1.0 if bool(postmortem.get("structural_break_detected")) else 0.0

        ticker_key = str(prediction.ticker or "").strip().upper()
        sector_key = _clean_key(prediction.sector)
        industry_key = (_clean_key(prediction.sector), _clean_key(prediction.industry))

        for bucket_name, bucket_key in (
            ("ticker", ticker_key),
            ("sector", sector_key),
            ("industry", industry_key),
        ):
            if not bucket_key or bucket_key == ("", ""):
                continue
            buckets[bucket_name].setdefault(bucket_key, []).append((weighted_error, structural_break))

    index: dict[str, dict[Any, CalibrationBucket]] = {"ticker": {}, "sector": {}, "industry": {}}
    for bucket_name, bucket_map in buckets.items():
        for bucket_key, samples in bucket_map.items():
            count = len(samples)
            if count == 0:
                continue
            mean_abs_error = sum(item[0] for item in samples) / count
            structural_break_rate = sum(item[1] for item in samples) / count
            index[bucket_name][bucket_key] = CalibrationBucket(
                samples=count,
                mean_abs_error_pct=round(mean_abs_error, 4),
                structural_break_rate=round(structural_break_rate, 4),
            )
    if ledger_reader is None:
        _CALIB_PRIORITY_CACHE = index
        _CALIB_PRIORITY_CACHE_TS = _time_mod.monotonic()
        # Persist to disk so next cold start loads in ~18ms instead of ~1.5s.
        try:
            import pickle as _pickle
            _CALIB_PKL_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CALIB_PKL_PATH.with_suffix(".pkl.tmp")
            with tmp.open("wb") as _f:
                _pickle.dump(index, _f, protocol=_pickle.HIGHEST_PROTOCOL)
            tmp.replace(_CALIB_PKL_PATH)
        except Exception:
            pass
    return index


def calibration_priority_for_symbol(symbol: dict[str, Any], index: dict[str, dict[Any, CalibrationBucket]]) -> dict[str, Any]:
    ticker_key = str(symbol.get("ticker") or "").strip().upper()
    sector_key = _clean_key(symbol.get("sector"))
    industry_key = (_clean_key(symbol.get("sector")), _clean_key(symbol.get("industry")))

    ticker_bucket = index.get("ticker", {}).get(ticker_key)
    industry_bucket = index.get("industry", {}).get(industry_key)
    sector_bucket = index.get("sector", {}).get(sector_key)
    cohort_bucket = industry_bucket or sector_bucket

    direct_samples = int(ticker_bucket.samples if ticker_bucket else 0)
    cohort_samples = int(cohort_bucket.samples if cohort_bucket else 0)
    direct_error = float(ticker_bucket.mean_abs_error_pct if ticker_bucket else 0.0)
    cohort_error = float(cohort_bucket.mean_abs_error_pct if cohort_bucket else 0.0)
    structural_break_rate = max(
        float(ticker_bucket.structural_break_rate if ticker_bucket else 0.0),
        float(cohort_bucket.structural_break_rate if cohort_bucket else 0.0),
    )

    min_samples = max(int(LEARNING_CONFIG.get("min_calibration_observations", 5)), 1)
    sample_gap = max(min_samples - direct_samples, 0)
    accuracy_need = direct_error if direct_samples > 0 else (cohort_error * 0.85 if cohort_samples > 0 else 0.0)

    score = 0.0
    if direct_samples > 0 or cohort_samples > 0:
        score += min(accuracy_need / 12.0, 3.2)
        score += min(sample_gap * 0.45, 2.0)
        score += min(structural_break_rate * 1.6, 1.6)

    mode = "ticker" if direct_samples > 0 else "cohort" if cohort_samples > 0 else "none"
    if mode == "ticker":
        note = f"{direct_samples} ticker postmortem(s) imply {direct_error:.1f}% mean miss pressure."
    elif mode == "cohort":
        note = f"{cohort_samples} cohort postmortem(s) imply {cohort_error:.1f}% mean miss pressure for this sector or industry."
    else:
        note = "No realized forecast-error history exists yet for this symbol or cohort."

    return {
        "score": round(score, 4),
        "mode": mode,
        "direct_samples": direct_samples,
        "cohort_samples": cohort_samples,
        "mean_abs_error_pct": round(max(direct_error, cohort_error), 4),
        "structural_break_rate": round(structural_break_rate, 4),
        "note": note,
    }


__all__ = [
    "CalibrationBucket",
    "build_calibration_priority_index",
    "calibration_priority_for_symbol",
]