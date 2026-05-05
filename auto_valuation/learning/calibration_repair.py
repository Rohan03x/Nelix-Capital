"""Repair persisted calibration priors using current residual safety bounds."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from auto_valuation.learning._layered_calibrator import CalibrationStore
from auto_valuation.learning.residual_controls import assumption_residual_bounds, clamp


def _bounded_std(assumption_name: str, value: object) -> float:
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return 0.0
    low, high = assumption_residual_bounds(assumption_name)
    return clamp(number, 0.0, max(abs(low), abs(high)))


def sanitize_calibration_priors(db_path: str | Path | None = None) -> dict[str, Any]:
    store = CalibrationStore(db_path) if db_path else CalibrationStore()
    if not Path(store.db_path).exists():
        return {"enabled": True, "repaired": 0, "reason": "calibration_db_missing", "db_path": str(store.db_path)}

    repaired = 0
    scanned = 0
    largest_before = 0.0
    largest_after = 0.0
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT prior_id, assumption_name, correction_mean, correction_std FROM calibration_priors"
        ).fetchall()
        for prior_id, assumption_name, correction_mean, correction_std in rows:
            scanned += 1
            base_assumption = str(assumption_name or "").split("@", 1)[0]
            try:
                mean_before = float(correction_mean)
            except (TypeError, ValueError):
                mean_before = 0.0
            low, high = assumption_residual_bounds(base_assumption)
            mean_after = clamp(mean_before, low, high)
            std_after = _bounded_std(base_assumption, correction_std)
            largest_before = max(largest_before, abs(mean_before))
            largest_after = max(largest_after, abs(mean_after))
            if mean_after != mean_before or std_after != correction_std:
                conn.execute(
                    "UPDATE calibration_priors SET correction_mean = ?, correction_std = ? WHERE prior_id = ?",
                    (mean_after, std_after, prior_id),
                )
                repaired += 1
        conn.commit()
    return {
        "enabled": True,
        "db_path": str(store.db_path),
        "scanned": scanned,
        "repaired": repaired,
        "largest_abs_before": round(largest_before, 6),
        "largest_abs_after": round(largest_after, 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clamp existing calibration priors to current residual bounds.")
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()
    print(json.dumps(sanitize_calibration_priors(args.db_path), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
